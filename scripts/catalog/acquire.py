#!/usr/bin/env python3
"""Bounded, resumable PDF acquisition for the sun-catalog-v1 pipeline.

Job file : {"items": [{"source_id", "ti_md5"|null, "ti_size"|null, ...}]}
State    : {"items": {source_id: {"state": pending|downloading|verified|failed:<reason>, ...}}}
Store    : <store>/<sha256>.pdf, one file per distinct content hash.
Partials : <store>/.partial/<source_id>.part (resumed with a Range request).

Exit codes: 0 every job item verified · 1 at least one item failed · 2 usage / input error
            3 stopped because the upstream refused twice in a row · 4 stopped by a cap
"""
from __future__ import annotations

import argparse
import http.client
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BROWSER_UA, MD5_RE, file_hashes, read_json, utc_now, write_json  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED, EXIT_CAP = 0, 1, 2, 3, 4
CHUNK = 256 * 1024


class Refused(Exception):
    """Upstream answered with a status other than 200/206."""

    def __init__(self, status: int):
        super().__init__(f"http-{status}")
        self.status = status


class ItemError(Exception):
    pass


def log(fh, msg: str) -> None:
    line = f"{utc_now()} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def load_state(path: Path) -> dict:
    state = read_json(path, default={"version": 1, "items": {}})
    state.setdefault("items", {})
    return state


def fetch_once(url: str, part: Path, ua: str, deadline: float, sock_timeout: float) -> dict:
    """One HTTP attempt. Appends to `part` when the server honours Range. Returns HTTP facts."""
    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": ua, "Accept": "application/pdf,*/*;q=0.8"}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=sock_timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:
            # Nothing left to send: the partial is already complete; verification decides.
            return {"status": 416, "resumed_from": have, "received": 0, "headers": {}}
        raise Refused(exc.code) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise ItemError(f"network:{type(exc).__name__}") from None
    with resp:
        status = resp.status
        hdr = {k.lower(): v for k, v in resp.headers.items()}
        if status == 206 and have:
            crange = hdr.get("content-range", "")
            if not crange.startswith(f"bytes {have}-"):
                raise ItemError("bad-content-range")
            mode = "ab"
        elif status == 200:
            mode = "wb"  # server ignored Range (or fresh start): restart from byte 0
            have = 0
        else:
            raise Refused(status)
        received = 0
        part.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(part, mode) as out:
                while True:
                    if time.monotonic() > deadline:
                        raise ItemError("timeout")
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    received += len(chunk)
                out.flush()
                os.fsync(out.fileno())
        except ItemError:
            raise
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            raise ItemError(f"network:{type(exc).__name__}") from None
    return {
        "status": status,
        "resumed_from": have,
        "received": received,
        "headers": {
            k: hdr.get(k)
            for k in ("etag", "last-modified", "content-length", "content-range", "content-type",
                      "content-disposition", "cache-control")
            if hdr.get(k) is not None
        },
    }


def verify_part(part: Path, item: dict) -> dict:
    """Size / magic / MD5 checks against the job expectations. Raises ItemError on mismatch."""
    if not part.exists() or part.stat().st_size == 0:
        raise ItemError("empty")
    with open(part, "rb") as fh:
        if fh.read(5) != b"%PDF-":
            raise ItemError("not-pdf")
    h = file_hashes(part)
    exp_size = item.get("ti_size")
    if exp_size is not None and h["bytes"] < int(exp_size):
        raise ItemError(f"short:{h['bytes']}<{int(exp_size)}")  # partial is kept and resumed
    if exp_size is not None and h["bytes"] > int(exp_size):
        raise ItemError(f"size-mismatch:{h['bytes']}>{int(exp_size)}")
    exp_md5 = (item.get("ti_md5") or "").lower()
    if MD5_RE.match(exp_md5):
        if exp_md5 != h["md5"]:
            raise ItemError("md5-mismatch")
        h["md5_check"] = "equal"
    elif exp_md5:
        h["md5_check"] = "multipart-etag:size-only"
    else:
        h["md5_check"] = "no-expected-md5"
    h["size_check"] = "equal" if exp_size is not None else "no-expected-size"
    return h


def run(args) -> int:
    job_path, state_path, store = Path(args.job), Path(args.state), Path(args.store)
    try:
        job = read_json(job_path)
        items = job["items"]
        assert isinstance(items, list)
        for it in items:
            assert isinstance(it["source_id"], str) and it["source_id"]
            assert "/" not in it["source_id"] and ".." not in it["source_id"]
    except Exception as exc:  # noqa: BLE001
        print(f"bad job file: {exc}", file=sys.stderr)
        return EXIT_USAGE
    state = load_state(state_path)
    st_items = state["items"]
    for it in items:
        st_items.setdefault(it["source_id"], {"state": "pending", "attempts": 0})

    def todo(it) -> bool:
        st = st_items[it["source_id"]]
        if st["state"] == "verified":
            if (store / f"{st['sha256']}.pdf").exists():
                return False
            st["state"] = "pending"  # the stored file vanished: honest state, fetch again
            return True
        if st["state"].startswith("failed:"):
            return bool(args.retry_failed)
        return True

    plan = [it for it in items if todo(it)]
    if args.dry_run:
        known = sum(int(it["ti_size"]) for it in plan if it.get("ti_size") is not None)
        print(f"dry-run: job={len(items)} to_fetch={len(plan)} "
              f"already_verified={len(items) - len(plan)} expected_bytes_known={known} "
              f"caps: count={args.max_count} bytes={args.max_bytes} seconds={args.max_seconds}")
        for it in plan:
            print(f"  would GET {args.base_url}{it['source_id']} "
                  f"expect size={it.get('ti_size')} md5={it.get('ti_md5')}")
        return EXIT_OK

    store.mkdir(parents=True, exist_ok=True)
    write_json(state_path, state)
    logfh = open(args.log, "a", encoding="utf-8") if args.log else None
    started = time.monotonic()
    run_stats = {"attempted": 0, "verified": 0, "failed": 0, "bytes_received": 0, "requests": 0}
    consecutive_refusals = 0
    stop_code = None
    first_request = True

    for it in plan:
        sid = it["source_id"]
        st = st_items[sid]
        if run_stats["attempted"] >= args.max_count:
            stop_code = EXIT_CAP
            log(logfh, f"STOP cap count={args.max_count}")
            break
        if time.monotonic() - started > args.max_seconds:
            stop_code = EXIT_CAP
            log(logfh, f"STOP cap seconds={args.max_seconds}")
            break
        part = store / ".partial" / f"{sid}.part"
        have = part.stat().st_size if part.exists() else 0
        expect = int(it["ti_size"]) if it.get("ti_size") is not None else None
        remaining = max(0, expect - have) if expect is not None else 0
        if run_stats["bytes_received"] + remaining > args.max_bytes:
            stop_code = EXIT_CAP
            log(logfh, f"STOP cap bytes={args.max_bytes} (next item needs {remaining})")
            break

        run_stats["attempted"] += 1
        url = f"{args.base_url}{sid}"
        outcome = None
        for attempt in (1, 2):  # one automatic retry, then failed
            if not first_request:
                time.sleep(args.pause)
            first_request = False
            st.update({"state": "downloading", "attempts": st.get("attempts", 0) + 1,
                       "last_attempt_at": utc_now()})
            write_json(state_path, state)
            run_stats["requests"] += 1
            t0 = time.monotonic()
            try:
                http = fetch_once(url, part, args.user_agent, t0 + args.timeout, args.socket_timeout)
                run_stats["bytes_received"] += http["received"]
                consecutive_refusals = 0
                facts = verify_part(part, it)
                final = store / f"{facts['sha256']}.pdf"
                if final.exists():
                    part.unlink()  # same content already stored once
                    deduped = True
                else:
                    os.replace(part, final)
                    deduped = False
                st.clear()
                st.update({
                    "state": "verified", "attempts": attempt, "sha256": facts["sha256"],
                    "md5": facts["md5"], "bytes": facts["bytes"], "md5_check": facts["md5_check"],
                    "size_check": facts["size_check"], "retrieved_at": utc_now(),
                    "retrieved_via": url, "http": http, "deduplicated": deduped,
                    "seconds": round(time.monotonic() - t0, 2),
                })
                outcome = "verified"
                log(logfh, f"VERIFIED {sid} bytes={facts['bytes']} sha256={facts['sha256'][:16]} "
                           f"md5={facts['md5_check']} status={http['status']} "
                           f"resumed_from={http['resumed_from']} s={st['seconds']}")
                break
            except Refused as exc:
                consecutive_refusals += 1
                outcome = f"failed:http-{exc.status}"
                log(logfh, f"REFUSED {sid} status={exc.status} attempt={attempt} "
                           f"consecutive={consecutive_refusals}")
                if consecutive_refusals >= 2:
                    break
            except ItemError as exc:
                reason = str(exc)
                outcome = f"failed:{reason}"
                log(logfh, f"ERROR {sid} {reason} attempt={attempt}")
                if reason.split(":")[0] in ("md5-mismatch", "size-mismatch", "not-pdf", "empty",
                                            "bad-content-range"):
                    # Content is wrong, not merely short: never keep it, restart from byte 0.
                    if part.exists():
                        part.unlink()
        if outcome == "verified":
            run_stats["verified"] += 1
        else:
            st["state"] = outcome or "failed:unknown"
            st["failed_at"] = utc_now()
            run_stats["failed"] += 1
        write_json(state_path, state)
        if consecutive_refusals >= 2:
            stop_code = EXIT_REFUSED
            log(logfh, "STOP two consecutive refusals; no client or identity rotation")
            break

    run_stats["seconds"] = round(time.monotonic() - started, 1)
    run_stats["finished_at"] = utc_now()
    run_stats["stop"] = {None: None, EXIT_CAP: "cap", EXIT_REFUSED: "refused"}[stop_code]
    state.setdefault("runs", []).append(run_stats)
    write_json(state_path, state)
    log(logfh, f"RUN {run_stats}")
    if logfh:
        logfh.close()
    if stop_code is not None:
        return stop_code
    all_ok = all(st_items[it["source_id"]]["state"] == "verified" for it in items)
    return EXIT_OK if all_ok else EXIT_FAILED


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--job", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--store", required=True, help="directory for <sha256>.pdf")
    p.add_argument("--base-url", default="https://jks-ai.bdfz.net/pdf/")
    p.add_argument("--max-count", type=int, required=True, help="max items attempted in this run")
    p.add_argument("--max-bytes", type=int, required=True, help="max bytes received in this run")
    p.add_argument("--max-seconds", type=float, required=True, help="no new item after this many seconds")
    p.add_argument("--pause", type=float, default=3.0, help="seconds between requests (>=3 in production)")
    p.add_argument("--timeout", type=float, default=1200.0, help="per-file wall clock limit")
    p.add_argument("--socket-timeout", type=float, default=60.0)
    p.add_argument("--user-agent", default=BROWSER_UA)
    p.add_argument("--retry-failed", action="store_true", help="re-attempt items already marked failed")
    p.add_argument("--dry-run", action="store_true", help="print the plan; no network request")
    p.add_argument("--log")
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
