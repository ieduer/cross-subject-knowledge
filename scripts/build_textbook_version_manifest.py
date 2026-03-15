#!/usr/bin/env python3
import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


REAL_CONTENT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27}$", re.IGNORECASE)

EDITION_RULES = (
    ("A版", (r"（A版）", r"\(A版\)", r"人民教育出版社.?北京.?A版", r"人民教育出版社A版")),
    ("B版", (r"（B版）", r"\(B版\)", r"中学数学教材实验研究组", r"数学.?B版")),
    ("北师大版", (r"北京师范大学出版社", r"北京师范大学出版社高中数学编辑室", r"王尚志", r"保继光", r"主编王蔷")),
    ("冀教版", (r"冀教版", r"河北教育出版社")),
    ("外研社版", (r"外语教学与研究出版社", r"外研社", r"Foreign Language Teaching and Research Press", r"陈琳")),
    ("上外教版", (r"上海外语教育出版社", r"上海市中小学（幼儿园）课程改革委员会组织编写", r"束定芳", r"上海外国语大学")),
    ("重大版", (r"重庆大学出版社", r"杨晓钰")),
    ("沪教版", (r"上海教育出版社", r"上海教育出版社有限公司", r"牛津大学出版社", r"华东师范大学", r"上海市中小学（幼儿园）课程改革委员会组织编写")),
    ("沪科版", (r"上海科学技术出版社", r"上海科技教育出版社", r"上海世纪出版", r"麻生明", r"陈寅", r"束炳如", r"何润伟")),
    ("苏教版", (r"苏教版", r"江苏凤凰教育出版社", r"江苏凤凰出版传媒", r"葛军", r"李善良", r"王祖浩")),
    ("鄂教版", (r"湖北教育出版社", r"武汉中远印务有限公司", r"彭双阶", r"胡典顺")),
    ("湘教版", (r"湖南教育出版社", r"湖南出版中心", r"张景中", r"黄步高", r"邹楚林", r"邹伟华")),
    ("鲁科版", (r"鲁科版", r"山东科学技术出版社", r"总主编王磊陈光巨", r"陈光巨")),
    ("人教版", (r"人民.{0,1}育出版社", r"人民教.?出版社", r"课程教材研究所", r"人教版")),
    ("中图版", (r"中国地图出版社",)),
    ("人民出版社版", (r"人民出版社",)),
)


def _normalize_text(text: str | None) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("\u3000", " ").replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _normalize_lookup_title(title: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", title or "")
    normalized = normalized.replace("（", "(").replace("）", ")")
    normalized = re.sub(r"（[^）]*版）", "", normalized)
    normalized = re.sub(r"\([^)]*版\)", "", normalized)
    normalized = normalized.replace("·", "")
    normalized = normalized.replace("_content_list", "")
    normalized = re.sub(r"_智慧中小学_[0-9a-f\-]{36}$", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.replace("_智慧中小学", "")
    normalized = normalized.replace("_", "")
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.strip()


def _is_real_content_id(value: str | None) -> bool:
    return bool(REAL_CONTENT_ID_RE.fullmatch(str(value or "").strip()))


def _with_edition(base_title: str, edition: str) -> str:
    cleaned_title = str(base_title or "").strip()
    cleaned_edition = str(edition or "").strip()
    if not cleaned_title or not cleaned_edition or cleaned_edition in cleaned_title:
        return cleaned_title
    return f"{cleaned_title}（{cleaned_edition}）"


def _detect_edition(*parts: str) -> tuple[str, str]:
    probe = "\n".join(part for part in parts if part)
    normalized = unicodedata.normalize("NFKC", probe)
    for edition, patterns in EDITION_RULES:
        for pattern in patterns:
            if re.search(pattern, normalized, re.IGNORECASE):
                return edition, pattern
    return "", ""


def _load_book_map(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_supplemental_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_primary_books(db_path: Path) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT DISTINCT book_key, title, subject, content_id, phase
            FROM chunks
            WHERE source = 'mineru' OR source IS NULL
            ORDER BY subject, title, book_key
            """
        ).fetchall()
        page_rows = con.execute(
            """
            SELECT book_key, COUNT(DISTINCT section) AS pages
            FROM chunks
            WHERE source = 'mineru' OR source IS NULL
            GROUP BY book_key
            """
        ).fetchall()
        probe_rows = con.execute(
            """
            SELECT book_key, section, text
            FROM chunks
            WHERE (source = 'mineru' OR source IS NULL)
              AND section <= 3
            ORDER BY book_key, section
            """
        ).fetchall()
    finally:
        con.close()

    page_counts = {str(row["book_key"]): int(row["pages"] or 0) for row in page_rows}
    text_by_book: dict[str, list[str]] = defaultdict(list)
    for row in probe_rows:
        text = _normalize_text(row["text"])
        if text:
            text_by_book[str(row["book_key"])].append(text)

    # Deduplicate and validate: each book_key must have exactly one phase
    books_by_key: dict[str, dict] = {}
    for row in rows:
        book_key = str(row["book_key"] or "").strip()
        phase = str(row["phase"] or "高中").strip()
        if book_key in books_by_key:
            existing_phase = books_by_key[book_key]["phase"]
            if existing_phase != phase:
                raise RuntimeError(
                    f"FATAL: book_key={book_key} has mixed phases: {existing_phase}, {phase}. "
                    "Each book_key must belong to exactly one phase. Fix the data."
                )
            continue  # skip duplicate rows for same book_key
        probe = "\n".join(text_by_book.get(book_key, []))
        books_by_key[book_key] = {
            "book_key": book_key,
            "phase": phase,
            "title": str(row["title"] or "").strip(),
            "subject": str(row["subject"] or "").strip(),
            "content_id": str(row["content_id"] or "").strip(),
            "pages": page_counts.get(book_key, 0),
            "probe": probe,
        }
    return list(books_by_key.values())


def _build_primary_manifest(primary_books: list[dict], book_map: dict) -> tuple[dict, dict]:
    by_book_key = {}
    by_content_id = {}
    duplicates: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    unresolved = []

    for book in primary_books:
        book_key = book["book_key"]
        book_info = book_map.get(book_key, {})
        info_title = str(book_info.get("display_title") or book_info.get("title") or book["title"] or "").strip()
        edition, evidence = _detect_edition(info_title, book_key, book["probe"])
        if not edition:
            unresolved.append(
                {
                    "book_key": book_key,
                    "subject": book["subject"],
                    "title": book["title"],
                }
            )
        title = book["title"]
        display_title = _with_edition(title, edition) if edition else title
        record = {
            "phase": book.get("phase") or "高中",
            "subject": book["subject"],
            "title": title,
            "edition": edition,
            "display_title": display_title,
            "book_key": book_key,
            "content_id": book["content_id"] if _is_real_content_id(book["content_id"]) else None,
            "pages": int(book.get("pages") or 0),
            "evidence": evidence,
        }
        by_book_key[book_key] = record
        if record["content_id"]:
            by_content_id[record["content_id"]] = record
        duplicates[(record["subject"], _normalize_lookup_title(title), record["edition"])].append(record)

    duplicate_groups = {
        key: value
        for key, value in duplicates.items()
        if key[2] and len(value) > 1
    }
    return (
        {
            "by_book_key": by_book_key,
            "by_content_id": by_content_id,
        },
        {
            "unresolved": unresolved,
            "duplicate_groups": duplicate_groups,
            "edition_counts": Counter(record["edition"] or "未识别" for record in by_book_key.values()),
        },
    )


def _build_reconciliation(primary_manifest: dict, supplemental_manifest: dict) -> dict:
    primary_catalog = list(primary_manifest["by_book_key"].values())
    primary_by_identity: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    primary_by_base: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in primary_catalog:
        base_key = (item["subject"], _normalize_lookup_title(item["title"]))
        primary_by_base[base_key].append(item)
        primary_by_identity[(item["subject"], _normalize_lookup_title(item["title"]), item["edition"])].append(item)

    safe_matches = []
    split_required = []
    seen_split_keys = set()

    for item in supplemental_manifest.get("book_catalog") or []:
        if item.get("has_page_images"):
            continue
        base_title = str(item.get("base_title") or item.get("title") or "").strip()
        edition = str(item.get("edition") or "").strip()
        base_key = (str(item.get("subject") or "").strip(), _normalize_lookup_title(base_title))
        identity_key = (base_key[0], base_key[1], edition)
        identity_matches = primary_by_identity.get(identity_key, [])
        if edition and len(identity_matches) == 1:
            target = identity_matches[0]
            safe_matches.append(
                {
                    "subject": base_key[0],
                    "base_title": base_title,
                    "edition": edition,
                    "supplemental_book_key": str(item.get("book_key") or "").strip(),
                    "supplemental_title": str(item.get("title") or "").strip(),
                    "supplemental_content_id": item.get("content_id"),
                    "supplemental_pages": int(item.get("pages") or 0),
                    "primary_book_key": target["book_key"],
                    "primary_title": target["display_title"],
                    "primary_content_id": target.get("content_id"),
                    "primary_pages": int(target.get("pages") or 0),
                    "primary_evidence": target.get("evidence"),
                }
            )
            continue
        primary_candidates = primary_by_base.get(base_key, [])
        if primary_candidates and base_key not in seen_split_keys:
            split_required.append(
                {
                    "subject": base_key[0],
                    "base_title": base_title,
                    "primary_editions": sorted({str(candidate.get("edition") or "").strip() or "未标注" for candidate in primary_candidates}),
                    "supplemental_editions": sorted(
                        {
                            str(entry.get("edition") or "").strip() or "未标注"
                            for entry in (supplemental_manifest.get("book_catalog") or [])
                            if not entry.get("has_page_images")
                            and str(entry.get("subject") or "").strip() == base_key[0]
                            and _normalize_lookup_title(str(entry.get("base_title") or entry.get("title") or "").strip()) == base_key[1]
                        }
                    ),
                }
            )
            seen_split_keys.add(base_key)

    return {
        "safe_merge_candidates": safe_matches,
        "split_required_groups": split_required,
    }


def _render_markdown_report(primary_manifest: dict, manifest_stats: dict, reconciliation: dict) -> str:
    primary_catalog = list(primary_manifest["by_book_key"].values())
    lines = [
        "# Textbook Identity Audit",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Summary",
        "",
        f"- Primary books audited: `{len(primary_catalog)}`",
        f"- Primary books with resolved editions: `{len(primary_catalog) - len(manifest_stats['unresolved'])}`",
        f"- Primary books unresolved: `{len(manifest_stats['unresolved'])}`",
        f"- Duplicate primary identity groups: `{len(manifest_stats['duplicate_groups'])}`",
        f"- Safe supplemental -> primary merges: `{len(reconciliation['safe_merge_candidates'])}`",
        f"- Same-base-title groups that must stay split by edition: `{len(reconciliation['split_required_groups'])}`",
        "",
        "## Primary Editions",
        "",
    ]
    for edition, count in sorted(manifest_stats["edition_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{edition}`: `{count}`")

    lines.extend(["", "## Safe Merge Candidates", ""])
    for item in reconciliation["safe_merge_candidates"]:
        lines.append(
            f"- `{item['subject']}` {item['supplemental_title']} -> `{item['primary_title']}` "
            f"(`supp {item['supplemental_pages']}p` vs `primary {item['primary_pages']}p`, evidence `{item['primary_evidence']}`)"
        )

    lines.extend(["", "## Split-Required Groups", ""])
    for item in reconciliation["split_required_groups"]:
        lines.append(
            f"- `{item['subject']}` {item['base_title']}: primary `{', '.join(item['primary_editions'])}` / supplemental `{', '.join(item['supplemental_editions'])}`"
        )

    if manifest_stats["unresolved"]:
        lines.extend(["", "## Unresolved Primary Books", ""])
        for item in manifest_stats["unresolved"]:
            lines.append(f"- `{item['subject']}` {item['title']} ({item['book_key']})")

    if manifest_stats["duplicate_groups"]:
        lines.extend(["", "## Duplicate Primary Identity Groups", ""])
        for key, items in sorted(manifest_stats["duplicate_groups"].items()):
            lines.append(f"- `{key[0]}` `{key[1]}` `{key[2]}` -> `{len(items)}` books")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the verified textbook version manifest and identity audit report.")
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[2] / "data/index/textbook_mineru_fts.db")
    parser.add_argument("--book-map", type=Path, default=Path(__file__).resolve().parents[1] / "frontend/assets/pages/book_map.json")
    parser.add_argument("--supplemental-manifest", type=Path, default=Path(__file__).resolve().parents[1] / "backend/supplemental_textbook_pages.manifest.json")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "backend/textbook_version_manifest.json")
    parser.add_argument("--report", type=Path, default=Path(__file__).resolve().parents[1] / "docs/textbook_identity_audit.md")
    parser.add_argument("--allow-unresolved", action="store_true")
    args = parser.parse_args()

    book_map = _load_book_map(args.book_map)
    supplemental_manifest = _load_supplemental_manifest(args.supplemental_manifest)
    primary_books = _load_primary_books(args.db)
    primary_manifest, manifest_stats = _build_primary_manifest(primary_books, book_map)
    reconciliation = _build_reconciliation(primary_manifest, supplemental_manifest)

    payload = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "primary_books": len(primary_manifest["by_book_key"]),
        "resolved_primary_books": len(primary_manifest["by_book_key"]) - len(manifest_stats["unresolved"]),
        "unresolved_primary_books": len(manifest_stats["unresolved"]),
        "duplicate_primary_identity_groups": len(manifest_stats["duplicate_groups"]),
        "edition_counts": dict(sorted(manifest_stats["edition_counts"].items())),
        "safe_merge_candidates": reconciliation["safe_merge_candidates"],
        "split_required_groups": reconciliation["split_required_groups"],
        "by_content_id": primary_manifest["by_content_id"],
        "by_book_key": primary_manifest["by_book_key"],
    }

    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(_render_markdown_report(primary_manifest, manifest_stats, reconciliation), encoding="utf-8")

    if manifest_stats["duplicate_groups"]:
        print(f"duplicate primary identity groups: {len(manifest_stats['duplicate_groups'])}")
        return 1
    if manifest_stats["unresolved"] and not args.allow_unresolved:
        print(f"unresolved primary books: {len(manifest_stats['unresolved'])}")
        return 1

    print(
        json.dumps(
            {
                "primary_books": payload["primary_books"],
                "resolved_primary_books": payload["resolved_primary_books"],
                "safe_merge_candidates": len(payload["safe_merge_candidates"]),
                "split_required_groups": len(payload["split_required_groups"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
