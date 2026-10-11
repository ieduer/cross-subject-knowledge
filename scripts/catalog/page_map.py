#!/usr/bin/env python3
"""Per-edition page maps for sun-catalog-v1.

Every row is data: {pdf_page (1-based), printed_label|null, printed_verified, legacy_image_index|null,
ocr_page|null}. There is no library-wide or per-book constant taken from another catalog.

Printed pages — whole-book folio verification (ported from keben `folioCheck`, thresholds unchanged):
  the printed folio is read from one zone of each page (first lines of the text layer, or the
  position-true top / bottom strip); the folios must fit ONE mapping pdf_page -> folio
  (folio = pdf_page - c; a later segment may restart at folio 1 and is recorded), with
    detected >= 60 % of body pages, matched >= 95 % of detected, last match in the final 15 %.
  A book that fails gets printed_label = null on every page.

Legacy page-image index — two admissible proofs only:
  same-bytes : the PDF is the file the images were rendered from and page counts are equal
               (legacy = pdf_page - 1);
  anchors    : corpus text of a legacy page is located on exactly one PDF page by shingle match;
               >= 8 distinct legacy pages must agree on a single mapping and none may contradict.
               Rows are filled on anchored pages; between two anchors at most 16 pages apart;
               before the first anchor only at offset 0 and within 16 pages; after the last anchor
               only when the anchors reach the last 15 %, both sequences end together and the tail
               is within 16 pages. Every row records which of these it is (`legacy_basis`); all
               other rows are null. `aligned` needs anchors to the end and no wide stretch.
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PAGE_MAP_VERSION, fold, read_json, write_json  # noqa: E402

# --- folio verification -------------------------------------------------------------------

FOLIO_MIN_COVERAGE = 0.60
FOLIO_MIN_RATE = 0.95
FOLIO_END_FRACTION = 0.85
RESTART_MIN_MATCHES = 10
MAX_RESTARTS = 3
ZONES = ("head", "top", "bottom")

_LEAD = re.compile(r"^(\d{1,3})(?=\D|$)")
_TRAIL = re.compile(r"(?:^|\D)(\d{1,3})$")
_KEBEN = re.compile(r"^(?:\D*?)(\d{1,3})$")


def folio_candidates(lines) -> set[int]:
    """Numbers a line starts or ends with (1–3 digits, not part of a longer number)."""
    out: set[int] = set()
    for raw in lines:
        line = unicodedata.normalize("NFKC", str(raw)).strip()
        if not line:
            continue
        for rx in (_LEAD, _TRAIL, _KEBEN):
            m = rx.search(line)
            if m:
                out.add(int(m.group(1)))
    out.discard(0)
    return out


def page_zone_candidates(page_rec: dict) -> dict[str, set[int]]:
    head = [l.strip() for l in (page_rec.get("text") or "").split("\n") if l.strip()][:3]
    margin = page_rec.get("margin") or {}
    return {
        "head": folio_candidates(head),
        "top": folio_candidates(margin.get("top") or []),
        "bottom": folio_candidates(margin.get("bottom") or []),
    }


def _evaluate(cands: list[set[int]], segments: list[tuple[int, int]], total: int) -> dict:
    """cands[i] = candidates of pdf page i+1. segments = [(start_pdf_page, c)], ascending."""
    first_body = segments[0][0]
    body = detected = match = last = 0
    off = []
    per_page: dict[int, tuple[int, bool]] = {}
    for p in range(first_body, total + 1):
        c = [s for s in segments if s[0] <= p][-1][1]
        expected = p - c
        body += 1
        cs = cands[p - 1]
        ok = expected in cs
        per_page[p] = (expected, ok)
        if not cs:
            continue
        detected += 1
        if ok:
            match += 1
            last = p
        elif len(off) < 8:
            off.append({"pdf_page": p, "expected": expected, "seen": sorted(cs)[:6]})
    coverage = detected / body if body else 0.0
    rate = match / detected if detected else 0.0
    reaches_end = last >= int(total * FOLIO_END_FRACTION)
    return {
        "body": body, "detected": detected, "match": match, "last_matched_pdf_page": last,
        "coverage": round(coverage, 4), "rate": round(rate, 4), "reaches_end": reaches_end,
        "verified": coverage >= FOLIO_MIN_COVERAGE and rate >= FOLIO_MIN_RATE and reaches_end,
        "off": off, "_per_page": per_page,
    }


def _verify_zone(cands: list[set[int]], total: int) -> dict | None:
    votes: Counter = Counter()
    pages_by_c: dict[int, list[int]] = defaultdict(list)
    for i, cs in enumerate(cands):
        for v in cs:
            c = (i + 1) - v
            if c >= 0:
                votes[c] += 1
                pages_by_c[c].append(i + 1)
    if not votes:
        return None
    # Deterministic choice: most votes, then smallest c.
    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    c1 = ranked[0][0]
    segments = [(c1 + 1, c1)]
    best = _evaluate(cands, segments, total)
    if not best["verified"]:
        # Documented restarts only: a later segment whose numbering begins again at 1, starting
        # after everything the previous segment matched. A mid-book shift is not a restart.
        for c2, n in ranked[1:]:
            if len(segments) > MAX_RESTARTS or n < RESTART_MIN_MATCHES:
                break
            start = c2 + 1
            prev_c = segments[-1][1]
            prev_last = max((p for p in pages_by_c[prev_c] if p < start), default=0)
            later_prev = [p for p in pages_by_c[prev_c] if p >= start]
            if c2 <= prev_c or start <= prev_last or len(later_prev) > 2:
                continue
            trial = sorted(segments + [(start, c2)])
            res = _evaluate(cands, trial, total)
            if res["rate"] > best["rate"]:
                segments, best = trial, res
            if best["verified"]:
                break
    best["segments"] = [{"start_pdf_page": s, "offset": c} for s, c in segments]
    best["restarts"] = len(segments) - 1
    return best


def verify_folios(page_recs: list[dict]) -> dict:
    """Whole-book folio verification. Returns the evidence and a per-page label list."""
    total = len(page_recs)
    zone_cands = [page_zone_candidates(p) for p in page_recs]
    results = {}
    for zone in ZONES:
        res = _verify_zone([zc[zone] for zc in zone_cands], total)
        if res:
            results[zone] = res
    labels = [{"printed_label": None, "printed_verified": False} for _ in range(total)]
    if not results:
        return {"verified": False, "reason": "no-folio-candidates", "zone": None, "labels": labels,
                "zones": {}}
    order = sorted(results, key=lambda z: (not results[z]["verified"], -results[z]["match"], ZONES.index(z)))
    zone = order[0]
    best = results[zone]
    if best["verified"]:
        last = best["last_matched_pdf_page"]
        for p, (expected, ok) in best["_per_page"].items():
            if p <= last and expected >= 1:
                # A page on which no number could be read between verified neighbours carries the
                # mapped folio, flagged unverified. A page on which a DIFFERENT number was read gets
                # no label at all. Nothing is asserted after the last verified page.
                if ok or not zone_cands[p - 1][zone]:
                    labels[p - 1] = {"printed_label": str(expected), "printed_verified": bool(ok)}
    evidence = {z: {k: v for k, v in r.items() if k != "_per_page"} for z, r in results.items()}
    out = {k: v for k, v in best.items() if k != "_per_page"}
    out.update({
        "zone": zone, "labels": labels, "zones": evidence,
        "thresholds": {"coverage": FOLIO_MIN_COVERAGE, "rate": FOLIO_MIN_RATE,
                       "end_fraction": FOLIO_END_FRACTION},
    })
    if not best["verified"]:
        fails = []
        if best["coverage"] < FOLIO_MIN_COVERAGE:
            fails.append("coverage")
        if best["rate"] < FOLIO_MIN_RATE:
            fails.append("rate")
        if not best["reaches_end"]:
            fails.append("end")
        out["reason"] = "below-threshold:" + ",".join(fails)
    return out


# --- legacy image index by text anchors -----------------------------------------------------

SHINGLE = 8
SHINGLE_STEP = 4
ANCHOR_MIN_VOTES = 5
ANCHOR_MIN_FRACTION = 0.50   # at least half of the chunk's shingles on the page (plus a lead-in)
ANCHOR_MARGIN = 2.0          # and at least twice the best page that is not a neighbour
LEAD_RUN = 3
LEGACY_MIN_ANCHORS = 8
LEGACY_END_FRACTION = 0.85   # anchors must reach the last 15 % before the book is called aligned
LEGACY_MAX_GAP = 16          # widest stretch without an anchor that may be filled by inference


def build_shingle_index(page_texts: list[str]) -> dict[str, set[int]]:
    """shingle -> set of pdf pages (1-based) whose folded text contains it."""
    index: dict[str, set[int]] = defaultdict(set)
    for i, text in enumerate(page_texts):
        f = fold(text)
        for j in range(0, max(0, len(f) - SHINGLE + 1)):
            index[f[j:j + SHINGLE]].add(i + 1)
    return index


def locate_text(index: dict[str, set[int]], text: str) -> dict:
    """Locate a corpus chunk on the PDF. `pdf_page` is None unless the location is unambiguous.

    A corpus chunk may begin at the foot of one page and run onto the next, and the corpus files
    such a chunk under either of the two pages. When the chunk's first uniquely placed shingles
    all lie on the page before the one holding most of it (M), the chunk is admissible on M and on
    M-1 (`alt_page`); it then cannot tell those two pages apart and is used only as a compatibility
    check. Text spread over non-adjacent pages, printed on two pages, or of which less than half
    sits on the located page(s), is not an anchor at all.
    """
    f = fold(text)
    shingles = [f[j:j + SHINGLE] for j in range(0, max(0, len(f) - SHINGLE + 1), SHINGLE_STEP)]
    votes: Counter = Counter()
    placed: list = []
    for sh in shingles:
        pages = index.get(sh, ())
        placed.append(pages)
        for p in pages:
            votes[p] += 1
    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    out = {"pdf_page": None, "alt_page": None, "votes": 0, "second": 0, "shingles": len(shingles),
           "best_candidate": None, "lead_in": False}
    if not ranked:
        return out
    m, top = ranked[0]
    unique_seq = [next(iter(pages)) for pages in placed if len(pages) == 1]
    lead = len(unique_seq) >= LEAD_RUN and all(p == m - 1 for p in unique_seq[:LEAD_RUN])
    # A neighbour is a rival only for the shingles it shares with M (the same text printed on both
    # pages); the part of a chunk that simply continues onto the neighbour is not a rival.
    shared = max((sum(1 for pages in placed if m in pages and q in pages) for q in (m - 1, m + 1)), default=0)
    rival = max(max((v for p, v in ranked if p not in (m - 1, m, m + 1)), default=0), shared)
    support = top + (votes.get(m - 1, 0) if lead else 0)
    ok = (top >= ANCHOR_MIN_VOTES and support >= ANCHOR_MIN_FRACTION * len(shingles)
          and top >= ANCHOR_MARGIN * rival)
    out.update({"pdf_page": m if ok else None, "alt_page": (m - 1) if ok and lead else None,
                "votes": top, "second": rival, "best_candidate": m, "lead_in": lead})
    return out


def collect_anchors(index: dict[str, set[int]], hits: list[dict]) -> dict:
    """hits: [{legacy_index, text}] -> one anchor per legacy page with its admissible PDF page(s).

    Several chunks of one legacy page must share an admissible page, otherwise they contradict.
    `offset` is set only for firm anchors (exactly one admissible page).
    """
    by_legacy: dict[int, list[set[int]]] = defaultdict(list)
    located = unlocated = 0
    for h in hits:
        loc = locate_text(index, h.get("text") or "")
        if loc["pdf_page"] is None:
            unlocated += 1
            continue
        located += 1
        by_legacy[int(h["legacy_index"])].append({loc["pdf_page"]} | ({loc["alt_page"]} if loc["alt_page"] else set()))
    anchors, split = [], []
    for k, sets in sorted(by_legacy.items()):
        common = set.intersection(*sets)
        if not common:
            split.append({"legacy_index": k, "pdf_pages": sorted(set.union(*sets))})
            continue
        pages = sorted(common)
        anchors.append({"legacy_index": k, "pdf_pages": pages, "pdf_page": pages[-1] if len(pages) == 1 else None,
                        "offset": pages[0] - 1 - k if len(pages) == 1 else None})
    return {"hits": len(hits), "located": located, "unlocated": unlocated,
            "anchors": anchors, "split_legacy_pages": split}


def legacy_mapping(anchor_info: dict, pdf_pages: int, legacy_pages: int | None) -> dict:
    """Accept only >= 8 firm anchors agreeing on one offset, with no anchor contradicting it."""
    all_anchors = anchor_info["anchors"]
    firm = [a for a in all_anchors if a["offset"] is not None]
    hist = Counter(a["offset"] for a in firm)
    d0 = min(hist) if len(hist) == 1 else None
    incompatible = [a for a in all_anchors if a["offset"] is None and d0 is not None
                    and (a["legacy_index"] + 1 + d0) not in a["pdf_pages"]]
    out = {
        "method": "text-anchors", "anchors": len(firm), "two_page_anchors": len(all_anchors) - len(firm),
        "hits": anchor_info["hits"],
        "located_hits": anchor_info["located"], "unlocated_hits": anchor_info["unlocated"],
        "offset_histogram": {str(k): v for k, v in sorted(hist.items())},
        "split_legacy_pages": len(anchor_info["split_legacy_pages"]),
        "incompatible_two_page_anchors": len(incompatible),
        "pdf_pages": pdf_pages, "legacy_pages": legacy_pages,
        "accepted": False, "offset": None, "rows": {},
    }
    anchors = [dict(a, pdf_page=a["legacy_index"] + 1 + d0) for a in all_anchors] if d0 is not None else firm
    contradictions = len(hist) > 1 or bool(anchor_info["split_legacy_pages"]) or bool(incompatible)
    if len(firm) < LEGACY_MIN_ANCHORS:
        out["reason"] = "too-few-anchors"
    elif contradictions:
        out["reason"] = "contradictory-anchors"
    else:
        d = d0
        lo = min(a["pdf_page"] for a in anchors)
        hi = max(a["pdf_page"] for a in anchors)
        anchored = {a["pdf_page"] for a in anchors}
        last_legacy = max(a["legacy_index"] for a in anchors)
        reaches_end = legacy_pages is not None and last_legacy >= int(legacy_pages * LEGACY_END_FRACTION)
        # An anchor proves its own page. A stretch without anchors is filled only when it is at most
        # LEGACY_MAX_GAP pages wide: one offset at both ends of a wide stretch does not exclude a page
        # added in one place and removed in another inside it. The same limit applies before the
        # first anchor (and only at offset 0, where both sequences start together) and after the
        # last anchor (and only when both sequences end together).
        order = sorted(anchored)
        fill_ok: dict[int, str] = {}
        wide_gaps = []
        for a, b2 in zip(order, order[1:]):
            if b2 - a <= LEGACY_MAX_GAP:
                for p in range(a + 1, b2):
                    fill_ok[p] = "between-anchors"
            elif b2 - a > 1:
                wide_gaps.append({"from_pdf_page": a, "to_pdf_page": b2, "unfilled_rows": b2 - a - 1})
        head_consistent = d == 0 and (lo - 1) <= LEGACY_MAX_GAP
        tail_consistent = bool(reaches_end and pdf_pages - d == legacy_pages and pdf_pages - hi <= LEGACY_MAX_GAP)
        rows = {}
        for p in range(1, pdf_pages + 1):
            li = p - 1 - d
            if li < 0 or (legacy_pages is not None and li >= legacy_pages):
                continue
            if p in anchored:
                rows[p] = (li, "anchor")
            elif p in fill_ok:
                rows[p] = (li, fill_ok[p])
            elif p < lo and head_consistent:
                rows[p] = (li, "head-consistent")
            elif p > hi and tail_consistent:
                rows[p] = (li, "tail-consistent")
        out.update({"accepted": True, "offset": d, "rows": rows,
                    "first_anchor_pdf_page": lo, "last_anchor_pdf_page": hi,
                    "last_anchor_legacy_index": last_legacy, "reaches_end": reaches_end,
                    "head_consistent": head_consistent, "tail_consistent": tail_consistent,
                    "max_gap": LEGACY_MAX_GAP, "wide_gaps": wide_gaps,
                    "rows_nulled_by_gap_rule": sum(g["unfilled_rows"] for g in wide_gaps)})
    # Relation as evidence for the catalog.
    #   aligned           accepted, anchors reach the last 15 %, and no wide unproven stretch between them
    #   partial           accepted, but only part of the book is proven (rows exist for that part)
    #   different-edition anchors contradict one another: the pagination differs somewhere
    #   none              nothing could be established (too few anchors); no rows
    if out["accepted"]:
        out["relation"] = "aligned" if out["reaches_end"] and not out["wide_gaps"] else "partial"
    elif out["reason"] == "contradictory-anchors":
        out["relation"] = "different-edition"
    else:
        out["relation"] = "none"
    return out


# --- legacy image index by per-page content proof (corpus database + page images) -------------

PROVEN_BASES = ("text-anchor", "image-match", "image+text")
CONTENT_MIN_PROVEN = 8


def corpus_page_texts(db_path, book_key: str) -> dict[int, str]:
    """Corpus text of every legacy page of a book: {section (= legacy image index): text}."""
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out: dict[int, list[str]] = defaultdict(list)
        for section, text in con.execute(
                "SELECT section, text FROM chunks WHERE book_key = ? AND source = 'mineru' "
                "AND section IS NOT NULL AND text IS NOT NULL ORDER BY section, id", (book_key,)):
            out[int(section)].append(text)
        return {k: "\n".join(v) for k, v in out.items()}
    finally:
        con.close()


def text_rows(index: dict[str, set[int]], page_texts: dict[int, str]) -> dict[int, set[int]]:
    """legacy_index -> admissible PDF pages, for corpus pages located unambiguously."""
    out = {}
    for li, text in sorted(page_texts.items()):
        loc = locate_text(index, text)
        if loc["pdf_page"] is not None:
            out[li] = {loc["pdf_page"]} | ({loc["alt_page"]} if loc["alt_page"] else set())
    return out


def _longest_increasing(pairs: list[tuple[int, int]]) -> set[int]:
    """Legacy indexes of the longest chain that is strictly increasing in both coordinates."""
    best_len, prev = [], []
    for i, (_, p) in enumerate(pairs):
        bl, bp = 1, -1
        for j in range(i):
            if pairs[j][1] < p and best_len[j] + 1 > bl:
                bl, bp = best_len[j] + 1, j
        best_len.append(bl)
        prev.append(bp)
    if not pairs:
        return set()
    i = max(range(len(pairs)), key=lambda k: (best_len[k], -k))
    keep = set()
    while i >= 0:
        keep.add(pairs[i][0])
        i = prev[i]
    return keep


def content_mapping(text: dict[int, set[int]] | None, image: dict[int, dict] | None,
                    pdf_pages: int, legacy_pages: int, informative: int | None = None,
                    same_threshold: float | None = None) -> dict:
    """Per-page proof of legacy_index -> pdf_page; the offset may differ between parts of a book.

    A legacy page is PROVEN when its page image matches one PDF page (calibrated test) and/or its
    corpus text is located on exactly one PDF page, and the two do not disagree. Proven rows must
    form one increasing sequence. Unproven rows are filled only between two proven rows that are
    at most LEGACY_MAX_GAP apart and have the same offset, before the first proven row at offset 0
    within that distance, and after the last when both sequences end together within it.
    """
    text, image = text or {}, image or {}
    proven: dict[int, tuple[int, str, float | None]] = {}
    conflicts = []
    for li in sorted(set(text) | set(image)):
        if not 0 <= li < legacy_pages:
            continue
        img, txt = image.get(li), text.get(li)
        if img and txt:
            if img["pdf_page"] in txt:
                proven[li] = (img["pdf_page"], "image+text", img["score"])
            else:
                conflicts.append({"legacy_index": li, "image": img["pdf_page"], "text": sorted(txt)})
        elif img:
            proven[li] = (img["pdf_page"], "image-match", img["score"])
        elif len(txt) == 1:
            proven[li] = (next(iter(txt)), "text-anchor", None)
    keep = _longest_increasing([(li, proven[li][0]) for li in sorted(proven)])
    dropped = sorted(set(proven) - keep)
    proven = {li: v for li, v in proven.items() if li in keep and 1 <= v[0] <= pdf_pages}
    out = {
        "method": "content", "pdf_pages": pdf_pages, "legacy_pages": legacy_pages,
        "text_located_pages": len(text), "image_matched_pages": len(image),
        "informative_image_pages": informative,
        "proven_pages": len(proven),
        "proven_by_basis": dict(sorted(Counter(v[1] for v in proven.values()).items())),
        "image_text_conflicts": conflicts[:20], "image_text_conflict_count": len(conflicts),
        "non_monotone_dropped": dropped[:20], "non_monotone_dropped_count": len(dropped),
        "max_gap": LEGACY_MAX_GAP, "accepted": False, "offset": None, "rows": {},
    }
    if len(proven) < CONTENT_MIN_PROVEN:
        out["reason"] = "too-few-proven-pages"
        compared = max(len(text) + len(conflicts), informative or 0)
        out["relation"] = "different-edition" if compared >= 20 else "none"
        return out
    order = sorted(proven)
    rows: dict[int, tuple[int, str, float | None]] = {proven[li][0]: (li, proven[li][1], proven[li][2]) for li in order}
    wide_gaps, offset_changes = [], []
    for la, lb in zip(order, order[1:]):
        pa, pb = proven[la][0], proven[lb][0]
        if lb - la == 1:
            if pb - pa != 1:
                offset_changes.append({"after_legacy_index": la, "offset_before": pa - 1 - la, "offset_after": pb - 1 - lb})
            continue
        if pb - pa != lb - la:
            offset_changes.append({"after_legacy_index": la, "offset_before": pa - 1 - la, "offset_after": pb - 1 - lb,
                                   "unfilled_rows": lb - la - 1})
        elif lb - la <= LEGACY_MAX_GAP:
            for k in range(1, lb - la):
                rows[pa + k] = (la + k, "between-anchors", None)
        else:
            wide_gaps.append({"from_legacy_index": la, "to_legacy_index": lb, "unfilled_rows": lb - la - 1})
    l0, lz = order[0], order[-1]
    p0, pz = proven[l0][0], proven[lz][0]
    if 0 < l0 <= LEGACY_MAX_GAP and p0 - 1 == l0:
        for k in range(l0):
            rows[k + 1] = (k, "head-consistent", None)
    tail = legacy_pages - 1 - lz
    if 0 < tail <= LEGACY_MAX_GAP and pdf_pages - pz == tail:
        for k in range(1, tail + 1):
            rows[pz + k] = (lz + k, "tail-consistent", None)
    hist = Counter(p - 1 - li for p, (li, _, _) in rows.items())
    mapped = len(rows)
    scores = [v[2] for v in proven.values() if v[2] is not None]
    same_rendering = bool(
        same_threshold is not None and informative and pdf_pages == legacy_pages and set(hist) == {0}
        and mapped == legacy_pages and len(scores) >= informative and min(scores) >= same_threshold)
    out.update({
        "accepted": True, "rows": rows, "offset": next(iter(hist)) if len(hist) == 1 else None,
        "offset_histogram": {str(k): v for k, v in sorted(hist.items())},
        "offset_changes": offset_changes, "wide_gaps": wide_gaps,
        "rows_nulled_by_gap_rule": sum(g["unfilled_rows"] for g in wide_gaps),
        "unmapped_legacy_pages": legacy_pages - mapped,
        "image_score_min": min(scores) if scores else None,
        "same_rendering": same_rendering,
        "relation": "aligned" if mapped == legacy_pages else "partial",
    })
    return out


def image_check_same_bytes(score_rows: list[dict], cal: dict) -> dict:
    """Independent re-proof of a same-bytes pairing from the page images: at least 95 % of the
    informative legacy images must match the PDF page at the same position under the calibrated
    rule, and none may match a page at another position."""
    informative = [r for r in score_rows if not r.get("blank") and r.get("score") is not None]
    passing = [r for r in informative
               if r["score"] >= cal["match_threshold"] and r["score"] - r["second"] >= cal["margin"]]
    ok = [r for r in passing if r["pdf_page"] == r["legacy_index"] + 1]
    elsewhere = len(passing) - len(ok)
    return {"informative_pages": len(informative), "matched_at_same_position": len(ok),
            "matched_elsewhere": elsewhere, "blank_pages": sum(1 for r in score_rows if r.get("blank")),
            "min_score": min((r["score"] for r in ok), default=None),
            "confirmed": len(informative) >= 5 and elsewhere == 0 and len(ok) >= 0.95 * len(informative)}


def legacy_unmapped(mapped: dict[int, int], legacy_pages: int | None) -> list[dict] | None:
    """Legacy indexes without a row, each with a HINT to the nearest mapped legacy index.

    The hint is not a mapping: it carries the neighbour's own legacy index and PDF page plus the
    distance, so that an old link can degrade to "the nearest page we can show" with a notice. Ties
    go to the earlier neighbour. None when the legacy page count is unknown; the hint is None when
    the edition has no mapped row at all.
    """
    if legacy_pages is None:
        return None
    known = sorted(mapped)
    out = []
    for li in range(legacy_pages):
        if li in mapped:
            continue
        near = min(known, key=lambda k: (abs(k - li), k)) if known else None
        out.append({"legacy_image_index": li,
                    "nearest_mapped": {"legacy_image_index": near, "pdf_page": mapped[near],
                                       "distance": abs(near - li)} if near is not None else None})
    return out


# --- assembly -------------------------------------------------------------------------------

def build_page_map(edition_id: str, sha256: str, text_rec: dict, legacy: dict) -> dict:
    """legacy: {"mode": "same-bytes"|"anchors"|"none", "legacy_pages": int|None, "hits": [...], "reason": str}"""
    page_recs = text_rec["page_texts"]
    n = len(page_recs)
    folio = verify_folios(page_recs)
    labels = folio.pop("labels")
    mode = legacy.get("mode", "none")
    legacy_pages = legacy.get("legacy_pages")
    rows_legacy: dict[int, tuple[int, str]] = {}
    if mode == "same-bytes":
        if legacy_pages != n:
            raise ValueError(f"{edition_id}: same-bytes claimed but pages differ ({n} != {legacy_pages})")
        rows_legacy = {p: (p - 1, "same-bytes") for p in range(1, n + 1)}
        legacy_ev = {"method": "same-bytes", "accepted": True, "offset": 0, "relation": "same-bytes",
                     "pdf_pages": n, "legacy_pages": legacy_pages,
                     "image_check": legacy.get("image_check")}
    elif mode == "reassembled":
        # The PDF was built from the legacy images, one per page: exact by construction.
        if legacy_pages != n:
            raise ValueError(f"{edition_id}: reassembled PDF has {n} pages, the image set {legacy_pages}")
        rows_legacy = {p: (p - 1, "reassembled") for p in range(1, n + 1)}
        legacy_ev = {"method": "reassembled", "accepted": True, "offset": 0, "relation": "legacy-image-source",
                     "pdf_pages": n, "legacy_pages": legacy_pages}
    elif mode == "content":
        index = build_shingle_index([p.get("text") or "" for p in page_recs])
        corpus = legacy.get("corpus_texts") or {}
        legacy_ev = content_mapping(text_rows(index, corpus) if corpus else None, legacy.get("image_rows"),
                                    n, legacy_pages, legacy.get("informative"), legacy.get("same_threshold"))
        rows_legacy = legacy_ev.pop("rows")
        legacy_ev["corpus_pages"] = len(corpus)
        legacy_ev["corpus_source"] = legacy.get("corpus_source")
        legacy_ev["image_source"] = legacy.get("image_source")
    elif mode == "anchors":
        info = collect_anchors(build_shingle_index([p.get("text") or "" for p in page_recs]),
                               legacy.get("hits") or [])
        legacy_ev = legacy_mapping(info, n, legacy_pages)
        rows_legacy = legacy_ev.pop("rows")
        legacy_ev["anchor_pairs"] = info["anchors"]
        legacy_ev["split_detail"] = info["split_legacy_pages"]
        legacy_ev["searches"] = legacy.get("searches")
    else:
        legacy_ev = {"method": "none", "accepted": False, "offset": None, "relation": legacy.get("relation", "none"),
                     "reason": legacy.get("reason"), "pdf_pages": n, "legacy_pages": legacy_pages}
    pages = []
    for i, rec in enumerate(page_recs):
        p = i + 1
        li = rows_legacy.get(p)
        pages.append({
            "pdf_page": p,
            "printed_label": labels[i]["printed_label"],
            "printed_verified": labels[i]["printed_verified"],
            "legacy_image_index": li[0] if li else None,
            "legacy_basis": li[1] if li else None,
            "legacy_score": li[2] if li and len(li) > 2 else None,
            "ocr_page": None,
            "text_layer": rec.get("kind"),
        })
    mapped = sum(1 for r in pages if r["legacy_image_index"] is not None)
    return {
        "legacy_unmapped": legacy_unmapped({r["legacy_image_index"]: r["pdf_page"] for r in pages
                                            if r["legacy_image_index"] is not None}, legacy_pages),
        "contract": "sun-catalog-v1/page-map",
        "page_map_version": PAGE_MAP_VERSION,
        "edition_id": edition_id,
        "pdf_sha256": sha256,
        "extractor_version": text_rec.get("extractor_version"),
        "pdf_pages": n,
        "printed": {
            "verified": bool(folio.get("verified")),
            "labelled_pages": sum(1 for r in pages if r["printed_label"] is not None),
            "verified_pages": sum(1 for r in pages if r["printed_verified"]),
            "evidence": folio,
        },
        "legacy": {
            "mapped_pages": mapped,
            "coverage": round(mapped / legacy_pages, 4) if legacy_pages else None,
            "evidence": legacy_ev,
        },
        "ocr": {"ocr_version": None, "ocr_needed_pages": sum(1 for r in pages if r["text_layer"] == "ocr_needed")},
        "pages": pages,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", required=True, help="editions plan JSON (from plan.py)")
    p.add_argument("--text-cache", required=True)
    p.add_argument("--sun-cache", help="directory of cached SUN search results (mode anchors only)")
    p.add_argument("--corpus-db", help="read-only snapshot of the corpus database (mode content)")
    p.add_argument("--images", help="page_images.py cache directory with scores/ and calibration.json")
    p.add_argument("--out", required=True, help="directory for <edition_id>.json")
    args = p.parse_args(argv)
    from inspect_pdf import cache_path

    plan = read_json(args.plan)
    out_dir = Path(args.out)
    images = Path(args.images) if args.images else None
    cal = read_json(images / "calibration.json") if images and (images / "calibration.json").exists() else None
    done = 0
    for ed in plan["editions"]:
        if not ed.get("pdf") or ed.get("holding", "sun") != "sun":
            continue          # no page map for a record without bytes held here
        sha = ed["pdf"]["sha256"]
        text_rec = read_json(cache_path(Path(args.text_cache), sha))
        spec = dict(ed["legacy_spec"])
        scores = None
        if images and spec.get("jc_short_key"):
            sp = images / "scores" / f"{spec['jc_short_key']}__{sha[:16]}.json"
            scores = read_json(sp)["rows"] if sp.exists() else None
        if spec.get("mode") == "anchors":
            from sun_search import load_hits
            hits, searches = load_hits(Path(args.sun_cache), spec["sun_book_key"])
            spec["hits"], spec["searches"] = hits, searches
        elif spec.get("mode") == "content":
            from page_images import image_matches
            if args.corpus_db:
                spec["corpus_texts"] = corpus_page_texts(args.corpus_db, spec["sun_book_key"])
                spec["corpus_source"] = "offline snapshot of the corpus database (chunks, source=mineru)"
            if scores is not None and cal:
                spec["image_rows"] = image_matches(scores, cal)
                spec["informative"] = sum(1 for r in scores if not r.get("blank") and r.get("score") is not None)
                spec["same_threshold"] = cal["same_threshold"]
                spec["image_source"] = cal.get("legacy_source")
        elif spec.get("mode") == "same-bytes" and scores is not None and cal:
            spec["image_check"] = image_check_same_bytes(scores, cal)
        doc = build_page_map(ed["edition_id"], sha, text_rec, spec)
        write_json(out_dir / f"{ed['edition_id']}.json", doc)
        done += 1
    print(f"page maps written: {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
