#!/usr/bin/env python3
import argparse
import gzip
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_primary_books(db_path: Path) -> dict[str, dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT DISTINCT book_key, title, subject, content_id
            FROM chunks
            WHERE source = 'mineru' OR source IS NULL
            ORDER BY subject, title, book_key
            """
        ).fetchall()
        page_stats = con.execute(
            """
            SELECT book_key, MAX(section) AS max_section, COUNT(DISTINCT section) AS pages
            FROM chunks
            WHERE source = 'mineru' OR source IS NULL
            GROUP BY book_key
            """
        ).fetchall()
    finally:
        con.close()

    by_key = {
        str(row["book_key"] or "").strip(): {
            "title": str(row["title"] or "").strip(),
            "subject": str(row["subject"] or "").strip(),
            "content_id": str(row["content_id"] or "").strip(),
        }
        for row in rows
        if str(row["book_key"] or "").strip()
    }
    for row in page_stats:
        book_key = str(row["book_key"] or "").strip()
        if book_key not in by_key:
            continue
        by_key[book_key]["max_section"] = int(row["max_section"] or 0)
        by_key[book_key]["pages"] = int(row["pages"] or 0)
    return by_key


def _compute_primary_textbook_fingerprint(db_path: Path, text_limit: int = 160) -> tuple[int, str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            """
            SELECT id, substr(text, 1, ?)
            FROM chunks
            WHERE source != 'gaokao' AND text IS NOT NULL AND text != ''
            ORDER BY id
            """,
            (text_limit,),
        ).fetchall()
    finally:
        con.close()
    digest = hashlib.sha256()
    for chunk_id, text in rows:
        payload = json.dumps([int(chunk_id), text or ""], ensure_ascii=False, separators=(",", ":"))
        digest.update(payload.encode("utf-8"))
        digest.update(b"\n")
    return len(rows), digest.hexdigest()


def verify_runtime_data(
    *,
    db_path: Path,
    book_map_path: Path,
    version_manifest_path: Path,
    supplemental_manifest_path: Path,
    supplemental_index_gz_path: Path,
) -> tuple[bool, dict]:
    primary_books = _load_primary_books(db_path)
    primary_rows, primary_fingerprint = _compute_primary_textbook_fingerprint(db_path)
    book_map = _load_json(book_map_path)
    version_manifest = _load_json(version_manifest_path)
    supplemental_manifest = _load_json(supplemental_manifest_path)

    issues: list[str] = []

    missing_book_map_keys = sorted(set(primary_books) - set(book_map))
    if missing_book_map_keys:
        issues.append(f"primary books missing page-image map: {len(missing_book_map_keys)}")
    supplemental_page_map_keys = sorted(set(book_map) - set(primary_books))

    if int(version_manifest.get("primary_books") or 0) != len(primary_books):
        issues.append("version manifest primary_books mismatch")
    if int(version_manifest.get("resolved_primary_books") or 0) != len(primary_books):
        issues.append("version manifest resolved_primary_books mismatch")
    if int(version_manifest.get("unresolved_primary_books") or 0) != 0:
        issues.append("version manifest unresolved_primary_books != 0")
    if int(version_manifest.get("duplicate_primary_identity_groups") or 0) != 0:
        issues.append("version manifest duplicate_primary_identity_groups != 0")
    if len(version_manifest.get("safe_merge_candidates") or []) != 0:
        issues.append("version manifest safe_merge_candidates != 0")

    if int(supplemental_manifest.get("edition_conflicts") or 0) != 0:
        issues.append("supplemental manifest edition_conflicts != 0")
    if int(supplemental_manifest.get("cross_source_identity_conflicts") or 0) != 0:
        issues.append("supplemental manifest cross_source_identity_conflicts != 0")
    if int(supplemental_manifest.get("blank_title_duplicate_groups") or 0) != 0:
        issues.append("supplemental manifest blank_title_duplicate_groups != 0")

    catalog = supplemental_manifest.get("book_catalog") or []
    primary_bound_books = [item for item in catalog if item.get("primary_bound")]
    supplemental_only_books = [item for item in catalog if not item.get("primary_bound")]
    supported_searchable_books = [item for item in supplemental_only_books if item.get("supported", True)]
    if len(catalog) != int(supplemental_manifest.get("books") or 0):
        issues.append("supplemental manifest books count mismatch")
    if len(primary_bound_books) != int(supplemental_manifest.get("primary_books") or 0):
        issues.append("supplemental manifest primary_books count mismatch")
    if len(supplemental_only_books) != int(supplemental_manifest.get("supplemental_only_books") or 0):
        issues.append("supplemental manifest supplemental_only_books count mismatch")

    primary_bound_missing_map = [
        item["book_key"] for item in primary_bound_books if str(item.get("book_key") or "").strip() not in book_map
    ]
    if primary_bound_missing_map:
        issues.append(f"primary-bound supplemental books missing book_map entries: {len(primary_bound_missing_map)}")

    supplemental_rows = 0
    supplemental_books_seen: set[str] = set()
    active_rows_with_page_images = 0
    active_rows_primary_bound = 0
    active_rows_supported_false = 0
    active_rows_in_book_map = 0
    bad_sections = 0
    subject_counts: Counter[str] = Counter()
    with gzip.open(supplemental_index_gz_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            entry = json.loads(line)
            supplemental_rows += 1
            book_key = str(entry.get("book_key") or "").strip()
            if book_key:
                supplemental_books_seen.add(book_key)
            if entry.get("has_page_images"):
                active_rows_with_page_images += 1
            if entry.get("primary_bound"):
                active_rows_primary_bound += 1
            if not entry.get("supported", True):
                active_rows_supported_false += 1
            if book_key in book_map:
                active_rows_in_book_map += 1
            section = entry.get("section")
            if section is None or int(section) < 0:
                bad_sections += 1
            subject_counts[str(entry.get("subject") or "").strip()] += 1

    if supplemental_rows != int(supplemental_manifest.get("pages") or 0):
        issues.append(f"supplemental row count mismatch: jsonl={supplemental_rows} manifest={supplemental_manifest.get('pages')}")
    if active_rows_primary_bound != 0:
        issues.append(f"searchable supplemental rows still marked primary_bound: {active_rows_primary_bound}")
    if active_rows_supported_false != 0:
        issues.append(f"searchable supplemental rows still marked unsupported: {active_rows_supported_false}")
    if bad_sections != 0:
        issues.append(f"supplemental rows with negative sections: {bad_sections}")
    if len(supplemental_books_seen) != len(supported_searchable_books):
        issues.append(
            f"supplemental searchable book count mismatch: rows={len(supplemental_books_seen)} catalog={len(supported_searchable_books)}"
        )

    summary = {
        "status": "ok" if not issues else "failed",
        "primary_db_books": len(primary_books),
        "book_map_books": len(book_map),
        "book_map_primary_books": len(primary_books) - len(missing_book_map_keys),
        "book_map_supplemental_books": len(supplemental_page_map_keys),
        "version_manifest": {
            "primary_books": int(version_manifest.get("primary_books") or 0),
            "resolved_primary_books": int(version_manifest.get("resolved_primary_books") or 0),
            "unresolved_primary_books": int(version_manifest.get("unresolved_primary_books") or 0),
            "duplicate_primary_identity_groups": int(version_manifest.get("duplicate_primary_identity_groups") or 0),
            "safe_merge_candidates": len(version_manifest.get("safe_merge_candidates") or []),
            "split_required_groups": len(version_manifest.get("split_required_groups") or []),
        },
        "supplemental_manifest": {
            "books": int(supplemental_manifest.get("books") or 0),
            "primary_books": int(supplemental_manifest.get("primary_books") or 0),
            "supplemental_only_books": int(supplemental_manifest.get("supplemental_only_books") or 0),
            "supported_books": int(supplemental_manifest.get("supported_books") or 0),
            "supported_searchable_books": int(supplemental_manifest.get("supported_searchable_books") or 0),
            "source_pages": int(supplemental_manifest.get("source_pages") or 0),
            "pages": int(supplemental_manifest.get("pages") or 0),
            "primary_bound_pages_omitted": int(supplemental_manifest.get("primary_bound_pages_omitted") or 0),
            "unsupported_pages_omitted": int(supplemental_manifest.get("unsupported_pages_omitted") or 0),
            "primary_bound_page_lookup_misses": int(supplemental_manifest.get("primary_bound_page_lookup_misses") or 0),
            "duplicate_pages_collapsed": max(
                0,
                int(supplemental_manifest.get("source_pages") or 0)
                - int(supplemental_manifest.get("primary_bound_pages_omitted") or 0)
                - int(supplemental_manifest.get("pages") or 0),
            ),
        },
        "supplemental_jsonl": {
            "rows": supplemental_rows,
            "books": len(supplemental_books_seen),
            "subject_rows": dict(sorted(subject_counts.items())),
            "has_page_images_rows": active_rows_with_page_images,
            "primary_bound_rows": active_rows_primary_bound,
            "unsupported_rows": active_rows_supported_false,
            "book_map_key_rows": active_rows_in_book_map,
        },
        "artifacts": {
            "db": {
                "path": str(db_path),
                "bytes": db_path.stat().st_size,
                "sha256": _sha256_file(db_path),
                "primary_textbook_rows": primary_rows,
                "primary_textbook_fingerprint_sha256": primary_fingerprint,
            },
            "book_map": {"path": str(book_map_path), "bytes": book_map_path.stat().st_size, "sha256": _sha256_file(book_map_path)},
            "version_manifest": {
                "path": str(version_manifest_path),
                "bytes": version_manifest_path.stat().st_size,
                "sha256": _sha256_file(version_manifest_path),
            },
            "supplemental_manifest": {
                "path": str(supplemental_manifest_path),
                "bytes": supplemental_manifest_path.stat().st_size,
                "sha256": _sha256_file(supplemental_manifest_path),
            },
            "supplemental_jsonl": {
                "path": str(supplemental_index_gz_path),
                "bytes": supplemental_index_gz_path.stat().st_size,
                "sha256": _sha256_file(supplemental_index_gz_path),
            },
        },
        "issues": issues,
    }
    return (not issues), summary


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    workspace_root = repo_root.parent

    parser = argparse.ArgumentParser(description="Verify textbook runtime data integrity before deployment.")
    parser.add_argument("--db", type=Path, default=workspace_root / "data" / "index" / "textbook_mineru_fts.db")
    parser.add_argument("--book-map", type=Path, default=repo_root / "frontend" / "assets" / "pages" / "book_map.json")
    parser.add_argument("--version-manifest", type=Path, default=repo_root / "backend" / "textbook_version_manifest.json")
    parser.add_argument("--supplemental-manifest", type=Path, default=repo_root / "backend" / "supplemental_textbook_pages.manifest.json")
    parser.add_argument("--supplemental-index", type=Path, default=repo_root / "backend" / "supplemental_textbook_pages.jsonl.gz")
    args = parser.parse_args()

    ok, summary = verify_runtime_data(
        db_path=args.db,
        book_map_path=args.book_map,
        version_manifest_path=args.version_manifest,
        supplemental_manifest_path=args.supplemental_manifest,
        supplemental_index_gz_path=args.supplemental_index,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
