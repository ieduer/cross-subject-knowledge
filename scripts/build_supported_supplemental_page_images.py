#!/usr/bin/env python3
"""
Render page-image products for the currently supported supplemental textbooks.

Supported policy:
  - all 人教版 textbooks
  - 英语 北师大版
  - 化学 鲁科版

This script only renders supplemental-only books that are in the supported
policy set. It updates `frontend/assets/pages/book_map.json` in-place by
merging the generated supplemental entries with the existing primary page map.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLATFORM_ROOT = ROOT / "platform"
BACKUP_ROOT = ROOT / "data" / "mineru_output_backup"
BOOK_MAP_PATH = PLATFORM_ROOT / "frontend" / "assets" / "pages" / "book_map.json"
PAGES_ROOT = PLATFORM_ROOT / "frontend" / "assets" / "pages"
SUPPLEMENTAL_MANIFEST_PATH = PLATFORM_ROOT / "backend" / "supplemental_textbook_pages.manifest.json"

DPI = 100
MAX_WIDTH = 1200
WEBP_QUALITY = 60
PAGE_NUMBER_RE = re.compile(r"^[\s\.]*\d+[\s\.]*$")


def _is_supported_runtime_edition(subject: str | None, edition: str | None) -> bool:
    normalized_subject = str(subject or "").strip()
    normalized_edition = str(edition or "").strip()
    return (
        normalized_edition == "人教版"
        or (normalized_subject == "英语" and normalized_edition == "北师大版")
        or (normalized_subject == "化学" and normalized_edition == "鲁科版")
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _detect_page_offset(model_json_path: Path | None) -> int:
    if not model_json_path or not model_json_path.exists():
        return 0
    try:
        data = json.loads(model_json_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0

    offsets: dict[int, int] = {}
    for page_idx, page_data in enumerate(data):
        if not isinstance(page_data, dict):
            continue
        page_info = page_data.get("page_info") or {}
        try:
            height = float(page_info.get("height") or 3500)
        except Exception:
            height = 3500
        top_margin = height * 0.15
        bottom_margin = height * 0.85
        for det in page_data.get("layout_dets", []):
            if not isinstance(det, dict):
                continue
            text = str(det.get("text") or "").strip()
            if not PAGE_NUMBER_RE.fullmatch(text):
                continue
            digits = re.sub(r"[^\d]", "", text)
            if not digits:
                continue
            number = int(digits)
            if number <= 0 or number >= 500:
                continue
            poly = det.get("poly") or []
            if len(poly) != 8:
                continue
            y_coords = [poly[1], poly[3], poly[5], poly[7]]
            y_center = sum(y_coords) / 4
            if top_margin <= y_center <= bottom_margin:
                continue
            offsets[page_idx - number] = offsets.get(page_idx - number, 0) + 1
            break
    if not offsets:
        return 0
    return sorted(offsets.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _resolve_unique_source_dir(backup_root: Path, content_id: str) -> Path:
    matches = sorted(
        {
            path.parent
            for path in backup_root.rglob(f"*{content_id}*_origin.pdf")
            if path.is_file()
        }
    )
    if len(matches) != 1:
        raise RuntimeError(f"{content_id}: expected 1 origin pdf directory, got {len(matches)}")
    return matches[0]


def _render_pdf(book_key: str, pdf_path: Path, out_root: Path) -> tuple[str, int, int, int]:
    import pymupdf
    from PIL import Image

    short_key = hashlib.md5(book_key.encode("utf-8")).hexdigest()[:12]
    out_dir = out_root / short_key
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(str(pdf_path))
    total = len(doc)
    converted = 0
    skipped = 0

    for page_num in range(total):
        out_file = out_dir / f"p{page_num}.webp"
        if out_file.exists() and out_file.stat().st_size > 500:
            skipped += 1
            continue

        page = doc[page_num]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(DPI / 72, DPI / 72))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        if img.width > MAX_WIDTH:
            ratio = MAX_WIDTH / img.width
            img = img.resize((MAX_WIDTH, int(img.height * ratio)), Image.LANCZOS)
        img.save(str(out_file), "WebP", quality=WEBP_QUALITY)
        converted += 1

    doc.close()
    return short_key, total, converted, skipped


def _iter_supported_supplemental_books(manifest: dict) -> list[dict]:
    books = []
    for item in manifest.get("book_catalog") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("source") or "").strip() != "supplemental_only":
            continue
        subject = str(item.get("subject") or "").strip()
        edition = str(item.get("edition") or "").strip()
        if not _is_supported_runtime_edition(subject, edition):
            continue
        content_id = str(item.get("content_id") or "").strip()
        if not content_id:
            raise RuntimeError(f"{item.get('book_key')}: supported supplemental book missing content_id")
        books.append(item)
    return sorted(books, key=lambda item: (item.get("subject") or "", item.get("title") or "", item.get("book_key") or ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build page-image assets for supported supplemental textbooks.")
    parser.add_argument("--backup-root", type=Path, default=BACKUP_ROOT)
    parser.add_argument("--manifest", type=Path, default=SUPPLEMENTAL_MANIFEST_PATH)
    parser.add_argument("--book-map", type=Path, default=BOOK_MAP_PATH)
    parser.add_argument("--pages-root", type=Path, default=PAGES_ROOT)
    args = parser.parse_args()

    backup_root = args.backup_root.expanduser().resolve()
    manifest_path = args.manifest.expanduser().resolve()
    book_map_path = args.book_map.expanduser().resolve()
    pages_root = args.pages_root.expanduser().resolve()

    manifest = _load_json(manifest_path)
    book_map = _load_json(book_map_path)
    books = _iter_supported_supplemental_books(manifest)
    print(f"Supported supplemental-only books: {len(books)}")

    generated = 0
    merged_map = dict(book_map)
    for index, item in enumerate(books, start=1):
        book_key = str(item["book_key"])
        subject = str(item.get("subject") or "").strip()
        title = str(item.get("title") or "").strip()
        content_id = str(item.get("content_id") or "").strip()

        source_dir = _resolve_unique_source_dir(backup_root, content_id)
        origin_pdf = next(source_dir.glob("*_origin.pdf"), None)
        model_json = next(source_dir.glob("*_model.json"), None)
        if not origin_pdf:
            raise RuntimeError(f"{book_key}: missing origin pdf in {source_dir}")

        short_key, total_pages, converted, skipped = _render_pdf(book_key, origin_pdf, pages_root)
        page_offset = _detect_page_offset(model_json)
        merged_map[book_key] = {
            "key": short_key,
            "title": str(item.get("base_title") or title).strip() or title,
            "display_title": title,
            "pages": total_pages,
            "page_offset": int(page_offset or 0),
            "content_id": content_id,
            "subject": subject,
            "edition": str(item.get("edition") or "").strip(),
        }
        generated += converted
        print(
            f"[{index:02d}/{len(books):02d}] {title} -> {short_key} "
            f"{total_pages}p ({converted} new, {skipped} cached)"
        )

    ordered = dict(sorted(merged_map.items(), key=lambda pair: (pair[1].get("title") or "", pair[0])))
    book_map_path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Updated book map: {book_map_path}")
    print(f"Total new pages rendered: {generated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
