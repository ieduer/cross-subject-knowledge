#!/usr/bin/env python3
"""Identity resolution for the preview set (the JC/SUN reader books). Offline; reads cached inputs.

Stages (each idempotent; later stages need the earlier network step to have run):
  ids       -> detail-ids.json   source ids whose SmartEdu details JSON is needed, plus the
                                 local-file hashes (sha256 / md5 / bytes) of the reused PDFs
  job       -> job.json          acquisition job (source ids with expected ti_md5 / ti_size)
  editions  -> plan.json         edition records + per-edition legacy spec, holdings.json

Identity rules (never by title):
  * SUN book_key == JC id; JC shortKey == md5(id)[:12] (asserted for every book).
  * 高中: JC contentId is the SmartEdu id only when that id exists in the live download catalog.
  * 初中: the local source PDF is tied to its SmartEdu id by SHA-256 equality with a registered
    learn-books PDF (baseline join), or by MD5+size equality with the platform's details JSON.
    Titles are used only to nominate candidates for that byte comparison.
  * The five 高中语文 JKS aliases are hand-entered candidates: their PDFs are catalogued as their
    own editions and never receive `legacy.sun_book_key`; the JC book keeps a PDF-less record.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (MD5_RE, UUID_RE, file_hashes, fold, jc_short_key, read_json, sha256_text,  # noqa: E402
                    write_json)

# JKS reader slug -> JC shortKey -> SmartEdu id (keben build-registry.mjs JKS_ALIASES; from JKS lib/books.ts)
JKS_ALIASES = [
    {"slug": "gzyw_bx_s", "shortKey": "1082bfb4a71a", "smartEduId": "b8e9a3fe-dae7-49c0-86cb-d146f883fd8e"},
    {"slug": "gzyw_bx_x", "shortKey": "9535dacc9628", "smartEduId": "9085151a-b698-4b28-8c00-2c4aaf0c91ad"},
    {"slug": "gzyw_xxbx_s", "shortKey": "47e3538c5b76", "smartEduId": "3b7a3baf-4e1e-4380-b2cc-3bf330d00cc3"},
    {"slug": "gzyw_xxbx_z", "shortKey": "0433798ca156", "smartEduId": "da694670-f25b-46a0-9c3f-a31f5a2f131a"},
    {"slug": "gzyw_xxbx_x", "shortKey": "822b23a796e3", "smartEduId": "2de54e6d-1f82-4fdc-9f26-c94dfed9c5af"},
]
REVISION_PREFIX = "(根据2022年版课程标准修订)"
UPSTREAM_PDF = "https://jks-ai.bdfz.net/pdf/"


def norm_title(title: str) -> str:
    t = (title or "").replace("（", "(").replace("）", ")")
    return fold(t.replace(REVISION_PREFIX, ""))


def load_downloads(dl_dir: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for f in sorted(dl_dir.glob("*__*.json")):
        phase, subject = f.stem.split("__")
        for r in read_json(f)["body"]:
            rows.setdefault(r["id"], {**r, "group_phase": phase, "group_subject": subject})
    return rows


def load_jc(inputs: Path) -> tuple[list[dict], set[str]]:
    jc = read_json(inputs / "jc-catalog.json")["books"]
    for b in jc:
        if jc_short_key(b["id"]) != b["shortKey"]:
            raise SystemExit(f"shortKey != md5(id)[:12]: {b['id']}")
    sun = {b["book_key"] for g in read_json(inputs / "sun-books.json") for b in g["books"]}
    return jc, sun


def local_hashes(inputs: Path, local_root: Path, out: Path) -> dict[str, dict]:
    """Hash the reused local PDFs once (read in place, never modified)."""
    cached = read_json(out, default={})
    join = read_json(inputs / "junior_sha_join.json")
    changed = False
    for row in join:
        path = local_root / row["path"]
        st = path.stat()
        c = cached.get(row["path"])
        if c and c["bytes"] == st.st_size and c["mtime_ns"] == st.st_mtime_ns:
            continue
        h = file_hashes(path)
        if h["sha256"] != row["sha256"]:
            raise SystemExit(f"local PDF no longer matches the baseline hash: {row['path']}")
        cached[row["path"]] = {**h, "mtime_ns": st.st_mtime_ns,
                               "mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))}
        changed = True
    if changed or not out.exists():
        write_json(out, cached)
    return cached


def pdf_item(details_body: dict) -> dict | None:
    items = [t for t in details_body.get("ti_items") or []
             if (t.get("ti_format") or "").lower() == "pdf" and t.get("ti_file_flag") == "source"]
    if len(items) != 1:
        items = [t for t in details_body.get("ti_items") or [] if (t.get("ti_format") or "").lower() == "pdf"]
    if len(items) != 1:
        return None
    t = items[0]
    pages = None
    for req in ((t.get("custom_properties") or {}).get("requirements") or []):
        if req.get("name") == "pagesize":
            try:
                pages = int(req.get("value"))
            except (TypeError, ValueError):
                pages = None
    storage = t.get("ti_storage") or ""
    return {"ti_md5": (t.get("ti_md5") or None), "ti_size": int(t["ti_size"]) if t.get("ti_size") else None,
            "platform_pages": pages, "storage_filename": storage.rsplit("/", 1)[-1] or None}


def details_meta(wrapped: dict) -> dict:
    b = wrapped["body"]
    tags = {}
    for t in b.get("tag_list") or []:
        tags.setdefault(t.get("tag_dimension_id"), t.get("tag_name"))
    providers = [p.get("name") for p in b.get("provider_list") or [] if p.get("type") == "provider"]
    return {
        "title": b.get("title"), "stage": tags.get("zxxxd"), "subject": tags.get("zxxxk"),
        "edition_label": tags.get("zxxbb"), "volume": tags.get("zxxcc"), "grade": tags.get("zxxnj"),
        "year_tag": tags.get("bknd"), "publisher": providers[0] if providers else None,
        "online_time": b.get("online_time"), "update_time": b.get("update_time"),
        "create_time": b.get("create_time"), "status": b.get("status"),
        "retrieved_at": wrapped.get("retrieved_at"), "retrieved_via": wrapped.get("url"),
        "pdf": pdf_item(b),
    }


def junior_candidates(row: dict, downloads: dict[str, dict]) -> list[str]:
    """Title only nominates; bytes decide later."""
    title = Path(row["path"]).stem
    want = norm_title(title)
    exact = sorted(i for i, r in downloads.items()
                   if r["group_phase"] == "初中" and norm_title(r["title"]) == want)
    if exact:
        return exact
    return sorted(i for i, r in downloads.items()
                  if r["group_phase"] == "初中" and (want in norm_title(r["title"]) or norm_title(r["title"]) in want))


def stage_ids(args) -> int:
    inputs, state = Path(args.inputs), Path(args.state)
    jc, _ = load_jc(inputs)
    downloads = load_downloads(Path(args.downloads))
    local_hashes(inputs, Path(args.local_root), state / "local-hashes.json")
    join = read_json(inputs / "junior_sha_join.json")
    ids: list[str] = []
    for row in join:
        if row.get("source_id"):
            ids.append(row["source_id"])
    for b in jc:
        if b["phase"] == "高中" and UUID_RE.match(b["contentId"] or "") and b["contentId"] in downloads:
            ids.append(b["contentId"])
    ids += [a["smartEduId"] for a in JKS_ALIASES]
    primary = list(dict.fromkeys(ids))
    cands: dict[str, list[str]] = {}
    budget = args.max_details - len(primary)
    for row in sorted((r for r in join if not r.get("source_id")), key=lambda r: r["path"]):
        c = [i for i in junior_candidates(row, downloads) if i not in primary]
        take = c[:max(0, budget)]
        budget -= len(take)
        cands[row["jc_shortKey"]] = {"nominated": c, "requested": take}
    all_ids = primary + [i for v in cands.values() for i in v["requested"] if i not in primary]
    all_ids = list(dict.fromkeys(all_ids))
    write_json(state / "detail-ids.json", all_ids)
    write_json(state / "junior-candidates.json", cands)
    print(f"detail ids: {len(all_ids)} (primary {len(primary)}, candidates {len(all_ids) - len(primary)})")
    return 0


def high_school_targets(jc: list[dict], downloads: dict[str, dict]) -> list[dict]:
    out = []
    for b in jc:
        if b["phase"] != "高中":
            continue
        if UUID_RE.match(b["contentId"] or "") and b["contentId"] in downloads:
            out.append({"source_id": b["contentId"], "basis": "identifier:jc-content-id", "jc": b})
    by_short = {b["shortKey"]: b for b in jc}
    for a in JKS_ALIASES:
        out.append({"source_id": a["smartEduId"], "basis": "identifier:jks-slug", "jc": by_short[a["shortKey"]],
                    "alias": a})
    return out


def stage_job(args) -> int:
    inputs, state = Path(args.inputs), Path(args.state)
    jc, _ = load_jc(inputs)
    downloads = load_downloads(Path(args.downloads))
    items, missing = [], []
    seen = set()
    for t in high_school_targets(jc, downloads):
        sid = t["source_id"]
        if sid in seen:
            raise SystemExit(f"two reader books claim one source id: {sid}")
        seen.add(sid)
        f = Path(args.details) / f"{sid}.json"
        if not f.exists():
            missing.append(sid)
            continue
        meta = details_meta(read_json(f))
        if not meta["pdf"]:
            missing.append(sid)
            continue
        items.append({"source_id": sid, "ti_md5": meta["pdf"]["ti_md5"], "ti_size": meta["pdf"]["ti_size"]})
    write_json(state / "job.json", {"items": items, "missing_details": missing})
    total = sum(i["ti_size"] or 0 for i in items)
    print(f"job items: {len(items)} expected_bytes: {total} missing_details: {len(missing)}")
    return 0


def work_id(stage, subject, title, edition_label, publisher) -> str:
    return "cnw-" + sha256_text("|".join(
        ["CN", stage or "", subject or "", norm_title(title or ""), edition_label or "", publisher or ""]))[:16]


def base_record(meta: dict | None, fallback: dict) -> dict:
    m = meta or {}
    rec = {
        "region": "CN",
        "stage": m.get("stage") or fallback.get("stage"),
        "subject": m.get("subject") or fallback.get("subject"),
        "title": m.get("title") or fallback.get("title"),
        "volume": m.get("volume"),
        "publisher": m.get("publisher"),
        "edition_label": m.get("edition_label") or fallback.get("edition_label"),
        "metadata_basis": "smartedu-details" if meta else fallback.get("basis"),
    }
    rec["work_id"] = work_id(rec["stage"], rec["subject"], rec["title"], rec["edition_label"], rec["publisher"])
    return rec


def source_block(sid, meta, *, current: bool | None, retrieved_at, retrieved_via, sid_basis) -> dict:
    m = meta or {}
    pdf = m.get("pdf") or {}
    return {
        "provider": "smartedu", "source_id": sid, "source_id_basis": sid_basis,
        "catalog_module": "tch_material" if sid else None,
        # Platform times and file facts describe the file the platform served at `details_retrieved_at`.
        # They are attached to an edition only when that edition IS that file.
        "online_time": m.get("online_time") if current else None,
        "update_time": m.get("update_time") if current else None,
        "ti_md5": pdf.get("ti_md5") if current else None,
        "ti_size": pdf.get("ti_size") if current else None,
        "storage_filename": pdf.get("storage_filename") if current else None,
        "platform_current": current,
        "platform_status": m.get("status"),   # ONLINE / OFFLINE as of details_retrieved_at
        "details_retrieved_at": m.get("retrieved_at"),
        "retrieved_at": retrieved_at, "retrieved_via": retrieved_via,
    }


def compare_local(local: dict, meta: dict | None) -> str:
    """same | different | indeterminate | unknown — local bytes vs. the platform's current file."""
    pdf = (meta or {}).get("pdf")
    if not pdf or pdf.get("ti_size") is None:
        return "unknown"
    if pdf["ti_size"] != local["bytes"]:
        return "different"
    md5 = (pdf.get("ti_md5") or "").lower()
    if MD5_RE.match(md5):
        return "same" if md5 == local["md5"] else "different"
    return "indeterminate"


def stage_editions(args) -> int:
    inputs, state = Path(args.inputs), Path(args.state)
    jc, sun_keys = load_jc(inputs)
    by_short = {b["shortKey"]: b for b in jc}
    downloads = load_downloads(Path(args.downloads))
    details_dir = Path(args.details)
    local_root = Path(args.local_root)
    lh = local_hashes(inputs, local_root, state / "local-hashes.json")
    join = read_json(inputs / "junior_sha_join.json")
    cands = read_json(state / "junior-candidates.json", default={})
    acq = read_json(state / "acquire-state.json", default={"items": {}})["items"]
    summ = read_json(state / "inspect-summary.json", default={"pdfs": {}})["pdfs"]
    store = Path(args.store)

    def meta_of(sid):
        f = details_dir / f"{sid}.json"
        return details_meta(read_json(f)) if sid and f.exists() else None

    def pdf_block(sha, md5, nbytes):
        s = summ.get(sha)
        return {"sha256": sha, "md5": md5, "bytes": nbytes,
                "pages": s["pages"] if s else None,
                "has_text_layer": s["has_text_layer"] if s else None,
                "producer": s["producer"] if s else None,
                "text_pages": s["text_pages"] if s else None,
                "ocr_needed_pages": s["ocr_needed_pages"] if s else None,
                "blank_pages": s["blank_pages"] if s else None}

    editions, holdings, issues = [], {}, []
    newer_platform, indeterminate, byte_resolved = [], [], []

    # --- 初中: 48 reused local source PDFs ---------------------------------------------------
    for row in sorted(join, key=lambda r: r["jc_shortKey"]):
        b = by_short[row["jc_shortKey"]]
        local = lh[row["path"]]
        sid, sid_basis = row.get("source_id"), None
        if sid:
            sid_basis = "bytes:sha256=learn-books"
        else:
            for cid in (cands.get(row["jc_shortKey"]) or {}).get("requested", []):
                if compare_local(local, meta_of(cid)) == "same":
                    sid, sid_basis = cid, "bytes:md5=ti_md5"
                    byte_resolved.append({"jc_short_key": b["shortKey"], "source_id": cid})
                    break
        meta = meta_of(sid)
        cmp_ = compare_local(local, meta) if sid else "unknown"
        current = {"same": True, "different": False}.get(cmp_)
        if b["pages"] != row["pdf_pages"]:
            issues.append(f"{b['shortKey']}: JC pages {b['pages']} != local PDF pages {row['pdf_pages']}")
        title_local = Path(row["path"]).stem
        rec = base_record(meta, {"stage": b["phase"], "subject": b["subject"], "title": title_local,
                                 "edition_label": None, "basis": "local-download-index"})
        rec.update({
            "edition_id": f"cn-{sid}-{local['sha256'][:12]}" if sid else f"cn-x-{local['sha256'][:16]}",
            "source": source_block(sid, meta, current=current, retrieved_at=local["mtime_utc"],
                                   retrieved_via="local reuse: textbook_ai_migration/tmp/smartedu_junior "
                                                 "(jks.sh private lineage; retrieved_at = file mtime)",
                                   sid_basis=sid_basis),
            "pdf": pdf_block(local["sha256"], local["md5"], local["bytes"]),
            "storage": {"vps": None, "drive": None,
                        "upstream": {"available": bool(current), "url": UPSTREAM_PDF + sid if current else None,
                                     "note": None if current else (
                                         "platform record is OFFLINE" if (meta or {}).get("status") == "OFFLINE"
                                         else "platform now serves a different file under this id" if sid
                                         else "SmartEdu id unknown")}},
            "legacy": {"sun_book_key": b["id"], "jc_short_key": b["shortKey"], "jks_slug": None,
                       "basis": "bytes:source-pdf", "in_sun_search": b["id"] in sun_keys, "alias_candidate": None},
            "status": "active",
            "provenance": "byte-proven-original",
            "legacy_spec": {"mode": "same-bytes", "legacy_pages": b["pages"], "sun_book_key": b["id"],
                            "subject": b["subject"]},
        })
        editions.append(rec)
        holdings[local["sha256"]] = {"path": str(local_root / row["path"]), "origin": "local-reuse",
                                     "bytes": local["bytes"]}
        if cmp_ == "different":
            p = meta["pdf"]
            newer = base_record(meta, {})
            newer.update({
                "edition_id": f"cn-{sid}-pending",
                "source": source_block(sid, meta, current=True, retrieved_at=None,
                                       retrieved_via=None, sid_basis=sid_basis),
                "pdf": None, "platform_pages": p.get("platform_pages"),
                "storage": {"vps": None, "drive": None,
                            "upstream": {"available": True, "url": UPSTREAM_PDF + sid}},
                "legacy": {"sun_book_key": None, "jc_short_key": None, "jks_slug": None, "basis": None,
                           "in_sun_search": False, "alias_candidate": None},
                "status": "pending-verification",
                "provenance": "platform-current-not-acquired",
                "supersedes_candidate": rec["edition_id"],
                "legacy_spec": {"mode": "none", "relation": "none", "reason": "not acquired in this run"},
            })
            editions.append(newer)
            newer_platform.append({"source_id": sid, "jc_short_key": b["shortKey"], "title": rec["title"],
                                   "held_bytes": local["bytes"], "platform_bytes": p["ti_size"],
                                   "platform_update_time": meta.get("update_time"),
                                   "held_pages": row["pdf_pages"], "platform_pages": p.get("platform_pages")})
        elif cmp_ == "indeterminate":
            indeterminate.append({"source_id": sid, "jc_short_key": b["shortKey"], "title": rec["title"]})

    # --- 高中 with a known SmartEdu id (JC contentId) and the five JKS aliases -------------------
    for t in high_school_targets(jc, downloads):
        sid, b, alias = t["source_id"], t["jc"], t.get("alias")
        meta = meta_of(sid)
        st = acq.get(sid) or {}
        dl = downloads.get(sid) or {}
        rec = base_record(meta, {"stage": b["phase"], "subject": b["subject"], "title": dl.get("title") or b["title"],
                                 "edition_label": dl.get("edition"), "basis": "download-catalog"})
        in_sun = b["id"] in sun_keys
        if st.get("state") == "verified":
            sha = st["sha256"]
            rec["edition_id"] = f"cn-{sid}-{sha[:12]}"
            rec["pdf"] = pdf_block(sha, st["md5"], st["bytes"])
            rec["source"] = source_block(sid, meta, current=True, retrieved_at=st["retrieved_at"],
                                         retrieved_via=st["retrieved_via"], sid_basis=t["basis"])
            # HTTP validators as received; the raw Content-Disposition stays in the acquisition state
            # (the file name is already recorded, correctly decoded, as storage_filename).
            rec["source"]["http"] = {k: v for k, v in ((st.get("http") or {}).get("headers") or {}).items()
                                     if k != "content-disposition"}
            rec["source"]["md5_check"] = st.get("md5_check")
            rec["status"] = "active"
            rec["provenance"] = "platform-current"
            holdings[sha] = {"path": str(store / f"{sha}.pdf"), "origin": "acquired", "bytes": st["bytes"]}
            if in_sun:
                spec = {"mode": "anchors", "legacy_pages": b["pages"], "sun_book_key": b["id"],
                        "subject": b["subject"]}
            else:
                spec = {"mode": "none", "relation": "none", "legacy_pages": b["pages"],
                        "reason": "book is not in the SUN search corpus (page images only)"}
        else:
            rec["edition_id"] = f"cn-{sid}-pending"
            rec["pdf"] = None
            rec["source"] = source_block(sid, meta, current=True, retrieved_at=None, retrieved_via=None,
                                         sid_basis=t["basis"])
            rec["status"] = "pending-verification"
            rec["provenance"] = "acquisition-" + (st.get("state") or "not-attempted")
            spec = {"mode": "none", "relation": "none", "reason": "PDF not acquired: " + (st.get("state") or "not attempted")}
        rec["storage"] = {"vps": None, "drive": None, "upstream": {"available": True, "url": UPSTREAM_PDF + sid}}
        if alias:
            rec["legacy"] = {"sun_book_key": None, "jc_short_key": None, "jks_slug": alias["slug"],
                             "basis": None, "in_sun_search": False,
                             "alias_candidate": {"sun_book_key": b["id"], "jc_short_key": b["shortKey"],
                                                 "basis": "hand-entered JKS alias (keben JKS_ALIASES); "
                                                          "not an identifier or byte proof"}}
        else:
            rec["legacy"] = {"sun_book_key": b["id"], "jc_short_key": b["shortKey"], "jks_slug": None,
                             "basis": "identifier:jc-content-id", "in_sun_search": in_sun, "alias_candidate": None}
        rec["legacy_spec"] = spec
        editions.append(rec)

    # --- reader books with no proven PDF: 高中 contentId unknown (incl. the five alias books) ----
    claimed = {e["legacy"]["sun_book_key"] for e in editions if e["legacy"]["sun_book_key"]}
    alias_shorts = {a["shortKey"] for a in JKS_ALIASES}
    for b in jc:
        if b["id"] in claimed:
            continue
        rec = base_record(None, {"stage": b["phase"], "subject": b["subject"], "title": b["title"],
                                 "edition_label": b.get("edition") or None, "basis": "jc-catalog"})
        rec.update({
            "edition_id": f"cn-x-legacy-{b['shortKey']}",
            "source": source_block(None, None, current=None, retrieved_at=None, retrieved_via=None, sid_basis=None),
            "pdf": None,
            "storage": {"vps": None, "drive": None, "upstream": {"available": False, "url": None}},
            "legacy": {"sun_book_key": b["id"], "jc_short_key": b["shortKey"], "jks_slug": None,
                       "basis": "identifier:jc-book-key", "in_sun_search": b["id"] in sun_keys,
                       "alias_candidate": None},
            "status": "pending-verification",
            "provenance": "no-source-id" if b["shortKey"] not in alias_shorts else "alias-only",
            "jc_content_id": b["contentId"],
            "legacy_spec": {"mode": "none", "relation": "none", "legacy_pages": b["pages"],
                            "reason": "no PDF: SmartEdu id unknown for this reader book"},
        })
        editions.append(rec)

    ids = [e["edition_id"] for e in editions]
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate edition ids")
    editions.sort(key=lambda e: e["edition_id"])
    write_json(state / "plan.json", {
        "editions": editions, "issues": issues, "newer_platform": newer_platform,
        "indeterminate_platform": indeterminate, "junior_resolved_by_bytes": byte_resolved,
        "junior_candidates": cands, "reader_books": len(jc), "sun_books": len(sun_keys),
    })
    write_json(state / "holdings.json", holdings)
    print(f"editions={len(editions)} with_pdf={sum(1 for e in editions if e['pdf'])} holdings={len(holdings)} "
          f"newer_platform={len(newer_platform)} indeterminate={len(indeterminate)} "
          f"junior_resolved_by_bytes={len(byte_resolved)} issues={len(issues)}")
    return 1 if issues else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stage", choices=["ids", "job", "editions"])
    p.add_argument("--inputs", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--downloads", required=True)
    p.add_argument("--details", required=True)
    p.add_argument("--local-root", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--max-details", type=int, default=160)
    args = p.parse_args(argv)
    return {"ids": stage_ids, "job": stage_job, "editions": stage_editions}[args.stage](args)


if __name__ == "__main__":
    sys.exit(main())
