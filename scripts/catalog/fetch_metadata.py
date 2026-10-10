#!/usr/bin/env python3
"""Bounded, cached, strictly sequential metadata GETs (no PDF bytes).

  details   : SmartEdu details JSON per source id  -> <out>/<id>.json
  downloads : jks-ai /search per (phase, subject)  -> <out>/<phase>__<subject>.json

Each cached file wraps the body: {"url", "status", "retrieved_at", "headers", "body"}.
A cached item is never fetched again. A ledger in <out>/_ledger.json counts every request ever
made into that directory and enforces --max across re-runs. Two consecutive non-200 answers stop
the job (exit 3); nothing is retried with another client or identity.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BROWSER_UA, UUID_RE, read_json, utc_now, write_json  # noqa: E402

DETAILS_URL = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{id}.json"
SEARCH_URL = "https://jks-ai.bdfz.net/search"
PHASES = ["小学", "初中", "高中"]
SUBJECTS = ["语文", "数学", "英语", "物理", "化学", "生物", "历史", "地理", "思想政治"]


def get(url: str, timeout: float = 40.0) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {}, b""
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": type(exc).__name__}, b""


def run(tasks: list[tuple[str, str]], out_dir: Path, max_requests: int, pause: float) -> int:
    """tasks: [(cache_name, url)]"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = out_dir / "_ledger.json"
    ledger = read_json(ledger_path, default={"requests": 0, "non_200": 0, "log": []})
    consecutive = 0
    fetched = cached = 0
    first = True
    for name, url in tasks:
        target = out_dir / f"{name}.json"
        if target.exists():
            cached += 1
            continue
        if ledger["requests"] >= max_requests:
            print(f"STOP cap requests={max_requests}")
            write_json(ledger_path, ledger)
            return 4
        if not first:
            time.sleep(pause)
        first = False
        status, headers, body = get(url)
        ledger["requests"] += 1
        ledger["log"].append({"at": utc_now(), "name": name, "status": status})
        if status != 200:
            ledger["non_200"] += 1
            consecutive += 1
            write_json(ledger_path, ledger)
            print(f"non-200 {status} {name}")
            if consecutive >= 2:
                print("STOP two consecutive non-200")
                return 3
            continue
        consecutive = 0
        try:
            parsed = json.loads(body.decode("utf-8"))
        except ValueError:
            ledger["log"][-1]["status"] = "not-json"
            write_json(ledger_path, ledger)
            continue
        keep = {k: headers[k] for k in ("etag", "last-modified", "content-length", "cache-control",
                                        "x-jks-catalog-version", "x-jks-search-source") if k in headers}
        write_json(target, {"url": url, "status": status, "retrieved_at": utc_now(),
                            "headers": keep, "body": parsed})
        write_json(ledger_path, ledger)
        fetched += 1
    write_json(ledger_path, ledger)
    print(f"fetched={fetched} cached={cached} ledger_requests={ledger['requests']} non_200={ledger['non_200']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("details")
    d.add_argument("--ids", required=True, help="JSON list of source ids")
    d.add_argument("--out", required=True)
    d.add_argument("--max", type=int, required=True)
    d.add_argument("--pause", type=float, default=1.0)
    s = sub.add_parser("downloads")
    s.add_argument("--out", required=True)
    s.add_argument("--max", type=int, required=True)
    s.add_argument("--pause", type=float, default=1.0)
    args = p.parse_args(argv)
    if args.cmd == "details":
        ids = read_json(args.ids)
        bad = [i for i in ids if not UUID_RE.match(i)]
        if bad:
            print(f"bad ids: {bad[:3]}", file=sys.stderr)
            return 2
        tasks = [(i, DETAILS_URL.format(id=i)) for i in ids]
    else:
        tasks = [(f"{ph}__{sj}", SEARCH_URL + "?" + urllib.parse.urlencode({"phase": ph, "subject": sj}))
                 for ph in PHASES for sj in SUBJECTS]
    return run(tasks, Path(args.out), args.max, max(args.pause, 1.0))


if __name__ == "__main__":
    sys.exit(main())
