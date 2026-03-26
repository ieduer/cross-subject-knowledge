#!/usr/bin/env python3
"""Build a local read-only SQLite index for 教育部《成語典》.

Data source: https://language.moe.gov.tw/001/Upload/Files/site_content/M0001/respub/dict_idiomsdict_download.html
License: CC BY-ND 3.0 TW

Usage:
    python build_moe_idiom_dict_index.py --download
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import sqlite3
import sys
import unicodedata
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"
INDEX_DIR = DATA_ROOT / "index"
DOWNLOAD_DIR = DATA_ROOT / "downloads" / "moe_idioms"

DEFAULT_DOWNLOAD_PAGE = (
    "https://language.moe.gov.tw/001/Upload/Files/site_content/M0001/respub/"
    "dict_idiomsdict_download.html"
)
DEFAULT_SOURCE_URL = (
    "https://language.moe.gov.tw/001/Upload/Files/site_content/M0001/respub/download/"
    "dict_idioms_2020_20251224.zip"
)
DEFAULT_SOURCE_PATH = DOWNLOAD_DIR / Path(DEFAULT_SOURCE_URL).name
DEFAULT_DB_PATH = INDEX_DIR / "dict_moe_idioms.db"
DEFAULT_DESCRIPTION = (
    "本典收錄常用成語，提供釋義、典故出處、用法辨析及例句，"
    "適合學生查詢成語意義與正確用法。"
)
DEFAULT_LICENSE = "CC BY-ND 3.0 TW"
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

HEADWORD_PATTERNS = (
    "成語", "成语",
    "字詞名", "字词名", "詞目", "词目", "字詞", "字词", "詞語", "词语", "名稱", "名称", "標題", "标题",
)
BOPOMOFO_PATTERNS = ("注音",)
PINYIN_PATTERNS = ("拼音", "漢語拼音", "汉语拼音")
TEXT_PATTERNS = (
    "釋義", "释义", "內容", "内容", "解釋", "解释", "義項", "义项",
    "典故說明", "典故说明", "用法說明", "用法说明",
    "書證", "书证", "例句", "出處", "出处", "補充", "补充",
    "辨似", "辨識", "附注", "附錄", "附录",
)
AUXILIARY_TEXT_PATTERNS = (
    "近義成語", "近义成语", "反義成語", "反义成语",
    "相似詞", "相似词", "相反詞", "相反词", "似", "反",
)
ID_PATTERNS = ("編號", "编号", "序號", "序号", "id")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a local read-only SQLite index for 教育部《成語典》."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_PATH, help="Source .zip or .xlsx path.")
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL, help="Official download URL.")
    parser.add_argument("--output", type=Path, default=DEFAULT_DB_PATH, help="SQLite output path.")
    parser.add_argument("--download", action="store_true", help="Download the source package before building.")
    return parser.parse_args()


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def normalize_header(value: object) -> str:
    text = normalize_text(value)
    return re.sub(r"\s+", "", text)


def compact_query(value: str) -> str:
    text = normalize_text(value)
    text = re.sub(r"[^\w\u4e00-\u9fff\s]", "", text)
    return re.sub(r"\s+", "", text)


def looks_like_id_header(header: str) -> bool:
    lowered = header.lower()
    return any(pattern.lower() == lowered or pattern.lower() in lowered for pattern in ID_PATTERNS)


def pick_header(headers: list[str], patterns: tuple[str, ...]) -> str | None:
    if not headers:
        return None
    for pattern in patterns:
        for header in headers:
            if header == pattern:
                return header
    for pattern in patterns:
        for header in headers:
            if pattern in header:
                return header
    return None


def pick_headers(headers: list[str], patterns: tuple[str, ...]) -> list[str]:
    matched = []
    for header in headers:
        if any(pattern in header for pattern in patterns):
            matched.append(header)
    return matched


def strip_markup(value: str) -> str:
    text = html.unescape(normalize_text(value))
    if not text:
        return ""
    text = re.sub(r"_x[0-9A-Fa-f]{4}_", "", text)
    text = re.sub(r"\*\d+\*", "", text)
    text = re.sub(r"#", "", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</?(?:div|p|span|font|b|strong|i|em|u|sup|sub|ol|ul|li)[^>]*>", "", text)
    text = re.sub(r"(?i)<[^>]+>", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def download_source(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, dest.open("wb") as fh:
        fh.write(response.read())


def find_nested_xlsx_name(container: zipfile.ZipFile) -> str:
    candidates = [name for name in container.namelist() if name.lower().endswith(".xlsx")]
    if not candidates:
        raise FileNotFoundError("No .xlsx file found inside the downloaded package.")
    candidates.sort(key=lambda name: (container.getinfo(name).file_size, name), reverse=True)
    return candidates[0]


def open_workbook_archive(source_path: Path) -> tuple[zipfile.ZipFile, str]:
    suffix = source_path.suffix.lower()
    if suffix == ".xlsx":
        archive = zipfile.ZipFile(source_path)
        return archive, source_path.name
    if suffix == ".zip":
        container = zipfile.ZipFile(source_path)
        nested_name = find_nested_xlsx_name(container)
        workbook_bytes = container.read(nested_name)
        container.close()
        archive = zipfile.ZipFile(io.BytesIO(workbook_bytes))
        return archive, nested_name
    raise ValueError(f"Unsupported source format: {source_path}")


def load_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    values = []
    for item in root.findall(f"{{{NS_MAIN}}}si"):
        text = "".join(node.text or "" for node in item.iter(f"{{{NS_MAIN}}}t"))
        values.append(normalize_text(text))
    return values


def resolve_sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_map = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.findall(f"{{{NS_PKG}}}Relationship")
    }
    sheets = []
    for sheet in workbook.findall(f".//{{{NS_MAIN}}}sheet"):
        name = sheet.attrib.get("name") or "Sheet1"
        rel_id = sheet.attrib.get(f"{{{NS_REL}}}id")
        target = rel_map.get(rel_id or "", "")
        if not target:
            continue
        if not target.startswith("xl/"):
            target = f"xl/{target.lstrip('/')}"
        sheets.append((name, target))
    if not sheets:
        raise FileNotFoundError("Workbook has no readable sheet.")
    return sheets


def column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return max(1, index) - 1


def parse_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        text = "".join(node.text or "" for node in cell.iter(f"{{{NS_MAIN}}}t"))
        return normalize_text(text)
    value_node = cell.find(f"{{{NS_MAIN}}}v")
    if value_node is None or value_node.text is None:
        return ""
    raw_value = value_node.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)]
        except Exception:
            return normalize_text(raw_value)
    return normalize_text(raw_value)


def iter_sheet_rows(archive: zipfile.ZipFile, sheet_target: str, shared_strings: list[str]):
    root = ET.fromstring(archive.read(sheet_target))
    sheet_data = root.find(f"{{{NS_MAIN}}}sheetData")
    if sheet_data is None:
        return
    for row in sheet_data.findall(f"{{{NS_MAIN}}}row"):
        values: dict[int, str] = {}
        for cell in row.findall(f"{{{NS_MAIN}}}c"):
            ref = cell.attrib.get("r", "")
            if not ref:
                continue
            values[column_index(ref)] = parse_cell_value(cell, shared_strings)
        if not values:
            continue
        max_index = max(values)
        yield [values.get(index, "") for index in range(max_index + 1)]


def extract_rows(source_path: Path) -> tuple[list[str], list[dict[str, str]], dict]:
    archive, workbook_name = open_workbook_archive(source_path)
    try:
        shared_strings = load_shared_strings(archive)
        sheets = resolve_sheet_targets(archive)
        sheet_name, sheet_target = sheets[0]
        rows_iter = iter_sheet_rows(archive, sheet_target, shared_strings)
        header_row = next(rows_iter, [])
        headers = [normalize_header(value) or f"欄位{index + 1}" for index, value in enumerate(header_row)]
        records = []
        for raw_row in rows_iter:
            if len(raw_row) < len(headers):
                raw_row += [""] * (len(headers) - len(raw_row))
            row_map = {}
            for index, header in enumerate(headers):
                value = raw_row[index] if index < len(raw_row) else ""
                if value:
                    row_map[header] = value
            if row_map:
                records.append(row_map)
        metadata = {
            "workbook_name": workbook_name,
            "sheet_name": sheet_name,
            "header_count": len(headers),
            "headers": headers,
        }
        return headers, records, metadata
    finally:
        archive.close()


def build_display_text(row_map: dict[str, str], selected_headers: dict[str, str | list[str]]) -> str:
    headword_header = selected_headers.get("headword")
    bopomofo_header = selected_headers.get("bopomofo")
    pinyin_header = selected_headers.get("pinyin")
    primary_text_headers = set(selected_headers.get("primary_text") or [])
    auxiliary_headers = set(selected_headers.get("auxiliary_text") or [])

    sections = []
    for header in primary_text_headers:
        value = strip_markup(row_map.get(header, ""))
        if value:
            sections.append(value)
    for header in auxiliary_headers:
        value = strip_markup(row_map.get(header, ""))
        if value:
            sections.append(f"{header}：{value}")

    if not sections:
        for header, value in row_map.items():
            if header in {headword_header, bopomofo_header, pinyin_header}:
                continue
            if looks_like_id_header(header):
                continue
            clean_value = strip_markup(value)
            if not clean_value:
                continue
            if len(header) <= 6:
                sections.append(f"{header}：{clean_value}")
            else:
                sections.append(clean_value)

    text = "\n\n".join(section for section in sections if section)
    return text.strip()


def normalize_record(row_map: dict[str, str], selected_headers: dict[str, str | list[str]]) -> dict | None:
    headword_header = selected_headers.get("headword")
    if not isinstance(headword_header, str):
        return None
    headword = normalize_text(row_map.get(headword_header, ""))
    if not headword:
        return None

    bopomofo_header = selected_headers.get("bopomofo")
    pinyin_header = selected_headers.get("pinyin")
    display_text = build_display_text(row_map, selected_headers)
    if not display_text:
        return None

    raw_digest = hashlib.sha1(
        json.dumps(row_map, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "headword": headword,
        "headword_norm": compact_query(headword),
        "bopomofo": normalize_text(row_map.get(bopomofo_header, "")) if isinstance(bopomofo_header, str) else "",
        "pinyin": normalize_text(row_map.get(pinyin_header, "")) if isinstance(pinyin_header, str) else "",
        "content_text": display_text,
        "content_hash": raw_digest,
        "raw_json": json.dumps(row_map, ensure_ascii=False, sort_keys=True),
    }


def detect_headers(headers: list[str]) -> dict[str, str | list[str]]:
    selected = {
        "headword": pick_header(headers, HEADWORD_PATTERNS),
        "bopomofo": pick_header(headers, BOPOMOFO_PATTERNS),
        "pinyin": pick_header(headers, PINYIN_PATTERNS),
        "primary_text": pick_headers(headers, TEXT_PATTERNS),
        "auxiliary_text": pick_headers(headers, AUXILIARY_TEXT_PATTERNS),
    }
    if not selected["headword"] and headers:
        selected["headword"] = headers[0]
    return selected


def write_database(output_path: Path, normalized_rows: list[dict], metadata: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    con = sqlite3.connect(output_path)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                headword TEXT NOT NULL,
                headword_norm TEXT NOT NULL,
                bopomofo TEXT,
                pinyin TEXT,
                content_text TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                raw_json TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX idx_idiom_entries_headword_norm ON entries(headword_norm)")
        con.execute("CREATE INDEX idx_idiom_entries_headword ON entries(headword)")
        con.execute("CREATE INDEX idx_idiom_entries_hash ON entries(content_hash)")
        con.executemany(
            """
            INSERT INTO entries (
                headword, headword_norm, bopomofo, pinyin, content_text, content_hash, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["headword"],
                    row["headword_norm"],
                    row["bopomofo"],
                    row["pinyin"],
                    row["content_text"],
                    row["content_hash"],
                    row["raw_json"],
                )
                for row in normalized_rows
            ],
        )
        con.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            sorted((key, json.dumps(value, ensure_ascii=False)) for key, value in metadata.items()),
        )
        con.commit()
    finally:
        con.close()


def main() -> int:
    args = parse_args()
    source_path = args.source.expanduser().resolve()
    output_path = args.output.expanduser().resolve()

    if args.download or not source_path.exists():
        source_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {args.source_url} -> {source_path}")
        download_source(args.source_url, source_path)

    if not source_path.exists():
        print(f"Source file not found: {source_path}", file=sys.stderr)
        return 1

    headers, raw_rows, workbook_meta = extract_rows(source_path)
    print(f"Detected headers: {headers}")
    selected_headers = detect_headers(headers)
    print(f"Selected: {json.dumps(selected_headers, ensure_ascii=False)}")
    normalized_rows = []
    duplicate_counter = Counter()

    for row_map in raw_rows:
        normalized = normalize_record(row_map, selected_headers)
        if not normalized:
            continue
        dedupe_key = (
            normalized["headword_norm"],
            normalized["bopomofo"],
            normalized["pinyin"],
            normalized["content_hash"],
        )
        duplicate_counter[dedupe_key] += 1
        if duplicate_counter[dedupe_key] > 1:
            continue
        normalized_rows.append(normalized)

    term_count = len({row["headword_norm"] for row in normalized_rows if row["headword_norm"]})
    metadata = {
        "source_id": "moe_idioms",
        "label": "教育部《成語典》",
        "download_page": DEFAULT_DOWNLOAD_PAGE,
        "source_url": args.source_url,
        "source_path": str(source_path),
        "description": DEFAULT_DESCRIPTION,
        "license": DEFAULT_LICENSE,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(normalized_rows),
        "term_count": term_count,
        "workbook": workbook_meta,
        "selected_headers": selected_headers,
    }

    write_database(output_path, normalized_rows, metadata)

    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output_path),
                "row_count": len(normalized_rows),
                "term_count": term_count,
                "sheet_name": workbook_meta.get("sheet_name"),
                "headword_header": selected_headers.get("headword"),
                "primary_text_headers": selected_headers.get("primary_text"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
