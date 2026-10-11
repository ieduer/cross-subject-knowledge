#!/usr/bin/env python3
"""Bounded corpus-text sampling from SUN's public read-only search API (for legacy page anchors).

SUN search costs 6–9 s per uncached call on a single worker and logs every query, so this job is
deliberately small: <= --per-book searches per book (limit=30, source=textbook), >= --pause s
apart, <= --max-total over the life of the cache directory (ledger), cached forever, and it stops
early for a book once --target anchors are located and they reach the last 15 % of the book. Two consecutive non-200 answers stop the job.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BROWSER_UA, jc_short_key, read_json, utc_now, write_json  # noqa: E402

SEARCH_URL = "https://sun.bdfz.net/api/search"
PAGE_URL_RE = re.compile(r"/pages/([0-9a-f]{12})/p(\d+)\.webp")
TERMS = {
    "语文": ["我们", "什么", "没有"],
    "数学": ["我们", "可以", "如图"],
    "英语": ["about", "people", "with"],
    "物理": ["我们", "可以", "实验"],
    "化学": ["我们", "可以", "实验"],
    "生物学": ["我们", "可以", "细胞"],
    "地理": ["我们", "可以", "地区"],
    "历史": ["我们", "发展", "人民"],
    "思想政治": ["我们", "发展", "人民"],
}
DEFAULT_TERMS = ["我们", "可以", "什么"]


def book_dir(cache: Path, book_key: str) -> Path:
    return cache / jc_short_key(book_key)


def load_hits(cache: Path, book_key: str) -> tuple[list[dict], list[dict]]:
    """All cached hits of a book as [{legacy_index, text, section, id}] plus a search summary."""
    hits, searches = [], []
    short = jc_short_key(book_key)
    d = book_dir(cache, book_key)
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        rec = read_json(f)
        n = skipped = 0
        for group in (rec.get("body") or {}).get("groups") or []:
            for r in group.get("results") or []:
                if r.get("book_key") != book_key or not r.get("text"):
                    skipped += 1
                    continue
                m = PAGE_URL_RE.search(r.get("page_url") or "")
                section = r.get("section")
                if m:
                    if m.group(1) != short or (section is not None and int(section) != int(m.group(2))):
                        skipped += 1  # the hit does not point at this book's page image
                        continue
                    legacy = int(m.group(2))
                elif section is not None:
                    legacy = int(section)
                else:
                    skipped += 1
                    continue
                hits.append({"legacy_index": legacy, "text": r["text"], "id": r.get("id")})
                n += 1
        searches.append({"q": rec.get("q"), "status": rec.get("status"), "usable_hits": n,
                         "skipped_hits": skipped, "retrieved_at": rec.get("retrieved_at")})
    return hits, searches


def get(url: str, timeout: float = 90.0):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), time.monotonic() - t0
    except urllib.error.HTTPError as exc:
        return exc.code, b"", time.monotonic() - t0
    except (urllib.error.URLError, OSError):
        return 0, b"", time.monotonic() - t0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", required=True)
    p.add_argument("--text-cache", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--max-total", type=int, required=True)
    p.add_argument("--per-book", type=int, default=3)
    p.add_argument("--pause", type=float, default=5.0)
    p.add_argument("--target", type=int, default=20, help="anchors per book that end sampling early")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    from inspect_pdf import cache_path
    from page_map import LEGACY_END_FRACTION, build_shingle_index, collect_anchors

    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    ledger_path = cache / "_ledger.json"
    ledger = read_json(ledger_path, default={"requests": 0, "non_200": 0, "log": []})
    per_book = min(args.per_book, 3)
    pause = max(args.pause, 5.0)
    plan = read_json(args.plan)
    consecutive = 0
    last_request = 0.0
    for ed in plan["editions"]:
        spec = ed.get("legacy_spec") or {}
        if spec.get("mode") != "anchors" or not ed.get("pdf"):
            continue
        book_key = spec["sun_book_key"]
        d = book_dir(cache, book_key)
        tp = cache_path(Path(args.text_cache), ed["pdf"]["sha256"])
        if not tp.exists():
            print(f"skip {jc_short_key(book_key)}: PDF not inspected yet")
            continue
        text_rec = read_json(tp)
        if not text_rec.get("has_text_layer"):
            continue  # nothing to compare against; no search is spent
        index = build_shingle_index([pg.get("text") or "" for pg in text_rec["page_texts"]])
        terms = TERMS.get(spec.get("subject") or "", DEFAULT_TERMS)[:per_book]
        for n, term in enumerate(terms):
            hits, _ = load_hits(cache, book_key)
            found = collect_anchors(index, hits)["anchors"]
            end = int((spec.get("legacy_pages") or 0) * LEGACY_END_FRACTION)
            if len(found) >= args.target and max(a["legacy_index"] for a in found) >= end:
                break  # enough anchors and they reach the last 15 % of the book
            target = d / f"{n}.json"
            if target.exists():
                continue
            if args.dry_run:
                print(f"would search {jc_short_key(book_key)} q={term}")
                continue
            if ledger["requests"] >= args.max_total:
                print(f"STOP cap searches={args.max_total}")
                write_json(ledger_path, ledger)
                return 4
            wait = pause - (time.monotonic() - last_request)
            if last_request and wait > 0:
                time.sleep(wait)
            url = SEARCH_URL + "?" + urllib.parse.urlencode(
                {"q": term, "book_key": book_key, "limit": 30, "source": "textbook"})
            status, body, secs = get(url)
            last_request = time.monotonic()
            ledger["requests"] += 1
            ledger["log"].append({"at": utc_now(), "book": jc_short_key(book_key), "q": term,
                                  "status": status, "seconds": round(secs, 2)})
            if status != 200:
                ledger["non_200"] += 1
                consecutive += 1
                write_json(ledger_path, ledger)
                print(f"non-200 {status} {jc_short_key(book_key)} q={term}")
                if consecutive >= 2:
                    print("STOP two consecutive non-200")
                    return 3
                continue
            consecutive = 0
            try:
                parsed = json.loads(body.decode("utf-8"))
            except ValueError:
                write_json(ledger_path, ledger)
                continue
            write_json(target, {"book_key": book_key, "q": term, "status": status,
                                "retrieved_at": utc_now(), "seconds": round(secs, 2), "body": parsed})
            write_json(ledger_path, ledger)
            hits, _ = load_hits(cache, book_key)
            print(f"{jc_short_key(book_key)} q={term} {secs:.1f}s hits={len(hits)} "
                  f"anchors={len(collect_anchors(index, hits)['anchors'])} total={ledger['requests']}",
                  flush=True)
    write_json(ledger_path, ledger)
    print(f"done ledger_requests={ledger['requests']} non_200={ledger['non_200']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
