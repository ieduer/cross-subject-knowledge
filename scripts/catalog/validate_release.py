#!/usr/bin/env python3
"""Validate a sun-catalog-v1 release directory: schema plus cross-file invariants.

Exit 0 when valid, 1 with one line per violation otherwise. Used by the tests and by the
deploy gate.

--holdings <dir>   directory of <sha256>.pdf (the files staged for the server). Every file is
                   hashed and opened: its SHA-256 must equal its name and the catalog's
                   pdf.sha256, its page count must equal pdf.pages, it must be referenced by
                   exactly one edition, and every edition with storage.vps must be present.
--holdings <json>  {sha256: {...}}: key comparison only (no hashing); for callers without the files.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CONTRACT, SHA256_RE, UUID_RE, file_hashes, jc_short_key, read_json  # noqa: E402

RELATIONS = {"same-bytes", "aligned", "partial", "different-edition", "none", "legacy-image-source"}
REASSEMBLED = "reassembled-from-legacy-images"
COVER_WIDTHS = ("w240", "w480")
TEXT_IDENTITY = {"same-bytes": ("same-file",),
                 "aligned": ("page-aligned-other-printing", "same-edition-image-proven"),
                 "partial": ("page-aligned-other-printing",), "different-edition": (None,), "none": (None,),
                 "legacy-image-source": ("legacy-images",)}
PROVEN_BASES = ("anchor", "text-anchor", "image-match", "image+text")
TEXT_BASES = ("anchor", "text-anchor", "image+text")
STATUSES = {"active", "superseded", "pending-verification"}
LEGACY_BASES = {"identifier:jc-content-id", "identifier:jc-book-key", "bytes:source-pdf",
                "content:per-page-comparison", "reassembled:legacy-images"}
SOURCE_ID_BASES = {"identifier:jc-content-id", "identifier:jks-slug", "bytes:sha256=learn-books", "bytes:md5=ti_md5",
                   "identifier:download", "identifier:learn-baseline"}
ID_BASES = {"md5-size-current", "downloaded-by-id:md5-equal", "downloaded-by-id:size-only",
            "baseline-join-unreproven", "platform-record", "learn-baseline-record",
            "reassembled-legacy-images"}
ASSET_RE = re.compile(r"^[0-9a-f]{16}$")


def is_held(e: dict) -> bool:
    """SUN holds the bytes (as opposed to a record known only from xue's learn binding)."""
    return e.get("pdf") is not None and e.get("holding") != "learn-only"
EDITION_KEYS = {"edition_id", "work_id", "region", "stage", "subject", "title", "volume", "publisher",
                "edition_label", "source", "pdf", "storage", "legacy", "corpus", "processing", "status",
                "platform_current", "newer_edition_id", "supersedes_edition_id", "metadata_basis",
                "preview_default", "holding", "cover"}
SOURCE_KEYS = {"provider", "source_id", "catalog_module", "online_time", "update_time", "ti_md5", "ti_size",
               "retrieved_at", "retrieved_via", "id_basis"}
PDF_KEYS = {"sha256", "bytes", "pages", "has_text_layer", "producer"}
ROW_KEYS = {"pdf_page", "printed_label", "printed_verified", "legacy_image_index", "ocr_page"}
DOWNLOAD_KEYS = {"source_id", "stage", "subject", "title", "edition_label", "publisher", "cover", "size_bytes",
                 "sources", "edition_id", "held_other_edition_ids", "upstream_status"}
DRIVE_KEYS = {"file_id", "resource_key", "view_url", "download_url", "shared"}
RELEASE_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
ALLOWED_HOSTS = ("jks-ai.bdfz.net", "img.rdfzer.com", "img.bdfz.net")
ALLOWED_SUFFIXES = (".smartedu.cn", ".ykt.cbern.com.cn")
DRIVE_HOSTS = ("drive.google.com", "drive.usercontent.google.com", "docs.google.com")
FORBIDDEN_WORDS = ("latest", "最新")
UPSTREAM_PDF = "https://jks-ai.bdfz.net/pdf/"


def host_ok(url, hosts=ALLOWED_HOSTS, suffixes=ALLOWED_SUFFIXES) -> bool:
    if not isinstance(url, str):
        return False
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname or u.username or u.port not in (None, 443):
        return False
    return u.hostname in hosts or any(u.hostname.endswith(s) for s in suffixes)


def edition_id_ok(e: dict) -> bool:
    eid, sid, pdf = e.get("edition_id") or "", (e.get("source") or {}).get("source_id"), e.get("pdf")
    if pdf:
        sha = pdf.get("sha256") or ""
        return eid == (f"cn-{sid}-{sha[:12]}" if sid else f"cn-x-{sha[:16]}")
    if sid:
        return eid == f"cn-{sid}-pending" and e.get("status") == "pending-verification"
    short = (e.get("legacy") or {}).get("jc_short_key")
    return bool(short) and eid == f"cn-x-legacy-{short}" and e.get("status") == "pending-verification"


def check_sums(release_dir: Path, E) -> None:
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


def check_storage(e: dict, E) -> None:
    eid, st, pdf = e["edition_id"], e["storage"] or {}, e["pdf"]
    sid = (e["source"] or {}).get("source_id")
    if set(st) != {"vps", "drive", "upstream", "learn"}:
        E(f"{eid}: storage must have vps / drive / upstream / learn")
        return
    learn, leg = st["learn"], e.get("legacy") or {}
    if learn is not None:
        if (pdf is None or not isinstance(learn, dict) or set(learn) != {"asset_id", "key"}
                or not ASSET_RE.match(str(learn.get("asset_id"))) or learn.get("key") != f"books/{learn.get('asset_id')}/book.pdf"):
            E(f"{eid}: storage.learn must be {{asset_id: 16 hex, key: books/<asset_id>/book.pdf}} on an edition with a PDF")
        elif leg.get("learn_asset_id") != learn["asset_id"] or leg.get("learn_source_sha256") != pdf.get("sha256"):
            E(f"{eid}: legacy.learn_asset_id / learn_source_sha256 must equal storage.learn and pdf.sha256 "
              "(the alias is by SHA-256 equality only)")
    elif leg.get("learn_asset_id") is not None or leg.get("learn_source_sha256") is not None:
        E(f"{eid}: learn alias without a storage.learn locator")
    if e.get("holding") == "learn-only" and (learn is None or st["vps"] is not None):
        E(f"{eid}: a learn-only edition must have storage.learn and no storage.vps")
    if e.get("holding") not in ("sun", "learn-only", None) or (e.get("holding") is None) != (pdf is None):
        E(f"{eid}: holding must be sun / learn-only for a record with a PDF and null otherwise")
    vps = st["vps"]
    if vps is not None:
        if pdf is None:
            E(f"{eid}: storage.vps set on an edition without a PDF")
        elif not isinstance(vps, dict) or set(vps) != {"path"} or vps["path"] != f"pdf/{pdf.get('sha256')}.pdf":
            E(f"{eid}: storage.vps.path must be exactly pdf/<pdf.sha256>.pdf")
    drive = st["drive"]
    if drive is not None:
        if pdf is None or not isinstance(drive, dict) or set(drive) != DRIVE_KEYS:
            E(f"{eid}: storage.drive must be null or {{{', '.join(sorted(DRIVE_KEYS))}}} on a held edition")
        elif not all(host_ok(drive[k], DRIVE_HOSTS, ()) for k in ("view_url", "download_url")):
            E(f"{eid}: storage.drive URL host not allowed")
    up = st["upstream"]
    if not isinstance(up, dict) or not {"available", "url"} <= set(up) or not isinstance(up.get("available"), bool):
        E(f"{eid}: storage.upstream must be {{available: bool, url}}")
        return
    if up["url"] is not None and (not host_ok(up["url"]) or up["url"] != UPSTREAM_PDF + str(sid)):
        E(f"{eid}: storage.upstream.url must be {UPSTREAM_PDF}<source_id> on an allowed host")
    if up["available"] and (up["url"] is None or e["platform_current"] is not True):
        E(f"{eid}: upstream marked available for a file that is not the platform's current file")


def check_cover(e: dict, release_dir: Path, E) -> None:
    eid, cover = e["edition_id"], e["cover"]
    if not is_held(e):
        if cover is not None:
            E(f"{eid}: cover on a record whose bytes are not held here")
        return
    if (not isinstance(cover, dict) or not isinstance(cover.get("sha256"), dict)
            or any(cover.get(w) != f"covers/{eid}-{w[1:]}.webp" for w in COVER_WIDTHS)):
        E(f"{eid}: a held edition needs cover.w240 / cover.w480 = covers/<edition_id>-<width>.webp with their SHA-256")
        return
    for w in COVER_WIDTHS:
        f = release_dir / cover[w]
        if not f.is_file():
            E(f"{eid}: cover file {cover[w]} is missing from the release")
        else:
            data = f.read_bytes()
            if hashlib.sha256(data).hexdigest() != cover["sha256"].get(w):
                E(f"{eid}: cover {cover[w]} does not match the SHA-256 in the record")
            elif data[:4] != b"RIFF" or data[8:12] != b"WEBP":
                E(f"{eid}: cover {cover[w]} is not a WebP file")


def check_unmapped(e: dict, pm: dict, E) -> None:
    """Every legacy index is either a mapped row or an unmapped entry with a nearest-neighbour hint."""
    eid, name = e["edition_id"], f"page-maps/{e['edition_id']}.json"
    legacy_pages = ((pm.get("legacy") or {}).get("evidence") or {}).get("legacy_pages")
    mapped = {r["legacy_image_index"]: r["pdf_page"] for r in pm.get("pages") or [] if r.get("legacy_image_index") is not None}
    um = pm.get("legacy_unmapped")
    owner = bool((e.get("legacy") or {}).get("sun_book_key"))
    if um is None:
        if owner or legacy_pages is not None:
            E(f"{name}: legacy_unmapped is missing although the legacy page count is known")
        return
    idx = [u.get("legacy_image_index") for u in um]
    if len(set(idx)) != len(idx) or set(idx) & set(mapped):
        E(f"{name}: a legacy index is listed twice or is both mapped and unmapped")
    if legacy_pages is not None and set(idx) | set(mapped) != set(range(legacy_pages)):
        E(f"{name}: mapped rows and legacy_unmapped together do not cover legacy indexes 0..{legacy_pages - 1}")
    if (e.get("legacy") or {}).get("unmapped_pages") != len(um):
        E(f"{eid}: legacy.unmapped_pages differs from the page map")
    known = sorted(mapped)
    for u in um:
        if "pdf_page" in u or "printed_label" in u:
            E(f"{name}: an unmapped legacy index carries a page of its own")
            break
        hint, li = u.get("nearest_mapped"), u.get("legacy_image_index")
        if not known:
            if hint is not None:
                E(f"{name}: nearest_mapped given although nothing is mapped")
                break
            continue
        best = min(known, key=lambda k: (abs(k - li), k))
        if (not isinstance(hint, dict) or hint.get("legacy_image_index") != best
                or hint.get("pdf_page") != mapped[best] or hint.get("distance") != abs(best - li)):
            E(f"{name}: nearest_mapped of legacy index {li} is not the nearest mapped row (ties: the earlier one)")
            break


def check_reassembled(e: dict, pm: dict, release_dir: Path, E) -> None:
    eid = e["edition_id"]
    is_re = e.get("provenance") == REASSEMBLED
    relation = (e.get("corpus") or {}).get("relation")
    if is_re != (relation == "legacy-image-source"):
        E(f"{eid}: relation legacy-image-source and provenance {REASSEMBLED} must go together")
        return
    if not is_re:
        if e.get("reassembly") is not None:
            E(f"{eid}: reassembly record on an edition that was not reassembled")
        return
    rows = pm.get("pages") or []
    if any(r.get("legacy_image_index") != r["pdf_page"] - 1 or r.get("legacy_basis") != "reassembled" for r in rows):
        E(f"{eid}: a reassembled edition must map every page to legacy index pdf_page - 1 with basis 'reassembled'")
    if e["pdf"].get("has_text_layer") is not False or e.get("platform_current") or e.get("preview_default"):
        E(f"{eid}: a reassembled edition has no text layer, is not the platform's file and is not the preview default")
    side_path = release_dir / str(e.get("reassembly"))
    if e.get("reassembly") != f"reassembly/{eid}.json" or not side_path.is_file():
        E(f"{eid}: reassembly/{eid}.json is missing")
        return
    side = read_json(side_path)
    src = side.get("page_sources") or []
    if (side.get("sha256") != e["pdf"].get("sha256") or side.get("pages") != len(rows) or len(src) != len(rows)
            or [x.get("legacy_image_index") for x in src] != list(range(len(rows)))
            or not all(SHA256_RE.match(str(x.get("source_sha256"))) for x in src)
            or side.get("jc_short_key") != (e.get("legacy") or {}).get("jc_short_key")):
        E(f"{eid}: the reassembly record does not list one verified source image per page of this PDF")


def check_page_map(e: dict, pm: dict, E) -> None:
    eid = e["edition_id"]
    name = f"page-maps/{eid}.json"
    rows = pm.get("pages") or []
    if pm.get("edition_id") != eid or pm.get("pdf_sha256") != e["pdf"].get("sha256"):
        E(f"{name}: edition id / PDF hash differ from the catalog")
    if len(rows) != e["pdf"].get("pages") or [r.get("pdf_page") for r in rows] != list(range(1, len(rows) + 1)):
        E(f"{name}: rows are not exactly pdf_page 1..{e['pdf'].get('pages')}")
        return
    if any(ROW_KEYS - set(r) for r in rows):
        E(f"{name}: a row is missing one of {sorted(ROW_KEYS)}")
        return
    printed, lev = pm.get("printed") or {}, ((pm.get("legacy") or {}).get("evidence") or {})
    verified_book = bool(printed.get("verified"))
    cat_printed = e.get("printed") or {}
    if bool(cat_printed.get("verified")) != verified_book:
        E(f"{eid}: printed.verified in the catalog differs from the page map")
    # --- printed labels: only in verified books, unique, and equal to pdf_page - c -------------
    segments = sorted(((s["start_pdf_page"], s["offset"]) for s in (printed.get("evidence") or {}).get("segments") or []))
    labels = [(r["pdf_page"], r["printed_label"]) for r in rows if r["printed_label"] is not None]
    if not verified_book and (labels or any(r["printed_verified"] for r in rows)):
        E(f"{name}: printed label in a book that failed folio verification")
    if any(r["printed_verified"] and r["printed_label"] is None for r in rows):
        E(f"{name}: a page is marked verified without a label")
    if verified_book:
        if not segments:
            E(f"{name}: verified book without a recorded folio mapping")
        elif cat_printed.get("offset") != segments[0][1]:
            E(f"{eid}: printed.offset in the catalog differs from the page map")
        for p, label in labels:
            seg = [s for s in segments if s[0] <= p]
            if not seg or label != str(p - seg[-1][1]):
                E(f"{name}: printed label {label!r} on pdf page {p} is not pdf_page - c")
                break
        if len(segments) == 1 and len({l for _, l in labels}) != len(labels):
            E(f"{name}: duplicate printed label")
    # --- legacy rows ---------------------------------------------------------------------------
    legacy = [(r["pdf_page"], r["legacy_image_index"], r.get("legacy_basis")) for r in rows
              if r["legacy_image_index"] is not None]
    idx = [li for _, li, _ in legacy]
    relation = (e.get("corpus") or {}).get("relation")
    leg = e.get("legacy") or {}
    accepted = bool(lev.get("accepted"))
    legacy_pages = lev.get("legacy_pages")
    if idx != sorted(set(idx)) or any(i < 0 for i in idx):
        E(f"{name}: legacy_image_index values are not unique, increasing and >= 0")
    if legacy_pages is not None and any(i >= legacy_pages for i in idx):
        E(f"{name}: legacy_image_index beyond the image set ({legacy_pages} pages)")
    if leg.get("mapped_pages") != len(legacy) or (pm.get("legacy") or {}).get("mapped_pages") != len(legacy):
        E(f"{eid}: legacy.mapped_pages differs from the rows in the page map")
    if relation != lev.get("relation"):
        E(f"{eid}: corpus.relation {relation!r} differs from the page-map evidence {lev.get('relation')!r}")
    if legacy:
        if relation not in ("same-bytes", "aligned", "partial", "legacy-image-source") or not accepted:
            E(f"{eid}: legacy_image_index filled while corpus.relation is {relation!r} (mapping accepted: {accepted})")
        if not (leg.get("sun_book_key") or leg.get("alias_candidate")):
            E(f"{eid}: legacy_image_index filled but the edition names no legacy book")
        offsets = {p - 1 - li for p, li, _ in legacy}
        if lev.get("method") != "content" and (len(offsets) != 1 or lev.get("offset") not in offsets):
            E(f"{eid}: legacy rows do not all equal pdf_page - 1 - offset (recorded offset {leg.get('offset')!r})")
        if leg.get("offset") != (next(iter(offsets)) if len(offsets) == 1 else None):
            E(f"{eid}: legacy.offset must be the single offset of the rows, or null when they differ")
        # an inferred row must lie between two individually proven rows that are close and agree
        max_gap = lev.get("max_gap")
        proven = sorted((p, li) for p, li, basis in legacy if basis in PROVEN_BASES)
        if max_gap is not None:
            for p, li, basis in legacy:
                if basis != "between-anchors":
                    continue
                before = max((a for a in proven if a[0] < p), default=None)
                after = min((a for a in proven if a[0] > p), default=None)
                if (before is None or after is None or after[0] - before[0] > max_gap
                        or after[0] - before[0] != after[1] - before[1] or li - before[1] != p - before[0]):
                    E(f"{name}: row {p} filled between anchors more than {max_gap} pages apart or of different offsets")
                    break
        if lev.get("method") == "content" and len(proven) < 8:
            E(f"{name}: fewer than 8 individually proven rows")
    elif relation in ("same-bytes", "aligned", "partial", "legacy-image-source"):
        E(f"{eid}: corpus.relation {relation!r} without any mapped row")
    if relation == "same-bytes" and (len(legacy) != len(rows) or any(li != p - 1 for p, li, _ in legacy)):
        E(f"{eid}: same-bytes requires legacy_image_index == pdf_page - 1 on every page")
    if relation == "aligned":
        if lev.get("method") == "content":
            if legacy_pages is None or len(legacy) != legacy_pages:
                E(f"{eid}: aligned requires every legacy page to be mapped")
        elif len(legacy) < 8 or not lev.get("reaches_end") or lev.get("wide_gaps"):
            E(f"{eid}: aligned requires an accepted mapping that reaches the end without a wide unproven stretch")
    if (e.get("corpus") or {}).get("text_identity") == "same-edition-image-proven" and not lev.get("same_rendering"):
        E(f"{eid}: same-edition-image-proven without the page-image evidence")
    # --- image-only PDF: nothing can have been read from it ------------------------------------
    if e["pdf"].get("has_text_layer") is False:
        if labels:
            E(f"{eid}: printed labels on a PDF without a text layer")
        if any(basis in TEXT_BASES for _, _, basis in legacy):
            E(f"{eid}: text-anchored legacy rows on a PDF without a text layer")


def check_holdings(holdings, by_id: dict, sha_refs: Counter, E) -> None:
    by_sha = {e["pdf"]["sha256"]: e for e in by_id.values() if e.get("pdf") and e["pdf"].get("sha256")}
    if isinstance(holdings, dict):
        stored = set(holdings)
    else:
        stored = set()
        for f in sorted(Path(holdings).glob("*.pdf")):
            sha = file_hashes(f, ("sha256",))["sha256"]
            if sha != f.stem:
                E(f"stored PDF {f.name}: content hashes to {sha[:16]}, not to its name")
                continue
            stored.add(sha)
            ed = by_sha.get(sha)
            if ed is not None:
                try:
                    import pymupdf
                    with pymupdf.open(str(f)) as doc:
                        pages = doc.page_count
                except Exception as exc:  # noqa: BLE001
                    E(f"stored PDF {sha[:16]}: cannot be opened ({type(exc).__name__})")
                    continue
                if pages != ed["pdf"].get("pages"):
                    E(f"stored PDF {sha[:16]}: {pages} pages, catalog says {ed['pdf'].get('pages')}")
                if f.stat().st_size != ed["pdf"].get("bytes"):
                    E(f"stored PDF {sha[:16]}: size differs from the catalog")
    for sha in sorted(stored):
        if sha_refs.get(sha, 0) != 1:
            E(f"stored PDF {sha[:16]} is referenced by {sha_refs.get(sha, 0)} editions")
    for sha in sorted(sha_refs):
        if sha not in stored:
            E(f"referenced PDF {sha[:16]} is not in the store")
    for e in by_id.values():
        if (e.get("storage") or {}).get("vps") and e.get("pdf") and e["pdf"]["sha256"] not in stored:
            E(f"{e['edition_id']}: storage.vps set but the file is not in the store")


def validate(release_dir: Path, holdings=None) -> list[str]:
    errs: list[str] = []
    E = errs.append
    for name in ("catalog.json", "downloads.json", "RELEASE.json", "SHA256SUMS"):
        if not (release_dir / name).is_file():
            E(f"missing file: {name}")
    if errs:
        return errs
    check_sums(release_dir, E)
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
    for fname in ("catalog.json", "downloads.json"):
        text = (release_dir / fname).read_text(encoding="utf-8").lower()
        for w in FORBIDDEN_WORDS:
            if w in text:
                E(f"{fname}: contains the word {w!r}; no edition may be called the latest")

    # --- editions ---------------------------------------------------------------------------
    editions = catalog.get("editions") or []
    by_id: dict[str, dict] = {}
    sha_refs: Counter = Counter()
    sun_keys: Counter = Counter()
    all_shas: Counter = Counter()
    active_by_work: dict[str, list[dict]] = defaultdict(list)
    complete: list[dict] = []
    for e in editions:
        eid = e.get("edition_id")
        missing = EDITION_KEYS - set(e)
        if missing:
            E(f"{eid}: missing fields {sorted(missing)}")
            continue
        if eid in by_id:
            E(f"{eid}: duplicate edition_id")
        by_id[eid] = e
        complete.append(e)
    for e in complete:
        eid = e["edition_id"]
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
        if (sid is None) != (src.get("id_basis") is None) or (sid is not None and src.get("id_basis") not in ID_BASES):
            E(f"{eid}: source.id_basis {src.get('id_basis')!r} does not say how the id was established")
        pdf = e["pdf"]
        # --- which file the record describes ---------------------------------------------------
        if not isinstance(e["platform_current"], bool):
            E(f"{eid}: platform_current must be a boolean")
        elif pdf is not None and e["platform_current"] != (src.get("platform_current") is True):
            E(f"{eid}: platform_current differs from source.platform_current")
        if e["platform_current"] is not True and e["metadata_basis"] == "smartedu-details":
            E(f"{eid}: title/publisher/edition taken from the platform's current record although this "
              "edition is not the platform's current file")
        if src.get("id_basis") == "md5-size-current" and e["platform_current"] is not True:
            E(f"{eid}: id_basis md5-size-current on a file the platform does not serve")
        newer, older = e["newer_edition_id"], e["supersedes_edition_id"]
        if newer is not None:
            n = by_id.get(newer)
            if (n is None or n["source"].get("source_id") != sid or n.get("platform_current") is not True
                    or n.get("supersedes_edition_id") != eid or e["platform_current"] or pdf is None):
                E(f"{eid}: newer_edition_id does not resolve to the platform-current record of the same source id")
        if older is not None:
            o = by_id.get(older)
            if (o is None or o.get("pdf") is None or o["source"].get("source_id") != sid
                    or o.get("newer_edition_id") != eid or e["platform_current"] is not True):
                E(f"{eid}: supersedes_edition_id does not resolve to a held edition of the same source id")
        if pdf is not None:
            if PDF_KEYS - set(pdf):
                E(f"{eid}: pdf missing {sorted(PDF_KEYS - set(pdf))}")
            elif not SHA256_RE.match(pdf["sha256"] or ""):
                E(f"{eid}: pdf.sha256 malformed")
            else:
                if is_held(e):
                    sha_refs[pdf["sha256"]] += 1
                all_shas[pdf["sha256"]] += 1
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
        elif "text_identity" not in cor or cor["text_identity"] not in TEXT_IDENTITY[cor["relation"]]:
            E(f"{eid}: corpus.text_identity {cor.get('text_identity')!r} does not fit relation {cor['relation']!r}")
        if not cor.get("basis"):
            E(f"{eid}: corpus.relation without a basis")
        if not is_held(e) and cor.get("relation") != "none":
            E(f"{eid}: corpus relation claimed without holding the PDF")
        check_storage(e, E)
        check_cover(e, release_dir, E)
        if e["status"] == "active":
            active_by_work[e["work_id"]].append(e)
    for sha, n in all_shas.items():
        if n != 1:
            E(f"pdf {sha[:16]}: referenced by {n} editions (must be exactly one)")
    for key, n in sun_keys.items():
        if n != 1:
            E(f"sun_book_key claimed by {n} editions: {key}")
    # --- exactly one default edition per work in the preview tab --------------------------------
    held_by_work: dict[str, list[dict]] = defaultdict(list)
    for e in complete:
        if not isinstance(e["preview_default"], bool):
            E(f"{e['edition_id']}: preview_default must be a boolean")
        elif e["pdf"] is None and e["preview_default"]:
            E(f"{e['edition_id']}: preview_default on a record without a PDF")
        if e.get("holding") == "learn-only" and e["preview_default"]:
            E(f"{e['edition_id']}: preview_default on a file SUN does not hold")
        if is_held(e):
            held_by_work[e["work_id"]].append(e)
    for work, group in held_by_work.items():
        n = sum(1 for g in group if g["preview_default"] is True)
        if n != 1:
            E(f"work {work}: {n} editions are preview_default (must be exactly one of "
              f"{', '.join(g['edition_id'] for g in group)})")
        elif len(group) > 1 and any(g["platform_current"] for g in group) and not \
                next(g for g in group if g["preview_default"])["platform_current"]:
            E(f"work {work}: the preview default is not the platform's current file")
    for work, group in active_by_work.items():
        if len(group) > 1 and not all(g.get("coexists_reason") for g in group):
            E(f"work {work}: {len(group)} active editions without an explicit coexists_reason "
              f"({', '.join(g['edition_id'] for g in group)})")
    if holdings is not None:
        check_holdings(holdings, by_id, sha_refs, E)

    # --- reader_books index -----------------------------------------------------------------
    rb = catalog.get("reader_books")
    if not isinstance(rb, list):
        E("catalog: reader_books missing")
        rb = []
    rb_keys = Counter(r.get("sun_book_key") for r in rb)
    for r in rb:
        key = r.get("sun_book_key")
        if rb_keys[key] != 1:
            E(f"reader_books: key listed {rb_keys[key]} times: {key}")
        if not key or r.get("jc_short_key") != jc_short_key(key or ""):
            E(f"reader_books: jc_short_key != md5(sun_book_key)[:12] for {key}")
        ed = by_id.get(r.get("edition_id"))
        if ed is None or (ed.get("legacy") or {}).get("sun_book_key") != key:
            E(f"reader_books: edition_id of {r.get('jc_short_key')} is not the edition holding that key")
        for cid in r.get("candidate_edition_ids") or []:
            c = by_id.get(cid)
            if c is None or ((c.get("legacy") or {}).get("alias_candidate") or {}).get("sun_book_key") != key:
                E(f"reader_books: candidate {cid} of {r.get('jc_short_key')} is not an alias candidate for that book")
    if set(rb_keys) != set(sun_keys):
        E("reader_books: keys differ from the sun_book_key values of the editions")

    # --- page maps --------------------------------------------------------------------------
    pm_dir = release_dir / "page-maps"
    pm_files = {p.stem: p for p in pm_dir.glob("*.json")} if pm_dir.is_dir() else {}
    page_maps = {}
    for eid in pm_files:
        if eid not in by_id:
            E(f"page-maps/{eid}.json: no such edition in the catalog")
        elif by_id[eid].get("pdf") is None:
            E(f"page-maps/{eid}.json: edition has no PDF")
    for e in complete:
        eid = e["edition_id"]
        if not is_held(e):
            if eid in pm_files and e.get("pdf") is not None:
                E(f"page-maps/{eid}.json: page map claimed for bytes SUN does not hold")
            continue
        if eid not in pm_files:
            E(f"{eid}: edition with a PDF has no page map")
            continue
        page_maps[eid] = read_json(pm_files[eid])
        check_page_map(e, page_maps[eid], E)
        check_unmapped(e, page_maps[eid], E)
        check_reassembled(e, page_maps[eid], release_dir, E)

    # --- downloads --------------------------------------------------------------------------
    ids: Counter = Counter()
    for r in downloads.get("rows") or []:
        sid = r.get("source_id")
        if DOWNLOAD_KEYS - set(r):
            E(f"downloads: row {sid} missing {sorted(DOWNLOAD_KEYS - set(r))}")
            continue
        ids[sid] += 1
        eid = r["edition_id"]
        if eid is not None:
            ed = by_id.get(eid)
            if ed is None or not is_held(ed):
                E(f"downloads: {sid} points at {eid}, which is not a held edition")
            elif (ed.get("source") or {}).get("source_id") != sid:
                E(f"downloads: {sid} points at an edition of another source id")
            elif ed.get("platform_current") is not True:
                E(f"downloads: {sid} edition_id names a held file that is not the platform's current file")
        for oid in r["held_other_edition_ids"]:
            o = by_id.get(oid)
            if (o is None or not is_held(o) or (o.get("source") or {}).get("source_id") != sid
                    or o.get("platform_current") is True):
                E(f"downloads: {sid} held_other_edition_ids entry {oid} is not an older held edition of that id")
        if r["cover"] is not None and not host_ok(r["cover"]):
            E(f"downloads: {sid} cover URL host not allowed")
        for s in r["sources"]:
            kind, seid = s.get("kind"), s.get("edition_id")
            sed = by_id.get(seid) if seid is not None else None
            if seid is not None and (sed is None or (sed.get("source") or {}).get("source_id") != sid):
                E(f"downloads: {sid} source names edition {seid}, which is not an edition of that id")
                continue
            if kind == "upstream":
                if not host_ok(s.get("url")) or s.get("url") != UPSTREAM_PDF + str(sid):
                    E(f"downloads: {sid} upstream URL not allowed")
                if r["upstream_status"] == "OFFLINE":
                    E(f"downloads: {sid} offers an upstream source although the platform record is OFFLINE")
                if sed is not None and sed.get("platform_current") is not True:
                    E(f"downloads: {sid} upstream source is attributed to an edition that is not the "
                      "platform's current file")
                if s.get("platform_current") is not True:
                    E(f"downloads: {sid} upstream source must be marked platform_current")
            elif kind == "vps":
                if sed is None or (sed.get("storage") or {}).get("vps") != {"path": s.get("path")}:
                    E(f"downloads: {sid} vps source does not match a staged edition")
                elif s.get("platform_current") is not sed.get("platform_current"):
                    E(f"downloads: {sid} vps source mislabels whether it is the platform's current file")
            else:
                E(f"downloads: {sid} unknown source kind {kind!r}")
    for sid, n in ids.items():
        if n != 1:
            E(f"downloads: source_id {sid} listed {n} times")

    # --- RELEASE.json counts: recomputed in full --------------------------------------------
    if not errs:
        from build_catalog import counts
        expect = counts(editions, page_maps, downloads,
                        {"newer_platform": release.get("newer_platform"),
                         "indeterminate_platform": release.get("indeterminate_platform")})
        got = release.get("counts") or {}
        for k in sorted(set(expect) | set(got)):
            if expect.get(k) != got.get(k):
                E(f"RELEASE: counts.{k} differs from the value recomputed from the release files")
    else:
        c = release.get("counts") or {}
        if c.get("editions") != len(editions) or (c.get("downloads") or {}).get("rows") != len(downloads.get("rows") or []):
            E("RELEASE: edition / download counts differ from the release files")
    return errs


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("release_dir")
    p.add_argument("--holdings", help="directory of <sha256>.pdf (hashed and opened) or a JSON key list")
    args = p.parse_args(argv)
    holdings = None
    if args.holdings:
        hp = Path(args.holdings)
        holdings = hp if hp.is_dir() else read_json(hp)
    errs = validate(Path(args.release_dir), holdings)
    for e in errs:
        print("INVALID " + e)
    print(f"{'OK' if not errs else 'FAILED'} violations={len(errs)} release={Path(args.release_dir).name}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
