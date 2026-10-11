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


# --- round 3: second editions and content-decided candidates ------------------------------------
# Edition wording differs between the reader catalog and the platform; these pairs only ORDER the
# nominations of one book (same stage, same subject group, same normalised title). Identity is
# decided later by page-by-page content comparison, never by any of this.
EDITION_HINTS = {"中图版": ("中图版", "中图中华地图版"), "沪教版": ("中图中华地图版", "中图版"),
                 "A版": ("人教A版",), "B版": ("人教版(B版)",), "人教版": ("统编版", "人教版"),
                 "人民出版社版": ("统编版",)}
MAX_CANDIDATES_PER_BOOK = 2
SUBJECT_GROUP = {"生物学": "生物", "道德与法治": "思想政治"}


def content_candidates(jc_book: dict, downloads: dict[str, dict], taken: set[str]) -> list[str]:
    want = norm_title(jc_book["title"])
    group = SUBJECT_GROUP.get(jc_book["subject"], jc_book["subject"])
    rows = [r for i, r in sorted(downloads.items())
            if i not in taken and r["group_phase"] == jc_book["phase"] and r["group_subject"] == group
            and norm_title(r["title"]) == want]
    hints = [fold(h) for h in EDITION_HINTS.get(jc_book.get("edition") or "", ())]

    def rank(r):
        ed = fold(r.get("edition") or "")
        hit = [n for n, h in enumerate(hints) if ed == h or ed == fold("配套") + h]
        return (hit[0] if hit else 99, r["id"])
    rows.sort(key=rank)
    if hints:
        rows = [r for r in rows if rank(r)[0] != 99] or rows
    return [r["id"] for r in rows[:MAX_CANDIDATES_PER_BOOK]]


def stage_r3(args) -> int:
    """Round-3 work list: newer platform files of held works + content candidates for PDF-less books."""
    inputs, state = Path(args.inputs), Path(args.state)
    jc, _ = load_jc(inputs)
    downloads = load_downloads(Path(args.downloads))
    plan = read_json(state / "plan.json")
    acq = read_json(state / "acquire-state.json", default={"items": {}})["items"]
    taken = {e["source"]["source_id"] for e in plan["editions"] if e["source"]["source_id"]}
    newer = sorted({n["source_id"] for n in plan.get("newer_platform") or []}
                   | {e["source"]["source_id"] for e in plan["editions"]
                      if e.get("supersedes_edition_id") and e["source"]["source_id"]})
    by_key = {b["id"]: b for b in jc}
    cands = {}
    for e in plan["editions"]:
        key = e["legacy"].get("sun_book_key")
        if key and not e.get("pdf") and e["provenance"] in ("no-source-id", "content-candidate-rejected"):
            b = by_key[key]
            cands[b["shortKey"]] = content_candidates(b, downloads, taken - set(read_json(
                state / "content-candidates.json", default={}).get(b["shortKey"], [])))
    prev = read_json(state / "content-candidates.json", default={})
    for k, v in prev.items():          # nominations are stable across re-runs
        cands.setdefault(k, v)
        if not cands[k]:
            cands[k] = v
    write_json(state / "content-candidates.json", cands)
    ids = list(dict.fromkeys(newer + [i for v in cands.values() for i in v]))
    write_json(state / "detail-ids-r3.json", ids)
    items, missing = [], []
    for sid in ids:
        f = Path(args.details) / f"{sid}.json"
        meta = details_meta(read_json(f)) if f.exists() else None
        if not meta or not meta["pdf"]:
            missing.append(sid)
            continue
        if (acq.get(sid) or {}).get("state") == "verified" and sid not in newer and sid not in \
                {i for v in cands.values() for i in v}:
            continue
        items.append({"source_id": sid, "ti_md5": meta["pdf"]["ti_md5"], "ti_size": meta["pdf"]["ti_size"]})
    write_json(state / "job-r3.json", {"items": items, "missing_details": missing})
    print(f"newer={len(newer)} books_with_candidates={sum(1 for v in cands.values() if v)}/{len(cands)} "
          f"candidate_ids={len({i for v in cands.values() for i in v})} job_items={len(items)} "
          f"expected_bytes={sum(i['ti_size'] or 0 for i in items)} missing_details={len(missing)}")
    return 0


# --- xue / learn-books: files bound in the learn release ------------------------------------------
XUE_PDF = "https://xue.bdfz.net/books/{asset}/book.pdf"


def load_learn(inputs: Path) -> list[dict]:
    f = inputs / "learn-shelf.json"
    return read_json(f)["assets"] if f.exists() else []


def stage_learn(args) -> int:
    """Reduce the learn release files to the bound PDFs and plan which of them to fetch from xue.

    learn-shelf.json: one row per bound asset {asset_id, sha256, bytes, r2_md5, source_id, title, pages,
    stage, subject, publisher, acquired_at}. A bound file is fetched only when it is a content candidate
    for a reader book that has no SmartEdu id AND its bytes differ from everything already held.
    """
    inputs, state, learn_root = Path(args.inputs), Path(args.state), Path(args.learn_root)
    bindings = read_json(learn_root / "data" / "source-pdf-bindings.v1.json")
    baseline = {b["asset_id"]: b for b in read_json(learn_root / "data" / "textbook-baseline.v1.json")["books"]}
    assets = []
    for o in sorted(bindings["objects"], key=lambda o: o["asset_id"]):
        b = baseline.get(o["asset_id"]) or {}
        if (b.get("source") or {}).get("source_sha256") not in (None, o["sha256"]):
            raise SystemExit(f"learn baseline and binding disagree for {o['asset_id']}")
        assets.append({
            "asset_id": o["asset_id"], "sha256": o["sha256"], "bytes": o["size"], "r2_md5": (o.get("r2_etag") or "").strip('"'),
            "source_id": (b.get("upstream") or {}).get("source_id"), "title": b.get("title"),
            "pages": (b.get("extraction") or {}).get("page_count"),
            "stage": (b.get("stage") or {}).get("label"), "subject": (b.get("subject") or {}).get("label"),
            "publisher": (b.get("publisher") or {}).get("value"),
            "acquired_at": (b.get("source") or {}).get("acquired_at"),
        })
    write_json(inputs / "learn-shelf.json", {
        "source": "platforms/learn data/source-pdf-bindings.v1.json + data/textbook-baseline.v1.json",
        "bindings_revision": bindings.get("revision"), "content_root_sha256": bindings.get("content_root_sha256"),
        "assets": assets})
    acq = read_json(state / "acquire-state.json", default={"items": {}})["items"]
    held = {v["sha256"] for v in acq.values() if v.get("state") == "verified"}
    held |= {v["sha256"] for v in read_json(state / "local-hashes.json", default={}).values()}
    cand_ids = {i for v in read_json(state / "content-candidates.json", default={}).values() for i in v}
    items, same = [], []
    for a in assets:
        if a["source_id"] not in cand_ids:
            continue
        if a["sha256"] in held:
            same.append(a["asset_id"])
            continue
        items.append({"source_id": f"xue:{a['asset_id']}", "url": XUE_PDF.format(asset=a["asset_id"]),
                      "ti_md5": a["r2_md5"] or None, "ti_size": a["bytes"], "sha256": a["sha256"],
                      "expect_headers": {"x-xue-source-sha256": a["sha256"]}})
    write_json(state / "job-xue.json", {"items": items, "already_held_same_bytes": same})
    print(f"learn assets={len(assets)} candidates_for_no_id_books={len(items) + len(same)} "
          f"already_held_same_bytes={len(same)} to_fetch={len(items)} bytes={sum(i['ti_size'] for i in items)}")
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


LOCAL_REUSE = "local-reuse:smartedu-junior-2026-03"   # neutral token; retrieved_at = file mtime


def source_block(sid, meta, *, current: bool | None, retrieved_at, retrieved_via, sid_basis,
                 id_basis=None) -> dict:
    m = meta or {}
    pdf = m.get("pdf") or {}
    return {
        "provider": "smartedu", "source_id": sid, "source_id_basis": sid_basis,
        # How strongly the id is tied to THIS file today:
        #   md5-size-current         held bytes equal the platform's current file (MD5 + size)
        #   downloaded-by-id:md5-equal / :size-only   fetched through this id in this run
        #   baseline-join-unreproven  SHA-256 join with learn-books made by the baseline; the
        #                             platform no longer serves these bytes, so it cannot be re-proven
        #   platform-record          the record describes the platform file itself (not held)
        "id_basis": id_basis if sid else None,
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


COEXIST_LEARN = ("the file bound in xue's learn release (acquired 2026-07-15) is catalogued beside the platform's "
                 "current file of the same SmartEdu id")
COEXIST_REASSEMBLED = ("the earlier edition, reassembled from the legacy page images, is kept beside the platform's "
                       "current file so that old page links stay exact")
COEXIST = ("the file the corpus and page images were made from is kept beside the platform's current "
           "file of the same work")


def held_platform_record(sid: str, meta: dict | None, st: dict, sid_basis: str, fallback: dict, store: Path,
                         pdf_block, holdings: dict) -> dict:
    """Edition record of a file downloaded through its SmartEdu id in this pipeline (platform-current)."""
    sha = st["sha256"]
    rec = base_record(meta, fallback)
    rec.update({
        "edition_id": f"cn-{sid}-{sha[:12]}",
        "pdf": pdf_block(sha, st["md5"], st["bytes"]),
        "source": source_block(
            sid, meta, current=True, retrieved_at=st["retrieved_at"], retrieved_via=st["retrieved_via"],
            sid_basis=sid_basis,
            id_basis="downloaded-by-id:" + ("md5-equal" if st.get("md5_check") == "equal" else "size-only")),
        "status": "active", "provenance": "platform-current",
        "platform_current": True, "newer_edition_id": None, "supersedes_edition_id": None,
        "storage": {"vps": None, "drive": None,
                    "upstream": {"available": True, "url": UPSTREAM_PDF + sid, "note": None}},
    })
    # HTTP validators as received; the raw Content-Disposition stays in the acquisition state
    # (the file name is already recorded, correctly decoded, as storage_filename).
    rec["source"]["http"] = {k: v for k, v in ((st.get("http") or {}).get("headers") or {}).items()
                             if k != "content-disposition"}
    rec["source"]["md5_check"] = st.get("md5_check")
    holdings[sha] = {"path": str(store / f"{sha}.pdf"), "origin": "acquired", "bytes": st["bytes"]}
    return rec


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
        local_fallback = {"stage": b["phase"], "subject": b["subject"], "title": title_local,
                          "edition_label": None, "basis": "local-download-index"}
        if current is True:
            # The held file IS the platform's current file: the platform record describes it.
            rec = base_record(meta, local_fallback)
        else:
            # The platform record now describes ANOTHER file (replaced or offline). Title, volume,
            # publisher and edition label of the held file come only from evidence about the held
            # file: its name in the download index at retrieval time and the reader catalog.
            rec = base_record(None, local_fallback)
            if meta and meta.get("title"):
                # Same logical work as the platform record of this id (shared by its newer file).
                rec["work_id"] = base_record(meta, local_fallback)["work_id"]
        id_basis = "md5-size-current" if cmp_ == "same" else "baseline-join-unreproven"
        newer_id = f"cn-{sid}-pending" if cmp_ == "different" else None
        rec.update({
            "edition_id": f"cn-{sid}-{local['sha256'][:12]}" if sid else f"cn-x-{local['sha256'][:16]}",
            "platform_current": current is True,
            "newer_edition_id": newer_id, "supersedes_edition_id": None,
            "source": source_block(sid, meta, current=current, retrieved_at=local["mtime_utc"],
                                   retrieved_via=LOCAL_REUSE, sid_basis=sid_basis, id_basis=id_basis),
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
                            "jc_short_key": b["shortKey"], "subject": b["subject"]},
        })
        editions.append(rec)
        holdings[local["sha256"]] = {"path": str(local_root / row["path"]), "origin": "local-reuse",
                                     "bytes": local["bytes"]}
        st_new = acq.get(sid) or {} if sid else {}
        if cmp_ == "different" and st_new.get("state") == "verified" and st_new["sha256"] != local["sha256"]:
            # The platform's current file of this work has been acquired: a second edition.
            p = meta["pdf"]
            newer = held_platform_record(sid, meta, st_new, sid_basis, {}, store, pdf_block, holdings)
            newer.update({
                "supersedes_edition_id": rec["edition_id"], "coexists_reason": COEXIST,
                "legacy": {"sun_book_key": None, "jc_short_key": None, "jks_slug": None, "basis": None,
                           "in_sun_search": False,
                           "alias_candidate": {"sun_book_key": b["id"], "jc_short_key": b["shortKey"],
                                               "basis": "newer platform file of the same SmartEdu id; legacy rows "
                                                        "only where proven page by page"}},
                "legacy_spec": {"mode": "content", "legacy_pages": b["pages"], "sun_book_key": b["id"],
                                "jc_short_key": b["shortKey"], "subject": b["subject"]},
            })
            rec["newer_edition_id"] = newer["edition_id"]
            rec["coexists_reason"] = COEXIST
            editions.append(newer)
            newer_platform.append({"source_id": sid, "jc_short_key": b["shortKey"], "title": rec["title"],
                                   "held_title": rec["title"], "platform_title": newer["title"],
                                   "held_edition_id": rec["edition_id"], "newer_edition_id": newer["edition_id"],
                                   "held_bytes": local["bytes"], "platform_bytes": st_new["bytes"],
                                   "platform_update_time": meta.get("update_time"), "acquired": True,
                                   "held_pages": row["pdf_pages"], "platform_pages": p.get("platform_pages")})
        elif cmp_ == "different":
            p = meta["pdf"]
            newer = base_record(meta, {})
            newer.update({
                "edition_id": f"cn-{sid}-pending",
                "platform_current": True, "newer_edition_id": None,
                "supersedes_edition_id": rec["edition_id"],
                "source": source_block(sid, meta, current=True, retrieved_at=None,
                                       retrieved_via=None, sid_basis=sid_basis, id_basis="platform-record"),
                "pdf": None, "platform_pages": p.get("platform_pages"),
                "storage": {"vps": None, "drive": None,
                            "upstream": {"available": True, "url": UPSTREAM_PDF + sid, "note": None}},
                "legacy": {"sun_book_key": None, "jc_short_key": None, "jks_slug": None, "basis": None,
                           "in_sun_search": False, "alias_candidate": None},
                "status": "pending-verification",
                "provenance": "platform-current-not-acquired",
                "legacy_spec": {"mode": "none", "relation": "none", "reason": "not acquired in this run"},
            })
            editions.append(newer)
            newer_platform.append({"source_id": sid, "jc_short_key": b["shortKey"], "title": rec["title"],
                                   "held_title": rec["title"], "platform_title": newer["title"],
                                   "held_edition_id": rec["edition_id"], "newer_edition_id": newer["edition_id"],
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
            rec = held_platform_record(sid, meta, st, t["basis"], {
                "stage": b["phase"], "subject": b["subject"], "title": dl.get("title") or b["title"],
                "edition_label": dl.get("edition"), "basis": "download-catalog"}, store, pdf_block, holdings)
            # Every legacy page is compared: corpus text from the offline database where the book is
            # in the corpus, and the page image itself.
            spec = {"mode": "content", "legacy_pages": b["pages"], "sun_book_key": b["id"],
                    "jc_short_key": b["shortKey"], "subject": b["subject"]}
        else:
            rec["edition_id"] = f"cn-{sid}-pending"
            rec["pdf"] = None
            rec["source"] = source_block(sid, meta, current=True, retrieved_at=None, retrieved_via=None,
                                         sid_basis=t["basis"], id_basis="platform-record")
            rec["status"] = "pending-verification"
            rec["provenance"] = "acquisition-" + (st.get("state") or "not-attempted")
            spec = {"mode": "none", "relation": "none", "reason": "PDF not acquired: " + (st.get("state") or "not attempted")}
        rec["storage"] = {"vps": None, "drive": None,
                          "upstream": {"available": True, "url": UPSTREAM_PDF + sid, "note": None}}
        rec.update({"platform_current": True, "newer_edition_id": None, "supersedes_edition_id": None})
        if alias:
            rec["content_claim"] = True   # may be given the reader book by page-by-page proof (build step)
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

    # --- content candidates for reader books without a SmartEdu id --------------------------------
    by_id = {b["id"]: b for b in jc}
    used = {e["source"]["source_id"] for e in editions if e["source"]["source_id"]}
    for short, ids_ in sorted(read_json(state / "content-candidates.json", default={}).items()):
        b = by_short[short]
        for sid in ids_:
            st = acq.get(sid) or {}
            if st.get("state") != "verified" or sid in used:
                continue
            used.add(sid)
            dl = downloads.get(sid) or {}
            rec = held_platform_record(sid, meta_of(sid), st, "identifier:download", {
                "stage": b["phase"], "subject": dl.get("group_subject"), "title": dl.get("title"),
                "edition_label": dl.get("edition"), "basis": "download-catalog"}, store, pdf_block, holdings)
            rec["content_claim"] = True
            rec["legacy"] = {"sun_book_key": None, "jc_short_key": None, "jks_slug": None, "basis": None,
                             "in_sun_search": False,
                             "alias_candidate": {"sun_book_key": b["id"], "jc_short_key": short,
                                                 "basis": "candidate nominated by stage, subject group and title; "
                                                          "identity is decided only by page-by-page comparison"}}
            rec["legacy_spec"] = {"mode": "content", "legacy_pages": b["pages"], "sun_book_key": b["id"],
                                  "jc_short_key": short, "subject": b["subject"]}
            editions.append(rec)

    # --- editions reassembled from the legacy page images (owner decision R1/R2) -------------------
    # Only for the listed books: the platform's file under the book's SmartEdu id is another edition,
    # so a PDF made of the legacy images themselves becomes the edition that answers old page links.
    for short, r in sorted(read_json(state / "reassembled.json", default={}).items()):
        b = by_short[short]
        cur = next(e for e in editions if e["legacy"].get("sun_book_key") == b["id"])
        sid = cur["source"]["source_id"]
        rec = base_record(None, {"stage": b["phase"], "subject": cur["subject"], "title": b["title"],
                                 "edition_label": b.get("edition") or None, "basis": "jc-catalog"})
        rec.update({
            "edition_id": f"cn-{sid}-{r['sha256'][:12]}", "work_id": cur["work_id"],
            "pdf": pdf_block(r["sha256"], r["md5"], r["bytes"]),
            "source": source_block(sid, meta_of(sid), current=False, retrieved_at=None,
                                   retrieved_via="reassembled:legacy-page-images",
                                   sid_basis="identifier:jc-content-id", id_basis="reassembled-legacy-images"),
            "status": "active", "provenance": "reassembled-from-legacy-images",
            "platform_current": False, "newer_edition_id": cur["edition_id"], "supersedes_edition_id": None,
            "coexists_reason": COEXIST_REASSEMBLED,
            "storage": {"vps": None, "drive": None,
                        "upstream": {"available": False, "url": None,
                                     "note": "reassembled from the legacy page images; the platform serves "
                                             "another edition under this id"}},
            "legacy": {"sun_book_key": b["id"], "jc_short_key": short, "jks_slug": None,
                       "basis": "reassembled:legacy-images", "in_sun_search": cur["legacy"]["in_sun_search"],
                       "alias_candidate": None},
            "legacy_spec": {"mode": "reassembled", "legacy_pages": b["pages"], "sun_book_key": b["id"],
                            "jc_short_key": short, "subject": b["subject"]},
        })
        cur["supersedes_edition_id"] = rec["edition_id"]
        cur["coexists_reason"] = COEXIST_REASSEMBLED
        cur["legacy"].update({
            "sun_book_key": None, "jc_short_key": None, "basis": None, "in_sun_search": False,
            "alias_candidate": {"sun_book_key": b["id"], "jc_short_key": short,
                                "basis": "platform-current file of the reader book's SmartEdu id "
                                         "(identifier:jc-content-id); another edition than the legacy images"}})
        holdings[r["sha256"]] = {"path": str(store / f"{r['sha256']}.pdf"), "origin": "reassembled", "bytes": r["bytes"]}
        editions.append(rec)

    # --- files bound in xue's learn release ------------------------------------------------------
    learn = load_learn(inputs)
    cand_short = {i: k for k, v in read_json(state / "content-candidates.json", default={}).items() for i in v}

    def by_sha():
        return {e["pdf"]["sha256"]: e for e in editions if e.get("pdf")}

    def link_older(older: dict, sid: str) -> None:
        """Tie an older file to the held platform-current edition of the same source id, if that
        edition is not already paired."""
        cur = next((e for e in editions if e.get("pdf") and e["source"]["source_id"] == sid
                    and e["platform_current"] and e.get("holding") != "learn-only"), None)
        if cur is None:
            return
        older["work_id"] = cur["work_id"]
        older["coexists_reason"] = cur["coexists_reason"] = COEXIST_LEARN
        if cur.get("supersedes_edition_id") is None:
            cur["supersedes_edition_id"], older["newer_edition_id"] = older["edition_id"], cur["edition_id"]

    def learn_record(a: dict, fallback_subject: str | None) -> dict:
        rec = base_record(None, {"stage": a["stage"], "subject": fallback_subject or a["subject"], "title": a["title"],
                                 "edition_label": a["publisher"], "basis": "learn-baseline"})
        sid = a["source_id"]
        rec.update({
            "edition_id": f"cn-{sid}-{a['sha256'][:12]}",
            "platform_current": False, "newer_edition_id": None, "supersedes_edition_id": None,
            "status": "active",
            "legacy": {"sun_book_key": None, "jc_short_key": None, "jks_slug": None, "basis": None,
                       "in_sun_search": False, "alias_candidate": None},
        })
        return rec

    # (1) bound files fetched from xue as content candidates for reader books without a SmartEdu id
    for a in learn:
        st = acq.get(f"xue:{a['asset_id']}") or {}
        if st.get("state") != "verified" or a["sha256"] in by_sha():
            continue
        short = cand_short.get(a["source_id"])
        b = by_short.get(short) if short else None
        rec = learn_record(a, b["subject"] if b else None)
        rec.update({
            "pdf": pdf_block(st["sha256"], st["md5"], st["bytes"]),
            "source": source_block(a["source_id"], meta_of(a["source_id"]), current=False,
                                   retrieved_at=st["retrieved_at"], retrieved_via=st["retrieved_via"],
                                   sid_basis="identifier:learn-baseline", id_basis="learn-baseline-record"),
            "provenance": "learn-bound-file",
            "storage": {"vps": None, "drive": None,
                        "upstream": {"available": False, "url": None,
                                     "note": "the platform now serves a different file under this id"}},
        })
        rec["source"]["learn_acquired_at"] = a["acquired_at"]
        rec["source"]["md5_check"] = st.get("md5_check")
        holdings[st["sha256"]] = {"path": str(store / f"{st['sha256']}.pdf"), "origin": "acquired-from-xue",
                                  "bytes": st["bytes"]}
        if b:
            rec["content_claim"] = True
            rec["legacy"]["alias_candidate"] = {
                "sun_book_key": b["id"], "jc_short_key": short,
                "basis": "file bound in xue's learn release under the candidate source id; identity is decided "
                         "only by page-by-page comparison"}
            rec["legacy_spec"] = {"mode": "content", "legacy_pages": b["pages"], "sun_book_key": b["id"],
                                  "jc_short_key": short, "subject": b["subject"]}
        else:
            rec["legacy_spec"] = {"mode": "none", "relation": "none", "reason": "no reader book to compare with"}
        editions.append(rec)
        link_older(rec, a["source_id"])

    # (2) bound files whose bytes SUN does not hold: catalogued from the learn binding, not compared
    for a in learn:
        if a["sha256"] in by_sha() or not a["source_id"]:
            continue
        rec = learn_record(a, None)
        rec.update({
            "holding": "learn-only",
            "pdf": {"sha256": a["sha256"], "md5": a["r2_md5"] or None, "bytes": a["bytes"], "pages": a["pages"],
                    "has_text_layer": None, "producer": None, "text_pages": None, "ocr_needed_pages": None,
                    "blank_pages": None},
            "source": source_block(a["source_id"], meta_of(a["source_id"]), current=False,
                                   retrieved_at=a["acquired_at"], retrieved_via="learn-baseline-2026-07-15",
                                   sid_basis="identifier:learn-baseline", id_basis="learn-baseline-record"),
            "provenance": "learn-only",
            "storage": {"vps": None, "drive": None,
                        "upstream": {"available": False, "url": None,
                                     "note": "bytes are those bound in xue's learn release; not held by SUN"}},
            "legacy_spec": {"mode": "none", "relation": "none",
                            "reason": "bytes held only in learn-books; not downloaded and not compared"},
        })
        editions.append(rec)
        link_older(rec, a["source_id"])

    # (3) aliases by SHA-256 equality only, and the general learn locator
    learn_by_sha = {a["sha256"]: a for a in learn}
    for e in editions:
        e.setdefault("holding", "sun" if e.get("pdf") else None)
        a = learn_by_sha.get(e["pdf"]["sha256"]) if e.get("pdf") else None
        e["storage"]["learn"] = {"asset_id": a["asset_id"], "key": f"books/{a['asset_id']}/book.pdf"} if a else None
        e["legacy"]["learn_asset_id"] = a["asset_id"] if a else None
        e["legacy"]["learn_source_sha256"] = a["sha256"] if a else None

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
            "platform_current": False, "newer_edition_id": None, "supersedes_edition_id": None,
            "storage": {"vps": None, "drive": None, "learn": None,
                        "upstream": {"available": False, "url": None, "note": "SmartEdu id unknown"}},
            "legacy": {"sun_book_key": b["id"], "jc_short_key": b["shortKey"], "jks_slug": None,
                       "basis": "identifier:jc-book-key", "in_sun_search": b["id"] in sun_keys,
                       "alias_candidate": None, "learn_asset_id": None, "learn_source_sha256": None},
            "status": "pending-verification", "holding": None,
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
    comparisons = sorted(
        ({"jc_short_key": e["legacy_spec"]["jc_short_key"], "sha256": e["pdf"]["sha256"],
          "legacy_pages": e["legacy_spec"]["legacy_pages"],
          "known_pairing": e["legacy_spec"]["mode"] == "same-bytes"}
         for e in editions if e.get("pdf") and e.get("holding") == "sun" and e["legacy_spec"].get("jc_short_key")
         and e["legacy_spec"]["mode"] != "reassembled"),
        key=lambda c: (c["jc_short_key"], c["sha256"]))
    write_json(state / "plan.json", {
        "editions": editions, "comparisons": comparisons, "issues": issues, "newer_platform": newer_platform,
        "indeterminate_platform": indeterminate, "junior_resolved_by_bytes": byte_resolved,
        "junior_candidates": cands, "reader_books": len(jc), "sun_books": len(sun_keys),
    })
    write_json(state / "holdings.json", holdings)
    print(f"editions={len(editions)} held={sum(1 for e in editions if e.get('holding') == 'sun')} "
          f"learn_only={sum(1 for e in editions if e.get('holding') == 'learn-only')} "
          f"learn_aliases={sum(1 for e in editions if e['legacy'].get('learn_asset_id') and e.get('holding') == 'sun')} "
          f"holdings={len(holdings)} "
          f"newer_platform={len(newer_platform)} indeterminate={len(indeterminate)} "
          f"junior_resolved_by_bytes={len(byte_resolved)} issues={len(issues)}")
    return 1 if issues else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stage", choices=["ids", "job", "editions", "r3", "learn"])
    p.add_argument("--learn-root", default="/Users/ylsuen/CF/platforms/learn")
    p.add_argument("--inputs", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--downloads", required=True)
    p.add_argument("--details", required=True)
    p.add_argument("--local-root", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--max-details", type=int, default=160)
    args = p.parse_args(argv)
    return {"ids": stage_ids, "job": stage_job, "editions": stage_editions, "r3": stage_r3,
            "learn": stage_learn}[args.stage](args)


if __name__ == "__main__":
    sys.exit(main())
