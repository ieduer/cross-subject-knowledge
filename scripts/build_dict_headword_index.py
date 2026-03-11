#!/Users/ylsuen/.venv/bin/python
"""Build the runtime headword-to-page index for the image-only dictionary view.

Current production strategy:
- `xuci`: parse `pdftotext -raw` in reading order, detect entry start lines, and
  derive full page spans from the next detected headword.
- `changyong`: OCR only the page-header headword strip with macOS Vision, then
  derive full page spans from the trusted headword order in the external CSV
  bottom text.

Student-facing output remains image-only. OCR and CSV data are used only for
index construction and audit artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_INDEX = REPO_ROOT / "data" / "index"
TMP_ROOT = REPO_ROOT / "tmp" / "dict_index_build"
BOOKS_DIR = Path("/Users/ylsuen/Books/books2stu")

XUCI_PDF = BOOKS_DIR / "古代汉语虚词词典.pdf"
CHANGYONG_PDF = BOOKS_DIR / "古汉语常用字字典 第5版 by 王力.pdf"
VISION_OCR_SWIFT = Path(__file__).with_name("vision_ocr.swift")

DEFAULT_CHANGYONG_CSV_CANDIDATES = (
    Path("/tmp/hanyu-dicts/古汉语常用字字典（第5版）.csv"),
    REPO_ROOT / "tmp" / "古汉语常用字字典（第5版）.csv",
)

REVIEW_TSV = DATA_INDEX / "dict_headword_review.tsv"
RUNTIME_JSON = DATA_INDEX / "dict_headword_pages.json"
QC_JSON = DATA_INDEX / "dict_headword_qc.json"
XUCI_CANDIDATES_JSONL = DATA_INDEX / "dict_headword_candidates_xuci.jsonl"
CHANGYONG_CANDIDATES_JSONL = DATA_INDEX / "dict_headword_candidates_changyong.jsonl"

REVIEW_FIELDS = [
    "dict_source",
    "headword",
    "headword_trad",
    "page_numbers",
    "verified",
    "confidence",
    "detector",
    "status",
    "notes",
]

XUCI_START_PAGE = 15
XUCI_CATEGORIES = {
    "副词",
    "助动词",
    "复合虚词",
    "连词",
    "介词",
    "代词",
    "语气词",
    "叹词",
}
XUCI_DETECTOR_PRIORITY = {
    "compound": 3,
    "inline": 2,
    "page_top": 1,
}

HANZI_RE = re.compile(r"[\u4e00-\u9fff]")
HEADWORD_PREFIX_RE = re.compile(r"^([\u4e00-\u9fff]{1,6})")
PINYINISH_RE = re.compile(r"^[A-Za-z0-9\u00C0-\u024F]+$")
ASCII_PUNCT_SPACES_RE = re.compile(r"[^a-z-]+")
ASCII_OR_DIGIT_RE = re.compile(r"[A-Za-z0-9]")

# Header crop tuned from manual page checks on 2026-03-06.
HEADER_X_RATIO = 0.056
HEADER_Y_RATIO = 0.011
HEADER_W_RATIO = 0.91
HEADER_H_RATIO = 0.072
HEADER_RESIZE = "200%"
VISION_BATCH_SIZE = 12
CHANGYONG_RENDER_DPI = 400
CHANGYONG_HEADER_CACHE_DIR = TMP_ROOT / "changyong_headers_cache"

DICT_PAGE_COUNTS = {
    "xuci": 921,
    "changyong": 659,
}

XUCI_QC_SAMPLES = {
    "恭": 188,
    "躬": 188,
    "躬亲": 188,
    "会": 260,
    "会当": 260,
    "会须": 261,
    "正使": 844,
    "政": 844,
    "之": 844,
    "安": 15,
    "暗暗": 20,
    "不成": 49,
}

CHANGYONG_QC_SAMPLES = {
    "觇": 99,
    "幨": 99,
    "襜": 99,
    "蝉": 99,
    "谗": 99,
    "欃": 99,
    "巉": 99,
    "孱": 100,
    "潺": 100,
    "缠": 100,
    "廛": 100,
    "躔": 100,
    "澶": 100,
    "蟾": 100,
    "产": 100,
    "阐": 101,
    "蒇": 101,
    "羼": 101,
    "伥": 101,
    "菖": 101,
    "猖": 101,
    "阊": 101,
    "长": 101,
}


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _contains_hanzi(text: str) -> bool:
    return bool(HANZI_RE.search(text or ""))


def _normalize_lines(page_text: str) -> list[str]:
    return [line for line in (_normalize_text(item).strip() for item in page_text.splitlines()) if line]


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return proc.stdout


def _ensure_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"Missing required tool: {name}")


def _split_pdf_text_pages(pdf_path: Path, *, raw: bool = False) -> list[str]:
    cmd = ["pdftotext"]
    if raw:
        cmd.append("-raw")
    cmd.extend([str(pdf_path), "-"])
    return _run(cmd).split("\f")


def _headword_from_token(token: str) -> str | None:
    match = HEADWORD_PREFIX_RE.match(_normalize_text(token))
    if not match:
        return None
    return match.group(1)


def _looks_like_headword_token(token: str) -> bool:
    normalized = _normalize_text(token)
    headword = _headword_from_token(normalized)
    if not headword:
        return False
    tail = normalized[len(headword):]
    if not tail:
        return True
    cleaned = re.sub(r"[\u4e00-\u9fff（）()\[\]{}【】｛｝]", "", tail)
    return cleaned == ""


def _parse_page_numbers(raw: str | None) -> list[int]:
    if not raw:
        return []
    pages: list[int] = []
    for chunk in re.split(r"[,\s]+", raw.strip()):
        if not chunk:
            continue
        if "-" in chunk:
            start_raw, end_raw = chunk.split("-", 1)
            try:
                start = int(start_raw)
                end = int(end_raw)
            except ValueError:
                continue
            if start <= 0:
                continue
            for page_num in range(start, max(start, end) + 1):
                pages.append(page_num)
        else:
            try:
                page_num = int(chunk)
            except ValueError:
                continue
            if page_num > 0:
                pages.append(page_num)
    return sorted(set(pages))


def _format_page_numbers(pages: list[int]) -> str:
    if not pages:
        return ""
    spans = []
    pages = sorted(set(pages))
    start = prev = pages[0]
    for page_num in pages[1:]:
        if page_num == prev + 1:
            prev = page_num
            continue
        spans.append((start, prev))
        start = prev = page_num
    spans.append((start, prev))
    parts = []
    for span_start, span_end in spans:
        if span_start == span_end:
            parts.append(str(span_start))
        else:
            parts.append(f"{span_start}-{span_end}")
    return ",".join(parts)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "verified"}


def _normalize_review_row(row: dict) -> dict:
    normalized = {field: str(row.get(field, "") or "").strip() for field in REVIEW_FIELDS}
    normalized["page_numbers"] = _format_page_numbers(_parse_page_numbers(normalized["page_numbers"]))
    return normalized


def _load_review_rows() -> list[dict]:
    if not REVIEW_TSV.exists():
        return []
    with REVIEW_TSV.open("r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh, delimiter="\t")]


def _write_candidates_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _extract_xuci_candidate(line: str, lines: list[str], idx: int, page_num: int) -> dict | None:
    normalized = _normalize_text(line)
    tokens = normalized.split()

    if len(tokens) == 2:
        headword = _headword_from_token(tokens[0])
        tail = tokens[1].strip()
        if headword and _looks_like_headword_token(tokens[0]) and tail and ASCII_OR_DIGIT_RE.search(tail):
            return {
                "headword": headword,
                "page_start": page_num,
                "line_index": idx,
                "detector": "inline",
                "source_line": normalized,
            }

    headword = _headword_from_token(normalized)
    if not headword or not _looks_like_headword_token(normalized):
        return None

    next_1 = lines[idx + 1] if idx + 1 < len(lines) else ""
    prev_1 = lines[idx - 1] if idx > 0 else ""
    prev_is_page_number = prev_1.isdigit()

    if idx == 0 and next_1.isdigit():
        return {
            "headword": headword,
            "page_start": page_num,
            "line_index": idx,
            "detector": "page_top",
            "source_line": normalized,
        }

    if next_1 == "复合虚词":
        return {
            "headword": headword,
            "page_start": page_num,
            "line_index": idx,
            "detector": "compound",
            "source_line": normalized,
        }

    if next_1 in XUCI_CATEGORIES and (idx <= 1 or prev_is_page_number):
        return {
            "headword": headword,
            "page_start": page_num,
            "line_index": idx,
            "detector": "page_top",
            "source_line": normalized,
        }
    return None


def _build_xuci_candidates() -> tuple[list[dict], dict]:
    if not XUCI_PDF.exists():
        raise FileNotFoundError(XUCI_PDF)
    raw_pages = _split_pdf_text_pages(XUCI_PDF, raw=True)

    raw_candidates: list[dict] = []
    for page_num, page_text in enumerate(raw_pages, start=1):
        if page_num < XUCI_START_PAGE:
            continue
        lines = _normalize_lines(page_text)
        for idx, line in enumerate(lines):
            candidate = _extract_xuci_candidate(line, lines, idx, page_num)
            if candidate:
                raw_candidates.append(candidate)

    raw_candidates.sort(
        key=lambda item: (
            item["page_start"],
            item["line_index"],
            -XUCI_DETECTOR_PRIORITY[item["detector"]],
        )
    )

    dedup_by_page_headword: dict[tuple[str, int], dict] = {}
    for row in raw_candidates:
        key = (row["headword"], row["page_start"])
        existing = dedup_by_page_headword.get(key)
        if existing is None:
            dedup_by_page_headword[key] = row
            continue
        better = (
            XUCI_DETECTOR_PRIORITY[row["detector"]],
            -row["line_index"],
        ) > (
            XUCI_DETECTOR_PRIORITY[existing["detector"]],
            -existing["line_index"],
        )
        if better:
            dedup_by_page_headword[key] = row

    ordered = sorted(
        dedup_by_page_headword.values(),
        key=lambda item: (
            item["page_start"],
            item["line_index"],
            -XUCI_DETECTOR_PRIORITY[item["detector"]],
        ),
    )

    unique_entries: list[dict] = []
    seen_headwords: set[str] = set()
    for row in ordered:
        headword = row["headword"]
        if headword in seen_headwords:
            continue
        unique_entries.append(row)
        seen_headwords.add(headword)

    page_total = len(raw_pages)
    candidates = []
    for idx, row in enumerate(unique_entries):
        next_page = unique_entries[idx + 1]["page_start"] if idx + 1 < len(unique_entries) else page_total
        end_page = max(row["page_start"], next_page - 1)
        pages = list(range(row["page_start"], end_page + 1))
        confidence = {
            "compound": 0.99,
            "inline": 0.97,
            "page_top": 0.94,
        }[row["detector"]]
        candidates.append(
            {
                "dict_source": "xuci",
                "headword": row["headword"],
                "headword_trad": "",
                "page_numbers": pages,
                "page_start": pages[0],
                "page_end": pages[-1],
                "detector": row["detector"],
                "confidence": confidence,
                "verified": True,
                "status": "auto_verified",
                "notes": f"raw:{row['source_line']}",
            }
        )

    qc = {
        "page_total": page_total,
        "entry_count": len(candidates),
        "sample_pages": {},
    }
    by_headword = {row["headword"]: row for row in candidates}
    for headword, expected_page in XUCI_QC_SAMPLES.items():
        row = by_headword.get(headword)
        qc["sample_pages"][headword] = {
            "expected_page": expected_page,
            "actual_pages": row["page_numbers"] if row else [],
            "pass": bool(row and expected_page in row["page_numbers"]),
        }

    return candidates, qc


def _resolve_changyong_csv(explicit_path: str | None) -> Path:
    if explicit_path:
        candidate = Path(explicit_path).expanduser().resolve()
        if not candidate.exists():
            raise FileNotFoundError(candidate)
        return candidate
    for candidate in DEFAULT_CHANGYONG_CSV_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Missing 古汉语常用字字典（第5版） CSV. "
        "Pass --changyong-csv or place it at /tmp/hanyu-dicts/古汉语常用字字典（第5版）.csv"
    )


def _normalize_pinyin_ascii(text: str) -> str:
    normalized = unicodedata.normalize("NFD", _normalize_text(text)).lower()
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = ASCII_PUNCT_SPACES_RE.sub("", normalized)
    return normalized


def _normalize_pinyin_range(text: str) -> str:
    normalized = unicodedata.normalize("NFD", _normalize_text(text)).lower()
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = re.sub(r"[^a-z\- ]+", "", normalized)
    normalized = normalized.replace(" ", "")
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized


def _load_changyong_headword_order(csv_path: Path) -> list[dict]:
    rows = csv.DictReader(csv_path.open("r", encoding="utf-8"))
    ordered: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        headword = _normalize_text(row.get("字词索引", "")).strip()
        if len(headword) != 1 or not HANZI_RE.fullmatch(headword):
            continue
        if headword in seen:
            continue
        seen.add(headword)
        ordered.append(
            {
                "headword": headword,
                "pinyin_raw": row.get("拼音", "").strip(),
                "pinyin_ascii": _normalize_pinyin_ascii(row.get("拼音", "")),
            }
        )
    return ordered


def _page_image_size(image_path: Path) -> tuple[int, int]:
    raw = _run(["identify", "-format", "%w %h", str(image_path)]).strip()
    width_raw, height_raw = raw.split()
    return int(width_raw), int(height_raw)


def _render_header_image(page_num: int, workdir: Path, width: int | None = None, height: int | None = None) -> Path:
    header_path = workdir / f"changyong_header_{page_num:03d}.png"
    if header_path.exists() and header_path.stat().st_size > 0:
        return header_path
    prefix = workdir / f"changyong_page_{page_num:03d}"
    subprocess.run(
        [
            "pdftoppm",
            "-r",
            str(CHANGYONG_RENDER_DPI),
            "-png",
            "-f",
            str(page_num),
            "-l",
            str(page_num),
            str(CHANGYONG_PDF),
            str(prefix),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    matches = sorted(workdir.glob(f"{prefix.name}-*.png"))
    if not matches:
        raise RuntimeError(f"Failed to render changyong page {page_num}")
    full_page = matches[0]
    if width is None or height is None:
        width, height = _page_image_size(full_page)

    crop_x = max(0, int(width * HEADER_X_RATIO))
    crop_y = max(0, int(height * HEADER_Y_RATIO))
    crop_w = max(1200, int(width * HEADER_W_RATIO))
    crop_h = max(180, int(height * HEADER_H_RATIO))
    subprocess.run(
        [
            "magick",
            str(full_page),
            "-crop",
            f"{crop_w}x{crop_h}+{crop_x}+{crop_y}",
            "+repage",
            "-resize",
            HEADER_RESIZE,
            "-colorspace",
            "Gray",
            "-contrast-stretch",
            "1%x1%",
            str(header_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    full_page.unlink(missing_ok=True)
    return header_path


def _chunked(items: list[Path], size: int) -> list[list[Path]]:
    return [items[idx: idx + size] for idx in range(0, len(items), size)]


def _run_vision_ocr(image_paths: list[Path]) -> dict[str, dict]:
    payloads: dict[str, dict] = {}
    chunks = _chunked(image_paths, VISION_BATCH_SIZE)
    for chunk_index, chunk in enumerate(chunks, start=1):
        print(
            f"[changyong] vision batch {chunk_index}/{len(chunks)} ({len(chunk)} headers)",
            file=sys.stderr,
            flush=True,
        )
        proc = subprocess.run(
            ["swift", str(VISION_OCR_SWIFT), *[str(path) for path in chunk]],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "Vision OCR failed for batch "
                f"{chunk_index}/{len(chunks)}: {proc.stderr.strip() or proc.stdout.strip() or 'unknown error'}"
            )
        output = proc.stdout
        for line in output.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            payloads[item["path"]] = item
    return payloads


def _extract_header_hanzi(payload: dict, valid_headwords: set[str]) -> str:
    observations = payload.get("observations") or []
    best_text = ""
    best_score = -math.inf
    for obs_index, obs in enumerate(observations[:5]):
        y = float(obs.get("y") or 0.0)
        if y < 0.30:
            continue
        height = float(obs.get("height") or 0.0)
        for cand_index, candidate in enumerate(obs.get("candidates") or []):
            text = _normalize_text(candidate)
            chars = "".join(ch for ch in text if ch in valid_headwords)
            if len(chars) < 4:
                continue
            score = (len(chars) * 10.0) + (height * 100.0) - (obs_index * 1.5) - (cand_index * 0.4)
            if score > best_score:
                best_score = score
                best_text = chars
    return best_text


def _extract_header_pinyin_range(payload: dict) -> str:
    observations = payload.get("observations") or []
    best = ""
    best_score = -math.inf
    for obs_index, obs in enumerate(observations[:5]):
        y = float(obs.get("y") or 0.0)
        if y < 0.30:
            continue
        height = float(obs.get("height") or 0.0)
        for cand_index, candidate in enumerate(obs.get("candidates") or []):
            normalized = _normalize_pinyin_range(candidate)
            if "-" not in normalized:
                continue
            left, right = normalized.split("-", 1)
            if len(left) < 3 or len(right) < 3:
                continue
            score = (len(left) + len(right)) + (height * 100.0) - (obs_index * 1.0) - (cand_index * 0.25)
            if score > best_score:
                best_score = score
                best = normalized
    return best


def _pinyin_in_range(pinyin_ascii: str, normalized_range: str) -> bool:
    if not normalized_range or not pinyin_ascii:
        return True
    try:
        left, right = normalized_range.split("-", 1)
    except ValueError:
        return True
    if not left or not right:
        return True
    return left <= pinyin_ascii <= right


def _build_changyong_candidates(csv_path: Path) -> tuple[list[dict], dict]:
    if not CHANGYONG_PDF.exists():
        raise FileNotFoundError(CHANGYONG_PDF)
    if not VISION_OCR_SWIFT.exists():
        raise FileNotFoundError(VISION_OCR_SWIFT)
    for tool in ("pdftoppm", "magick", "identify", "swift"):
        _ensure_tool(tool)

    ordered = _load_changyong_headword_order(csv_path)
    headword_meta = {item["headword"]: item for item in ordered}
    valid_headwords = set(headword_meta)

    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    CHANGYONG_HEADER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    first_header = _render_header_image(1, CHANGYONG_HEADER_CACHE_DIR)
    header_paths = {1: first_header}
    for page_num in range(2, DICT_PAGE_COUNTS["changyong"] + 1):
        if page_num == 2 or page_num % 50 == 0 or page_num == DICT_PAGE_COUNTS["changyong"]:
            print(
                f"[changyong] rendered header page {page_num}/{DICT_PAGE_COUNTS['changyong']}",
                file=sys.stderr,
                flush=True,
            )
        header_paths[page_num] = _render_header_image(page_num, CHANGYONG_HEADER_CACHE_DIR)

    ocr_payloads = _run_vision_ocr([header_paths[page_num] for page_num in sorted(header_paths)])

    page_headers = []
    start_page_by_headword: dict[str, int] = {}
    raw_header_by_headword: dict[str, str] = {}

    for page_num in range(1, DICT_PAGE_COUNTS["changyong"] + 1):
        payload = ocr_payloads.get(str(header_paths[page_num]), {})
        hanzi_line = _extract_header_hanzi(payload, valid_headwords)
        pinyin_range = _extract_header_pinyin_range(payload)
        filtered_chars = []
        for ch in hanzi_line:
            if ch in filtered_chars:
                continue
            meta = headword_meta.get(ch)
            if not meta:
                continue
            if not _pinyin_in_range(meta["pinyin_ascii"], pinyin_range):
                continue
            filtered_chars.append(ch)
        page_headers.append(
            {
                "page": page_num,
                "pinyin_range": pinyin_range,
                "ocr_hanzi": hanzi_line,
                "headwords": filtered_chars,
            }
        )
        for ch in filtered_chars:
            start_page_by_headword.setdefault(ch, page_num)
            raw_header_by_headword.setdefault(ch, hanzi_line)

    mapped_order = []
    for item in ordered:
        start_page = start_page_by_headword.get(item["headword"])
        if start_page is None:
            continue
        mapped_order.append((item, start_page))

    candidates = []
    for idx, (item, start_page) in enumerate(mapped_order):
        next_page = mapped_order[idx + 1][1] if idx + 1 < len(mapped_order) else DICT_PAGE_COUNTS["changyong"]
        end_page = max(start_page, next_page - 1)
        pages = list(range(start_page, end_page + 1))
        headword = item["headword"]
        candidates.append(
            {
                "dict_source": "changyong",
                "headword": headword,
                "headword_trad": "",
                "page_numbers": pages,
                "page_start": pages[0],
                "page_end": pages[-1],
                "detector": "page_header_vision",
                "confidence": 0.96,
                "verified": True,
                "status": "auto_verified",
                "notes": f"header:{raw_header_by_headword.get(headword, '')}",
            }
        )

    mapped_headwords = {row["headword"] for row in candidates}
    qc = {
        "csv_path": str(csv_path),
        "unique_single_char_headwords": len(ordered),
        "mapped_headwords": len(mapped_headwords),
        "coverage_ratio": round(len(mapped_headwords) / max(1, len(ordered)), 4),
        "header_samples": page_headers[98:101],
        "sample_pages": {},
    }
    by_headword = {row["headword"]: row for row in candidates}
    for headword, expected_page in CHANGYONG_QC_SAMPLES.items():
        row = by_headword.get(headword)
        qc["sample_pages"][headword] = {
            "expected_page": expected_page,
            "actual_pages": row["page_numbers"] if row else [],
            "pass": bool(row and row["page_start"] == expected_page),
        }

    return candidates, qc


def _merge_review_rows(candidate_rows: list[dict], existing_rows: list[dict]) -> list[dict]:
    candidate_keys = {(row["dict_source"], row["headword"]) for row in candidate_rows}
    by_key = {}
    for row in existing_rows:
        normalized = _normalize_review_row(row)
        key = (normalized["dict_source"], normalized["headword"])
        if key not in candidate_keys:
            continue
        by_key[key] = normalized

    for candidate in candidate_rows:
        key = (candidate["dict_source"], candidate["headword"])
        current = by_key.get(key, {})
        by_key[key] = {
            "dict_source": candidate["dict_source"],
            "headword": candidate["headword"],
            "headword_trad": current.get("headword_trad", ""),
            "page_numbers": _format_page_numbers(candidate["page_numbers"]),
            "verified": current.get("verified") or "1",
            "confidence": f"{float(current.get('confidence') or candidate['confidence']):.2f}",
            "detector": current.get("detector") or candidate["detector"],
            "status": current.get("status") or candidate["status"],
            "notes": current.get("notes") or candidate["notes"],
        }
    rows = list(by_key.values())
    rows.sort(key=lambda item: (item["dict_source"], item["headword"]))
    return rows


def _write_review_rows(rows: list[dict]) -> None:
    REVIEW_TSV.parent.mkdir(parents=True, exist_ok=True)
    with REVIEW_TSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REVIEW_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _build_runtime_json(review_rows: list[dict], candidate_rows: list[dict]) -> dict:
    candidate_count_by_source = Counter(row["dict_source"] for row in candidate_rows)
    verified_count_by_source = Counter()
    entries: dict[str, list[dict]] = defaultdict(list)

    for row in review_rows:
        normalized = _normalize_review_row(row)
        if not normalized["headword"] or not normalized["dict_source"]:
            continue
        if normalized["status"].lower() in {"drop", "disabled"}:
            continue
        if not _truthy(normalized["verified"]):
            continue
        page_numbers = _parse_page_numbers(normalized["page_numbers"])
        if not page_numbers:
            continue
        verified_count_by_source[normalized["dict_source"]] += 1
        entries[normalized["headword"]].append(
            {
                "dict_source": normalized["dict_source"],
                "display_headword": normalized["headword"],
                "headword_trad": normalized["headword_trad"] or None,
                "page_numbers": page_numbers,
                "verified": True,
                "confidence": float(normalized["confidence"] or 1.0),
                "detector": normalized["detector"] or "manual",
                "notes": normalized["notes"] or "",
            }
        )

    for headword in entries:
        entries[headword].sort(key=lambda item: (item["dict_source"], item["page_numbers"][0]))

    return {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "xuci": {
                "candidate_headwords": int(candidate_count_by_source.get("xuci", 0)),
                "verified_headwords": int(verified_count_by_source.get("xuci", 0)),
            },
            "changyong": {
                "candidate_headwords": int(candidate_count_by_source.get("changyong", 0)),
                "verified_headwords": int(verified_count_by_source.get("changyong", 0)),
            },
        },
        "entries": entries,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--changyong-csv",
        help="Trusted bottom-text CSV for 王力《古汉语常用字字典（第5版）》.",
    )
    parser.add_argument(
        "--skip-xuci",
        action="store_true",
        help="Skip rebuilding xuci candidates.",
    )
    parser.add_argument(
        "--skip-changyong",
        action="store_true",
        help="Skip rebuilding changyong candidates.",
    )
    args = parser.parse_args()

    DATA_INDEX.mkdir(parents=True, exist_ok=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)

    candidate_rows: list[dict] = []
    qc_payload = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "xuci": {},
        "changyong": {},
    }

    if args.skip_xuci:
        if XUCI_CANDIDATES_JSONL.exists():
            candidate_rows.extend(
                json.loads(line)
                for line in XUCI_CANDIDATES_JSONL.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
    else:
        xuci_candidates, xuci_qc = _build_xuci_candidates()
        _write_candidates_jsonl(XUCI_CANDIDATES_JSONL, xuci_candidates)
        candidate_rows.extend(xuci_candidates)
        qc_payload["xuci"] = xuci_qc

    if args.skip_changyong:
        if CHANGYONG_CANDIDATES_JSONL.exists():
            candidate_rows.extend(
                json.loads(line)
                for line in CHANGYONG_CANDIDATES_JSONL.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
    else:
        changyong_csv = _resolve_changyong_csv(args.changyong_csv)
        changyong_candidates, changyong_qc = _build_changyong_candidates(changyong_csv)
        _write_candidates_jsonl(CHANGYONG_CANDIDATES_JSONL, changyong_candidates)
        candidate_rows.extend(changyong_candidates)
        qc_payload["changyong"] = changyong_qc

    review_rows = _merge_review_rows(candidate_rows, _load_review_rows())
    _write_review_rows(review_rows)

    runtime_payload = _build_runtime_json(review_rows, candidate_rows)
    _write_json(RUNTIME_JSON, runtime_payload)
    _write_json(QC_JSON, qc_payload)

    print(
        json.dumps(
            {
                "candidate_jsonl": {
                    "xuci": str(XUCI_CANDIDATES_JSONL),
                    "changyong": str(CHANGYONG_CANDIDATES_JSONL),
                },
                "review_tsv": str(REVIEW_TSV),
                "runtime_json": str(RUNTIME_JSON),
                "qc_json": str(QC_JSON),
                "xuci_candidates": runtime_payload["sources"]["xuci"]["candidate_headwords"],
                "xuci_verified": runtime_payload["sources"]["xuci"]["verified_headwords"],
                "changyong_candidates": runtime_payload["sources"]["changyong"]["candidate_headwords"],
                "changyong_verified": runtime_payload["sources"]["changyong"]["verified_headwords"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
