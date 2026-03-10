#!/usr/bin/env python3
import argparse
import gzip
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


SUBJECT_META = {
    "语文": {"icon": "📖", "color": "#e74c3c"},
    "数学": {"icon": "📐", "color": "#3498db"},
    "英语": {"icon": "🌍", "color": "#2ecc71"},
    "物理": {"icon": "⚛️", "color": "#9b59b6"},
    "化学": {"icon": "🧪", "color": "#e67e22"},
    "生物学": {"icon": "🧬", "color": "#1abc9c"},
    "历史": {"icon": "📜", "color": "#f39c12"},
    "地理": {"icon": "🗺️", "color": "#16a085"},
    "思想政治": {"icon": "⚖️", "color": "#c0392b"},
}


def _clean_query_text(query: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff\s]", "", (query or "")).strip()


def _compact_query_text(query: str) -> str:
    return re.sub(r"\s+", "", _clean_query_text(query))


def _normalize_text_line(text: str | None) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("\u3000", " ").replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _normalize_lookup_title(title: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", title or "")
    normalized = normalized.replace("_content_list", "")
    normalized = normalized.replace("（", "(").replace("）", ")")
    normalized = normalized.replace("·", "")
    normalized = re.sub(r"_智慧中小学_[0-9a-f\-]{36}$", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.replace("_智慧中小学", "")
    normalized = normalized.replace("_", "")
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.strip()


def _parse_subject_from_title(title: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", title or "")
    for subject_name in SUBJECT_META:
        if subject_name in normalized:
            return subject_name
    if "习近平新时代中国特色社会主义思想学生读本" in normalized:
        return "思想政治"
    return ""


def _parse_content_id_from_text(text: str | None) -> str:
    match = re.search(r"([0-9a-f]{8}-[0-9a-f\-]{27})", text or "", re.IGNORECASE)
    return match.group(1) if match else ""


def _merge_supplemental_page_blocks(blocks: list[str]) -> str:
    merged = []
    seen = set()
    for raw in blocks:
        text = _normalize_text_line(raw)
        if len(text) < 2:
            continue
        key = text.casefold()
        if merged and (text == merged[-1] or text in merged[-1]):
            continue
        if key in seen and len(text) < 32:
            continue
        seen.add(key)
        merged.append(text)
    return "\n".join(merged)


def _iter_source_paths(source_root: Path):
    for path in sorted(source_root.rglob("*_content_list.json")):
        lowered = str(path).lower()
        if "test_" in lowered or "/test" in lowered:
            continue
        yield path


def _load_textbook_registry(db_path: Path) -> dict:
    if not db_path.exists():
        return {
            "by_content_id": {},
            "by_title_subject": {},
            "by_title": {},
            "page_lookup": {},
        }

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT DISTINCT content_id, title, book_key, subject
            FROM chunks
            WHERE source = 'mineru' OR source IS NULL
            """
        ).fetchall()
        page_rows = con.execute(
            """
            SELECT DISTINCT book_key, section, logical_page
            FROM chunks
            WHERE (source = 'mineru' OR source IS NULL)
              AND book_key IS NOT NULL
            """
        ).fetchall()
    finally:
        con.close()

    by_content_id = {}
    by_title_subject = {}
    by_title = {}
    page_lookup = {}

    for row in rows:
        item = {
            "content_id": row["content_id"],
            "title": row["title"],
            "book_key": row["book_key"],
            "subject": row["subject"],
        }
        content_id = str(row["content_id"] or "").strip()
        title_key = _normalize_lookup_title(row["title"])
        subject_name = str(row["subject"] or "").strip()
        if content_id and content_id not in by_content_id:
            by_content_id[content_id] = item
        if title_key and subject_name and (title_key, subject_name) not in by_title_subject:
            by_title_subject[(title_key, subject_name)] = item
        if title_key and title_key not in by_title:
            by_title[title_key] = item

    for row in page_rows:
        book_key = str(row["book_key"] or "").strip()
        if not book_key:
            continue
        try:
            section = int(row["section"])
        except Exception:
            continue
        logical_page = row["logical_page"]
        if logical_page is None:
            logical_page = section
        page_lookup[(book_key, section)] = int(logical_page)

    return {
        "by_content_id": by_content_id,
        "by_title_subject": by_title_subject,
        "by_title": by_title,
        "page_lookup": page_lookup,
    }


def _resolve_supplemental_book_meta(path: Path, registry: dict) -> dict:
    raw_title = path.stem
    display_title = raw_title
    display_title = re.sub(r"_content_list$", "", display_title)
    display_title = re.sub(r"_智慧中小学_[0-9a-f\-]{36}$", "", display_title, flags=re.IGNORECASE)
    display_title = re.sub(r"^高中_[^_]+_", "", display_title)
    display_title = display_title.replace("_", " ").strip()

    content_id = _parse_content_id_from_text(str(path))
    subject_name = _parse_subject_from_title(display_title) or _parse_subject_from_title(str(path))
    matched = registry["by_content_id"].get(content_id)
    if not matched:
        title_key = _normalize_lookup_title(display_title)
        matched = registry["by_title_subject"].get((title_key, subject_name)) or registry["by_title"].get(title_key)

    if matched:
        return {
            "content_id": matched.get("content_id") or content_id,
            "title": matched.get("title") or display_title,
            "book_key": matched.get("book_key"),
            "subject": matched.get("subject") or subject_name,
        }
    return {
        "content_id": content_id or None,
        "title": display_title,
        "book_key": None,
        "subject": subject_name,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_index(source_root: Path, db_path: Path, output_gz: Path, manifest_path: Path, *, allow_partial: bool) -> int:
    registry = _load_textbook_registry(db_path)
    source_paths = list(_iter_source_paths(source_root))
    source_subjects = set()
    stats_by_subject: dict[str, dict[str, int | set[str]]] = {}
    problems: list[str] = []
    books_indexed = set()
    pages_written = 0
    chars_written = 0
    indexed_files = 0

    output_gz.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output_gz.with_name(f"{output_gz.name}.tmp")
    tmp_manifest = manifest_path.with_name(f"{manifest_path.name}.tmp")

    with gzip.open(tmp_output, "wt", encoding="utf-8", compresslevel=6) as out:
        for path in source_paths:
            source_subject = _parse_subject_from_title(str(path))
            if source_subject:
                source_subjects.add(source_subject)

            meta = _resolve_supplemental_book_meta(path, registry)
            subject = str(meta.get("subject") or "").strip()
            title = str(meta.get("title") or "").strip()
            if not subject:
                problems.append(f"missing subject: {path}")
                continue
            if not title:
                problems.append(f"missing title: {path}")
                continue

            try:
                with path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception as exc:
                problems.append(f"invalid json: {path}: {exc}")
                continue

            if not isinstance(payload, list):
                problems.append(f"unexpected payload type: {path}")
                continue

            blocks_by_page = defaultdict(list)
            for item in payload:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                try:
                    page_idx = int(item.get("page_idx"))
                except Exception:
                    continue
                if page_idx < 0:
                    continue
                text = _normalize_text_line(item.get("text"))
                if len(text) < 2:
                    continue
                blocks_by_page[page_idx].append(text)

            if not blocks_by_page:
                problems.append(f"no text pages: {path}")
                continue

            page_count_for_file = 0
            book_identity = str(meta.get("content_id") or meta.get("book_key") or path)
            subject_stats = stats_by_subject.setdefault(subject, {"books": set(), "pages": 0, "chars": 0})

            for page_num in sorted(blocks_by_page):
                merged_text = _merge_supplemental_page_blocks(blocks_by_page[page_num])
                if len(merged_text) < 20:
                    continue
                book_key = meta.get("book_key")
                logical_page = registry["page_lookup"].get((book_key, page_num)) if book_key else None
                entry = {
                    "id": f"supp:{hashlib.md5(f'{path}:{page_num}'.encode('utf-8')).hexdigest()[:16]}",
                    "content_id": meta.get("content_id"),
                    "subject": subject,
                    "title": title,
                    "book_key": book_key,
                    "section": int(page_num),
                    "logical_page": int(logical_page) if logical_page is not None else int(page_num),
                    "text": merged_text,
                    "path": str(path.relative_to(source_root.parent)),
                }
                out.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
                out.write("\n")
                pages_written += 1
                chars_written += len(merged_text)
                subject_stats["pages"] += 1
                subject_stats["chars"] += len(merged_text)
                page_count_for_file += 1

            if page_count_for_file <= 0:
                problems.append(f"no merged pages: {path}")
                continue
            indexed_files += 1
            books_indexed.add(book_identity)
            subject_stats["books"].add(book_identity)

    built_subjects = set(stats_by_subject)
    missing_subjects = sorted(source_subjects - built_subjects)
    if missing_subjects:
        problems.append(f"missing subjects in output: {', '.join(missing_subjects)}")
    if indexed_files != len(source_paths):
        problems.append(f"indexed files mismatch: expected {len(source_paths)}, got {indexed_files}")

    if problems and not allow_partial:
        tmp_output.unlink(missing_ok=True)
        tmp_manifest.unlink(missing_ok=True)
        sys.stderr.write("supplemental index build failed:\n")
        for item in problems[:50]:
            sys.stderr.write(f" - {item}\n")
        if len(problems) > 50:
            sys.stderr.write(f" - ... and {len(problems) - 50} more\n")
        return 1

    tmp_output.replace(output_gz)
    output_sha256 = _sha256_file(output_gz)
    output_bytes = output_gz.stat().st_size

    subject_manifest = {
        subject: {
            "books": len(stats["books"]),
            "pages": int(stats["pages"]),
            "chars": int(stats["chars"]),
        }
        for subject, stats in sorted(stats_by_subject.items())
    }
    manifest = {
        "schema_version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "generator": "scripts/build_supplemental_textbook_index.py",
        "source_root": str(source_root),
        "source_files_total": len(source_paths),
        "source_files_indexed": indexed_files,
        "books": len(books_indexed),
        "pages": pages_written,
        "chars": chars_written,
        "subjects": subject_manifest,
        "db_path": str(db_path),
        "output": {
            "path": str(output_gz),
            "bytes": output_bytes,
            "sha256": output_sha256,
        },
        "problems": problems,
        "allow_partial": allow_partial,
    }
    with tmp_manifest.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp_manifest.replace(manifest_path)

    print(
        json.dumps(
            {
                "status": "ok",
                "source_files_total": len(source_paths),
                "source_files_indexed": indexed_files,
                "books": len(books_indexed),
                "pages": pages_written,
                "chars": chars_written,
                "subjects": {k: v["pages"] for k, v in subject_manifest.items()},
                "output_bytes": output_bytes,
                "output_sha256": output_sha256,
                "problems": len(problems),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    workspace_root = repo_root.parent

    parser = argparse.ArgumentParser(description="Build compact page-level supplemental textbook index.")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=workspace_root / "data" / "mineru_output_backup",
        help="Root directory containing backup *_content_list.json files.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=workspace_root / "data" / "index" / "textbook_mineru_fts.db",
        help="SQLite DB used for content_id/book_key/logical_page resolution.",
    )
    parser.add_argument(
        "--output-gz",
        type=Path,
        default=repo_root / "backend" / "supplemental_textbook_pages.jsonl.gz",
        help="Output gzip JSONL path.",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=repo_root / "backend" / "supplemental_textbook_pages.manifest.json",
        help="Output manifest JSON path.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Write output even when some source files fail validation.",
    )
    args = parser.parse_args()

    return build_index(
        args.source_root.expanduser().resolve(),
        args.db_path.expanduser().resolve(),
        args.output_gz.expanduser().resolve(),
        args.manifest_path.expanduser().resolve(),
        allow_partial=args.allow_partial,
    )


if __name__ == "__main__":
    raise SystemExit(main())
