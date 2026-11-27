#!/usr/bin/env python3
"""Scheduled check of the platform's textbook catalog on the SUN host (stdlib only).

  catalog_refresh_host.py run [--force]      (systemd timer, daily)
  catalog_refresh_host.py status

1. Reads the live release (`data/catalog/CURRENT`) and the platform's `data_version.json`. When the
   platform's module_version equals the one the live release was built from, it records a heartbeat
   and stops: one small request.
2. Otherwise it downloads the parts named there, checks the platform's details record of every record in
   scope (cached in <state>/checked, refreshed after 30 days, a missing file after 3 days; one request per
   second, at most 3,500 per run) so that only books with a downloadable PDF are listed, builds a
   candidate with refresh_downloads.py,
   validates it (validate_release.py), installs it under `data/catalog/releases/<id>` (inert: CURRENT
   is never written here) and has the running container's own library code load it (read-only).
3. Guards decide `ready` (the Mac driver may switch to it) or `held` (only an owner-approved switch):
   rows withdrawn <= 5 % and added <= 20 % of the live list, no subject disappears, every row with a
   file held on this site is still listed, at least 1,000 rows. A candidate that fails validation or the
   load check is `rejected` and removed.
4. Keeps the 4 newest releases this job built plus CURRENT and the ids in keep.json (the driver's
   rollback); never touches releases built by hand. Run directories are kept for 30 days.

State: <state>/state.json (candidates), latest.json (newest candidate, read by the Mac driver),
heartbeat.jsonl. A second run while one is active exits at once (flock).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/root/cross-subject-knowledge")
STATE = ROOT / "state" / "catalog-refresh"
TOOL = Path(__file__).resolve().parent
VERSION_PATH = "/zxx/ndrs/resources/tch_material/version/data_version.json"
S_FILE_HOSTS = ("https://s-file-1.ykt.cbern.com.cn", "https://s-file-2.ykt.cbern.com.cn", "https://s-file-3.ykt.cbern.com.cn")
CONTAINER = "textbook-knowledge"
GUARDS = {"max_withdrawn_share": 0.05, "max_added_share": 0.20, "min_rows": 1000}
KEEP_BUILT = 4
RUN_DAYS = 30
UA = {"User-Agent": "Mozilla/5.0 (sun.bdfz.net catalog refresh)", "Accept": "application/json"}
DETAILS_PATH = "/zxx/ndrv2/resources/tch_material/details/{id}.json"
CHECK_TTL_OK, CHECK_TTL_MISSING, MAX_CHECKS, CHECK_PAUSE = 30 * 86400, 3 * 86400, 3500, 1.0


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def log(event: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "heartbeat.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now(), **event}, ensure_ascii=False) + "\n")


def get(url: str, timeout: float = 120.0) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        if r.status != 200:
            raise OSError(f"HTTP {r.status} {url}")
        return r.read()


def get_any(path_or_url: str) -> bytes:
    """A part URL names one host; on failure the same path is tried once on the other data hosts."""
    tried = []
    for host in ([None] + list(S_FILE_HOSTS)):
        url = path_or_url if host is None else host + "/" + path_or_url.split("/", 3)[3]
        if url in tried:
            continue
        tried.append(url)
        try:
            return get(url)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2)
    raise OSError(f"all hosts failed: {last}")


def check_files(items: list[dict]) -> dict:
    """Write <state>/checked/<id>.json for the records in scope whose check is missing or stale."""
    sys.path.insert(0, str(TOOL))
    import refresh_downloads as rd  # noqa: E402  (same tool directory, same rules)
    checked = STATE / "checked"
    checked.mkdir(parents=True, exist_ok=True)
    ids = [x["id"] for x in items if rd.in_scope(x)[0]]
    stats = {"in_scope": len(ids), "fetched": 0, "cached": 0, "errors": 0, "capped": 0}
    for sid in ids:
        f = checked / f"{sid}.json"
        old = read_json(f, None)
        if old:
            age = time.time() - datetime.strptime(old["checked_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            if age < (CHECK_TTL_OK if old.get("http_status") == 200 and old.get("pdf") else CHECK_TTL_MISSING):
                stats["cached"] += 1
                continue
        if stats["fetched"] + stats["errors"] >= MAX_CHECKS:
            stats["capped"] += 1
            continue
        result = None
        for host in S_FILE_HOSTS:
            try:
                body = json.loads(get(host + DETAILS_PATH.format(id=sid), 40))
                items_ = body.get("ti_items") or []
                pdf = [t for t in items_ if (t.get("ti_format") or "").lower() == "pdf"]
                size = next((int(t["ti_size"]) for t in pdf if t.get("ti_file_flag") == "source" and t.get("ti_size")), None)
                result = {"http_status": 200, "pdf": bool(pdf), "size": size, "status": body.get("status")}
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404):
                    result = {"http_status": exc.code, "pdf": False, "size": None}
                    break
            except Exception:  # noqa: BLE001
                continue
        time.sleep(CHECK_PAUSE)
        if result is None:
            stats["errors"] += 1
            continue
        write_json(f, {**result, "checked_at": now()})
        stats["fetched"] += 1
    return stats


def catalog_dir() -> Path:
    return ROOT / "data" / "catalog"


def current_id() -> str:
    return (catalog_dir() / "CURRENT").read_text(encoding="utf-8").strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_sums(release: Path) -> None:
    for line in (release / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        if sha256_file(release / name) != digest:
            raise ValueError("SHA256SUMS mismatch: " + name)


def guards(parent_rows: list[dict], refresh: dict) -> list[str]:
    diff, reasons = refresh["diff"], []
    n = max(1, len(parent_rows))
    if diff["withdrawn"] > GUARDS["max_withdrawn_share"] * n:
        reasons.append(f"withdrawn {diff['withdrawn']} > {GUARDS['max_withdrawn_share']:.0%} of {n}")
    if diff["added"] > GUARDS["max_added_share"] * n:
        reasons.append(f"added {diff['added']} > {GUARDS['max_added_share']:.0%} of {n}")
    lost = sorted(set(diff["subjects_before"]) - set(diff["subjects_after"]))
    if lost:
        reasons.append("subjects lost: " + ", ".join(lost))
    held = {r["source_id"] for r in parent_rows if any(s["kind"] == "vps" for s in r["sources"])}
    gone = sorted(held & set(diff["withdrawn_ids"]))
    if gone:
        reasons.append(f"{len(gone)} rows with a file held on this site would disappear")
    if refresh["rows"] < GUARDS["min_rows"]:
        reasons.append(f"only {refresh['rows']} rows")
    return reasons


LOAD_CHECK = r"""
import json, sys
sys.path.insert(0, '/app/backend')
import library
from pathlib import Path
s = library.Settings.from_env(Path('/data'))
r = library.load_release(s.catalog_root, s, sys.argv[1])
print(json.dumps({'release_id': r.release_id, 'downloads': len(r.downloads), 'editions': len(r.editions)}))
"""


def load_check(release_id: str) -> dict:
    out = subprocess.run(["docker", "exec", "-i", CONTAINER, "python", "-", release_id], input=LOAD_CHECK,
                         capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise ValueError("load check failed: " + (out.stderr or out.stdout)[-400:])
    return json.loads(out.stdout.strip().splitlines()[-1])


def prune(state: dict) -> list[str]:
    releases = catalog_dir() / "releases"
    keep = {current_id(), *(read_json(STATE / "keep.json", {}) or {}).get("ids", [])}
    built = []
    for d in releases.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            rel = read_json(d / "RELEASE.json", {}) or {}
            if "refresh" in rel and "refresh_downloads.py" in (rel["refresh"].get("tool") or {}):
                built.append(d)
    built.sort(key=lambda d: d.name, reverse=True)
    removed = []
    for d in built[KEEP_BUILT:]:
        if d.name not in keep:
            shutil.rmtree(d)
            removed.append(d.name)
    cutoff = time.time() - RUN_DAYS * 86400
    for d in sorted((STATE / "runs").glob("*")) if (STATE / "runs").exists() else []:
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d)
    return removed


def run(force: bool) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    lock = (STATE / ".lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log({"result": "busy"})
        return 0
    state = read_json(STATE / "state.json", {"candidates": []})
    live = current_id()
    parent = catalog_dir() / "releases" / live
    parent_release = read_json(parent / "RELEASE.json")
    parent_mv = (parent_release.get("refresh") or {}).get("module_version")
    version_raw, version = None, None
    for host in S_FILE_HOSTS:
        try:
            version_raw = get(host + VERSION_PATH, 40)
            version = json.loads(version_raw)
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
    if version is None:
        log({"result": "error", "step": "data_version", "error": str(last)[:200]})
        return 2
    mv = version.get("module_version")
    if mv == parent_mv and not force:
        log({"result": "no_change", "module_version": mv, "live": live})
        return 0
    known = [c for c in state["candidates"] if c["module_version"] == mv and c["parent"] == live and c["status"] in ("ready", "held")]
    if known and not force:
        log({"result": "candidate_exists", "module_version": mv, "candidate": known[-1]["release_id"], "status": known[-1]["status"]})
        return 0
    stamp = now()
    run_dir = STATE / "runs" / stamp.replace(":", "")
    parts = run_dir / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    (parts / "data_version.json").write_bytes(version_raw)
    urls = version["urls"].split(",") if isinstance(version.get("urls"), str) else list(version.get("urls") or [])
    try:
        for n, url in enumerate(urls, 1):
            body = get_any(url.strip())
            if not isinstance(json.loads(body), list):
                raise ValueError(f"part {n} is not a list")
            (parts / f"part_{n}.json").write_bytes(body)
    except Exception as exc:  # noqa: BLE001
        log({"result": "error", "step": "parts", "module_version": mv, "error": str(exc)[:200]})
        return 2
    items = []
    for n in range(1, len(urls) + 1):
        items += json.loads((parts / f"part_{n}.json").read_bytes())
    checks = check_files(items)
    log({"result": "checked", "module_version": mv, **checks})
    out = run_dir / "out"
    out.mkdir(exist_ok=True)
    built = subprocess.run([sys.executable, str(TOOL / "refresh_downloads.py"), "--parent", str(parent), "--parts", str(parts),
                            "--checked", str(STATE / "checked"), "--out", str(out), "--stamp", stamp], capture_output=True, text=True, timeout=900)
    (run_dir / "build.log").write_text(built.stdout + built.stderr, encoding="utf-8")
    record = {"module_version": mv, "parent": live, "built_at": stamp, "run": str(run_dir), "file_checks": checks}
    if built.returncode != 0:
        record.update(status="rejected", reasons=["build failed: " + (built.stderr or built.stdout)[-300:]])
        return finish(state, record, 2)
    release_id = json.loads(built.stdout.strip().splitlines()[-1])["release_id"]
    candidate = out / release_id
    record["release_id"] = release_id
    valid = subprocess.run([sys.executable, str(TOOL / "validate_release.py"), str(candidate)], capture_output=True, text=True, timeout=600)
    (run_dir / "validate.log").write_text(valid.stdout + valid.stderr, encoding="utf-8")
    if valid.returncode != 0:
        record.update(status="rejected", reasons=["validate_release failed: " + valid.stdout[-300:]])
        return finish(state, record, 2)
    refresh = read_json(candidate / "RELEASE.json")["refresh"]
    parent_rows = read_json(parent / "downloads.json")["rows"]
    target = catalog_dir() / "releases" / release_id
    if not target.exists():
        tmp = target.with_name("." + release_id + ".installing")
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(candidate, tmp)
        check_sums(tmp)
        os.rename(tmp, target)
    try:
        loaded = load_check(release_id)
    except Exception as exc:  # noqa: BLE001
        if target.exists() and release_id != current_id():
            shutil.rmtree(target)
        record.update(status="rejected", reasons=[str(exc)[:300]])
        return finish(state, record, 2)
    reasons = guards(parent_rows, refresh)
    diff = refresh["diff"]
    record.update(status="held" if reasons else "ready", reasons=reasons, loaded=loaded, rows=refresh["rows"],
                  parent_rows=len(parent_rows), added=diff["added"], withdrawn=diff["withdrawn"], changed=diff["changed"],
                  added_by_stage_subject=diff["added_by_stage_subject"], withdrawn_by_stage_subject=diff["withdrawn_by_stage_subject"],
                  superseded=len(refresh["superseded_left_out"]), subjects_new=sorted(set(diff["subjects_after"]) - set(diff["subjects_before"])),
                  superseded_withdrawn=len({x["source_id"] for x in refresh["superseded_left_out"]} & set(diff["withdrawn_ids"])),
                  added_ids_sample=diff["added_ids"][:3],
                  release_sha256sums=sha256_file(target / "SHA256SUMS"))
    shutil.rmtree(out)                 # the installed copy is the candidate; keep parts and logs only
    return finish(state, record, 0)


def finish(state: dict, record: dict, code: int) -> int:
    state["candidates"] = (state["candidates"] + [record])[-30:]
    removed = prune(state)
    write_json(STATE / "state.json", state)
    write_json(STATE / "latest.json", record)
    log({"result": record["status"], "module_version": record["module_version"], "candidate": record.get("release_id"),
         "reasons": record.get("reasons", []), "pruned": removed})
    return code


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("run", "status"))
    p.add_argument("--force", action="store_true", help="build even when the platform version is unchanged")
    args = p.parse_args(argv)
    if args.command == "status":
        print(json.dumps({"live": current_id(), "latest": read_json(STATE / "latest.json")}, ensure_ascii=False, indent=1))
        return 0
    return run(args.force)


if __name__ == "__main__":
    sys.exit(main())
