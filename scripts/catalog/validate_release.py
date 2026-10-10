#!/usr/bin/env python3
"""Validate a sun-catalog-v1 release directory: schema plus cross-file invariants.

Exit 0 when valid, 1 with one line per violation otherwise. Used by the tests and by the
deploy gate. With --holdings (JSON {sha256: {...}} of the PDFs actually stored) it also checks
that every stored PDF is referenced by exactly one edition and every referenced PDF is stored.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CONTRACT, SHA256_RE, UUID_RE, jc_short_key, read_json  # noqa: E402

RELATIONS = {"same-bytes", "aligned", "partial", "different-edition", "none"}
STATUSES = {"active", "superseded", "pending-verification"}
LEGACY_BASES = {"identifier:jc-content-id", "identifier:jc-book-key", "bytes:source-pdf"}
SOURCE_ID_BASES = {"identifier:jc-content-id", "identifier:jks-slug", "bytes:sha256=learn-books", "bytes:md5=ti_md5"}
EDITION_KEYS = {"edition_id", "work_id", "region", "stage", "subject", "title", "volume", "publisher",
                "edition_label", "source", "pdf", "storage", "legacy", "corpus", "processing", "status"}
SOURCE_KEYS = {"provider", "source_id", "catalog_module", "online_time", "update_time", "ti_md5", "ti_size",
               "retrieved_at", "retrieved_via"}
PDF_KEYS = {"sha256", "bytes", "pages", "has_text_layer", "producer"}
ROW_KEYS = {"pdf_page", "printed_label", "printed_verified", "legacy_image_index", "ocr_page"}
DOWNLOAD_KEYS = {"source_id", "stage", "subject", "title", "edition_label", "publisher", "cover", "size_bytes",
                 "sources", "edition_id"}
RELEASE_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


def edition_id_ok(e: dict) -> bool:
    eid, sid, pdf = e.get("edition_id") or "", (e.get("source") or {}).get("source_id"), e.get("pdf")
    if pdf:
        sha = pdf.get("sha256") or ""
        return eid == (f"cn-{sid}-{sha[:12]}" if sid else f"cn-x-{sha[:16]}")
    if sid:
        return eid == f"cn-{sid}-pending" and e.get("status") == "pending-verification"
    short = (e.get("legacy") or {}).get("jc_short_key")
    return bool(short) and eid == f"cn-x-legacy-{short}" and e.get("status") == "pending-verification"


def validate(release_dir: Path, holdings: dict | None = None) -> list[str]:
    errs: list[str] = []
    E = errs.append
    for name in ("catalog.json", "downloads.json", "RELEASE.json", "SHA256SUMS"):
        if not (release_dir / name).is_file():
            E(f"missing file: {name}")
    if errs:
        return errs

    # --- SHA256SUMS covers every file and verifies -----------------------------------------
    listed = {}
    for line in (release_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([0-9a-f]{64})  (.+)$", line)
        if not m:
            E(f"SHA256SUMS: bad line: {line[:60]}")
            continue
        listed[m.group(2)] = m.group(1)
    actual = sorted(str(p.relative_to(release_dir)) for p in release_dir.rglob("*")
                    if p.is_file() and p.name != "SHA256SUMS")
    for name in actual:
        if name not in listed:
            E(f"SHA256SUMS: file not listed: {name}")
        elif hashlib.sha256((release_dir / name).read_bytes()).hexdigest() != listed[name]:
            E(f"SHA256SUMS: hash mismatch: {name}")
    for name in listed:
        if name not in actual:
            E(f"SHA256SUMS: listed file missing: {name}")

    try:
        catalog = read_json(release_dir / "catalog.json")
        downloads = read_json(release_dir / "downloads.json")
        release = read_json(release_dir / "RELEASE.json")
    except ValueError as exc:
        return errs + [f"invalid JSON: {exc}"]

    rid = release.get("release_id")
    if not RELEASE_ID_RE.match(rid or ""):
        E(f"RELEASE: bad release_id {rid!r}")
    if release_dir.name != rid:
        E(f"RELEASE: directory name {release_dir.name!r} != release_id {rid!r}")
    for doc, label in ((catalog, "catalog"), (downloads, "downloads")):
        if doc.get("release_id") != rid:
            E(f"{label}: release_id differs from RELEASE.json")
    if catalog.get("contract") != CONTRACT or release.get("contract") != CONTRACT:
        E("contract is not sun-catalog-v1")

    # --- editions ---------------------------------------------------------------------------
    editions = catalog.get("editions") or []
    by_id: dict[str, dict] = {}
    sha_refs: Counter = Counter()
    sun_keys: Counter = Counter()
    active_by_work: dict[str, list[dict]] = defaultdict(list)
    for e in editions:
        eid = e.get("edition_id")
        missing = EDITION_KEYS - set(e)
        if missing:
            E(f"{eid}: missing fields {sorted(missing)}")
            continue
        if eid in by_id:
            E(f"{eid}: duplicate edition_id")
        by_id[eid] = e
        if not edition_id_ok(e):
            E(f"{eid}: edition_id does not follow the identity rule for its source id / content hash")
        if e["status"] not in STATUSES:
            E(f"{eid}: bad status {e['status']!r}")
        if e["region"] != "CN":
            E(f"{eid}: region must be CN in this release")
        src = e["source"] or {}
        if SOURCE_KEYS - set(src):
            E(f"{eid}: source missing {sorted(SOURCE_KEYS - set(src))}")
        sid = src.get("source_id")
        if sid is not None and not UUID_RE.match(sid):
            E(f"{eid}: source_id is not a SmartEdu uuid")
        if sid is not None and src.get("source_id_basis") not in SOURCE_ID_BASES:
            E(f"{eid}: source id attached without an identifier or byte basis "
              f"({src.get('source_id_basis')!r}) — editions must not be merged by title")
        pdf = e["pdf"]
        if pdf is not None:
            if PDF_KEYS - set(pdf):
                E(f"{eid}: pdf missing {sorted(PDF_KEYS - set(pdf))}")
            elif not SHA256_RE.match(pdf["sha256"] or ""):
                E(f"{eid}: pdf.sha256 malformed")
            else:
                sha_refs[pdf["sha256"]] += 1
                if not isinstance(pdf["pages"], int) or pdf["pages"] < 1 or not isinstance(pdf["bytes"], int):
                    E(f"{eid}: pdf.pages / pdf.bytes invalid")
        elif e["status"] == "active":
            E(f"{eid}: active edition without a PDF")
        leg = e["legacy"] or {}
        key = leg.get("sun_book_key")
        if key is not None:
            sun_keys[key] += 1
            if leg.get("jc_short_key") != jc_short_key(key):
                E(f"{eid}: jc_short_key != md5(sun_book_key)[:12]")
            if leg.get("basis") not in LEGACY_BASES:
                E(f"{eid}: legacy alias without an identifier or byte basis ({leg.get('basis')!r}) "
                  "— editions must not be merged by title")
            if leg.get("basis") == "bytes:source-pdf" and pdf is None:
                E(f"{eid}: byte-based legacy alias without a PDF")
        cor = e["corpus"] or {}
        if cor.get("relation") not in RELATIONS:
            E(f"{eid}: bad corpus.relation {cor.get('relation')!r}")
        if not cor.get("basis"):
            E(f"{eid}: corpus.relation without a basis")
        if pdf is None and cor.get("relation") != "none":
            E(f"{eid}: corpus relation claimed without a PDF")
        st = e["storage"] or {}
        if set(st) != {"vps", "drive", "upstream"}:
            E(f"{eid}: storage must have vps / drive / upstream")
        if e["status"] == "active":
            active_by_work[e["work_id"]].append(e)
    for sha, n in sha_refs.items():
        if n != 1:
            E(f"pdf {sha[:16]}: referenced by {n} editions (must be exactly one)")
    for key, n in sun_keys.items():
        if n != 1:
            E(f"sun_book_key claimed by {n} editions: {key}")
    for work, group in active_by_work.items():
        if len(group) > 1 and not all(g.get("coexists_reason") for g in group):
            E(f"work {work}: {len(group)} active editions without an explicit coexists_reason "
              f"({', '.join(g['edition_id'] for g in group)})")
    if holdings is not None:
        for sha in holdings:
            if sha_refs.get(sha, 0) != 1:
                E(f"stored PDF {sha[:16]} is referenced by {sha_refs.get(sha, 0)} editions")
        for sha in sha_refs:
            if sha not in holdings:
                E(f"referenced PDF {sha[:16]} is not in the store")

    # --- page maps --------------------------------------------------------------------------
    pm_dir = release_dir / "page-maps"
    pm_files = {p.stem: p for p in pm_dir.glob("*.json")} if pm_dir.is_dir() else {}
    for eid in pm_files:
        if eid not in by_id:
            E(f"page-maps/{eid}.json: no such edition in the catalog")
        elif by_id[eid].get("pdf") is None:
            E(f"page-maps/{eid}.json: edition has no PDF")
    for eid, e in by_id.items():
        if e.get("pdf") is None or EDITION_KEYS - set(e):
            continue
        if eid not in pm_files:
            E(f"{eid}: edition with a PDF has no page map")
            continue
        pm = read_json(pm_files[eid])
        rows = pm.get("pages") or []
        if pm.get("edition_id") != eid or pm.get("pdf_sha256") != e["pdf"].get("sha256"):
            E(f"page-maps/{eid}.json: edition id / PDF hash differ from the catalog")
        if len(rows) != e["pdf"].get("pages") or [r.get("pdf_page") for r in rows] != list(range(1, len(rows) + 1)):
            E(f"page-maps/{eid}.json: rows are not exactly pdf_page 1..{e['pdf'].get('pages')}")
            continue
        verified_book = bool((pm.get("printed") or {}).get("verified"))
        seen_legacy = []
        for r in rows:
            if ROW_KEYS - set(r):
                E(f"page-maps/{eid}.json: row {r.get('pdf_page')} missing {sorted(ROW_KEYS - set(r))}")
                break
            if not verified_book and (r["printed_label"] is not None or r["printed_verified"]):
                E(f"page-maps/{eid}.json: printed label on page {r['pdf_page']} of a book that failed folio verification")
                break
            if r["printed_verified"] and r["printed_label"] is None:
                E(f"page-maps/{eid}.json: page {r['pdf_page']} verified without a label")
                break
            if r["legacy_image_index"] is not None:
                seen_legacy.append((r["pdf_page"], r["legacy_image_index"]))
        idx = [li for _, li in seen_legacy]
        if idx != sorted(set(idx)) or any(i < 0 for i in idx):
            E(f"page-maps/{eid}.json: legacy_image_index values are not unique, increasing and >= 0")
        relation = (e.get("corpus") or {}).get("relation")
        leg = e.get("legacy") or {}
        accepted = bool(((pm.get("legacy") or {}).get("evidence") or {}).get("accepted"))
        if seen_legacy:
            if relation not in ("same-bytes", "aligned", "partial") or not accepted:
                E(f"{eid}: legacy_image_index filled while corpus.relation is {relation!r} "
                  f"(mapping accepted: {accepted})")
            if not (leg.get("sun_book_key") or leg.get("alias_candidate")):
                E(f"{eid}: legacy_image_index filled but the edition names no legacy book")
        if relation == "same-bytes" and any(li != p - 1 for p, li in seen_legacy) or \
                relation == "same-bytes" and len(seen_legacy) != len(rows):
            E(f"{eid}: same-bytes requires legacy_image_index == pdf_page - 1 on every page")
        if relation == "aligned" and len(seen_legacy) < 8:
            E(f"{eid}: aligned requires an accepted mapping")

    # --- downloads --------------------------------------------------------------------------
    ids: Counter = Counter()
    for r in downloads.get("rows") or []:
        if DOWNLOAD_KEYS - set(r):
            E(f"downloads: row {r.get('source_id')} missing {sorted(DOWNLOAD_KEYS - set(r))}")
            continue
        ids[r["source_id"]] += 1
        eid = r["edition_id"]
        if eid is not None:
            ed = by_id.get(eid)
            if ed is None or ed.get("pdf") is None:
                E(f"downloads: {r['source_id']} points at {eid}, which is not a held edition")
            elif (ed.get("source") or {}).get("source_id") != r["source_id"]:
                E(f"downloads: {r['source_id']} points at an edition of another source id")
    for sid, n in ids.items():
        if n != 1:
            E(f"downloads: source_id {sid} listed {n} times")
    c = (release.get("counts") or {})
    if c.get("editions") != len(editions) or c.get("editions_with_pdf") != sum(sha_refs.values()):
        E("RELEASE: edition counts differ from catalog.json")
    if (c.get("downloads") or {}).get("rows") != len(downloads.get("rows") or []):
        E("RELEASE: download row count differs from downloads.json")
    return errs


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("release_dir")
    p.add_argument("--holdings")
    args = p.parse_args(argv)
    holdings = read_json(args.holdings) if args.holdings else None
    errs = validate(Path(args.release_dir), holdings)
    for e in errs:
        print("INVALID " + e)
    print(f"{'OK' if not errs else 'FAILED'} violations={len(errs)} release={Path(args.release_dir).name}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
