#!/usr/bin/env python3
"""Legacy page-image coverage of a catalog release, for the image-retirement programme.

For every reader book: which legacy image indexes map to a PDF page of the edition that answers
for the book (and by which evidence), which do not and why, and the same for any other edition
that carries rows for the book. Totals say how many books / legacy pages can move to PDF pages.
Optionally evaluates named consumer page sets. Offline; reads only the release and local inputs.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import read_json, write_json  # noqa: E402

REF_RE = re.compile(r"pages/([0-9a-f]{12})/p(\d+)\.webp")


def ranges(xs: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for x in sorted(xs):
        if out and x == out[-1][1] + 1:
            out[-1][1] = x
        else:
            out.append([x, x])
    return out


def edition_rows(release: Path, edition_id: str) -> dict[int, dict]:
    pm = read_json(release / "page-maps" / f"{edition_id}.json")
    return {r["legacy_image_index"]: r for r in pm["pages"] if r["legacy_image_index"] is not None}


def blank_legacy(images: Path | None, short: str, sha: str | None) -> set[int]:
    if not images or not sha:
        return set()
    f = images / "scores" / f"{short}__{sha[:16]}.json"
    return {r["legacy_index"] for r in read_json(f)["rows"] if r.get("blank")} if f.exists() else set()


def build(release: Path, jc_catalog: Path, images: Path | None) -> dict:
    cat = read_json(release / "catalog.json")
    by_id = {e["edition_id"]: e for e in cat["editions"]}
    jc = {b["shortKey"]: b for b in read_json(jc_catalog)["books"]}
    held = lambda e: e.get("pdf") is not None and e.get("holding") != "learn-only"  # noqa: E731
    books = []
    for rb in cat["reader_books"]:
        short = rb["jc_short_key"]
        n = jc[short]["pages"]
        owner = by_id[rb["edition_id"]]
        cands = [by_id[c] for c in rb.get("candidate_edition_ids") or []]
        per_edition = []
        for e in ([owner] if held(owner) else []) + [c for c in cands if held(c)]:
            rows = edition_rows(release, e["edition_id"])
            per_edition.append({
                "edition_id": e["edition_id"], "role": "owner" if e is owner else "other-edition-of-the-work",
                "mapped": len(rows), "by_basis": dict(sorted(Counter(r["legacy_basis"] for r in rows.values()).items())),
                "relation": e["corpus"]["relation"], "text_identity": e["corpus"]["text_identity"],
                "printed_verified": bool(e["printed"] and e["printed"]["verified"]),
                "platform_current": e["platform_current"], "preview_default": e["preview_default"],
                "pdf_pages": e["pdf"]["pages"], "pdf_sha256": e["pdf"]["sha256"],
                "learn_asset_id": e["legacy"].get("learn_asset_id"), "_rows": rows,
            })
        # The edition chosen for old page links: the one that answers for the book (it has the most
        # complete proven mapping by construction); None when the book has no usable PDF.
        chosen = per_edition[0] if per_edition and per_edition[0]["role"] == "owner" else None
        rows = chosen["_rows"] if chosen else {}
        unmapped = [i for i in range(n) if i not in rows]
        blank = blank_legacy(images, short, chosen["pdf_sha256"] if chosen else None)
        if not chosen:
            why = {"no-usable-pdf": len(unmapped)}
        else:
            why = dict(sorted(Counter("blank-legacy-page-outside-a-proven-stretch" if i in blank
                                      else "not-proven-by-image-or-text" for i in unmapped).items()))
        work = [e for e in cat["editions"] if held(e) and chosen and e["work_id"] == by_id[chosen["edition_id"]]["work_id"]]
        preview = next((e for e in work if e["preview_default"]), None)
        learn_ids = sorted({e["legacy"]["learn_asset_id"] for e in cat["editions"]
                            if e["legacy"].get("learn_asset_id") and chosen
                            and e["work_id"] == by_id[chosen["edition_id"]]["work_id"]})
        books.append({
            "jc_short_key": short, "sun_book_key": rb["sun_book_key"],
            "stage": jc[short]["phase"], "subject": jc[short]["subject"], "title": jc[short]["title"],
            "legacy_pages": n,
            "edition_id": chosen["edition_id"] if chosen else None,
            "mapped": len(rows), "by_basis": chosen["by_basis"] if chosen else {},
            "unmapped": len(unmapped), "unmapped_ranges": ranges(unmapped), "unmapped_reasons": why,
            "coverage": round(len(rows) / n, 4) if n else None,
            "status": "full" if not unmapped else ("partial" if rows else "none"),
            "relation": chosen["relation"] if chosen else "none",
            "text_identity": chosen["text_identity"] if chosen else None,
            "printed_verified": chosen["printed_verified"] if chosen else False,
            "platform_current": chosen["platform_current"] if chosen else None,
            "preview_default_edition_id": preview["edition_id"] if preview else None,
            "chosen_is_preview_default": bool(chosen and preview and preview["edition_id"] == chosen["edition_id"]),
            "cover_index_0_mapped": 0 in rows,
            "learn_asset_id": chosen["learn_asset_id"] if chosen else None,
            "learn_asset_ids_of_the_work": learn_ids,
            "learn_bound_file_is_preview_default_bytes": bool(preview and preview["legacy"].get("learn_asset_id")),
            "learn_bound_file_is_chosen_edition_bytes": bool(chosen and chosen["learn_asset_id"]),
            "content_decision": rb.get("content_decision"),
            "other_editions": [{k: v for k, v in pe.items() if k != "_rows"} for pe in per_edition[1:]],
            "_rows": rows,
        })
    totals = {
        "books": len(books), "books_full": sum(1 for b in books if b["status"] == "full"),
        "books_partial": sum(1 for b in books if b["status"] == "partial"),
        "books_none": sum(1 for b in books if b["status"] == "none"),
        "legacy_pages": sum(b["legacy_pages"] for b in books),
        "legacy_pages_mapped": sum(b["mapped"] for b in books),
        "legacy_pages_unmapped": sum(b["unmapped"] for b in books),
        "mapped_by_basis": dict(sorted(sum((Counter(b["by_basis"]) for b in books), Counter()).items())),
        "unmapped_by_reason": dict(sorted(sum((Counter(b["unmapped_reasons"]) for b in books), Counter()).items())),
        "covers_mapped": sum(1 for b in books if b["cover_index_0_mapped"]),
        "by_text_identity": dict(Counter(str(b["text_identity"]) for b in books)),
        "printed_verified_books": sum(1 for b in books if b["printed_verified"]),
        "books_with_learn_asset": sum(1 for b in books if b["learn_asset_ids_of_the_work"]),
        "learn_bound_file_is_chosen_edition_bytes": sum(1 for b in books if b["learn_bound_file_is_chosen_edition_bytes"]),
        "learn_bound_file_is_preview_default_bytes": sum(1 for b in books if b["learn_bound_file_is_preview_default_bytes"]),
    }
    return {"release_id": cat["release_id"], "totals": totals, "books": books}


def consumer_sets(doc: dict, refs_file: Path | None) -> dict:
    by_short = {b["jc_short_key"]: b for b in doc["books"]}
    out = {}
    if refs_file and refs_file.exists():
        refs = sorted({(m.group(1), int(m.group(2))) for m in REF_RE.finditer(refs_file.read_text())})
        per = {}
        for short, idx in refs:
            d = per.setdefault(short, {"referenced": 0, "mapped": 0, "unmapped_indexes": []})
            d["referenced"] += 1
            if short in by_short and idx in by_short[short]["_rows"]:
                d["mapped"] += 1
            else:
                d["unmapped_indexes"].append(idx)
        out["yw_lesson_pages"] = {"referenced": len(refs), "mapped": sum(d["mapped"] for d in per.values()),
                                  "by_book": per}
    daofa = [b for b in doc["books"] if b["stage"] == "初中" and b["subject"] == "道德与法治"]
    out["daofa_all_pages"] = {"books": len(daofa), "legacy_pages": sum(b["legacy_pages"] for b in daofa),
                              "mapped": sum(b["mapped"] for b in daofa),
                              "by_book": {b["jc_short_key"]: [b["mapped"], b["legacy_pages"]] for b in daofa}}
    yuwen = [b for b in doc["books"] if b["subject"] == "语文"]
    out["user_center_covers"] = {"books": len(yuwen), "mapped": sum(1 for b in yuwen if b["cover_index_0_mapped"]),
                                 "unmapped": [b["jc_short_key"] for b in yuwen if not b["cover_index_0_mapped"]]}
    out["xt_covers"] = {"books": len(doc["books"]), "mapped": doc["totals"]["covers_mapped"],
                        "unmapped": [b["jc_short_key"] for b in doc["books"] if not b["cover_index_0_mapped"]]}
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("release_dir")
    p.add_argument("--jc-catalog", required=True)
    p.add_argument("--images", help="page_images.py cache (for the blank-page reason)")
    p.add_argument("--yw-refs", help="text file with the page-image URLs referenced by yw lessons")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    doc = build(Path(args.release_dir), Path(args.jc_catalog), Path(args.images) if args.images else None)
    doc["consumers"] = consumer_sets(doc, Path(args.yw_refs) if args.yw_refs else None)
    for b in doc["books"]:
        del b["_rows"]
    write_json(args.out, doc)
    t = doc["totals"]
    print(f"books full/partial/none = {t['books_full']}/{t['books_partial']}/{t['books_none']}; "
          f"legacy pages mapped {t['legacy_pages_mapped']} of {t['legacy_pages']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
