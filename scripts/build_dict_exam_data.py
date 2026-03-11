#!/Users/ylsuen/.venv/bin/python
"""Build runtime exam-term data for the dictionary page's added exam modes.

Current scope:
- Build Beijing classical-Chinese xuci / shici datasets from gaokao_chunks.jsonl
- Build standardized national classical-Chinese xuci / shici datasets from GAOKAO-Bench

Output:
- data/index/dict_exam_xuci.json
- data/index/dict_exam_shici.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_INDEX = REPO_ROOT / "data" / "index"

GAOKAO_CHUNKS_PATH = DATA_INDEX / "gaokao_chunks.jsonl"
NATIONAL_RAW_PATH = (
    REPO_ROOT
    / "data"
    / "gaokao_raw"
    / "GAOKAO-Bench"
    / "Data"
    / "Subjective_Questions"
    / "2010-2022_Chinese_Language_Classical_Chinese_Reading.json"
)

XUCI_OUTPUT_PATH = DATA_INDEX / "dict_exam_xuci.json"
SHICI_OUTPUT_PATH = DATA_INDEX / "dict_exam_shici.json"

QUESTION_SPLIT_RE = re.compile(r"(?:^|\n)\s*(?:[（(]\s*)?(\d+)\s*(?:[)）]|[\.．])\s*", re.M)
OPTION_SPLIT_RE = re.compile(r"([A-D])[\.．]\s*")
ITEM_SPLIT_RE = re.compile(
    r"([①②③④⑤⑥⑦⑧⑨⑩])\s*(.*?)(?=(?:[①②③④⑤⑥⑦⑧⑨⑩]\s*)|(?:[A-D][\.．]\s*)|$)",
    re.S,
)
STAR_TOKEN_RE = re.compile(r"\*([^*]{1,4})\*")
DOT_TOKEN_RE = re.compile(r"([\u4e00-\u9fff]{1,4})[．·•]")
QUOTED_HEADWORD_RE = re.compile(r"[“\"]([\u4e00-\u9fff]{1,4})[”\"]字的解释")
GLOSS_RE = re.compile(r"([\u4e00-\u9fff]{1,4})\s*[：:]\s*(.+)")

XUCI_SUBTYPE_SAME_RE = re.compile(r"意义和用法.{0,6}(都相同|相同)")
XUCI_SUBTYPE_DIFF_RE = re.compile(r"意义和用法.{0,6}(不同|不相同)")
EXPLANATION_RE = re.compile(r"加点词(?:语)?(?:的|语的)解释|加点词语的解说")
NATIONAL_LEXICAL_PROMPT_RE = re.compile(
    r"(?:对下列|下列)句子中加点(?:的)?词(?:语)?(?:的)?解释"
)
TRANSLATION_PROMPT_RE = re.compile(
    r"(?:把|将)文中画(?:横线|波浪线|线)(?:的)?句子翻译成现代汉语|翻译文中画(?:横线|波浪线|线)(?:的)?句子"
)
SCORE_MARKER_RE = re.compile(
    r"(?:第[一二三四五六七八九十0-9]+题)?(?:得分点|要点|注意以下关键词)[：:]"
)
SCORE_STOP_RE = re.compile(r"(?:参考译文|【点睛】|译文[：:])")
TRANSLATION_SECTION_RE = re.compile(
    r"【13题详解】|此题考查文言文翻译的能力|本题考查学生理解并翻译文言文句子的能力"
)
OPTION_GLOSS_RE = re.compile(r"([\u4e00-\u9fff]{1,4})\s*[：:]\s*([^\n。；]+)")
QUOTED_TRANSLATION_GLOSS_RE = re.compile(
    r"“([^”]{1,6})”\s*(?:解释为|翻译为|译为)?\s*[，,:：]?\s*“?([^”；;。\n“]+)"
)
COLON_TRANSLATION_GLOSS_RE = re.compile(r"([\u4e00-\u9fff]{1,6})\s*[：:]\s*([^。；;\n“]+)")
ANALYSIS_SECTION_MARKER_RE = re.compile(
    r"(?:^|\n)\s*[（(]\s*(\d{1,2})\s*[)）]\s*|【\s*(\d{1,2})题详解\s*】",
    re.M,
)
ANALYSIS_SECTION_STOP_RE = re.compile(r"【点睛】|【答案】|答案[：:]|参考译文|译文[：:]")
DETAIL_ANALYSIS_SECTION_RE = re.compile(r"【\s*(\d{1,2})题详解\s*】")
LEGACY_ANALYSIS_SECTION_RE = re.compile(r"[（(]\s*(\d{1,2})\s*[)）]")
NATIONAL_INLINE_QUESTION_MARKER_RE = re.compile(
    r"(?<!\n)([（(]\s*\d{1,2}\s*[)）])\s*(?=(?:对下列|下列对|把文中画|翻译文中画))"
)
NATIONAL_QUESTION_SPLIT_RE = re.compile(
    r"(?:^|\n)\s*(?:(\d+)[\.．]|[（(]\s*(\d{1,2})\s*[)）])\s*(?=(?:对下列|下列对|把文中画|翻译文中画))",
    re.M,
)
TRANSLATION_ITEM_SPLIT_RE = re.compile(
    r"([①②③④⑤⑥⑦⑧⑨⑩]|[（(]\s*\d{1,2}\s*[)）])\s*(.*?)(?=(?:[①②③④⑤⑥⑦⑧⑨⑩]|[（(]\s*\d{1,2}\s*[)）])\s*|$)",
    re.S,
)
TRANSLATION_CLAUSE_SPLIT_RE = re.compile(r"[；;。]\s*")
TRANSLATION_DIRECT_GLOSS_PATTERNS = (
    re.compile(
        r"^[“\"]?([\u4e00-\u9fff]{1,8})[”\"]?\s*(?:解释为|翻译为|译为|可译为|意为|这里指|此处指|文中指|指|即|是)\s*[“\"]?([^”\"；;。\n]+)"
    ),
    re.compile(r"^[“\"]?([\u4e00-\u9fff]{1,8})[”\"]?\s*[：:，,]\s*([^；;。\n]+)"),
)
NATIONAL_CATEGORY_NORMALIZATION = {
    "（新课标i）": "（新课标Ⅰ）",
    "（新课标ⅰ）": "（新课标Ⅰ）",
    "（新课标ⅱ）": "（新课标Ⅱ）",
    "（新课标ⅲ）": "（新课标Ⅲ）",
}

# Conservative set for early-phase split between xuci and shici.
COMMON_XUCI_HEADWORDS = {
    "之", "其", "而", "以", "于", "乃", "则", "者", "也", "焉", "乎", "所",
    "与", "为", "且", "若", "因", "由", "抑", "或", "夫", "盖", "故", "诚",
    "既", "耳", "矣", "已", "哉", "欤", "遂", "即", "虽", "但", "何", "胡",
    "安", "孰", "奚", "斯", "兹", "然", "恶乎",
}
TRANSLATION_META_HEADWORDS = {
    "要点",
    "得分点",
    "关键词",
    "注意以下关键词",
    "第一题得分点",
    "第二题得分点",
    "第三题得分点",
}
TRANSLATION_META_SNIPPETS = (
    "得分点",
    "关键词",
    "译文",
    "译为",
    "句子",
    "本题",
    "下列",
    "这里",
    "此处",
    "文中",
)
TRANSLATION_STRUCTURAL_GLOSS_MARKERS = (
    "省略句",
    "倒装句",
    "判断句",
    "介词短语后置句",
)


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").replace("\r\n", "\n").replace("\r", "\n")


def _clean_excerpt(text: str, limit: int = 220) -> str:
    collapsed = re.sub(r"\s+", " ", _normalize_text(text)).strip()
    return collapsed[:limit]


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            rows.append(json.loads(raw))
    return rows


def _load_xuci_headword_set() -> set[str]:
    return set(COMMON_XUCI_HEADWORDS)


def _load_pending_national_meta() -> dict:
    if not NATIONAL_RAW_PATH.exists():
        return {
            "status": "missing",
            "raw_question_count": 0,
            "raw_year_range": None,
        }
    payload = json.loads(NATIONAL_RAW_PATH.read_text(encoding="utf-8"))
    items = payload.get("example") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        items = []
    years = sorted(
        {
            int(item["year"])
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("year"), str)
            and item["year"].isdigit()
        }
    )
    return {
        "status": "pending_standardization",
        "raw_question_count": len(items),
        "raw_year_range": [years[0], years[-1]] if years else None,
    }


def _normalize_national_category(category: str) -> str:
    value = _normalize_text(category).strip()
    return NATIONAL_CATEGORY_NORMALIZATION.get(value, value)


def _paper_fingerprint(text: str) -> str:
    normalized = _normalize_text(text)
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", normalized)


def _dedupe_national_items(items: list[dict]) -> tuple[list[dict], int]:
    best_by_fingerprint: dict[str, dict] = {}
    for item in items:
        fingerprint = _paper_fingerprint(str(item.get("question", "")))
        if not fingerprint:
            continue
        candidate = dict(item)
        candidate["category"] = _normalize_national_category(str(item.get("category", "")).strip())
        candidate["paper_fingerprint"] = fingerprint
        candidate["paper_key"] = (
            f"national-{str(candidate.get('year') or '').strip()}-"
            f"{hashlib.md5(fingerprint.encode('utf-8')).hexdigest()[:12]}"
        )
        score = len(str(candidate.get("question", ""))) + len(str(candidate.get("analysis", "")))
        existing = best_by_fingerprint.get(fingerprint)
        if not existing:
            best_by_fingerprint[fingerprint] = candidate
            continue
        existing_score = len(str(existing.get("question", ""))) + len(str(existing.get("analysis", "")))
        if score > existing_score:
            best_by_fingerprint[fingerprint] = candidate
    deduped = sorted(
        best_by_fingerprint.values(),
        key=lambda item: (
            int(str(item.get("year") or "0")) if str(item.get("year") or "").isdigit() else 0,
            str(item.get("category") or ""),
            int(item.get("index") or 0),
        ),
    )
    return deduped, max(0, len(items) - len(deduped))


def _clean_headword(text: str) -> str:
    value = _normalize_text(text)
    value = value.strip().strip("“”\"'()（）[]【】,，:：;；。. ")
    value = re.sub(r"\s+", "", value)
    return value


def _clean_gloss(text: str) -> str:
    value = _normalize_text(text)
    value = value.strip().strip("“”\"'()（）[]【】,，:：;；。 ")
    value = re.split(r"[；;。]|(?=“)", value, maxsplit=1)[0].strip()
    return value


def _clean_translation_gloss(text: str) -> str:
    value = _normalize_text(text)
    for marker in ("这里指", "此处指", "文中指", "这里译为", "此处译为", "文中译为"):
        if marker in value:
            value = value.split(marker, 1)[1]
    for marker in ("句子翻译为", "句子可翻译为", "句子译为", "可意译为", "可意译作"):
        if marker in value:
            value = value.split(marker, 1)[0]
    value = value.strip().strip("“”\"'()（）[]【】,，:：;；。. ")
    value = re.sub(r"(?:之意|的意思|之义)$", "", value).strip()
    value = re.sub(r"\s+", "", value)
    return value


def _segment_excerpt(segment: str, start: int, end: int) -> str:
    left = max(0, start - 28)
    right = min(len(segment), end + 72)
    return _clean_excerpt(segment[left:right], limit=180)


def _iter_translation_segments(text: str) -> list[tuple[str, int]]:
    normalized = _normalize_text(text)
    segments: list[tuple[str, int]] = []
    marker_matches = list(SCORE_MARKER_RE.finditer(normalized))
    if marker_matches:
        for index, match in enumerate(marker_matches):
            start = match.start()
            next_start = marker_matches[index + 1].start() if index + 1 < len(marker_matches) else len(normalized)
            stop_match = SCORE_STOP_RE.search(normalized, match.end())
            end = min(next_start, stop_match.start()) if stop_match else next_start
            segment = normalized[start:end].strip()
            if segment:
                segments.append((segment, 4))
        return segments

    section_match = TRANSLATION_SECTION_RE.search(normalized)
    if not section_match:
        return []
    stop_match = SCORE_STOP_RE.search(normalized, section_match.end())
    end = stop_match.start() if stop_match else min(len(normalized), section_match.end() + 700)
    segment = normalized[section_match.start():end].strip()
    return [(segment, 13)] if segment else []


def _extract_analysis_sections(text: str) -> dict[int, str]:
    normalized = _normalize_text(text)
    matches = list(DETAIL_ANALYSIS_SECTION_RE.finditer(normalized))
    if not matches:
        matches = list(LEGACY_ANALYSIS_SECTION_RE.finditer(normalized))
    sections: dict[int, str] = {}
    for index, match in enumerate(matches):
        question_number_raw = match.group(1)
        if not question_number_raw:
            continue
        question_number = int(question_number_raw)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        segment = normalized[start:end].strip()
        stop_match = ANALYSIS_SECTION_STOP_RE.search(segment)
        if stop_match:
            segment = segment[:stop_match.start()]
        segment = segment.strip()
        if not segment:
            continue
        previous = sections.get(question_number)
        if not previous or len(segment) > len(previous):
            sections[question_number] = segment
    return sections


def _split_translation_items(segment: str) -> list[str]:
    matches = TRANSLATION_ITEM_SPLIT_RE.findall(segment)
    if not matches:
        return [segment]
    return [content.strip() for _, content in matches if content.strip()]


def _extract_translation_pair_from_clause(clause: str) -> tuple[str, str] | None:
    text = re.sub(r"\s+", "", _normalize_text(clause))
    text = re.sub(
        r"^(?:本题关键词有|(?:第[一二三四两0-9]+题)?得分点|要点|关键词|注意以下关键词)[：:]\s*",
        "",
        text,
    ).strip()
    for pattern in TRANSLATION_DIRECT_GLOSS_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        headword = _clean_headword(match.group(1))
        gloss = _clean_translation_gloss(match.group(2))
        if (
            not headword
            or not gloss
            or len(headword) > 4
            or len(gloss) > 40
            or any(snippet in headword for snippet in TRANSLATION_META_SNIPPETS)
            or headword in TRANSLATION_META_HEADWORDS
            or (
                len(headword) >= 3
                and any(marker in gloss for marker in TRANSLATION_STRUCTURAL_GLOSS_MARKERS)
            )
        ):
            continue
        return headword, gloss
    return None


def _iter_translation_keyword_pairs(segment: str) -> list[tuple[str, str, str]]:
    pairs: list[tuple[str, str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for item_text in _split_translation_items(segment):
        for clause in TRANSLATION_CLAUSE_SPLIT_RE.split(item_text):
            clause = _normalize_text(clause).strip()
            if not clause:
                continue
            parsed = _extract_translation_pair_from_clause(clause)
            if not parsed:
                continue
            headword, gloss = parsed
            pair_key = (headword, gloss)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            record = (headword, gloss, _clean_excerpt(clause, limit=180))
            pairs.append(record)
    for pattern in (QUOTED_TRANSLATION_GLOSS_RE, COLON_TRANSLATION_GLOSS_RE):
        for match in pattern.finditer(segment):
            headword = _clean_headword(match.group(1))
            gloss = _clean_translation_gloss(match.group(2))
            if (
                not headword
                or not gloss
                or len(headword) > 4
                or len(gloss) > 40
                or headword in TRANSLATION_META_HEADWORDS
                or any(snippet in headword for snippet in TRANSLATION_META_SNIPPETS)
                or (
                    len(headword) >= 3
                    and any(marker in gloss for marker in TRANSLATION_STRUCTURAL_GLOSS_MARKERS)
                )
            ):
                continue
            pair_key = (headword, gloss)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            record = (headword, gloss, _segment_excerpt(segment, match.start(), match.end()))
            pairs.append(record)
    return pairs


def _is_national_lexical_block(block: str) -> bool:
    normalized = _normalize_text(block)
    if NATIONAL_LEXICAL_PROMPT_RE.search(normalized):
        return True
    option_hits = 0
    for _, option_text in _split_options(normalized):
        if OPTION_GLOSS_RE.search(option_text):
            option_hits += 1
    return option_hits >= 3 and "解释" in normalized


def _is_translation_block(block: str) -> bool:
    normalized = _normalize_text(block)
    return bool(TRANSLATION_PROMPT_RE.search(normalized))


def _iter_national_question_blocks(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_text(text)
    normalized = NATIONAL_INLINE_QUESTION_MARKER_RE.sub(r"\n\1", normalized)
    matches = list(NATIONAL_QUESTION_SPLIT_RE.finditer(normalized))
    blocks: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        number_raw = match.group(1) or match.group(2)
        if not number_raw:
            continue
        blocks.append((int(number_raw), normalized[start:end].strip()))
    return blocks


def _build_national_occurrences(xuci_headwords: set[str]) -> tuple[list[dict], list[dict], dict, dict[str, dict]]:
    if not NATIONAL_RAW_PATH.exists():
        return [], [], {
            "label": "全国",
            "status": "missing",
            "raw_question_count": 0,
            "raw_year_range": None,
            "extracted_question_count": 0,
            "extracted_year_range": None,
        }, {}

    payload = json.loads(NATIONAL_RAW_PATH.read_text(encoding="utf-8"))
    items = payload.get("example") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        items = []
    papers, duplicate_record_count = _dedupe_national_items(items)

    xuci_occurrences: list[dict] = []
    shici_occurrences: list[dict] = []
    covered_paper_keys: set[str] = set()
    extracted_years: set[int] = set()
    question_docs: dict[str, dict] = {}

    for item in papers:
        question = _normalize_text(str(item.get("question", "")))
        analysis = _normalize_text(str(item.get("analysis", "")))
        year_raw = str(item.get("year", "")).strip()
        if not year_raw.isdigit():
            continue
        year = int(year_raw)
        category = str(item.get("category", "")).strip()
        paper_key = str(item.get("paper_key") or "").strip()
        title = f"{year} {category} 文言文阅读".strip()
        if paper_key:
            question_docs[paper_key] = {
                "paper_key": paper_key,
                "title": title,
                "year": year,
                "paper": category or "全国卷",
                "category": category,
                "text": question,
                "answer": analysis,
                "source_mode": "bundled_runtime_question",
            }
        question_blocks = _iter_national_question_blocks(question)
        analysis_sections = _extract_analysis_sections(analysis)
        lexical_hit = False
        translation_hit = False

        for question_number, block in question_blocks:
            if not _is_national_lexical_block(block):
                continue
            for option_label, option_text in _split_options(block):
                match = OPTION_GLOSS_RE.search(option_text)
                if not match:
                    continue
                headword = _clean_headword(match.group(1))
                gloss = _clean_gloss(match.group(2))
                if not headword or len(headword) > 4 or not gloss:
                    continue
                kind = _term_kind(headword, xuci_headwords)
                target = xuci_occurrences if kind == "xuci" else shici_occurrences
                target.append(
                    {
                        "headword": headword,
                        "scope": "national",
                        "scope_label": "全国",
                        "year": year,
                        "paper": category or "全国卷",
                        "category": category,
                        "paper_key": paper_key,
                        "title": title,
                        "question_number": question_number,
                        "question_subtype": "national_raw_gloss_option",
                        "option_label": option_label,
                        "gloss": gloss,
                        "excerpt": _clean_excerpt(option_text),
                    }
                )
                lexical_hit = True
                extracted_years.add(year)
        if lexical_hit and paper_key:
            covered_paper_keys.add(paper_key)

        translation_segments: list[tuple[str, int]] = []
        translation_question_numbers = [
            question_number
            for question_number, block in question_blocks
            if _is_translation_block(block)
        ]
        for question_number in translation_question_numbers:
            section = analysis_sections.get(question_number)
            if section:
                translation_segments.append((section, question_number))
        if not translation_segments:
            translation_segments.extend(_iter_translation_segments(analysis))

        for segment, question_number in translation_segments:
            for headword, gloss, excerpt in _iter_translation_keyword_pairs(segment):
                kind = _term_kind(headword, xuci_headwords)
                target = xuci_occurrences if kind == "xuci" else shici_occurrences
                target.append(
                    {
                        "headword": headword,
                        "scope": "national",
                        "scope_label": "全国",
                        "year": year,
                        "paper": category or "全国卷",
                        "category": category,
                        "paper_key": paper_key,
                        "title": title,
                        "question_number": question_number,
                        "question_subtype": "national_raw_translation_keyword",
                        "option_label": "",
                        "gloss": gloss,
                        "excerpt": excerpt,
                    }
                )
                translation_hit = True
                extracted_years.add(year)
        if translation_hit and paper_key:
            covered_paper_keys.add(paper_key)

    raw_years = sorted(
        {
            int(item["year"])
            for item in papers
            if isinstance(item, dict)
            and isinstance(item.get("year"), str)
            and item["year"].isdigit()
        }
    )
    uncovered_titles = sorted(
        {
            f"{int(str(item.get('year') or 0))} {str(item.get('category') or '').strip()} 文言文阅读".strip()
            for item in papers
            if str(item.get("paper_key") or "") not in covered_paper_keys
        }
    )
    coverage = {
        "label": "全国",
        "status": "standardized" if papers and not uncovered_titles else "partial_standardization",
        "raw_record_count": len(items),
        "raw_question_count": len(papers),
        "raw_year_range": [raw_years[0], raw_years[-1]] if raw_years else None,
        "duplicate_record_count": duplicate_record_count,
        "extracted_question_count": len(covered_paper_keys),
        "extracted_year_range": [min(extracted_years), max(extracted_years)] if extracted_years else None,
        "uncovered_titles": uncovered_titles,
    }
    return xuci_occurrences, shici_occurrences, coverage, question_docs


def _iter_question_blocks(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_text(text)
    matches = list(QUESTION_SPLIT_RE.finditer(normalized))
    blocks: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        number = int(match.group(1))
        blocks.append((number, normalized[start:end].strip()))
    return blocks


def _split_options(block: str) -> list[tuple[str, str]]:
    matches = list(OPTION_SPLIT_RE.finditer(block))
    options: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        label = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(block)
        option_text = block[start:end].strip()
        options.append((label, option_text))
    return options


def _split_items(block: str) -> list[tuple[str, str]]:
    return [(marker, content.strip()) for marker, content in ITEM_SPLIT_RE.findall(block)]


def _extract_emphasis_tokens(text: str) -> list[str]:
    tokens = [token.strip() for token in STAR_TOKEN_RE.findall(text) if token.strip()]
    if tokens:
        return tokens
    return [token.strip() for token in DOT_TOKEN_RE.findall(text) if token.strip()]


def _extract_gloss_from_unit(unit_text: str, *, explicit_headword: str | None = None) -> tuple[str, str] | None:
    normalized = _clean_excerpt(unit_text, limit=400)
    if explicit_headword:
        explicit_pattern = re.compile(rf"{re.escape(explicit_headword)}\s*[：:]\s*(.+)")
        match = explicit_pattern.search(normalized)
        if match:
            gloss = match.group(1).strip()
            gloss = re.split(r"\s+[A-D][\.．]\s+", gloss)[0].strip()
            return explicit_headword, gloss
    match = GLOSS_RE.search(normalized)
    if not match:
        return None
    headword = match.group(1).strip()
    gloss = match.group(2).strip()
    gloss = re.split(r"\s+[A-D][\.．]\s+", gloss)[0].strip()
    return headword, gloss


def _detect_block_subtype(block: str) -> str | None:
    if XUCI_SUBTYPE_SAME_RE.search(block):
        return "xuci_compare_same"
    if XUCI_SUBTYPE_DIFF_RE.search(block):
        return "xuci_compare_diff"
    if EXPLANATION_RE.search(block) or QUOTED_HEADWORD_RE.search(block):
        return "gloss_explanation"
    return None


def _term_kind(headword: str, xuci_headwords: set[str], *, prefer_xuci: bool = False) -> str:
    if prefer_xuci or headword in xuci_headwords:
        return "xuci"
    return "shici"


def _build_occurrences() -> tuple[list[dict], list[dict], dict, dict[str, dict]]:
    xuci_headwords = _load_xuci_headword_set()
    rows = _load_jsonl(GAOKAO_CHUNKS_PATH)
    beijing_rows = [
        row
        for row in rows
        if row.get("subject") == "语文"
        and row.get("question_type") == "古文"
        and row.get("region") == "北京"
    ]
    xuci_occurrences: list[dict] = []
    shici_occurrences: list[dict] = []
    question_docs: dict[str, dict] = {}

    for row in sorted(beijing_rows, key=lambda item: (int(item.get("year") or 0), int(item.get("id") or 0))):
        text = _normalize_text(str(row.get("text", "")))
        if not text:
            continue
        paper_key = f"beijing-{row.get('year')}-{row.get('id')}"
        question_docs[paper_key] = {
            "paper_key": paper_key,
            "title": row.get("title"),
            "year": int(row["year"]),
            "paper": "北京卷",
            "category": row.get("category"),
            "text": text,
            "answer": _normalize_text(str(row.get("answer", ""))),
            "source_mode": "bundled_runtime_question",
        }
        for question_number, block in _iter_question_blocks(text):
            subtype = _detect_block_subtype(block)
            if not subtype:
                continue

            quoted_match = QUOTED_HEADWORD_RE.search(block)
            explicit_headword = quoted_match.group(1) if quoted_match else None

            if subtype.startswith("xuci_compare"):
                for option_label, option_text in _split_options(block):
                    tokens = _extract_emphasis_tokens(option_text)
                    if not tokens:
                        continue
                    excerpt = _clean_excerpt(option_text)
                    for pair_index, token in enumerate(tokens, start=1):
                        if token not in xuci_headwords:
                            continue
                        xuci_occurrences.append(
                            {
                                "headword": token,
                                "scope": "beijing",
                                "scope_label": "北京",
                                "year": int(row["year"]),
                                "paper": "北京卷",
                                "category": row.get("category"),
                                "paper_key": paper_key,
                                "title": row.get("title"),
                                "question_number": question_number,
                                "question_subtype": subtype,
                                "option_label": option_label,
                                "pair_index": pair_index,
                                "excerpt": excerpt,
                            }
                        )
                continue

            items = _split_items(block)
            if items:
                units = items
            else:
                units = _split_options(block)
            for unit_label, unit_text in units:
                parsed = _extract_gloss_from_unit(unit_text, explicit_headword=explicit_headword)
                if not parsed:
                    continue
                headword, gloss = parsed
                kind = _term_kind(headword, xuci_headwords, prefer_xuci=bool(explicit_headword and headword == explicit_headword))
                target = xuci_occurrences if kind == "xuci" else shici_occurrences
                target.append(
                    {
                        "headword": headword,
                        "scope": "beijing",
                        "scope_label": "北京",
                        "year": int(row["year"]),
                        "paper": "北京卷",
                        "category": row.get("category"),
                        "paper_key": paper_key,
                        "title": row.get("title"),
                        "question_number": question_number,
                        "question_subtype": "xuci_explanation" if kind == "xuci" else "shici_explanation",
                        "option_label": unit_label,
                        "gloss": gloss,
                        "excerpt": _clean_excerpt(unit_text),
                    }
                )

    national_xuci_occurrences, national_shici_occurrences, national_coverage, national_question_docs = _build_national_occurrences(xuci_headwords)
    xuci_occurrences.extend(national_xuci_occurrences)
    shici_occurrences.extend(national_shici_occurrences)
    question_docs.update(national_question_docs)

    national_status = str((national_coverage or {}).get("status") or "")
    national_note = "全国卷文言文题已完成 2010-2022 标准化抽取。"
    if national_status != "standardized":
        national_note = "全国卷文言文题标准化抽取仍有缺口，需继续核对。"
    duplicate_record_count = int((national_coverage or {}).get("duplicate_record_count") or 0)
    if duplicate_record_count:
        national_note = f"{national_note} GAOKAO-Bench 原始文件含 {duplicate_record_count} 条重复记录，运行时已去重。"

    coverage = {
        "beijing": {
            "label": "北京",
            "question_count": len(beijing_rows),
            "year_range": [2002, 2025] if beijing_rows else None,
        },
        "national": national_coverage if national_coverage else {"label": "全国", **_load_pending_national_meta()},
        "notes": [
            "当前新增真题词表区先接入已标准化的北京卷语文古文题。",
            national_note,
        ],
    }
    return xuci_occurrences, shici_occurrences, coverage, question_docs


def _aggregate_dataset(kind: str, occurrences: list[dict], coverage: dict, question_docs: dict[str, dict]) -> dict:
    by_headword: dict[str, list[dict]] = defaultdict(list)
    for occurrence in occurrences:
        by_headword[occurrence["headword"]].append(occurrence)

    terms: list[dict] = []
    for headword, rows in by_headword.items():
        rows.sort(
            key=lambda item: (
                -int(item.get("year") or 0),
                0 if item.get("scope") == "beijing" else 1,
                str(item.get("title") or ""),
                str(item.get("option_label") or ""),
                int(item.get("pair_index") or 0),
            )
        )
        years = sorted({int(item["year"]) for item in rows})
        question_keys = {
            f"{item.get('paper_key') or item['title']}#q{item['question_number']}"
            for item in rows
        }
        question_type_counts = Counter(item["question_subtype"] for item in rows)
        scope_counts = Counter(item.get("scope") or "" for item in rows)
        source_tags = []
        if scope_counts.get("beijing"):
            source_tags.append("北京")
        if scope_counts.get("national"):
            source_tags.append("全国")
        glosses = [
            item["gloss"]
            for item in rows
            if item.get("gloss")
        ]
        sample_glosses: list[str] = []
        for gloss in glosses:
            if gloss not in sample_glosses:
                sample_glosses.append(gloss)
            if len(sample_glosses) >= 5:
                break
        terms.append(
            {
                "headword": headword,
                "display_headword": headword,
                "total_occurrences": len(rows),
                "beijing_occurrences": scope_counts.get("beijing", 0),
                "national_occurrences": scope_counts.get("national", 0),
                "question_count": len(question_keys),
                "years": years,
                "year_labels": [str(year) for year in years],
                "question_type_counts": dict(question_type_counts),
                "source_tags": source_tags,
                "sample_glosses": sample_glosses,
                "occurrences": rows,
            }
        )

    terms.sort(
        key=lambda item: (
            -item["total_occurrences"],
            -item["question_count"],
            -(item["years"][-1] if item["years"] else 0),
            item["headword"],
        )
    )

    return {
        "kind": kind,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "available": bool(terms),
        "coverage": coverage,
        "stats": {
            "term_count": len(terms),
            "occurrence_count": len(occurrences),
            "question_count": len(
                {
                    f"{item.get('paper_key') or item['title']}#q{item['question_number']}"
                    for item in occurrences
                }
            ),
        },
        "question_docs": question_docs,
        "terms": terms,
    }


def build_payloads() -> tuple[dict, dict]:
    xuci_occurrences, shici_occurrences, coverage, question_docs = _build_occurrences()
    return (
        _aggregate_dataset("xuci", xuci_occurrences, coverage, question_docs),
        _aggregate_dataset("shici", shici_occurrences, coverage, question_docs),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Print both payloads to stdout")
    args = parser.parse_args()

    xuci_payload, shici_payload = build_payloads()
    if args.json:
        print(json.dumps({"xuci": xuci_payload, "shici": shici_payload}, ensure_ascii=False, indent=2))
        return 0

    DATA_INDEX.mkdir(parents=True, exist_ok=True)
    XUCI_OUTPUT_PATH.write_text(json.dumps(xuci_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    SHICI_OUTPUT_PATH.write_text(json.dumps(shici_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"wrote {XUCI_OUTPUT_PATH}")
    print(f"wrote {SHICI_OUTPUT_PATH}")
    print(f"xuci terms: {xuci_payload['stats']['term_count']}, occurrences: {xuci_payload['stats']['occurrence_count']}")
    print(f"shici terms: {shici_payload['stats']['term_count']}, occurrences: {shici_payload['stats']['occurrence_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
