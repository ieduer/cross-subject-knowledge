#!/usr/bin/env python3
"""Assemble one sun-catalog-v1 release directory (deterministic: same inputs -> same bytes).

  <out>/<release_id>/catalog.json      edition records (+ index of the reader books)
  <out>/<release_id>/downloads.json    full download catalog (metadata only)
  <out>/<release_id>/page-maps/<edition_id>.json
  <out>/<release_id>/RELEASE.json      release id, tool versions, input hashes, counts
  <out>/<release_id>/SHA256SUMS

release_id = <latest input retrieval time, UTC>-<8 hex of the input hashes>. No wall-clock time,
host name or absolute path is written into the release. Unknown values are null, never guessed.
"""
from __future__ import annotations

import argparse
import platform
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONTRACT, EXTRACTOR_VERSION, PAGE_MAP_VERSION, TOOL_VERSION, dumps, file_hashes,  # noqa: E402
                    read_json, sha256_bytes, sha256_text, write_atomic)

PHASE_ORDER = {"小学": 0, "初中": 1, "高中": 2}
# Groups that exist in the upstream catalog but are not offered by the service's 27 UI groups and
# were not enumerated in this run (basis: BASELINE-PDF-IMAGES.md §A2/A3, catalog 1512443771).
NOT_ENUMERATED = [
    {"group": "小学 / 科学、艺术、音乐、美术、体育与健康", "approx_records": 623, "reason": "not in the 27 UI groups"},
    {"group": "初中 / 艺术(舞蹈·影视·戏剧)、音乐、美术、科学、体育、日语、俄语、法语、德语、西班牙语",
     "approx_records": 451, "reason": "not in the 27 UI groups"},
    {"group": "高中 / 通用技术、信息技术、音乐、美术、艺术、体育、日语、俄语、法语、德语、西班牙语",
     "approx_records": 268, "reason": "not in the 27 UI groups"},
    {"group": "特殊教育 rows outside the 27 UI groups", "approx_records": 142, "reason": "not in the 27 UI groups"},
    {"group": "untagged records (教师用书, per-lesson items)", "approx_records": 654,
     "reason": "reachable by no phase/subject pair"},
]


def corpus_block(ed: dict, pm: dict | None) -> dict:
    spec = ed.get("legacy_spec") or {}
    if not ed.get("pdf") or pm is None:
        return {"relation": "none", "basis": spec.get("reason") or "no PDF held", "coverage": None,
                "verification": "not-possible"}
    ev = pm["legacy"]["evidence"]
    if ev["method"] == "same-bytes":
        return {"relation": "same-bytes", "coverage": 1.0, "verification": "bytes",
                "basis": "the held PDF is the source file of the page images and corpus; "
                         f"page counts equal ({ev['pdf_pages']})"}
    if ev["method"] == "text-anchors":
        if ed["pdf"].get("has_text_layer") is False:
            return {"relation": "partial", "coverage": None, "verification": "not-possible",
                    "basis": "identifier only: the PDF has no text layer, page-by-page comparison "
                             "with the corpus was not possible (OCR needed)"}
        basis = (f"text anchors vs SUN corpus: {ev['anchors']} legacy pages located "
                 f"({ev['located_hits']}/{ev['hits']} hits), offsets {ev['offset_histogram']}, "
                 f"pdf pages {ev['pdf_pages']} vs legacy pages {ev['legacy_pages']}")
        if ev["accepted"]:
            tail = "anchors reach the last 15 %" if ev["reaches_end"] else \
                f"anchors stop at legacy page {ev['last_anchor_legacy_index']} of {ev['legacy_pages']}: tail untested"
            return {"relation": ev["relation"], "coverage": pm["legacy"]["coverage"],
                    "verification": "text-anchors", "basis": basis + f"; single offset {ev['offset']}; {tail}"}
        return {"relation": ev["relation"], "coverage": None, "verification": "text-anchors",
                "basis": basis + f"; not accepted: {ev.get('reason')}"}
    return {"relation": "none", "basis": ev.get("reason") or spec.get("reason"), "coverage": None,
            "verification": "not-possible"}


def finalize_edition(ed: dict, pm: dict | None) -> dict:
    rec = {k: v for k, v in ed.items() if k != "legacy_spec"}
    rec["corpus"] = corpus_block(ed, pm)
    rec["processing"] = {"ocr_version": None, "index_version": None,
                         "page_map_version": PAGE_MAP_VERSION if pm else None,
                         "extractor_version": EXTRACTOR_VERSION if pm else None}
    if pm:
        pe = pm["printed"]["evidence"]
        rec["printed"] = {"verified": pm["printed"]["verified"], "labelled_pages": pm["printed"]["labelled_pages"],
                          "verified_pages": pm["printed"]["verified_pages"], "zone": pe.get("zone"),
                          "restarts": pe.get("restarts"), "reason": pe.get("reason")}
        rec["legacy"]["mapped_pages"] = pm["legacy"]["mapped_pages"]
        rec["legacy"]["offset"] = pm["legacy"]["evidence"].get("offset")
    else:
        rec["printed"] = None
        rec["legacy"]["mapped_pages"] = 0
        rec["legacy"]["offset"] = None
    return rec


def build_downloads(dl_dir: Path, details_dir: Path, editions: list[dict]) -> dict:
    current, other = {}, {}
    for e in editions:
        sid = e["source"]["source_id"]
        if not sid or not e.get("pdf"):
            continue
        if e["source"].get("platform_current") is True:
            current[sid] = e["edition_id"]
        else:
            other.setdefault(sid, []).append(e["edition_id"])
    rows, groups, versions, seen = [], [], set(), set()
    for f in sorted(dl_dir.glob("*__*.json")):
        stage, subject = f.stem.split("__")
        wrapped = read_json(f)
        versions.add((wrapped.get("headers") or {}).get("x-jks-catalog-version"))
        groups.append({"stage": stage, "subject": subject, "rows": len(wrapped["body"]),
                       "retrieved_at": wrapped.get("retrieved_at")})
        for r in wrapped["body"]:
            sid = r["id"]
            if sid in seen:
                continue
            seen.add(sid)
            size = status = None
            df = details_dir / f"{sid}.json"
            if df.exists():
                status = read_json(df)["body"].get("status")
                for t in read_json(df)["body"].get("ti_items") or []:
                    if (t.get("ti_format") or "").lower() == "pdf" and t.get("ti_file_flag") == "source" and t.get("ti_size"):
                        size = int(t["ti_size"])
            rows.append({
                "source_id": sid, "stage": stage, "subject": subject, "title": r.get("title") or None,
                "edition_label": r.get("edition") or None, "publisher": r.get("publisher") or None,
                "cover": r.get("cover_url") or None, "size_bytes": size,
                "sources": [{"kind": "upstream", "url": f"https://jks-ai.bdfz.net/pdf/{sid}"}],
                "upstream_status": status,   # ONLINE / OFFLINE where the details JSON was read, else null
                "edition_id": current.get(sid),
                "held_other_edition_ids": sorted(other.get(sid, [])),
            })
    rows.sort(key=lambda r: (PHASE_ORDER.get(r["stage"], 9), r["subject"], r["title"] or "", r["source_id"]))
    groups.sort(key=lambda g: (PHASE_ORDER.get(g["stage"], 9), g["subject"]))
    return {"contract": CONTRACT + "/downloads", "upstream_catalog_versions": sorted(v for v in versions if v),
            "groups": groups, "not_enumerated": NOT_ENUMERATED, "rows": rows}


def counts(editions: list[dict], page_maps: dict[str, dict], downloads: dict, plan: dict) -> dict:
    held = [e for e in editions if e.get("pdf")]
    reader = [e for e in editions if e["legacy"].get("sun_book_key")]
    c = {
        "editions": len(editions),
        "editions_with_pdf": len(held),
        "pdf_bytes": sum(e["pdf"]["bytes"] for e in held),
        "pdf_pages": sum(e["pdf"]["pages"] or 0 for e in held),
        "by_status": dict(Counter(e["status"] for e in editions)),
        "by_provenance": dict(Counter(e["provenance"] for e in editions)),
        "by_stage_subject_with_pdf": dict(Counter(f"{e['stage']}/{e['subject']}" for e in held)),
        "by_stage_subject_all": dict(Counter(f"{e['stage']}/{e['subject']}" for e in editions)),
        "by_corpus_relation": dict(Counter(e["corpus"]["relation"] for e in editions)),
        "by_corpus_relation_with_pdf": dict(Counter(e["corpus"]["relation"] for e in held)),
        "printed_verified": dict(Counter(str(bool(e["printed"] and e["printed"]["verified"])).lower() for e in held)),
        "has_text_layer": dict(Counter(str(e["pdf"]["has_text_layer"]).lower() for e in held)),
        "ocr_needed_books": sum(1 for e in held if (e["pdf"]["ocr_needed_pages"] or 0) > 0),
        "ocr_needed_pages": sum(e["pdf"]["ocr_needed_pages"] or 0 for e in held),
        "legacy_mapped_pages": sum(pm["legacy"]["mapped_pages"] for pm in page_maps.values()),
        "printed_labelled_pages": sum(pm["printed"]["labelled_pages"] for pm in page_maps.values()),
        "printed_verified_pages": sum(pm["printed"]["verified_pages"] for pm in page_maps.values()),
        "reader_books": {
            "total": len(reader),
            "pdf_held": sum(1 for e in reader if e.get("pdf")),
            "no_pdf": sum(1 for e in reader if not e.get("pdf")),
            "by_provenance": dict(Counter(e["provenance"] for e in reader)),
            "by_corpus_relation": dict(Counter(e["corpus"]["relation"] for e in reader)),
            "legacy_mapping_accepted": sum(1 for e in reader if e["legacy"]["mapped_pages"]),
            "printed_verified": sum(1 for e in reader if e["printed"] and e["printed"]["verified"]),
            "in_sun_search": sum(1 for e in reader if e["legacy"].get("in_sun_search")),
        },
        "alias_candidate_editions": sum(1 for e in editions if e["legacy"].get("alias_candidate")),
        "newer_platform_file_not_acquired": len(plan.get("newer_platform") or []),
        "platform_comparison_indeterminate": len(plan.get("indeterminate_platform") or []),
        "downloads": {
            "rows": len(downloads["rows"]),
            "by_stage": dict(Counter(r["stage"] for r in downloads["rows"])),
            "by_stage_subject": dict(Counter(f"{r['stage']}/{r['subject']}" for r in downloads["rows"])),
            "with_edition_id": sum(1 for r in downloads["rows"] if r["edition_id"]),
            "with_size": sum(1 for r in downloads["rows"] if r["size_bytes"] is not None),
            "upstream_offline": sorted(r["source_id"] for r in downloads["rows"] if r["upstream_status"] == "OFFLINE"),
        },
    }
    return c


def latest_timestamp(plan: dict, dl_dir: Path) -> str:
    stamps = []
    for e in plan["editions"]:
        for k in ("retrieved_at", "details_retrieved_at"):
            v = e["source"].get(k)
            if v:
                stamps.append(v)
    for f in dl_dir.glob("*__*.json"):
        v = read_json(f).get("retrieved_at")
        if v:
            stamps.append(v)
    if not stamps:
        raise SystemExit("no retrieval timestamp in the inputs")
    return max(stamps)


def build(args) -> int:
    plan_path, pm_dir, dl_dir, details_dir = Path(args.plan), Path(args.page_maps), Path(args.downloads), Path(args.details)
    out_root = Path(args.out)
    plan = read_json(plan_path)
    page_maps: dict[str, dict] = {}
    inputs = {"plan.json": file_hashes(plan_path, ("sha256",))["sha256"]}
    for e in plan["editions"]:
        if e.get("pdf"):
            f = pm_dir / f"{e['edition_id']}.json"
            if not f.exists():
                raise SystemExit(f"missing page map for {e['edition_id']}")
            page_maps[e["edition_id"]] = read_json(f)
            inputs[f"page-maps/{f.name}"] = file_hashes(f, ("sha256",))["sha256"]
            if page_maps[e["edition_id"]]["pdf_sha256"] != e["pdf"]["sha256"]:
                raise SystemExit(f"page map of {e['edition_id']} was built from another PDF")
    for f in sorted(dl_dir.glob("*__*.json")):
        inputs[f"downloads/{f.name}"] = file_hashes(f, ("sha256",))["sha256"]
    details_digest = sha256_text("\n".join(
        f"{f.name}:{file_hashes(f, ('sha256',))['sha256']}" for f in sorted(details_dir.glob("*.json"))
        if not f.name.startswith("_")))
    inputs["details/*"] = details_digest
    for extra in args.stats or []:
        p = Path(extra)
        if p.exists():
            inputs[f"stats/{p.name}"] = file_hashes(p, ("sha256",))["sha256"]
    tool_dir = Path(__file__).resolve().parent
    tools = {f.name: file_hashes(f, ("sha256",))["sha256"] for f in sorted(tool_dir.glob("*.py"))}
    stamp = latest_timestamp(plan, dl_dir)
    input_hash = sha256_text(dumps({"inputs": inputs, "tools": tools}))
    release_id = stamp.replace("-", "").replace(":", "") + "-" + input_hash[:8]

    editions = [finalize_edition(e, page_maps.get(e["edition_id"])) for e in plan["editions"]]
    editions.sort(key=lambda e: e["edition_id"])
    downloads = build_downloads(dl_dir, details_dir, editions)
    downloads["release_id"] = release_id
    reader_index = sorted(
        ({"sun_book_key": e["legacy"]["sun_book_key"], "jc_short_key": e["legacy"]["jc_short_key"],
          "edition_id": e["edition_id"],
          "candidate_edition_ids": sorted(x["edition_id"] for x in editions
                                          if (x["legacy"].get("alias_candidate") or {}).get("sun_book_key")
                                          == e["legacy"]["sun_book_key"])}
         for e in editions if e["legacy"].get("sun_book_key")), key=lambda r: r["jc_short_key"])
    catalog = {"contract": CONTRACT, "release_id": release_id, "generated_at": stamp, "region_default": "CN",
               "editions": editions, "reader_books": reader_index}
    try:
        import pymupdf
        pymupdf_version = pymupdf.__version__
    except ImportError:  # the builder itself does not need it
        pymupdf_version = None
    stats = {}
    for extra in args.stats or []:
        p = Path(extra)
        if not p.exists():
            continue
        d = read_json(p)
        if p.name == "acquire-state.json":
            st = Counter(v["state"].split(":")[0] for v in d["items"].values())
            stats["acquire"] = {"items": len(d["items"]), "by_state": dict(st),
                                "failed": {k: v["state"] for k, v in sorted(d["items"].items())
                                           if v["state"].startswith("failed")},
                                "bytes_verified": sum(v.get("bytes", 0) for v in d["items"].values()
                                                      if v["state"] == "verified"),
                                "runs": d.get("runs", [])}
        elif p.name == "_ledger.json":
            stats[f"requests:{p.parent.name}"] = {"requests": d["requests"], "non_200": d["non_200"]}
    release = {
        "contract": CONTRACT, "release_id": release_id, "generated_at": stamp,
        "tool": {"version": TOOL_VERSION, "extractor_version": EXTRACTOR_VERSION,
                 "page_map_version": PAGE_MAP_VERSION, "python": platform.python_version(),
                 "pymupdf": pymupdf_version, "scripts_sha256": tools},
        "inputs": inputs, "input_hash": input_hash,
        "counts": counts(editions, page_maps, downloads, plan),
        "run_stats": stats,
        "newer_platform": plan.get("newer_platform") or [],
        "indeterminate_platform": plan.get("indeterminate_platform") or [],
        "junior_resolved_by_bytes": plan.get("junior_resolved_by_bytes") or [],
        "issues": plan.get("issues") or [],
    }

    files: dict[str, bytes] = {
        "catalog.json": dumps(catalog).encode(),
        "downloads.json": dumps(downloads).encode(),
        "RELEASE.json": dumps(release).encode(),
    }
    for eid, pm in sorted(page_maps.items()):
        files[f"page-maps/{eid}.json"] = dumps(pm).encode()
    sums = "".join(f"{sha256_bytes(data)}  {name}\n" for name, data in sorted(files.items()))
    files["SHA256SUMS"] = sums.encode()

    target = out_root / release_id
    if target.exists():
        existing = (target / "SHA256SUMS").read_bytes() if (target / "SHA256SUMS").exists() else b""
        if existing == files["SHA256SUMS"]:
            print(f"unchanged {release_id}")
            print(target)
            return 0
        raise SystemExit(f"release {release_id} exists with different content; refusing to overwrite")
    tmp = out_root / f".{release_id}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    for name, data in files.items():
        write_atomic(tmp / name, data)
    tmp.rename(target)
    print(f"built {release_id} files={len(files)}")
    print(target)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", required=True)
    p.add_argument("--page-maps", required=True)
    p.add_argument("--downloads", required=True)
    p.add_argument("--details", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--stats", action="append", help="acquire-state.json / _ledger.json files to summarise")
    return build(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
