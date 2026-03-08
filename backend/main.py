"""
跨学科教材知识平台 · FastAPI 后端
"""
import asyncio, sqlite3, json, math, os, re, time, functools, hashlib, threading, unicodedata
from collections import Counter
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
    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer
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
DICT_HEADWORD_INDEX_PATH = _resolve_data_asset("dict_headword_pages.json")
DICT_QC_PATH = _resolve_data_asset("dict_headword_qc.json")
TEXTBOOK_CLASSICS_MANIFEST_PATH = DATA_ROOT / "index" / "textbook_classics_manifest.json"
BUNDLED_TEXTBOOK_CLASSICS_MANIFEST_PATH = Path(__file__).with_name("textbook_classics_manifest.json")
TEXTBOOK_VERSION_MANIFEST_PATH = Path(__file__).with_name("textbook_version_manifest.json")
XUCI_SINGLE_CHAR_INDEX_PATH = DATA_ROOT / "index" / "xuci_single_char_index.json"
BUNDLED_XUCI_SINGLE_CHAR_INDEX_PATH = Path(__file__).with_name("xuci_single_char_index.json")
FRONTEND = Path(__file__).parent.parent / "frontend"
FAISS_INDEX_PATH = _resolve_data_asset("textbook_chunks.index")
FAISS_MANIFEST_PATH = _resolve_data_asset("textbook_chunks.manifest.json")
SQLITE_BUSY_TIMEOUT_MS = max(1000, int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000")))
SQLITE_CONNECT_TIMEOUT_SEC = max(1.0, SQLITE_BUSY_TIMEOUT_MS / 1000.0)
FAISS_SCORE_THRESHOLD = max(0.0, min(1.0, float(os.getenv("FAISS_SCORE_THRESHOLD", "0.62"))))

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
embedder = None
EMBEDDER_NAME = os.getenv("EMBEDDER", "BAAI/bge-m3")  # upgraded from bge-small-zh-v1.5
faiss_status_reason = None
faiss_manifest = None
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


def _ranked_row_sort_key(item: dict, sort: str = "relevance") -> tuple:
    if sort == "images":
        return (-((item.get("text") or "").count("![")), item.get("rank", 0), item.get("id", 0))
    return (item.get("rank", 0), item.get("id", 0))


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


def _fetch_chat_rows(con, clean_q: str, *, source: str, limit: int):
    candidate_limit = _candidate_window_limit(limit, multiplier=2, minimum=max(8, limit * 2), cap=160)

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
        WHERE c.source = ? AND c.text LIKE ?
        LIMIT ?
        """,
        (source, f"%{clean_q}%", candidate_limit),
    ).fetchall()

    try:
        fts_rows = con.execute(
            """
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
            WHERE c.source = ? AND chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (source, clean_q, candidate_limit),
        ).fetchall()
    except Exception:
        fts_rows = []

    return _merge_ranked_rows(like_rows, fts_rows)[:limit]


def _fetch_ai_gaokao_rows_for_terms(con, terms: list[str], limit: int) -> list[dict]:
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
                """
                SELECT c.id, c.subject, c.title, c.book_key, c.section, c.logical_page,
                       c.text, c.source, c.year, c.category, -90.0 AS rank,
                       '' AS ai_summary,
                       ag.summary AS ai_gaokao_summary,
                       ag.knowledge_points AS ai_gaokao_knowledge_points,
                       ag.textbook_refs AS ai_gaokao_textbook_refs
                FROM ai_gaokao_links ag
                JOIN chunks c ON c.id = ag.chunk_id
                WHERE c.source = 'gaokao'
                  AND (ag.summary LIKE ? OR ag.knowledge_points LIKE ? OR ag.textbook_refs LIKE ?)
                ORDER BY c.year DESC, c.id DESC
                LIMIT ?
                """,
                (like_term, like_term, like_term, per_term_limit),
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


def _fetch_chat_rows_for_terms(con, terms: list[str], *, source: str, limit: int):
    rows = []
    existing_ids = set()
    if not terms:
        return rows

    per_term_limit = max(4, math.ceil(limit / max(1, min(len(terms), 3))))
    for idx, term in enumerate(terms):
        for row in _fetch_chat_rows(con, term, source=source, limit=per_term_limit):
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


def _build_chat_context_payload(con, query: str, user_message: str, history: list[dict] | None = None) -> dict:
    clean_q = _clean_query_text(query)
    if not clean_q:
        raise HTTPException(400, "Invalid query")

    search_terms = _derive_chat_search_terms(query, user_message)
    retrieval_terms, alias_hints = _expand_chat_search_terms(con, search_terms)
    relation_hints = _fetch_ai_relation_hints(con, search_terms, limit=4)

    textbook_rows = _fetch_chat_rows_for_terms(con, retrieval_terms, source="mineru", limit=16)
    gaokao_rows = _fetch_chat_rows_for_terms(con, retrieval_terms, source="gaokao", limit=4)
    for row in _fetch_ai_gaokao_rows_for_terms(con, retrieval_terms, limit=4):
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
        "top_subjects": [
            {"subject": group["subject"], "count": group["count"]}
            for group in groups[:4]
        ],
    }

    return {
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
        "summary": summary,
        "suggested_questions": [
            f"请先解释「{query}」在不同学科里的共同核心。",
            f"「{query}」在高考里最常见的考法是什么？",
            f"围绕「{query}」最容易混淆的概念有哪些？",
            f"如果我要复习「{query}」，应该按什么顺序串起来学？",
        ],
    }


def _build_chat_context_for_request(query: str, user_message: str, history: list[dict] | None = None) -> dict:
    con = get_db()
    try:
        return _build_chat_context_payload(con, query, user_message, history=history)
    finally:
        con.close()


def _build_chat_prompt(query: str, user_message: str, context_payload: dict, history: list[dict] | None = None) -> str:
    history_text = (context_payload.get("history_text") or "").strip()
    if history and not history_text:
        history_text = "\n".join(_format_chat_history_lines(history))
    if not history_text:
        history_text = "（无）"

    return f"""你是一位资深跨学科教育专家。用户当前搜索词是「{context_payload.get('query') or query}」。

本轮检索关注词：
{ "、".join(context_payload.get("search_terms_used") or [query]) }

检索扩展词（含别名）：
{ "、".join(context_payload.get("retrieval_terms_used") or context_payload.get("search_terms_used") or [query]) }

概念别名 / 同义表达：
{context_payload.get('alias_text') or '（无）'}

概念关系提示：
{context_payload.get('relation_text') or '（无）'}

教材证据（多学科原文）：
{context_payload.get('context_text') or '（无）'}

高考证据（如有）：
{context_payload.get('gaokao_text') or '（无）'}

历史对话：
{history_text}

用户本轮问题：
{user_message}

请按以下结构回答：
【核心结论】先用 1-2 句讲清本质。
【学科联动】分点说明不同学科如何描述同一概念，尽量标注出处，格式：[学科·书名·p页码]。
【高考考法】如果给定证据里有真题，再说明常见考法 / 易错点；没有就写“高考证据不足”。
【学习建议】给出面向高中生的复习顺序或追问方向。

规则：
1. 只根据给定证据回答，不要编造页码或教材内容。
2. 如果证据不足，必须明确说“证据不足”。
3. 若用户追问，保持连续回答，不重复整段前文。
4. 可以参考“概念别名 / 关系提示”组织答案，但不能把它们当成教材原文引用。
5. 语言简洁、具体，避免空泛套话。
6. 总长度尽量控制在 280 字以内。"""


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
                    result_count INTEGER DEFAULT 0,
                    ts REAL NOT NULL
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON search_logs(ts DESC)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_logs_qn ON search_logs(query_normalized)")
            con.commit()
        finally:
            con.close()

init_search_logs()


def log_search(query: str, subject=None, book_key=None, source=None, result_count=0):
    """Record a search query asynchronously."""
    normalized = re.sub(r'\s+', '', query.strip().lower())
    if len(normalized) < 1:
        return
    try:
        with _write_lock:
            con = get_db()
            try:
                con.execute(
                    "INSERT INTO search_logs (query, query_normalized, subject, book_key, source, result_count, ts) VALUES (?,?,?,?,?,?,?)",
                    (query.strip(), normalized, subject, book_key, source, result_count, time.time())
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
                    success INTEGER DEFAULT 0,
                    error TEXT,
                    ts REAL NOT NULL
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_ai_chat_logs_ts ON ai_chat_logs(ts DESC)")
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
):
    try:
        with _write_lock:
            con = get_db()
            try:
                con.execute(
                    """
                    INSERT INTO ai_chat_logs (
                        query, user_message, subject_count, evidence_count, gaokao_hit_count,
                        provider, success, error, ts
                    ) VALUES (?,?,?,?,?,?,?,?,?)
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
):
    summary = context_payload.get("summary") or {}
    _write_ai_chat_log(
        query,
        user_message,
        summary,
        provider=provider or AI_SERVICE_LABEL,
        success=success,
        error=error,
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
) -> str:
    history_text = "\n".join(_format_chat_history_lines(history)) or "（无）"
    return f"""你是“实虚词典”的古汉语学习教练。用户当前检索的是「{headword}」。

词典证据：
{dict_context[:5000] or '（无）'}

教材中的古文 / 古诗词证据：
{textbook_context[:5000] or '（无）'}

语文真题中的古文 / 古诗词证据：
{gaokao_context[:4000] or '（无）'}

历史对话：
{history_text}

用户本轮问题：
{user_message}

请按这个结构回答：
【字词定位】先说明这是实词、虚词，或两者兼有；若证据不足，明确写“证据不足”。
【教材必记】只根据给定教材证据，提炼最该记住的义项、句法位置、固定搭配。
【真题拿分】只根据给定真题证据，总结高频考法、常见误判、答题抓手。
【速记方法】给出可直接背诵的 3-6 条记忆或辨析规则。

规则：
1. 只能依据给定证据，不得编造出处、页码、义项或例句。
2. 若同一字在三本词典解释不同，要点明差异。
3. 若用户追问，保持上下文连续，不重复整段前文。
4. 语言尽量具体，面向高中语文得分。
5. 优先使用简体；若涉及《辞源》原字头，可顺带标出繁体。"""


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


def _build_dict_chat_context_for_request(headword: str) -> dict:
    dict_payload = dict_search(headword, limit=8)
    textbook_payload = dict_textbook(headword, limit=8)
    gaokao_payload = dict_gaokao(headword, limit=6)
    dict_entries = dict_payload.get("entries") or []
    textbook_results = textbook_payload.get("results") or []
    gaokao_results = gaokao_payload.get("results") or []
    return {
        "dict_entries": dict_entries,
        "textbook_results": textbook_results,
        "gaokao_results": gaokao_results,
        "dict_context": _build_dict_context_block(dict_entries),
        "textbook_context": _build_dict_textbook_context_block(textbook_results),
        "gaokao_context": _build_dict_gaokao_context_block(gaokao_results),
        "summary": {
            "subject_count": 1 if textbook_results or gaokao_results else 0,
            "evidence_count": len(dict_entries) + len(textbook_results) + len(gaokao_results),
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
            "summary": "官方大型国语辞典，适合核对本义、书证和历史用法。后续建议用教育部授权包做站内镜像检索。",
            "url": _build_moe_search_url("dict.revised.moe.edu.tw", compact_query),
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
            "url": _build_moe_search_url("dict.concised.moe.edu.tw", compact_query),
            "action_label": "打开简编",
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


@app.get("/api/search")
def search(
    q: str = Query(..., min_length=1, max_length=200),
    subject: Optional[str] = Query(None),
    book_key: Optional[str] = Query(None),
    source: Optional[str] = Query(None, description="Filter by source: textbook, gaokao, or all"),
    sort: str = Query("relevance"),
    has_images: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Full-text search with cross-subject grouping, sorting, and filtering."""
    con = get_db()
    try:
        # Clean query for FTS5
        clean_q = re.sub(r'[^\w\u4e00-\u9fff\s]', '', q).strip()
        if not clean_q:
            raise HTTPException(400, "Invalid query")

        # Build WHERE filters shared by both queries
        where_extra = ""
        filter_params = []
        if subject:
            where_extra += " AND c.subject = ?"
            filter_params.append(subject)
        if book_key:
            where_extra += " AND c.book_key = ?"
            filter_params.append(book_key)
        if source == 'textbook':
            where_extra += " AND c.source = 'mineru'"
        elif source == 'gaokao':
            where_extra += " AND c.source = 'gaokao'"

        candidate_limit = _candidate_window_limit(limit, offset, multiplier=2, minimum=max(24, limit * 2), cap=600)

        # 1. Exact Substring Match (LIKE)
        # Guarantees we NEVER miss a direct textual match due to FTS5 tokenization issues
        like_params = [clean_q, f"%{clean_q}%"] + filter_params + [candidate_limit]
        like_rows = con.execute(f"""
            SELECT c.id, c.content_id, c.subject, c.title, c.book_key, c.section, c.logical_page,
                   SUBSTR(c.text, MAX(1, INSTR(c.text, ?)-30), 120) as snippet,
                   c.text, c.source, c.year, c.category,
                   -100.0 as rank
            FROM chunks c
            WHERE c.text LIKE ? {where_extra}
            LIMIT ?
        """, like_params).fetchall()
        like_ranked = []
        for row in like_rows:
            d = dict(row)
            d["snippet"] = d["snippet"].replace(clean_q, f"<mark>{clean_q}</mark>")
            d["match_channel"] = "exact"
            like_ranked.append(d)

        # 2. Fuzzy/Keyword Match (FTS5)
        # Handles multiple keywords, spaces, etc.
        fts_params = [clean_q] + filter_params + [candidate_limit]
        
        # Order clause
        order_clause = "ORDER BY rank"
        if sort == "images":
            order_clause = "ORDER BY (LENGTH(c.text) - LENGTH(REPLACE(c.text, '![', ''))) DESC, rank"

        try:
            fts_rows = con.execute(f"""
                SELECT c.id, c.content_id, c.subject, c.title, c.book_key, c.section, c.logical_page,
                       snippet(chunks_fts, 0, '<mark>', '</mark>', '…', 40) as snippet,
                       c.text, c.source, c.year, c.category,
                       f.rank as rank
                FROM chunks c
                JOIN chunks_fts f ON c.id = f.rowid
                WHERE chunks_fts MATCH ? {where_extra}
                {order_clause}
                LIMIT ?
            """, fts_params).fetchall()
        except Exception:
            fts_rows = []

        fts_ranked = []
        for row in fts_rows:
            d = dict(row)
            d["match_channel"] = "fts"
            fts_ranked.append(d)

        # 3. Merge, then apply filters and pagination globally.
        all_rows = _merge_ranked_rows(like_ranked, fts_ranked, sort=sort)

        # Optional: filter to only results with images
        if has_images:
            all_rows = [r for r in all_rows if '![' in (r['text'] or '')]
        rows = all_rows[offset: offset + limit]

        # Group by subject
        by_subject = {}
        for r in rows:
            s = r["subject"]
            if s not in by_subject:
                meta = SUBJECT_META.get(s, {"icon": "📚", "color": "#95a5a6"})
                by_subject[s] = {"subject": s, **meta, "results": [], "count": 0}
            # Count images in this chunk
            text = r["text"] or ""
            img_count = text.count('![')
            # Page image URL from R2
            bk = r["book_key"]
            short_key = _book_key_to_short.get(bk, "")
            page_num = r["section"] or 0
            page_url = f"{IMG_CDN}/pages/{short_key}/p{page_num}.webp" if short_key else None
            bm_info = _book_map.get(bk, {})
            result_item = {
                "id": r["id"],
                "book_key": bk,
                "section": r["section"],
                "logical_page": r["logical_page"] if "logical_page" in r.keys() and r["logical_page"] is not None else r["section"],
                "snippet": r["snippet"],
                "text": text[:2000],
                "image_count": img_count,
                "source": r["source"] or "mineru",
                "match_channel": r.get("match_channel", "fts"),
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

        subject_counts = _compute_search_subject_counts(
            con,
            clean_q,
            where_extra,
            filter_params,
            has_images=has_images,
        )
        total = sum(subject_counts.values())

        # Cross-subject hint
        cross_subjects = [s for s in subject_counts if subject_counts[s] > 0]
        hint = None
        if len(cross_subjects) >= 2:
            names = "、".join(cross_subjects[:4])
            hint = f"💡 「{q}」横跨 {len(cross_subjects)} 个学科（{names}），它们从不同角度描述了同一概念！"

        # Sort groups by cross-subject count if requested
        groups = list(by_subject.values())
        if sort == "cross":
            groups.sort(key=lambda g: g["count"], reverse=True)

        # Log the search query
        log_search(q, subject=subject, book_key=book_key, source=source, result_count=total)

        return {
            "query": q,
            "total": total,
            "subject_counts": subject_counts,
            "cross_hint": hint,
            "groups": groups,
        }
    finally:
        con.close()


@app.get("/api/search/trending")
def search_trending():
    """Return recent queries and popular queries for display."""
    con = get_db()
    try:
        # Recent unique queries (last 50, deduplicated, max 15)
        recent_rows = con.execute("""
            SELECT query, MAX(ts) as latest_ts, MAX(result_count) as cnt
            FROM search_logs
            WHERE result_count > 0
            GROUP BY query_normalized
            ORDER BY latest_ts DESC
            LIMIT 15
        """).fetchall()
        recent = [{"query": r["query"], "count": r["cnt"]} for r in recent_rows]

        # Popular queries (last 7 days, by frequency, min 2 searches)
        week_ago = time.time() - 7 * 86400
        popular_rows = con.execute("""
            SELECT query, query_normalized, COUNT(*) as freq, MAX(result_count) as cnt
            FROM search_logs
            WHERE ts > ? AND result_count > 0
            GROUP BY query_normalized
            HAVING freq >= 1
            ORDER BY freq DESC
            LIMIT 20
        """, (week_ago,)).fetchall()
        popular = [{"query": r["query"], "freq": r["freq"], "count": r["cnt"]} for r in popular_rows]

        return {"recent": recent, "popular": popular}
    finally:
        con.close()


@app.get("/api/stats")
def stats():
    """Database statistics (cached 5min)."""
    if 'stats' in _cache:
        return _cache['stats']
    con = get_db()
    try:
        total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        textbook_count = con.execute("SELECT COUNT(*) FROM chunks WHERE source='mineru' OR source IS NULL").fetchone()[0]
        gaokao_count = con.execute("SELECT COUNT(*) FROM chunks WHERE source='gaokao'").fetchone()[0]
        textbook_books = con.execute(
            "SELECT COUNT(DISTINCT book_key) FROM chunks WHERE source='mineru' OR source IS NULL"
        ).fetchone()[0]
        gaokao_multimodal = con.execute(
            "SELECT COUNT(*) FROM chunks WHERE source='gaokao' AND text LIKE '%https://img.rdfzer.com/gaokao/%'"
        ).fetchone()[0]
        dist = con.execute(
            "SELECT subject, COUNT(*) as cnt FROM chunks GROUP BY subject ORDER BY cnt DESC"
        ).fetchall()

        # Gaokao specific stats
        gaokao_years = con.execute(
            "SELECT MIN(year) as min_y, MAX(year) as max_y FROM chunks WHERE source='gaokao' AND year IS NOT NULL"
        ).fetchone()
        gaokao_by_subject = con.execute(
            "SELECT subject, COUNT(*) as cnt FROM chunks WHERE source='gaokao' GROUP BY subject ORDER BY cnt DESC"
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
        _cache['stats'] = result
        return result
    finally:
        con.close()


@app.get("/api/keywords")
def keywords(limit: int = Query(120, ge=1, le=500)):
    """Return curated academic keywords (cached 5min)."""
    cache_key = f'keywords_{limit}'
    if cache_key in _cache:
        return _cache[cache_key]
    con = get_db()
    try:
        has_table = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='curated_keywords'"
        ).fetchone()
        if has_table:
            rows = con.execute(
                "SELECT term, subject_count, total_count FROM curated_keywords ORDER BY subject_count DESC, total_count DESC LIMIT ?",
                (limit,)
            ).fetchall()
            result = {"keywords": [{"term": r["term"], "subjects": r["subject_count"], "count": r["total_count"]} for r in rows]}
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
def books():
    """List all textbooks grouped by subject."""
    con = get_db()
    try:
        rows = con.execute("""
            SELECT DISTINCT book_key, title, subject, content_id
            FROM chunks
            WHERE source != 'gaokao'
            ORDER BY subject, title
        """).fetchall()
        by_subject = {}
        for r in rows:
            s = r["subject"]
            if s not in by_subject:
                meta = SUBJECT_META.get(s, {"icon": "📚", "color": "#95a5a6"})
                by_subject[s] = {"subject": s, **meta, "books": []}
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
            })
        for payload in by_subject.values():
            payload["books"].sort(key=lambda item: (item.get("title") or "", item.get("book_key") or ""))
        return list(by_subject.values())
    finally:
        con.close()


@app.get("/api/related")
def related(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(8, ge=1, le=20),
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
    return await run_in_threadpool(_build_chat_context_for_request, query, user_message, history)


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
    if not query or not user_message:
        raise HTTPException(400, "query and user_message are required")

    context_payload = await run_in_threadpool(_build_chat_context_for_request, query, user_message, history)
    prompt = _build_chat_prompt(query, user_message, context_payload, history=history)
    try:
        ai_data = await _call_ai_service(prompt)
        await run_in_threadpool(log_ai_chat, query, user_message, context_payload, success=True)
    except HTTPException as e:
        await run_in_threadpool(log_ai_chat, query, user_message, context_payload, success=False, error=str(e.detail))
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


@app.get("/api/dict/textbook")
def dict_textbook(
    q: str = Query(..., min_length=1, max_length=40),
    limit: int = Query(30, ge=1, le=80),
):
    clean_q = _clean_query_text(q)
    if not clean_q:
        raise HTTPException(400, "Invalid query")

    con = get_db()
    try:
        rows = con.execute(
            """
            SELECT id, subject, title, book_key, section, logical_page, text
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
            LIMIT ?
            """,
            (f"%{clean_q}%", clean_q, clean_q, max(limit * 6, 120)),
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
    context_payload = await run_in_threadpool(_build_dict_chat_context_for_request, headword)
    prompt = _build_dict_chat_prompt(
        headword,
        user_message,
        dict_context=context_payload.get("dict_context", ""),
        textbook_context=context_payload.get("textbook_context", ""),
        gaokao_context=context_payload.get("gaokao_context", ""),
        history=history,
    )
    try:
        ai_data = await _call_ai_service(prompt)
        await run_in_threadpool(log_ai_chat, headword, user_message, context_payload, success=True)
    except HTTPException as e:
        await run_in_threadpool(log_ai_chat, headword, user_message, context_payload, success=False, error=str(e.detail))
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
    limit: int = Query(50, ge=1, le=200),
):
    """Word frequency for curated academic terms (pre-computed)."""
    con = get_db()
    try:
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
def heatmap():
    """Cross-subject concept sharing matrix."""
    con = get_db()
    try:
        # Get all curated concepts and their subject associations
        curated = {r["term"] for r in con.execute("SELECT term FROM curated_keywords").fetchall()}

        rows = con.execute("""
            SELECT concept, subject, SUM(count) as cnt
            FROM concept_map
            GROUP BY concept, subject
        """).fetchall()

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
def coverage(limit: int = Query(30, ge=1, le=100)):
    """Textbook vs Exam concept coverage analysis (pre-computed)."""
    con = get_db()
    try:
        has_kc = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='keyword_counts'").fetchone()
        if has_kc:
            rows = con.execute("""
                SELECT term,
                    COALESCE(SUM(CASE WHEN source='textbook' THEN count END), 0) as textbook,
                    COALESCE(SUM(CASE WHEN source='gaokao' THEN count END), 0) as gaokao
                FROM keyword_counts GROUP BY term
                HAVING textbook > 0 OR gaokao > 0
            """).fetchall()
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
def concept_breadth(limit: int = Query(50, ge=1, le=200)):
    """Rank curated concepts by cross-subject breadth (cached 5min)."""
    cache_key = f'breadth_{limit}'
    if cache_key in _cache:
        return _cache[cache_key]
    con = get_db()
    try:
        rows = con.execute("""
            SELECT ck.term, ck.subject_count, ck.total_count
            FROM curated_keywords ck
            ORDER BY ck.subject_count DESC, ck.total_count DESC
            LIMIT ?
        """, (limit,)).fetchall()

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


def _fetch_graph_local_related(con, center_term: str, center_subjects: set[str], limit: int = 15) -> list[dict]:
    """Mine related graph concepts from the center term's own high-signal chunks."""
    try:
        chunk_rows = con.execute("""
            SELECT c.subject, c.text
            FROM chunks c JOIN chunks_fts ON chunks_fts.rowid = c.id
            WHERE chunks_fts MATCH ? AND c.source = 'mineru'
            LIMIT 80
        """, (center_term,)).fetchall()
    except Exception:
        return []

    signal_chunks = [row for row in chunk_rows if _is_high_signal_graph_chunk(row["text"] or "")]
    if not signal_chunks:
        signal_chunks = chunk_rows
    if not signal_chunks:
        return []

    curated_rows = con.execute("SELECT term, subject_count, total_count FROM curated_keywords").fetchall()
    concept_rows = con.execute("SELECT concept, subject FROM concept_map").fetchall()

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
def graph_search(q: str = Query(..., min_length=1)):
    """Return a concept subgraph centered on the search term."""
    con = get_db()
    try:
        q_clean = q.strip()

        # Use FTS for precise subject distribution (not LIKE)
        try:
            center_dist = con.execute("""
                SELECT c.subject, COUNT(*) as cnt
                FROM chunks c JOIN chunks_fts ON chunks_fts.rowid = c.id
                WHERE chunks_fts MATCH ? AND c.source = 'mineru'
                GROUP BY c.subject ORDER BY cnt DESC
            """, (q_clean,)).fetchall()
        except Exception:
            center_dist = []

        if not center_dist:
            return {"center": q_clean, "nodes": [], "links": []}

        center_subjects = {r["subject"] for r in center_dist}

        # ── Priority 1: cross_subject_map cluster siblings ──────────
        cluster_related = []
        try:
            clusters = con.execute(
                "SELECT DISTINCT cluster_name FROM cross_subject_map WHERE concept = ?",
                (q_clean,)
            ).fetchall()
            for cl in clusters:
                siblings = con.execute(
                    "SELECT concept, subject FROM cross_subject_map WHERE cluster_name = ? AND concept != ?",
                    (cl["cluster_name"], q_clean)
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
        for item in _fetch_graph_local_related(con, q_clean, center_subjects, limit=20):
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
    limit: int = Query(60, ge=10, le=200),
):
    """Knowledge graph: cross-subject or per-subject concept network."""
    con = get_db()
    try:
        nodes = []
        links = []

        if mode == "subject" and subject:
            # Per-subject mode: show concepts within one subject
            rows = con.execute("""
                SELECT concept, count FROM concept_map
                WHERE subject = ? ORDER BY count DESC LIMIT ?
            """, (subject, limit)).fetchall()
            concepts = [{"term": r["concept"], "count": r["count"]} for r in rows]
            for c in concepts:
                nodes.append({"id": c["term"], "type": "concept", "weight": c["count"]})

            # Link concepts that co-occur in the same chunk (use FTS)
            terms = [c["term"] for c in concepts]
            for i, t1 in enumerate(terms[:30]):
                for t2 in terms[i+1:30]:
                    try:
                        co = con.execute("""
                            SELECT COUNT(*) as cnt FROM chunks c
                            JOIN chunks_fts ON chunks_fts.rowid = c.id
                            WHERE c.subject = ? AND chunks_fts MATCH ?
                        """, (subject, f'"{t1}" AND "{t2}"')).fetchone()
                        if co and co["cnt"] >= 2:
                            links.append({"source": t1, "target": t2, "weight": co["cnt"]})
                    except Exception:
                        pass

        else:
            # Cross-subject mode: use cross_subject_map clusters as primary edges
            # Then supplement with high-overlap concept_map concepts

            # ── Layer 1: cross_subject_map clusters (high quality) ───
            try:
                cluster_rows = con.execute("""
                    SELECT cluster_name, concept, subject FROM cross_subject_map
                    ORDER BY cluster_name
                """).fetchall()
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
                            "SELECT DISTINCT subject FROM concept_map WHERE concept = ?", (cid,)
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
                rows = con.execute("""
                    SELECT concept, COUNT(DISTINCT subject) as subj_count, SUM(count) as total
                    FROM concept_map GROUP BY concept
                    HAVING subj_count >= 3
                    ORDER BY subj_count DESC, total DESC LIMIT ?
                """, (extra_needed,)).fetchall()
                for r in rows:
                    if r["concept"] not in cluster_node_ids:
                        subjs = con.execute(
                            "SELECT DISTINCT subject FROM concept_map WHERE concept = ?", (r["concept"],)
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
    # Cache stats
    status["cache"] = {"size": len(_cache), "maxsize": getattr(_cache, 'maxsize', 'unlimited')}
    if not status["faiss"]["ok"] or not status["model"]["ok"]:
        status["status"] = "degraded"
    return status


# Images served from Cloudflare R2 CDN
IMG_CDN = os.getenv("IMG_CDN", "https://img.rdfzer.com")

# ── Book Map for page images ─────────────────────────────────────────
_book_map = {}  # book_key -> {key, title, pages}
_book_key_to_short = {}  # book_key -> short_key (12-char hash)
_book_version_manifest = {}  # content_id -> {title, display_title, edition, ...}
try:
    if TEXTBOOK_VERSION_MANIFEST_PATH.exists():
        with open(TEXTBOOK_VERSION_MANIFEST_PATH, encoding="utf-8") as _f:
            _book_version_manifest = json.load(_f)
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
    manifest_row = _book_version_manifest.get(resolved_content_id, {}) if resolved_content_id else {}
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
):
    """Return R2 CDN URLs for a page and surrounding context pages."""
    # Find the book in book_map
    info = _book_map.get(book_key)
    if not info:
        raise HTTPException(404, f"Book not found: {book_key[:60]}")

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
def book_pages():
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
