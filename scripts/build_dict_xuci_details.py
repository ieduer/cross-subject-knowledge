#!/Users/ylsuen/.venv/bin/python
"""Build detailed runtime data for exam-tested xuci headwords.

Output:
- data/index/dict_exam_xuci_details.json

Current scope:
- Use the verified headword-page index to locate entries in:
  - 《古代汉语虚词词典》 (text layer via pdftotext)
  - 《古汉语常用字字典》第5版 (targeted OCR via pdftoppm + tesseract)
- Attach textbook classical examples from the MinerU textbook runtime DB.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_INDEX = REPO_ROOT / "data" / "index"
BOOKS_DIR = Path("/Users/ylsuen/Books/books2stu")
TMP_ROOT = REPO_ROOT / "tmp" / "dict_xuci_detail_build"

EXAM_XUCI_PATH = DATA_INDEX / "dict_exam_xuci.json"
HEADWORD_INDEX_PATH = DATA_INDEX / "dict_headword_pages.json"
TEXTBOOK_DB_PATH = DATA_INDEX / "textbook_mineru_fts.db"
TEXTBOOK_MANIFEST_PATH = REPO_ROOT / "platform" / "backend" / "textbook_classics_manifest.json"
OUTPUT_PATH = DATA_INDEX / "dict_exam_xuci_details.json"

XUCI_PDF = BOOKS_DIR / "古代汉语虚词词典.pdf"
CHANGYONG_PDF = BOOKS_DIR / "古汉语常用字字典 第5版 by 王力.pdf"

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
XUCI_SPECIAL_HEADERS = {"固定格式", "惯用词组"}
XUCI_USAGE_HEADERS = set(XUCI_CATEGORIES) | {"感叹词"} | XUCI_SPECIAL_HEADERS
XUCI_HEAD_RE = re.compile(r"^[\u4e00-\u9fff]{1,6}$")
XUCI_OUTLINE_RE = re.compile(r"^[一二三四五六七八九十]+[、,，]")
PINYIN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9;,' -]{0,24}$")
PUNCT_ONLY_RE = re.compile(r"^[`'\"“”‘’·,，:：;；。.!！？?、（）()<>《》【】\[\]{}\-—_+=/\\|~…]+$")
XUCI_LOOKBACK_PAGES = 12
XUCI_LOOKAHEAD_PAGES = 6
XUCI_EXCERPT_LIMIT = 3800
TEXTBOOK_TRIM_HINTS = (
    "单元学习任务",
    "学习提示",
    "思考与探究",
    "积累与运用",
    "研习任务",
    "练习",
)
MODERN_TEXTBOOK_HINTS = (
    "学习提示",
    "阅读提示",
    "本课",
    "课文",
    "阅读下面",
    "体会",
    "理解",
    "分析",
    "思考",
    "探究",
    "提示",
    "作者",
    "文章",
    "寓意",
    "形象",
    "结构",
    "逻辑",
    "风格",
    "写作",
    "注释",
    "教材",
    "这首诗",
    "全文",
    "一说",
    "古代诗文中",
    "值得我们",
    "大意",
    "代称",
    "感受",
    "意境",
    "情怀",
    "哲思",
    "这里",
    "意思是",
    "反衬",
    "说明",
)
TEXTBOOK_REJECT_HINTS = (
    "诵读这首诗",
    "感受诗人",
    "体会其中",
    "核心意象",
    "营造出",
    "空灵曼妙",
    "情怀和哲思",
    "句中处于相同位置上的词语",
    "意思相近",
    "语气助词",
    "词类",
    "用于句首",
    "用于句中",
    "用于句末",
    "表达判断",
    "强化语气",
    "接着、不久",
    "辅助太守",
    "即官宦",
    "承继祖辈的仕籍",
    "脱漏之句",
    "前例",
    "前者写于",
    "后者写于",
    "借古鉴今",
    "针砭时弊",
    "星宿名",
    "分别是",
    "称为分野",
    "指晋惠公",
    "修筑防御工事",
)
TEXTBOOK_REJECT_PATTERNS = (
    re.compile(r"^《.+》中有"),
    re.compile(r"^归园田居[（(]其一[）)]$"),
    re.compile(r"^因而"),
    re.compile(r"^句中"),
    re.compile(r"^诵读"),
    re.compile(r"[」”\"]指"),
    re.compile(r"[（(](?:今|古人)[^）)]{1,40}[）)]"),
)
MIN_CLASSIC_EXAMPLE_SCORE = 58
FALSE_POSITIVE_HEADWORD_COMPOUNDS = {
    "于": ("单于",),
}
XUCI_HEADWORD_ALIASES = {
    "于": ("千",),
}

_XUCI_LAYOUT_PAGES: list[str] | None = None


def _normalize_text(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "").replace("\r\n", "\n").replace("\r", "\n")


def _compact_text(text: str | None) -> str:
    return re.sub(r"\s+", "", _normalize_text(text))


def _clean_excerpt(text: str | None, limit: int = 420) -> str:
    collapsed = re.sub(r"\s+", " ", _normalize_text(text)).strip()
    return collapsed[:limit]


def _xuci_headword_tokens(headword: str) -> tuple[str, ...]:
    return (headword, *XUCI_HEADWORD_ALIASES.get(headword, ()))


def _is_pinyin_like_noise(text: str) -> bool:
    compact = _normalize_text(text).strip()
    if not compact:
        return False
    if PINYIN_RE.fullmatch(compact):
        return True
    han_count = len(re.findall(r"[\u4e00-\u9fff]", compact))
    latin_count = len(re.findall(r"[A-Za-z]", compact))
    digit_count = len(re.findall(r"\d", compact))
    punct_count = len(re.findall(r"[.'`·…\-_/]", compact))
    return han_count <= 1 and latin_count >= 3 and latin_count + digit_count + punct_count >= max(4, len(compact) - 2)


def _clean_xuci_text_fragment(text: str | None) -> str:
    cleaned = _normalize_text(text)
    cleaned = cleaned.replace("`", "").replace("〈", "《").replace("〉", "》")
    cleaned = cleaned.replace("<", "(").replace(">", ")").replace("{", "(").replace("}", ")")
    cleaned = cleaned.replace("<说文)", "(说文)").replace("<说文>", "(说文)")
    cleaned = cleaned.replace("{说文", "(说文").replace("（说文》", "（说文）")
    cleaned = cleaned.replace("..", ".").replace("''", '"')
    cleaned = re.sub(r"([A-Za-z]{1,6})\s+[A-Za-z]{1,3}\s+([（(<《])", r"\1 \2", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"([（(《])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([）)》])", r"\1", cleaned)
    cleaned = re.sub(r"([。；，、：！？])\s+", r"\1", cleaned)
    return cleaned.strip()


def _normalize_special_pattern(text: str, headword: str) -> str:
    cleaned = _clean_xuci_text_fragment(text)
    compact = re.sub(r"\s+", "", cleaned)
    compact = compact.replace("……", "......")
    compact = re.sub(r"[.·•]{2,}", "......", compact)
    compact = re.sub(r"([一-龥])(?:[.·•]|\.{1,6}|[-_/]){1,}([一-龥])", r"\1......\2", compact)
    compact = re.sub(rf"({re.escape(headword)})(?:[.·•]|\.{{1,6}})+", rf"\1......", compact)
    return compact or cleaned


def _is_low_signal_xuci_text(text: str | None, headword: str | None = None) -> bool:
    compact = _compact_text(text)
    if not compact:
        return True
    if "�" in compact or _is_pinyin_like_noise(compact):
        return True
    if re.search(r"[（(《][^）)》]{0,40}[）)》]", compact) and len(compact) <= 28:
        return True
    if headword and len(compact) <= 4 and headword not in compact:
        return True
    return False


def _has_meaningful_headword_occurrence(headword: str, text: str | None) -> bool:
    compact = _compact_text(text)
    if headword not in compact:
        return False
    for compound in FALSE_POSITIVE_HEADWORD_COMPOUNDS.get(headword, ()):
        compact = compact.replace(compound, "")
    return headword in compact


def _run(cmd: list[str], *, input_bytes: bytes | None = None) -> str:
    proc = subprocess.run(
        cmd,
        input=input_bytes,
        check=True,
        capture_output=True,
        text=input_bytes is None,
    )
    if input_bytes is None:
        return proc.stdout
    return proc.stdout.decode("utf-8", errors="replace")


def _load_exam_terms() -> list[str]:
    payload = json.loads(EXAM_XUCI_PATH.read_text(encoding="utf-8"))
    terms = payload.get("terms") if isinstance(payload, dict) else None
    if not isinstance(terms, list):
        return []
    return [str(item.get("headword", "")).strip() for item in terms if str(item.get("headword", "")).strip()]


def _load_headword_index() -> dict:
    payload = json.loads(HEADWORD_INDEX_PATH.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _load_textbook_manifest() -> dict:
    payload = json.loads(TEXTBOOK_MANIFEST_PATH.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _load_xuci_layout_pages() -> list[str]:
    global _XUCI_LAYOUT_PAGES
    if _XUCI_LAYOUT_PAGES is not None:
        return _XUCI_LAYOUT_PAGES
    raw_text = _run(["pdftotext", "-layout", str(XUCI_PDF), "-"])
    pages = [_normalize_text(chunk) for chunk in raw_text.split("\f")]
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    _XUCI_LAYOUT_PAGES = pages
    return pages


def _find_xuci_layout_start_page(headword: str) -> int | None:
    best_score = -999
    best_page: int | None = None
    pages = _load_xuci_layout_pages()
    head_tokens = _xuci_headword_tokens(headword)
    head_pattern = re.compile(
        rf"^(?:{'|'.join(re.escape(token) for token in head_tokens)})(?:[（(][^）)\n]{{0,12}}[）)])?(?:\s*[A-Za-z][A-Za-z0-9;,' -]{{0,24}})?$"
    )
    for page_number, page_text in enumerate(pages, start=1):
        lines = page_text.splitlines()
        for index, raw_line in enumerate(lines):
            line = raw_line.strip()
            if not line or headword not in line or len(line) > 28:
                continue
            if head_pattern.fullmatch(line):
                score = 10
            elif line == headword:
                score = 9
            else:
                continue

            next_line = ""
            next_nonempty_index = index + 1
            while next_nonempty_index < len(lines):
                candidate = lines[next_nonempty_index].strip()
                if candidate:
                    next_line = candidate
                    break
                next_nonempty_index += 1
            lookahead = "\n".join(lines[index : min(len(lines), index + 20)])
            if next_line and PINYIN_RE.fullmatch(next_line):
                score += 4
            if re.search(r"(说文|虚词|惯用词组|固定格式|复合虚词)", lookahead):
                score += 4
            if any(category in lookahead for category in XUCI_USAGE_HEADERS):
                score += 3

            if score > best_score:
                best_score = score
                best_page = page_number
    return best_page


def _find_manifest_hits(manifest: dict, book_key: str | None, logical_page: int | None) -> list[dict]:
    if not book_key or logical_page is None:
        return []
    ranges = manifest.get(book_key)
    if not isinstance(ranges, list):
        return []
    hits = []
    for item in ranges:
        if not isinstance(item, dict):
            continue
        start = int(item.get("page_start") or -1)
        end = int(item.get("page_end") or start)
        if start <= logical_page <= end:
            hits.append(item)
    hits.sort(key=lambda item: (int(item.get("page_start") or 0), int(item.get("page_end") or 0)))
    return hits


def _clip_textbook_text(text: str | None, manifest_hit: dict | None = None) -> str:
    clipped = _normalize_text(text).strip()
    if not clipped:
        return ""
    start_index = 0
    if manifest_hit:
        start_marker = str(manifest_hit.get("start_marker") or "").strip() or str(manifest_hit.get("title") or "").strip()
        if start_marker:
            marker_index = clipped.find(start_marker)
            if marker_index >= 0:
                start_index = marker_index
        end_marker = str(manifest_hit.get("end_marker") or "").strip()
        if end_marker:
            marker_index = clipped.find(end_marker, start_index + 1)
            if marker_index > start_index:
                clipped = clipped[start_index:marker_index]
                start_index = 0
    if start_index > 0:
        clipped = clipped[start_index:]
    trim_points = [clipped.find(hint) for hint in TEXTBOOK_TRIM_HINTS if clipped.find(hint) > 24]
    if trim_points:
        clipped = clipped[:min(trim_points)]
    return clipped.strip()


def _context_snippet(text: str, needle: str, *, width: int = 64) -> str:
    normalized = re.sub(r"\s+", "", text)
    compact_needle = re.sub(r"\s+", "", needle)
    if compact_needle and compact_needle in normalized:
        # Map compact index back to raw-ish text using a simple forward scan.
        compact_index = normalized.find(compact_needle)
        seen = 0
        raw_start = 0
        raw_end = len(text)
        for index, ch in enumerate(text):
            if ch.isspace():
                continue
            if seen == compact_index:
                raw_start = max(0, index - width)
                break
            seen += 1
        seen = 0
        target_end = compact_index + len(compact_needle)
        for index, ch in enumerate(text):
            if ch.isspace():
                continue
            seen += 1
            if seen >= target_end:
                raw_end = min(len(text), index + width)
                break
        return _clean_excerpt(text[raw_start:raw_end], limit=220)
    return _clean_excerpt(text[: width * 2], limit=220)


def _iter_sentence_candidates(text: str) -> list[str]:
    normalized = _normalize_text(text)
    candidates: list[str] = []
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if len(line) <= 2:
            continue
        pieces = re.split(r"(?<=[。！？；])", line)
        for piece in pieces:
            cleaned = piece.strip()
            if cleaned:
                candidates.append(cleaned)
    return candidates


def _clean_textbook_sentence(sentence: str, headword: str | None = None) -> str:
    cleaned = _normalize_text(sentence)
    bracket_match = re.search(r"[〔【\[]\s*([^\]】〕]{4,80})[\]】〕]?", cleaned)
    if bracket_match:
        bracket_text = bracket_match.group(1).strip()
        if bracket_text and (headword is None or headword in bracket_text):
            cleaned = bracket_text
    cleaned = re.sub(r"\$\\textcircled\{[^}]+\}\$", "", cleaned)
    cleaned = re.sub(r"〔[^〕]{0,48}〕", "", cleaned)
    cleaned = re.sub(r"[①②③④⑤⑥⑦⑧⑨⑩@]", "", cleaned)
    cleaned = re.sub(r"^[^。！？；]{0,20}》[,，:\s]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" ，,;；:：")


def _is_title_like_example(sentence: str, title: str | None) -> bool:
    if not title:
        return False

    def _strip_title_marks(value: str) -> str:
        compact = _compact_text(value)
        return re.sub(r"[《》“”\"'`·,，。！？!?：:;；（）()\[\]{}]", "", compact)

    clean_sentence = _strip_title_marks(sentence)
    clean_title = _strip_title_marks(title)
    return bool(clean_sentence and clean_title and clean_sentence == clean_title)


def _is_probably_commentary_sentence(headword: str, sentence: str, title: str | None = None) -> bool:
    if not _has_meaningful_headword_occurrence(headword, sentence):
        return True
    if title and _is_title_like_example(sentence, title):
        return True
    if any(pattern.search(sentence) for pattern in TEXTBOOK_REJECT_PATTERNS):
        return True
    if any(hint in sentence for hint in TEXTBOOK_REJECT_HINTS):
        return True
    if "《" in sentence and "》" in sentence and any(hint in sentence for hint in ("中有", "之语")):
        return True
    return False


def _score_classic_candidate(headword: str, sentence: str, title: str | None = None) -> int:
    sentence = _clean_textbook_sentence(sentence, headword)
    compact = _compact_text(sentence)
    if not _has_meaningful_headword_occurrence(headword, sentence):
        return -10_000
    if _is_probably_commentary_sentence(headword, sentence, title):
        return -800
    score = 0
    han_count = len(re.findall(r"[\u4e00-\u9fff]", sentence))
    score += min(han_count, 48)
    if 8 <= han_count <= 44:
        score += 40
    elif han_count < 6:
        score -= 40
    else:
        score -= min(30, han_count - 44)
    modern_hits = sum(1 for hint in MODERN_TEXTBOOK_HINTS if hint in sentence)
    if modern_hits:
        score -= 50 * modern_hits
    if any(marker in sentence for marker in ("〔", "〕", "【", "】", "[", "]", "$\\textcircled")):
        score -= 35
    if re.search(r"[A-Za-z0-9$\\\\]", sentence):
        score -= 20
    if any(marker in sentence for marker in ("曰", "矣", "焉", "乎", "哉", "兮", "也", "者")):
        score += 18
    if sentence.endswith(("。", "！", "？", "；")):
        score += 8
    head_index = compact.find(headword)
    if 0 <= head_index <= 2:
        score += 6
    if compact.endswith((headword, f"{headword}也", f"{headword}矣", f"{headword}乎")):
        score += 10
    if len(compact) > 80:
        score -= 20
    score -= 8 * len(re.findall(r"[的了呢吗吧着]", sentence))
    return score


def _extract_best_textbook_sentence(text: str, headword: str, title: str | None = None) -> tuple[str, int]:
    candidates = [_clean_textbook_sentence(item, headword) for item in _iter_sentence_candidates(text)]
    candidates = [item for item in candidates if item]
    if not candidates:
        fallback = _context_snippet(text, headword)
        fallback_score = _score_classic_candidate(headword, fallback, title)
        return (fallback, fallback_score) if fallback_score >= MIN_CLASSIC_EXAMPLE_SCORE else ("", -10_000)

    scored = sorted(
        ((_score_classic_candidate(headword, candidate, title), candidate) for candidate in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best_sentence = scored[0]
    if best_score < MIN_CLASSIC_EXAMPLE_SCORE:
        return "", best_score
    if len(best_sentence) > 72:
        best_sentence = _context_snippet(best_sentence, headword, width=36)
    return best_sentence, best_score


def _best_hit_snippet(
    con: sqlite3.Connection,
    book_key: str | None,
    manifest_hit: dict,
    headword: str,
    title: str | None = None,
) -> tuple[str, int | None, int]:
    if not book_key:
        return "", None, -10_000
    start = int(manifest_hit.get("page_start") or -1)
    end = int(manifest_hit.get("page_end") or start)
    if start <= 0 or end < start:
        return "", None, -10_000

    rows = con.execute(
        """
        SELECT id, section, logical_page, text
        FROM chunks
        WHERE source = 'mineru'
          AND subject = '语文'
          AND book_key = ?
          AND COALESCE(logical_page, section) BETWEEN ? AND ?
        ORDER BY COALESCE(logical_page, section), id
        LIMIT 24
        """,
        (book_key, start, end),
    ).fetchall()

    best_score = -10_000
    best_snippet = ""
    best_page: int | None = None
    for row in rows:
        clipped = _clip_textbook_text(row["text"], manifest_hit)
        compact = _compact_text(clipped)
        if headword not in compact:
            continue
        snippet, score = _extract_best_textbook_sentence(clipped, headword, title)
        if score > best_score:
            best_score = score
            best_snippet = snippet
            best_page = row["logical_page"] if row["logical_page"] is not None else row["section"]
    if best_score < MIN_CLASSIC_EXAMPLE_SCORE:
        return "", None, best_score
    return best_snippet, best_page, best_score


def _get_headword_entry(headword_index: dict, headword: str, source: str) -> dict | None:
    entries = headword_index.get("entries", {}).get(headword)
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("dict_source") == source:
            return entry
    return None


def _iter_lines_with_offsets(text: str) -> list[tuple[int, str]]:
    offset = 0
    rows: list[tuple[int, str]] = []
    for raw_line in text.splitlines(keepends=True):
        rows.append((offset, raw_line.rstrip("\n")))
        offset += len(raw_line)
    if text and not rows:
        rows.append((0, text))
    return rows


def _next_nonempty_line(lines: list[tuple[int, str]], index: int, *, limit: int = 5) -> str:
    for next_index in range(index + 1, min(len(lines), index + 1 + limit)):
        candidate = lines[next_index][1].strip()
        if candidate:
            return candidate
    return ""


def _is_punct_only(text: str) -> bool:
    compact = text.strip()
    if not compact:
        return True
    return bool(PUNCT_ONLY_RE.fullmatch(compact))


def _locate_xuci_anchor(text: str, headword: str) -> int | None:
    lines = _iter_lines_with_offsets(text)
    best_score = -999
    best_offset: int | None = None
    head_tokens = _xuci_headword_tokens(headword)
    head_pattern = re.compile(
        rf"^(?:{'|'.join(re.escape(token) for token in head_tokens)})(?:[（(][^）)\n]{{0,12}}[）)])?(?:\s*[A-Za-z][A-Za-z0-9;,' -]{{0,24}})?$"
    )

    for index, (offset, raw_line) in enumerate(lines):
        line = raw_line.strip()
        if not line or len(line) > 40 or not any(token in line for token in head_tokens):
            continue

        if head_pattern.fullmatch(line):
            score = 9
        elif line == headword:
            score = 4
        elif line.startswith(headword) and len(line) <= 10 and not re.match(
            rf"^{re.escape(headword)}[\u4e00-\u9fff]",
            line,
        ):
            score = 2
        else:
            continue

        next_line = _next_nonempty_line(lines, index)
        window = text[offset : offset + 360]
        if line != headword:
            score -= 1
        if PINYIN_RE.fullmatch(next_line):
            score += 3
        if re.fullmatch(r"\d{1,4}", next_line):
            score -= 4
        if any(marker in window for marker in ("(说文", "（说文", "<说文", "{说文")):
            score += 3
        if any(
            marker in window
            for marker in (
                f"虚词 {headword}",
                f"{headword} 与本义无关",
                f"{headword} 是假借字",
                f"{headword} 可作",
            )
        ):
            score += 3
        if any(category in window for category in XUCI_USAGE_HEADERS):
            score += 2
        if next_line and XUCI_HEAD_RE.fullmatch(next_line) and next_line != headword:
            score -= 2

        if score > best_score:
            best_score = score
            best_offset = offset

    if best_offset is not None and best_score >= 4:
        return best_offset

    for marker in (f"虚词 {headword}", f"{headword} 与本义无关", f"{headword} 是假借字", f"{headword} 可作"):
        marker_index = text.find(marker)
        if marker_index < 0:
            continue
        line_index = text.rfind(f"\n{headword}\n", max(0, marker_index - 240), marker_index + len(marker) + 8)
        if line_index >= 0:
            return line_index + 1
        return max(0, marker_index - 40)

    return None


def _page_index_for_offset(pages: list[str], offset: int) -> int:
    if offset <= 0 or not pages:
        return 0
    cursor = 0
    for index, chunk in enumerate(pages):
        chunk_end = cursor + len(chunk)
        if offset <= chunk_end:
            return index
        cursor = chunk_end + 1
    return len(pages) - 1


def _excerpt_limit_for_pages(page_count: int) -> int:
    clean_page_count = max(1, int(page_count or 1))
    return min(24000, max(XUCI_EXCERPT_LIMIT, 2600 * clean_page_count))


def _find_next_xuci_entry_offset(text: str, start_offset: int, current_headword: str) -> int | None:
    lines = _iter_lines_with_offsets(text)
    head_pattern = re.compile(
        r"^[\u4e00-\u9fff]{1,6}(?:[（(][^）)\n]{0,12}[）)])?(?:\s*[A-Za-z][A-Za-z0-9;,' -]{0,24})?$"
    )
    for index, (offset, raw_line) in enumerate(lines):
        if offset <= start_offset + 600:
            continue
        line = raw_line.strip()
        if not line or len(line) > 40:
            continue
        if not head_pattern.fullmatch(line):
            continue
        compact_line = re.sub(r"\s+", "", line)
        if current_headword and compact_line.startswith(current_headword):
            continue
        next_line = _next_nonempty_line(lines, index)
        window = text[offset : offset + 320]
        has_head_marker = any(marker in window for marker in ("(说文", "（说文", "<说文", "{说文", "虚词"))
        has_pinyin = bool(PINYIN_RE.fullmatch(next_line)) or bool(re.search(r"\s+[A-Za-z][A-Za-z0-9;,' -]{0,24}$", line))
        if has_head_marker and has_pinyin:
            return offset
    return None


def _clean_xuci_excerpt(text: str, *, limit: int = XUCI_EXCERPT_LIMIT) -> str:
    lines: list[str] = []
    blank_run = 0
    for raw_line in _normalize_text(text).replace("\f", "\n").splitlines():
        line = raw_line.strip()
        if re.fullmatch(r"\d{1,4}", line):
            continue
        if _is_punct_only(line):
            continue
        if re.fullmatch(r"[A-Za-z.·…\-]{1,12}", line) and not PINYIN_RE.fullmatch(line):
            continue
        if not line:
            if blank_run == 0 and lines:
                lines.append("")
            blank_run += 1
            continue
        blank_run = 0
        lines.append(line)
    return "\n".join(lines).strip()[:limit]


def _normalize_xuci_headword_lead(headword: str, excerpt: str) -> str:
    lines = excerpt.splitlines()
    if not lines:
        return excerpt
    aliases = XUCI_HEADWORD_ALIASES.get(headword, ())
    for index, raw_line in enumerate(lines[:3]):
        stripped = raw_line.strip()
        for alias in aliases:
            if stripped == alias:
                lines[index] = raw_line.replace(alias, headword, 1)
                return "\n".join(lines)
            if stripped.startswith(f"{alias} "):
                lines[index] = raw_line.replace(alias, headword, 1)
                return "\n".join(lines)
    return excerpt


def _prune_xuci_noise_lines(headword: str, excerpt: str) -> str:
    head_tokens = _xuci_headword_tokens(headword)
    cleaned_lines: list[str] = []
    for raw_line in excerpt.splitlines():
        line = _clean_xuci_text_fragment(raw_line)
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        if "�" in line:
            continue
        if _is_pinyin_like_noise(line) and not any(token in line for token in head_tokens):
            continue
        compact = _compact_text(line)
        if (
            len(compact) <= 2
            and line not in XUCI_USAGE_HEADERS
            and not PINYIN_RE.fullmatch(line)
            and not any(token in line for token in head_tokens)
        ):
            continue
        cleaned_lines.append(line)
    while cleaned_lines and not cleaned_lines[-1]:
        cleaned_lines.pop()
    return "\n".join(cleaned_lines)


def _build_xuci_overview(headword: str, excerpt: str, sections: list[dict] | None = None) -> str:
    usage_labels: list[str] = []
    special_labels: list[str] = []
    for section in sections or []:
        usage = str(section.get("usage") or "").strip()
        summary = str(section.get("summary") or "").strip()
        if usage in XUCI_SPECIAL_HEADERS:
            senses = section.get("senses") or []
            special_label = str((senses[0] or {}).get("label") or summary or "").strip() if senses else summary
            if special_label and special_label not in special_labels:
                special_labels.append(special_label)
            continue
        if usage and usage not in usage_labels:
            usage_labels.append(usage)
    parts = []
    if usage_labels:
        parts.append(f"可用作{'、'.join(usage_labels[:5])}")
    if special_labels:
        parts.append(f"常见{('、'.join(special_labels[:3]))}")
    if not parts:
        fallback_lines: list[str] = []
        head_tokens = _xuci_headword_tokens(headword)
        for raw_line in excerpt.splitlines():
            line = _clean_xuci_text_fragment(raw_line)
            if not line:
                continue
            if line in XUCI_USAGE_HEADERS:
                break
            if _is_punct_only(line) or _is_pinyin_like_noise(line):
                continue
            if any(line == token or line.startswith(token) for token in head_tokens):
                continue
            fallback_lines.append(line)
            if len(" ".join(fallback_lines)) >= 100:
                break
        fallback = _clean_xuci_text_fragment(_clean_excerpt(" ".join(fallback_lines), limit=160))
        return f"{headword}。{fallback}" if fallback else headword
    return f"{headword}。{'；'.join(parts)}。"


def _first_meaningful_line(text: str) -> str:
    for raw_line in _normalize_text(text).splitlines():
        line = raw_line.strip()
        if line:
            return line
    return ""


def _looks_like_good_xuci_excerpt(headword: str, excerpt: str, sections: list[dict]) -> bool:
    first_line = _first_meaningful_line(excerpt)
    head_tokens = _xuci_headword_tokens(headword)
    if not first_line:
        return False
    if first_line in head_tokens:
        return True
    if any(
        first_line.startswith(token) and not re.match(rf"^{re.escape(token)}[\u4e00-\u9fff]", first_line)
        for token in head_tokens
    ):
        return True
    return bool(sections) and headword in excerpt[:80]


def _extract_xuci_entry_text_from_pages(headword: str, page_numbers: list[int]) -> dict:
    if not page_numbers:
        return {"pages": [], "excerpt": "", "overview": "", "outline": [], "sections": [], "mindmap": {"label": headword, "children": []}}
    start_page = min(page_numbers)
    end_page = max(page_numbers)
    search_start_page = max(1, start_page - XUCI_LOOKBACK_PAGES)
    search_end_page = end_page + XUCI_LOOKAHEAD_PAGES
    raw_text = _run(["pdftotext", "-f", str(search_start_page), "-l", str(search_end_page), str(XUCI_PDF), "-"])
    page_chunks = [_normalize_text(chunk) for chunk in raw_text.split("\f")]
    if page_chunks and not page_chunks[-1].strip():
        page_chunks = page_chunks[:-1]
    text = "\f".join(page_chunks)

    anchor = _locate_xuci_anchor(text, headword)
    if anchor is None:
        anchor = 0
    excerpt_limit = _excerpt_limit_for_pages(len(page_numbers))
    next_entry_offset = _find_next_xuci_entry_offset(text, anchor, headword)
    excerpt_end = min(
        len(text),
        next_entry_offset if next_entry_offset is not None else anchor + excerpt_limit,
    )
    excerpt = _clean_xuci_excerpt(text[anchor:excerpt_end], limit=excerpt_limit)
    excerpt = _prune_xuci_noise_lines(headword, excerpt)
    excerpt = _normalize_xuci_headword_lead(headword, excerpt)
    sections = _parse_xuci_sections(headword, excerpt)
    overview = _build_xuci_overview(headword, excerpt, sections)
    outline: list[str] = []
    for section in sections:
        usage = str(section.get("usage") or "").strip()
        if usage and usage not in outline:
            outline.append(usage)
        for sense in section.get("senses") or []:
            label = str(sense.get("label") or "").strip()
            if label:
                outline.append(label[:40])
            if len(outline) >= 12:
                break
        if len(outline) >= 12:
            break

    pages = sorted(set(page_numbers))
    anchor_page = search_start_page + _page_index_for_offset(page_chunks, anchor)
    excerpt_end_page = search_start_page + _page_index_for_offset(page_chunks, excerpt_end)
    if pages:
        page_start = min(anchor_page, min(pages))
        page_end = max(page_start, min(excerpt_end_page, search_end_page))
        pages = list(range(page_start, page_end + 1))

    return {
        "pages": pages,
        "excerpt": excerpt,
        "overview": overview,
        "outline": outline,
        "sections": sections,
        "mindmap": _build_xuci_mindmap(headword, sections),
    }


def _extract_xuci_entry_text(headword: str, page_numbers: list[int]) -> dict:
    detail = _extract_xuci_entry_text_from_pages(headword, page_numbers)
    if _looks_like_good_xuci_excerpt(headword, detail.get("excerpt", ""), detail.get("sections") or []):
        return detail

    fallback_start_page = _find_xuci_layout_start_page(headword)
    if fallback_start_page is None:
        return detail

    fallback_pages = [fallback_start_page, fallback_start_page + 1, fallback_start_page + 2]
    fallback_detail = _extract_xuci_entry_text_from_pages(headword, fallback_pages)
    if _looks_like_good_xuci_excerpt(headword, fallback_detail.get("excerpt", ""), fallback_detail.get("sections") or []):
        return fallback_detail
    return detail


def _clean_section_line(line: str) -> str:
    cleaned = _clean_xuci_text_fragment(_clean_excerpt(line, limit=120))
    cleaned = cleaned.strip(" .-·,，:：;；")
    if not cleaned or _is_punct_only(cleaned):
        return ""
    if len(cleaned) <= 1 and not re.search(r"[A-Za-z0-9]", cleaned):
        return ""
    if re.fullmatch(r"([\u4e00-\u9fff])\1", cleaned):
        return ""
    if _is_pinyin_like_noise(cleaned):
        return ""
    return cleaned


def _next_nonempty_excerpt_line(lines: list[str], index: int, *, limit: int = 4) -> str:
    for next_index in range(index + 1, min(len(lines), index + 1 + limit)):
        candidate = lines[next_index].strip()
        if candidate:
            return candidate
    return ""


def _nearby_special_header(lines: list[str], index: int, *, limit: int = 12) -> str:
    for next_index in range(index + 1, min(len(lines), index + 1 + limit)):
        candidate = lines[next_index].strip()
        if candidate in XUCI_SPECIAL_HEADERS:
            return candidate
    return ""


def _parse_xuci_sections(headword: str, excerpt: str) -> list[dict]:
    sections: list[dict] = []
    current_section: dict | None = None
    current_sense: dict | None = None
    pending_special_title = ""
    lines = excerpt.splitlines()
    head_tokens = _xuci_headword_tokens(headword)
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if line == headword or re.fullmatch(r"[A-Za-z0-9 -]{1,16}", line):
            continue
        if re.fullmatch(r"\d{1,4}", line):
            continue
        next_line = _next_nonempty_excerpt_line(lines, index)
        nearby_special_header = _nearby_special_header(lines, index)
        cleaned = _clean_section_line(line)
        if (
            cleaned
            and nearby_special_header
            and len(cleaned) <= 36
            and any(token in cleaned for token in head_tokens)
            and not re.search(r"[，,。；;：:！？?]", cleaned)
            and not XUCI_OUTLINE_RE.match(cleaned)
        ):
            pending_special_title = cleaned
            continue
        if line in XUCI_USAGE_HEADERS:
            current_section = {
                "usage": line,
                "summary": "",
                "senses": [],
            }
            sections.append(current_section)
            current_sense = None
            if pending_special_title and line in XUCI_SPECIAL_HEADERS:
                current_section["summary"] = pending_special_title[:90]
                current_sense = {"label": pending_special_title[:48], "summary": ""}
                current_section["senses"].append(current_sense)
            pending_special_title = ""
            continue
        if XUCI_OUTLINE_RE.match(line):
            if current_section is None:
                current_section = {"usage": "未归类", "summary": "", "senses": []}
                sections.append(current_section)
            current_sense = {"label": line[:48], "summary": ""}
            current_section["senses"].append(current_sense)
            continue
        if re.match(r"^[（(][0-9A-Za-z一二三四五六七八九十]+[)）]", line):
            continue
        if re.match(r"^[•.．]\s*[（(]?[0-9A-Za-z一二三四五六七八九十]+[)）]", line):
            continue
        if not cleaned:
            continue
        if current_sense is not None and not current_sense["summary"]:
            current_sense["summary"] = cleaned[:90]
            continue
        if current_section is not None and not current_section["summary"]:
            current_section["summary"] = cleaned[:90]
    normalized_sections: list[dict] = []
    for section in sections[:12]:
        usage = _clean_xuci_text_fragment(section.get("usage") or "")
        summary = _clean_xuci_text_fragment(section.get("summary") or "")
        senses: list[dict] = []
        for sense in section.get("senses") or []:
            label = _clean_xuci_text_fragment(sense.get("label") or "")
            sense_summary = _clean_xuci_text_fragment(sense.get("summary") or "")
            if usage in XUCI_SPECIAL_HEADERS:
                label = _normalize_special_pattern(label, headword)
            if not label or _is_low_signal_xuci_text(label, headword):
                continue
            if _is_low_signal_xuci_text(sense_summary, headword):
                sense_summary = ""
            senses.append({"label": label[:48], "summary": sense_summary[:90]})

        if usage in XUCI_SPECIAL_HEADERS:
            summary = _normalize_special_pattern(summary, headword)
        if _is_low_signal_xuci_text(summary, headword):
            summary = ""
        if usage in XUCI_SPECIAL_HEADERS and senses:
            if not summary:
                summary = senses[0]["label"]
            if summary == senses[0]["label"] and senses[0]["summary"]:
                summary = senses[0]["summary"]
        elif not summary and senses and senses[0]["summary"]:
            summary = senses[0]["summary"]
        if usage in XUCI_SPECIAL_HEADERS and not summary and not senses:
            continue

        normalized_sections.append(
            {
                "usage": usage or "未归类",
                "summary": summary[:90],
                "senses": senses[:8],
            }
        )
    return normalized_sections


def _build_xuci_mindmap(headword: str, sections: list[dict]) -> dict:
    children = []
    for section in sections:
        usage = str(section.get("usage") or "").strip()
        if not usage:
            continue
        section_children = []
        for sense in section.get("senses") or []:
            label = str(sense.get("label") or "").strip()
            summary = str(sense.get("summary") or "").strip()
            if not label:
                continue
            section_children.append(
                {
                    "label": label,
                    "summary": summary,
                }
            )
        children.append(
            {
                "label": usage,
                "summary": str(section.get("summary") or "").strip(),
                "children": section_children[:8],
            }
        )
    return {"label": headword, "children": children[:8]}


def _render_pdf_page(pdf_path: Path, page: int, prefix: str) -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    image_path = TMP_ROOT / f"{prefix}_{page}.png"
    if image_path.exists() and image_path.stat().st_size > 0:
        return image_path
    subprocess.run(
        [
            "pdftoppm",
            "-f",
            str(page),
            "-l",
            str(page),
            "-r",
            "300",
            "-png",
            "-singlefile",
            str(pdf_path),
            str(image_path.with_suffix("")),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return image_path


def _ocr_png(path: Path) -> str:
    cache_path = path.with_suffix(".ocr.txt")
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return _normalize_text(cache_path.read_text(encoding="utf-8"))
    raw = path.read_bytes()
    text = _normalize_text(
        _run(
            ["tesseract", "stdin", "stdout", "-l", "chi_sim", "--psm", "6"],
            input_bytes=raw,
        )
    )
    cache_path.write_text(text, encoding="utf-8")
    return text


def _extract_changyong_entry_text(headword: str, page_numbers: list[int]) -> dict:
    if not page_numbers or len(page_numbers) > 4:
        return {"pages": sorted(set(page_numbers)), "excerpt": "", "skipped": len(page_numbers) > 4}
    page = min(page_numbers)
    image_path = _render_pdf_page(CHANGYONG_PDF, page, "changyong")
    text = _ocr_png(image_path)
    if not text:
        return {"pages": [page], "excerpt": "", "skipped": False}
    compact_text = re.sub(r"\s+", "", text)
    compact_headword = re.sub(r"\s+", "", headword)
    index = compact_text.find(compact_headword) if compact_headword else -1
    if index >= 0:
        excerpt = _context_snippet(text, headword, width=180)
    else:
        excerpt = _clean_excerpt(text[:1400], limit=900)
    return {"pages": [page], "excerpt": excerpt, "skipped": False}


def _load_textbook_examples(headword: str, manifest: dict) -> list[dict]:
    if not TEXTBOOK_DB_PATH.exists():
        return []
    con = sqlite3.connect(str(TEXTBOOK_DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, title, book_key, section, logical_page, text
            FROM chunks
            WHERE source = 'mineru'
              AND subject = '语文'
              AND text LIKE ?
            ORDER BY
                CASE
                    WHEN INSTR(text, ?) > 0 THEN INSTR(text, ?)
                    ELSE 999999
                END,
                COALESCE(logical_page, section),
                id
            LIMIT 320
            """,
            (f"%{headword}%", headword, headword),
        ).fetchall()
        candidates: list[dict] = []
        seen_examples: set[tuple[str, str]] = set()
        normalized_headword = _compact_text(headword)
        for row in rows:
            logical_page = row["logical_page"] if row["logical_page"] is not None else row["section"]
            hits = _find_manifest_hits(manifest, row["book_key"], logical_page)
            if not hits:
                continue
            for manifest_hit in hits:
                title = str(manifest_hit.get("title") or row["title"] or "").strip()
                snippet, snippet_page, score = _best_hit_snippet(con, row["book_key"], manifest_hit, headword, title)
                if not snippet:
                    clipped = _clip_textbook_text(row["text"], manifest_hit)
                    compact = _compact_text(clipped)
                    if normalized_headword not in compact:
                        continue
                    snippet, score = _extract_best_textbook_sentence(clipped, headword, title)
                    snippet_page = logical_page
                if score < MIN_CLASSIC_EXAMPLE_SCORE or _is_probably_commentary_sentence(headword, snippet, title):
                    continue
                dedupe_key = (title, snippet)
                if not title or not snippet or dedupe_key in seen_examples:
                    continue
                seen_examples.add(dedupe_key)
                candidates.append(
                    {
                        "title": title,
                        "kind": str(manifest_hit.get("kind") or "").strip(),
                        "book_key": row["book_key"],
                        "logical_page": snippet_page if snippet_page is not None else logical_page,
                        "snippet": snippet,
                        "_score": score,
                    }
                )
        examples: list[dict] = []
        seen_titles: set[str] = set()
        for item in sorted(
            candidates,
            key=lambda candidate: (
                -int(candidate.get("_score") or 0),
                int(candidate.get("logical_page") or 0),
                str(candidate.get("title") or ""),
            ),
        ):
            title = str(item.get("title") or "").strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            clean_item = dict(item)
            clean_item.pop("_score", None)
            examples.append(clean_item)
            if len(examples) >= 6:
                break
        return examples
    finally:
        con.close()


def build_payload() -> dict:
    terms = _load_exam_terms()
    headword_index = _load_headword_index()
    textbook_manifest = _load_textbook_manifest()

    payload_terms: dict[str, dict] = {}
    for headword in terms:
        xuci_entry = _get_headword_entry(headword_index, headword, "xuci") or {}
        changyong_entry = _get_headword_entry(headword_index, headword, "changyong") or {}

        xuci_pages = list(xuci_entry.get("page_numbers") or [])
        changyong_pages = list(changyong_entry.get("page_numbers") or [])

        xuci_detail = _extract_xuci_entry_text(headword, xuci_pages) if xuci_pages else {"pages": [], "excerpt": "", "outline": []}
        changyong_detail = _extract_changyong_entry_text(headword, changyong_pages) if changyong_pages else {"pages": [], "excerpt": "", "skipped": False}
        textbook_examples = _load_textbook_examples(headword, textbook_manifest)

        payload_terms[headword] = {
            "headword": headword,
            "xuci_dict": {
                "pages": xuci_detail.get("pages", []),
                "excerpt": xuci_detail.get("excerpt", ""),
                "overview": xuci_detail.get("overview", ""),
                "outline": xuci_detail.get("outline", []),
                "sections": xuci_detail.get("sections", []),
                "mindmap": xuci_detail.get("mindmap", {"label": headword, "children": []}),
            },
            "changyong_dict": {
                "pages": changyong_detail.get("pages", []),
                "excerpt": changyong_detail.get("excerpt", ""),
                "skipped": bool(changyong_detail.get("skipped")),
            },
            "textbook_examples": textbook_examples,
        }

    return {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "term_count": len(payload_terms),
        "terms": payload_terms,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Print payload to stdout")
    args = parser.parse_args()

    payload = build_payload()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUTPUT_PATH}")
    print(f"term_count: {payload['term_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
