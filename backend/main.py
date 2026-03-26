"""
跨学科教材知识平台 · FastAPI 后端
"""
import asyncio, gzip, sqlite3, json, math, os, re, time, functools, hashlib, threading, unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional
from urllib.parse import quote
import httpx
from fastapi import FastAPI, Query, HTTPException, Body
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

try:
    from cachetools import TTLCache
    _cache = TTLCache(maxsize=64, ttl=300)  # 5 min TTL
except ImportError:
    _cache = {}  # fallback: simple dict (never expires, but resets on restart)

try:
    import opencc as _opencc_mod
    _S2T_CONVERTER = _opencc_mod.OpenCC("s2t")
except ImportError:
    _S2T_CONVERTER = None


def _to_traditional(text: str) -> str:
    """Convert simplified Chinese to traditional. No-op if already traditional or opencc unavailable."""
    if _S2T_CONVERTER is None:
        return text
    return _S2T_CONVERTER.convert(text)


def _make_runtime_cache(maxsize: int, ttl: int):
    if "TTLCache" in globals():
        try:
            return TTLCache(maxsize=maxsize, ttl=ttl)
        except Exception:
            return {}
    return {}

try:
    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer, CrossEncoder
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False

# ── paths ─────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", Path(__file__).resolve().parents[1])).expanduser().resolve()
_DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
_ALT_DATA_ROOT = PROJECT_ROOT.parent / "data"
if not (_DEFAULT_DATA_ROOT / "index" / "textbook_chunks.index").exists() and (_ALT_DATA_ROOT / "index" / "textbook_chunks.index").exists():
    _DEFAULT_DATA_ROOT = _ALT_DATA_ROOT
DATA_ROOT = Path(os.getenv("DATA_ROOT", _DEFAULT_DATA_ROOT)).expanduser().resolve()
STATE_ROOT = Path(os.getenv("STATE_ROOT", PROJECT_ROOT / "state")).expanduser().resolve()
STATE_ROOT.mkdir(parents=True, exist_ok=True)
for d in ("logs", "cache", "tmp", "batch"):
    (STATE_ROOT / d).mkdir(parents=True, exist_ok=True)

def _resolve_data_asset(filename: str) -> Path:
    primary = DATA_ROOT / "index" / filename
    legacy = DATA_ROOT / filename
    if primary.exists():
        return primary
    if legacy.exists():
        return legacy
    return primary


DB_PATH = _resolve_data_asset("textbook_mineru_fts.db")
DICT_DB_PATH = _resolve_data_asset("dictionary_index.db")
DICT_MOE_REVISED_DB_PATH = _resolve_data_asset("dict_moe_revised.db")
DICT_MOE_IDIOMS_DB_PATH = _resolve_data_asset("dict_moe_idioms.db")
DICT_HEADWORD_INDEX_PATH = _resolve_data_asset("dict_headword_pages.json")
DICT_QC_PATH = _resolve_data_asset("dict_headword_qc.json")
TEXTBOOK_CLASSICS_MANIFEST_PATH = DATA_ROOT / "index" / "textbook_classics_manifest.json"
BUNDLED_TEXTBOOK_CLASSICS_MANIFEST_PATH = Path(__file__).with_name("textbook_classics_manifest.json")
TEXTBOOK_VERSION_MANIFEST_PATH = Path(__file__).with_name("textbook_version_manifest.json")
XUCI_SINGLE_CHAR_INDEX_PATH = DATA_ROOT / "index" / "xuci_single_char_index.json"
BUNDLED_XUCI_SINGLE_CHAR_INDEX_PATH = Path(__file__).with_name("xuci_single_char_index.json")
DICT_EXAM_XUCI_PATH = _resolve_data_asset("dict_exam_xuci.json")
DICT_EXAM_SHICI_PATH = _resolve_data_asset("dict_exam_shici.json")
DICT_EXAM_XUCI_DETAILS_PATH = _resolve_data_asset("dict_exam_xuci_details.json")
FRONTEND = Path(__file__).parent.parent / "frontend"
FAISS_INDEX_PATH = _resolve_data_asset("textbook_chunks.index")
FAISS_MANIFEST_PATH = _resolve_data_asset("textbook_chunks.manifest.json")
SUPPLEMENTAL_TEXTBOOK_ROOT = DATA_ROOT / "mineru_output_backup"
SUPPLEMENTAL_TEXTBOOK_INDEX_GZ_PATH = _resolve_data_asset("supplemental_textbook_pages.jsonl.gz")
SUPPLEMENTAL_TEXTBOOK_INDEX_PATH = _resolve_data_asset("supplemental_textbook_pages.jsonl")
SUPPLEMENTAL_TEXTBOOK_MANIFEST_PATH = _resolve_data_asset("supplemental_textbook_pages.manifest.json")
SUPPLEMENTAL_VECTOR_INDEX_PATH = _resolve_data_asset("supplemental_textbook_pages.index")
SUPPLEMENTAL_VECTOR_MANIFEST_PATH = _resolve_data_asset("supplemental_textbook_pages.vector.manifest.json")
RAW_TEXTBOOK_OCR_INDEX_CANDIDATES = (
    _resolve_data_asset("mineru_chunks.jsonl"),
    _resolve_data_asset("mineru_chunks.next.jsonl"),
    _resolve_data_asset("mineru_chunks.base_from_db.jsonl"),
)
BUNDLED_SUPPLEMENTAL_TEXTBOOK_INDEX_GZ_PATH = Path(__file__).with_name("supplemental_textbook_pages.jsonl.gz")
BUNDLED_SUPPLEMENTAL_TEXTBOOK_INDEX_PATH = Path(__file__).with_name("supplemental_textbook_pages.jsonl")
BUNDLED_SUPPLEMENTAL_TEXTBOOK_MANIFEST_PATH = Path(__file__).with_name("supplemental_textbook_pages.manifest.json")
BUNDLED_SUPPLEMENTAL_VECTOR_INDEX_PATH = Path(__file__).with_name("supplemental_textbook_pages.index")
BUNDLED_SUPPLEMENTAL_VECTOR_MANIFEST_PATH = Path(__file__).with_name("supplemental_textbook_pages.vector.manifest.json")
SUPPLEMENTAL_REQUIRED = os.getenv("SUPPLEMENTAL_REQUIRED", "1").strip().lower() not in {"0", "false", "no"}
SQLITE_BUSY_TIMEOUT_MS = max(1000, int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000")))
SQLITE_CONNECT_TIMEOUT_SEC = max(1.0, SQLITE_BUSY_TIMEOUT_MS / 1000.0)
FAISS_SCORE_THRESHOLD = max(0.0, min(1.0, float(os.getenv("FAISS_SCORE_THRESHOLD", "0.62"))))
SUPPLEMENTAL_VECTOR_ENABLED = os.getenv("SUPPLEMENTAL_VECTOR_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
SUPPLEMENTAL_VECTOR_SCORE_THRESHOLD = max(
    0.0,
    min(1.0, float(os.getenv("SUPPLEMENTAL_VECTOR_SCORE_THRESHOLD", "0.58"))),
)
QUERY_TERM_PLAN_LIMIT = max(4, int(os.getenv("QUERY_TERM_PLAN_LIMIT", "8")))
SUPPLEMENTAL_FALLBACK_LIMIT = max(20, int(os.getenv("SUPPLEMENTAL_FALLBACK_LIMIT", "180")))
SEMANTIC_QUERY_CACHE = _make_runtime_cache(
    max(64, int(os.getenv("SEMANTIC_QUERY_CACHE_MAXSIZE", "256"))),
    max(60, int(os.getenv("SEMANTIC_QUERY_CACHE_TTL_SEC", "1800"))),
)
EVIDENCE_SPAN_CACHE = _make_runtime_cache(
    max(256, int(os.getenv("EVIDENCE_SPAN_CACHE_MAXSIZE", "4096"))),
    max(300, int(os.getenv("EVIDENCE_SPAN_CACHE_TTL_SEC", "3600"))),
)
EVIDENCE_MAX_SPAN_CHARS = max(80, int(os.getenv("EVIDENCE_MAX_SPAN_CHARS", "190")))
EVIDENCE_MAX_SEGMENTS = max(24, int(os.getenv("EVIDENCE_MAX_SEGMENTS", "120")))

def _parse_csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


CORS_ALLOW_ORIGINS = _parse_csv_env(
    "CORS_ALLOW_ORIGINS",
    "https://sun.bdfz.net,https://jks.bdfz.net,https://ai.bdfz.net",
)
CORS_ALLOW_METHODS = _parse_csv_env("CORS_ALLOW_METHODS", "GET,POST,OPTIONS")
CORS_ALLOW_HEADERS = _parse_csv_env("CORS_ALLOW_HEADERS", "*") or ["*"]
CORS_ALLOW_ORIGIN_REGEX = os.getenv(
    "CORS_ALLOW_ORIGIN_REGEX",
    r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
).strip() or None

app = FastAPI(title="跨学科教材知识平台", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_origin_regex=CORS_ALLOW_ORIGIN_REGEX,
    allow_methods=CORS_ALLOW_METHODS,
    allow_headers=CORS_ALLOW_HEADERS,
)

# ── Global AI Models ──────────────────────────────────────────────────
faiss_index = None
supplemental_faiss_index = None
embedder = None
reranker = None
EMBEDDER_NAME = os.getenv("EMBEDDER", "BAAI/bge-m3")  # upgraded from bge-small-zh-v1.5
RERANKER_NAME = os.getenv("RERANKER", "BAAI/bge-reranker-base").strip() or "BAAI/bge-reranker-base"
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
RERANKER_PRELOAD = os.getenv("RERANKER_PRELOAD", "1").strip().lower() not in {"0", "false", "no"}
RERANKER_MAX_CANDIDATES = max(12, int(os.getenv("RERANKER_MAX_CANDIDATES", "36")))
RERANKER_FINAL_LIMIT = max(4, int(os.getenv("RERANKER_FINAL_LIMIT", "8")))
GRAPH_RAG_ENABLED = os.getenv("GRAPH_RAG_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
GRAPH_RAG_MAX_RELATIONS = max(2, int(os.getenv("GRAPH_RAG_MAX_RELATIONS", "6")))
faiss_status_reason = None
faiss_manifest = None
supplemental_faiss_status_reason = None
supplemental_faiss_manifest = None
reranker_status_reason = None
_reranker_lock = threading.Lock()
_runtime_warmup_lock = threading.Lock()
_runtime_warmup_started = False
# Frontend should stay on ai.bdfz.net, but the current VPS reaches the same Worker more reliably via workers.dev.
AI_SERVICE_URL = os.getenv("AI_SERVICE_URL", "https://apis.bdfz.workers.dev/")
AI_SERVICE_LABEL = os.getenv("AI_SERVICE_LABEL", "Gemini")
AI_SERVICE_TIMEOUT = float(os.getenv("AI_SERVICE_TIMEOUT_SEC", "35"))
AI_SERVICE_RETRIES = max(0, int(os.getenv("AI_SERVICE_RETRIES", "1")))
AI_SERVICE_RETRY_DELAY = max(0.0, float(os.getenv("AI_SERVICE_RETRY_DELAY_SEC", "0.8")))
AI_SERVICE_MODEL = os.getenv("AI_SERVICE_MODEL", "gemini-flash-latest").strip() or "gemini-flash-latest"
AI_SERVICE_ORIGIN = os.getenv("AI_SERVICE_ORIGIN", "https://sun.bdfz.net").rstrip("/")
AI_SERVICE_REFERER = os.getenv("AI_SERVICE_REFERER", f"{AI_SERVICE_ORIGIN}/")
AI_SERVICE_USER_AGENT = os.getenv(
    "AI_SERVICE_USER_AGENT",
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
)
AI_SERVICE_PROJECT = os.getenv("AI_SERVICE_PROJECT", "").strip()
AI_SERVICE_TASK_TYPE = os.getenv("AI_SERVICE_TASK_TYPE", "chat").strip() or "chat"
AI_SERVICE_THINKING_LEVEL = os.getenv("AI_SERVICE_THINKING_LEVEL", "low").strip() or "low"
AI_INTERNAL_TOKEN = os.getenv("AI_INTERNAL_TOKEN", "").strip()
_write_lock = threading.Lock()
_jieba_concept_token = None
CHAT_STOPWORDS = {
    "请", "先", "再", "继续", "解释", "一下", "分析", "总结", "说明", "告诉", "给我",
    "这个", "这个概念", "这个问题", "它", "那", "哪些", "哪个", "什么", "为什么",
    "怎么", "如何", "最", "常见", "共同", "核心", "关系", "区别", "联系", "如果",
    "我要", "复习", "应该", "顺序", "串起来", "学习", "建议", "围绕", "容易", "混淆",
    "还有", "以及", "一下子", "可以", "请问", "高考", "学科", "里的",
    "综合", "综合解读", "解读", "突出", "跨学科", "联动", "整合", "对比", "比较",
    "展开", "展开讲讲", "梳理", "串联", "理解", "给出", "提出",
}
CHAT_HISTORY_MAX_MESSAGES = max(2, int(os.getenv("CHAT_HISTORY_MAX_MESSAGES", "6")))
CHAT_HISTORY_TRUNCATED_CHARS = max(200, int(os.getenv("CHAT_HISTORY_TRUNCATED_CHARS", "600")))
CHAT_HISTORY_FULL_TAIL_MESSAGES = max(2, int(os.getenv("CHAT_HISTORY_FULL_TAIL_MESSAGES", "4")))
MATCH_PHRASE_MAX_WINDOW = max(2, int(os.getenv("MATCH_PHRASE_MAX_WINDOW", "6")))
CHAT_BOOK_QUOTA_PER_BOOK = max(1, int(os.getenv("CHAT_BOOK_QUOTA_PER_BOOK", "2")))
DICT_TEXTBOOK_RESPONSE_TEXT_LIMIT = max(240, int(os.getenv("DICT_TEXTBOOK_RESPONSE_TEXT_LIMIT", "900")))
DICT_GAOKAO_RESPONSE_TEXT_LIMIT = max(240, int(os.getenv("DICT_GAOKAO_RESPONSE_TEXT_LIMIT", "900")))
DICT_EXAM_QUESTION_TEXT_LIMIT = max(1200, int(os.getenv("DICT_EXAM_QUESTION_TEXT_LIMIT", "8000")))
DICT_EXAM_DATASET_CACHE = _make_runtime_cache(8, 300)
TRENDING_QUERY_EXCLUDE_RE = re.compile(
    r"(?:concurrency[\W_/-]*smoke|smoke[\W_/-]*test|health[\W_/-]*check|load[\W_/-]*test|synthetic[\W_/-]*smoke|dummy[\W_/-]*smoke)",
    re.IGNORECASE,
)
TRENDING_QUERY_MARKERS = (
    "concurrencysmoke",
    "smoketest",
    "healthcheck",
    "loadtest",
    "syntheticsmoke",
    "dummysmoke",
)
ENGLISH_ANALYTICS_MIN_TERM_COUNT = 2
ENGLISH_ANALYTICS_MAX_DOC_SHARE = 0.12
ENGLISH_ANALYTICS_MAX_DOC_TERMS = 8
ENGLISH_ANALYTICS_ACRONYMS = {"AI", "DNA", "GDP", "PCR", "RNA", "UN", "UK", "US"}
ENGLISH_ANALYTICS_STOPWORDS = {
    "a", "about", "after", "again", "all", "also", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "before", "being", "between", "both", "but", "by",
    "can", "could",
    "did", "do", "does", "doing", "down", "during",
    "each", "either", "else",
    "for", "from", "further",
    "get", "gets", "getting",
    "had", "has", "have", "having", "he", "her", "here", "hers", "him", "his", "how",
    "if", "in", "into", "is", "it", "its", "itself",
    "just",
    "let", "like",
    "may", "might", "more", "most", "much", "must", "my",
    "near", "need", "no", "nor", "not", "now",
    "of", "off", "on", "once", "one", "only", "or", "other", "our", "out", "over", "own",
    "same", "she", "should", "since", "so", "some", "such",
    "than", "that", "the", "their", "them", "then", "there", "these", "they", "this", "those", "through", "to", "too",
    "under", "until", "up", "us",
    "very",
    "was", "we", "were", "what", "when", "where", "which", "while", "who", "why", "will", "with", "would",
    "you", "your",
    "am", "isn", "aren", "wasn", "weren", "don", "doesn", "didn", "won", "wouldn", "couldn", "shouldn",
    "lesson", "unit", "topic", "topics", "page", "pages",
    "activity", "activities", "task", "tasks", "project", "projects",
    "read", "reading", "reader", "listen", "listening", "speak", "speaking", "talk", "talking",
    "write", "writing", "written", "match", "circle", "underline", "complete", "choose",
    "question", "questions", "answer", "answers", "example", "examples", "passage", "text", "texts", "report",
    "dialogue", "conversation", "grammar", "vocabulary", "language", "communication", "skill", "skills",
    "learning", "review", "homework", "warm", "pair", "group", "word", "words", "sentence", "sentences",
    "photo", "photos", "picture", "pictures", "chart", "table", "tables", "figure", "figures",
    "section", "sections", "part", "parts", "module", "modules", "tip", "tips", "focus", "self", "assessment",
    "english", "chinese", "student", "students", "teacher", "teachers", "class", "classes",
    "img", "image", "images", "jpg", "jpeg", "png", "webp", "gif",
    "unit", "units", "lesson", "lessons", "workbook", "workbooks", "workshop", "workshops",
    "builder", "builders", "club", "clubs", "assessment", "assessments", "progress", "textbook",
    "activate", "view", "views", "general", "understanding", "critical", "thinking",
    "adj", "adv", "prep", "pron", "conj", "noun", "verb", "phrase", "phr", "vt", "vi",
    "people", "person", "time", "times", "life", "many", "make", "made", "use", "used", "think", "work",
    "day", "days", "year", "years", "thing", "things", "way", "ways", "place", "places",
    "kind", "kinds", "sort", "sorts", "someone", "somebody", "everyone", "everybody", "everything",
    "find", "help", "world", "know", "first", "good", "information", "new", "story", "learn",
    "two", "three", "four", "five", "share", "well", "important", "look", "give", "different",
    "see", "take", "great", "using", "said", "going", "want", "expression", "expressions",
    "one", "second", "third", "also", "another", "around", "back", "best", "better", "come",
    "coming", "got", "go", "came", "left", "right", "long", "little", "big", "small",
    "don't", "i'm", "im", "say", "something", "even", "correct", "form", "really", "feel",
    "ideas", "discuss", "below", "book", "books", "notes", "following", "often", "learnt",
    "always", "last", "present", "yes",
}
DICT_SOURCE_META = {
    "changyong": {
        "label": "常用字",
        "sort_order": 1,
        "page_prefix": "dict_changyong",
        "page_count": 659,
        "book_page_offset": 60,
        "entry_page_limit": 553,
    },
    "xuci": {
        "label": "虚词",
        "sort_order": 2,
        "page_prefix": "dict_xuci",
        "page_count": 921,
        "book_page_offset": 12,
        "entry_page_limit": 888,
    },
    "ciyuan": {
        "label": "辞源",
        "sort_order": 3,
        "page_prefix": "dict_ciyuan",
        "page_count": 3940,
        "book_page_offset": 0,
        "entry_page_limit": 3940,
    },
}
_dict_enabled_sources = [
    source
    for source in _parse_csv_env("DICT_ENABLED_SOURCES", "xuci,changyong")
    if source in DICT_SOURCE_META
]
DICT_ENABLED_SOURCES = tuple(_dict_enabled_sources or DICT_SOURCE_META.keys())
DICT_ENABLED_SOURCE_SET = set(DICT_ENABLED_SOURCES)
CLASSICAL_TEXTBOOK_EXCLUDE_HINTS = (
    "目录",
    "单元学习任务",
    "单元研习任务",
    "学习提示",
    "词语积累与词语解释",
    "整本书阅读",
    "写作",
    "口语交际",
    "普通高中教科书",
    "教育部组织编写",
)
CLASSICAL_GAOKAO_HINTS = (
    "文言",
    "文言文",
    "古文",
    "古诗",
    "古诗文",
    "古诗词",
    "诗歌鉴赏",
    "古代诗歌阅读",
    "古代诗文阅读",
)
CLASSICAL_MARKERS_STRONG = (
    "曰",
    "矣",
    "焉",
    "哉",
    "兮",
    "寡人",
    "若夫",
    "者也",
    "何以",
    "是故",
    "君子",
    "小人",
    "吾",
    "汝",
    "尔",
)
CLASSICAL_MARKERS_LIGHT = (
    "乃",
    "则",
    "岂",
    "孰",
    "奚",
    "未尝",
    "故",
    "夫",
)
TEXTBOOK_CLASSICS_TRIM_HINTS = (
    "学习提示",
    "单元学习任务",
    "单元研习任务",
)


def _compute_vector_source_fingerprint(text_limit: int) -> tuple[Optional[int], Optional[str]]:
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            """
            SELECT id, substr(text, 1, ?)
            FROM chunks
            WHERE source != 'gaokao' AND text IS NOT NULL AND text != ''
            ORDER BY id
            """,
            (text_limit,),
        ).fetchall()
        con.close()
    except Exception:
        return None, None

    h = hashlib.sha256()
    for chunk_id, text in rows:
        payload = json.dumps([int(chunk_id), text or ""], ensure_ascii=False, separators=(",", ":"))
        h.update(payload.encode("utf-8"))
        h.update(b"\n")
    return len(rows), h.hexdigest()


def _expected_vector_rows() -> Optional[int]:
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute(
            "SELECT COUNT(*) FROM chunks WHERE source != 'gaokao' AND text IS NOT NULL AND text != ''"
        ).fetchone()
        con.close()
        return int(row[0]) if row else None
    except Exception:
        return None


def _load_faiss_manifest() -> Optional[dict]:
    if not FAISS_MANIFEST_PATH.exists():
        return None
    try:
        return json.loads(FAISS_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read FAISS manifest: {e}", flush=True)
        return None


def _validate_faiss_manifest(index_obj, expected_rows: Optional[int], manifest: Optional[dict]) -> list[str]:
    issues = []
    if manifest is None:
        issues.append("missing_manifest")
        return issues

    manifest_model = (manifest.get("model") or {}).get("name")
    if manifest_model != EMBEDDER_NAME:
        issues.append(f"manifest_model={manifest_model!r} runtime_model={EMBEDDER_NAME!r}")

    manifest_dim = (manifest.get("index") or {}).get("dimension")
    if manifest_dim != index_obj.d:
        issues.append(f"manifest_dim={manifest_dim} index_dim={index_obj.d}")

    manifest_rows = (manifest.get("index") or {}).get("vector_rows")
    if manifest_rows != index_obj.ntotal:
        issues.append(f"manifest_rows={manifest_rows} index_rows={index_obj.ntotal}")

    if expected_rows is not None and index_obj.ntotal != expected_rows:
        issues.append(f"index_rows={index_obj.ntotal} expected_rows={expected_rows}")

    vector_source = manifest.get("vector_source") or {}
    manifest_source_rows = vector_source.get("row_count")
    if expected_rows is not None and manifest_source_rows != expected_rows:
        issues.append(f"manifest_source_rows={manifest_source_rows} expected_rows={expected_rows}")

    manifest_text_limit = int((manifest.get("model") or {}).get("text_limit_chars") or 512)
    current_rows, current_fingerprint = _compute_vector_source_fingerprint(manifest_text_limit)
    if current_rows is None or current_fingerprint is None:
        issues.append("vector_source_fingerprint_unavailable")
    else:
        if expected_rows is not None and current_rows != expected_rows:
            issues.append(f"current_source_rows={current_rows} expected_rows={expected_rows}")
        manifest_fingerprint = vector_source.get("fingerprint_sha256")
        if manifest_fingerprint != current_fingerprint:
            issues.append("vector_source_fingerprint_mismatch")

    return issues


def _compute_supplemental_vector_source_fingerprint(text_limit: int) -> tuple[Optional[int], Optional[str]]:
    source_candidates = (
        (SUPPLEMENTAL_TEXTBOOK_INDEX_GZ_PATH, gzip.open),
        (SUPPLEMENTAL_TEXTBOOK_INDEX_PATH, open),
        (BUNDLED_SUPPLEMENTAL_TEXTBOOK_INDEX_GZ_PATH, gzip.open),
        (BUNDLED_SUPPLEMENTAL_TEXTBOOK_INDEX_PATH, open),
    )
    source_path = None
    opener = None
    for candidate_path, candidate_opener in source_candidates:
        if candidate_path.exists():
            source_path = candidate_path
            opener = candidate_opener
            break
    if source_path is None or opener is None:
        return None, None

    try:
        h = hashlib.sha256()
        count = 0
        with opener(source_path, "rt", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                payload = json.dumps(
                    [str(item.get("id") or ""), str(item.get("text") or "")[:text_limit]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                h.update(payload.encode("utf-8"))
                h.update(b"\n")
                count += 1
        return count, h.hexdigest()
    except Exception:
        return None, None


def _load_supplemental_vector_manifest() -> Optional[dict]:
    for path in (SUPPLEMENTAL_VECTOR_MANIFEST_PATH, BUNDLED_SUPPLEMENTAL_VECTOR_MANIFEST_PATH):
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Failed to read supplemental vector manifest {path}: {e}", flush=True)
            return None
    return None


def _get_supplemental_vector_source_info() -> dict:
    if SUPPLEMENTAL_VECTOR_INDEX_PATH.exists():
        return {"available": True, "source": "runtime_index", "path": SUPPLEMENTAL_VECTOR_INDEX_PATH}
    if BUNDLED_SUPPLEMENTAL_VECTOR_INDEX_PATH.exists():
        return {"available": True, "source": "bundled_index", "path": BUNDLED_SUPPLEMENTAL_VECTOR_INDEX_PATH}
    return {"available": False, "source": "missing", "path": None}


def _validate_supplemental_vector_manifest(index_obj, manifest: Optional[dict]) -> list[str]:
    issues = []
    if manifest is None:
        issues.append("missing_manifest")
        return issues

    manifest_model = (manifest.get("model") or {}).get("name")
    if manifest_model != EMBEDDER_NAME:
        issues.append(f"manifest_model={manifest_model!r} runtime_model={EMBEDDER_NAME!r}")

    manifest_dim = (manifest.get("index") or {}).get("dimension")
    if manifest_dim != index_obj.d:
        issues.append(f"manifest_dim={manifest_dim} index_dim={index_obj.d}")

    manifest_rows = (manifest.get("index") or {}).get("vector_rows")
    if manifest_rows != index_obj.ntotal:
        issues.append(f"manifest_rows={manifest_rows} index_rows={index_obj.ntotal}")

    vector_source = manifest.get("vector_source") or {}
    manifest_source_rows = vector_source.get("row_count")
    if manifest_source_rows != index_obj.ntotal:
        issues.append(f"manifest_source_rows={manifest_source_rows} index_rows={index_obj.ntotal}")

    manifest_text_limit = int((manifest.get("model") or {}).get("text_limit_chars") or 512)
    current_rows, current_fingerprint = _compute_supplemental_vector_source_fingerprint(manifest_text_limit)
    if current_rows is None or current_fingerprint is None:
        issues.append("vector_source_fingerprint_unavailable")
    else:
        if current_rows != index_obj.ntotal:
            issues.append(f"current_source_rows={current_rows} index_rows={index_obj.ntotal}")
        manifest_fingerprint = vector_source.get("fingerprint_sha256")
        if manifest_fingerprint != current_fingerprint:
            issues.append("vector_source_fingerprint_mismatch")
    return issues


def _has_local_sentence_transformer_snapshot(model_name: str) -> bool:
    direct_path = Path(model_name).expanduser()
    if direct_path.exists():
        return True

    snapshots_dir = (
        Path.home()
        / ".cache"
        / "huggingface"
        / "hub"
        / f"models--{model_name.replace('/', '--')}"
        / "snapshots"
    )
    if not snapshots_dir.exists():
        return False

    candidates = sorted(
        (path for path in snapshots_dir.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return bool(candidates)


def _load_sentence_transformer(model_name: str):
    kwargs = {}
    use_local_files_only = _has_local_sentence_transformer_snapshot(model_name)
    if use_local_files_only:
        kwargs["local_files_only"] = True

    restore_ssl_keylog = None
    restore_hf_offline = None
    ssl_keylogfile = (os.getenv("SSLKEYLOGFILE") or "").strip()
    if ssl_keylogfile:
        ssl_path = Path(ssl_keylogfile).expanduser()
        if not ssl_path.exists() or not os.access(ssl_path, os.W_OK):
            restore_ssl_keylog = ssl_keylogfile
            os.environ.pop("SSLKEYLOGFILE", None)
    if use_local_files_only and os.getenv("HF_HUB_OFFLINE") != "1":
        restore_hf_offline = os.getenv("HF_HUB_OFFLINE")
        os.environ["HF_HUB_OFFLINE"] = "1"

    try:
        return SentenceTransformer(model_name, **kwargs)
    finally:
        if restore_ssl_keylog is not None:
            os.environ["SSLKEYLOGFILE"] = restore_ssl_keylog
        if restore_hf_offline is not None:
            os.environ["HF_HUB_OFFLINE"] = restore_hf_offline
        elif use_local_files_only:
            os.environ.pop("HF_HUB_OFFLINE", None)


def _load_cross_encoder(model_name: str):
    kwargs = {}
    use_local_files_only = _has_local_sentence_transformer_snapshot(model_name)
    if use_local_files_only:
        kwargs["local_files_only"] = True
    return CrossEncoder(model_name, **kwargs)


def _get_reranker():
    global reranker, reranker_status_reason
    if not RERANKER_ENABLED or not FAISS_AVAILABLE:
        return None
    if reranker is not None:
        return reranker
    with _reranker_lock:
        if reranker is not None:
            return reranker
        try:
            reranker = _load_cross_encoder(RERANKER_NAME)
            reranker_status_reason = None
        except Exception as e:
            reranker = None
            reranker_status_reason = str(e)
            print(f"Failed to load reranker: {e}", flush=True)
    return reranker


def _cached_query_embedding(query_text: str):
    clean_query = _clean_query_text(query_text)[:512]
    if not clean_query or not embedder or not FAISS_AVAILABLE:
        return None
    cache_key = (EMBEDDER_NAME, clean_query)
    cached = SEMANTIC_QUERY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        query_vec = embedder.encode([clean_query], normalize_embeddings=True).astype("float32")
    except Exception:
        return None
    SEMANTIC_QUERY_CACHE[cache_key] = query_vec
    return query_vec


def _warm_runtime_components():
    if RERANKER_PRELOAD and RERANKER_ENABLED:
        _get_reranker()


def _schedule_runtime_warmup():
    global _runtime_warmup_started
    if not (RERANKER_PRELOAD and RERANKER_ENABLED):
        return
    with _runtime_warmup_lock:
        if _runtime_warmup_started:
            return
        _runtime_warmup_started = True
    threading.Thread(target=_warm_runtime_components, name="runtime-model-warmup", daemon=True).start()


@app.on_event("startup")
async def _startup_runtime_warmup():
    _schedule_runtime_warmup()

if FAISS_AVAILABLE and FAISS_INDEX_PATH.exists():
    try:
        print(f"Loading FAISS index from {FAISS_INDEX_PATH}...", flush=True)
        raw_index = faiss.read_index(str(FAISS_INDEX_PATH))
        expected_rows = _expected_vector_rows()
        faiss_manifest = _load_faiss_manifest()
        validation_issues = _validate_faiss_manifest(raw_index, expected_rows, faiss_manifest)
        if validation_issues:
            faiss_status_reason = "; ".join(validation_issues)
            print(
                "FAISS disabled by validation gate: "
                f"{faiss_status_reason}. Rebuild textbook_chunks.index with a matching manifest.",
                flush=True,
            )
        else:
            # Keep the offline-built index as-is. Runtime auto-conversion can break explicit ID mapping.
            faiss_index = raw_index
            embedder = _load_sentence_transformer(EMBEDDER_NAME)
            print(f"FAISS index loaded with {faiss_index.ntotal} vectors. Model: {EMBEDDER_NAME}", flush=True)
    except Exception as e:
        faiss_index = None
        embedder = None
        faiss_status_reason = str(e)
        print(f"Failed to load FAISS/model: {e}", flush=True)
        import traceback; traceback.print_exc()
elif not FAISS_AVAILABLE:
    faiss_status_reason = "faiss_dependencies_unavailable"
elif not FAISS_INDEX_PATH.exists():
    faiss_status_reason = f"missing_index:{FAISS_INDEX_PATH}"

if FAISS_AVAILABLE and SUPPLEMENTAL_VECTOR_ENABLED:
    supplemental_vector_source = _get_supplemental_vector_source_info()
    if supplemental_vector_source["available"]:
        try:
            print(f"Loading supplemental vector index from {supplemental_vector_source['path']}...", flush=True)
            raw_supplemental_index = faiss.read_index(str(supplemental_vector_source["path"]))
            supplemental_faiss_manifest = _load_supplemental_vector_manifest()
            validation_issues = _validate_supplemental_vector_manifest(raw_supplemental_index, supplemental_faiss_manifest)
            if validation_issues:
                supplemental_faiss_status_reason = "; ".join(validation_issues)
                print(
                    "Supplemental FAISS disabled by validation gate: "
                    f"{supplemental_faiss_status_reason}. Rebuild supplemental_textbook_pages.index with a matching manifest.",
                    flush=True,
                )
            else:
                supplemental_faiss_index = raw_supplemental_index
                if embedder is None:
                    embedder = _load_sentence_transformer(EMBEDDER_NAME)
                print(
                    f"Supplemental vector index loaded with {supplemental_faiss_index.ntotal} vectors. Model: {EMBEDDER_NAME}",
                    flush=True,
                )
        except Exception as e:
            supplemental_faiss_index = None
            supplemental_faiss_status_reason = str(e)
            print(f"Failed to load supplemental vector index: {e}", flush=True)
    else:
        supplemental_faiss_status_reason = f"missing_index:{SUPPLEMENTAL_VECTOR_INDEX_PATH}"
elif not FAISS_AVAILABLE:
    supplemental_faiss_status_reason = "faiss_dependencies_unavailable"
elif not SUPPLEMENTAL_VECTOR_ENABLED:
    supplemental_faiss_status_reason = "disabled_by_config"

# ── Jieba custom dictionary ──────────────────────────────────────────
try:
    import jieba
    _jieba_loaded = False
    def _load_jieba_userdict():
        global _jieba_loaded
        if _jieba_loaded:
            return
        try:
            con = sqlite3.connect(DB_PATH)
            rows = con.execute("SELECT term FROM curated_keywords").fetchall()
            con.close()
            for r in rows:
                jieba.add_word(r[0], freq=10000)  # high freq = never split
            _jieba_loaded = True
            print(f"Jieba: loaded {len(rows)} curated terms as user dict", flush=True)
        except Exception as e:
            print(f"Jieba dict load failed: {e}", flush=True)
    _load_jieba_userdict()
except ImportError:
    pass


from textbook_config import (
    CANONICAL_SUBJECT_META,
    SUBJECT_ALIASES,
    EDITION_PATTERNS,
    SERIES_PATTERNS,
    normalize_subject,
    display_subject as _display_subject,
    subject_meta as _subject_meta_for_phase,
    edition_ok,
    catalog_visible,
    search_enabled,
    page_image_enabled,
)

# Back-compat alias: old code references SUBJECT_META
SUBJECT_META = CANONICAL_SUBJECT_META

REAL_CONTENT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27}$", re.IGNORECASE)


def get_db():
    con = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        timeout=SQLITE_CONNECT_TIMEOUT_SEC,
    )
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return con


def _clean_query_text(query: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff\s]", "", (query or "")).strip()


def _compact_query_text(query: str) -> str:
    return re.sub(r"\s+", "", _clean_query_text(query))


def _query_characters(query: str) -> list[str]:
    return [ch for ch in _compact_query_text(query) if "\u4e00" <= ch <= "\u9fff"]


def _unique_query_characters(query: str) -> list[str]:
    seen = set()
    chars = []
    for ch in _query_characters(query):
        if ch in seen:
            continue
        seen.add(ch)
        chars.append(ch)
    return chars


def _is_single_hanzi_query(query: str) -> bool:
    compact = _compact_query_text(query)
    return len(compact) == 1 and bool(_query_characters(compact))


def _db_cache_token() -> tuple[str, int, int]:
    try:
        stat = DB_PATH.stat()
        return (str(DB_PATH), stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        return (str(DB_PATH), -1, -1)


def _normalize_match_text(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _contains_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def _segment_text_tokens(text: str) -> list[str]:
    normalized = _normalize_match_text(text)
    if not normalized:
        return []
    if "jieba" in globals():
        try:
            return [token.strip() for token in jieba.cut(normalized) if token and token.strip()]
        except Exception:
            pass
    return re.findall(r"[A-Za-z0-9+\-./]+|[\u0370-\u03ff]+|[\u4e00-\u9fff]+", normalized)


def _build_token_phrases(tokens: list[str], max_window: int = MATCH_PHRASE_MAX_WINDOW) -> set[str]:
    phrases = set()
    token_count = len(tokens)
    for start in range(token_count):
        merged = ""
        for end in range(start, min(token_count, start + max_window)):
            merged += tokens[end]
            if merged:
                phrases.add(merged)
    return phrases


def _build_text_match_context(text: str | None) -> tuple[str, set[str]]:
    normalized_text = _normalize_match_text(text)
    return normalized_text, _build_token_phrases(_segment_text_tokens(normalized_text))


@functools.lru_cache(maxsize=2048)
def _compile_non_cjk_term_pattern(term: str):
    return re.compile(rf"(?<![0-9A-Za-z]){re.escape(term)}(?![0-9A-Za-z])", re.IGNORECASE)


def _concept_matches_text(concept: str, normalized_text: str, phrase_set: set[str]) -> bool:
    if not concept:
        return False
    if _contains_chinese(concept):
        return concept in phrase_set
    return bool(_compile_non_cjk_term_pattern(concept).search(normalized_text))


def _is_synthetic_query(query: str | None) -> bool:
    raw = str(query or "").strip()
    normalized = re.sub(r"[\s_/\-]+", "", raw.lower())
    if not normalized:
        return False
    if any(marker in normalized for marker in TRENDING_QUERY_MARKERS):
        return True
    return bool(TRENDING_QUERY_EXCLUDE_RE.search(raw))


def _current_query_result_count(con: sqlite3.Connection, query: str, *, phase: str | None = None, cap: int = 200) -> int:
    clean_q = str(query or "").strip()
    if not clean_q:
        return 0
    like_pattern = f"%{clean_q}%"
    phase_filter = " AND c.phase = ?" if phase else ""
    phase_filter_plain = " AND phase = ?" if phase else ""
    phase_params: list = [phase] if phase else []
    try:
        row = con.execute(
            f"""
                SELECT COUNT(*) AS cnt
                FROM (
                    SELECT c.id
                    FROM chunks c
                    JOIN chunks_fts ON chunks_fts.rowid = c.id
                    WHERE c.text IS NOT NULL AND c.text != '' AND chunks_fts MATCH ?{phase_filter}
                    UNION
                    SELECT id
                    FROM chunks
                    WHERE text LIKE ?{phase_filter_plain}
                    LIMIT ?
                )
            """,
            (clean_q, *phase_params, like_pattern, *phase_params, cap),
        ).fetchone()
        return int(row["cnt"] or 0) if row else 0
    except Exception:
        row = con.execute(
            f"""
                SELECT COUNT(*) AS cnt
                FROM (
                    SELECT id
                    FROM chunks
                    WHERE text LIKE ?{phase_filter_plain}
                    LIMIT ?
                )
            """,
            (like_pattern, *phase_params, cap),
        ).fetchone()
        return int(row["cnt"] or 0) if row else 0


def _normalize_english_analytics_token(token: str) -> str:
    compact = unicodedata.normalize("NFKC", token or "").strip().strip("-'")
    compact = re.sub(r"'s$", "", compact, flags=re.IGNORECASE)
    compact = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", compact)
    if not compact:
        return ""
    upper = compact.upper()
    if upper in ENGLISH_ANALYTICS_ACRONYMS:
        return upper
    return compact.lower()


def _is_valid_english_analytics_token(token: str) -> bool:
    if not token:
        return False
    if token.upper() in ENGLISH_ANALYTICS_ACRONYMS:
        return True
    if not re.fullmatch(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", token):
        return False
    if len(token) < 3 or len(token) > 32:
        return False
    return token.lower() not in ENGLISH_ANALYTICS_STOPWORDS


def _extract_english_analytics_tokens(text: str | None) -> list[tuple[str, str]]:
    cleaned_text = _clean_english_analytics_text(text)
    if not cleaned_text:
        return []
    pairs: list[tuple[str, str]] = []
    for raw in re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", cleaned_text):
        canonical = _normalize_english_analytics_token(raw)
        if _is_valid_english_analytics_token(canonical):
            pairs.append((canonical, raw.strip()))
    return pairs


def _clean_english_analytics_text(text: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    if not normalized:
        return ""

    normalized = re.sub(r"!\[[^\]]*]\([^)]*\)", " ", normalized)
    normalized = re.sub(r"\bimages/[A-Za-z0-9._/-]+\b", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp|gif)\b", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\$[^$\n]{0,160}\$", " ", normalized)
    normalized = re.sub(r"\\[A-Za-z]+", " ", normalized)

    cleaned_lines: list[str] = []
    for raw_line in re.split(r"[\r\n]+", normalized):
        line = " ".join(raw_line.split())
        if not line:
            continue

        words = re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", line)
        if not words:
            continue

        if len(re.findall(r"\bpp?\.\s*\d", line, flags=re.IGNORECASE)) >= 2:
            continue
        if _contains_chinese(line) and (
            re.search(r"/[^/\n]{1,40}/", line)
            or re.search(r"\b(?:adj|adv|prep|pron|conj|noun|verb|phrase|phr|vt|vi)\b", line, flags=re.IGNORECASE)
        ):
            continue

        upper_words = sum(1 for word in words if len(word) > 1 and word.isupper())
        if len(words) >= 6 and upper_words / len(words) >= 0.6 and not re.search(r"[.!?]", line):
            continue
        if len(re.findall(r"\b(?:UNIT|Unit|LESSON|Lesson)\b", line)) >= 2:
            continue

        cleaned_lines.append(line)

    return " ".join(cleaned_lines)


def _english_source_where(source: str, *, phase: str | None = None) -> tuple[str, tuple]:
    parts = ["subject = ?"]
    params: list = ["英语"]
    if source == "gaokao":
        parts.append("source = 'gaokao'")
    elif source == "textbook":
        parts.append("(source = 'mineru' OR source IS NULL)")
    parts.append("text IS NOT NULL AND text != ''")
    if phase:
        parts.append("phase = ?")
        params.append(phase)
    return " AND ".join(parts), tuple(params)


def _pick_english_display_term(canonical: str, surface_counts: Counter | None) -> str:
    if canonical.upper() in ENGLISH_ANALYTICS_ACRONYMS:
        return canonical.upper()
    if not surface_counts:
        return canonical
    surface = max(
        surface_counts.items(),
        key=lambda item: (item[1], any(ch.isupper() for ch in item[0]), len(item[0])),
    )[0]
    if surface and surface[0].isupper():
        return surface
    return canonical


@functools.lru_cache(maxsize=8)
def _build_english_term_stats(db_token: tuple[str, int, int], source: str, *, phase: str | None = None) -> dict:
    if db_token[1] < 0:
        return {"terms": [], "pairs": [], "subjects": ["英语"]}

    where_sql, params = _english_source_where(source, phase=phase)
    con = get_db()
    try:
        rows = con.execute(f"SELECT text FROM chunks WHERE {where_sql}", params).fetchall()
    finally:
        con.close()

    term_counts: Counter = Counter()
    doc_counts: Counter = Counter()
    surface_counts: dict[str, Counter] = defaultdict(Counter)
    pair_counts: Counter = Counter()
    total_docs = 0

    for row in rows:
        token_pairs = _extract_english_analytics_tokens(row["text"] or "")
        if not token_pairs:
            continue
        total_docs += 1
        doc_counter: Counter = Counter()
        for canonical, surface in token_pairs:
            term_counts[canonical] += 1
            doc_counter[canonical] += 1
            surface_counts[canonical][surface] += 1
        for canonical in doc_counter:
            doc_counts[canonical] += 1
        doc_terms = [term for term, _ in doc_counter.most_common(ENGLISH_ANALYTICS_MAX_DOC_TERMS)]
        for idx, left in enumerate(doc_terms):
            for right in doc_terms[idx + 1:]:
                pair_counts[tuple(sorted((left, right)))] += 1

    terms = []
    for canonical, count in term_counts.items():
        doc_freq = int(doc_counts[canonical] or 0)
        if count < ENGLISH_ANALYTICS_MIN_TERM_COUNT or doc_freq < ENGLISH_ANALYTICS_MIN_TERM_COUNT:
            continue
        if total_docs and doc_freq / total_docs > ENGLISH_ANALYTICS_MAX_DOC_SHARE:
            continue
        terms.append(
            {
                "canonical": canonical,
                "term": _pick_english_display_term(canonical, surface_counts.get(canonical)),
                "count": int(count),
                "docs": doc_freq,
            }
        )

    terms.sort(key=lambda item: (-item["count"], -item["docs"], item["term"].lower()))
    allowed = {item["canonical"] for item in terms[: max(40, min(len(terms), 120))]}
    pairs = [
        {"source": left, "target": right, "weight": int(weight)}
        for (left, right), weight in pair_counts.items()
        if weight >= 2 and left in allowed and right in allowed
    ]
    pairs.sort(key=lambda item: (-item["weight"], item["source"], item["target"]))
    return {"terms": terms, "pairs": pairs, "subjects": ["英语"]}


def _present_terms_in_text(
    terms: list[str],
    text: str,
    *,
    normalized_text: str | None = None,
    phrase_set: set[str] | None = None,
) -> list[str]:
    if normalized_text is None or phrase_set is None:
        normalized_text, phrase_set = _build_text_match_context(text)
    present = []
    for term in terms:
        if _concept_matches_text(term, normalized_text, phrase_set):
            present.append(term)
    return present


def _candidate_window_limit(limit: int, offset: int = 0, *, multiplier: int = 2, minimum: int = 24, cap: int = 1000) -> int:
    return min(cap, max(minimum, (offset + limit) * multiplier))


def _stable_sort_id(value) -> str:
    return str(value or "")


def _ranked_row_sort_key(item: dict, sort: str = "relevance") -> tuple:
    if sort == "images":
        return (-((item.get("text") or "").count("![")), item.get("rank", 0), _stable_sort_id(item.get("id")))
    return (item.get("rank", 0), _stable_sort_id(item.get("id")))


def _merge_ranked_rows(*row_groups, sort: str = "relevance") -> list[dict]:
    merged = []
    existing_ids = set()
    for group in row_groups:
        for row in group:
            data = row if isinstance(row, dict) else dict(row)
            row_id = data.get("id")
            if row_id in existing_ids:
                continue
            existing_ids.add(row_id)
            merged.append(data)
    merged.sort(key=lambda item: _ranked_row_sort_key(item, sort))
    return merged


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
    # Check canonical subjects first
    for subject_name in CANONICAL_SUBJECT_META:
        if subject_name in normalized:
            return subject_name
    # Check aliases
    for alias, canonical in SUBJECT_ALIASES.items():
        if alias in normalized:
            return canonical
    if "习近平新时代中国特色社会主义思想学生读本" in normalized:
        return "思想政治"
    return ""


def _parse_content_id_from_text(text: str | None) -> str:
    match = re.search(r"([0-9a-f]{8}-[0-9a-f\-]{27})", text or "", re.IGNORECASE)
    return match.group(1) if match else ""


def _is_real_content_id(value: str | None) -> bool:
    return bool(REAL_CONTENT_ID_RE.fullmatch(str(value or "").strip()))


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
    probe = "\n".join(part for part in (display_title, str(path), text_probe) if part)
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
    normalized_subject = str(subject or "").strip()
    normalized_edition = str(edition or "").strip()
    return edition_ok(phase, normalized_subject, normalized_edition)


def _normalize_textbook_version_manifest(payload) -> dict:
    if not isinstance(payload, dict):
        return {"by_content_id": {}, "by_book_key": {}}
    if "by_content_id" in payload or "by_book_key" in payload:
        by_content_id = payload.get("by_content_id") if isinstance(payload.get("by_content_id"), dict) else {}
        by_book_key = payload.get("by_book_key") if isinstance(payload.get("by_book_key"), dict) else {}
        return {"by_content_id": by_content_id, "by_book_key": by_book_key}
    by_content_id = {k: v for k, v in payload.items() if isinstance(v, dict)}
    by_book_key = {}
    for item in by_content_id.values():
        book_key = str(item.get("book_key") or "").strip()
        if book_key:
            by_book_key[book_key] = item
    return {"by_content_id": by_content_id, "by_book_key": by_book_key}


def _book_version_manifest_row(book_key: str | None = None, content_id: str | None = None) -> dict:
    normalized_book_key = str(book_key or "").strip()
    normalized_content_id = str(content_id or "").strip()
    if normalized_book_key:
        row = _book_version_manifest.get("by_book_key", {}).get(normalized_book_key)
        if isinstance(row, dict):
            return row
    if _is_real_content_id(normalized_content_id):
        row = _book_version_manifest.get("by_content_id", {}).get(normalized_content_id)
        if isinstance(row, dict):
            return row
    return {}


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


@functools.lru_cache(maxsize=1)
def _load_textbook_registry() -> dict:
    con = get_db()
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
    by_title_subject = defaultdict(list)
    by_title = defaultdict(list)
    page_lookup = {}

    for row in rows:
        book_key = str(row["book_key"] or "").strip()
        content_id = str(row["content_id"] or "").strip()
        book_info = _book_map.get(book_key, {}) if book_key else {}
        manifest_row = _book_version_manifest_row(book_key=book_key, content_id=content_id)
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

    return {
        "by_content_id": by_content_id,
        "by_title_subject": {k: tuple(v) for k, v in by_title_subject.items()},
        "by_title": {k: tuple(v) for k, v in by_title.items()},
        "page_lookup": page_lookup,
        "book_map": _book_map,
    }


def _resolve_supplemental_book_meta(path: Path, payload: list[dict] | None = None) -> dict:
    registry = _load_textbook_registry()
    raw_title = path.stem
    display_title = raw_title
    display_title = re.sub(r"_content_list$", "", display_title)
    display_title = re.sub(r"_智慧中小学_[0-9a-f\-]{36}$", "", display_title, flags=re.IGNORECASE)
    display_title = re.sub(r"^(高中|初中)_[^_]+_", "", display_title)
    display_title = display_title.replace("_", " ").strip()

    content_id = _parse_content_id_from_text(str(path))
    subject_name = _parse_subject_from_title(display_title)
    if not subject_name:
        subject_name = _parse_subject_from_title(str(path))
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
        "has_page_images": synthetic_has_page_images,
        "primary_bound": False,
        "supported": _is_supported_runtime_edition(subject_name, edition_hint),
        "synthetic": True,
    }


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


@functools.lru_cache(maxsize=1)
def _load_supplemental_manifest() -> dict:
    for candidate in (SUPPLEMENTAL_TEXTBOOK_MANIFEST_PATH, BUNDLED_SUPPLEMENTAL_TEXTBOOK_MANIFEST_PATH):
        if not candidate.exists():
            continue
        try:
            with candidate.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
                if isinstance(payload, dict):
                    return payload
        except Exception:
            continue
    return {}


def _get_supplemental_source_info() -> dict:
    if SUPPLEMENTAL_TEXTBOOK_INDEX_GZ_PATH.exists():
        return {"available": True, "source": "runtime_index_gzip", "path": SUPPLEMENTAL_TEXTBOOK_INDEX_GZ_PATH}
    if SUPPLEMENTAL_TEXTBOOK_INDEX_PATH.exists():
        return {"available": True, "source": "runtime_index_jsonl", "path": SUPPLEMENTAL_TEXTBOOK_INDEX_PATH}
    if BUNDLED_SUPPLEMENTAL_TEXTBOOK_INDEX_GZ_PATH.exists():
        return {"available": True, "source": "bundled_index_gzip", "path": BUNDLED_SUPPLEMENTAL_TEXTBOOK_INDEX_GZ_PATH}
    if BUNDLED_SUPPLEMENTAL_TEXTBOOK_INDEX_PATH.exists():
        return {"available": True, "source": "bundled_index_jsonl", "path": BUNDLED_SUPPLEMENTAL_TEXTBOOK_INDEX_PATH}
    if SUPPLEMENTAL_TEXTBOOK_ROOT.exists():
        return {"available": True, "source": "directory", "path": SUPPLEMENTAL_TEXTBOOK_ROOT}
    return {"available": False, "source": "absent", "path": None}


def _get_raw_textbook_ocr_source_info() -> dict:
    for path in RAW_TEXTBOOK_OCR_INDEX_CANDIDATES:
        if path.exists():
            return {"available": True, "source": "runtime_jsonl", "path": path}
    return {"available": False, "source": "absent", "path": None}


def _normalize_supplemental_page_entry(entry: dict) -> dict | None:
    if not isinstance(entry, dict):
        return None
    subject = str(entry.get("subject") or "").strip()
    title = str(entry.get("title") or "").strip()
    base_title = str(entry.get("base_title") or title).strip()
    text = _normalize_text_line(entry.get("text"))
    if not subject or not title or len(text) < 20:
        return None
    try:
        section = int(entry.get("section"))
    except Exception:
        return None
    logical_page = entry.get("logical_page")
    try:
        logical_page_int = int(logical_page) if logical_page is not None else section
    except Exception:
        logical_page_int = section
    stable_id = str(entry.get("id") or "").strip()
    if not stable_id:
        stable_id = f"supp:{hashlib.md5(f'{subject}:{title}:{section}'.encode('utf-8')).hexdigest()[:16]}"
    # Infer phase from title: "义务教育" → 初中, default 高中
    raw_phase = str(entry.get("phase") or "").strip()
    if not raw_phase:
        if "义务教育" in title or "初中" in title:
            raw_phase = "初中"
        else:
            raw_phase = "高中"
    return {
        "id": stable_id,
        "content_id": str(entry.get("content_id") or "").strip() or None,
        "subject": subject,
        "title": title,
        "base_title": base_title,
        "edition": str(entry.get("edition") or "").strip(),
        "book_key": str(entry.get("book_key") or "").strip() or None,
        "phase": raw_phase,
        "section": section,
        "logical_page": logical_page_int,
        "text": text,
        "normalized_text": _compact_query_text(text),
        "path": str(entry.get("path") or "").strip() or None,
        "has_page_images": bool(entry.get("has_page_images")),
        "primary_bound": bool(entry.get("primary_bound")),
        "supported": bool(entry.get("supported", True)),
        "synthetic": bool(entry.get("synthetic")),
    }


def _normalize_raw_ocr_entry(entry: dict) -> dict | None:
    if not isinstance(entry, dict):
        return None
    subject = str(entry.get("subject") or "").strip()
    title = str(entry.get("title") or "").strip()
    text = _normalize_text_line(entry.get("text"))
    if not subject or not title or len(text) < 20:
        return None
    section_value = entry.get("section")
    if section_value is None:
        section_value = entry.get("page")
    try:
        section = int(section_value)
    except Exception:
        return None
    logical_page = entry.get("logical_page")
    try:
        logical_page_int = int(logical_page) if logical_page is not None else section
    except Exception:
        logical_page_int = section
    book_key = str(entry.get("book_key") or "").strip() or None
    content_id = str(entry.get("content_id") or "").strip() or None
    stable_basis = book_key or content_id or f"{subject}:{title}"
    stable_id = f"ocr:{hashlib.md5(f'{stable_basis}:{section}:{text[:120]}'.encode('utf-8')).hexdigest()[:16]}"
    return {
        "id": stable_id,
        "content_id": content_id,
        "subject": subject,
        "title": title,
        "book_key": book_key,
        "section": section,
        "logical_page": logical_page_int,
        "text": text,
        "normalized_text": _compact_query_text(text),
        "source": str(entry.get("source") or "mineru_ocr").strip() or "mineru_ocr",
        "path": str(entry.get("path") or "").strip() or None,
    }


@functools.lru_cache(maxsize=1)
def _load_supplemental_textbook_pages() -> tuple[dict, ...]:
    source_info = _get_supplemental_source_info()
    if not source_info["available"]:
        return tuple()

    page_entries = []
    if source_info["source"] in {"runtime_index_gzip", "runtime_index_jsonl", "bundled_index_gzip", "bundled_index_jsonl"}:
        opener = gzip.open if source_info["source"].endswith("_gzip") else open
        try:
            with opener(source_info["path"], "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    normalized = _normalize_supplemental_page_entry(item)
                    if normalized:
                        page_entries.append(normalized)
        except Exception:
            return tuple()
    else:
        registry = _load_textbook_registry()
        page_entries_by_key: dict[tuple[str, int], dict] = {}
        for path in sorted(SUPPLEMENTAL_TEXTBOOK_ROOT.rglob("*_content_list.json")):
            lowered = str(path).lower()
            if "test_" in lowered or "/test" in lowered:
                continue
            try:
                with path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception:
                continue
            if not isinstance(payload, list):
                continue
            meta = _resolve_supplemental_book_meta(path, payload)
            if not meta.get("subject") or not meta.get("supported"):
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

            for page_num, blocks in blocks_by_page.items():
                merged_text = _merge_supplemental_page_blocks(blocks)
                if len(merged_text) < 20:
                    continue
                book_key = meta.get("book_key")
                if meta.get("primary_bound"):
                    continue
                logical_page = registry["page_lookup"].get((book_key, page_num)) if meta.get("primary_bound") else None
                normalized = _normalize_supplemental_page_entry(
                    {
                        "id": f"supp:{hashlib.md5(f'{book_key}:{page_num}'.encode('utf-8')).hexdigest()[:16]}",
                        "content_id": meta.get("content_id"),
                        "subject": meta.get("subject"),
                        "title": meta.get("title"),
                        "base_title": meta.get("base_title") or meta.get("title"),
                        "edition": meta.get("edition"),
                        "book_key": book_key,
                        "section": int(page_num),
                        "logical_page": int(logical_page) if logical_page is not None else int(page_num),
                        "text": merged_text,
                        "path": str(path),
                        "has_page_images": bool(meta.get("has_page_images")),
                        "primary_bound": bool(meta.get("primary_bound")),
                        "supported": bool(meta.get("supported")),
                        "synthetic": bool(meta.get("synthetic")),
                    }
                )
                if normalized:
                    normalized["_quality_score"] = _page_text_quality(normalized.get("text") or "")
                    page_entries_by_key[(str(book_key or ""), int(page_num))] = _pick_better_page(
                        page_entries_by_key.get((str(book_key or ""), int(page_num))),
                        normalized,
                    )
        page_entries.extend(page_entries_by_key.values())

    page_entries.sort(
        key=lambda item: (
            item.get("subject") or "",
            item.get("title") or "",
            int(item.get("section") or 0),
        )
    )
    normalized_entries = []
    for item in page_entries:
        item.pop("_quality_score", None)
        normalized_entries.append(item)
    return tuple(normalized_entries)


@functools.lru_cache(maxsize=1)
def _load_raw_textbook_ocr_pages() -> tuple[dict, ...]:
    source_info = _get_raw_textbook_ocr_source_info()
    if not source_info["available"]:
        return tuple()

    pages_by_key: dict[tuple[str, int], dict] = {}
    try:
        with source_info["path"].open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                normalized = _normalize_raw_ocr_entry(item)
                if not normalized:
                    continue
                normalized["_quality_score"] = _page_text_quality(normalized.get("text") or "")
                page_key = (
                    str(normalized.get("book_key") or normalized.get("content_id") or normalized.get("title") or ""),
                    int(normalized.get("section") or 0),
                )
                pages_by_key[page_key] = _pick_better_page(pages_by_key.get(page_key), normalized)
    except Exception:
        return tuple()

    pages = list(pages_by_key.values())
    pages.sort(
        key=lambda item: (
            item.get("subject") or "",
            item.get("title") or "",
            int(item.get("section") or 0),
        )
    )
    for item in pages:
        item.pop("_quality_score", None)
    return tuple(pages)


@functools.lru_cache(maxsize=1)
def _load_supplemental_book_catalog() -> tuple[dict, ...]:
    manifest = _load_supplemental_manifest()
    catalog = manifest.get("book_catalog") if isinstance(manifest, dict) else None
    if isinstance(catalog, list) and catalog:
        normalized = []
        for item in catalog:
            if not isinstance(item, dict):
                continue
            book_key = str(item.get("book_key") or "").strip()
            subject = str(item.get("subject") or "").strip()
            title = str(item.get("title") or "").strip()
            if not book_key or not subject or not title:
                continue
            normalized.append(
                {
                    "book_key": book_key,
                    "phase": str(item.get("phase") or "高中").strip(),
                    "subject": subject,
                    "title": title,
                    "base_title": str(item.get("base_title") or title).strip() or title,
                    "edition": str(item.get("edition") or "").strip(),
                    "content_id": str(item.get("content_id") or "").strip() or None,
                    "has_page_images": bool(item.get("has_page_images")),
                    "primary_bound": bool(item.get("primary_bound")),
                    "supported": bool(item.get("supported", True)),
                    "source": str(item.get("source") or ("primary_bound" if item.get("primary_bound") else "supplemental_only")).strip(),
                    "pages": int(item.get("pages") or 0),
                }
            )
        if normalized:
            return tuple(normalized)

    by_book = {}
    for entry in _load_supplemental_textbook_pages():
        book_key = str(entry.get("book_key") or "").strip()
        if not book_key:
            continue
        item = by_book.setdefault(
            book_key,
            {
                "book_key": book_key,
                "subject": entry.get("subject") or "",
                "title": entry.get("title") or "",
                "base_title": entry.get("base_title") or entry.get("title") or "",
                "edition": entry.get("edition") or "",
                "content_id": entry.get("content_id"),
                "has_page_images": bool(entry.get("has_page_images")),
                "primary_bound": bool(entry.get("primary_bound")),
                "supported": bool(entry.get("supported", True)),
                "source": "primary_bound" if entry.get("primary_bound") else "supplemental_only",
                "pages": 0,
            },
        )
        item["pages"] += 1
    return tuple(
        sorted(
            by_book.values(),
            key=lambda item: (item.get("subject") or "", item.get("title") or "", item.get("book_key") or ""),
        )
    )


@functools.lru_cache(maxsize=8)
def _load_supported_textbook_sets(phase: str | None = None) -> dict[str, set[str]]:
    supported_book_keys: set[str] = set()
    supported_content_ids: set[str] = set()

    for row in (_book_version_manifest.get("by_book_key") or {}).values():
        if not isinstance(row, dict):
            continue
        subject = str(row.get("subject") or "").strip()
        edition = str(row.get("edition") or "").strip()
        row_phase = str(row.get("phase") or "高中").strip()
        if phase and row_phase != phase:
            continue
        if not _is_supported_runtime_edition(subject, edition, phase=row_phase):
            continue
        book_key = str(row.get("book_key") or "").strip()
        content_id = str(row.get("content_id") or "").strip()
        if book_key:
            supported_book_keys.add(book_key)
        if _is_real_content_id(content_id):
            supported_content_ids.add(content_id)

    for item in _load_supplemental_book_catalog():
        subject = str(item.get("subject") or "").strip()
        edition = str(item.get("edition") or "").strip()
        item_phase = str(item.get("phase") or "高中").strip()
        if phase and item_phase != phase:
            continue
        if not _is_supported_runtime_edition(subject, edition, phase=item_phase):
            continue
        book_key = str(item.get("book_key") or "").strip()
        content_id = str(item.get("content_id") or "").strip()
        if book_key:
            supported_book_keys.add(book_key)
        if _is_real_content_id(content_id):
            supported_content_ids.add(content_id)

    return {
        "book_keys": supported_book_keys,
        "content_ids": supported_content_ids,
    }


def _is_supported_textbook_book(
    *,
    book_key: str | None = None,
    content_id: str | None = None,
    subject: str | None = None,
    edition: str | None = None,
    phase: str | None = None,
) -> bool:
    if _is_supported_runtime_edition(subject, edition, phase=phase or "高中"):
        return True
    supported_sets = _load_supported_textbook_sets(phase=phase)
    normalized_book_key = str(book_key or "").strip()
    normalized_content_id = str(content_id or "").strip()
    if normalized_book_key and normalized_book_key in supported_sets["book_keys"]:
        return True
    if _is_real_content_id(normalized_content_id) and normalized_content_id in supported_sets["content_ids"]:
        return True
    # Fallback: detect edition from book_key when not in manifest
    if normalized_book_key and not edition:
        detected = _detect_edition_label(normalized_book_key, Path(), "")
        if detected and _is_supported_runtime_edition(subject, detected, phase=phase or "高中"):
            return True
    return False


def _count_textbook_term_hits(
    con: sqlite3.Connection,
    term: str,
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
) -> int:
    clean_term = _compact_query_text(term)
    if not clean_term:
        return 0
    where_parts = ["(source = 'mineru' OR source IS NULL)", "text LIKE ?"]
    params = [f"%{clean_term}%"]
    if book_key:
        where_parts.append("book_key = ?")
        params.append(book_key)
    elif scope_subject:
        where_parts.append("subject = ?")
        params.append(scope_subject)
    try:
        row = con.execute(
            f"SELECT COUNT(*) AS cnt FROM chunks WHERE {' AND '.join(where_parts)}",
            params,
        ).fetchone()
        return int(row["cnt"] or 0) if row else 0
    except Exception:
        return 0


def _count_supplemental_term_hits(
    term: str,
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
    cap: int = 24,
) -> int:
    compact_term = _compact_query_text(term)
    if not compact_term:
        return 0
    hits = 0
    for entry in _load_supplemental_textbook_pages():
        if book_key and entry.get("book_key") != book_key:
            continue
        if scope_subject and entry.get("subject") != scope_subject:
            continue
        if compact_term in entry.get("normalized_text", ""):
            hits += 1
            if hits >= cap:
                break
    return hits


def _count_raw_ocr_term_hits(
    term: str,
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
    cap: int = 24,
) -> int:
    compact_term = _compact_query_text(term)
    if not compact_term:
        return 0
    hits = 0
    for entry in _load_raw_textbook_ocr_pages():
        if book_key and entry.get("book_key") != book_key:
            continue
        if scope_subject and entry.get("subject") != scope_subject:
            continue
        if compact_term in entry.get("normalized_text", ""):
            hits += 1
            if hits >= cap:
                break
    return hits


def _supplemental_whole_term_hit(query_text: str, candidate: dict) -> bool:
    compact_query = _compact_query_text(query_text)
    reference = _compact_query_text(
        "\n".join(
            part for part in (
                candidate.get("matched_term") or "",
                candidate.get("snippet") or "",
                candidate.get("text") or "",
            ) if part
        )
    )
    if not reference:
        return False
    if compact_query and len(compact_query) >= 2 and compact_query in reference:
        return True
    matched_term = _compact_query_text(candidate.get("matched_term") or "")
    return len(matched_term) >= 2 and matched_term in reference


def _supplemental_candidate_passes_guardrails(query_text: str, candidate: dict) -> bool:
    if candidate.get("retrieval_source") != "supplemental":
        return True
    # Users search for concept-bearing terms, not arbitrary character overlap.
    if candidate.get("match_basis") == "character_fallback":
        return False
    if _supplemental_whole_term_hit(query_text, candidate):
        return True
    return (
        candidate.get("snippet_source") == "sentence"
        and float(candidate.get("evidence_score") or candidate.get("evidence_bonus") or 0.0) >= 18.0
    )


def _derive_query_candidate_terms(query: str, *, limit: int = 18) -> list[str]:
    compact = _compact_query_text(query)
    if not compact:
        return []

    candidates = []
    seen = set()

    def add_term(value: str):
        term = _compact_query_text(value)
        if len(term) < 2:
            return
        if term in seen:
            return
        seen.add(term)
        candidates.append(term)

    add_term(compact)

    token_candidates = []
    try:
        import jieba  # type: ignore

        token_candidates = [token.strip() for token in jieba.cut(compact) if token and token.strip()]
    except Exception:
        token_candidates = []

    if len(token_candidates) >= 2:
        for window in range(min(4, len(token_candidates)), 0, -1):
            for start in range(0, len(token_candidates) - window + 1):
                add_term("".join(token_candidates[start:start + window]))
    elif token_candidates and len(token_candidates) == 1 and _compact_query_text(token_candidates[0]) == compact:
        return candidates[:limit]
    elif _contains_chinese(compact) and len(compact) >= 4:
        max_len = min(8, len(compact))
        min_len = 2 if len(compact) <= 4 else 3
        for length in range(max_len, min_len - 1, -1):
            for start in range(0, len(compact) - length + 1):
                add_term(compact[start:start + length])

    return candidates[:limit]


def _analyze_search_query(
    con: sqlite3.Connection,
    query: str,
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
) -> dict:
    clean_q = _compact_query_text(query)
    if not clean_q:
        return {
            "query": query,
            "mode": "invalid",
            "summary": "检索词无效。",
            "concept_terms": [],
            "fallback_terms": [],
            "retrieval_terms": [],
            "display_term": "",
            "used_supplemental_fallback": False,
        }

    concept_matches = []
    catalog = _get_concept_catalog(_db_cache_token())
    normalized_query, query_phrase_set = _build_text_match_context(clean_q)
    for concept, subjects, _has_chinese in catalog:
        if scope_subject and subjects and scope_subject not in subjects:
            continue
        if concept == clean_q:
            score = 300 + len(concept)
            match_type = "exact"
        elif concept in clean_q:
            score = 220 + len(concept)
            match_type = "contained"
        elif clean_q in concept:
            score = 200 + len(clean_q)
            match_type = "expanded"
        elif _concept_matches_text(concept, normalized_query, query_phrase_set):
            score = 180 + len(concept)
            match_type = "phrase"
        else:
            continue
        concept_matches.append(
            {
                "term": concept,
                "match_type": match_type,
                "subjects": sorted(subjects.keys()),
                "source": "concept_map",
                "score": score,
            }
        )

    try:
        synonym_rows = con.execute(
            """
            SELECT term, synonyms
            FROM ai_synonyms
            WHERE term = ? OR term LIKE ? OR synonyms LIKE ?
            LIMIT 24
            """,
            (clean_q, f"%{clean_q}%", f"%{clean_q}%"),
        ).fetchall()
    except Exception:
        synonym_rows = []

    alias_matches = []
    for row in synonym_rows:
        term = _compact_query_text(row["term"])
        if not term:
            continue
        record = _load_json_object(row["synonyms"])
        aliases = _collect_synonym_aliases(record, limit=8)
        matched_aliases = []
        for alias in aliases:
            alias_clean = _compact_query_text(alias)
            if not alias_clean:
                continue
            if alias_clean == clean_q or alias_clean in clean_q or clean_q in alias_clean:
                matched_aliases.append(alias)
        if term == clean_q or matched_aliases:
            alias_matches.append(
                {
                    "term": row["term"],
                    "match_type": "alias" if matched_aliases else "term",
                    "aliases": matched_aliases[:4],
                    "source": "ai_synonyms",
                    "score": 260 if term == clean_q else 210 + len(matched_aliases) * 10,
                }
            )

    concept_matches.sort(key=lambda item: (-item["score"], -len(item["term"]), item["term"]))
    alias_matches.sort(key=lambda item: (-item["score"], -len(item["term"]), item["term"]))

    fallback_scored = []
    for term in _derive_query_candidate_terms(clean_q):
        textbook_hits = _count_textbook_term_hits(
            con,
            term,
            scope_subject=scope_subject,
            book_key=book_key,
        )
        supplemental_hits = _count_supplemental_term_hits(
            term,
            scope_subject=scope_subject,
            book_key=book_key,
        )
        if textbook_hits <= 0 and supplemental_hits <= 0:
            continue
        total_hits = textbook_hits + supplemental_hits
        score = 0
        if term == clean_q:
            score += 240
        if term in clean_q:
            score += len(term) * 24 + 40
            if clean_q.startswith(term):
                score += 45
            if clean_q.endswith(term):
                score += 65
            if len(term) >= 4:
                score += 30
        else:
            score += len(term) * 12
        score += min(total_hits, 6) * 10
        if supplemental_hits > 0 and textbook_hits <= 0:
            score += 22
        if 0 < total_hits <= 4:
            score += (5 - total_hits) * 8
        if total_hits > 12:
            score -= min(60, (total_hits - 12) * 2)
        fallback_scored.append(
            {
                "term": term,
                "textbook_hits": textbook_hits,
                "supplemental_hits": supplemental_hits,
                "score": score,
            }
        )

    fallback_scored.sort(
        key=lambda item: (
            -item["score"],
            -(item["textbook_hits"] + item["supplemental_hits"]),
            -len(item["term"]),
            item["term"],
        )
    )

    retrieval_terms = []
    seen_terms = set()

    def add_retrieval_term(term: str):
        compact_term = _compact_query_text(term)
        if len(compact_term) < 2 or compact_term in seen_terms:
            return
        seen_terms.add(compact_term)
        retrieval_terms.append(compact_term)

    for item in concept_matches[:4]:
        add_retrieval_term(item["term"])
    for item in alias_matches[:4]:
        add_retrieval_term(item["term"])
        for alias in item.get("aliases", [])[:3]:
            add_retrieval_term(alias)
    plan_fallback_terms = fallback_scored[:QUERY_TERM_PLAN_LIMIT] if not concept_matches and not alias_matches else []
    for item in plan_fallback_terms:
        add_retrieval_term(item["term"])
    add_retrieval_term(clean_q)

    concept_terms = concept_matches[:4] + alias_matches[:4]
    fallback_terms = plan_fallback_terms
    used_supplemental_fallback = (
        bool(fallback_terms)
        and not any(item.get("textbook_hits", 0) > 0 for item in fallback_terms)
        and any(item.get("supplemental_hits", 0) > 0 for item in fallback_terms)
    )

    if concept_matches:
        summary = f"已按标准概念术语优先检索：{'、'.join(item['term'] for item in concept_matches[:3])}"
        mode = "concept"
        display_term = concept_matches[0]["term"]
    elif alias_matches:
        summary = f"已按概念别名归并检索：{'、'.join(item['term'] for item in alias_matches[:3])}"
        mode = "alias"
        display_term = alias_matches[0]["term"]
    elif fallback_terms:
        fallback_label = "、".join(item["term"] for item in fallback_terms[:3])
        if used_supplemental_fallback:
            summary = f"标准术语库未直接命中，已按备份教材原文兜底检索：{fallback_label}"
        else:
            summary = f"标准术语库未直接命中，已按教材原文短语兜底检索：{fallback_label}"
        mode = "fallback"
        display_term = fallback_terms[0]["term"]
    else:
        summary = f"未识别出标准概念术语，已按原检索词直接检索：{clean_q}"
        mode = "direct"
        display_term = clean_q

    return {
        "query": query,
        "mode": mode,
        "summary": summary,
        "concept_terms": concept_terms,
        "fallback_terms": fallback_terms,
        "retrieval_terms": retrieval_terms[:QUERY_TERM_PLAN_LIMIT],
        "display_term": display_term,
        "used_supplemental_fallback": used_supplemental_fallback,
    }


def _build_search_term_plan(query_analysis: dict) -> list[dict]:
    plan = []
    seen = set()

    def add_item(term: str, basis: str):
        compact_term = _compact_query_text(term)
        if len(compact_term) < 2 or compact_term in seen:
            return
        seen.add(compact_term)
        plan.append({"term": compact_term, "basis": basis})

    for item in query_analysis.get("concept_terms", []):
        add_item(item.get("term", ""), item.get("match_type") or "concept")
        for alias in item.get("aliases", [])[:3]:
            add_item(alias, "alias")
    for item in query_analysis.get("fallback_terms", [])[:QUERY_TERM_PLAN_LIMIT]:
        add_item(item.get("term", ""), "fallback")
    add_item(query_analysis.get("query", ""), "query")
    return plan[:QUERY_TERM_PLAN_LIMIT]


def _build_textbook_search_filters(
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
) -> tuple[str, list]:
    where_extra = " AND (c.source = 'mineru' OR c.source IS NULL)"
    filter_params: list = []
    if phase:
        where_extra += " AND c.phase = ?"
        filter_params.append(phase)
    if scope_subject:
        where_extra += " AND c.subject = ?"
        filter_params.append(scope_subject)
    if book_key:
        where_extra += " AND c.book_key = ?"
        filter_params.append(book_key)
    return where_extra, filter_params


def _collect_legacy_search_rows(
    con: sqlite3.Connection,
    query: str,
    query_analysis: dict,
    search_plan: list[dict],
    *,
    where_extra: str,
    filter_params: list,
    candidate_limit: int,
    sort: str,
    scope_subject: str | None = None,
    book_key: str | None = None,
    source: str | None = None,
    include_supplemental: bool = True,
    seen_ids: set | None = None,
    rank_shift: float = 0.0,
    phase: str | None = None,
) -> list[dict]:
    rows = []
    local_seen = seen_ids if seen_ids is not None else set()
    for priority, item in enumerate(search_plan):
        rows_for_term = _search_chunks_by_term(
            con,
            item["term"],
            where_extra=where_extra,
            filter_params=filter_params,
            candidate_limit=candidate_limit,
            sort=sort,
        )
        for row in rows_for_term:
            row_id = row.get("id")
            if row_id in local_seen:
                continue
            local_seen.add(row_id)
            merged = dict(row)
            merged["matched_term"] = item["term"]
            merged["match_basis"] = item["basis"]
            merged["retrieval_source"] = "primary"
            merged["rank"] = float(merged.get("rank", 0)) + priority * 0.001 + rank_shift
            rows.append(merged)

    supplemental_gap_terms = [
        item
        for item in (query_analysis.get("fallback_terms") or [])[:QUERY_TERM_PLAN_LIMIT]
        if int(item.get("supplemental_hits") or 0) > 0 and int(item.get("textbook_hits") or 0) <= 0
    ]
    supplemental_needed = include_supplemental and source != "gaokao" and (
        not rows
        or bool(supplemental_gap_terms)
        or bool(query_analysis.get("used_supplemental_fallback"))
    )
    if supplemental_needed:
        supplemental_rows = _search_supplemental_textbook_pages(
            query,
            search_plan,
            scope_subject=scope_subject,
            book_key=book_key,
            phase=phase,
            limit=max(SUPPLEMENTAL_FALLBACK_LIMIT, candidate_limit),
        )
        for row in supplemental_rows:
            row_id = row.get("id")
            if row_id in local_seen:
                continue
            if not _supplemental_candidate_passes_guardrails(query, row):
                continue
            local_seen.add(row_id)
            rows.append(row)

    return rows


def _search_chunks_by_term(
    con: sqlite3.Connection,
    term: str,
    *,
    where_extra: str,
    filter_params: list,
    candidate_limit: int,
    sort: str,
) -> list[dict]:
    clean_term = _clean_query_text(term)
    if not clean_term:
        return []

    like_params = [clean_term, f"%{clean_term}%"] + filter_params + [candidate_limit]
    like_rows = con.execute(
        f"""
        SELECT c.id, c.content_id, c.subject, c.title, c.book_key, c.section, c.logical_page,
               SUBSTR(c.text, MAX(1, INSTR(c.text, ?)-30), 160) AS snippet,
               c.text, c.source, c.year, c.category,
               -100.0 AS rank
        FROM chunks c
        WHERE c.text LIKE ? {where_extra}
        LIMIT ?
        """,
        like_params,
    ).fetchall()
    like_ranked = []
    for row in like_rows:
        data = dict(row)
        if not _is_supported_textbook_book(
            book_key=data.get("book_key"),
            content_id=data.get("content_id"),
            subject=data.get("subject"),
            edition=_book_version_manifest_row(
                book_key=data.get("book_key"),
                content_id=data.get("content_id"),
            ).get("edition"),
        ):
            continue
        data["snippet"] = data["snippet"].replace(clean_term, f"<mark>{clean_term}</mark>")
        data["match_channel"] = "exact"
        like_ranked.append(data)

    fts_params = [clean_term] + filter_params + [candidate_limit]
    order_clause = "ORDER BY rank"
    if sort == "images":
        order_clause = "ORDER BY (LENGTH(c.text) - LENGTH(REPLACE(c.text, '![', ''))) DESC, rank"

    try:
        fts_rows = con.execute(
            f"""
            SELECT c.id, c.content_id, c.subject, c.title, c.book_key, c.section, c.logical_page,
                   snippet(chunks_fts, 0, '<mark>', '</mark>', '…', 40) AS snippet,
                   c.text, c.source, c.year, c.category,
                   f.rank AS rank
            FROM chunks c
            JOIN chunks_fts f ON c.id = f.rowid
            WHERE chunks_fts MATCH ? {where_extra}
            {order_clause}
            LIMIT ?
            """,
            fts_params,
        ).fetchall()
    except Exception:
        fts_rows = []

    fts_ranked = []
    for row in fts_rows:
        data = dict(row)
        if not _is_supported_textbook_book(
            book_key=data.get("book_key"),
            content_id=data.get("content_id"),
            subject=data.get("subject"),
            edition=_book_version_manifest_row(
                book_key=data.get("book_key"),
                content_id=data.get("content_id"),
            ).get("edition"),
        ):
            continue
        data["match_channel"] = "fts"
        fts_ranked.append(data)
    return _merge_ranked_rows(like_ranked, fts_ranked, sort=sort)


def _search_supplemental_textbook_pages(
    query: str,
    plan: list[dict],
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
    limit: int = 80,
) -> list[dict]:
    compact_query = _compact_query_text(query)
    if not compact_query:
        return []

    results = []
    for entry in _load_supplemental_textbook_pages():
        if entry.get("primary_bound") or not entry.get("supported", True):
            continue
        if book_key and entry.get("book_key") != book_key:
            continue
        if scope_subject and entry.get("subject") != scope_subject:
            continue
        if phase and entry.get("phase") != phase:
            continue

        matched = None
        for priority, item in enumerate(plan):
            compact_term = _compact_query_text(item.get("term"))
            if not compact_term:
                continue
            hit_count = entry["normalized_text"].count(compact_term)
            if hit_count <= 0:
                continue
            score = 420 - priority * 24 + min(90, hit_count * 18) + min(24, len(compact_term) * 3)
            matched = {
                "term": compact_term,
                "basis": item.get("basis") or "fallback",
                "score": score,
                "hit_count": hit_count,
            }
            break

        if not matched:
            continue

        results.append(
            {
                "id": entry["id"],
                "content_id": entry.get("content_id"),
                "subject": entry["subject"],
                "title": entry["title"],
                "book_key": entry.get("book_key"),
                "section": entry["section"],
                "logical_page": entry.get("logical_page"),
                "snippet": _build_context_snippet(entry["text"], matched["term"]),
                "text": entry["text"],
                "source": "mineru",
                "year": None,
                "category": None,
                "rank": -matched["score"],
                "match_channel": "supplemental",
                "match_basis": matched["basis"],
                "matched_term": matched["term"],
                "retrieval_source": "supplemental",
            }
        )

    results.sort(key=lambda item: (item["rank"], item["subject"], item["title"], item["section"]))
    return results[:limit]


@functools.lru_cache(maxsize=1)
def _get_concept_catalog(db_token: tuple[str, int, int]) -> tuple[tuple[str, dict, bool], ...]:
    if db_token[1] < 0:
        return tuple()

    con = get_db()
    try:
        has_map = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='concept_map'"
        ).fetchone()
        if not has_map:
            return tuple()
        rows = con.execute(
            """
            SELECT concept, subject, SUM(count) AS total_count
            FROM concept_map
            WHERE length(concept) >= 2
               OR concept IN ('DNA', 'RNA', 'ATP', 'ADP', 'PCR')
            GROUP BY concept, subject
            """
        ).fetchall()
    finally:
        con.close()

    by_concept: dict[str, dict[str, int]] = {}
    for row in rows:
        concept = row["concept"]
        subjects = by_concept.setdefault(concept, {})
        subjects[row["subject"]] = int(row["total_count"] or 0)

    catalog = []
    for concept, subjects in by_concept.items():
        has_chinese = _contains_chinese(concept)
        if not has_chinese and concept.upper() not in ("DNA", "RNA", "ATP", "ADP", "PCR"):
            continue
        catalog.append((concept, subjects, has_chinese))
    catalog.sort(key=lambda item: len(item[0]), reverse=True)
    return tuple(catalog)


def _ensure_jieba_concepts_loaded(catalog: tuple[tuple[str, dict, bool], ...], db_token: tuple[str, int, int]) -> None:
    global _jieba_concept_token
    if "jieba" not in globals() or _jieba_concept_token == db_token:
        return
    try:
        for concept, _, has_chinese in catalog:
            if has_chinese:
                jieba.add_word(concept, freq=max(10000, len(concept) * 2000))
        _jieba_concept_token = db_token
    except Exception as e:
        print(f"Jieba concept dict load failed: {e}", flush=True)


def _chat_excerpt(text: str, limit: int = 280) -> str:
    cleaned = re.sub(r"!\[.*?\]\(.*?\)", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


def _load_json_object(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _normalize_text_line(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _normalize_multiline_text(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _load_dict_exam_dataset(kind: str) -> dict:
    path_map = {
        "xuci": DICT_EXAM_XUCI_PATH,
        "shici": DICT_EXAM_SHICI_PATH,
    }
    label_map = {
        "xuci": "真题虚词",
        "shici": "真题实词",
    }
    path = path_map.get(kind)
    if not path:
        return {
            "kind": kind,
            "available": False,
            "label": label_map.get(kind, kind),
            "coverage": {"notes": ["未知数据类型。"]},
            "stats": {"term_count": 0, "occurrence_count": 0, "question_count": 0},
            "terms": [],
        }

    if not path.exists():
        return {
            "kind": kind,
            "available": False,
            "label": label_map[kind],
            "coverage": {"notes": ["运行时数据尚未生成。"]},
            "stats": {"term_count": 0, "occurrence_count": 0, "question_count": 0},
            "terms": [],
        }

    try:
        cache_key = (kind, path.stat().st_mtime_ns)
    except Exception:
        cache_key = (kind, 0)

    try:
        cached = DICT_EXAM_DATASET_CACHE.get(cache_key)
    except Exception:
        cached = None
    if cached is not None:
        return cached

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {
            "kind": kind,
            "available": False,
            "label": label_map[kind],
            "coverage": {"notes": [f"运行时数据读取失败：{e}"]},
            "stats": {"term_count": 0, "occurrence_count": 0, "question_count": 0},
            "terms": [],
        }

    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("kind", kind)
    payload.setdefault("label", label_map[kind])
    payload.setdefault("available", bool(payload.get("terms")))
    payload.setdefault("coverage", {"notes": []})
    payload.setdefault("stats", {"term_count": 0, "occurrence_count": 0, "question_count": 0})
    payload.setdefault("question_docs", {})
    payload.setdefault("terms", [])

    try:
        DICT_EXAM_DATASET_CACHE[cache_key] = payload
    except Exception:
        pass
    return payload


def _load_dict_exam_xuci_details_payload() -> dict:
    path = DICT_EXAM_XUCI_DETAILS_PATH
    if not path.exists():
        return {"available": False, "terms": {}, "term_count": 0}

    try:
        cache_key = ("xuci_details", path.stat().st_mtime_ns)
    except Exception:
        cache_key = ("xuci_details", 0)

    try:
        cached = DICT_EXAM_DATASET_CACHE.get(cache_key)
    except Exception:
        cached = None
    if cached is not None:
        return cached

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {
            "available": False,
            "terms": {},
            "term_count": 0,
            "error": f"运行时数据读取失败：{e}",
        }

    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("available", bool(payload.get("terms")))
    payload.setdefault("terms", {})
    payload.setdefault("term_count", len(payload.get("terms") or {}))

    try:
        DICT_EXAM_DATASET_CACHE[cache_key] = payload
    except Exception:
        pass
    return payload


def _load_dict_exam_xuci_detail(headword: str) -> dict:
    clean_headword = _clean_query_text(headword)
    if not clean_headword:
        return {"available": False, "headword": headword, "detail": None}

    payload = _load_dict_exam_xuci_details_payload()
    terms = payload.get("terms") or {}
    detail = terms.get(clean_headword)
    if not isinstance(detail, dict):
        return {"available": False, "headword": clean_headword, "detail": None}
    return {
        "available": True,
        "headword": clean_headword,
        "detail": detail,
        "built_at": payload.get("built_at"),
    }


def _load_dict_exam_term(kind: str, headword: str) -> tuple[dict, dict | None]:
    payload = _load_dict_exam_dataset(kind)
    clean_headword = _clean_query_text(headword)
    if not clean_headword:
        return payload, None

    for item in payload.get("terms") or []:
        if not isinstance(item, dict):
            continue
        term_headword = _clean_query_text(item.get("headword"))
        if term_headword == clean_headword:
            return payload, item
    return payload, None


def _load_exam_question_row(con, title: str | None, year: int | None, category: str | None):
    clean_title = str(title or "").strip()
    if not clean_title:
        return None
    rows = con.execute(
        """
        SELECT id, title, year, category, text, answer
        FROM chunks
        WHERE source = 'gaokao'
          AND subject = '语文'
          AND title = ?
        ORDER BY year DESC, id DESC
        LIMIT 8
        """,
        (clean_title,),
    ).fetchall()
    if not rows:
        return None

    def _row_score(row) -> int:
        score = 0
        if clean_title and str(row["title"] or "").strip() == clean_title:
            score += 12
        if year is not None and row["year"] == year:
            score += 8
        if category and str(row["category"] or "").strip() == category:
            score += 4
        return score

    return max(rows, key=_row_score)


def _build_exam_question_fallback(occurrences: list[dict], headword: str) -> str:
    snippets = []
    seen = set()
    for item in occurrences:
        excerpt = _normalize_multiline_text(item.get("excerpt"))
        if not excerpt:
            continue
        compact = _compact_query_text(excerpt)
        if compact in seen:
            continue
        seen.add(compact)
        snippets.append(excerpt)
        if len(snippets) >= 6:
            break
    if not snippets:
        return f"当前仅保留到「{headword}」的题目片段，真题全文待补。"
    return "当前未命中真题全文缓存，以下为该题相关片段：\n\n" + "\n".join(snippets)


def _load_dict_exam_questions(kind: str, headword: str) -> dict:
    clean_headword = _clean_query_text(headword)
    if not clean_headword:
        return {"available": False, "kind": kind, "headword": headword, "questions": [], "years": []}

    payload, term = _load_dict_exam_term(kind, clean_headword)
    if not term:
        return {
            "available": False,
            "kind": kind,
            "headword": clean_headword,
            "questions": [],
            "years": [],
            "built_at": payload.get("built_at"),
        }

    grouped: dict[str, dict] = {}
    for raw_item in term.get("occurrences") or []:
        if not isinstance(raw_item, dict):
            continue
        title = str(raw_item.get("title") or "").strip()
        category = str(raw_item.get("category") or "").strip()
        year = raw_item.get("year")
        try:
            year_int = int(year) if year is not None else None
        except Exception:
            year_int = None
        paper_key = str(raw_item.get("paper_key") or "").strip()
        question_key = paper_key or f"{title}|{year_int or ''}|{category}"
        bucket = grouped.setdefault(
            question_key,
            {
                "question_key": question_key,
                "paper_key": paper_key or None,
                "title": title,
                "year": year_int,
                "category": category,
                "paper": str(raw_item.get("paper") or "").strip() or None,
                "scope_labels": set(),
                "question_numbers": set(),
                "glosses": set(),
                "occurrences": [],
            },
        )
        scope_label = str(raw_item.get("scope_label") or "").strip()
        if scope_label:
            bucket["scope_labels"].add(scope_label)
        try:
            question_number = int(raw_item.get("question_number"))
        except Exception:
            question_number = None
        if question_number is not None:
            bucket["question_numbers"].add(question_number)
        gloss = _normalize_text_line(raw_item.get("gloss"))
        if gloss:
            bucket["glosses"].add(gloss)
        bucket["occurrences"].append(
            {
                "scope_label": scope_label,
                "question_number": question_number,
                "question_subtype": str(raw_item.get("question_subtype") or "").strip() or None,
                "option_label": str(raw_item.get("option_label") or "").strip() or None,
                "gloss": gloss or None,
                "excerpt": _normalize_multiline_text(raw_item.get("excerpt")),
            }
        )

    question_docs = payload.get("question_docs") or {}
    questions: list[dict] = []
    con = get_db()
    try:
        for item in grouped.values():
            doc = question_docs.get(item.get("paper_key") or "")
            text = ""
            answer = ""
            source_mode = "runtime_excerpt_fallback"
            if isinstance(doc, dict):
                text = _normalize_multiline_text(doc.get("text"))[:DICT_EXAM_QUESTION_TEXT_LIMIT]
                answer = _normalize_multiline_text(doc.get("answer"))[:DICT_EXAM_QUESTION_TEXT_LIMIT]
                source_mode = str(doc.get("source_mode") or "bundled_runtime_question")
            if not text:
                row = _load_exam_question_row(con, item.get("title"), item.get("year"), item.get("category"))
                text = _normalize_multiline_text(row["text"])[:DICT_EXAM_QUESTION_TEXT_LIMIT] if row and row["text"] else ""
                answer = _normalize_multiline_text(row["answer"])[:DICT_EXAM_QUESTION_TEXT_LIMIT] if row and row["answer"] else ""
                if text:
                    source_mode = "gaokao_chunks_db"
            if not text:
                text = _build_exam_question_fallback(item["occurrences"], clean_headword)
            questions.append(
                {
                    "question_key": item["question_key"],
                    "paper_key": item["paper_key"],
                    "title": item["title"],
                    "year": item["year"],
                    "category": item["category"],
                    "paper": item["paper"],
                    "scope_labels": sorted(item["scope_labels"]),
                    "question_numbers": sorted(item["question_numbers"]),
                    "glosses": sorted(item["glosses"]),
                    "occurrence_count": len(item["occurrences"]),
                    "occurrences": item["occurrences"],
                    "source_mode": source_mode,
                    "text": text,
                    "answer": answer or None,
                }
            )
    finally:
        con.close()

    questions.sort(
        key=lambda item: (
            -(item.get("year") or 0),
            item.get("title") or "",
            item.get("question_key") or "",
        )
    )
    years = sorted({int(item["year"]) for item in questions if item.get("year") is not None}, reverse=True)
    return {
        "available": bool(questions),
        "kind": kind,
        "headword": clean_headword,
        "questions": questions,
        "years": years,
        "built_at": payload.get("built_at"),
    }


def _format_chat_history_lines(history: list[dict] | None) -> list[str]:
    recent_messages = list((history or [])[-CHAT_HISTORY_MAX_MESSAGES:])
    full_tail_start = max(0, len(recent_messages) - CHAT_HISTORY_FULL_TAIL_MESSAGES)
    history_lines = []
    for idx, msg in enumerate(recent_messages):
        role = "用户" if msg.get("role") == "user" else "助手"
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if idx < full_tail_start and len(content) > CHAT_HISTORY_TRUNCATED_CHARS:
            content = content[:CHAT_HISTORY_TRUNCATED_CHARS].rstrip() + "…"
        history_lines.append(f"{role}: {content}")
    return history_lines


def _load_ai_summary(con, chunk_id: int) -> str:
    try:
        row = con.execute(
            "SELECT summary FROM ai_summaries WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
    except Exception:
        return ""
    return _normalize_text_line(row["summary"]) if row and row["summary"] else ""


def _load_ai_gaokao_record(con, chunk_id: int) -> dict:
    try:
        row = con.execute(
            """
            SELECT subject, knowledge_points, textbook_refs, summary
            FROM ai_gaokao_links
            WHERE chunk_id = ?
            """,
            (chunk_id,),
        ).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    return {
        "subject": row["subject"],
        "knowledge_points": [item for item in _load_json_list(row["knowledge_points"]) if isinstance(item, str) and item.strip()],
        "textbook_refs": [item for item in _load_json_list(row["textbook_refs"]) if isinstance(item, str) and item.strip()],
        "summary": _normalize_text_line(row["summary"]),
    }


def _parse_textbook_ref(ref: str) -> Optional[dict]:
    match = re.match(r"^(?P<subject>[^·]+)·(?P<title>.+)·p(?P<page>-?\d+)$", (ref or "").strip())
    if not match:
        return None
    try:
        page = int(match.group("page"))
    except Exception:
        return None
    return {
        "subject": match.group("subject").strip(),
        "title": match.group("title").strip(),
        "page": page,
    }


def _resolve_textbook_ref_row(con, parsed: dict):
    try:
        rows = con.execute(
            """
            SELECT c.id, c.subject, c.title, c.book_key, c.section, c.logical_page, c.text,
                   c.content_id, s.summary AS ai_summary
            FROM chunks c
            LEFT JOIN ai_summaries s ON s.chunk_id = c.id
            WHERE c.source = 'mineru'
              AND c.subject = ?
              AND (c.logical_page = ? OR c.section = ?)
            ORDER BY CASE
                WHEN c.logical_page = ? THEN 0
                WHEN c.section = ? THEN 1
                ELSE 2
            END, c.id
            """,
            (
                parsed["subject"],
                parsed["page"],
                parsed["page"],
                parsed["page"],
                parsed["page"],
            ),
        ).fetchall()
    except Exception:
        return None

    parsed_title = (parsed.get("title") or "").strip()
    if not rows:
        return None
    if not parsed_title:
        return rows[0]

    best_row = None
    best_score = -1
    for row in rows:
        meta = _resolve_book_runtime_meta(
            row["book_key"],
            fallback_title=row["title"],
            content_id=row["content_id"] if "content_id" in row.keys() else None,
        )
        candidate_titles = [
            (row["title"] or "").strip(),
            (meta.get("title") or "").strip(),
            (meta.get("display_title") or "").strip(),
        ]
        score = -1
        if parsed_title == candidate_titles[2]:
            score = 100
        elif parsed_title == candidate_titles[1]:
            score = 95
        elif parsed_title == candidate_titles[0]:
            score = 90
        elif parsed_title in {title for title in candidate_titles if title}:
            score = 80
        if score > best_score:
            best_row = row
            best_score = score

    return best_row or rows[0]


def _compose_chunk_snippet(summary: str | None, text: str | None, *, limit: int = 220) -> str:
    clean_summary = _normalize_text_line(summary)
    if clean_summary:
        return clean_summary
    return _chat_excerpt(text or "", limit=limit)


def _resolve_textbook_refs(con, refs: list[str], *, question_subject: str | None, limit: int = 6) -> list[dict]:
    resolved = []
    seen_ids = set()
    for idx, ref in enumerate(refs):
        parsed = _parse_textbook_ref(ref)
        if not parsed:
            continue
        row = _resolve_textbook_ref_row(con, parsed)
        if not row or row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        snippet = _compose_chunk_snippet(row["ai_summary"], row["text"], limit=180)
        logical_page = row["logical_page"] if row["logical_page"] is not None else row["section"]
        resolved.append(
            _apply_book_runtime_meta(
                {
                    "id": row["id"],
                    "subject": row["subject"],
                    "title": row["title"],
                    "book_key": row["book_key"],
                    "section": row["section"],
                    "logical_page": logical_page,
                    "snippet": snippet,
                    "summary": _normalize_text_line(row["ai_summary"]),
                    "text": row["text"] or "",
                    "link_type": "precomputed",
                    "relevance_score": max(80, 96 - idx * 3),
                    "matched_concepts": [],
                    "precomputed_ref": ref,
                    **SUBJECT_META.get(row["subject"], {"icon": "📚", "color": "#95a5a6"}),
                },
                book_key=row["book_key"],
                fallback_title=row["title"],
            )
        )
        if len(resolved) >= limit:
            break

    if question_subject:
        resolved.sort(
            key=lambda item: (
                0 if item["subject"] == question_subject else 1,
                -item["relevance_score"],
            )
        )
    return resolved[:limit]


def _load_ai_synonym_record(con, term: str) -> dict:
    if not term:
        return {}
    try:
        row = con.execute(
            "SELECT synonyms FROM ai_synonyms WHERE term = ?",
            (term,),
        ).fetchone()
    except Exception:
        return {}
    return _load_json_object(row["synonyms"]) if row and row["synonyms"] else {}


def _collect_synonym_aliases(record: dict, *, limit: int = 6) -> list[str]:
    aliases = []
    seen = set()
    for key in ("synonyms", "near_synonyms", "english", "abbreviations", "aliases"):
        values = record.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            normalized = re.sub(r"\s+", " ", value).strip()
            if not normalized:
                continue
            dedupe_key = normalized.casefold()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            aliases.append(normalized)
            if len(aliases) >= limit:
                return aliases
    return aliases


def _expand_chat_search_terms(con, search_terms: list[str], limit: int = 10) -> tuple[list[str], list[dict]]:
    expanded = []
    seen = set()
    alias_hints = []

    def add_term(value: str):
        clean = _clean_query_text(value)
        if not clean:
            return
        normalized = re.sub(r"\s+", " ", clean).strip()
        if not normalized:
            return
        dedupe_key = normalized.casefold()
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        expanded.append(normalized)

    for term in search_terms:
        add_term(term)

    for term in search_terms[:4]:
        record = _load_ai_synonym_record(con, term)
        aliases = [a for a in _collect_synonym_aliases(record, limit=4) if a.casefold() != term.casefold()]
        if aliases:
            alias_hints.append({"term": term, "aliases": aliases[:4]})
        for alias in aliases:
            add_term(alias)
            if len(expanded) >= limit:
                return expanded[:limit], alias_hints
    return expanded[:limit], alias_hints


def get_ai_relation(con, a: str, b: str) -> Optional[dict]:
    """Look up an AI-generated relation label for a concept pair."""
    try:
        row = con.execute(
            """
            SELECT relation_type, description
            FROM ai_relations
            WHERE (concept_a = ? AND concept_b = ?)
               OR (concept_a = ? AND concept_b = ?)
            """,
            (a, b, b, a),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {
        "type": row["relation_type"],
        "description": row["description"],
    }


def _fetch_ai_relation_hints(con, terms: list[str], limit: int = 6) -> list[dict]:
    if not terms:
        return []
    hints = []
    seen = set()

    def add_hint(anchor: str, related: str, relation_type: str, description: str):
        if not anchor or not related:
            return
        pair_key = tuple(sorted((anchor.casefold(), related.casefold())))
        if pair_key in seen:
            return
        seen.add(pair_key)
        hints.append(
            {
                "anchor": anchor,
                "related": related,
                "relation": relation_type,
                "description": description,
            }
        )

    # First, prioritize direct relations between the current search terms.
    for idx, anchor in enumerate(terms):
        for related in terms[idx + 1 :]:
            ai_rel = get_ai_relation(con, anchor, related)
            if not ai_rel:
                continue
            add_hint(anchor, related, ai_rel["type"], ai_rel["description"])
            if len(hints) >= limit:
                return hints

    placeholders = ",".join("?" for _ in terms)
    try:
        rows = con.execute(
            f"""
            SELECT concept_a, concept_b, relation_type, description, ts
            FROM ai_relations
            WHERE concept_a IN ({placeholders}) OR concept_b IN ({placeholders})
            ORDER BY ts DESC
            LIMIT ?
            """,
            tuple(terms) + tuple(terms) + (max(limit * 4, 12),),
        ).fetchall()
    except Exception:
        return hints

    matched_terms = {term.casefold(): term for term in terms}
    for row in rows:
        a = str(row["concept_a"] or "").strip()
        b = str(row["concept_b"] or "").strip()
        a_key = a.casefold()
        b_key = b.casefold()
        if a_key in matched_terms:
            anchor = matched_terms[a_key]
            related = b
        elif b_key in matched_terms:
            anchor = matched_terms[b_key]
            related = a
        else:
            continue
        add_hint(anchor, related, row["relation_type"], row["description"])
        if len(hints) >= limit:
            break
    return hints


def _build_graphrag_relation_hints(con, query: str, query_analysis: dict, limit: int = GRAPH_RAG_MAX_RELATIONS) -> list[dict]:
    if not GRAPH_RAG_ENABLED or limit <= 0:
        return []

    seeds = []
    seen_seeds = set()
    for item in (query_analysis.get("concept_terms") or [])[:3]:
        term = _clean_query_text(item.get("term") or "")
        compact = _compact_query_text(term)
        if not compact or compact in seen_seeds:
            continue
        seeds.append(term)
        seen_seeds.add(compact)

    clean_query = _clean_query_text(query)
    compact_query = _compact_query_text(clean_query)
    if compact_query and compact_query not in seen_seeds:
        seeds.append(clean_query)

    hints = []
    seen_pairs = set()
    for seed in seeds[:3]:
        try:
            center_dist = con.execute(
                """
                SELECT c.subject, COUNT(*) AS cnt
                FROM chunks c JOIN chunks_fts ON chunks_fts.rowid = c.id
                WHERE chunks_fts MATCH ? AND c.source = 'mineru'
                GROUP BY c.subject ORDER BY cnt DESC
                """,
                (seed,),
            ).fetchall()
        except Exception:
            center_dist = []
        center_subjects = {row["subject"] for row in center_dist}
        if len(center_subjects) < 2:
            continue

        for item in _fetch_graph_local_related(con, seed, center_subjects, limit=max(limit * 2, 6)):
            related = _clean_query_text(item.get("term") or "")
            if not related:
                continue
            pair_key = tuple(sorted((seed.casefold(), related.casefold())))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            shared_subjects = item.get("shared_subjects") or []
            hints.append(
                {
                    "anchor": seed,
                    "related": related,
                    "relation": "图谱共现",
                    "description": (
                        f"跨 {len(shared_subjects)} 科共现：{'、'.join(shared_subjects[:4])}"
                        if shared_subjects
                        else "在高信号教材段落中共现"
                    ),
                    "source": "graphrag",
                }
            )
            if len(hints) >= limit:
                return hints
    return hints


def _derive_chat_search_terms(query: str, user_message: str) -> list[str]:
    terms = []
    seen = set()

    def add_term(value: str):
        clean = _clean_query_text(value)
        if not clean:
            return
        normalized = re.sub(r"\s+", " ", clean).strip()
        if not normalized:
            return
        key = normalized.casefold()
        if key in seen:
            return
        seen.add(key)
        terms.append(normalized)

    query_clean = _clean_query_text(query)
    if query_clean:
        add_term(query_clean)

    for quoted in re.findall(r"[「“\"]([^」”\"]{2,24})[」”\"]", user_message or ""):
        add_term(quoted)

    message_clean = _clean_query_text(user_message)
    if message_clean and message_clean.casefold() != query_clean.casefold():
        candidate_terms = []
        if "jieba" in globals():
            try:
                candidate_terms = [
                    token.strip()
                    for token in jieba.cut(message_clean)
                    if token and token.strip()
                ]
            except Exception:
                candidate_terms = []
        if not candidate_terms:
            candidate_terms = re.findall(r"[A-Za-z0-9\-]{2,24}|[\u4e00-\u9fff]{2,12}", message_clean)

        filtered = []
        for token in candidate_terms:
            token = token.strip()
            if not token or token in CHAT_STOPWORDS:
                continue
            if len(token) < 2:
                continue
            if len(token) > 24:
                continue
            filtered.append(token)

        for token in filtered[:6]:
            add_term(token)

    return terms[:5]


def _fetch_chat_rows(
    con,
    clean_q: str,
    *,
    source: str,
    limit: int,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
):
    candidate_limit = _candidate_window_limit(limit, multiplier=2, minimum=max(8, limit * 2), cap=160)
    where_parts = ["c.source = ?"]
    like_params: list[object] = [source]
    fts_params: list[object] = [source]
    if phase:
        where_parts.append("c.phase = ?")
        like_params.append(phase)
        fts_params.append(phase)
    if source != "gaokao" and book_key:
        where_parts.append("c.book_key = ?")
        like_params.append(book_key)
        fts_params.append(book_key)
    elif scope_subject:
        where_parts.append("c.subject = ?")
        like_params.append(scope_subject)
        fts_params.append(scope_subject)
    where_sql = " AND ".join(where_parts)

    like_rows = con.execute(
        f"""
        SELECT c.id, c.subject, c.title, c.book_key, c.section, c.logical_page,
               c.text, c.source, c.year, c.category, -100.0 AS rank,
               s.summary AS ai_summary,
               ag.summary AS ai_gaokao_summary,
               ag.knowledge_points AS ai_gaokao_knowledge_points,
               ag.textbook_refs AS ai_gaokao_textbook_refs
        FROM chunks c
        LEFT JOIN ai_summaries s ON s.chunk_id = c.id
        LEFT JOIN ai_gaokao_links ag ON ag.chunk_id = c.id
        WHERE {where_sql} AND c.text LIKE ?
        LIMIT ?
        """,
        tuple(like_params + [f"%{clean_q}%", candidate_limit]),
    ).fetchall()

    try:
        fts_rows = con.execute(
            f"""
            SELECT c.id, c.subject, c.title, c.book_key, c.section, c.logical_page,
                   c.text, c.source, c.year, c.category, f.rank AS rank,
                   s.summary AS ai_summary,
                   ag.summary AS ai_gaokao_summary,
                   ag.knowledge_points AS ai_gaokao_knowledge_points,
                   ag.textbook_refs AS ai_gaokao_textbook_refs
            FROM chunks c
            JOIN chunks_fts f ON c.id = f.rowid
            LEFT JOIN ai_summaries s ON s.chunk_id = c.id
            LEFT JOIN ai_gaokao_links ag ON ag.chunk_id = c.id
            WHERE {where_sql} AND chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            tuple(fts_params + [clean_q, candidate_limit]),
        ).fetchall()
    except Exception:
        fts_rows = []

    return _merge_ranked_rows(like_rows, fts_rows)[:limit]


def _fetch_ai_gaokao_rows_for_terms(
    con,
    terms: list[str],
    limit: int,
    *,
    scope_subject: str | None = None,
) -> list[dict]:
    rows = []
    existing_ids = set()
    if not terms:
        return rows

    per_term_limit = max(2, math.ceil(limit / max(1, min(len(terms), 4))))
    for idx, term in enumerate(terms[:6]):
        if not term:
            continue
        like_term = f"%{term}%"
        try:
            term_rows = con.execute(
                f"""
                SELECT c.id, c.subject, c.title, c.book_key, c.section, c.logical_page,
                       c.text, c.source, c.year, c.category, -90.0 AS rank,
                       '' AS ai_summary,
                       ag.summary AS ai_gaokao_summary,
                       ag.knowledge_points AS ai_gaokao_knowledge_points,
                       ag.textbook_refs AS ai_gaokao_textbook_refs
                FROM ai_gaokao_links ag
                JOIN chunks c ON c.id = ag.chunk_id
                WHERE c.source = 'gaokao'
                  {'AND c.subject = ?' if scope_subject else ''}
                  AND (ag.summary LIKE ? OR ag.knowledge_points LIKE ? OR ag.textbook_refs LIKE ?)
                ORDER BY c.year DESC, c.id DESC
                LIMIT ?
                """,
                tuple(([scope_subject] if scope_subject else []) + [like_term, like_term, like_term, per_term_limit]),
            ).fetchall()
        except Exception:
            term_rows = []
        for row in term_rows:
            if row["id"] in existing_ids:
                continue
            merged = dict(row)
            merged["matched_term"] = term
            merged["_term_index"] = idx
            rows.append(merged)
            existing_ids.add(row["id"])

    rows.sort(key=lambda item: (item.get("_term_index", 0), -(item.get("year") or 0), item["id"]))
    return rows[:limit]


def _apply_chat_book_diversity(rows: list[dict], *, limit: int, quota_per_book: int = CHAT_BOOK_QUOTA_PER_BOOK) -> list[dict]:
    if not rows:
        return []

    selected = []
    selected_ids = set()
    per_book_counts = Counter()
    grouped_rows: dict[str, list[dict]] = {}
    for row in rows:
        book_key = row.get("book_key") or f"id:{row.get('id')}"
        grouped_rows.setdefault(book_key, []).append(row)

    # Pass 1: maximize book diversity first.
    for book_rows in grouped_rows.values():
        row = book_rows[0]
        selected.append(row)
        selected_ids.add(row.get("id"))
        book_key = row.get("book_key") or f"id:{row.get('id')}"
        per_book_counts[book_key] += 1
        if len(selected) >= limit:
            return selected[:limit]

    # Pass 2: fill remaining slots while respecting the per-book quota.
    for book_key, book_rows in grouped_rows.items():
        for row in book_rows[1:]:
            if row.get("id") in selected_ids:
                continue
            if per_book_counts[book_key] >= quota_per_book:
                continue
            selected.append(row)
            selected_ids.add(row.get("id"))
            per_book_counts[book_key] += 1
            if len(selected) >= limit:
                break
        if len(selected) >= limit:
            break

    # Pass 3: if we still don't have enough, allow overflow rows back in.
    if len(selected) < limit:
        for row in rows:
            if row.get("id") in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(row.get("id"))
            if len(selected) >= limit:
                break
    return selected[:limit]


def _fetch_chat_rows_for_terms(
    con,
    terms: list[str],
    *,
    source: str,
    limit: int,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
):
    rows = []
    existing_ids = set()
    if not terms:
        return rows

    per_term_limit = max(4, math.ceil(limit / max(1, min(len(terms), 3))))
    for idx, term in enumerate(terms):
        for row in _fetch_chat_rows(
            con,
            term,
            source=source,
            limit=per_term_limit,
            scope_subject=scope_subject,
            book_key=book_key,
            phase=phase,
        ):
            row_id = row["id"]
            if row_id in existing_ids:
                continue
            merged = dict(row)
            merged["matched_term"] = term
            merged["_term_index"] = idx
            rows.append(merged)
            existing_ids.add(row_id)

    rows.sort(key=lambda item: (item["rank"], item.get("_term_index", 0), item["id"]))
    return _apply_chat_book_diversity(rows, limit=limit)


PRECISION_QUERY_PATTERNS = (
    re.compile(r"什么是.+"),
    re.compile(r".+是什么"),
    re.compile(r".+的(?:定义|概念|本质|特点|条件|作用|过程|原因)"),
    re.compile(r".+指什么"),
    re.compile(r".+(?:定律|定理|法则|原理|公式)$"),
    re.compile(r".+(?:有什么)?区别"),
    re.compile(r".+和.+的区别"),
    re.compile(r".+的(?:例子|实例)"),
    re.compile(r".+为什么.+"),
)


def _is_precision_query(query: str, user_message: str = "") -> bool:
    compact_query = _compact_query_text(query)
    compact_message = _compact_query_text(user_message)
    if compact_query and any(pattern.search(compact_query) for pattern in PRECISION_QUERY_PATTERNS):
        return True
    if compact_message and any(pattern.search(compact_message) for pattern in PRECISION_QUERY_PATTERNS):
        return True
    return False


def _trim_precision_target(text: str) -> str:
    target = _clean_query_text(text)
    if not target:
        return ""
    patterns = [
        r"^请(?:直接)?(?:根据教材)?(?:回答|解释|说明)?",
        r"^(?:帮我|请你)?(?:只)?(?:在课本中|在教材中)?(?:找|查|说明|解释)?",
        r"(?:的定义|的概念|的本质)$",
        r"(?:是什么|指什么|是什么意思)$",
    ]
    for pattern in patterns:
        target = re.sub(pattern, "", target)
    target = re.sub(r"^(什么是)", "", target)
    target = re.sub(r"[「」“”\"'？?。！，,：:；;（）()\[\]{}]+", " ", target)
    target = re.sub(r"\s+", " ", target).strip()
    return target


def _build_precision_query_profile(query: str, user_message: str = "") -> dict:
    clean_query = _clean_query_text(query)
    clean_message = _clean_query_text(user_message)
    combined = clean_query
    if clean_message and (not clean_query or (not _is_precision_query(clean_query) and _is_precision_query(clean_message))):
        combined = clean_message
    compact = _compact_query_text(combined)
    intent = "lookup"
    target = clean_query

    patterns = [
        ("definition", re.compile(r"^(?P<target>.+?)的定义$")),
        ("definition", re.compile(r"^(?P<target>.+?)的概念$")),
        ("definition", re.compile(r"^(?P<target>.+?)是什么$")),
        ("definition", re.compile(r"^什么是(?P<target>.+)$")),
        ("definition", re.compile(r"^(?P<target>.+?)指什么$")),
        ("comparison", re.compile(r"^(?P<target>.+?)(?:有什么)?区别$")),
        ("comparison", re.compile(r"^(?P<target>.+?)和(?P<other>.+?)的区别$")),
        ("example", re.compile(r"^(?P<target>.+?)的例子$")),
        ("reason", re.compile(r"^(?P<target>.+?)为什么.+$")),
        ("process", re.compile(r"^(?P<target>.+?)的过程$")),
    ]
    for matched_intent, pattern in patterns:
        matched = pattern.match(compact)
        if not matched:
            continue
        intent = matched_intent
        if matched_intent == "comparison" and matched.groupdict().get("other"):
            other = _trim_precision_target(matched.groupdict().get("other") or "")
            primary = _trim_precision_target(matched.groupdict().get("target") or clean_query)
            target = "和".join(part for part in (primary, other) if part)
        else:
            target = _trim_precision_target(matched.groupdict().get("target") or clean_query)
        break

    if intent == "lookup" and "定义" in compact:
        intent = "definition"
        target = _trim_precision_target(compact.replace("定义", ""))
    elif intent == "lookup" and ("什么是" in compact or compact.endswith("是什么")):
        intent = "definition"
        target = _trim_precision_target(compact.replace("什么是", "").replace("是什么", ""))
    elif intent == "lookup" and re.search(r"(?:定律|定理|法则|原理|公式)$", compact):
        intent = "definition"
        target = _trim_precision_target(clean_query)
    elif intent == "lookup" and ("区别" in compact or "不同" in compact):
        intent = "comparison"
        target = _trim_precision_target(clean_query)
    elif intent == "lookup" and ("例子" in compact or "实例" in compact):
        intent = "example"
        target = _trim_precision_target(clean_query)

    target = _trim_precision_target(target or clean_query) or clean_query
    return {
        "query": query,
        "user_message": user_message,
        "intent": intent,
        "target": target,
        "precision_mode": _is_precision_query(query, user_message),
    }


def _is_low_signal_precision_term(term: str, target: str = "") -> bool:
    compact = _compact_query_text(term)
    target_compact = _compact_query_text(target)
    if len(compact) < 2:
        return True
    if compact in {"定义", "概念", "本质", "特点", "条件", "作用", "过程", "原因", "什么是", "是什么"}:
        return True
    if re.fullmatch(r"的(?:定义|概念|本质|特点|条件|作用|过程|原因)", compact):
        return True
    if target_compact and compact in {f"{target_compact}的", f"的{target_compact}", f"是{target_compact}"}:
        return True
    return False


def _build_precision_search_terms(query_profile: dict, query_analysis: dict, *, round_index: int = 0) -> list[str]:
    terms = []
    seen = set()

    def add_term(value: str):
        clean = _clean_query_text(value)
        compact = _compact_query_text(clean)
        if len(compact) < 2 or compact in seen:
            return
        seen.add(compact)
        terms.append(clean)

    query = query_profile.get("query") or ""
    target = query_profile.get("target") or query
    intent = query_profile.get("intent") or "lookup"

    add_term(query)
    add_term(target)

    if intent == "definition":
        if round_index == 0:
            for term in (
                f"{target}的定义",
                f"{target}是什么",
                f"什么是{target}",
                f"{target}是",
                f"{target}概念",
                f"称为{target}",
                f"才能称为{target}",
            ):
                add_term(term)
        else:
            for term in (
                f"{target}是指",
                f"{target}通常指",
                f"{target}称为",
                f"{target}叫做",
                f"什么样的物质才能称为{target}",
            ):
                add_term(term)
    elif intent == "comparison":
        if round_index == 0:
            add_term(f"{target}区别")
            add_term(f"{target}不同")
        else:
            add_term(f"{target}联系")
    elif intent == "example":
        add_term(f"{target}例子")
        add_term(f"{target}实例")
    elif intent == "reason":
        add_term(f"{target}原因")
        add_term(f"{target}为什么")
    elif intent == "process":
        add_term(f"{target}过程")
        add_term(f"{target}步骤")

    for item in (query_analysis.get("concept_terms") or [])[:4]:
        add_term(item.get("term") or "")
        for alias in item.get("aliases", [])[:2]:
            add_term(alias)
    for term in (query_analysis.get("retrieval_terms") or [])[:6]:
        if _is_low_signal_precision_term(term, target):
            continue
        add_term(term)

    return terms[:12]


def _search_textbook_semantic_candidates(
    con: sqlite3.Connection,
    query_text: str,
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
    limit: int = 24,
) -> list[dict]:
    clean_query = _clean_query_text(query_text)
    if not clean_query or not faiss_index or not embedder:
        return []
    query_vec = _cached_query_embedding(clean_query)
    if query_vec is None:
        return []
    # Oversample 3x (or 9x with phase filter) for post-filter precision
    oversample = 3 if not phase else 9
    try:
        distances, ids = faiss_index.search(query_vec, limit * oversample)
    except Exception:
        return []

    ranked_ids = []
    seen = set()
    for score, match_id in zip(distances[0], ids[0]):
        if match_id == -1 or match_id in seen or score <= FAISS_SCORE_THRESHOLD:
            continue
        seen.add(int(match_id))
        ranked_ids.append((int(match_id), float(score)))
        if len(ranked_ids) >= limit:
            break
    if not ranked_ids:
        return []

    placeholders = ",".join("?" for _ in ranked_ids)
    params = [item[0] for item in ranked_ids]
    where_parts = [f"c.id IN ({placeholders})", "(c.source = 'mineru' OR c.source IS NULL)"]
    if phase:
        where_parts.append("c.phase = ?")
        params.append(phase)
    if scope_subject:
        where_parts.append("c.subject = ?")
        params.append(scope_subject)
    if book_key:
        where_parts.append("c.book_key = ?")
        params.append(book_key)
    try:
        rows = con.execute(
            f"""
            SELECT c.id, c.content_id, c.subject, c.title, c.book_key, c.section, c.logical_page,
                   c.text, c.source, s.summary AS ai_summary
            FROM chunks c
            LEFT JOIN ai_summaries s ON s.chunk_id = c.id
            WHERE {' AND '.join(where_parts)}
            """,
            params,
        ).fetchall()
    except Exception:
        return []

    row_by_id = {int(row["id"]): dict(row) for row in rows}
    results = []
    for row_id, score in ranked_ids:
        row = row_by_id.get(row_id)
        if not row:
            continue
        if not _is_supported_textbook_book(
            book_key=row.get("book_key"),
            content_id=row.get("content_id"),
            subject=row.get("subject"),
            edition=_book_version_manifest_row(
                book_key=row.get("book_key"),
                content_id=row.get("content_id"),
            ).get("edition"),
        ):
            continue
        row["semantic_score"] = score
        row["match_channel"] = "semantic"
        results.append(row)
    return results


def _search_supplemental_semantic_candidates(
    query_text: str,
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
    limit: int = 18,
) -> list[dict]:
    clean_query = _clean_query_text(query_text)
    if not clean_query or not supplemental_faiss_index or not embedder:
        return []
    query_vec = _cached_query_embedding(clean_query)
    if query_vec is None:
        return []
    try:
        distances, ids = supplemental_faiss_index.search(query_vec, limit * 6)
    except Exception:
        return []

    entries = _load_supplemental_textbook_pages()
    results = []
    seen = set()
    for score, ordinal in zip(distances[0], ids[0]):
        if ordinal == -1 or ordinal in seen or score <= SUPPLEMENTAL_VECTOR_SCORE_THRESHOLD:
            continue
        if ordinal < 0 or ordinal >= len(entries):
            continue
        seen.add(int(ordinal))
        entry = entries[int(ordinal)]
        if entry.get("primary_bound") or not entry.get("supported", True):
            continue
        if book_key and entry.get("book_key") != book_key:
            continue
        if scope_subject and entry.get("subject") != scope_subject:
            continue
        if phase and entry.get("phase") != phase:
            continue
        results.append(
            {
                "id": entry["id"],
                "content_id": entry.get("content_id"),
                "subject": entry["subject"],
                "title": entry["title"],
                "book_key": entry.get("book_key"),
                "section": entry["section"],
                "logical_page": entry.get("logical_page"),
                "snippet": _build_context_snippet(entry["text"], clean_query),
                "text": entry["text"],
                "source": "mineru",
                "semantic_score": float(score),
                "match_channel": "semantic",
                "retrieval_source": "supplemental",
            }
        )
        if len(results) >= limit:
            break
    return results


def _append_precision_candidate(
    bucket: dict[int | str, dict],
    row: dict,
    *,
    base_score: float,
    matched_term: str,
    match_basis: str,
    match_channel: str,
    retrieval_source: str,
):
    row_id = row.get("id")
    if row_id is None:
        return
    text = row.get("text") or ""
    snippet = row.get("snippet") or _compose_chunk_snippet(row.get("ai_summary"), text, limit=180)
    logical_page = row.get("logical_page")
    if logical_page is None:
        logical_page = row.get("section")
    data = {
        "id": row_id,
        "content_id": row.get("content_id"),
        "subject": row.get("subject") or "",
        "title": row.get("title") or "",
        "book_key": row.get("book_key"),
        "section": row.get("section"),
        "logical_page": logical_page,
        "snippet": snippet,
        "text": text,
        "source": row.get("source") or "mineru",
        "match_channel": match_channel,
        "match_basis": match_basis,
        "matched_term": matched_term,
        "retrieval_source": retrieval_source,
        "semantic_score": float(row.get("semantic_score") or 0.0),
        "base_score": float(base_score),
    }
    existing = bucket.get(row_id)
    if existing and existing.get("base_score", 0) >= data["base_score"]:
        return
    bucket[row_id] = data


def _definition_intent_bonus(query_profile: dict, candidate: dict) -> float:
    if query_profile.get("intent") != "definition":
        return 0.0
    target_raw = _clean_query_text(query_profile.get("target") or "")
    target = _compact_query_text(target_raw)
    if not target or not target_raw:
        return 0.0
    title = _compact_query_text(candidate.get("title") or "")
    raw_text = candidate.get("text") or ""
    text = _compact_query_text(raw_text)
    bonus = 0.0
    anchored_patterns = (
        rf"(?:^|[。！？；：\n])(?:什么是)?{re.escape(target_raw)}(?:通常)?是指",
        rf"(?:^|[。！？；：\n])(?:什么是)?{re.escape(target_raw)}(?:通常)?指",
        rf"(?:^|[。！？；：\n]){re.escape(target_raw)}(?:是|叫做|称为)",
        rf"(?:把|将)[^。！？；：\n]{{0,40}}(?:称为|叫做){re.escape(target_raw)}",
        rf"[^。！？；：\n]{{0,40}}物质(?:称为|叫做){re.escape(target_raw)}",
        rf"什么样的物质才能称为{re.escape(target_raw)}",
        rf"才能称为{re.escape(target_raw)}",
    )
    for pattern in anchored_patterns:
        if re.search(pattern, raw_text):
            bonus += 22.0
    compact_patterns = (
        f"{target}是指",
        f"{target}通常指",
        f"{target}称为",
        f"{target}叫做",
        f"什么是{target}",
        f"称为{target}",
        f"才能称为{target}",
        f"什么样的物质才能称为{target}",
        f"{target}概念",
    )
    for pattern in compact_patterns:
        if pattern in text:
            bonus += 10.0
    if any(marker in raw_text for marker in ("什么样的", "究竟什么", "有什么不同", "为什么")):
        bonus -= 12.0
    if re.search(rf"(?:称为|叫做){re.escape(target_raw)}[A-Za-z0-9\u4e00-\u9fff]", raw_text):
        bonus -= 14.0
    if "定义" in title or "概念" in title:
        bonus += 10.0
    if target and target in title:
        bonus += 6.0
    if _compact_query_text(candidate.get("matched_term") or "") == target and bonus <= 0:
        bonus -= 24.0
    return max(-24.0, min(54.0, bonus))


def _normalize_rerank_score(score: float) -> float:
    try:
        value = float(score)
    except Exception:
        return 0.0
    value = max(-12.0, min(12.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def _rerank_precision_candidates(query_text: str, query_profile: dict, candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    prepared = []
    for candidate in candidates:
        evidence = _extract_candidate_evidence(query_text, query_profile, candidate)
        coverage_bonus = _query_coverage_bonus(query_text, candidate)
        merged = dict(candidate)
        merged["snippet"] = evidence["snippet"]
        merged["evidence_text"] = evidence["text"]
        merged["evidence_score"] = evidence["score"]
        merged["snippet_source"] = evidence["source"]
        merged["coverage_bonus"] = coverage_bonus
        merged["pre_rerank_score"] = (
            float(candidate.get("base_score", 0.0))
            + float(evidence.get("score") or 0.0)
            + coverage_bonus
            + float(candidate.get("semantic_score", 0.0)) * 24.0
        )
        prepared.append(merged)

    prepared.sort(
        key=lambda item: (
            -item.get("pre_rerank_score", 0.0),
            -len(item.get("matched_term") or ""),
            _stable_sort_id(item.get("id")),
        )
    )
    prepared = [item for item in prepared if _supplemental_candidate_passes_guardrails(query_text, item)]
    if not prepared:
        return []

    rerank_model = _get_reranker()
    rerank_scores = [0.0] * len(prepared)
    if rerank_model:
        try:
            pairs = [
                (
                    query_text,
                    "\n".join(
                        part for part in (
                            candidate.get("title") or "",
                            (candidate.get("evidence_text") or "")[:220],
                            (candidate.get("snippet") or "")[:220],
                            "" if candidate.get("snippet_source") == "sentence" else (candidate.get("text") or "")[:360],
                        ) if part
                    ),
                )
                for candidate in prepared[:RERANKER_MAX_CANDIDATES]
            ]
            raw_scores = rerank_model.predict(pairs, batch_size=8, show_progress_bar=False)
            rerank_scores = [_normalize_rerank_score(score) for score in raw_scores] + [0.0] * max(0, len(prepared) - len(raw_scores))
        except Exception as e:
            print(f"Precision rerank failed: {e}", flush=True)

    reranked = []
    for index, candidate in enumerate(prepared):
        rerank_score = rerank_scores[index] if index < len(rerank_scores) else 0.0
        intent_bonus = _definition_intent_bonus(query_profile, candidate)
        semantic_bonus = candidate.get("semantic_score", 0.0) * 35.0
        evidence_bonus = float(candidate.get("evidence_score") or 0.0)
        coverage_bonus = float(candidate.get("coverage_bonus") or 0.0)
        final_score = candidate.get("base_score", 0.0) + rerank_score * 100.0 + intent_bonus + semantic_bonus + evidence_bonus + coverage_bonus
        merged = dict(candidate)
        merged["rerank_score"] = rerank_score
        merged["intent_bonus"] = intent_bonus
        merged["evidence_bonus"] = evidence_bonus
        merged["coverage_bonus"] = coverage_bonus
        merged["final_score"] = final_score
        reranked.append(merged)

    reranked.sort(
        key=lambda item: (
            -item.get("final_score", 0.0),
            -item.get("rerank_score", 0.0),
            -len(item.get("matched_term") or ""),
            _stable_sort_id(item.get("id")),
        )
    )
    return reranked


def _collect_precision_candidates(
    con: sqlite3.Connection,
    query: str,
    query_profile: dict,
    query_analysis: dict,
    *,
    round_index: int = 0,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
) -> tuple[list[dict], list[str]]:
    search_terms = _build_precision_search_terms(query_profile, query_analysis, round_index=round_index)
    bucket: dict[int | str, dict] = {}

    for priority, term in enumerate(search_terms):
        _phase_where = " AND (c.source = 'mineru' OR c.source IS NULL)"
        _phase_params: list = []
        if phase:
            _phase_where += " AND c.phase = ?"
            _phase_params.append(phase)
        rows = _search_chunks_by_term(
            con,
            term,
            where_extra=_phase_where,
            filter_params=_phase_params,
            candidate_limit=18,
            sort="relevance",
        )
        for row in rows[:12]:
            if scope_subject and row.get("subject") != scope_subject:
                continue
            if book_key and row.get("book_key") != book_key:
                continue
            channel = row.get("match_channel") or "fts"
            channel_bonus = 42.0 if channel == "exact" else 24.0 if channel == "fts" else 10.0
            base_score = 210.0 - priority * 14.0 + channel_bonus
            _append_precision_candidate(
                bucket,
                dict(row),
                base_score=base_score,
                matched_term=term,
                match_basis="precision_query",
                match_channel=channel,
                retrieval_source="primary",
            )

    for row in _search_textbook_semantic_candidates(
        con,
        query_profile.get("target") or query,
        scope_subject=scope_subject,
        book_key=book_key,
        phase=phase,
        limit=18,
    ):
        _append_precision_candidate(
            bucket,
            row,
            base_score=170.0,
            matched_term=query_profile.get("target") or query,
            match_basis="semantic_query",
            match_channel="semantic",
            retrieval_source="primary",
        )

    for row in _search_supplemental_semantic_candidates(
        query_profile.get("target") or query,
        scope_subject=scope_subject,
        book_key=book_key,
        limit=12,
    ):
        _append_precision_candidate(
            bucket,
            row,
            base_score=162.0,
            matched_term=query_profile.get("target") or query,
            match_basis="semantic_query",
            match_channel="semantic",
            retrieval_source="supplemental",
        )

    plan = [{"term": term, "basis": "precision_query"} for term in search_terms]
    for index, row in enumerate(
        _search_supplemental_textbook_pages(
            query_profile.get("target") or query,
            plan,
            scope_subject=scope_subject,
            book_key=book_key,
            limit=18,
        )
    ):
        _append_precision_candidate(
            bucket,
            row,
            base_score=190.0 - index * 6.0,
            matched_term=row.get("matched_term") or (query_profile.get("target") or query),
            match_basis=row.get("match_basis") or "precision_query",
            match_channel=row.get("match_channel") or "supplemental",
            retrieval_source="supplemental",
        )

    return list(bucket.values()), search_terms


def _build_hybrid_search_term_plan(query_analysis: dict, query_profile: dict) -> list[dict]:
    plan = []
    seen = set()
    target = query_profile.get("target") or query_analysis.get("display_term") or query_analysis.get("query") or ""
    precision_mode = bool(query_profile.get("precision_mode"))

    def add_item(term: str, basis: str):
        clean = _clean_query_text(term)
        compact = _compact_query_text(clean)
        if len(compact) < 2 or compact in seen:
            return
        if precision_mode and _is_low_signal_precision_term(clean, target):
            return
        seen.add(compact)
        plan.append({"term": clean, "basis": basis})

    if precision_mode:
        deferred_items = []
        for round_index, basis in ((0, "precision_query"), (1, "precision_followup")):
            for term in _build_precision_search_terms(query_profile, query_analysis, round_index=round_index):
                compact_term = _compact_query_text(term)
                if query_profile.get("intent") == "definition" and compact_term == _compact_query_text(target):
                    deferred_items.append((term, basis))
                    continue
                add_item(term, basis)
        for term, basis in deferred_items:
            add_item(term, basis)
    else:
        for item in _build_search_term_plan(query_analysis):
            add_item(item.get("term", ""), item.get("basis") or "query")
        display_term = query_analysis.get("display_term") or ""
        if display_term:
            add_item(display_term, "display_term")

    add_item(query_analysis.get("query", ""), "query")
    return plan[: max(QUERY_TERM_PLAN_LIMIT + 4, 12)]


def _hybrid_search_basis_bonus(basis: str) -> float:
    return {
        "exact": 54.0,
        "concept": 50.0,
        "contained": 46.0,
        "phrase": 44.0,
        "alias": 40.0,
        "expanded": 34.0,
        "precision_query": 58.0,
        "precision_followup": 42.0,
        "display_term": 30.0,
        "fallback": 24.0,
        "query": 18.0,
    }.get(basis, 20.0)


def _hybrid_search_channel_bonus(channel: str) -> float:
    return {
        "exact": 44.0,
        "fts": 24.0,
        "semantic": 18.0,
        "supplemental": 16.0,
    }.get(channel, 12.0)


def _build_search_semantic_queries(query: str, query_analysis: dict, query_profile: dict) -> list[str]:
    queries = []
    seen = set()
    precision_definition = bool(query_profile.get("precision_mode")) and query_profile.get("intent") == "definition"
    target = query_profile.get("target") or ""

    def add_query(value: str):
        clean = _clean_query_text(value)
        compact = _compact_query_text(clean)
        if len(compact) < 2 or compact in seen:
            return
        seen.add(compact)
        queries.append(clean)

    add_query(query)
    if not precision_definition:
        add_query(target)
    display_term = query_analysis.get("display_term") or ""
    if not (
        query_profile.get("precision_mode")
        and (
            _is_low_signal_precision_term(display_term, target or query)
            or (precision_definition and _compact_query_text(display_term) == _compact_query_text(target))
        )
    ):
        add_query(display_term)

    if precision_definition:
        target = target or query
        for value in (f"{target}是什么", f"什么是{target}", f"{target}是"):
            add_query(value)

    return queries[:4]


def _search_row_book_token(row: dict) -> str:
    source = row.get("source") or "mineru"
    return str(row.get("book_key") or f"{source}::{row.get('subject') or ''}::{row.get('title') or ''}")


def _search_row_page_token(row: dict) -> tuple[str, int | str] | None:
    logical_page = row.get("logical_page")
    if logical_page is None:
        logical_page = row.get("section")
    if logical_page is None:
        return None
    return (_search_row_book_token(row), logical_page)


def _reorder_hybrid_search_rows(rows: list[dict], query_profile: dict) -> list[dict]:
    if not rows or not query_profile.get("precision_mode"):
        return rows

    intent = query_profile.get("intent") or "lookup"
    book_cap = 2 if intent == "definition" else 3
    page_cap = 1 if intent in {"definition", "comparison"} else 2
    book_counts: Counter[str] = Counter()
    page_counts: Counter[tuple[str, int | str]] = Counter()
    prioritized = []
    deferred = []

    for row in rows:
        allow = True
        book_token = _search_row_book_token(row)
        page_token = _search_row_page_token(row)
        if book_counts[book_token] >= book_cap:
            allow = False
        if page_token and page_counts[page_token] >= page_cap:
            allow = False
        if allow:
            prioritized.append(row)
            book_counts[book_token] += 1
            if page_token:
                page_counts[page_token] += 1
        else:
            deferred.append(row)

    return prioritized + deferred


def _collect_hybrid_search_rows(
    con: sqlite3.Connection,
    query: str,
    query_analysis: dict,
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
    candidate_limit: int = 80,
) -> tuple[list[dict], dict]:
    query_profile = _build_precision_query_profile(query, query)
    term_plan = _build_hybrid_search_term_plan(query_analysis, query_profile)
    textbook_where_extra, textbook_filter_params = _build_textbook_search_filters(
        scope_subject=scope_subject,
        book_key=book_key,
        phase=phase,
    )
    lexical_limit = max(8, min(24, math.ceil(candidate_limit / max(1, min(len(term_plan), 4)))))
    supplemental_limit = max(12, min(SUPPLEMENTAL_FALLBACK_LIMIT, math.ceil(candidate_limit / 2)))
    bucket: dict[int | str, dict] = {}
    definition_target = _compact_query_text(query_profile.get("target") or query)

    for priority, item in enumerate(term_plan):
        compact_term = _compact_query_text(item["term"])
        term_candidate_limit = lexical_limit
        if query_profile.get("intent") == "definition" and compact_term == definition_target:
            term_candidate_limit = max(4, min(6, lexical_limit))
        rows = _search_chunks_by_term(
            con,
            item["term"],
            where_extra=textbook_where_extra,
            filter_params=textbook_filter_params,
            candidate_limit=term_candidate_limit,
            sort="relevance",
        )
        for row in rows[:term_candidate_limit]:
            channel = row.get("match_channel") or "fts"
            base_score = 168.0 - priority * 10.0
            base_score += _hybrid_search_basis_bonus(item.get("basis") or "query")
            base_score += _hybrid_search_channel_bonus(channel)
            if query_profile.get("intent") == "definition":
                if compact_term == definition_target:
                    base_score -= 42.0
                elif (
                    compact_term.endswith("是")
                    or any(marker in compact_term for marker in ("是什么", "什么是", "是指", "称为", "叫做", "定义", "概念"))
                ):
                    base_score += 22.0
            _append_precision_candidate(
                bucket,
                dict(row),
                base_score=base_score,
                matched_term=item["term"],
                match_basis=item.get("basis") or "query",
                match_channel=channel,
                retrieval_source="primary",
            )

    semantic_queries = _build_search_semantic_queries(query, query_analysis, query_profile)
    for semantic_priority, semantic_query in enumerate(semantic_queries):
        for row in _search_textbook_semantic_candidates(
            con,
            semantic_query,
            scope_subject=scope_subject,
            book_key=book_key,
            phase=phase,
            limit=max(8, min(18, math.ceil(candidate_limit / 3))),
        ):
            _append_precision_candidate(
                bucket,
                row,
                base_score=164.0 - semantic_priority * 12.0,
                matched_term=semantic_query,
                match_basis="semantic_query",
                match_channel="semantic",
                retrieval_source="primary",
            )
        for row in _search_supplemental_semantic_candidates(
            semantic_query,
            scope_subject=scope_subject,
            book_key=book_key,
            phase=phase,
            limit=max(6, min(12, math.ceil(candidate_limit / 4))),
        ):
            _append_precision_candidate(
                bucket,
                row,
                base_score=156.0 - semantic_priority * 10.0,
                matched_term=semantic_query,
                match_basis="semantic_query",
                match_channel="semantic",
                retrieval_source="supplemental",
            )

    supplemental_rows = _search_supplemental_textbook_pages(
        query_profile.get("target") or query,
        term_plan,
        scope_subject=scope_subject,
        book_key=book_key,
        phase=phase,
        limit=supplemental_limit,
    )
    for index, row in enumerate(supplemental_rows):
        _append_precision_candidate(
            bucket,
            row,
            base_score=176.0 - index * 5.0,
            matched_term=row.get("matched_term") or (query_profile.get("target") or query),
            match_basis=row.get("match_basis") or "fallback",
            match_channel=row.get("match_channel") or "supplemental",
            retrieval_source="supplemental",
        )

    candidates = list(bucket.values())
    candidates.sort(
        key=lambda item: (
            -item.get("base_score", 0.0),
            -item.get("semantic_score", 0.0),
            -len(item.get("matched_term") or ""),
            _stable_sort_id(item.get("id")),
        )
    )
    reranked = _rerank_precision_candidates(query, query_profile, candidates)
    reranked = _reorder_hybrid_search_rows(reranked, query_profile)
    for item in reranked:
        item["rank"] = -float(item.get("final_score", item.get("base_score", 0.0)))

    return reranked, {
        "mode": "hybrid_rerank",
        "precision_mode": bool(query_profile.get("precision_mode")),
        "query_intent": query_profile.get("intent") or "lookup",
        "candidate_count": len(candidates),
        "term_plan": term_plan,
        "semantic_queries": semantic_queries,
        "reranker_loaded": bool(reranker),
    }


def _build_precision_followups(query_profile: dict) -> list[str]:
    query = query_profile.get("query") or ""
    target = query_profile.get("target") or query
    intent = query_profile.get("intent") or "lookup"
    if intent == "definition":
        return [
            f"请只根据教材原文，进一步说明「{target}」的关键特征。",
            f"「{target}」最容易和哪些概念混淆？",
            f"如果要背「{target}」的定义，最短表述是什么？",
        ]
    if intent == "comparison":
        return [
            f"请继续比较「{target}」的核心差异。",
            f"哪些教材原文最能体现「{target}」的区别？",
        ]
    return [
        f"请只根据教材原文解释「{query}」的核心。",
        f"围绕「{query}」最容易混淆的概念有哪些？",
    ]


def _build_precision_chat_context_payload(
    con: sqlite3.Connection,
    query: str,
    user_message: str,
    history: list[dict] | None = None,
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
) -> dict:
    query_profile = _build_precision_query_profile(query, user_message)
    query_analysis = _analyze_search_query(con, query, scope_subject=scope_subject, book_key=book_key)
    rounds = []
    reranked: list[dict] = []

    for round_index in range(2):
        candidates, round_terms = _collect_precision_candidates(
            con,
            query,
            query_profile,
            query_analysis,
            round_index=round_index,
            scope_subject=scope_subject,
            book_key=book_key,
            phase=phase,
        )
        reranked = _rerank_precision_candidates(user_message or query, query_profile, candidates)[: max(RERANKER_MAX_CANDIDATES, 16)]
        rounds.append(
            {
                "round": round_index + 1,
                "search_terms": round_terms,
                "candidate_count": len(candidates),
                "top_score": reranked[0]["final_score"] if reranked else 0.0,
            }
        )
        if reranked and (
            reranked[0].get("final_score", 0.0) >= 230.0
            or sum(1 for item in reranked[:3] if item.get("final_score", 0.0) >= 190.0) >= 2
        ):
            break

    evidence = []
    groups_by_subject: dict[str, dict] = {}
    for candidate in reranked[:RERANKER_FINAL_LIMIT]:
        logical_page = candidate.get("logical_page") if candidate.get("logical_page") is not None else candidate.get("section")
        item = _apply_book_runtime_meta(
            {
                "id": candidate["id"],
                "subject": candidate["subject"],
                "title": candidate["title"],
                "book_key": candidate.get("book_key"),
                "section": candidate.get("section"),
                "logical_page": logical_page,
                "snippet": candidate.get("snippet") or _chat_excerpt(candidate.get("text") or "", limit=180),
                "summary": "",
                "text": candidate.get("text") or "",
                "match_channel": candidate.get("match_channel"),
                "matched_term": candidate.get("matched_term"),
                "retrieval_source": candidate.get("retrieval_source"),
                "final_score": candidate.get("final_score"),
            },
            book_key=candidate.get("book_key"),
            fallback_title=candidate.get("title"),
            content_id=candidate.get("content_id"),
        )
        item["citation"] = f"[{item['subject']}·{item['title']}·p{item.get('logical_page')}]"
        evidence.append(item)
        subject_group = groups_by_subject.setdefault(item["subject"], {"subject": item["subject"], "count": 0, "items": []})
        subject_group["count"] += 1
        if len(subject_group["items"]) < 2:
            subject_group["items"].append(item)

    context_lines = []
    for subject, group in sorted(groups_by_subject.items(), key=lambda pair: pair[1]["count"], reverse=True):
        lines = [f"【{subject}】（{group['count']}条高相关证据）"]
        for item in group["items"]:
            lines.append(f"{item['citation']} {item['snippet']}")
        context_lines.append("\n".join(lines))

    summary = {
        "coverage_line": (
            f"精准检索 · {len(rounds)} 轮召回 · 证据 {len(evidence)} 条 · "
            f"学科 {len(groups_by_subject)} 个"
        ),
        "search_terms_used": rounds[0]["search_terms"] if rounds else [query],
        "retrieval_terms_used": list(dict.fromkeys(term for round_info in rounds for term in round_info["search_terms"]))[:12],
        "top_subjects": [
            {"subject": group["subject"], "count": group["count"]}
            for group in sorted(groups_by_subject.values(), key=lambda item: item["count"], reverse=True)[:4]
        ],
        "relation_hint_count": 0,
        "gaokao_hit_count": 0,
        "agent_rounds": rounds,
        "query_intent": query_profile.get("intent"),
    }

    return {
        "mode": "precision_agent",
        "query": query,
        "user_message": user_message,
        "search_terms_used": summary["search_terms_used"],
        "retrieval_terms_used": summary["retrieval_terms_used"],
        "alias_hints": [],
        "relation_hints": [],
        "alias_text": "（无）",
        "relation_text": "（无）",
        "context_text": "\n\n".join(context_lines),
        "gaokao_text": "（无）",
        "history_text": "\n".join(_format_chat_history_lines(history)) if history else "（无）",
        "query_analysis": query_analysis,
        "query_profile": query_profile,
        "query_resolution_text": query_analysis.get("summary") or "",
        "summary": summary,
        "suggested_questions": _build_precision_followups(query_profile),
        "evidence": evidence,
        "groups": list(groups_by_subject.values()),
        "gaokao_examples": [],
        "phase": phase,
    }


def _build_chat_context_payload_legacy(
    con,
    query: str,
    user_message: str,
    history: list[dict] | None = None,
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
) -> dict:
    clean_q = _clean_query_text(query)
    if not clean_q:
        raise HTTPException(400, "Invalid query")

    query_analysis = _analyze_search_query(con, query, scope_subject=scope_subject, book_key=book_key)
    search_terms = list(
        dict.fromkeys(
            (query_analysis.get("retrieval_terms") or [])
            + _derive_chat_search_terms(query, user_message)
        )
    )
    retrieval_terms, alias_hints = _expand_chat_search_terms(con, search_terms)
    relation_hints = _fetch_ai_relation_hints(con, search_terms, limit=4)
    if len(relation_hints) < 4:
        relation_hints.extend(
            _build_graphrag_relation_hints(
                con,
                query,
                query_analysis,
                limit=max(0, 4 - len(relation_hints)),
            )
        )

    textbook_rows = _fetch_chat_rows_for_terms(
        con,
        retrieval_terms,
        source="mineru",
        limit=16,
        scope_subject=scope_subject,
        book_key=book_key,
        phase=phase,
    )
    gaokao_rows = []
    if not book_key and phase != "初中":
        gaokao_rows = _fetch_chat_rows_for_terms(
            con,
            retrieval_terms,
            source="gaokao",
            limit=4,
            scope_subject=scope_subject,
        )
        for row in _fetch_ai_gaokao_rows_for_terms(con, retrieval_terms, limit=4, scope_subject=scope_subject):
            if any(existing["id"] == row["id"] for existing in gaokao_rows):
                continue
            gaokao_rows.append(row)
            if len(gaokao_rows) >= 4:
                break

    by_subject = {}
    for row in textbook_rows:
        by_subject.setdefault(row["subject"], []).append(row)

    groups = []
    evidence = []
    for subject, subject_rows in sorted(by_subject.items(), key=lambda item: len(item[1]), reverse=True)[:4]:
        diverse_rows = _apply_chat_book_diversity(subject_rows, limit=max(2, len(subject_rows)))
        selected = []
        for row in diverse_rows[:2]:
            logical_page = row["logical_page"] if row["logical_page"] is not None else row["section"]
            snippet = _compose_chunk_snippet(row.get("ai_summary"), row.get("text"), limit=180)
            item = _apply_book_runtime_meta(
                {
                    "id": row["id"],
                    "subject": subject,
                    "title": row["title"],
                    "book_key": row["book_key"],
                    "section": row["section"],
                    "logical_page": logical_page,
                    "snippet": snippet,
                    "matched_term": row.get("matched_term"),
                },
                book_key=row["book_key"],
                fallback_title=row["title"],
                content_id=row.get("content_id"),
            )
            item["citation"] = f"[{subject}·{item['title']}·p{logical_page}]"
            selected.append(item)
            evidence.append(item)
        groups.append({"subject": subject, "count": len(subject_rows), "items": selected})

    gaokao_examples = []
    for row in gaokao_rows[:2]:
        knowledge_points = [item for item in _load_json_list(row.get("ai_gaokao_knowledge_points")) if isinstance(item, str)]
        textbook_refs = [item for item in _load_json_list(row.get("ai_gaokao_textbook_refs")) if isinstance(item, str)]
        ai_summary = _normalize_text_line(row.get("ai_gaokao_summary"))
        gaokao_examples.append(
            {
                "id": row["id"],
                "subject": row["subject"],
                "year": row["year"],
                "category": row["category"],
                "title": row["title"],
                "snippet": ai_summary or _chat_excerpt(row["text"], limit=220),
                "summary": ai_summary,
                "knowledge_points": knowledge_points[:5],
                "textbook_refs": textbook_refs[:3],
            }
        )

    context_lines = []
    for group in groups:
        lines = [f"【{group['subject']}】（{group['count']}条命中）"]
        for item in group["items"]:
            lines.append(f"{item['citation']} {item['snippet']}")
        context_lines.append("\n".join(lines))

    gaokao_lines = [
        " ".join(
            part
            for part in [
                f"[{item['subject']}·{item['year'] or '未知年份'}·{item['category'] or '真题'}]",
                item["summary"] or item["snippet"],
                f"知识点：{'、'.join(item['knowledge_points'][:3])}" if item.get("knowledge_points") else "",
                f"教材锚点：{'；'.join(item['textbook_refs'][:2])}" if item.get("textbook_refs") else "",
            ]
            if part
        )
        for item in gaokao_examples
    ]
    alias_lines = [
        f"{item['term']}：{'、'.join(item['aliases'])}"
        for item in alias_hints
        if item.get("aliases")
    ]
    relation_lines = [
        f"{item['anchor']} ↔ {item['related']}：{item['relation']}；{item['description']}"
        for item in relation_hints
    ]

    history_lines = _format_chat_history_lines(history)

    summary = {
        "subject_count": len(groups),
        "textbook_hit_count": len(textbook_rows),
        "gaokao_hit_count": len(gaokao_examples),
        "evidence_count": len(evidence),
        "coverage_line": (
            f"覆盖 {len(groups)} 个学科 · 教材命中 {len(textbook_rows)} 条 · "
            f"真题例子 {len(gaokao_examples)} 条"
        ),
        "search_terms_used": search_terms,
        "retrieval_terms_used": retrieval_terms,
        "alias_hint_count": len(alias_hints),
        "relation_hint_count": len(relation_hints),
        "graph_hint_count": sum(1 for item in relation_hints if item.get("source") == "graphrag"),
        "top_subjects": [
            {"subject": group["subject"], "count": group["count"]}
            for group in groups[:4]
        ],
    }

    return {
        "mode": "cross_subject",
        "query": query,
        "user_message": user_message,
        "subject_count": len(groups),
        "evidence": evidence,
        "groups": groups,
        "gaokao_examples": gaokao_examples,
        "context_text": "\n\n".join(context_lines),
        "gaokao_text": "\n".join(gaokao_lines),
        "history_text": "\n".join(history_lines) if history_lines else "（无）",
        "search_terms_used": search_terms,
        "retrieval_terms_used": retrieval_terms,
        "alias_hints": alias_hints,
        "alias_text": "\n".join(alias_lines),
        "relation_hints": relation_hints,
        "relation_text": "\n".join(relation_lines),
        "query_analysis": query_analysis,
        "query_resolution_text": query_analysis.get("summary") or "",
        "summary": summary,
        "suggested_questions": [
            f"请先解释「{query}」在不同学科里的共同核心。",
            f"「{query}」在高考里最常见的考法是什么？",
            f"围绕「{query}」最容易混淆的概念有哪些？",
            f"如果我要复习「{query}」，应该按什么顺序串起来学？",
        ],
        "phase": phase,
    }


def _build_chat_context_payload(
    con,
    query: str,
    user_message: str,
    history: list[dict] | None = None,
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
) -> dict:
    if _is_precision_query(query, user_message):
        return _build_precision_chat_context_payload(
            con,
            query,
            user_message,
            history=history,
            scope_subject=scope_subject,
            book_key=book_key,
            phase=phase,
        )
    return _build_chat_context_payload_legacy(
        con,
        query,
        user_message,
        history=history,
        scope_subject=scope_subject,
        book_key=book_key,
        phase=phase,
    )


def _build_chat_context_for_request(
    query: str,
    user_message: str,
    history: list[dict] | None = None,
    *,
    scope_subject: str | None = None,
    book_key: str | None = None,
    phase: str | None = None,
) -> dict:
    con = get_db()
    try:
        return _build_chat_context_payload(
            con,
            query,
            user_message,
            history=history,
            scope_subject=scope_subject,
            book_key=book_key,
            phase=phase,
        )
    finally:
        con.close()


def _build_chat_prompt_legacy(query: str, user_message: str, context_payload: dict, history: list[dict] | None = None) -> str:
    history_text = (context_payload.get("history_text") or "").strip()
    if history and not history_text:
        history_text = "\n".join(_format_chat_history_lines(history))
    if not history_text:
        history_text = "（无）"

    _phase = context_payload.get("phase")
    _display_phase = "初中" if _phase == "初中" else "高中"

    _gaokao_evidence = ""
    _gaokao_instruction = ""
    if _phase == "初中":
        _gaokao_evidence = "考试真题证据：\n（初中暂无考试真题数据）"
        _gaokao_instruction = "【考点提示】根据教材证据总结可能的考查方向；如证据不足就写\"证据不足\"。"
    else:
        _gaokao_evidence = f"高考证据（如有）：\n{context_payload.get('gaokao_text') or '（无）'}"
        _gaokao_instruction = "【高考考法】如果给定证据里有真题，再说明常见考法 / 易错点；没有就写\"高考证据不足\"。"

    return f"""你是一位资深跨学科教育专家，面向{_display_phase}学生。用户当前搜索词是「{context_payload.get('query') or query}」。

本轮检索关注词：
{ "、".join(context_payload.get("search_terms_used") or [query]) }

术语解析：
{context_payload.get('query_resolution_text') or '（无）'}

检索扩展词（含别名）：
{ "、".join(context_payload.get("retrieval_terms_used") or context_payload.get("search_terms_used") or [query]) }

概念别名 / 同义表达：
{context_payload.get('alias_text') or '（无）'}

概念关系提示：
{context_payload.get('relation_text') or '（无）'}

教材证据（多学科原文）：
{context_payload.get('context_text') or '（无）'}

{_gaokao_evidence}

历史对话：
{history_text}

用户本轮问题：
{user_message}

请按以下结构回答：
【核心结论】先用 1-2 句讲清本质。
【学科联动】分点说明不同学科如何描述同一概念，尽量标注出处，格式：[学科·书名·p页码]。
{_gaokao_instruction}
【学习建议】给出面向{_display_phase}生的复习顺序或追问方向。

规则：
1. 只根据给定证据回答，不要编造页码或教材内容。
2. 如果证据不足，必须明确说"证据不足"。
3. 若用户追问，保持连续回答，不重复整段前文。
4. 可以参考"概念别名 / 关系提示"组织答案，但不能把它们当成教材原文引用。
5. 语言简洁、具体，避免空泛套话。
6. 总长度尽量控制在 280 字以内。"""


def _build_precision_chat_prompt(
    query: str,
    user_message: str,
    context_payload: dict,
    history: list[dict] | None = None,
) -> str:
    history_text = (context_payload.get("history_text") or "").strip()
    if history and not history_text:
        history_text = "\n".join(_format_chat_history_lines(history))
    if not history_text:
        history_text = "（无）"

    query_profile = context_payload.get("query_profile") or {}
    intent = query_profile.get("intent") or "lookup"
    target = query_profile.get("target") or query
    rounds = (context_payload.get("summary") or {}).get("agent_rounds") or []
    intent_label = {
        "definition": "定义检索",
        "comparison": "比较检索",
        "example": "例证检索",
        "reason": "因果检索",
        "process": "过程检索",
    }.get(intent, "精准检索")

    _phase = context_payload.get("phase")
    _display_phase = "初中" if _phase == "初中" else "高中"

    return f"""你是一位严谨的教材检索助手，面向{_display_phase}学生。当前搜索词是「{context_payload.get('query') or query}」，用户当前要解决的是「{target}」的{intent_label}。

Agent 检索轮次：{len(rounds)} 轮
本轮检索关注词：
{ "、".join(context_payload.get("search_terms_used") or [query]) }

检索扩展词：
{ "、".join(context_payload.get("retrieval_terms_used") or context_payload.get("search_terms_used") or [query]) }

术语解析：
{context_payload.get('query_resolution_text') or '（无）'}

教材证据：
{context_payload.get('context_text') or '（无）'}

历史对话：
{history_text}

用户本轮问题：
{user_message}

请按以下规则回答：
1. 先直接回答问题本身，不要先空泛铺垫。
2. 只能根据教材证据作答，每个核心判断尽量附 1 个出处，格式：[学科·书名·p页码]。
3. 如果用户在问"定义"，先给最短可背诵表述，再补 1-2 个关键特征。
4. 如果证据不足或检索结果彼此不一致，必须明确写"教材证据不足"。
5. 忽略低相关结果，不要为了凑字数复述无关内容。
6. 若用户继续追问，保持连续回答，不重复整段前文。
7. 总长度尽量控制在 220 字以内。"""


def _build_chat_prompt(query: str, user_message: str, context_payload: dict, history: list[dict] | None = None) -> str:
    if context_payload.get("mode") == "precision_agent":
        return _build_precision_chat_prompt(query, user_message, context_payload, history=history)
    return _build_chat_prompt_legacy(query, user_message, context_payload, history=history)


async def _call_ai_service(prompt: str) -> dict:
    payload_obj = {
        "prompt": prompt,
        "model": AI_SERVICE_MODEL,
        "taskType": AI_SERVICE_TASK_TYPE,
        "thinkingLevel": AI_SERVICE_THINKING_LEVEL,
    }
    headers = {
        "Accept": "application/json",
        "Origin": AI_SERVICE_ORIGIN,
        "Referer": AI_SERVICE_REFERER,
        "User-Agent": AI_SERVICE_USER_AGENT,
        "X-Task-Type": AI_SERVICE_TASK_TYPE,
        "X-Thinking-Level": AI_SERVICE_THINKING_LEVEL,
    }
    if AI_SERVICE_PROJECT:
        headers["X-Project-Name"] = AI_SERVICE_PROJECT
    if AI_INTERNAL_TOKEN:
        headers["X-Internal-Token"] = AI_INTERNAL_TOKEN

    last_http_error: Optional[tuple[int, str]] = None
    last_network_error: Optional[str] = None
    timeout_hit = False

    async with httpx.AsyncClient(timeout=httpx.Timeout(AI_SERVICE_TIMEOUT)) as client:
        for attempt in range(AI_SERVICE_RETRIES + 1):
            try:
                response = await client.post(AI_SERVICE_URL, json=payload_obj, headers=headers)
                raw = response.text
                if response.status_code >= 400:
                    detail = raw[:400]
                    last_http_error = (response.status_code, detail)
                    if response.status_code >= 500 and attempt < AI_SERVICE_RETRIES:
                        await asyncio.sleep(AI_SERVICE_RETRY_DELAY)
                        continue
                    raise HTTPException(502, f"AI service http error: {response.status_code} {detail}")
                break
            except httpx.TimeoutException as e:
                timeout_hit = True
                if attempt < AI_SERVICE_RETRIES:
                    await asyncio.sleep(AI_SERVICE_RETRY_DELAY)
                    continue
                raise HTTPException(504, "AI service timeout") from e
            except httpx.RequestError as e:
                last_network_error = str(e)
                if attempt < AI_SERVICE_RETRIES:
                    await asyncio.sleep(AI_SERVICE_RETRY_DELAY)
                    continue
                raise HTTPException(502, f"AI service unavailable: {e}") from e
        else:
            if last_http_error:
                raise HTTPException(502, f"AI service http error: {last_http_error[0]} {last_http_error[1]}")
            if last_network_error:
                raise HTTPException(502, f"AI service unavailable: {last_network_error}")
            if timeout_hit:
                raise HTTPException(504, "AI service timeout")
            raise HTTPException(502, "AI service unavailable")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(502, "AI service returned invalid JSON") from e

    answer = str(data.get("answer") or "").strip()
    if not answer:
        raise HTTPException(502, f"AI service returned no answer: {data.get('error') or 'unknown error'}")
    return data


# ── Search logs table ─────────────────────────────────────────────────
def init_search_logs():
    """Create search_logs table if not exists."""
    with _write_lock:
        con = get_db()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS search_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    query_normalized TEXT NOT NULL,
                    subject TEXT,
                    book_key TEXT,
                    source TEXT,
                    phase TEXT,
                    result_count INTEGER DEFAULT 0,
                    ts REAL NOT NULL
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON search_logs(ts DESC)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_logs_qn ON search_logs(query_normalized)")
            # Add phase column to existing tables (safe for re-run)
            try:
                con.execute("ALTER TABLE search_logs ADD COLUMN phase TEXT")
            except Exception:
                pass  # column already exists
            con.commit()
        finally:
            con.close()

init_search_logs()


def log_search(query: str, subject=None, book_key=None, source=None, result_count=0, phase=None):
    """Record a search query asynchronously."""
    normalized = re.sub(r'\s+', '', query.strip().lower())
    if len(normalized) < 1 or _is_synthetic_query(normalized):
        return
    try:
        with _write_lock:
            con = get_db()
            try:
                con.execute(
                    "INSERT INTO search_logs (query, query_normalized, subject, book_key, source, phase, result_count, ts) VALUES (?,?,?,?,?,?,?,?)",
                    (query.strip(), normalized, subject, book_key, source, phase, result_count, time.time())
                )
                con.commit()
            finally:
                con.close()
    except Exception:
        pass  # never block search for logging failures


def init_ai_chat_logs():
    """Create ai_chat_logs table if not exists."""
    with _write_lock:
        con = get_db()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS ai_chat_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    subject_count INTEGER DEFAULT 0,
                    evidence_count INTEGER DEFAULT 0,
                    gaokao_hit_count INTEGER DEFAULT 0,
                    provider TEXT,
                    phase TEXT,
                    success INTEGER DEFAULT 0,
                    error TEXT,
                    ts REAL NOT NULL
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_ai_chat_logs_ts ON ai_chat_logs(ts DESC)")
            # Add phase column to existing tables (safe for re-run)
            try:
                con.execute("ALTER TABLE ai_chat_logs ADD COLUMN phase TEXT")
            except Exception:
                pass  # column already exists
            con.commit()
        finally:
            con.close()


init_ai_chat_logs()


def _write_ai_chat_log(
    query: str,
    user_message: str,
    summary: dict,
    *,
    provider: str,
    success: bool,
    error: str | None = None,
    phase: str | None = None,
):
    try:
        with _write_lock:
            con = get_db()
            try:
                con.execute(
                    """
                    INSERT INTO ai_chat_logs (
                        query, user_message, subject_count, evidence_count, gaokao_hit_count,
                        provider, success, error, phase, ts
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        query.strip(),
                        user_message.strip(),
                        int(summary.get("subject_count") or 0),
                        int(summary.get("evidence_count") or 0),
                        int(summary.get("gaokao_hit_count") or 0),
                        provider,
                        1 if success else 0,
                        error,
                        phase,
                        time.time(),
                    ),
                )
                con.commit()
            finally:
                con.close()
    except Exception:
        pass


def log_ai_chat(
    query: str,
    user_message: str,
    context_payload: dict,
    *,
    provider: str | None = None,
    success: bool,
    error: str | None = None,
    phase: str | None = None,
):
    summary = context_payload.get("summary") or {}
    _write_ai_chat_log(
        query,
        user_message,
        summary,
        provider=provider or AI_SERVICE_LABEL,
        success=success,
        error=error,
        phase=phase or context_payload.get("phase"),
    )


def _compute_search_subject_counts(
    con: sqlite3.Connection,
    clean_q: str,
    where_extra: str,
    filter_params: list,
    *,
    has_images: bool,
) -> dict[str, int]:
    count_where_extra = where_extra
    if has_images:
        count_where_extra += " AND c.text LIKE '%![%'"

    like_params = [f"%{clean_q}%"] + list(filter_params)
    fts_params = [clean_q] + list(filter_params)

    try:
        rows = con.execute(
            f"""
                SELECT subject, COUNT(*) AS cnt
                FROM (
                    SELECT c.id, c.subject
                    FROM chunks c
                    WHERE c.text LIKE ? {count_where_extra}
                    UNION
                    SELECT c.id, c.subject
                    FROM chunks c
                    JOIN chunks_fts f ON c.id = f.rowid
                    WHERE chunks_fts MATCH ? {count_where_extra}
                )
                GROUP BY subject
                ORDER BY cnt DESC, subject
            """,
            like_params + fts_params,
        ).fetchall()
    except Exception:
        rows = con.execute(
            f"""
                SELECT c.subject AS subject, COUNT(*) AS cnt
                FROM chunks c
                WHERE c.text LIKE ? {count_where_extra}
                GROUP BY c.subject
                ORDER BY cnt DESC, c.subject
            """,
            like_params,
        ).fetchall()

    return {row["subject"]: row["cnt"] for row in rows}


def _path_cache_token(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        return (str(path), -1, -1)


@functools.lru_cache(maxsize=8)
def _load_json_file_cached(path_str: str, mtime_ns: int, size: int):
    if mtime_ns < 0 or size < 0:
        return {}
    try:
        return json.loads(Path(path_str).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_textbook_classics_manifest() -> dict:
    token = _path_cache_token(TEXTBOOK_CLASSICS_MANIFEST_PATH)
    data = _load_json_file_cached(*token)
    if isinstance(data, dict) and data:
        return data
    bundled_token = _path_cache_token(BUNDLED_TEXTBOOK_CLASSICS_MANIFEST_PATH)
    bundled = _load_json_file_cached(*bundled_token)
    return bundled if isinstance(bundled, dict) else {}


def _normalize_single_char_page_map(payload) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    raw_map = payload.get("anchors") if isinstance(payload.get("anchors"), dict) else payload
    anchors: dict[str, int] = {}
    for raw_headword, raw_page in raw_map.items():
        headword = _compact_query_text(str(raw_headword or ""))
        if not _is_single_hanzi_query(headword):
            continue
        try:
            page_num = int(raw_page)
        except Exception:
            continue
        if page_num > 0:
            anchors[headword] = page_num
    return anchors


def _load_xuci_single_char_index() -> dict[str, int]:
    token = _path_cache_token(XUCI_SINGLE_CHAR_INDEX_PATH)
    data = _normalize_single_char_page_map(_load_json_file_cached(*token))
    if data:
        return data
    bundled_token = _path_cache_token(BUNDLED_XUCI_SINGLE_CHAR_INDEX_PATH)
    return _normalize_single_char_page_map(_load_json_file_cached(*bundled_token))


def _load_dict_headword_index() -> dict:
    token = _path_cache_token(DICT_HEADWORD_INDEX_PATH)
    data = _load_json_file_cached(*token)
    return data if isinstance(data, dict) else {}


def _load_dict_qc_payload() -> dict:
    token = _path_cache_token(DICT_QC_PATH)
    data = _load_json_file_cached(*token)
    return data if isinstance(data, dict) else {}


def _get_dict_db() -> Optional[sqlite3.Connection]:
    if not DICT_DB_PATH.exists() or DICT_DB_PATH.stat().st_size <= 0:
        return None
    con = sqlite3.connect(
        DICT_DB_PATH,
        check_same_thread=False,
        timeout=SQLITE_CONNECT_TIMEOUT_SEC,
    )
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return con


def _get_moe_revised_db() -> Optional[sqlite3.Connection]:
    if not DICT_MOE_REVISED_DB_PATH.exists() or DICT_MOE_REVISED_DB_PATH.stat().st_size <= 0:
        return None
    con = sqlite3.connect(
        DICT_MOE_REVISED_DB_PATH,
        check_same_thread=False,
        timeout=SQLITE_CONNECT_TIMEOUT_SEC,
    )
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return con


def _load_moe_revised_metadata() -> dict:
    con = _get_moe_revised_db()
    if con is None:
        return {
            "available": False,
            "label": "教育部《重编国语辞典修订本》",
            "description": "教育部授权数据包尚未导入站内结果区。",
            "license": "CC BY-ND 3.0 TW",
        }
    try:
        rows = con.execute("SELECT key, value FROM metadata").fetchall()
    except sqlite3.DatabaseError:
        con.close()
        return {
            "available": False,
            "label": "教育部《重编国语辞典修订本》",
            "description": "教育部授权数据包读取失败。",
            "license": "CC BY-ND 3.0 TW",
        }
    finally:
        try:
            con.close()
        except Exception:
            pass

    payload = {}
    for row in rows:
        key = row["key"]
        raw_value = row["value"]
        try:
            payload[key] = json.loads(raw_value)
        except Exception:
            payload[key] = raw_value
    payload.setdefault("available", True)
    payload.setdefault("label", "教育部《重编国语辞典修订本》")
    payload.setdefault(
        "description",
        "本典为一部历史语言辞典，记录中古至现代各类词语，并大量引用古典文献书证，字音部分则兼收现代及传统音读。",
    )
    payload.setdefault("license", "CC BY-ND 3.0 TW")
    return payload


def _dict_book_page_offset(dict_source: str) -> int:
    meta = DICT_SOURCE_META.get(dict_source, {})
    return max(0, int(meta.get("book_page_offset") or 0))


def _dict_book_page_limit(dict_source: str) -> int:
    meta = DICT_SOURCE_META.get(dict_source, {})
    configured = int(meta.get("entry_page_limit") or 0)
    if configured > 0:
        return configured
    page_count = int(meta.get("page_count") or 0)
    return max(0, page_count - _dict_book_page_offset(dict_source))


def _dict_pdf_page(dict_source: str, book_page: int | None) -> Optional[int]:
    if book_page is None:
        return None
    try:
        page_num = int(book_page)
    except Exception:
        return None
    if page_num <= 0:
        return None
    pdf_page = page_num + _dict_book_page_offset(dict_source)
    page_count = int(DICT_SOURCE_META.get(dict_source, {}).get("page_count") or 0)
    if page_count > 0 and pdf_page > page_count:
        return None
    return pdf_page


def _dict_book_page(dict_source: str, pdf_page: int | None) -> Optional[int]:
    if pdf_page is None:
        return None
    try:
        page_num = int(pdf_page)
    except Exception:
        return None
    if page_num <= 0:
        return None
    book_page = page_num - _dict_book_page_offset(dict_source)
    if book_page <= 0:
        return None
    return book_page


def _dict_book_page_numbers(dict_source: str, pdf_pages: list[int]) -> list[int]:
    pages = []
    for pdf_page in pdf_pages:
        book_page = _dict_book_page(dict_source, pdf_page)
        if book_page is not None:
            pages.append(book_page)
    return sorted(set(pages))


def _dict_page_url(dict_source: str, page: int | None) -> Optional[str]:
    meta = DICT_SOURCE_META.get(dict_source)
    if not meta or page is None:
        return None
    pdf_page = _dict_pdf_page(dict_source, page)
    if pdf_page is None:
        return None
    return f"{IMG_CDN}/pages/{meta['page_prefix']}/p{pdf_page}.webp"


def _dict_page_urls(
    dict_source: str,
    page_start: int | None,
    page_end: int | None,
    *,
    max_pages: int = 12,
) -> list[str]:
    try:
        start = int(page_start or 0)
    except Exception:
        start = 0
    try:
        end = int(page_end or start)
    except Exception:
        end = start
    if start <= 0:
        return []
    if end < start:
        end = start
    return [
        _dict_page_url(dict_source, page)
        for page in range(start, min(end, start + max_pages - 1) + 1)
        if _dict_page_url(dict_source, page)
    ]


def _normalize_page_number_list(
    raw_pages,
    *,
    page_start: int | None = None,
    page_end: int | None = None,
) -> list[int]:
    pages = []
    if isinstance(raw_pages, list):
        for item in raw_pages:
            try:
                page_num = int(item)
            except Exception:
                continue
            if page_num > 0:
                pages.append(page_num)
    if not pages:
        try:
            start = int(page_start or 0)
        except Exception:
            start = 0
        try:
            end = int(page_end or start)
        except Exception:
            end = start
        if start > 0:
            end = max(start, end)
            pages = list(range(start, end + 1))
    return sorted(set(pages))


def _normalize_page_url_list(dict_source: str, raw_urls, page_numbers: list[int]) -> list[str]:
    urls = []
    if isinstance(raw_urls, list):
        urls = [item.strip() for item in raw_urls if isinstance(item, str) and item.strip()]
    if urls:
        return urls
    return [
        _dict_page_url(dict_source, page_num)
        for page_num in page_numbers
        if _dict_page_url(dict_source, page_num)
    ]


def _build_dict_page_entry_payload(raw_entry: dict, *, fallback_headword: str | None = None) -> Optional[dict]:
    if not isinstance(raw_entry, dict):
        return None
    dict_source = str(raw_entry.get("dict_source") or raw_entry.get("source") or "").strip()
    if dict_source not in DICT_SOURCE_META or dict_source not in DICT_ENABLED_SOURCE_SET:
        return None
    headword = str(
        raw_entry.get("display_headword")
        or raw_entry.get("headword")
        or fallback_headword
        or ""
    ).strip()
    if not headword:
        return None
    headword_trad = str(raw_entry.get("headword_trad") or "").strip() or None
    raw_pdf_page_numbers = _normalize_page_number_list(
        raw_entry.get("page_numbers") or raw_entry.get("pages"),
        page_start=raw_entry.get("page_start"),
        page_end=raw_entry.get("page_end"),
    )
    if not raw_pdf_page_numbers:
        return None
    page_numbers = _dict_book_page_numbers(dict_source, raw_pdf_page_numbers)
    if not page_numbers:
        page_numbers = raw_pdf_page_numbers
    page_urls = _normalize_page_url_list(dict_source, raw_entry.get("page_urls"), page_numbers)
    meta = DICT_SOURCE_META.get(dict_source, {})
    entry_text = _normalize_text_line(raw_entry.get("entry_text"))
    return {
        "id": raw_entry.get("id") or f"{dict_source}:{headword}:{page_numbers[0]}",
        "headword": headword,
        "headword_trad": headword_trad,
        "dict_source": dict_source,
        "dict_label": meta.get("label", dict_source),
        "entry_text": entry_text,
        "page_start": page_numbers[0],
        "page_end": page_numbers[-1],
        "page_url": page_urls[0] if page_urls else _dict_page_url(dict_source, page_numbers[0]),
        "page_urls": page_urls,
        "page_numbers": page_numbers,
        "pdf_page_numbers": raw_pdf_page_numbers,
        "page_count": len(page_numbers),
        "sort_order": int(raw_entry.get("sort_order") or meta.get("sort_order", 99)),
        "verified": bool(raw_entry.get("verified", True)),
        "display_mode": "page_images",
        "match_mode": str(raw_entry.get("match_mode") or "headword"),
    }


def _expand_xuci_page_window(entry: dict, *, window_size: int = 20) -> dict:
    if entry.get("dict_source") != "xuci":
        return entry
    headword = _compact_query_text(str(entry.get("headword") or ""))
    if not _is_single_hanzi_query(headword):
        return entry
    anchor_page = _load_xuci_single_char_index().get(headword)
    start_page = int(anchor_page or entry.get("page_start") or 0)
    if start_page <= 0:
        return entry
    page_limit = _dict_book_page_limit("xuci")
    if page_limit <= 0:
        return entry
    end_page = min(page_limit, start_page + window_size - 1)
    page_numbers = list(range(start_page, end_page + 1))
    pdf_page_numbers = [
        pdf_page
        for page_num in page_numbers
        if (pdf_page := _dict_pdf_page("xuci", page_num)) is not None
    ]
    page_urls = _normalize_page_url_list("xuci", None, page_numbers)
    updated = dict(entry)
    updated["page_start"] = page_numbers[0]
    updated["page_end"] = page_numbers[-1]
    updated["page_numbers"] = page_numbers
    updated["pdf_page_numbers"] = pdf_page_numbers
    updated["page_urls"] = page_urls
    updated["page_url"] = page_urls[0] if page_urls else None
    updated["page_count"] = len(page_numbers)
    if anchor_page:
        updated["page_anchor_source"] = "xuci_single_char_index"
    return updated


def _dict_candidate_sort_key(item: dict) -> tuple[int, int, int, str]:
    exact_rank = 0 if str(item.get("match_mode") or "") == "exact_headword" else 1
    return (
        exact_rank,
        int(item.get("sort_order") or 99),
        int(item.get("page_start") or 0),
        str(item.get("headword") or ""),
    )


def _load_headword_page_candidates(clean_q: str, limit: int) -> list[dict]:
    payload = _load_dict_headword_index()
    if not payload:
        return []
    entries = payload.get("entries", payload)
    if not isinstance(entries, dict):
        return []

    candidates = []
    direct_values = entries.get(clean_q)
    if isinstance(direct_values, dict):
        direct_values = [direct_values]
    if isinstance(direct_values, list):
        for item in direct_values:
            payload_item = _build_dict_page_entry_payload(item, fallback_headword=clean_q)
            if payload_item:
                payload_item["match_mode"] = "exact_headword"
                if _is_single_hanzi_query(clean_q):
                    payload_item = _expand_xuci_page_window(payload_item)
                candidates.append(payload_item)

    if len(candidates) >= limit:
        return sorted(candidates, key=_dict_candidate_sort_key)[:limit]

    seen = {(item["dict_source"], item["headword"], item["page_start"], item["page_end"]) for item in candidates}
    for headword_key, raw_items in entries.items():
        if headword_key == clean_q or clean_q not in str(headword_key):
            continue
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            payload_item = _build_dict_page_entry_payload(item, fallback_headword=str(headword_key))
            if not payload_item:
                continue
            candidate_key = (
                payload_item["dict_source"],
                payload_item["headword"],
                payload_item["page_start"],
                payload_item["page_end"],
            )
            if candidate_key in seen:
                continue
            payload_item["match_mode"] = "fuzzy_headword"
            candidates.append(payload_item)
            seen.add(candidate_key)
            if len(candidates) >= limit:
                return sorted(candidates, key=_dict_candidate_sort_key)[:limit]

    return sorted(candidates, key=_dict_candidate_sort_key)[:limit]


def _build_dict_db_entries(clean_q: str, limit: int) -> list[dict]:
    con = _get_dict_db()
    if con is None:
        return []

    try:
        candidate_limit = max(limit * 4, 24)
        source_placeholders = ", ".join("?" for _ in DICT_ENABLED_SOURCES)
        rows = con.execute(
            f"""
            SELECT id, headword, headword_trad, dict_source, entry_text,
                   page_start, page_end, sort_order, page_urls_json
            FROM dict_entries
            WHERE dict_source IN ({source_placeholders})
              AND (headword = ? OR headword_trad = ?)
            ORDER BY sort_order ASC, id ASC
            LIMIT ?
            """,
            (*DICT_ENABLED_SOURCES, clean_q, clean_q, candidate_limit),
        ).fetchall()
        if len(rows) < limit:
            like_rows = con.execute(
                f"""
                SELECT id, headword, headword_trad, dict_source, entry_text,
                       page_start, page_end, sort_order, page_urls_json
                FROM dict_entries
                WHERE dict_source IN ({source_placeholders})
                  AND (headword LIKE ? OR COALESCE(headword_trad, '') LIKE ? OR entry_text LIKE ?)
                ORDER BY
                    CASE
                        WHEN headword = ? OR headword_trad = ? THEN 0
                        WHEN headword LIKE ? OR COALESCE(headword_trad, '') LIKE ? THEN 1
                        ELSE 2
                    END,
                    sort_order ASC,
                    id ASC
                LIMIT ?
                """,
                (
                    *DICT_ENABLED_SOURCES,
                    f"%{clean_q}%",
                    f"%{clean_q}%",
                    f"%{clean_q}%",
                    clean_q,
                    clean_q,
                    f"{clean_q}%",
                    f"{clean_q}%",
                    candidate_limit,
                ),
            ).fetchall()
            seen = {row["id"] for row in rows}
            rows = list(rows) + [row for row in like_rows if row["id"] not in seen]

        return [_build_dict_entry_payload(row) for row in rows[:limit]]
    finally:
        con.close()


def _search_moe_revised_entries(clean_q: str, limit: int) -> dict:
    metadata = _load_moe_revised_metadata()
    con = _get_moe_revised_db()
    if con is None:
        return {
            "available": False,
            "source_id": "moe_revised",
            "label": metadata.get("label"),
            "description": metadata.get("description"),
            "license": metadata.get("license"),
            "entries": [],
            "source_mode": "unavailable",
        }

    compact_q = _compact_query_text(clean_q)
    compact_q_trad = _to_traditional(compact_q)
    seen = set()
    entries = []

    def append_rows(rows: list[sqlite3.Row], match_mode: str):
        for row in rows:
            row_id = row["id"]
            if row_id in seen:
                continue
            seen.add(row_id)
            headword = row["headword"] or clean_q
            entries.append(
                {
                    "id": row_id,
                    "headword": headword,
                    "bopomofo": row["bopomofo"] or "",
                    "pinyin": row["pinyin"] or "",
                    "content_text": row["content_text"] or "",
                    "match_mode": match_mode,
                    "dict_label": metadata.get("label", "教育部《重编国语辞典修订本》"),
                    "license": metadata.get("license", "CC BY-ND 3.0 TW"),
                    "source_url": _build_moe_search_url("dict.revised.moe.edu.tw", headword),
                }
            )

    try:
        exact_rows = con.execute(
            """
            SELECT id, headword, bopomofo, pinyin, content_text
            FROM entries
            WHERE headword_norm = ? OR headword_norm = ?
            ORDER BY LENGTH(headword) ASC, id ASC
            LIMIT ?
            """,
            (compact_q_trad, compact_q, limit),
        ).fetchall()
        append_rows(exact_rows, "exact_headword")

        if len(entries) < limit:
            prefix_rows = con.execute(
                """
                SELECT id, headword, bopomofo, pinyin, content_text
                FROM entries
                WHERE headword_norm LIKE ? OR headword_norm LIKE ?
                ORDER BY
                    CASE WHEN headword_norm = ? OR headword_norm = ? THEN 0 ELSE 1 END,
                    LENGTH(headword) ASC,
                    id ASC
                LIMIT ?
                """,
                (f"{compact_q_trad}%", f"{compact_q}%", compact_q_trad, compact_q, max(limit * 2, 8)),
            ).fetchall()
            append_rows(prefix_rows, "prefix_headword")

        if len(entries) < limit:
            like_rows = con.execute(
                """
                SELECT id, headword, bopomofo, pinyin, content_text
                FROM entries
                WHERE headword_norm LIKE ? OR headword_norm LIKE ?
                ORDER BY LENGTH(headword) ASC, id ASC
                LIMIT ?
                """,
                (f"%{compact_q_trad}%", f"%{compact_q}%", max(limit * 2, 8)),
            ).fetchall()
            append_rows(like_rows, "fuzzy_headword")
    finally:
        con.close()

    source_mode = "sqlite_exact" if entries else "sqlite_miss"
    return {
        "available": bool(entries),
        "source_id": "moe_revised",
        "label": metadata.get("label", "教育部《重编国语辞典修订本》"),
        "description": metadata.get("description"),
        "license": metadata.get("license", "CC BY-ND 3.0 TW"),
        "download_page": metadata.get("download_page"),
        "built_at": metadata.get("built_at"),
        "term_count": metadata.get("term_count") or 0,
        "row_count": metadata.get("row_count") or 0,
        "source_mode": source_mode,
        "entries": entries[:limit],
    }


def _get_moe_idioms_db() -> Optional[sqlite3.Connection]:
    if not DICT_MOE_IDIOMS_DB_PATH.exists() or DICT_MOE_IDIOMS_DB_PATH.stat().st_size <= 0:
        return None
    con = sqlite3.connect(
        DICT_MOE_IDIOMS_DB_PATH,
        check_same_thread=False,
        timeout=SQLITE_CONNECT_TIMEOUT_SEC,
    )
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return con


def _load_moe_idioms_metadata() -> dict:
    con = _get_moe_idioms_db()
    if con is None:
        return {
            "available": False,
            "label": "教育部《成語典》",
            "description": "教育部授权成语数据包尚未导入站内结果区。",
            "license": "CC BY-ND 3.0 TW",
        }
    try:
        rows = con.execute("SELECT key, value FROM metadata").fetchall()
    except sqlite3.DatabaseError:
        con.close()
        return {
            "available": False,
            "label": "教育部《成語典》",
            "description": "教育部授权成语数据包读取失败。",
            "license": "CC BY-ND 3.0 TW",
        }
    data = {"available": True}
    for row in rows:
        try:
            data[row["key"]] = json.loads(row["value"])
        except Exception:
            data[row["key"]] = row["value"]
    con.close()
    return data


def _search_moe_idioms_entries(clean_q: str, limit: int) -> dict:
    metadata = _load_moe_idioms_metadata()
    con = _get_moe_idioms_db()
    if con is None:
        return {
            "available": False,
            "source_id": "moe_idioms",
            "label": metadata.get("label"),
            "description": metadata.get("description"),
            "license": metadata.get("license"),
            "entries": [],
            "source_mode": "unavailable",
        }

    compact_q = _compact_query_text(clean_q)
    compact_q_trad = _to_traditional(compact_q)
    seen = set()
    entries = []

    def _parse_idiom_sections(raw_json_str: str) -> dict:
        """Parse raw_json into structured sections for frontend display."""
        try:
            raw = json.loads(raw_json_str) if raw_json_str else {}
        except (json.JSONDecodeError, TypeError):
            return {}

        import re as _re_local

        def _clean(val: str) -> str:
            """Remove XML escape artifacts from raw XLSX cell values."""
            t = (val or "").strip()
            if not t:
                return ""
            t = _re_local.sub(r"_x[0-9A-Fa-f]{4}_", "", t)
            t = _re_local.sub(r"\*\d+\*", "", t)
            t = _re_local.sub(r"#", "", t)
            return t.strip()

        sections = {}
        if raw.get("釋義"):
            sections["definition"] = _clean(raw["釋義"])
        if raw.get("典故說明"):
            sections["story"] = _clean(raw["典故說明"])
        source_name = (raw.get("典源文獻名稱") or "").strip()
        source_text = _clean(raw.get("典源文獻內容") or "")
        if source_text:
            source_text = _re_local.sub(r"\n(?!\n)", "", source_text)
            source_text = _re_local.sub(r"\n{2,}", "\n", source_text)
            sections["source_text"] = source_text.strip()
        if source_name:
            sections["source_name"] = source_name
        source_notes = _clean(raw.get("典源注解") or "")
        if source_notes:
            sections["source_notes"] = source_notes
        usage_cat = _clean(raw.get("用法說明-使用類別") or "")
        if usage_cat:
            sections["usage_category"] = usage_cat
        usage_desc = _clean(raw.get("用法說明-語義說明") or "")
        if usage_desc:
            sections["usage_description"] = usage_desc
        usage_examples = _clean(raw.get("用法說明-例句") or "")
        if usage_examples:
            sections["usage_examples"] = usage_examples
        citations = _clean(raw.get("書證") or "")
        if citations:
            sections["citations"] = citations
        synonyms = _clean(raw.get("近義成語") or "")
        if synonyms:
            sections["synonyms"] = synonyms
        antonyms = _clean(raw.get("反義成語") or "")
        if antonyms:
            sections["antonyms"] = antonyms
        discrimination = _clean(raw.get("辨似") or raw.get("辨識") or "")
        if discrimination:
            sections["discrimination"] = discrimination
        ref_words = _clean(raw.get("參考詞語") or "")
        if ref_words:
            sections["ref_words"] = ref_words
        return sections

    def append_rows(rows: list[sqlite3.Row], match_mode: str):
        for row in rows:
            row_id = row["id"]
            if row_id in seen:
                continue
            seen.add(row_id)
            headword = row["headword"] or clean_q
            sections = _parse_idiom_sections(row["raw_json"] if "raw_json" in row.keys() else "")
            entries.append(
                {
                    "id": row_id,
                    "headword": headword,
                    "bopomofo": row["bopomofo"] or "",
                    "pinyin": row["pinyin"] or "",
                    "content_text": row["content_text"] or "",
                    "sections": sections,
                    "match_mode": match_mode,
                    "dict_label": metadata.get("label", "教育部《成語典》"),
                    "license": metadata.get("license", "CC BY-ND 3.0 TW"),
                    "source_url": _build_moe_search_url("dict.idioms.moe.edu.tw", headword),
                }
            )

    try:
        exact_rows = con.execute(
            """
            SELECT id, headword, bopomofo, pinyin, content_text, raw_json
            FROM entries
            WHERE headword_norm = ? OR headword_norm = ?
            ORDER BY LENGTH(headword) ASC, id ASC
            LIMIT ?
            """,
            (compact_q_trad, compact_q, limit),
        ).fetchall()
        append_rows(exact_rows, "exact_headword")

        if len(entries) < limit:
            prefix_rows = con.execute(
                """
                SELECT id, headword, bopomofo, pinyin, content_text, raw_json
                FROM entries
                WHERE headword_norm LIKE ? OR headword_norm LIKE ?
                ORDER BY
                    CASE WHEN headword_norm = ? OR headword_norm = ? THEN 0 ELSE 1 END,
                    LENGTH(headword) ASC,
                    id ASC
                LIMIT ?
                """,
                (f"{compact_q_trad}%", f"{compact_q}%", compact_q_trad, compact_q, max(limit * 2, 8)),
            ).fetchall()
            append_rows(prefix_rows, "prefix_headword")

        if len(entries) < limit:
            like_rows = con.execute(
                """
                SELECT id, headword, bopomofo, pinyin, content_text, raw_json
                FROM entries
                WHERE headword_norm LIKE ? OR headword_norm LIKE ?
                ORDER BY LENGTH(headword) ASC, id ASC
                LIMIT ?
                """,
                (f"%{compact_q_trad}%", f"%{compact_q}%", max(limit * 2, 8)),
            ).fetchall()
            append_rows(like_rows, "fuzzy_headword")
    finally:
        con.close()

    source_mode = "sqlite_exact" if entries else "sqlite_miss"
    return {
        "available": bool(entries),
        "source_id": "moe_idioms",
        "label": metadata.get("label", "教育部《成語典》"),
        "description": metadata.get("description"),
        "license": metadata.get("license", "CC BY-ND 3.0 TW"),
        "download_page": metadata.get("download_page"),
        "built_at": metadata.get("built_at"),
        "term_count": metadata.get("term_count") or 0,
        "row_count": metadata.get("row_count") or 0,
        "source_mode": source_mode,
        "entries": entries[:limit],
    }


def _book_page_url(book_key: str | None, page: int | None) -> Optional[str]:
    if not book_key or page is None:
        return None
    short_key = _book_key_to_short.get(book_key, "")
    if not short_key:
        return None
    try:
        page_num = int(page)
    except Exception:
        return None
    return f"{IMG_CDN}/pages/{short_key}/p{page_num}.webp"


def _build_context_snippet(text: str | None, query: str, *, window: int = 180) -> str:
    plain = re.sub(r"\s+", " ", text or "").strip()
    if not plain:
        return ""
    idx = plain.find(query)
    if idx < 0:
        return plain[:window]
    half = max(50, window // 2)
    start = max(0, idx - half)
    end = min(len(plain), idx + len(query) + half)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(plain) else ""
    return f"{prefix}{plain[start:end]}{suffix}"


def _highlight_snippet_terms(snippet: str, *terms: str) -> str:
    highlighted = snippet or ""
    if not highlighted or "<mark>" in highlighted:
        return highlighted
    for term in sorted({str(item or "").strip() for item in terms if str(item or "").strip()}, key=len, reverse=True):
        if term not in highlighted:
            continue
        try:
            highlighted = re.sub(re.escape(term), lambda match: f"<mark>{match.group(0)}</mark>", highlighted, count=1)
            break
        except Exception:
            continue
    return highlighted


def _build_evidence_segments(text: str, intent: str) -> list[str]:
    plain = re.sub(r"\s+", " ", text or "").strip()
    if not plain:
        return []
    pieces = []
    for block in re.split(r"[\r\n]+", plain):
        block = block.strip()
        if not block:
            continue
        parts = [item.strip() for item in re.split(r"(?<=[。！？!?；;])\s*", block) if item.strip()]
        if not parts:
            parts = [block]
        for item in parts:
            if 4 <= len(item) <= EVIDENCE_MAX_SPAN_CHARS:
                pieces.append(item)
    if not pieces:
        pieces = [plain[:EVIDENCE_MAX_SPAN_CHARS]]
    pieces = pieces[:EVIDENCE_MAX_SEGMENTS]
    max_width = 3 if intent == "process" else 2
    windows = []
    seen = set()
    for start in range(len(pieces)):
        combined = ""
        for width in range(max_width):
            idx = start + width
            if idx >= len(pieces):
                break
            combined += pieces[idx]
            candidate = combined.strip()
            if len(candidate) < 8 or len(candidate) > EVIDENCE_MAX_SPAN_CHARS:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            windows.append(candidate)
    return windows or [plain[:EVIDENCE_MAX_SPAN_CHARS]]


def _score_evidence_span(query_text: str, query_profile: dict, candidate: dict, span: str) -> float:
    raw = span.strip()
    compact = _compact_query_text(raw)
    if not raw or not compact:
        return -999.0

    score = 0.0
    target_raw = _clean_query_text(query_profile.get("target") or "")
    target = _compact_query_text(target_raw)
    matched_term = _clean_query_text(candidate.get("matched_term") or "")
    matched_compact = _compact_query_text(matched_term)
    query_compact = _compact_query_text(query_text)
    intent = query_profile.get("intent") or "lookup"

    if target and target in compact:
        score += 22.0
    if matched_compact and matched_compact in compact:
        score += 12.0
    if query_compact and query_compact in compact:
        score += 8.0

    if intent == "definition":
        patterns = (
            rf"(?:^|[。！？；：])(?:什么是)?{re.escape(target_raw)}(?:通常)?是指",
            rf"(?:^|[。！？；：])(?:什么是)?{re.escape(target_raw)}(?:通常)?指",
            rf"(?:^|[。！？；：]){re.escape(target_raw)}(?:是|叫做|称为)",
            rf"(?:把|将)[^。！？；：]{{0,40}}(?:称为|叫做){re.escape(target_raw)}",
            rf"[^。！？；：]{{0,40}}物质(?:称为|叫做){re.escape(target_raw)}",
            rf"什么样的物质才能称为{re.escape(target_raw)}",
        )
        for pattern in patterns:
            if target_raw and re.search(pattern, raw):
                score += 30.0
        for marker in ("是指", "通常指", "称为", "叫做", "是什么", "概念", "定义"):
            if marker in compact:
                score += 10.0
        if any(marker in raw for marker in ("什么样的", "为什么", "有什么不同")):
            score -= 10.0
        if target_raw and re.search(rf"(?:称为|叫做){re.escape(target_raw)}[A-Za-z0-9\u4e00-\u9fff]", raw):
            score -= 16.0
    elif intent == "comparison":
        for marker in ("区别", "不同", "相同", "联系", "相比", "而", "但是"):
            if marker in compact:
                score += 8.0
    elif intent == "example":
        for marker in ("例如", "比如", "如", "实例", "例子", "典型"):
            if marker in compact:
                score += 8.0
    elif intent == "reason":
        for marker in ("原因", "因为", "由于", "所以", "导致"):
            if marker in compact:
                score += 8.0
    elif intent == "process":
        for marker in ("过程", "步骤", "先", "然后", "接着", "最后"):
            if marker in compact:
                score += 7.0

    score -= max(0.0, (len(raw) - 120) * 0.08)
    if len(raw) < 10:
        score -= 8.0
    return score


def _extract_candidate_evidence(query_text: str, query_profile: dict, candidate: dict) -> dict:
    text = candidate.get("text") or ""
    fallback_snippet = candidate.get("snippet") or _build_context_snippet(
        text,
        candidate.get("matched_term") or query_profile.get("target") or query_text,
    )
    if not text:
        return {"snippet": fallback_snippet, "score": 0.0, "source": "context", "text": fallback_snippet}

    cache_key = (
        hashlib.md5(text.encode("utf-8")).hexdigest()[:16],
        _compact_query_text(query_text),
        _compact_query_text(query_profile.get("target") or ""),
        query_profile.get("intent") or "lookup",
        _compact_query_text(candidate.get("matched_term") or ""),
    )
    cached = EVIDENCE_SPAN_CACHE.get(cache_key)
    if cached is not None:
        return cached

    best_span = ""
    best_score = -999.0
    for span in _build_evidence_segments(text, query_profile.get("intent") or "lookup"):
        span_score = _score_evidence_span(query_text, query_profile, candidate, span)
        if span_score > best_score:
            best_score = span_score
            best_span = span

    if best_span and best_score >= 18.0:
        result = {
            "snippet": _highlight_snippet_terms(
                best_span,
                candidate.get("matched_term") or "",
                query_profile.get("target") or "",
                query_text,
            ),
            "score": best_score,
            "source": "sentence",
            "text": best_span,
        }
    else:
        result = {
            "snippet": _highlight_snippet_terms(
                fallback_snippet,
                candidate.get("matched_term") or "",
                query_profile.get("target") or "",
                query_text,
            ),
            "score": max(0.0, best_score),
            "source": "context",
            "text": fallback_snippet,
        }

    EVIDENCE_SPAN_CACHE[cache_key] = result
    return result


def _query_coverage_bonus(query_text: str, candidate: dict) -> float:
    compact_query = _compact_query_text(query_text)
    if len(compact_query) < 2:
        return 0.0
    reference = _compact_query_text(
        "\n".join(
            part for part in (
                candidate.get("snippet") or "",
                (candidate.get("text") or "")[:600],
            ) if part
        )
    )
    if not reference:
        return 0.0

    bonus = 0.0
    if compact_query in reference:
        bonus += 42.0

    bigrams = []
    for index in range(len(compact_query) - 1):
        token = compact_query[index:index + 2]
        if len(token) == 2:
            bigrams.append(token)
    if bigrams:
        unique_bigrams = tuple(dict.fromkeys(bigrams))
        matched_bigrams = sum(1 for token in unique_bigrams if token in reference)
        bonus += (matched_bigrams / max(1, len(unique_bigrams))) * 28.0

    unique_chars = tuple(dict.fromkeys(ch for ch in compact_query if ch.strip()))
    if unique_chars:
        matched_chars = sum(1 for ch in unique_chars if ch in reference)
        coverage = matched_chars / max(1, len(unique_chars))
        bonus += coverage * 18.0
        if coverage < 0.42:
            bonus -= 18.0

    return max(-18.0, min(68.0, bonus))


def _extract_passage_heading(text: str | None) -> str:
    for raw_line in (text or "").splitlines()[:6]:
        line = raw_line.strip().strip("*")
        if not line:
            continue
        if len(line) > 28:
            continue
        if re.fullmatch(r"[0-9一二三四五六七八九十百千（）()·.、 ]+", line):
            continue
        if any(mark in line for mark in ("。", "？", "！", "；", "：", ":")):
            continue
        return line
    return ""


def _classical_marker_score(text: str) -> int:
    if not text:
        return 0
    score = 0
    for marker in CLASSICAL_MARKERS_STRONG:
        score += min(4, text.count(marker)) * 2
    for marker in CLASSICAL_MARKERS_LIGHT:
        score += min(4, text.count(marker))
    if "《" in text and "》" in text:
        score += 2
    return score


def _looks_like_poem(text: str) -> bool:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 4:
        return False
    sample = lines[:16]
    short_lines = []
    for line in sample:
        normalized = re.sub(r"[（(].*?[)）]", "", line)
        normalized = normalized.strip("· ")
        if 2 <= len(normalized) <= 18 and not normalized.endswith("。"):
            short_lines.append(normalized)
    return len(short_lines) >= 4 and len(short_lines) >= max(4, int(len(sample) * 0.4))


def _find_textbook_classics_hits(book_key: str | None, logical_page: int | None) -> list[dict]:
    if not book_key or logical_page is None:
        return []
    manifest = _load_textbook_classics_manifest()
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


def _find_textbook_classics_hit(book_key: str | None, logical_page: int | None) -> Optional[dict]:
    hits = _find_textbook_classics_hits(book_key, logical_page)
    return hits[0] if hits else None


def _book_has_textbook_classics_manifest(book_key: str | None) -> bool:
    if not book_key:
        return False
    manifest = _load_textbook_classics_manifest()
    ranges = manifest.get(book_key)
    return isinstance(ranges, list) and bool(ranges)


def _clip_textbook_classics_text(text: str | None, manifest_hit: dict | None = None) -> str:
    clipped = (text or "").strip()
    if not clipped:
        return ""

    start_index = 0
    if manifest_hit:
        start_marker = str(manifest_hit.get("start_marker") or "").strip()
        if not start_marker:
            start_marker = str(manifest_hit.get("title") or "").strip()
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

    trim_points = []
    for hint in TEXTBOOK_CLASSICS_TRIM_HINTS:
        marker_index = clipped.find(hint)
        if marker_index > 24:
            trim_points.append(marker_index)
    if trim_points:
        clipped = clipped[:min(trim_points)]

    return clipped.strip()


def _score_textbook_classical_row(row: sqlite3.Row) -> int:
    text = _normalize_text_line(row["text"])
    if not text or len(text) < 12:
        return 0

    if any(hint in text[:120] for hint in CLASSICAL_TEXTBOOK_EXCLUDE_HINTS) and not _looks_like_poem(text):
        return 0

    score = _classical_marker_score(text)
    if _looks_like_poem(text):
        score += 6
    heading = _extract_passage_heading(text)
    if heading:
        score += 2
    if re.search(r"[兮矣焉哉曰]", text):
        score += 3
    return score


def _score_gaokao_classical_row(row: sqlite3.Row) -> int:
    text = _normalize_text_line(row["text"])
    if not text or len(text) < 12:
        return 0
    title = _normalize_text_line(row["title"])
    category = _normalize_text_line(row["category"])
    hint_score = sum(
        4 for hint in CLASSICAL_GAOKAO_HINTS
        if hint in title or hint in category
    )
    score = hint_score + _classical_marker_score(text)
    if _looks_like_poem(text):
        score += 4
    return score


def _build_dict_entry_payload(row: sqlite3.Row) -> dict:
    dict_source = row["dict_source"] or "changyong"
    meta = DICT_SOURCE_META.get(dict_source, {})
    raw_page_start = row["page_start"]
    raw_page_end = row["page_end"] if "page_end" in row.keys() else row["page_start"]
    raw_pdf_page_numbers = _normalize_page_number_list(None, page_start=raw_page_start, page_end=raw_page_end)
    page_numbers = _dict_book_page_numbers(dict_source, raw_pdf_page_numbers)
    if not page_numbers:
        page_numbers = raw_pdf_page_numbers
    page_urls = []
    if "page_urls_json" in row.keys() and row["page_urls_json"]:
        loaded = _load_json_list(row["page_urls_json"])
        page_urls = [item for item in loaded if isinstance(item, str) and item.strip()]
    if not page_urls:
        page_urls = _dict_page_urls(dict_source, page_numbers[0], page_numbers[-1])
    return {
        "id": row["id"],
        "headword": row["headword"],
        "headword_trad": row["headword_trad"] if "headword_trad" in row.keys() else None,
        "dict_source": dict_source,
        "dict_label": meta.get("label", dict_source),
        "entry_text": row["entry_text"],
        "page_start": page_numbers[0],
        "page_end": page_numbers[-1],
        "page_url": page_urls[0] if page_urls else _dict_page_url(dict_source, page_numbers[0]),
        "page_urls": page_urls,
        "page_numbers": page_numbers,
        "pdf_page_numbers": raw_pdf_page_numbers,
        "page_count": len(page_numbers) if page_numbers else len(page_urls),
        "sort_order": row["sort_order"] if "sort_order" in row.keys() else meta.get("sort_order", 99),
        "display_mode": "page_images",
    }


def _build_dict_chat_prompt(
    headword: str,
    user_message: str,
    *,
    dict_context: str,
    textbook_context: str,
    gaokao_context: str,
    history: list[dict] | None,
    phase: str | None = None,
) -> str:
    history_text = "\n".join(_format_chat_history_lines(history)) or "（无）"
    _display_phase = "初中" if phase == "初中" else "高中"

    _gaokao_section = ""
    _gaokao_instruction = ""
    if phase == "初中":
        _gaokao_section = "考试真题证据：\n（初中暂无考试真题数据）"
        _gaokao_instruction = '【考点提示】根据教材证据总结可能的考查方向；如证据不足就写"证据不足"。'
    else:
        _gaokao_section = f"语文真题中的古文 / 古诗词证据：\n{gaokao_context[:4000] or '（无）'}"
        _gaokao_instruction = "【真题拿分】只根据给定真题证据，总结高频考法、常见误判、答题抓手。"

    return f'''你是"实虚词典"的古汉语学习教练，面向{_display_phase}学生。用户当前检索的是「{headword}」。

词典证据：
{dict_context[:5000] or '（无）'}

教材中的古文 / 古诗词证据：
{textbook_context[:5000] or '（无）'}

{_gaokao_section}

历史对话：
{history_text}

用户本轮问题：
{user_message}

请按这个结构回答：
【字词定位】先说明这是实词、虚词，或两者兼有；若证据不足，明确写"证据不足"。
【教材必记】只根据给定教材证据，提炼最该记住的义项、句法位置、固定搭配。
{_gaokao_instruction}
【速记方法】给出可直接背诵的 3-6 条记忆或辨析规则。

规则：
1. 只能依据给定证据，不得编造出处、页码、义项或例句。
2. 若同一字在三本词典解释不同，要点明差异。
3. 若用户追问，保持上下文连续，不重复整段前文。
4. 语言尽量具体，面向{_display_phase}语文。
5. 优先使用简体；若涉及《辞源》原字头，可顺带标出繁体。'''


def _build_dict_context_block(entries: list[dict]) -> str:
    blocks = []
    for item in entries[:8]:
        page_numbers = item.get("page_numbers") or []
        if page_numbers:
            if len(page_numbers) > 1:
                page_label = f"p{page_numbers[0]}-{page_numbers[-1]}"
            else:
                page_label = f"p{page_numbers[0]}"
        else:
            page_label = "p?"
        trad = item.get("headword_trad")
        trad_text = f"（{trad}）" if trad and trad != item.get("headword") else ""
        entry_text = _normalize_text_line(item.get("entry_text")) or f"学生端仅展示馆藏原页图片，当前条目定位到 {page_label}。"
        blocks.append(f"[{item.get('dict_label', item.get('dict_source', '词典'))}·{page_label}] {item.get('headword', '')}{trad_text}\n{entry_text[:420]}")
    return "\n\n".join(blocks)


def _build_dict_textbook_context_block(results: list[dict]) -> str:
    blocks = []
    for item in results[:8]:
        display_title = item.get("display_title") or item.get("title") or ""
        page_label = f"p{item.get('logical_page')}" if item.get("logical_page") is not None else ""
        blocks.append(f"[教材·{display_title}·{page_label}] {(_normalize_text_line(item.get('text')) or _normalize_text_line(item.get('snippet')))[:260]}")
    return "\n\n".join(blocks)


def _build_dict_gaokao_context_block(results: list[dict]) -> str:
    blocks = []
    for item in results[:6]:
        meta = " · ".join(str(part) for part in (item.get("year"), item.get("category"), item.get("title")) if part)
        blocks.append(f"[真题·{meta}] {(_normalize_text_line(item.get('text')) or _normalize_text_line(item.get('snippet')))[:260]}")
    return "\n\n".join(blocks)


def _build_moe_context_block(entries: list[dict], label: str) -> str:
    if not entries:
        return ""
    lines = []
    for item in entries[:4]:
        headword = item.get("headword", "")
        pronunciation = " · ".join(filter(None, [item.get("bopomofo"), item.get("pinyin")]))
        meta = f" · {pronunciation}" if pronunciation else ""
        text = str(item.get("content_text") or "")[:500]
        lines.append(f"[{label}{meta}] {headword}\n{text}")
    return "\n\n".join(lines)


def _build_dict_chat_context_for_request(headword: str, *, phase: str | None = None) -> dict:
    dict_payload = dict_search(headword, limit=8)
    textbook_payload = dict_textbook(headword, limit=8, phase=phase)
    dict_entries = dict_payload.get("entries") or []
    textbook_results = textbook_payload.get("results") or []
    gaokao_results: list[dict] = []
    if phase != "初中":
        gaokao_payload = dict_gaokao(headword, limit=6)
        gaokao_results = gaokao_payload.get("results") or []

    moe_revised_payload = _search_moe_revised_entries(headword, limit=4)
    moe_revised_entries = moe_revised_payload.get("entries") or []
    moe_idiom_payload = _search_moe_idioms_entries(headword, limit=4)
    moe_idiom_entries = moe_idiom_payload.get("entries") or []

    moe_revised_context = _build_moe_context_block(moe_revised_entries, "教育部修訂本")
    moe_idiom_context = _build_moe_context_block(moe_idiom_entries, "教育部成語典")

    dict_context_parts = [_build_dict_context_block(dict_entries)]
    if moe_revised_context:
        dict_context_parts.append(moe_revised_context)
    if moe_idiom_context:
        dict_context_parts.append(moe_idiom_context)
    combined_dict_context = "\n\n".join(part for part in dict_context_parts if part)

    total_evidence = len(dict_entries) + len(textbook_results) + len(gaokao_results) + len(moe_revised_entries) + len(moe_idiom_entries)
    return {
        "dict_entries": dict_entries,
        "textbook_results": textbook_results,
        "gaokao_results": gaokao_results,
        "moe_revised_entries": moe_revised_entries,
        "moe_idiom_entries": moe_idiom_entries,
        "dict_context": combined_dict_context,
        "textbook_context": _build_dict_textbook_context_block(textbook_results),
        "gaokao_context": _build_dict_gaokao_context_block(gaokao_results),
        "phase": phase,
        "summary": {
            "subject_count": 1 if textbook_results or gaokao_results else 0,
            "evidence_count": total_evidence,
            "gaokao_hit_count": len(gaokao_results),
        },
    }


def _build_moe_search_url(host: str, query: str) -> str:
    return (
        f"https://{host}/search.jsp"
        f"?la=1&powerMode=0&md=1&word={quote(query, safe='')}&qMd=0&qCol=1"
    )


def _build_moe_variant_url(query: str) -> str:
    return f"https://dict.variants.moe.edu.tw/search.jsp?QTP=0&WORD={quote(query, safe='')}&la=1"


def _build_external_reference_payload(query: str) -> dict:
    compact_query = _compact_query_text(query)
    compact_query_trad = _to_traditional(compact_query)
    single_char = _is_single_hanzi_query(compact_query)
    split_chars = _unique_query_characters(compact_query)

    references = [
        {
            "id": "moe_revised",
            "label": "教育部《重编国语辞典修订本》",
            "category": "official",
            "scope": "字词",
            "match_mode": "exact_term",
            "integration_mode": "deep_link",
            "priority": 1,
            "summary": "官方大型国语辞典，适合核对本义、书证和历史用法。本站查典页已补只读结果区，这里保留官方原站入口。",
            "url": _build_moe_search_url("dict.revised.moe.edu.tw", compact_query_trad),
            "action_label": "打开重编",
        },
        {
            "id": "moe_concised",
            "label": "教育部《国语辞典简编本》",
            "category": "official",
            "scope": "字词",
            "match_mode": "exact_term",
            "integration_mode": "deep_link",
            "priority": 2,
            "summary": "官方简明释义，适合学生先看常见义，再回到古文语境辨析。",
            "url": _build_moe_search_url("dict.concised.moe.edu.tw", compact_query_trad),
            "action_label": "打开简编",
        },
        {
            "id": "moe_idioms",
            "label": "教育部《成語典》",
            "category": "official",
            "scope": "成语",
            "match_mode": "exact_term",
            "integration_mode": "deep_link",
            "priority": 3,
            "summary": "官方成语辞典，收录常用成语释义、典故出处和用法辨析。本站查典页已补只读结果区。",
            "url": _build_moe_search_url("dict.idioms.moe.edu.tw", compact_query_trad),
            "action_label": "打开成语典",
        },
    ]

    if single_char:
        references.extend(
            [
                {
                    "id": "moe_variants",
                    "label": "教育部《异体字字典》",
                    "category": "official",
                    "scope": "单字",
                    "match_mode": "single_char",
                    "integration_mode": "deep_link",
                    "priority": 3,
                    "summary": "适合核对正字、异体、繁简和古文常见写法差异。",
                    "url": _build_moe_variant_url(compact_query),
                    "action_label": "看异体",
                },
                {
                    "id": "zi_tools",
                    "label": "zi.tools",
                    "category": "supplementary",
                    "scope": "单字",
                    "match_mode": "single_char",
                    "integration_mode": "deep_link",
                    "priority": 4,
                    "summary": "适合补充字形、字源、部件和音韵信息，不作为本站核心判定来源。",
                    "url": f"https://zi.tools/zi/{quote(compact_query, safe='')}",
                    "action_label": "打开 zi.tools",
                },
                {
                    "id": "humanum",
                    "label": "汉语多功能字库",
                    "category": "supplementary",
                    "scope": "单字",
                    "match_mode": "single_char",
                    "integration_mode": "deep_link",
                    "priority": 5,
                    "summary": "适合深查说文、广韵、形义通解和部件树，作为单字深层补充。",
                    "url": f"https://humanum.arts.cuhk.edu.hk/Lexis/lexi-mf/search.php?word={quote(compact_query, safe='')}",
                    "action_label": "打开字库",
                },
            ]
        )
    elif split_chars:
        split_items = [{"char": ch, "url": f"https://zi.tools/zi/{quote(ch, safe='')}"} for ch in split_chars]
        references.extend(
            [
                {
                    "id": "zi_tools_chars",
                    "label": "zi.tools 单字拆查",
                    "category": "supplementary",
                    "scope": "拆字",
                    "match_mode": "split_chars",
                    "integration_mode": "split_chars",
                    "priority": 4,
                    "summary": "zi.tools 更适合单字。多字词建议拆成关键字逐个核对字形和字源。",
                    "items": split_items,
                },
                {
                    "id": "humanum_chars",
                    "label": "汉语多功能字库单字拆查",
                    "category": "supplementary",
                    "scope": "拆字",
                    "match_mode": "split_chars",
                    "integration_mode": "split_chars",
                    "priority": 5,
                    "summary": "多字词在字库中同样建议拆字查看，重点看构形和古文字材料。",
                    "items": [
                        {
                            "char": ch,
                            "url": f"https://humanum.arts.cuhk.edu.hk/Lexis/lexi-mf/search.php?word={quote(ch, safe='')}",
                        }
                        for ch in split_chars
                    ],
                },
            ]
        )

    references.sort(key=lambda item: (item.get("priority", 99), item.get("label", "")))
    return {
        "query": query,
        "compact_query": compact_query,
        "query_kind": "single_char" if single_char else "term",
        "references": references,
    }


def _build_dict_status_payload() -> dict:
    payload = _load_dict_headword_index()
    qc_payload = _load_dict_qc_payload()
    source_summaries = {}
    payload_sources = payload.get("sources") if isinstance(payload, dict) else {}
    if not isinstance(payload_sources, dict):
        payload_sources = {}

    entries = payload.get("entries") if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        entries = {}

    verified_counts = Counter()
    for raw_items in entries.values():
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            source = str(item.get("dict_source") or item.get("source") or "").strip()
            if source:
                verified_counts[source] += 1

    for source, meta in DICT_SOURCE_META.items():
        record = payload_sources.get(source, {}) if source in payload_sources else {}
        if not isinstance(record, dict):
            record = {}
        qc_record = qc_payload.get(source) if isinstance(qc_payload, dict) else {}
        if not isinstance(qc_record, dict):
            qc_record = {}
        verified = int(record.get("verified_headwords") or verified_counts.get(source, 0))
        candidate = int(record.get("candidate_headwords") or 0)
        source_summaries[source] = {
            "label": meta["label"],
            "enabled": source in DICT_ENABLED_SOURCE_SET,
            "verified_headwords": verified,
            "candidate_headwords": candidate,
            "has_candidates": candidate > 0 or verified > 0,
            "coverage_ratio": qc_record.get("coverage_ratio"),
            "page_count": meta["page_count"],
        }

    return {
        "available": bool(entries),
        "built_at": payload.get("built_at") if isinstance(payload, dict) else None,
        "enabled_sources": list(DICT_ENABLED_SOURCES),
        "student_safe_mode": "page_images_only",
        "external_reference_mode": "deep_links",
        "source_summaries": source_summaries,
    }


@app.get("/api/subject-meta")
def subject_meta_endpoint(
    phase: str = Query(..., description="学段: 高中 or 初中"),
):
    """Return subject metadata for a given phase."""
    return _subject_meta_for_phase(phase)


@app.get("/api/search")
def search(
    q: str = Query(..., min_length=1, max_length=200),
    subject: Optional[str] = Query(None),
    scope_subject: Optional[str] = Query(None, description="Restrict textbook search to one subject across all books"),
    book_key: Optional[str] = Query(None),
    source: Optional[str] = Query(None, description="Filter by source: textbook, gaokao, or all"),
    phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all"),
    sort: str = Query("relevance"),
    has_images: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Full-text search with concept-first parsing and full-textbook fallback."""
    con = get_db()
    try:
        clean_q = _clean_query_text(q)
        if not clean_q:
            raise HTTPException(400, "Invalid query")

        where_extra = ""
        filter_params = []
        if phase:
            where_extra += " AND c.phase = ?"
            filter_params.append(phase)
        if subject:
            where_extra += " AND c.subject = ?"
            filter_params.append(subject)
        if book_key:
            where_extra += " AND c.book_key = ?"
            filter_params.append(book_key)
        elif scope_subject:
            where_extra += " AND (c.source = 'mineru' OR c.source IS NULL) AND c.subject = ?"
            filter_params.append(scope_subject)
        if source == 'textbook':
            where_extra += " AND (c.source = 'mineru' OR c.source IS NULL)"
        elif source == 'gaokao':
            where_extra += " AND c.source = 'gaokao'"

        candidate_limit = _candidate_window_limit(limit, offset, multiplier=2, minimum=max(24, limit * 2), cap=600)
        query_analysis = _analyze_search_query(
            con,
            q,
            scope_subject=scope_subject or subject,
            book_key=book_key,
        )
        search_plan = _build_search_term_plan(query_analysis)
        textbook_scope_subject = scope_subject or subject
        use_hybrid_search = (
            source != "gaokao"
            and sort == "relevance"
            and not has_images
            and len(_compact_query_text(clean_q)) >= 2
        )

        hybrid_meta = None
        if use_hybrid_search:
            hybrid_rows, hybrid_meta = _collect_hybrid_search_rows(
                con,
                q,
                query_analysis,
                scope_subject=textbook_scope_subject,
                book_key=book_key,
                phase=phase,
                candidate_limit=candidate_limit,
            )
            all_rows = list(hybrid_rows)
            seen_ids = {row.get("id") for row in all_rows if row.get("id") is not None}
            include_gaokao = not book_key and not scope_subject and source != "textbook" and phase != "初中"
            if include_gaokao:
                gaokao_where_extra = " AND c.source = 'gaokao'"
                gaokao_filter_params = []
                if subject:
                    gaokao_where_extra += " AND c.subject = ?"
                    gaokao_filter_params.append(subject)
                gaokao_rows = _collect_legacy_search_rows(
                    con,
                    q,
                    query_analysis,
                    search_plan,
                    where_extra=gaokao_where_extra,
                    filter_params=gaokao_filter_params,
                    candidate_limit=max(12, min(60, math.ceil(candidate_limit / 2))),
                    sort=sort,
                    source="gaokao",
                    include_supplemental=False,
                    seen_ids=seen_ids,
                    rank_shift=40.0,
                )
                all_rows = _merge_ranked_rows(all_rows, gaokao_rows, sort=sort)
            else:
                all_rows = _merge_ranked_rows(all_rows, sort=sort)
        else:
            all_rows = _collect_legacy_search_rows(
                con,
                q,
                query_analysis,
                search_plan,
                where_extra=where_extra,
                filter_params=filter_params,
                candidate_limit=candidate_limit,
                sort=sort,
                scope_subject=textbook_scope_subject,
                book_key=book_key,
                source=source,
                phase=phase,
            )
            all_rows = _merge_ranked_rows(all_rows, sort=sort)

        if has_images:
            all_rows = [r for r in all_rows if '![' in (r.get('text') or '')]
        rows = all_rows[offset: offset + limit]

        by_subject = {}
        for r in rows:
            s = r["subject"]
            if s not in by_subject:
                meta = SUBJECT_META.get(s, {"icon": "📚", "color": "#95a5a6"})
                by_subject[s] = {"subject": s, **meta, "results": [], "count": 0}
            text = r["text"] or ""
            img_count = text.count('![')
            bk = r["book_key"]
            page_num = r["section"] or 0
            page_url = _book_page_url(bk, page_num)
            bm_info = _book_map.get(bk, {})
            result_item = {
                "id": r["id"],
                "book_key": bk,
                "section": r["section"],
                "logical_page": r["logical_page"] if "logical_page" in r.keys() and r["logical_page"] is not None else r["section"],
                "snippet": r["snippet"],
                "snippet_source": r.get("snippet_source") or "context",
                "evidence_score": r.get("evidence_score") or r.get("evidence_bonus") or 0.0,
                "text": text[:2000],
                "image_count": img_count,
                "source": r["source"] or "mineru",
                "match_channel": r.get("match_channel", "fts"),
                "match_basis": r.get("match_basis", "query"),
                "matched_term": r.get("matched_term") or clean_q,
                "retrieval_source": r.get("retrieval_source", "primary"),
                "page_url": page_url,
                "page_num": page_num,
                "total_pages": bm_info.get("pages", 0),
            }
            if (r["source"] or "mineru") != "gaokao":
                result_item = _apply_book_runtime_meta(
                    result_item,
                    book_key=bk,
                    fallback_title=r["title"],
                    content_id=r["content_id"] if "content_id" in r.keys() else None,
                )
            else:
                result_item["title"] = r["title"]
            if r["source"] == "gaokao":
                result_item["year"] = r["year"]
                result_item["category"] = r["category"]
            by_subject[s]["results"].append(result_item)
            by_subject[s]["count"] += 1

        subject_counts = dict(Counter(row["subject"] for row in all_rows))
        total = len(all_rows)

        cross_subjects = [s for s in subject_counts if subject_counts[s] > 0]
        hint = None
        if len(cross_subjects) >= 2:
            names = "、".join(cross_subjects[:4])
            hint = f"💡 「{q}」横跨 {len(cross_subjects)} 个学科（{names}），它们从不同角度描述了同一概念！"

        groups = list(by_subject.values())
        if sort == "cross" or sort == "images":
            groups.sort(key=lambda g: g["count"], reverse=True)

        scope_label = "全部教材"
        if book_key:
            scope_label = "单本教材"
        elif scope_subject:
            scope_label = f"{scope_subject}·全部教材"

        log_search(
            q,
            subject=subject or scope_subject,
            book_key=book_key,
            source=source or ("textbook" if book_key or scope_subject else None),
            result_count=total,
            phase=phase,
        )

        return {
            "query": q,
            "total": total,
            "subject_counts": subject_counts,
            "cross_hint": hint,
            "query_analysis": {
                **query_analysis,
                "scope_label": scope_label,
                "search_plan": hybrid_meta["term_plan"] if hybrid_meta else search_plan,
                "search_mode": hybrid_meta["mode"] if hybrid_meta else "lexical",
                "precision_query": hybrid_meta["precision_mode"] if hybrid_meta else _is_precision_query(q),
                "query_intent": hybrid_meta["query_intent"] if hybrid_meta else None,
                "hybrid_candidate_count": hybrid_meta["candidate_count"] if hybrid_meta else 0,
                "semantic_queries": hybrid_meta["semantic_queries"] if hybrid_meta else [],
                "reranker_loaded": hybrid_meta["reranker_loaded"] if hybrid_meta else bool(reranker),
                "supplemental_semantic_loaded": bool(supplemental_faiss_index),
                "supplemental_index_enabled": _get_supplemental_source_info()["available"],
                "supplemental_index_source": _get_supplemental_source_info()["source"],
                "supplemental_manifest": {
                    "present": bool(_load_supplemental_manifest()),
                    "books": int((_load_supplemental_manifest() or {}).get("books") or 0),
                    "pages": int((_load_supplemental_manifest() or {}).get("pages") or 0),
                    "subjects": int(len((_load_supplemental_manifest() or {}).get("subjects") or {})),
                    "source_files_total": int((_load_supplemental_manifest() or {}).get("source_files_total") or 0),
                    "source_files_indexed": int((_load_supplemental_manifest() or {}).get("source_files_indexed") or 0),
                    "edition_conflicts": int((_load_supplemental_manifest() or {}).get("edition_conflicts") or 0),
                },
            },
            "groups": groups,
        }
    finally:
        con.close()


@app.get("/api/search/trending")
def search_trending(phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all")):
    """Return recent queries and popular queries for display."""
    con = get_db()
    try:
        phase_filter = " AND phase = ?" if phase else ""
        phase_params = [phase] if phase else []
        # Recent unique queries (last 50, deduplicated, max 15)
        recent_rows = con.execute(f"""
            SELECT query, MAX(ts) as latest_ts, MAX(result_count) as cnt
            FROM search_logs
            WHERE result_count > 0{phase_filter}
            GROUP BY query_normalized
            ORDER BY latest_ts DESC
            LIMIT 50
        """, phase_params).fetchall()
        recent = []
        for row in recent_rows:
            query = row["query"]
            if _is_synthetic_query(query):
                continue
            current_count = _current_query_result_count(con, query, phase=phase)
            if current_count <= 0:
                continue
            recent.append({"query": query, "count": current_count})
            if len(recent) >= 15:
                break

        # Popular queries (last 7 days, by frequency, min 2 searches)
        week_ago = time.time() - 7 * 86400
        popular_rows = con.execute(f"""
            SELECT query, query_normalized, COUNT(*) as freq, MAX(result_count) as cnt
            FROM search_logs
            WHERE ts > ? AND result_count > 0{phase_filter}
            GROUP BY query_normalized
            HAVING freq >= 2
            ORDER BY freq DESC
            LIMIT 50
        """, [week_ago] + phase_params).fetchall()
        popular = []
        for row in popular_rows:
            query = row["query"]
            if _is_synthetic_query(query):
                continue
            current_count = _current_query_result_count(con, query, phase=phase)
            if current_count <= 0:
                continue
            popular.append({"query": query, "freq": int(row["freq"] or 0), "count": current_count})
            if len(popular) >= 20:
                break

        return {"recent": recent, "popular": popular}
    finally:
        con.close()


@app.get("/api/stats")
def stats(phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all")):
    """Database statistics (cached 5min)."""
    cache_key = f'stats_{phase or "all"}'
    if cache_key in _cache:
        return _cache[cache_key]
    con = get_db()
    try:
        phase_filter = " AND phase = ?" if phase else ""
        phase_params = (phase,) if phase else ()

        total = con.execute(f"SELECT COUNT(*) FROM chunks WHERE 1=1{phase_filter}", phase_params).fetchone()[0]
        textbook_count = con.execute(
            f"SELECT COUNT(*) FROM chunks WHERE (source='mineru' OR source IS NULL){phase_filter}", phase_params
        ).fetchone()[0]
        gaokao_count = con.execute(
            f"SELECT COUNT(*) FROM chunks WHERE source='gaokao'{phase_filter}", phase_params
        ).fetchone()[0]
        textbook_books = con.execute(
            f"SELECT COUNT(DISTINCT book_key) FROM chunks WHERE (source='mineru' OR source IS NULL){phase_filter}",
            phase_params,
        ).fetchone()[0]
        gaokao_multimodal = con.execute(
            f"SELECT COUNT(*) FROM chunks WHERE source='gaokao' AND text LIKE '%https://img.rdfzer.com/gaokao/%'{phase_filter}",
            phase_params,
        ).fetchone()[0]
        dist = con.execute(
            f"SELECT subject, COUNT(*) as cnt FROM chunks WHERE 1=1{phase_filter} GROUP BY subject ORDER BY cnt DESC",
            phase_params,
        ).fetchall()

        # Gaokao specific stats
        gaokao_years = con.execute(
            f"SELECT MIN(year) as min_y, MAX(year) as max_y FROM chunks WHERE source='gaokao' AND year IS NOT NULL{phase_filter}",
            phase_params,
        ).fetchone()
        gaokao_by_subject = con.execute(
            f"SELECT subject, COUNT(*) as cnt FROM chunks WHERE source='gaokao'{phase_filter} GROUP BY subject ORDER BY cnt DESC",
            phase_params,
        ).fetchall()
        ai_table_counts = {
            "explanations": con.execute("SELECT COUNT(*) FROM ai_explanations").fetchone()[0],
            "synonyms": con.execute("SELECT COUNT(*) FROM ai_synonyms").fetchone()[0],
            "relations": con.execute("SELECT COUNT(*) FROM ai_relations").fetchone()[0],
            "summaries": con.execute("SELECT COUNT(*) FROM ai_summaries").fetchone()[0],
            "gaokao_links": con.execute("SELECT COUNT(*) FROM ai_gaokao_links").fetchone()[0],
        }

        result = {
            "total_chunks": total,
            "textbook_chunks": textbook_count,
            "gaokao_chunks": gaokao_count,
            "textbook_books": textbook_books,
            "gaokao_multimodal": gaokao_multimodal,
            "subjects_count": len(dist),
            "ai_model": AI_SERVICE_LABEL,
            "ai_tables": ai_table_counts,
            "faiss_enabled": faiss_index is not None,
            "faiss_vectors": faiss_index.ntotal if faiss_index else 0,
            "gaokao_year_range": [gaokao_years["min_y"], gaokao_years["max_y"]] if gaokao_years and gaokao_years["min_y"] else None,
            "gaokao_by_subject": [
                {"name": r["subject"], "count": r["cnt"],
                 **SUBJECT_META.get(r["subject"], {"icon": "📚", "color": "#95a5a6"})}
                for r in gaokao_by_subject
            ],
            "subjects": [
                {
                    "name": r["subject"],
                    "count": r["cnt"],
                    **SUBJECT_META.get(r["subject"], {"icon": "📚", "color": "#95a5a6"}),
                }
                for r in dist
            ],
        }
        _cache[cache_key] = result
        return result
    finally:
        con.close()


@app.get("/api/keywords")
def keywords(
    limit: int = Query(120, ge=1, le=500),
    phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all"),
):
    """Return curated academic keywords (cached 5min)."""
    cache_key = f'keywords_{limit}_{phase or "all"}'
    if cache_key in _cache:
        return _cache[cache_key]
    con = get_db()
    try:
        has_table = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='curated_keywords'"
        ).fetchone()
        rows = None
        if has_table:
            has_phase_col = any(
                r[1] == "phase"
                for r in con.execute("PRAGMA table_info(curated_keywords)").fetchall()
            )
            if has_phase_col and phase:
                rows = con.execute(
                    "SELECT term, subject_count, total_count FROM curated_keywords WHERE phase = ? ORDER BY subject_count DESC, total_count DESC LIMIT ?",
                    (phase, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT term, subject_count, total_count FROM curated_keywords ORDER BY subject_count DESC, total_count DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            if rows:
                result = {"keywords": [{"term": r["term"], "subjects": r["subject_count"], "count": r["total_count"]} for r in rows]}
            else:
                rows = None  # fall through to fallback
        if not has_table or rows is None:
            if phase == "初中":
                fallback = ["一次函数", "化学方程式", "光合作用", "丝绸之路", "勾股定理", "欧姆定律",
                            "细胞", "地球运动", "法治", "光的折射", "二元一次方程", "生态系统",
                            "物态变化", "辛亥革命", "全等三角形", "酸碱盐", "大洲大洋", "文言文"]
            else:
                fallback = ["蛋白质", "DNA", "光合作用", "细胞呼吸", "牛顿第二定律", "勒夏特列原理",
                            "氧化还原", "基因表达", "丝绸之路", "全球变暖", "元素周期表", "椭圆",
                            "自然选择", "分离定律", "盖斯定律", "平衡移动", "文艺复兴", "电磁波"]
            result = {"keywords": [{"term": t, "subjects": 0, "count": 0} for t in fallback]}
        _cache[cache_key] = result
        return result
    finally:
        con.close()


@app.get("/api/cross-links")
def cross_links():
    """Dynamic cross-subject concept links from pre-computed concept_map."""
    if 'cross_links' in _cache:
        return _cache['cross_links']
    con = get_db()
    try:
        # Check if concept_map table exists
        has_map = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='concept_map'"
        ).fetchone()

        if has_map:
            # Use concept_map for dynamic discovery
            concept_rows = con.execute("""
                SELECT concept, subject, SUM(count) as cnt
                FROM concept_map
                GROUP BY concept, subject
            """).fetchall()

            concept_subjects = {}
            for r in concept_rows:
                c = r["concept"]
                if c not in concept_subjects:
                    concept_subjects[c] = []
                concept_subjects[c].append((r["subject"], r["cnt"]))

            nodes = []
            links = []
            for concept, subjects in concept_subjects.items():
                if len(subjects) < 2:
                    continue
                total = sum(cnt for _, cnt in subjects)
                nodes.append({"id": concept, "count": total, "subjects": len(subjects)})
                for i, (s1, c1) in enumerate(subjects):
                    for s2, c2 in subjects[i + 1:]:
                        links.append({
                            "source": s1, "target": s2,
                            "concept": concept, "weight": min(c1, c2),
                        })

            # Sort nodes by cross-subject breadth, then frequency
            nodes.sort(key=lambda n: (n["subjects"], n["count"]), reverse=True)
            nodes = nodes[:150]  # cap for performance
            # Filter links to only include concepts in our node set
            node_ids = {n["id"] for n in nodes}
            links = [l for l in links if l["concept"] in node_ids]
        else:
            # Fallback: hardcoded concepts
            fallback = [
                "蛋白质", "DNA", "电子", "光", "溶液", "细胞", "向量", "函数",
                "温室效应", "生态系统", "能量", "平衡", "丝绸之路", "概率", "氧化",
                "光合作用", "进化", "水循环", "原子结构", "全球化",
            ]
            nodes = []
            links = []
            for concept in fallback:
                try:
                    rows = con.execute("""
                        SELECT c.subject, COUNT(*) as cnt
                        FROM chunks c JOIN chunks_fts f ON c.id = f.rowid
                        WHERE chunks_fts MATCH ?
                        GROUP BY c.subject ORDER BY cnt DESC
                    """, [concept]).fetchall()
                except Exception:
                    continue
                subjects = [(r["subject"], r["cnt"]) for r in rows]
                if len(subjects) < 2:
                    continue
                total = sum(c for _, c in subjects)
                nodes.append({"id": concept, "count": total, "subjects": len(subjects)})
                for i, (s1, c1) in enumerate(subjects):
                    for s2, c2 in subjects[i + 1:]:
                        links.append({
                            "source": s1, "target": s2,
                            "concept": concept, "weight": min(c1, c2),
                        })

        # Add cluster info from cross_subject_map if available
        clusters = []
        has_csm = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cross_subject_map'"
        ).fetchone()
        if has_csm:
            cluster_rows = con.execute("""
                SELECT cluster_name, GROUP_CONCAT(DISTINCT subject) as subjects,
                       COUNT(DISTINCT concept) as n_concepts
                FROM cross_subject_map
                GROUP BY cluster_name
                HAVING COUNT(DISTINCT subject) >= 2
                ORDER BY n_concepts DESC
            """).fetchall()
            clusters = [
                {"name": r["cluster_name"], "subjects": r["subjects"].split(","),
                 "concept_count": r["n_concepts"]}
                for r in cluster_rows
            ]

        subject_nodes = [
            {"id": s, "type": "subject", **SUBJECT_META.get(s, {"icon": "📚", "color": "#95a5a6"})}
            for s in SUBJECT_META
        ]
        result = {
            "concept_nodes": nodes,
            "subject_nodes": subject_nodes,
            "links": links,
            "clusters": clusters,
        }
        _cache['cross_links'] = result
        return result
    finally:
        con.close()


@app.get("/api/books")
def books(phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all")):
    """List all textbooks grouped by subject."""
    con = get_db()
    try:
        if phase:
            rows = con.execute("""
                SELECT DISTINCT book_key, title, subject, content_id
                FROM chunks
                WHERE source != 'gaokao' AND phase = ?
                ORDER BY subject, title
            """, (phase,)).fetchall()
        else:
            rows = con.execute("""
                SELECT DISTINCT book_key, title, subject, content_id
                FROM chunks
                WHERE source != 'gaokao'
                ORDER BY subject, title
            """).fetchall()
        by_subject = {}
        seen_book_keys = set()
        for r in rows:
            s = r["subject"]
            manifest_row = _book_version_manifest_row(
                book_key=r["book_key"],
                content_id=r["content_id"] if "content_id" in r.keys() else None,
            )
            if not _is_supported_textbook_book(
                book_key=r["book_key"],
                content_id=r["content_id"] if "content_id" in r.keys() else None,
                subject=r["subject"],
                edition=manifest_row.get("edition"),
                phase=phase,
            ):
                continue
            if s not in by_subject:
                meta = SUBJECT_META.get(s, {"icon": "📚", "color": "#95a5a6"})
                by_subject[s] = {"subject": s, **meta, "books": []}
            seen_book_keys.add(r["book_key"])
            book_item = _apply_book_runtime_meta(
                {
                    "book_key": r["book_key"],
                },
                book_key=r["book_key"],
                fallback_title=r["title"],
                content_id=r["content_id"] if "content_id" in r.keys() else None,
            )
            by_subject[s]["books"].append({
                "book_key": r["book_key"],
                "title": book_item["title"],
                "base_title": book_item["base_title"],
                "edition": book_item.get("edition", ""),
                "source": "primary",
                "has_page_images": True,
            })
        for item in _load_supplemental_book_catalog():
            book_key = str(item.get("book_key") or "").strip()
            subject = str(item.get("subject") or "").strip()
            title = str(item.get("title") or "").strip()
            item_phase = str(item.get("phase") or "高中").strip()
            if phase and item_phase != phase:
                continue
            if (
                not book_key
                or not subject
                or not title
                or book_key in seen_book_keys
                or not _is_supported_textbook_book(
                    book_key=book_key,
                    content_id=item.get("content_id"),
                    subject=subject,
                    edition=item.get("edition"),
                    phase=phase,
                )
            ):
                continue
            if subject not in by_subject:
                meta = SUBJECT_META.get(subject, {"icon": "📚", "color": "#95a5a6"})
                by_subject[subject] = {"subject": subject, **meta, "books": []}
            by_subject[subject]["books"].append({
                "book_key": book_key,
                "title": title,
                "base_title": str(item.get("base_title") or title).strip() or title,
                "edition": str(item.get("edition") or "").strip(),
                "source": str(item.get("source") or "supplemental_only").strip() or "supplemental_only",
                "has_page_images": bool(item.get("has_page_images")),
            })
        for payload in by_subject.values():
            title_counts = Counter(str(item.get("title") or "").strip() for item in payload["books"] if str(item.get("title") or "").strip())
            for item in payload["books"]:
                title = str(item.get("title") or "").strip()
                edition = str(item.get("edition") or "").strip()
                extras = []
                if edition and edition not in title:
                    extras.append(edition)
                if not item.get("has_page_images"):
                    extras.append("补充教材")
                if title and title_counts.get(title, 0) > 1 and not edition:
                    marker = str(item.get("book_key") or "").strip()[-4:]
                    if marker:
                        extras.append(f"#{marker}")
                item["display_title"] = f"{title}（{' · '.join(extras)}）" if title and extras else title
            payload["books"].sort(key=lambda item: (item.get("title") or "", item.get("book_key") or ""))
        return list(by_subject.values())
    finally:
        con.close()


@app.get("/api/related")
def related(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(8, ge=1, le=20),
    phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all"),
):
    """Find recognized concepts that co-occur with the query term."""
    con = get_db()
    try:
        clean_q = re.sub(r'[^\w\u4e00-\u9fff\s]', '', q).strip()
        if not clean_q:
            return []

        # Get text chunks matching the query
        rows = con.execute("""
            SELECT c.subject, c.text
            FROM chunks c
            JOIN chunks_fts f ON c.id = f.rowid
            WHERE chunks_fts MATCH ?
            LIMIT 100
        """, [clean_q]).fetchall()

        if not rows:
            return []

        # Aggregate only exact concept hits already recognized by concept_map.
        word_counter = Counter()
        query_chars = set(clean_q)
        for r in rows:
            text = r["text"] or ""
            subject = r["subject"] or ""
            for concept in _match_concepts(text, subject, con):
                w = concept["concept"]
                if w == clean_q or w in clean_q or clean_q in w:
                    continue
                if w in GRAPH_GENERIC_TERMS:
                    continue
                if len(w) < 2:
                    continue
                word_counter[w] += 1

        candidates = [
            {"term": term, "count": count}
            for term, count in word_counter.most_common(limit * 3)
            if count >= 2
        ][:limit]

        return candidates
    finally:
        con.close()


@app.post("/api/chat/context")
async def chat_context(payload: dict = Body(...)):
    """Build grounded context for AI chat before calling an external model service."""
    query = str(payload.get("query", "")).strip()
    user_message = str(payload.get("user_message", "")).strip()
    history = payload.get("history") or []
    scope_subject = str(payload.get("scope_subject", "")).strip() or None
    book_key = str(payload.get("book_key", "")).strip() or None
    phase = str(payload.get("phase", "")).strip() or None
    return await run_in_threadpool(
        _build_chat_context_for_request,
        query,
        user_message,
        history,
        scope_subject=scope_subject,
        book_key=book_key,
        phase=phase,
    )


@app.post("/api/chat/log")
def chat_log(payload: dict = Body(...)):
    """Client-side fallback telemetry for AI chat."""
    query = str(payload.get("query", "")).strip()
    user_message = str(payload.get("user_message", "")).strip()
    if not query or not user_message:
        raise HTTPException(400, "query and user_message are required")

    summary = payload.get("summary") or {}
    provider = str(payload.get("provider", "")).strip() or AI_SERVICE_LABEL
    success = bool(payload.get("success", True))
    error = str(payload.get("error", "")).strip() or None

    _write_ai_chat_log(
        query,
        user_message,
        summary,
        provider=provider,
        success=success,
        error=error,
    )
    return {"ok": True}


@app.post("/api/chat")
async def chat(payload: dict = Body(...)):
    """Server-side grounded AI chat orchestration."""
    query = str(payload.get("query", "")).strip()
    user_message = str(payload.get("user_message", "")).strip()
    history = payload.get("history") or []
    scope_subject = str(payload.get("scope_subject", "")).strip() or None
    book_key = str(payload.get("book_key", "")).strip() or None
    phase = str(payload.get("phase", "")).strip() or None
    if not query or not user_message:
        raise HTTPException(400, "query and user_message are required")

    context_payload = await run_in_threadpool(
        _build_chat_context_for_request,
        query,
        user_message,
        history,
        scope_subject=scope_subject,
        book_key=book_key,
        phase=phase,
    )
    prompt = _build_chat_prompt(query, user_message, context_payload, history=history)
    try:
        ai_data = await _call_ai_service(prompt)
        await run_in_threadpool(log_ai_chat, query, user_message, context_payload, success=True, phase=phase)
    except HTTPException as e:
        await run_in_threadpool(log_ai_chat, query, user_message, context_payload, success=False, error=str(e.detail), phase=phase)
        raise

    return {
        "answer": ai_data.get("answer"),
        "provider": AI_SERVICE_LABEL,
        "context": {
            "summary": context_payload.get("summary"),
            "evidence": context_payload.get("evidence"),
            "alias_hints": context_payload.get("alias_hints"),
            "relation_hints": context_payload.get("relation_hints"),
            "suggested_questions": context_payload.get("suggested_questions"),
        },
    }


# ── Dictionary APIs ────────────────────────────────────────────────────

@app.get("/api/dict/search")
def dict_search(
    q: str = Query(..., min_length=1, max_length=40),
    limit: int = Query(20, ge=1, le=50),
):
    clean_q = _clean_query_text(q)
    if not clean_q:
        raise HTTPException(400, "Invalid query")
    query_kind = "single_char" if _is_single_hanzi_query(clean_q) else "term"

    entries = _load_headword_page_candidates(clean_q, limit)
    source_mode = "headword_page_index" if entries else None
    if not entries:
        entries = _build_dict_db_entries(clean_q, limit)
        source_mode = "dict_db" if entries else None

    return {
        "query": q,
        "query_kind": query_kind,
        "available": bool(entries),
        "enabled_sources": list(DICT_ENABLED_SOURCES),
        "display_mode": "page_images",
        "student_safe_only": True,
        "source_mode": source_mode or "unavailable",
        "entries": entries,
    }


@app.get("/api/dict/moe-revised")
def dict_moe_revised(
    q: str = Query(..., min_length=1, max_length=40),
    limit: int = Query(6, ge=1, le=20),
):
    clean_q = _clean_query_text(q)
    if not clean_q:
        raise HTTPException(400, "Invalid query")
    payload = _search_moe_revised_entries(clean_q, limit)
    payload.update(
        {
            "query": q,
            "query_kind": "single_char" if _is_single_hanzi_query(clean_q) else "term",
            "display_mode": "read_only_cards",
            "integration_mode": "local_read_only",
        }
    )
    return payload


@app.get("/api/dict/moe-idioms")
def dict_moe_idioms(
    q: str = Query(..., min_length=1, max_length=40),
    limit: int = Query(6, ge=1, le=20),
):
    clean_q = _clean_query_text(q)
    if not clean_q:
        raise HTTPException(400, "Invalid query")
    payload = _search_moe_idioms_entries(clean_q, limit)
    payload.update(
        {
            "query": q,
            "query_kind": "single_char" if _is_single_hanzi_query(clean_q) else "term",
            "display_mode": "read_only_cards",
            "integration_mode": "local_read_only",
        }
    )
    return payload


@app.get("/api/dict/references")
def dict_references(
    q: str = Query(..., min_length=1, max_length=40),
):
    clean_q = _clean_query_text(q)
    if not clean_q:
        raise HTTPException(400, "Invalid query")
    payload = _build_external_reference_payload(clean_q)
    payload["student_safe_mode"] = "external_cards"
    return payload


@app.get("/api/dict/status")
def dict_status():
    return _build_dict_status_payload()


@app.get("/api/dict/exam/xuci")
def dict_exam_xuci():
    return _load_dict_exam_dataset("xuci")


@app.get("/api/dict/exam/shici")
def dict_exam_shici():
    return _load_dict_exam_dataset("shici")


@app.get("/api/dict/exam/xuci-detail")
def dict_exam_xuci_detail(
    headword: str = Query(..., min_length=1, max_length=8),
):
    return _load_dict_exam_xuci_detail(headword)


@app.get("/api/dict/exam/questions")
def dict_exam_questions(
    kind: str = Query(..., pattern="^(xuci|shici)$"),
    headword: str = Query(..., min_length=1, max_length=8),
):
    return _load_dict_exam_questions(kind, headword)


@app.get("/api/dict/textbook")
def dict_textbook(
    q: str = Query(..., min_length=1, max_length=40),
    limit: int = Query(30, ge=1, le=80),
    phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all"),
):
    clean_q = _clean_query_text(q)
    if not clean_q:
        raise HTTPException(400, "Invalid query")

    con = get_db()
    try:
        dict_phase_filter = " AND phase = ?" if phase else ""
        dict_phase_params = [phase] if phase else []
        rows = con.execute(
            f"""
            SELECT id, subject, title, book_key, section, logical_page, text
            FROM chunks
            WHERE source = 'mineru'
              AND subject = '语文'
              AND text LIKE ?{dict_phase_filter}
            ORDER BY
                CASE
                    WHEN INSTR(text, ?) > 0 THEN INSTR(text, ?)
                    ELSE 999999
                END,
                COALESCE(logical_page, section),
                id
            LIMIT ?
            """,
            [f"%{clean_q}%"] + dict_phase_params + [clean_q, clean_q, max(limit * 6, 120)],
        ).fetchall()

        matches = []
        seen = set()
        normalized_query = re.sub(r"\s+", "", clean_q)
        for row in rows:
            raw_text = _normalize_text_line(row["text"])
            if not raw_text:
                continue
            physical_page = row["section"]
            logical_page = row["logical_page"] if row["logical_page"] is not None else physical_page
            manifest_hits = _find_textbook_classics_hits(row["book_key"], logical_page)

            if manifest_hits:
                for manifest_hit in manifest_hits:
                    clipped_text = _clip_textbook_classics_text(raw_text, manifest_hit)
                    if not clipped_text:
                        continue
                    if normalized_query not in re.sub(r"\s+", "", clipped_text):
                        continue
                    heading = str(manifest_hit.get("title") or "").strip() or _extract_passage_heading(clipped_text)
                    display_title = heading or row["title"]
                    dedupe_key = (row["id"], display_title, physical_page)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    score = max(8, _classical_marker_score(clipped_text))
                    if _looks_like_poem(clipped_text):
                        score += 6
                    if heading:
                        score += 2
                    matches.append(
                        _apply_book_runtime_meta(
                            {
                                "id": row["id"],
                                "book_key": row["book_key"],
                                "title": row["title"],
                                "display_title": display_title,
                                "classical_title": heading or None,
                                "section": physical_page,
                                "logical_page": logical_page,
                                "text": clipped_text[:DICT_TEXTBOOK_RESPONSE_TEXT_LIMIT],
                                "snippet": _build_context_snippet(clipped_text, clean_q),
                                "page_url": _book_page_url(row["book_key"], physical_page),
                                "classical_kind": manifest_hit.get("kind"),
                                "score": score,
                            },
                            book_key=row["book_key"],
                            fallback_title=row["title"],
                        )
                    )
                continue

            if _book_has_textbook_classics_manifest(row["book_key"]):
                continue

            clipped_text = _clip_textbook_classics_text(raw_text)
            if not clipped_text:
                continue
            score = _score_textbook_classical_row(row)
            if score < 5:
                continue
            if normalized_query not in re.sub(r"\s+", "", clipped_text):
                continue

            heading = _extract_passage_heading(clipped_text)
            display_title = heading or row["title"]
            dedupe_key = (row["id"], display_title, physical_page)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            matches.append(
                _apply_book_runtime_meta(
                    {
                        "id": row["id"],
                        "book_key": row["book_key"],
                        "title": row["title"],
                        "display_title": display_title,
                        "classical_title": heading or None,
                        "section": physical_page,
                        "logical_page": logical_page,
                        "text": clipped_text[:DICT_TEXTBOOK_RESPONSE_TEXT_LIMIT],
                        "snippet": _build_context_snippet(clipped_text, clean_q),
                        "page_url": _book_page_url(row["book_key"], physical_page),
                        "classical_kind": None,
                        "score": score,
                    },
                    book_key=row["book_key"],
                    fallback_title=row["title"],
                )
            )

        matches.sort(
            key=lambda item: (
                -item["score"],
                -item["text"].count(clean_q),
                item["text"].find(clean_q) if clean_q in item["text"] else 999999,
                item["logical_page"],
                item["id"],
            )
        )
        return {
            "query": q,
            "results": [
                {
                    k: v
                    for k, v in item.items()
                    if k not in {"score"}
                }
                for item in matches[:limit]
            ],
        }
    finally:
        con.close()


@app.get("/api/dict/gaokao")
def dict_gaokao(
    q: str = Query(..., min_length=1, max_length=40),
    limit: int = Query(20, ge=1, le=60),
):
    clean_q = _clean_query_text(q)
    if not clean_q:
        raise HTTPException(400, "Invalid query")

    con = get_db()
    try:
        rows = con.execute(
            """
            SELECT id, title, year, category, text, answer
            FROM chunks
            WHERE source = 'gaokao'
              AND subject = '语文'
              AND text LIKE ?
            ORDER BY year DESC, id DESC
            LIMIT ?
            """,
            (f"%{clean_q}%", max(limit * 8, 120)),
        ).fetchall()

        matches = []
        for row in rows:
            score = _score_gaokao_classical_row(row)
            if score < 6:
                continue
            matches.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "year": row["year"],
                    "category": row["category"],
                    "text": (row["text"] or "")[:DICT_GAOKAO_RESPONSE_TEXT_LIMIT],
                    "answer": row["answer"],
                    "snippet": _build_context_snippet(row["text"], clean_q, window=220),
                    "score": score,
                }
            )

        matches.sort(
            key=lambda item: (
                -item["score"],
                -(item["text"] or "").count(clean_q),
                -(item["year"] or 0),
                item["id"],
            )
        )
        return {
            "query": q,
            "results": [
                {
                    k: v
                    for k, v in item.items()
                    if k not in {"score"}
                }
                for item in matches[:limit]
            ],
        }
    finally:
        con.close()


@app.get("/api/dict/page-images")
def dict_page_images(
    dict_source: str = Query(..., description="changyong, xuci, or ciyuan"),
    page: int = Query(..., ge=1, description="1-based dictionary book page"),
    context: int = Query(2, ge=0, le=8),
):
    meta = DICT_SOURCE_META.get(dict_source)
    if not meta:
        raise HTTPException(404, "Dictionary source not found")
    if dict_source not in DICT_ENABLED_SOURCE_SET:
        raise HTTPException(404, "Dictionary source not enabled")

    total_pages = _dict_book_page_limit(dict_source) or int(meta["page_count"])
    current_page = max(1, min(int(page), total_pages))
    start = max(1, current_page - context)
    end = min(total_pages, current_page + context)
    pages = []
    for page_num in range(start, end + 1):
        url = _dict_page_url(dict_source, page_num)
        pdf_page = _dict_pdf_page(dict_source, page_num)
        if not url or pdf_page is None:
            continue
        pages.append(
            {
                "page": page_num,
                "pdf_page": pdf_page,
                "url": url,
                "current": page_num == current_page,
            }
        )
    return {
        "dict_source": dict_source,
        "dict_label": meta["label"],
        "current_page": current_page,
        "current_pdf_page": _dict_pdf_page(dict_source, current_page),
        "total_pages": total_pages,
        "pages": pages,
    }


@app.post("/api/dict/chat")
async def dict_chat(payload: dict = Body(...)):
    headword = str(payload.get("headword", "")).strip()
    user_message = str(payload.get("user_message", "")).strip()
    if not headword or not user_message:
        raise HTTPException(400, "headword and user_message are required")

    history = payload.get("history") or []
    phase = str(payload.get("phase", "")).strip() or None
    context_payload = await run_in_threadpool(_build_dict_chat_context_for_request, headword, phase=phase)
    prompt = _build_dict_chat_prompt(
        headword,
        user_message,
        dict_context=context_payload.get("dict_context", ""),
        textbook_context=context_payload.get("textbook_context", ""),
        gaokao_context=context_payload.get("gaokao_context", ""),
        history=history,
        phase=phase,
    )
    try:
        ai_data = await _call_ai_service(prompt)
        await run_in_threadpool(log_ai_chat, headword, user_message, context_payload, success=True, phase=phase)
    except HTTPException as e:
        await run_in_threadpool(log_ai_chat, headword, user_message, context_payload, success=False, error=str(e.detail), phase=phase)
        raise
    return {
        "answer": ai_data.get("answer"),
        "provider": AI_SERVICE_LABEL,
    }


# ── Gaokao APIs ───────────────────────────────────────────────────────

@app.get("/api/gaokao")
def gaokao(
    subject: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    category: Optional[str] = Query(None),
    question_type: Optional[str] = Query(None, description="objective or subjective"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Browse gaokao exam questions with filtering."""
    con = get_db()
    try:
        where = ["source='gaokao'"]
        params = []
        if subject:
            where.append("subject = ?")
            params.append(subject)
        if year:
            where.append("year = ?")
            params.append(year)
        if category:
            where.append("category = ?")
            params.append(category)
        if question_type:
            where.append("question_type = ?")
            params.append(question_type)

        where_clause = " AND ".join(where)
        params.extend([limit, offset])

        rows = con.execute(f"""
            SELECT id, content_id, subject, year, category, region,
                   question_type, score, title, text, answer
            FROM chunks
            WHERE {where_clause}
            ORDER BY year DESC, subject, section
            LIMIT ? OFFSET ?
        """, params).fetchall()

        # Get total count for pagination
        count_params = params[:-2]  # remove limit/offset
        total = con.execute(f"""
            SELECT COUNT(*) FROM chunks WHERE {where_clause}
        """, count_params).fetchone()[0]

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "questions": [
                {
                    "id": r["id"],
                    "content_id": r["content_id"],
                    "subject": r["subject"],
                    "year": r["year"],
                    "category": r["category"],
                    "region": r["region"],
                    "question_type": r["question_type"],
                    "score": r["score"],
                    "title": r["title"],
                    "text": r["text"],
                    "answer": r["answer"],
                    **SUBJECT_META.get(r["subject"], {"icon": "📚", "color": "#95a5a6"}),
                }
                for r in rows
            ],
        }
    finally:
        con.close()


@app.get("/api/gaokao/years")
def gaokao_years():
    """List available years, categories, and subjects for filtering."""
    con = get_db()
    try:
        years = con.execute("""
            SELECT DISTINCT year FROM chunks
            WHERE source='gaokao' AND year IS NOT NULL
            ORDER BY year DESC
        """).fetchall()

        categories = con.execute("""
            SELECT DISTINCT category FROM chunks
            WHERE source='gaokao' AND category IS NOT NULL AND category != ''
            ORDER BY category
        """).fetchall()

        subjects = con.execute("""
            SELECT subject, COUNT(*) as cnt FROM chunks
            WHERE source='gaokao'
            GROUP BY subject ORDER BY cnt DESC
        """).fetchall()

        return {
            "years": [r["year"] for r in years],
            "categories": [r["category"] for r in categories],
            "subjects": [
                {"name": r["subject"], "count": r["cnt"],
                 **SUBJECT_META.get(r["subject"], {"icon": "📚", "color": "#95a5a6"})}
                for r in subjects
            ],
        }
    finally:
        con.close()


# ── Semantic Matching Helpers ─────────────────────────────────────────

_STOP_WORDS = {
    '选择', '问题', '下列', '以下', '关于', '其中', '正确', '错误',
    '不正确', '说法', '叙述', '表述', '选项', '答案', '分析', '解答',
    '已知', '求解', '设有', '如图', '所示', '可以', '可能', '区域',
    '不能', '属于', '不属于', '一定', '不一定', '详解',
    '根据', '由此', '可知', '因此', '所以', '由于', '如果', '那么',
    '题目', '材料', '文中', '图中', '表中', '实验', '方案', '含量',
    '条件', '下面', '上面', '哪个', '哪些', '什么', '为什么',
    '判断', '推断', '分别', '同时', '以及', '或者', '而且',
    '进行', '使用', '利用', '通过', '发生', '产生', '得到', '变化',
    '增大', '减小', '增加', '减少', '提高', '降低', '保持', '影响',
    '表示', '反映', '说明', '体现', '指出', '认为', '表明',
    '主要', '一般', '通常', '特别', '特殊', '基本', '重要',
    '合理', '适当', '必要', '需要', '应该', '能够',
    '过程', '结果', '作用', '功能', '特点', '特征', '方法',
    '大小', '多少', '高低', '长短', '快慢', '强弱',
    '的', '了', '是', '在', '和', '与', '为', '中', '不',
    '这', '那', '其', '也', '都', '就', '还', '又',
}


def _extract_weighted_terms(text: str, con) -> list[tuple[str, float]]:
    """Extract terms from text, weighted by IDF if available.
    Uses jieba segmentation when available, falls back to regex."""
    try:
        import jieba
        raw_words = list(jieba.cut(text))
        # Filter: Chinese words 2-6 chars, or known English acronyms
        raw = [w for w in raw_words 
               if (2 <= len(w) <= 6 and any('\u4e00' <= c <= '\u9fff' for c in w))
               or w.upper() in ('DNA', 'RNA', 'ATP', 'ADP', 'PCR')]
    except ImportError:
        raw = re.findall(r'[\u4e00-\u9fff]{2,6}', text)
    
    counts = Counter(raw)
    filtered = [(t, c) for t, c in counts.items()
                if t not in _STOP_WORDS and len(t) >= 2]

    # Try IDF weighting
    has_idf = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='concept_idf'"
    ).fetchone()
    if has_idf and filtered:
        terms_list = [t for t, _ in filtered]
        placeholders = ','.join('?' * len(terms_list))
        idf_rows = con.execute(
            f"SELECT term, idf FROM concept_idf WHERE term IN ({placeholders})",
            terms_list
        ).fetchall()
        idf_map = {r["term"]: r["idf"] for r in idf_rows}
        max_idf = max(idf_map.values()) if idf_map else 1.0
        # Score = tf * idf_normalized
        scored = []
        for t, c in filtered:
            idf_val = idf_map.get(t, max_idf * 0.5)  # unknown terms get medium weight
            scored.append((t, c * idf_val / max_idf))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored
    else:
        filtered.sort(key=lambda x: x[1], reverse=True)
        return [(t, float(c)) for t, c in filtered]


def _match_concepts(text: str, subject: str, con) -> list[dict]:
    """Match text against concept_map using cached exact-token phrases with long-term fallback."""
    db_token = _db_cache_token()
    catalog = _get_concept_catalog(db_token)
    if not catalog:
        return []

    _ensure_jieba_concepts_loaded(catalog, db_token)
    normalized_text = _normalize_match_text(text)
    phrase_set = _build_token_phrases(_segment_text_tokens(normalized_text))

    matched = []
    for concept, subjects, has_chinese in catalog:
        if has_chinese and any(len(item["concept"]) > len(concept) and concept in item["concept"] for item in matched):
            continue
        if not _concept_matches_text(concept, normalized_text, phrase_set):
            continue
        matched.append({
            "concept": concept,
            "subjects": dict(subjects),
            "is_cross": len(subjects) >= 2,
            "is_same_subject": subject in subjects,
        })
    return matched


def _expand_cross_subject(concepts: list[dict], con) -> list[str]:
    """Use cross_subject_map to find related concepts in other subjects."""
    has_csm = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='cross_subject_map'"
    ).fetchone()
    if not has_csm:
        return []

    expanded_terms = []
    for c in concepts:
        # Find clusters containing this concept
        clusters = con.execute(
            "SELECT DISTINCT cluster_name FROM cross_subject_map WHERE concept = ?",
            [c["concept"]]
        ).fetchall()
        for cl in clusters:
            # Get all concepts in this cluster (from other subjects)
            related = con.execute(
                "SELECT concept, subject FROM cross_subject_map WHERE cluster_name = ?",
                [cl["cluster_name"]]
            ).fetchall()
            for r in related:
                if r["concept"] != c["concept"]:
                    expanded_terms.append(r["concept"])
    return list(set(expanded_terms))


def _score_result(result_text: str, query_terms: list[str],
                  matched_concepts: list[str], is_same_subject: bool,
                  *, normalized_text: str | None = None, phrase_set: set[str] | None = None) -> int:
    """Compute relevance score 0-100 with IDF-weighted term importance."""
    score = 0
    if normalized_text is None or phrase_set is None:
        normalized_text, phrase_set = _build_text_match_context(result_text)
    
    # Term overlap — weight longer/rarer terms higher (max 35 points)
    term_hits = 0
    for t in query_terms[:15]:
        if _concept_matches_text(t, normalized_text, phrase_set):
            # Longer terms are more specific and worth more
            weight = min(3, len(t) - 1)  # 2-char=1, 3-char=2, 4+=3
            term_hits += weight
    score += min(35, term_hits)
    
    # Concept matches — high-value signal (max 40 points)
    concept_hits = 0
    for c in matched_concepts:
        if _concept_matches_text(c, normalized_text, phrase_set):
            # Longer concept names = more specific = higher score
            concept_hits += min(15, len(c) * 2)
    score += min(40, concept_hits)
    
    # Same subject bonus (15 points) — reduced to avoid over-rewarding
    if is_same_subject:
        score += 15
    
    # Penalize very short result texts (likely table-of-contents or headers)
    if len(result_text) < 50:
        score = max(0, score - 20)
    
    return min(100, score)


@app.get("/api/gaokao/link")
def gaokao_link(
    question_id: int = Query(..., description="ID of the gaokao question"),
    limit: int = Query(10, ge=1, le=30),
):
    """3-layer semantic matching: concept graph → IDF-weighted FTS → cross-subject expansion."""
    con = get_db()
    try:
        q_row = con.execute(
            "SELECT * FROM chunks WHERE id = ? AND source = 'gaokao'",
            [question_id]
        ).fetchone()
        if not q_row:
            raise HTTPException(404, "Question not found")

        text = q_row["text"] or ""
        q_subject = q_row["subject"]
        ai_gaokao = _load_ai_gaokao_record(con, question_id)
        precomputed_terms = ai_gaokao.get("knowledge_points") or []
        precomputed_refs = ai_gaokao.get("textbook_refs") or []
        precomputed_summary = ai_gaokao.get("summary") or ""
        precomputed_links = _resolve_textbook_refs(
            con,
            precomputed_refs,
            question_subject=q_subject,
            limit=limit,
        )
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', text))

        if not has_chinese and not precomputed_terms and not precomputed_links:
            return {
                "question_id": question_id,
                "question_title": q_row["title"],
                "question_subject": q_subject,
                "question_year": q_row["year"] if "year" in q_row.keys() else None,
                "question_category": q_row["category"] if "category" in q_row.keys() else None,
                "question_type": q_row["question_type"] if "question_type" in q_row.keys() else None,
                "search_terms": [],
                "matched_concepts": [],
                "expanded_terms": [],
                "precomputed_analysis": None,
                "links": [],
                "cross_links": [],
            }

        # ── Layer 1: Concept graph matching ────────────────────────────
        matched_concepts = _match_concepts(text, q_subject, con) if has_chinese else []
        concept_names = [c["concept"] for c in matched_concepts]

        # ── Layer 2: IDF-weighted term extraction ─────────────────────
        weighted_terms = _extract_weighted_terms(text, con) if has_chinese else []
        top_terms = [t for t, _ in weighted_terms[:15]]

        if not top_terms and not concept_names and not precomputed_terms and not precomputed_links:
            return {
                "question_id": question_id,
                "question_title": q_row["title"],
                "question_subject": q_subject,
                "question_year": q_row["year"] if "year" in q_row.keys() else None,
                "question_category": q_row["category"] if "category" in q_row.keys() else None,
                "question_type": q_row["question_type"] if "question_type" in q_row.keys() else None,
                "search_terms": [],
                "matched_concepts": [],
                "expanded_terms": [],
                "precomputed_analysis": (
                    {
                        "summary": precomputed_summary,
                        "knowledge_points": precomputed_terms[:6],
                        "textbook_refs": precomputed_refs[:6],
                        "resolved_ref_count": len(precomputed_links),
                    }
                    if precomputed_summary or precomputed_terms or precomputed_refs
                    else None
                ),
                "links": precomputed_links[:limit],
                "cross_links": [],
            }

        # ── Layer 3: Cross-subject expansion ──────────────────────────
        expanded_terms = _expand_cross_subject(matched_concepts, con) if matched_concepts else []

        # ── Combined FTS search ───────────────────────────────────────
        # Primary: top IDF terms + matched concepts
        search_terms = list(dict.fromkeys(precomputed_terms[:6] + top_terms[:8] + concept_names[:5]))
        if not search_terms:
            search_terms = precomputed_terms[:6] or top_terms[:10]

        search_q = ' OR '.join(search_terms[:12]) if search_terms else None

        # Secondary: expanded cross-subject terms
        expanded_q = ' OR '.join(expanded_terms[:8]) if expanded_terms else None

        seen_ids = {item["id"] for item in precomputed_links}
        all_results = []

        # Primary search
        if search_q:
            try:
                rows = con.execute("""
                    SELECT c.id, c.content_id, c.subject, c.title, c.book_key, c.section, c.logical_page,
                           snippet(chunks_fts, 0, '<mark>', '</mark>', '…', 40) as snippet,
                           c.text, s.summary AS ai_summary
                    FROM chunks c
                    JOIN chunks_fts f ON c.id = f.rowid
                    LEFT JOIN ai_summaries s ON s.chunk_id = c.id
                    WHERE chunks_fts MATCH ? AND c.source != 'gaokao'
                    ORDER BY rank
                    LIMIT ?
                """, [search_q, limit * 3]).fetchall()
                for r in rows:
                    if r["id"] not in seen_ids:
                        seen_ids.add(r["id"])
                        all_results.append((r, "explicit"))
            except Exception:
                pass

        # ── Dense Vector Retrieval (FAISS) ────────────────────────────
        if has_chinese and faiss_index and embedder:
            try:
                # Encode the query text with the runtime embedding model.
                query_vec = embedder.encode([text[:512]], normalize_embeddings=True).astype('float32')
                D, I = faiss_index.search(query_vec, limit * 2)
                
                faiss_ids = []
                for score, match_id in zip(D[0], I[0]):
                    if match_id != -1 and match_id not in seen_ids and score > FAISS_SCORE_THRESHOLD:
                        faiss_ids.append(int(match_id))
                
                if faiss_ids:
                    placeholders = ','.join('?' * len(faiss_ids))
                    faiss_rows = con.execute(f"""
                        SELECT c.id, c.content_id, c.subject, c.title, c.book_key, c.section, c.logical_page,
                               substr(c.text, 1, 100) as snippet,
                               c.text, s.summary AS ai_summary
                        FROM chunks c
                        LEFT JOIN ai_summaries s ON s.chunk_id = c.id
                        WHERE c.id IN ({placeholders})
                    """, faiss_ids).fetchall()
                    
                    for r in faiss_rows:
                        if r["id"] not in seen_ids:
                            seen_ids.add(r["id"])
                            all_results.append((r, "implicit"))  # Semantic matches represent deep implicit connections
            except Exception as e:
                print(f"FAISS search error: {e}")

        # Score, classify, and filter results
        same_subject = []
        cross_subject = []
        for item in precomputed_links:
            item["matched_concepts"] = _present_terms_in_text(
                precomputed_terms[:5],
                f"{item.get('text') or ''} {item.get('summary') or ''}",
            ) or precomputed_terms[:3]
            if item["subject"] == q_subject:
                same_subject.append(item)
            else:
                cross_subject.append(item)

        scoring_terms = list(dict.fromkeys(precomputed_terms[:6] + top_terms[:10]))
        scoring_concepts = list(dict.fromkeys(precomputed_terms[:6] + concept_names[:8]))
        for r, link_type in all_results:
            r_text = r["text"] or ""
            normalized_text, phrase_set = _build_text_match_context(r_text)
            # Find which concepts matched in this result
            r_matched = _present_terms_in_text(
                scoring_concepts,
                r_text,
                normalized_text=normalized_text,
                phrase_set=phrase_set,
            )
            score = _score_result(
                r_text,
                scoring_terms,
                scoring_concepts,
                r["subject"] == q_subject,
                normalized_text=normalized_text,
                phrase_set=phrase_set,
            )
            # Skip results below minimum quality threshold
            if score < 15:
                continue
            ai_summary = r["ai_summary"] if "ai_summary" in r.keys() else ""
            snippet = _compose_chunk_snippet(ai_summary, r["text"], limit=180)
            item = {
                "id": r["id"],
                "subject": r["subject"],
                "title": r["title"],
                "book_key": r["book_key"],
                "section": r["section"],
                "logical_page": r["logical_page"] if "logical_page" in r.keys() else r["section"],
                "snippet": snippet or r["snippet"],
                "summary": _normalize_text_line(ai_summary),
                "text": r_text[:1500],
                "link_type": link_type,
                "relevance_score": score,
                "matched_concepts": r_matched[:5],
                **SUBJECT_META.get(r["subject"], {"icon": "📚", "color": "#95a5a6"}),
            }
            item = _apply_book_runtime_meta(
                item,
                book_key=r["book_key"],
                fallback_title=r["title"],
                content_id=r["content_id"] if "content_id" in r.keys() else None,
            )
            if r["subject"] == q_subject:
                same_subject.append(item)
            else:
                cross_subject.append(item)

        # Sort by relevance score
        same_subject.sort(key=lambda x: x["relevance_score"], reverse=True)
        cross_subject.sort(key=lambda x: x["relevance_score"], reverse=True)

        matched_output = []
        seen_concepts = set()
        for c in matched_concepts[:10]:
            key = c["concept"].casefold()
            if key in seen_concepts:
                continue
            seen_concepts.add(key)
            matched_output.append(
                {
                    "concept": c["concept"],
                    "is_cross": c["is_cross"],
                    "subjects": list(c["subjects"].keys()),
                    "source": "graph",
                }
            )
        for term in precomputed_terms[:8]:
            key = term.casefold()
            if key in seen_concepts:
                continue
            seen_concepts.add(key)
            matched_output.append(
                {
                    "concept": term,
                    "is_cross": False,
                    "subjects": [q_subject] if q_subject else [],
                    "source": "precomputed",
                }
            )

        return {
            "question_id": question_id,
            "question_title": q_row["title"],
            "question_subject": q_subject,
            "question_year": q_row["year"] if "year" in q_row.keys() else None,
            "question_category": q_row["category"] if "category" in q_row.keys() else None,
            "question_type": q_row["question_type"] if "question_type" in q_row.keys() else None,
            "search_terms": search_terms[:10],
            "matched_concepts": matched_output[:10],
            "expanded_terms": expanded_terms[:8],
            "precomputed_analysis": (
                {
                    "summary": precomputed_summary,
                    "knowledge_points": precomputed_terms[:6],
                    "textbook_refs": precomputed_refs[:6],
                    "resolved_ref_count": len(precomputed_links),
                }
                if precomputed_summary or precomputed_terms or precomputed_refs
                else None
            ),
            "links": same_subject[:limit],
            "cross_links": cross_subject[:limit],
        }
    finally:
        con.close()


@app.get("/api/textbook/links")
def textbook_links(
    chunk_id: int = Query(..., description="ID of the textbook chunk"),
    limit: int = Query(10, ge=1, le=30),
):
    """Discover cross-subject links for a textbook chunk."""
    con = get_db()
    try:
        ch = con.execute(
            "SELECT * FROM chunks WHERE id = ? AND source != 'gaokao'",
            [chunk_id]
        ).fetchone()
        if not ch:
            raise HTTPException(404, "Chunk not found")

        text = ch["text"] or ""
        subject = ch["subject"]

        matched_concepts = _match_concepts(text, subject, con)
        weighted_terms = _extract_weighted_terms(text, con)
        top_terms = [t for t, _ in weighted_terms[:10]]
        expanded = _expand_cross_subject(matched_concepts, con)

        search_terms = list(dict.fromkeys(top_terms[:6] + [c["concept"] for c in matched_concepts[:4]]))
        if not search_terms:
            return {"chunk_id": chunk_id, "links": [], "matched_concepts": []}

        search_q = ' OR '.join(search_terms[:10])
        try:
            rows = con.execute("""
                SELECT c.id, c.subject, c.title, c.book_key, c.section,
                       snippet(chunks_fts, 0, '<mark>', '</mark>', '…', 40) as snippet,
                       c.text
                FROM chunks c
                JOIN chunks_fts f ON c.id = f.rowid
                WHERE chunks_fts MATCH ? AND c.source != 'gaokao' AND c.id != ?
                      AND c.subject != ?
                ORDER BY rank
                LIMIT ?
            """, [search_q, chunk_id, subject, limit * 2]).fetchall()
        except Exception:
            return {"chunk_id": chunk_id, "links": [], "matched_concepts": []}

        concept_names = [c["concept"] for c in matched_concepts]
        results = []
        for r in rows:
            r_text = r["text"] or ""
            normalized_text, phrase_set = _build_text_match_context(r_text)
            r_matched = _present_terms_in_text(
                concept_names,
                r_text,
                normalized_text=normalized_text,
                phrase_set=phrase_set,
            )
            score = _score_result(
                r_text,
                top_terms,
                concept_names,
                False,
                normalized_text=normalized_text,
                phrase_set=phrase_set,
            )
            results.append({
                "id": r["id"],
                "subject": r["subject"],
                "title": r["title"],
                "book_key": r["book_key"],
                "section": r["section"],
                "snippet": r["snippet"],
                "relevance_score": score,
                "matched_concepts": r_matched[:5],
                "link_type": "implicit" if any(c in expanded for c in r_matched) else "explicit",
                **SUBJECT_META.get(r["subject"], {"icon": "📚", "color": "#95a5a6"}),
            })

        # ── Dense Vector Retrieval (FAISS) ────────────────────────────
        seen_ids = {r["id"] for r in rows}
        seen_ids.add(chunk_id)
        
        if faiss_index and embedder:
            try:
                query_vec = embedder.encode([text[:512]], normalize_embeddings=True).astype('float32')
                D, I = faiss_index.search(query_vec, limit * 2)
                
                faiss_ids = []
                for score, match_id in zip(D[0], I[0]):
                    if match_id != -1 and match_id not in seen_ids and score > FAISS_SCORE_THRESHOLD:
                        faiss_ids.append(int(match_id))
                
                if faiss_ids:
                    placeholders = ','.join('?' * len(faiss_ids))
                    faiss_rows = con.execute(f"""
                        SELECT c.id, c.subject, c.title, c.book_key, c.section,
                               substr(c.text, 1, 100) as snippet,
                               c.text
                        FROM chunks c
                        WHERE c.id IN ({placeholders}) AND c.subject != ?
                    """, [*faiss_ids, subject]).fetchall()
                    
                    for r in faiss_rows:
                        if r["id"] not in seen_ids:
                            seen_ids.add(r["id"])
                            results.append({
                                "id": r["id"],
                                "subject": r["subject"],
                                "title": r["title"],
                                "book_key": r["book_key"],
                                "section": r["section"],
                                "snippet": r["snippet"] + "...",
                                "relevance_score": 60,  # Semantic matches get a solid base score
                                "matched_concepts": [],
                                "link_type": "implicit",
                                **SUBJECT_META.get(r["subject"], {"icon": "📚", "color": "#95a5a6"}),
                            })
            except Exception as e:
                print(f"FAISS search error in textbook_links: {e}")

        results.sort(key=lambda x: x["relevance_score"], reverse=True)
        return {
            "chunk_id": chunk_id,
            "source_subject": subject,
            "matched_concepts": [
                {"concept": c["concept"], "is_cross": c["is_cross"],
                 "subjects": list(c["subjects"].keys())}
                for c in matched_concepts[:10]
            ],
            "links": results[:limit],
        }
    finally:
        con.close()


# ── Analytics APIs ────────────────────────────────────────────────────

@app.get("/api/analytics/word-freq")
def word_freq(
    source: str = Query("all", description="textbook, gaokao, or all"),
    subject: Optional[str] = Query(None),
    phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all"),
    limit: int = Query(50, ge=1, le=200),
):
    """Word frequency for curated academic terms (pre-computed)."""
    con = get_db()
    try:
        if subject == "英语":
            stats = _build_english_term_stats(_db_cache_token(), source, phase=phase)
            return {
                "frequencies": [
                    {"term": item["term"], "count": item["count"]}
                    for item in stats["terms"][:limit]
                ],
                "source": source,
                "subject": subject,
            }
        # Use pre-computed keyword_counts table for fast lookups
        has_kc = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='keyword_counts'").fetchone()
        if has_kc:
            where_parts = []
            params = []
            if source == "gaokao":
                where_parts.append("source = 'gaokao'")
            elif source == "textbook":
                where_parts.append("source = 'textbook'")
            if subject:
                where_parts.append("subject = ?")
                params.append(subject)
            has_phase_col = any(
                r[1] == "phase"
                for r in con.execute("PRAGMA table_info(keyword_counts)").fetchall()
            )
            if has_phase_col and phase:
                where_parts.append("phase = ?")
                params.append(phase)
            where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
            rows = con.execute(f"""
                SELECT term, SUM(count) as cnt FROM keyword_counts
                {where_clause}
                GROUP BY term ORDER BY cnt DESC LIMIT ?
            """, params + [limit]).fetchall()
            return {
                "frequencies": [{"term": r["term"], "count": r["cnt"]} for r in rows],
                "source": source, "subject": subject,
            }
        # Fallback: use curated_keywords total_count
        rows = con.execute("""
            SELECT term, total_count as cnt FROM curated_keywords
            ORDER BY total_count DESC LIMIT ?
        """, (limit,)).fetchall()
        return {
            "frequencies": [{"term": r["term"], "count": r["cnt"]} for r in rows],
            "source": source, "subject": subject,
        }
    finally:
        con.close()


@app.get("/api/analytics/heatmap")
def heatmap(phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all")):
    """Cross-subject concept sharing matrix."""
    con = get_db()
    try:
        hm_phase_filter = " WHERE phase = ?" if phase else ""
        hm_phase_params = [phase] if phase else []
        # Get all curated concepts and their subject associations
        curated_sql = "SELECT term FROM curated_keywords" + (" WHERE phase = ?" if phase else "")
        curated = {r["term"] for r in con.execute(curated_sql, hm_phase_params).fetchall()}

        rows = con.execute(f"""
            SELECT concept, subject, SUM(count) as cnt
            FROM concept_map{hm_phase_filter}
            GROUP BY concept, subject
        """, hm_phase_params).fetchall()

        # Build concept -> set of subjects
        concept_subjects = {}
        for r in rows:
            c = r["concept"]
            if c not in curated:
                continue
            if c not in concept_subjects:
                concept_subjects[c] = set()
            concept_subjects[c].add(r["subject"])

        # Count shared concepts between each pair of subjects
        subjects = sorted({r["subject"] for r in rows})
        matrix = {s1: {s2: 0 for s2 in subjects} for s1 in subjects}

        for concept, subj_set in concept_subjects.items():
            subj_list = list(subj_set)
            for i, s1 in enumerate(subj_list):
                for s2 in subj_list[i+1:]:
                    matrix[s1][s2] += 1
                    matrix[s2][s1] += 1
            # Self = total concepts for that subject
            for s in subj_list:
                matrix[s][s] += 1

        return {
            "subjects": subjects,
            "matrix": [[matrix[s1][s2] for s2 in subjects] for s1 in subjects],
            "total_concepts": len(concept_subjects),
        }
    finally:
        con.close()


@app.get("/api/analytics/coverage")
def coverage(
    limit: int = Query(30, ge=1, le=100),
    phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all"),
):
    """Textbook vs Exam concept coverage analysis (pre-computed)."""
    con = get_db()
    try:
        has_kc = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='keyword_counts'").fetchone()
        if has_kc:
            cov_phase_filter = " WHERE phase = ?" if phase else ""
            cov_phase_params = [phase] if phase else []
            rows = con.execute(f"""
                SELECT term,
                    COALESCE(SUM(CASE WHEN source='textbook' THEN count END), 0) as textbook,
                    COALESCE(SUM(CASE WHEN source='gaokao' THEN count END), 0) as gaokao
                FROM keyword_counts{cov_phase_filter} GROUP BY term
                HAVING textbook > 0 OR gaokao > 0
            """, cov_phase_params).fetchall()
        else:
            return {"hidden_exam_focus": [], "low_exam_focus": []}

        results = []
        for r in rows:
            tb_count = r["textbook"]
            gk_count = r["gaokao"]
            results.append({
                "term": r["term"],
                "textbook": tb_count,
                "gaokao": gk_count,
                "ratio": round(gk_count / max(tb_count, 1) * 100, 1),
            })

        results.sort(key=lambda x: x["ratio"], reverse=True)
        return {
            "hidden_exam_focus": results[:limit],
            "low_exam_focus": sorted(results, key=lambda x: x["ratio"])[:limit],
        }
    finally:
        con.close()


@app.get("/api/analytics/concept-breadth")
def concept_breadth(
    limit: int = Query(50, ge=1, le=200),
    phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all"),
):
    """Rank curated concepts by cross-subject breadth (cached 5min)."""
    cache_key = f'breadth_{limit}_{phase or "all"}'
    if cache_key in _cache:
        return _cache[cache_key]
    con = get_db()
    try:
        cb_phase_filter = " WHERE ck.phase = ?" if phase else ""
        cb_phase_params = [phase, limit] if phase else [limit]
        rows = con.execute(f"""
            SELECT ck.term, ck.subject_count, ck.total_count
            FROM curated_keywords ck{cb_phase_filter}
            ORDER BY ck.subject_count DESC, ck.total_count DESC
            LIMIT ?
        """, cb_phase_params).fetchall()

        result = {
            "concepts": [
                {"term": r["term"], "subjects": r["subject_count"], "count": r["total_count"]}
                for r in rows
            ]
        }
        _cache[cache_key] = result
        return result
    finally:
        con.close()


def _is_high_signal_graph_chunk(text: str) -> bool:
    """Suppress glossary/index-style chunks when mining related graph concepts."""
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) < 40:
        return False

    title_count = compact.count("《")
    latin_token_count = len(re.findall(r"[A-Za-z]{3,}", compact))
    number_count = len(re.findall(r"\b\d+\b", compact))

    if title_count >= 3 and (latin_token_count >= 6 or number_count >= 6):
        return False

    if re.search(r"[。！？；]", compact):
        return True

    return bool(re.search(r"[，、：:;；]", compact)) and len(compact) >= 80 and title_count < 3


GRAPH_GENERIC_TERMS = {
    "描写", "引用", "命题", "修辞", "叙事", "抒情",
    "阅读", "思考", "写作",
}


def _fetch_graph_local_related(con, center_term: str, center_subjects: set[str], limit: int = 15, phase: str | None = None) -> list[dict]:
    """Mine related graph concepts from the center term's own high-signal chunks."""
    gr_phase_filter = " AND c.phase = ?" if phase else ""
    gr_phase_params = [phase] if phase else []
    try:
        chunk_rows = con.execute(f"""
            SELECT c.subject, c.text
            FROM chunks c JOIN chunks_fts ON chunks_fts.rowid = c.id
            WHERE chunks_fts MATCH ? AND c.source = 'mineru'{gr_phase_filter}
            LIMIT 80
        """, [center_term] + gr_phase_params).fetchall()
    except Exception:
        return []

    signal_chunks = [row for row in chunk_rows if _is_high_signal_graph_chunk(row["text"] or "")]
    if not signal_chunks:
        signal_chunks = chunk_rows
    if not signal_chunks:
        return []

    gr_phase_where = " WHERE phase = ?" if phase else ""
    curated_rows = con.execute(f"SELECT term, subject_count, total_count FROM curated_keywords{gr_phase_where}", gr_phase_params).fetchall()
    concept_rows = con.execute(f"SELECT concept, subject FROM concept_map{gr_phase_where}", gr_phase_params).fetchall()

    concept_subjects: dict[str, set[str]] = {}
    for row in concept_rows:
        concept_subjects.setdefault(row["concept"], set()).add(row["subject"])

    candidates = []
    for row in curated_rows:
        term = row["term"]
        if term == center_term:
            continue
        if term in GRAPH_GENERIC_TERMS:
            continue

        term_subjects = concept_subjects.get(term, set())
        overlap = center_subjects & term_subjects
        if len(overlap) < 2:
            continue

        local_hits = 0
        local_subjects = set()
        for chunk in signal_chunks:
            chunk_text = chunk["text"] or ""
            if term in chunk_text:
                local_hits += 1
                local_subjects.add(chunk["subject"])

        if local_hits == 0:
            continue

        subject_count = int(row["subject_count"] or len(term_subjects))
        total_count = int(row["total_count"] or 0)

        if local_hits < 2 and total_count > 20:
            continue

        score = local_hits * 10 + len(local_subjects) * 4 + len(overlap) - subject_count
        candidates.append({
            "term": term,
            "shared_subjects": sorted(overlap),
            "overlap": len(overlap),
            "source": "local_chunks",
            "local_hits": local_hits,
            "local_subjects": sorted(local_subjects),
            "subject_count": subject_count,
            "total_count": total_count,
            "score": score,
        })

    candidates.sort(
        key=lambda item: (
            item["score"],
            item["local_hits"],
            len(item["local_subjects"]),
            item["overlap"],
            -item["subject_count"],
            -item["total_count"],
        ),
        reverse=True,
    )
    return candidates[:limit]


@app.get("/api/graph/search")
def graph_search(
    q: str = Query(..., min_length=1),
    phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all"),
):
    """Return a concept subgraph centered on the search term."""
    con = get_db()
    try:
        q_clean = q.strip()
        phase_filter = " AND c.phase = ?" if phase else ""
        phase_params = (phase,) if phase else ()

        # Use FTS for precise subject distribution (not LIKE)
        try:
            center_dist = con.execute(f"""
                SELECT c.subject, COUNT(*) as cnt
                FROM chunks c JOIN chunks_fts ON chunks_fts.rowid = c.id
                WHERE chunks_fts MATCH ? AND c.source = 'mineru'{phase_filter}
                GROUP BY c.subject ORDER BY cnt DESC
            """, (q_clean,) + phase_params).fetchall()
        except Exception:
            center_dist = []

        if not center_dist:
            return {"center": q_clean, "nodes": [], "links": []}

        center_subjects = {r["subject"] for r in center_dist}

        # ── Priority 1: cross_subject_map cluster siblings ──────────
        cluster_related = []
        try:
            clusters = con.execute(
                f"SELECT DISTINCT cluster_name FROM cross_subject_map WHERE concept = ?{phase_filter}",
                (q_clean,) + phase_params
            ).fetchall()
            for cl in clusters:
                siblings = con.execute(
                    f"SELECT concept, subject FROM cross_subject_map WHERE cluster_name = ? AND concept != ?{phase_filter}",
                    (cl["cluster_name"], q_clean) + phase_params
                ).fetchall()
                for s in siblings:
                    cluster_related.append({
                        "term": s["concept"],
                        "shared_subjects": [s["subject"]],
                        "overlap": 10,  # high priority
                        "source": "cluster",
                        "cluster": cl["cluster_name"],
                    })
        except Exception:
            pass

        # ── Priority 2: local co-mentions in high-signal center chunks ─────
        curated_related = []
        seen_terms = {r["term"] for r in cluster_related}
        for item in _fetch_graph_local_related(con, q_clean, center_subjects, limit=20, phase=phase):
            if item["term"] == q_clean or item["term"] in seen_terms:
                continue
            curated_related.append(item)

        # Merge: clusters first, then curated (max 15 total)
        related = cluster_related + curated_related[:15 - len(cluster_related)]

        # Deduplicate by term
        seen = set()
        deduped = []
        for r in related:
            if r["term"] not in seen:
                seen.add(r["term"])
                deduped.append(r)
        related = deduped[:15]

        # ── Build nodes and links ───────────────────────────────────
        nodes = [{"id": q_clean, "type": "center", "subjects": [r["subject"] for r in center_dist]}]
        links = []

        for r in related:
            node_data = {"id": r["term"], "type": "related", "overlap": r["overlap"]}
            if r.get("cluster"):
                node_data["cluster"] = r["cluster"]
            if r.get("source"):
                node_data["link_source"] = r["source"]
            nodes.append(node_data)
            # Unique link per concept (not per shared subject)
            link_data = {
                "source": q_clean, "target": r["term"],
                "subjects": r["shared_subjects"],
                "strength": r["overlap"],
            }
            if r.get("local_hits"):
                link_data["evidence_hits"] = r["local_hits"]
            ai_rel = get_ai_relation(con, q_clean, r["term"])
            if ai_rel:
                link_data["relation"] = ai_rel["type"]
                link_data["description"] = ai_rel["description"]
            links.append(link_data)

        # Add subject nodes
        for s in sorted(center_subjects):
            nodes.append({"id": s, "type": "subject"})

        return {"center": q_clean, "nodes": nodes, "links": links}
    finally:
        con.close()


@app.get("/api/graph/overview")
def graph_overview(
    mode: str = Query("cross", description="cross=cross-subject, subject=per-subject"),
    subject: Optional[str] = Query(None),
    phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all"),
    limit: int = Query(60, ge=10, le=200),
):
    """Knowledge graph: cross-subject or per-subject concept network."""
    con = get_db()
    try:
        go_phase_filter = " AND phase = ?" if phase else ""
        go_phase_where = " WHERE phase = ?" if phase else ""
        go_phase_params = [phase] if phase else []
        nodes = []
        links = []

        if mode == "subject" and subject:
            # Per-subject mode: show concepts within one subject
            if subject == "英语":
                english_stats = _build_english_term_stats(_db_cache_token(), "textbook")
                top_terms = english_stats["terms"][:limit]
                display_by_canonical = {item["canonical"]: item["term"] for item in top_terms}
                for item in top_terms:
                    nodes.append({"id": item["term"], "type": "concept", "weight": item["count"]})
                allowed = set(display_by_canonical.keys())
                for pair in english_stats["pairs"]:
                    if pair["source"] not in allowed or pair["target"] not in allowed:
                        continue
                    links.append(
                        {
                            "source": display_by_canonical[pair["source"]],
                            "target": display_by_canonical[pair["target"]],
                            "weight": pair["weight"],
                        }
                    )
            else:
                rows = con.execute(f"""
                    SELECT concept, count FROM concept_map
                    WHERE subject = ?{go_phase_filter} ORDER BY count DESC LIMIT ?
                """, [subject] + go_phase_params + [limit]).fetchall()
                concepts = [{"term": r["concept"], "count": r["count"]} for r in rows]
                for c in concepts:
                    nodes.append({"id": c["term"], "type": "concept", "weight": c["count"]})

                # Link concepts that co-occur in the same chunk (use FTS)
                terms = [c["term"] for c in concepts]
                for i, t1 in enumerate(terms[:30]):
                    for t2 in terms[i+1:30]:
                        try:
                            co = con.execute(f"""
                                SELECT COUNT(*) as cnt FROM chunks c
                                JOIN chunks_fts ON chunks_fts.rowid = c.id
                                WHERE c.subject = ? AND chunks_fts MATCH ?{go_phase_filter}
                            """, [subject, f'"{t1}" AND "{t2}"'] + go_phase_params).fetchone()
                            if co and co["cnt"] >= 2:
                                links.append({"source": t1, "target": t2, "weight": co["cnt"]})
                        except Exception:
                            pass

        else:
            # Cross-subject mode: use cross_subject_map clusters as primary edges
            # Then supplement with high-overlap concept_map concepts

            # ── Layer 1: cross_subject_map clusters (high quality) ───
            try:
                cluster_rows = con.execute(f"""
                    SELECT cluster_name, concept, subject FROM cross_subject_map{go_phase_where}
                    ORDER BY cluster_name
                """, go_phase_params).fetchall()
            except Exception:
                cluster_rows = []

            cluster_concepts = {}  # cluster_name -> [{concept, subject}]
            for r in cluster_rows:
                cluster_concepts.setdefault(r["cluster_name"], []).append({
                    "concept": r["concept"], "subject": r["subject"]
                })

            cluster_node_ids = set()
            for cl_name, members in cluster_concepts.items():
                for m in members:
                    cid = m["concept"]
                    if cid not in cluster_node_ids:
                        cluster_node_ids.add(cid)
                        subjs = con.execute(
                            f"SELECT DISTINCT subject FROM concept_map WHERE concept = ?{go_phase_filter}", [cid] + go_phase_params
                        ).fetchall()
                        nodes.append({
                            "id": cid, "type": "concept",
                            "weight": len(subjs), "cluster": cl_name,
                            "subjects": [s["subject"] for s in subjs],
                        })
                # Link cluster members to each other
                for i, m1 in enumerate(members):
                    for m2 in members[i+1:]:
                        link_data = {
                            "source": m1["concept"], "target": m2["concept"],
                            "cluster": cl_name, "weight": 3,
                        }
                        ai_rel = get_ai_relation(con, m1["concept"], m2["concept"])
                        if ai_rel:
                            link_data["relation"] = ai_rel["type"]
                            link_data["description"] = ai_rel["description"]
                        links.append(link_data)

            # ── Layer 2: top cross-subject concepts (≥3 subjects, supplement) ─
            extra_needed = max(0, limit - len(cluster_node_ids))
            if extra_needed > 0:
                rows = con.execute(f"""
                    SELECT concept, COUNT(DISTINCT subject) as subj_count, SUM(count) as total
                    FROM concept_map{go_phase_where} GROUP BY concept
                    HAVING subj_count >= 3
                    ORDER BY subj_count DESC, total DESC LIMIT ?
                """, go_phase_params + [extra_needed]).fetchall()
                for r in rows:
                    if r["concept"] not in cluster_node_ids:
                        subjs = con.execute(
                            f"SELECT DISTINCT subject FROM concept_map WHERE concept = ?{go_phase_filter}", [r["concept"]] + go_phase_params
                        ).fetchall()
                        nodes.append({
                            "id": r["concept"], "type": "concept",
                            "weight": r["subj_count"], "total": r["total"],
                            "subjects": [s["subject"] for s in subjs],
                        })

            # Add subject nodes
            all_subjects = set()
            for n in nodes:
                if n["type"] == "concept":
                    for s in n.get("subjects", []):
                        all_subjects.add(s)
            for s in sorted(all_subjects):
                nodes.append({"id": s, "type": "subject"})

            # Links: concept -> subject (only for non-cluster nodes)
            for n in nodes:
                if n["type"] == "concept" and not n.get("cluster"):
                    for s in n.get("subjects", []):
                        links.append({"source": n["id"], "target": s, "weight": 1})

        # Get available subjects for mode selector
        subjects = [r["subject"] for r in con.execute(
            "SELECT DISTINCT subject FROM concept_map ORDER BY subject"
        ).fetchall()]

        return {"mode": mode, "subject": subject, "nodes": nodes, "links": links, "subjects": subjects}
    finally:
        con.close()


# ── Health Check ─────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    """Health check: DB, FAISS, model status."""
    status = {"status": "ok", "ts": time.time()}
    supplemental_source = _get_supplemental_source_info()
    supplemental_manifest = _load_supplemental_manifest()
    supplemental_has_manifest = bool(supplemental_manifest)
    supplemental_source_is_index = supplemental_source["source"] != "directory"
    supplemental_unresolved_pages = int((supplemental_manifest or {}).get("unresolved_pages") or 0)
    supplemental_unresolved_books = int((supplemental_manifest or {}).get("unresolved_books") or 0)
    supplemental_edition_conflicts = int((supplemental_manifest or {}).get("edition_conflicts") or 0)
    supplemental_ok = bool(supplemental_source["available"]) and (
        not supplemental_source_is_index or (
            supplemental_has_manifest
            and supplemental_unresolved_pages == 0
            and supplemental_unresolved_books == 0
            and supplemental_edition_conflicts == 0
        )
    )
    # DB check
    try:
        con = get_db()
        n = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        status["db"] = {"ok": True, "chunks": n}
        con.close()
    except Exception as e:
        status["db"] = {"ok": False, "error": str(e)}
        status["status"] = "degraded"
    # FAISS check
    status["faiss"] = {
        "ok": faiss_index is not None,
        "vectors": faiss_index.ntotal if faiss_index else 0,
        "type": type(faiss_index).__name__ if faiss_index else None,
        "reason": faiss_status_reason,
        "manifest": {
            "present": faiss_manifest is not None,
            "schema_version": (faiss_manifest or {}).get("schema_version"),
            "model": (faiss_manifest or {}).get("model", {}).get("name"),
            "vector_rows": (faiss_manifest or {}).get("index", {}).get("vector_rows"),
            "dimension": (faiss_manifest or {}).get("index", {}).get("dimension"),
        },
    }
    # Model check
    status["model"] = {
        "ok": embedder is not None,
        "name": EMBEDDER_NAME,
    }
    status["reranker"] = {
        "enabled": RERANKER_ENABLED,
        "preload": RERANKER_PRELOAD,
        "loaded": reranker is not None,
        "name": RERANKER_NAME,
        "reason": reranker_status_reason,
        "max_candidates": RERANKER_MAX_CANDIDATES,
        "final_limit": RERANKER_FINAL_LIMIT,
    }
    status["graphrag"] = {
        "enabled": GRAPH_RAG_ENABLED,
        "max_relations": GRAPH_RAG_MAX_RELATIONS,
    }
    status["supplemental_vectors"] = {
        "enabled": SUPPLEMENTAL_VECTOR_ENABLED,
        "loaded": supplemental_faiss_index is not None,
        "vectors": supplemental_faiss_index.ntotal if supplemental_faiss_index else 0,
        "path": str((_get_supplemental_vector_source_info().get("path") or "")) or None,
        "reason": supplemental_faiss_status_reason,
        "manifest": {
            "present": supplemental_faiss_manifest is not None,
            "schema_version": (supplemental_faiss_manifest or {}).get("schema_version"),
            "model": (supplemental_faiss_manifest or {}).get("model", {}).get("name"),
            "vector_rows": (supplemental_faiss_manifest or {}).get("index", {}).get("vector_rows"),
            "dimension": (supplemental_faiss_manifest or {}).get("index", {}).get("dimension"),
        },
    }
    status["supplemental"] = {
        "ok": supplemental_ok,
        "required": SUPPLEMENTAL_REQUIRED,
        "source": supplemental_source["source"],
        "path": str(supplemental_source["path"]) if supplemental_source["path"] else None,
        "manifest": {
            "present": supplemental_has_manifest,
            "schema_version": supplemental_manifest.get("schema_version"),
            "source_files_total": supplemental_manifest.get("source_files_total"),
            "source_files_indexed": supplemental_manifest.get("source_files_indexed"),
            "books": supplemental_manifest.get("books"),
            "primary_books": supplemental_manifest.get("primary_books"),
            "supplemental_only_books": supplemental_manifest.get("supplemental_only_books"),
            "pages": supplemental_manifest.get("pages"),
            "subjects": len(supplemental_manifest.get("subjects") or {}),
            "unresolved_books": supplemental_unresolved_books,
            "unresolved_pages": supplemental_unresolved_pages,
            "edition_conflicts": supplemental_edition_conflicts,
            "output_sha256": (supplemental_manifest.get("output") or {}).get("sha256"),
        },
    }
    # Cache stats
    status["cache"] = {"size": len(_cache), "maxsize": getattr(_cache, 'maxsize', 'unlimited')}
    if not status["faiss"]["ok"] or not status["model"]["ok"]:
        status["status"] = "degraded"
    if SUPPLEMENTAL_REQUIRED and not status["supplemental"]["ok"]:
        status["status"] = "degraded"
    return status


# Images served from Cloudflare R2 CDN
IMG_CDN = os.getenv("IMG_CDN", "https://img.rdfzer.com")

# ── Book Map for page images ─────────────────────────────────────────
_book_map = {}  # book_key -> {key, title, pages}
_book_key_to_short = {}  # book_key -> short_key (12-char hash)
_book_version_manifest = {}  # {"by_content_id": {...}, "by_book_key": {...}}
try:
    if TEXTBOOK_VERSION_MANIFEST_PATH.exists():
        with open(TEXTBOOK_VERSION_MANIFEST_PATH, encoding="utf-8") as _f:
            _book_version_manifest = _normalize_textbook_version_manifest(json.load(_f))
    else:
        _book_version_manifest = {"by_content_id": {}, "by_book_key": {}}
    _bm_path = FRONTEND / "assets/pages/book_map.json"
    if _bm_path.exists():
        with open(_bm_path) as _f:
            _book_map = json.load(_f)
        _book_key_to_short = {bk: info["key"] for bk, info in _book_map.items()}
        print(f"Book map loaded: {len(_book_map)} books", flush=True)
except Exception as e:
    print(f"Book map load failed: {e}", flush=True)


def _extract_book_content_id(book_key: str | None) -> str:
    if not book_key:
        return ""
    match = re.search(r"([0-9a-f]{8}-[0-9a-f\-]{27})", book_key, re.IGNORECASE)
    return match.group(1) if match else ""


def _resolve_book_runtime_meta(book_key: str | None, fallback_title: str | None = None, content_id: str | None = None) -> dict:
    info = _book_map.get(book_key or "", {})
    resolved_content_id = str(content_id or info.get("content_id") or _extract_book_content_id(book_key)).strip()
    manifest_row = _book_version_manifest_row(book_key=book_key, content_id=resolved_content_id)
    base_title = str(manifest_row.get("title") or info.get("title") or fallback_title or "").strip()
    display_title = str(manifest_row.get("display_title") or info.get("display_title") or base_title or fallback_title or "").strip()
    edition = str(manifest_row.get("edition") or info.get("edition") or "").strip()
    subject = str(manifest_row.get("subject") or info.get("subject") or "").strip()
    return {
        "content_id": resolved_content_id,
        "title": base_title or str(fallback_title or "").strip(),
        "display_title": display_title or base_title or str(fallback_title or "").strip(),
        "edition": edition,
        "subject": subject,
    }


def _apply_book_runtime_meta(payload: dict, *, book_key: str | None, fallback_title: str | None = None, content_id: str | None = None) -> dict:
    meta = _resolve_book_runtime_meta(book_key, fallback_title=fallback_title, content_id=content_id)
    payload["title"] = meta["display_title"] or payload.get("title") or fallback_title or ""
    payload["base_title"] = meta["title"] or fallback_title or ""
    payload["display_title"] = payload.get("display_title") or meta["display_title"] or payload["title"]
    payload["edition"] = meta["edition"]
    if meta["content_id"]:
        payload["content_id"] = meta["content_id"]
    return payload


@app.get("/api/page-image")
def page_image(
    book_key: str = Query(..., description="book_key from search result"),
    page: int = Query(..., ge=0, description="Page number (0-indexed)"),
    context: int = Query(4, ge=0, le=8, description="Number of context pages before/after"),
    phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all"),
):
    """Return R2 CDN URLs for a page and surrounding context pages."""
    # Find the book in book_map
    info = _book_map.get(book_key)
    if not info:
        raise HTTPException(404, f"Book not found: {book_key[:60]}")
    if not _is_supported_textbook_book(
        book_key=book_key,
        content_id=info.get("content_id"),
        subject=info.get("subject"),
        edition=info.get("edition"),
        phase=phase,
    ):
        raise HTTPException(404, f"Book not available in current edition scope: {book_key[:60]}")

    short_key = info["key"]
    total_pages = info["pages"]
    title = _resolve_book_runtime_meta(
        book_key,
        fallback_title=info.get("title"),
        content_id=info.get("content_id"),
    )["display_title"] or info["title"]

    # Clamp page to valid range
    page = max(0, min(page, total_pages - 1))

    # Build context page list
    start = max(0, page - context)
    end = min(total_pages - 1, page + context)
    pages = []
    for p in range(start, end + 1):
        pages.append({
            "page": p,
            "url": f"{IMG_CDN}/pages/{short_key}/p{p}.webp",
            "current": p == page,
        })

    return {
        "book_key": book_key,
        "short_key": short_key,
        "title": title,
        "current_page": page,
        "total_pages": total_pages,
        "pages": pages,
    }


@app.get("/api/book-pages")
def book_pages(phase: Optional[str] = Query(None, description="Filter by phase: 高中, 初中, or None for all")):
    """Return the full book map for frontend use."""
    return {
        bk: {
            "key": info["key"],
            "title": _resolve_book_runtime_meta(
                bk,
                fallback_title=info.get("title"),
                content_id=info.get("content_id"),
            )["display_title"] or info.get("title", ""),
            "pages": info["pages"],
            "edition": info.get("edition", ""),
        }
        for bk, info in _book_map.items()
        if _is_supported_textbook_book(
            book_key=bk,
            content_id=info.get("content_id"),
            subject=info.get("subject"),
            edition=info.get("edition"),
            phase=phase,
        )
        and (not phase or (str(info.get("phase") or ("初中" if bk.startswith("初中_") else "高中")).strip() == phase))
    }


# Serve frontend
if FRONTEND.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (FRONTEND / "index.html").read_text(encoding="utf-8")

    @app.get("/dict.html", response_class=HTMLResponse)
    def dict_page():
        return (FRONTEND / "dict.html").read_text(encoding="utf-8")

    @app.get("/chuzhong.html", response_class=HTMLResponse)
    def chuzhong_page():
        return (FRONTEND / "chuzhong.html").read_text(encoding="utf-8")

    @app.get("/chuzhong-dict.html", response_class=HTMLResponse)
    def chuzhong_dict_page():
        return (FRONTEND / "chuzhong-dict.html").read_text(encoding="utf-8")
