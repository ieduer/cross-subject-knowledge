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

# Import shared config — add workspace scripts/ and platform/backend/ to path
_SCRIPT_DIR = Path(__file__).resolve().parent
_WORKSPACE_ROOT = _SCRIPT_DIR.parents[1]
for _p in [str(_WORKSPACE_ROOT / "scripts"), str(_SCRIPT_DIR.parent / "backend")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from textbook_config import (
    CANONICAL_SUBJECT_META as SUBJECT_META,
    EDITION_PATTERNS,
    normalize_subject,
    edition_ok,
)

REAL_CONTENT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27}$", re.IGNORECASE)
TEXTBOOK_VERSION_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "backend" / "textbook_version_manifest.json"


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


def _extract_embedded_edition(title: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", title or "")
    for edition, keywords in EDITION_PATTERNS:
        if any(keyword in normalized for keyword in keywords):
            return edition
    return ""


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


def _is_real_content_id(value: str | None) -> bool:
    return bool(REAL_CONTENT_ID_RE.fullmatch(str(value or "").strip()))


def _load_textbook_version_manifest() -> dict:
    if not TEXTBOOK_VERSION_MANIFEST_PATH.exists():
        return {"by_content_id": {}, "by_book_key": {}}
    try:
        payload = json.loads(TEXTBOOK_VERSION_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"by_content_id": {}, "by_book_key": {}}
    if isinstance(payload, dict) and ("by_content_id" in payload or "by_book_key" in payload):
        by_content_id = payload.get("by_content_id") if isinstance(payload.get("by_content_id"), dict) else {}
        by_book_key = payload.get("by_book_key") if isinstance(payload.get("by_book_key"), dict) else {}
        return {"by_content_id": by_content_id, "by_book_key": by_book_key}
    if isinstance(payload, dict):
        by_content_id = {k: v for k, v in payload.items() if isinstance(v, dict)}
        by_book_key = {}
        for item in by_content_id.values():
            book_key = str(item.get("book_key") or "").strip()
            if book_key:
                by_book_key[book_key] = item
        return {"by_content_id": by_content_id, "by_book_key": by_book_key}
    return {"by_content_id": {}, "by_book_key": {}}


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
            "page_sections": {},
            "book_map": {},
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
    book_map = {}
    version_manifest = _load_textbook_version_manifest()
    book_map_path = Path(__file__).resolve().parents[1] / "frontend" / "assets" / "pages" / "book_map.json"
    if book_map_path.exists():
        try:
            book_map = json.loads(book_map_path.read_text(encoding="utf-8"))
        except Exception:
            book_map = {}

    by_title_subject = defaultdict(list)
    by_title = defaultdict(list)
    page_lookup = {}
    page_sections: dict[str, set[int]] = defaultdict(set)

    for row in rows:
        book_key = str(row["book_key"] or "").strip()
        content_id = str(row["content_id"] or "").strip()
        book_info = book_map.get(book_key, {}) if book_key else {}
        manifest_row = {}
        if book_key:
            manifest_row = version_manifest["by_book_key"].get(book_key, {}) or {}
        if not manifest_row and _is_real_content_id(content_id):
            manifest_row = version_manifest["by_content_id"].get(content_id, {}) or {}
        base_title = str(manifest_row.get("title") or row["title"] or "").strip()
        display_title = str(manifest_row.get("display_title") or book_info.get("display_title") or book_info.get("title") or base_title).strip()
        edition = str(manifest_row.get("edition") or book_info.get("edition") or "").strip() or _extract_embedded_edition(display_title) or _extract_embedded_edition(base_title)
        item = {
            "content_id": content_id,
            "title": base_title,
            "display_title": display_title or base_title,
            "book_key": book_key,
            "subject": str(manifest_row.get("subject") or row["subject"] or "").strip(),
            "edition": edition,
        }
        title_key = _normalize_lookup_title(base_title)
        subject_name = str(item["subject"] or "").strip()
        if _is_real_content_id(content_id) and content_id not in by_content_id:
            by_content_id[content_id] = item
        if title_key and subject_name:
            by_title_subject[(title_key, subject_name)].append(item)
        if title_key:
            by_title[title_key].append(item)

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
        page_sections[book_key].add(section)

    return {
        "by_content_id": by_content_id,
        "by_title_subject": {k: tuple(v) for k, v in by_title_subject.items()},
        "by_title": {k: tuple(v) for k, v in by_title.items()},
        "page_lookup": page_lookup,
        "page_sections": {k: tuple(sorted(v)) for k, v in page_sections.items()},
        "book_map": book_map,
    }


def _build_text_probe(payload: list[dict], *, max_blocks: int = 600, max_chars: int = 60000) -> str:
    parts = []
    total = 0
    for item in payload:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = _normalize_text_line(item.get("text"))
        if len(text) < 2:
            continue
        parts.append(text)
        total += len(text)
        if len(parts) >= max_blocks or total >= max_chars:
            break
    return "\n".join(parts)


def _detect_edition_label(display_title: str, path: Path, text_probe: str) -> str:
    probe = "\n".join(
        part for part in (display_title, str(path), text_probe) if part
    )
    normalized = unicodedata.normalize("NFKC", probe)
    for edition, keywords in EDITION_PATTERNS:
        if any(keyword in normalized for keyword in keywords):
            return edition
    return ""


def _with_edition(base_title: str, edition: str) -> str:
    cleaned_title = str(base_title or "").strip()
    cleaned_edition = str(edition or "").strip()
    if not cleaned_title or not cleaned_edition:
        return cleaned_title
    if cleaned_edition in cleaned_title:
        return cleaned_title
    return f"{cleaned_title}（{cleaned_edition}）"


def _is_supported_runtime_edition(subject: str | None, edition: str | None, phase: str = "高中") -> bool:
    """Legacy wrapper — delegates to textbook_config.edition_ok()."""
    return edition_ok(phase, str(subject or "").strip(), str(edition or "").strip())


def _match_registry_candidate(candidates, edition_hint: str) -> dict | None:
    if not candidates:
        return None
    normalized_hint = str(edition_hint or "").strip()

    def candidate_matches(item: dict) -> bool:
        if not normalized_hint:
            return True
        return (
            normalized_hint == str(item.get("edition") or "").strip()
            or normalized_hint in str(item.get("display_title") or "")
            or normalized_hint in str(item.get("title") or "")
            or normalized_hint in str(item.get("book_key") or "")
        )

    if normalized_hint:
        matched = [item for item in candidates if candidate_matches(item)]
        if len(matched) == 1:
            return dict(matched[0])
        return None

    unique_book_keys = {str(item.get("book_key") or "").strip() for item in candidates if str(item.get("book_key") or "").strip()}
    if len(unique_book_keys) == 1 and candidates:
        return dict(candidates[0])
    if len(candidates) == 1:
        return dict(candidates[0])
    return None


def _make_supplemental_book_key(subject: str, base_title: str, edition: str, fallback: str) -> str:
    cleaned_subject = str(subject or "").strip()
    cleaned_title = str(base_title or "").strip()
    cleaned_edition = str(edition or "").strip()
    cleaned_fallback = str(fallback or "").strip()
    if cleaned_edition:
        basis = "|".join([cleaned_subject, cleaned_title, cleaned_edition])
    else:
        basis = "|".join([cleaned_subject, cleaned_title, cleaned_fallback])
    return f"suppbook:{hashlib.md5(basis.encode('utf-8')).hexdigest()[:16]}"


def _resolve_supplemental_book_meta(path: Path, registry: dict, payload: list[dict] | None = None) -> dict:
    raw_title = path.stem
    display_title = raw_title
    display_title = re.sub(r"_content_list$", "", display_title)
    display_title = re.sub(r"_智慧中小学_[0-9a-f\-]{36}$", "", display_title, flags=re.IGNORECASE)
    display_title = re.sub(r"^(高中|初中)_[^_]+_", "", display_title)
    display_title = display_title.replace("_", " ").strip()

    content_id = _parse_content_id_from_text(str(path))
    subject_name = _parse_subject_from_title(display_title) or _parse_subject_from_title(str(path))
    text_probe = _build_text_probe(payload or [])
    edition_hint = _detect_edition_label(display_title, path, text_probe)
    matched = registry["by_content_id"].get(content_id)
    if matched and edition_hint and not _match_registry_candidate((matched,), edition_hint):
        matched = None
    if not matched:
        title_key = _normalize_lookup_title(display_title)
        matched = _match_registry_candidate(
            registry["by_title_subject"].get((title_key, subject_name)) or registry["by_title"].get(title_key) or (),
            edition_hint,
        )

    if matched:
        matched_subject = matched.get("subject") or subject_name
        matched_edition = matched.get("edition") or edition_hint
        return {
            "content_id": matched.get("content_id") or content_id,
            "title": matched.get("display_title") or matched.get("title") or _with_edition(display_title, edition_hint),
            "base_title": matched.get("title") or display_title,
            "book_key": matched.get("book_key"),
            "subject": matched_subject,
            "edition": matched_edition,
            "phase": matched.get("phase") or "高中",
            "has_page_images": bool(matched.get("book_key")),
            "primary_bound": True,
            "supported": _is_supported_runtime_edition(matched_subject, matched_edition),
            "synthetic": False,
        }
    synthetic_key = _make_supplemental_book_key(
        subject_name,
        display_title,
        edition_hint,
        content_id or str(path.parent),
    )
    synthetic_has_page_images = bool((registry.get("book_map") or {}).get(synthetic_key))
    return {
        "content_id": content_id or None,
        "title": _with_edition(display_title, edition_hint),
        "base_title": display_title,
        "book_key": synthetic_key,
        "subject": subject_name,
        "edition": edition_hint,
        "phase": "高中",
        "has_page_images": synthetic_has_page_images,
        "primary_bound": False,
        "supported": _is_supported_runtime_edition(subject_name, edition_hint),
        "synthetic": True,
    }


def _page_text_quality(text: str) -> int:
    normalized = text or ""
    cjk = sum(1 for ch in normalized if "\u4e00" <= ch <= "\u9fff")
    letters = sum(1 for ch in normalized if ch.isalpha())
    digits = sum(1 for ch in normalized if ch.isdigit())
    noise = len(re.findall(r"(?:^|[\s/])[\-_=~]{2,}(?:$|[\s/])", normalized))
    return cjk * 6 + letters * 2 + digits + len(normalized) - noise * 20


def _pick_better_page(current: dict | None, candidate: dict) -> dict:
    if current is None:
        return candidate
    current_score = int(current.get("_quality_score") or 0)
    candidate_score = int(candidate.get("_quality_score") or 0)
    if candidate_score != current_score:
        return candidate if candidate_score > current_score else current
    current_len = len(current.get("text") or "")
    candidate_len = len(candidate.get("text") or "")
    if candidate_len != current_len:
        return candidate if candidate_len > current_len else current
    current_path = str(current.get("path") or "")
    candidate_path = str(candidate.get("path") or "")
    return candidate if candidate_path < current_path else current


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
    books_catalog: dict[str, dict] = {}
    page_entries_by_key: dict[tuple[str, int], dict] = {}
    edition_conflicts: list[dict] = []
    pages_written = 0
    chars_written = 0
    source_pages_total = 0
    source_chars_total = 0
    primary_bound_pages_omitted = 0
    unsupported_pages_omitted = 0
    primary_bound_page_lookup_misses = 0
    primary_bound_page_lookup_miss_samples: list[dict] = []
    indexed_files = 0

    output_gz.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output_gz.with_name(f"{output_gz.name}.tmp")
    tmp_manifest = manifest_path.with_name(f"{manifest_path.name}.tmp")

    for path in source_paths:
        source_subject = _parse_subject_from_title(str(path))
        if source_subject:
            source_subjects.add(source_subject)

        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            problems.append(f"invalid json: {path}: {exc}")
            continue

        if not isinstance(payload, list):
            problems.append(f"unexpected payload type: {path}")
            continue

        meta = _resolve_supplemental_book_meta(path, registry, payload)
        subject = str(meta.get("subject") or "").strip()
        title = str(meta.get("title") or "").strip()
        base_title = str(meta.get("base_title") or title).strip()
        if not subject:
            problems.append(f"missing subject: {path}")
            continue
        if not title:
            problems.append(f"missing title: {path}")
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

        source_page_count_for_file = 0
        book_key = str(meta.get("book_key") or "").strip()
        subject_stats = stats_by_subject.setdefault(
            subject,
            {
                "books": set(),
                "source_pages": 0,
                "source_chars": 0,
                "searchable_pages": 0,
                "searchable_chars": 0,
            },
        )
        catalog = books_catalog.setdefault(
            book_key,
            {
                "book_key": book_key,
                "phase": meta.get("phase") or "高中",
                "subject": subject,
                "title": title,
                "base_title": base_title,
                "edition": str(meta.get("edition") or "").strip(),
                "content_id": meta.get("content_id"),
                "has_page_images": bool(meta.get("has_page_images")),
                "primary_bound": bool(meta.get("primary_bound")),
                "supported": bool(meta.get("supported")),
                "source": "primary_bound" if meta.get("primary_bound") else "supplemental_only",
                "source_files": set(),
                "pages": 0,
                "search_pages": 0,
            },
        )
        existing_edition = str(catalog.get("edition") or "").strip()
        incoming_edition = str(meta.get("edition") or "").strip()
        if existing_edition and incoming_edition and existing_edition != incoming_edition:
            edition_conflicts.append(
                {
                    "book_key": book_key,
                    "subject": subject,
                    "title": title,
                    "existing_edition": existing_edition,
                    "incoming_edition": incoming_edition,
                    "path": str(path.relative_to(source_root.parent)),
                }
            )
            problems.append(
                f"edition conflict for {book_key}: {existing_edition} vs {incoming_edition} ({path.relative_to(source_root.parent)})"
            )
        elif not existing_edition and incoming_edition:
            catalog["edition"] = incoming_edition
        if meta.get("has_page_images"):
            catalog["has_page_images"] = True
        if meta.get("primary_bound"):
            catalog["primary_bound"] = True
            catalog["source"] = "primary_bound"
        if meta.get("supported"):
            catalog["supported"] = True
        catalog["source_files"].add(str(path.relative_to(source_root.parent)))

        for page_num in sorted(blocks_by_page):
            merged_text = _merge_supplemental_page_blocks(blocks_by_page[page_num])
            if len(merged_text) < 20:
                continue
            source_page_count_for_file += 1
            source_pages_total += 1
            source_chars_total += len(merged_text)
            subject_stats["source_pages"] += 1
            subject_stats["source_chars"] += len(merged_text)
            catalog["pages"] += 1

            if meta.get("primary_bound"):
                primary_bound_pages_omitted += 1
                if (book_key, int(page_num)) not in registry["page_lookup"]:
                    primary_bound_page_lookup_misses += 1
                    if len(primary_bound_page_lookup_miss_samples) < 20:
                        sections = registry["page_sections"].get(book_key) or ()
                        primary_bound_page_lookup_miss_samples.append(
                            {
                                "book_key": book_key,
                                "subject": subject,
                                "title": title,
                                "page_num": int(page_num),
                                "primary_min_section": sections[0] if sections else None,
                                "primary_max_section": sections[-1] if sections else None,
                                "path": str(path.relative_to(source_root.parent)),
                            }
                        )
                continue
            if not meta.get("supported"):
                unsupported_pages_omitted += 1
                continue

            entry = {
                "id": f"supp:{hashlib.md5(f'{book_key}:{page_num}'.encode('utf-8')).hexdigest()[:16]}",
                "content_id": meta.get("content_id"),
                "phase": meta.get("phase") or "高中",
                "subject": subject,
                "title": title,
                "base_title": base_title,
                "edition": str(meta.get("edition") or "").strip(),
                "book_key": book_key,
                "section": int(page_num),
                "logical_page": int(page_num),
                "text": merged_text,
                "path": str(path.relative_to(source_root.parent)),
                "has_page_images": bool(meta.get("has_page_images")),
                "primary_bound": bool(meta.get("primary_bound")),
                "supported": bool(meta.get("supported")),
                "synthetic": bool(meta.get("synthetic")),
                "_quality_score": _page_text_quality(merged_text),
            }
            page_entries_by_key[(book_key, int(page_num))] = _pick_better_page(page_entries_by_key.get((book_key, int(page_num))), entry)

        if source_page_count_for_file <= 0:
            problems.append(f"no merged pages: {path}")
            continue
        indexed_files += 1
        subject_stats["books"].add(book_key)

    selected_entries = sorted(
        page_entries_by_key.values(),
        key=lambda item: (
            item.get("subject") or "",
            item.get("title") or "",
            int(item.get("section") or 0),
            item.get("book_key") or "",
        ),
    )
    with gzip.open(tmp_output, "wt", encoding="utf-8", compresslevel=6) as out:
        for entry in selected_entries:
            payload = dict(entry)
            payload.pop("_quality_score", None)
            out.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            out.write("\n")
            pages_written += 1
            chars_written += len(payload.get("text") or "")
            subject_stats = stats_by_subject.setdefault(
                payload["subject"],
                {
                    "books": set(),
                    "source_pages": 0,
                    "source_chars": 0,
                    "searchable_pages": 0,
                    "searchable_chars": 0,
                },
            )
            subject_stats["books"].add(payload["book_key"])
            subject_stats["searchable_pages"] += 1
            subject_stats["searchable_chars"] += len(payload.get("text") or "")
            books_catalog[payload["book_key"]]["search_pages"] += 1

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
    duplicate_pages_collapsed = max(
        0,
        source_pages_total - primary_bound_pages_omitted - unsupported_pages_omitted - pages_written,
    )

    subject_manifest = {
        subject: {
            "books": len(stats["books"]),
            "source_pages": int(stats["source_pages"]),
            "source_chars": int(stats["source_chars"]),
            "pages": int(stats["searchable_pages"]),
            "chars": int(stats["searchable_chars"]),
        }
        for subject, stats in sorted(stats_by_subject.items())
    }
    content_id_missing_books = sum(1 for item in books_catalog.values() if not item.get("content_id"))
    identity_conflicts = []
    identity_buckets: dict[tuple[str, str, str], list[dict]] = {}
    for item in books_catalog.values():
        key = (
            item.get("subject") or "",
            _normalize_lookup_title(item.get("base_title") or item.get("title") or ""),
            str(item.get("edition") or "").strip(),
        )
        identity_buckets.setdefault(key, []).append(item)
    for (subject, base_title_key, edition), items in sorted(identity_buckets.items()):
        sources = {bool(item.get("has_page_images")) for item in items}
        if len(items) <= 1 or len(sources) <= 1:
            continue
        identity_conflicts.append(
            {
                "subject": subject,
                "base_title_key": base_title_key,
                "edition": edition,
                "books": [
                    {
                        "book_key": item.get("book_key"),
                        "title": item.get("title"),
                        "content_id": item.get("content_id"),
                        "source": item.get("source"),
                        "source_files": len(item.get("source_files") or []),
                        "pages": item.get("pages"),
                    }
                    for item in sorted(items, key=lambda row: (row.get("source") or "", row.get("book_key") or ""))
                ],
            }
        )
        problems.append(f"cross-source identity conflict: {subject}/{base_title_key}/{edition}")
    blank_title_groups = []
    blank_title_buckets: dict[tuple[str, str], list[dict]] = {}
    for item in books_catalog.values():
        edition = (item.get("edition") or "").strip()
        if edition:
            continue
        key = (item.get("subject") or "", item.get("base_title") or item.get("title") or "")
        blank_title_buckets.setdefault(key, []).append(item)
    for (subject, base_title), items in sorted(blank_title_buckets.items()):
        if len(items) <= 1:
            continue
        blank_title_groups.append(
            {
                "subject": subject,
                "base_title": base_title,
                "books": [
                    {
                        "book_key": item.get("book_key"),
                        "content_id": item.get("content_id"),
                        "source": item.get("source"),
                        "source_files": len(item.get("source_files") or []),
                        "pages": item.get("pages"),
                    }
                    for item in sorted(items, key=lambda row: (row.get("book_key") or ""))
                ],
            }
        )
    manifest = {
        "schema_version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "generator": "scripts/build_supplemental_textbook_index.py",
        "source_root": str(source_root),
        "source_files_total": len(source_paths),
        "source_files_indexed": indexed_files,
        "books": len(books_catalog),
        "pages": pages_written,
        "searchable_pages": pages_written,
        "chars": chars_written,
        "source_pages": source_pages_total,
        "source_chars": source_chars_total,
        "primary_books": sum(1 for item in books_catalog.values() if item.get("primary_bound")),
        "supplemental_only_books": sum(1 for item in books_catalog.values() if not item.get("primary_bound")),
        "primary_bound_pages_omitted": primary_bound_pages_omitted,
        "unsupported_pages_omitted": unsupported_pages_omitted,
        "supported_books": sum(1 for item in books_catalog.values() if item.get("supported")),
        "supported_searchable_books": sum(
            1 for item in books_catalog.values() if item.get("supported") and not item.get("primary_bound")
        ),
        "primary_bound_page_lookup_misses": primary_bound_page_lookup_misses,
        "primary_bound_page_lookup_miss_samples": primary_bound_page_lookup_miss_samples,
        "duplicate_pages_collapsed": duplicate_pages_collapsed,
        "content_id_missing_books": content_id_missing_books,
        "unresolved_pages": 0,
        "unresolved_books": 0,
        "edition_conflicts": len(edition_conflicts),
        "edition_conflict_samples": edition_conflicts[:20],
        "cross_source_identity_conflicts": len(identity_conflicts),
        "cross_source_identity_conflict_samples": identity_conflicts[:20],
        "blank_title_duplicate_groups": len(blank_title_groups),
        "blank_title_duplicate_samples": blank_title_groups[:20],
        "book_catalog": sorted(
            [
                {
                    "book_key": item["book_key"],
                    "phase": item.get("phase", "高中"),
                    "subject": item["subject"],
                    "title": item["title"],
                    "base_title": item["base_title"],
                    "edition": item["edition"],
                    "content_id": item["content_id"],
                    "has_page_images": item["has_page_images"],
                    "primary_bound": bool(item.get("primary_bound")),
                    "supported": bool(item.get("supported")),
                    "source": item["source"],
                    "source_files": len(item["source_files"]),
                    "pages": item["pages"],
                    "search_pages": item["search_pages"],
                }
                for item in books_catalog.values()
            ],
            key=lambda item: (item["subject"], item["title"], item["book_key"]),
        ),
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
                "books": len(books_catalog),
                "pages": pages_written,
                "source_pages": source_pages_total,
                "chars": chars_written,
                "subjects": {k: v["pages"] for k, v in subject_manifest.items()},
                "primary_books": sum(1 for item in books_catalog.values() if item.get("primary_bound")),
                "supplemental_only_books": sum(1 for item in books_catalog.values() if not item.get("primary_bound")),
                "primary_bound_pages_omitted": primary_bound_pages_omitted,
                "unsupported_pages_omitted": unsupported_pages_omitted,
                "duplicate_pages_collapsed": duplicate_pages_collapsed,
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
