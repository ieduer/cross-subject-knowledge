#!/usr/bin/env python3
"""Candidate current-edition pages for legacy lesson pages that have no proven row (proposals only).

For each (reader book, legacy index): the corpus text of that legacy page (offline database) is
compared with the text layer of every page of the edition named for the book; the three best
pages are listed with a similarity score, excerpts from both sides and the lesson the legacy page
belongs to. Nothing here is written into a page map: the result is for a person to confirm.

similarity = share of the legacy page's 8-character shingles found on the PDF page (0..1).
verdict:  confident   best >= 0.50 and at least twice the second best
          spans-two-pages  the two best pages are adjacent and together hold >= 0.60 of the text
                           (the page was re-flowed across a page break)
          probable    best >= 0.30 and at least 1.5 times the second best
          unclear     some overlap, no page stands out
          no-counterpart   no legacy text, or best < 0.10
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import fold, read_json, write_json  # noqa: E402
from inspect_pdf import cache_path  # noqa: E402
from page_map import SHINGLE, SHINGLE_STEP, corpus_page_texts  # noqa: E402

REF_RE = re.compile(r"pages/([0-9a-f]{12})/p(\d+)\.webp")


def excerpt(text: str, n: int = 120) -> str:
    return " ".join((text or "").split())[:n]


def lesson_of(toc: list[dict], index: int) -> str | None:
    """Title of the last table-of-contents entry (below unit level) that starts at or before the page."""
    best = None
    for t in toc or []:
        if t.get("level", 2) >= 2 and isinstance(t.get("page"), int) and t["page"] <= index:
            if best is None or t["page"] >= best["page"]:
                best = t
    return best["label"] if best else None


def title_pages(title: str | None, folded_pages: list[str]) -> list[int]:
    """PDF pages on which the lesson title (its longest run of CJK characters) occurs."""
    if not title:
        return []
    runs = re.findall(r"[一-鿿]{2,}", fold(title.split("/")[0]))
    probe = max(runs, key=len) if runs else None
    if not probe or len(probe) < 3:
        return []
    return [i + 1 for i, f in enumerate(folded_pages) if probe in f]


def score_pages(legacy_text: str, folded_pages: list[str]) -> list[tuple[float, int]]:
    f = fold(legacy_text)
    shingles = [f[j:j + SHINGLE] for j in range(0, max(0, len(f) - SHINGLE + 1), SHINGLE_STEP)]
    if not shingles:
        return []
    out = []
    for i, page in enumerate(folded_pages):
        hit = sum(1 for sh in shingles if sh in page)
        if hit:
            out.append((round(hit / len(shingles), 4), i + 1))
    out.sort(key=lambda t: (-t[0], t[1]))
    return out


def verdict(scores: list[tuple[float, int]], has_text: bool) -> str:
    if not has_text or not scores or scores[0][0] < 0.10:
        return "no-counterpart"
    best = scores[0][0]
    second = scores[1][0] if len(scores) > 1 else 0.0
    if best >= 0.50 and best >= 2 * second:
        return "confident"
    if len(scores) > 1 and abs(scores[0][1] - scores[1][1]) == 1 and best + second >= 0.60:
        third = scores[2][0] if len(scores) > 2 else 0.0
        if best + second >= 4 * third:
            return "spans-two-pages"
    if best >= 0.30 and best >= 1.5 * second:
        return "probable"
    return "unclear"


def candidates(book: dict, index: int, corpus: dict[int, str], pdf_pages: list[str], mapped: dict[int, int]) -> dict:
    text = corpus.get(index, "")
    folded = [fold(t) for t in pdf_pages]
    scores = score_pages(text, folded)
    v = verdict(scores, bool(fold(text)))
    title = lesson_of(book.get("toc"), index)
    known = sorted(mapped)
    before = max((k for k in known if k < index), default=None)
    after = min((k for k in known if k > index), default=None)
    # what the current edition prints where this page used to be: the PDF pages strictly between
    # the pages of the nearest mapped neighbours
    between = []
    if before is not None and after is not None:
        between = list(range(mapped[before] + 1, mapped[after]))
    # pages of the current edition near that position that no legacy page maps to: new material
    used = set(mapped.values())
    lo = mapped[before] if before is not None else 1
    hi = mapped[after] if after is not None else len(pdf_pages)
    new_nearby = [p for p in range(max(1, lo - 8), min(len(pdf_pages), hi + 8) + 1) if p not in used]
    out = {
        "jc_short_key": book["shortKey"], "book": book["title"], "legacy_image_index": index,
        "legacy_lesson": title, "legacy_text_chars": len(fold(text)), "legacy_excerpt": excerpt(text),
        "verdict": v,
        "candidates": [{"pdf_page": p, "similarity": s, "pdf_excerpt": excerpt(pdf_pages[p - 1])}
                       for s, p in scores[:3]],
        "lesson_title_found_on_pdf_pages": title_pages(title, folded)[:12],
        "nearest_mapped_before": {"legacy_image_index": before, "pdf_page": mapped[before]} if before is not None else None,
        "nearest_mapped_after": {"legacy_image_index": after, "pdf_page": mapped[after]} if after is not None else None,
        "current_edition_pages_in_that_position": [
            {"pdf_page": p, "pdf_excerpt": excerpt(pdf_pages[p - 1])} for p in between[:6]],
        "current_edition_pages_nearby_without_a_legacy_counterpart": [
            {"pdf_page": p, "pdf_excerpt": excerpt(pdf_pages[p - 1], 60)} for p in new_nearby[:10]],
    }
    if v == "no-counterpart":
        out["evidence"] = ("the legacy page has no corpus text (blank or unprinted page)" if not fold(text) else
                           f"no page of the current edition shares more than {scores[0][0] if scores else 0:.0%} of its text")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("release_dir")
    p.add_argument("--jc-catalog", required=True)
    p.add_argument("--corpus-db", required=True)
    p.add_argument("--text-cache", required=True)
    p.add_argument("--refs", required=True, help="text file with the page-image URLs a consumer references")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    release = Path(args.release_dir)
    cat = read_json(release / "catalog.json")
    by_id = {e["edition_id"]: e for e in cat["editions"]}
    owner = {r["jc_short_key"]: by_id[r["edition_id"]] for r in cat["reader_books"]}
    jc = {b["shortKey"]: b for b in read_json(args.jc_catalog)["books"]}
    refs = sorted({(m.group(1), int(m.group(2))) for m in REF_RE.finditer(Path(args.refs).read_text())})
    rows, cache = [], {}
    for short, index in refs:
        ed = owner[short]
        if short not in cache:
            pm = read_json(release / "page-maps" / f"{ed['edition_id']}.json")
            mapped = {r["legacy_image_index"]: r["pdf_page"] for r in pm["pages"] if r["legacy_image_index"] is not None}
            texts = [pg.get("text") or "" for pg in read_json(cache_path(Path(args.text_cache), ed["pdf"]["sha256"]))["page_texts"]]
            cache[short] = (mapped, texts, corpus_page_texts(args.corpus_db, jc[short]["id"]))
        mapped, texts, corpus = cache[short]
        if index in mapped:
            continue
        row = candidates(jc[short], index, corpus, texts, mapped)
        row["edition_id"] = ed["edition_id"]
        rows.append(row)
    from collections import Counter
    summary = dict(sorted(Counter(r["verdict"] for r in rows).items()))
    write_json(args.out, {"release_id": cat["release_id"], "note": "proposals for confirmation; not part of any page map",
                          "referenced_pages": len(refs), "unmapped_pages": len(rows), "by_verdict": summary, "pages": rows})
    print(f"referenced={len(refs)} unmapped={len(rows)} {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
