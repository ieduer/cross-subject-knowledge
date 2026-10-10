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
               Rows are filled on anchored pages, between anchors, before the first anchor only
               when the offset is 0, and after the last anchor only when the anchors reach the
               last 15 % and both sequences end together; every row records which of these it is
               (`legacy_basis`). Anchors that stop earlier prove a prefix only: relation `partial`.
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
                # Pages between verified neighbours carry the mapped folio but are not
                # individually verified; nothing is asserted after the last verified page.
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
        # Beyond the outermost anchors a row is filled only where no hidden page can be inserted or
        # dropped without contradicting an anchor: before the first anchor when both sequences start
        # together (offset 0); after the last anchor when the anchors already reach the last 15 %
        # and both sequences end together.
        head_consistent = d == 0
        tail_consistent = bool(reaches_end and pdf_pages - d == legacy_pages)
        rows = {}
        for p in range(1, pdf_pages + 1):
            li = p - 1 - d
            if li < 0 or (legacy_pages is not None and li >= legacy_pages):
                continue
            if p in anchored:
                rows[p] = (li, "anchor")
            elif lo < p < hi:
                rows[p] = (li, "between-anchors")
            elif p < lo and head_consistent:
                rows[p] = (li, "head-consistent")
            elif p > hi and tail_consistent:
                rows[p] = (li, "tail-consistent")
        out.update({"accepted": True, "offset": d, "rows": rows,
                    "first_anchor_pdf_page": lo, "last_anchor_pdf_page": hi,
                    "last_anchor_legacy_index": last_legacy, "reaches_end": reaches_end,
                    "head_consistent": head_consistent, "tail_consistent": tail_consistent})
    # Relation as evidence for the catalog. An accepted mapping whose anchors stop before the last
    # 15 % proves only a prefix (the tail was never tested): partial, rows kept for the proven span.
    strong = [k for k, v in hist.items() if v >= 2]
    if out["accepted"]:
        out["relation"] = "aligned" if out["reaches_end"] else "partial"
    elif len(strong) >= 2:
        out["relation"] = "different-edition"
    else:
        out["relation"] = "partial"
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
                     "pdf_pages": n, "legacy_pages": legacy_pages}
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
            "ocr_page": None,
            "text_layer": rec.get("kind"),
        })
    mapped = sum(1 for r in pages if r["legacy_image_index"] is not None)
    return {
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
    p.add_argument("--sun-cache", required=True, help="directory of cached SUN search results")
    p.add_argument("--out", required=True, help="directory for <edition_id>.json")
    args = p.parse_args(argv)
    from inspect_pdf import cache_path
    from sun_search import load_hits

    plan = read_json(args.plan)
    out_dir = Path(args.out)
    done = 0
    for ed in plan["editions"]:
        if not ed.get("pdf"):
            continue
        text_rec = read_json(cache_path(Path(args.text_cache), ed["pdf"]["sha256"]))
        spec = dict(ed["legacy_spec"])
        if spec.get("mode") == "anchors":
            hits, searches = load_hits(Path(args.sun_cache), ed["legacy_spec"]["sun_book_key"])
            spec["hits"], spec["searches"] = hits, searches
        doc = build_page_map(ed["edition_id"], ed["pdf"]["sha256"], text_rec, spec)
        write_json(out_dir / f"{ed['edition_id']}.json", doc)
        done += 1
    print(f"page maps written: {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
