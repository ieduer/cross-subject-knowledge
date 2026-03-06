"""
跨学科教材知识平台 · FastAPI 后端
"""
import sqlite3, json, math, os, re, time, functools, hashlib
import urllib.request, urllib.error
from collections import Counter
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Query, HTTPException, Body
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
FRONTEND = Path(__file__).parent.parent / "frontend"
FAISS_INDEX_PATH = _resolve_data_asset("textbook_chunks.index")
FAISS_MANIFEST_PATH = _resolve_data_asset("textbook_chunks.manifest.json")

app = FastAPI(title="跨学科教材知识平台", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
CHAT_STOPWORDS = {
    "请", "先", "再", "继续", "解释", "一下", "分析", "总结", "说明", "告诉", "给我",
    "这个", "这个概念", "这个问题", "它", "那", "哪些", "哪个", "什么", "为什么",
    "怎么", "如何", "最", "常见", "共同", "核心", "关系", "区别", "联系", "如果",
    "我要", "复习", "应该", "顺序", "串起来", "学习", "建议", "围绕", "容易", "混淆",
    "还有", "以及", "一下子", "可以", "请问", "高考", "学科", "里的",
    "综合", "综合解读", "解读", "突出", "跨学科", "联动", "整合", "对比", "比较",
    "展开", "展开讲讲", "梳理", "串联", "理解", "给出", "提出",
}


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
            embedder = SentenceTransformer(EMBEDDER_NAME)
            print(f"FAISS index loaded with {faiss_index.ntotal} vectors. Model: {EMBEDDER_NAME}", flush=True)
    except Exception as e:
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
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _clean_query_text(query: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff\s]", "", (query or "")).strip()


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
        try:
            row = con.execute(
                """
                SELECT c.id, c.subject, c.title, c.book_key, c.section, c.logical_page, c.text,
                       s.summary AS ai_summary
                FROM chunks c
                LEFT JOIN ai_summaries s ON s.chunk_id = c.id
                WHERE c.source = 'mineru'
                  AND c.subject = ?
                  AND c.title = ?
                  AND (c.logical_page = ? OR c.section = ?)
                ORDER BY CASE
                    WHEN c.logical_page = ? THEN 0
                    WHEN c.section = ? THEN 1
                    ELSE 2
                END, c.id
                LIMIT 1
                """,
                (
                    parsed["subject"],
                    parsed["title"],
                    parsed["page"],
                    parsed["page"],
                    parsed["page"],
                    parsed["page"],
                ),
            ).fetchone()
        except Exception:
            row = None
        if not row or row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        snippet = _compose_chunk_snippet(row["ai_summary"], row["text"], limit=180)
        logical_page = row["logical_page"] if row["logical_page"] is not None else row["section"]
        resolved.append(
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
            }
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
    rows = []
    existing_ids = set()

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
        (source, f"%{clean_q}%", limit),
    ).fetchall()
    for r in like_rows:
        rows.append(dict(r))
        existing_ids.add(r["id"])

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
            (source, clean_q, limit),
        ).fetchall()
    except Exception:
        fts_rows = []

    for r in fts_rows:
        if r["id"] in existing_ids:
            continue
        rows.append(dict(r))
        existing_ids.add(r["id"])

    rows.sort(key=lambda item: item["rank"])
    return rows[:limit]


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
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break

    rows.sort(key=lambda item: (item.get("_term_index", 0), -(item.get("year") or 0), item["id"]))
    return rows[:limit]


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
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break

    rows.sort(key=lambda item: (item.get("_term_index", 0), item["rank"]))
    return rows[:limit]


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
        selected = []
        for row in subject_rows[:2]:
            logical_page = row["logical_page"] if row["logical_page"] is not None else row["section"]
            snippet = _compose_chunk_snippet(row.get("ai_summary"), row.get("text"), limit=180)
            citation = f"[{subject}·{row['title']}·p{logical_page}]"
            item = {
                "id": row["id"],
                "subject": subject,
                "title": row["title"],
                "book_key": row["book_key"],
                "section": row["section"],
                "logical_page": logical_page,
                "snippet": snippet,
                "citation": citation,
                "matched_term": row.get("matched_term"),
            }
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

    history_lines = []
    for msg in (history or [])[-6:]:
        role = "用户" if msg.get("role") == "user" else "助手"
        content = (msg.get("content") or "").strip()
        if content:
            history_lines.append(f"{role}: {content[:300]}")

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


def _build_chat_prompt(query: str, user_message: str, context_payload: dict, history: list[dict] | None = None) -> str:
    history_text = (context_payload.get("history_text") or "").strip()
    if history and not history_text:
        history_text = "\n".join(
            f"{'用户' if msg.get('role') == 'user' else '助手'}: {(msg.get('content') or '').strip()[:300]}"
            for msg in history[-6:]
            if (msg.get("content") or "").strip()
        )
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


def _call_ai_service(prompt: str) -> dict:
    payload_obj = {
        "prompt": prompt,
        "model": AI_SERVICE_MODEL,
        "taskType": AI_SERVICE_TASK_TYPE,
        "thinkingLevel": AI_SERVICE_THINKING_LEVEL,
    }
    payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
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

    for attempt in range(AI_SERVICE_RETRIES + 1):
        request = urllib.request.Request(
            AI_SERVICE_URL,
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=AI_SERVICE_TIMEOUT) as response:
                raw = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")[:400]
            last_http_error = (e.code, detail)
            if e.code >= 500 and attempt < AI_SERVICE_RETRIES:
                time.sleep(AI_SERVICE_RETRY_DELAY)
                continue
            raise HTTPException(502, f"AI service http error: {e.code} {detail}") from e
        except urllib.error.URLError as e:
            last_network_error = str(e.reason)
            if attempt < AI_SERVICE_RETRIES:
                time.sleep(AI_SERVICE_RETRY_DELAY)
                continue
            raise HTTPException(502, f"AI service unavailable: {e.reason}") from e
        except TimeoutError as e:
            timeout_hit = True
            if attempt < AI_SERVICE_RETRIES:
                time.sleep(AI_SERVICE_RETRY_DELAY)
                continue
            raise HTTPException(504, "AI service timeout") from e
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
        con = get_db()
        con.execute(
            "INSERT INTO search_logs (query, query_normalized, subject, book_key, source, result_count, ts) VALUES (?,?,?,?,?,?,?)",
            (query.strip(), normalized, subject, book_key, source, result_count, time.time())
        )
        con.commit()
        con.close()
    except Exception:
        pass  # never block search for logging failures


def init_ai_chat_logs():
    """Create ai_chat_logs table if not exists."""
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
        con = get_db()
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

        rows = []
        existing_ids = set()

        # 1. Exact Substring Match (LIKE)
        # Guarantees we NEVER miss a direct textual match due to FTS5 tokenization issues
        like_params = [clean_q, f"%{clean_q}%"] + filter_params + [limit, offset]
        like_rows = con.execute(f"""
            SELECT c.id, c.subject, c.title, c.book_key, c.section, c.logical_page,
                   SUBSTR(c.text, MAX(1, INSTR(c.text, ?)-30), 120) as snippet,
                   c.text, c.source, c.year, c.category,
                   -100.0 as rank
            FROM chunks c
            WHERE c.text LIKE ? {where_extra}
            LIMIT ? OFFSET ?
        """, like_params).fetchall()
        
        for r in like_rows:
            d = dict(r)
            # Add basic highlighting for the LIKE snippet
            d['snippet'] = d['snippet'].replace(clean_q, f"<mark>{clean_q}</mark>")
            d["match_channel"] = "exact"
            rows.append(d)
            existing_ids.add(d['id'])

        # 2. Fuzzy/Keyword Match (FTS5)
        # Handles multiple keywords, spaces, etc.
        fts_params = [clean_q] + filter_params + [limit, offset]
        
        # Order clause
        order_clause = "ORDER BY rank"
        if sort == "images":
            order_clause = "ORDER BY (LENGTH(c.text) - LENGTH(REPLACE(c.text, '![', ''))) DESC, rank"

        fts_rows = con.execute(f"""
            SELECT c.id, c.subject, c.title, c.book_key, c.section, c.logical_page,
                   snippet(chunks_fts, 0, '<mark>', '</mark>', '…', 40) as snippet,
                   c.text, c.source, c.year, c.category,
                   f.rank as rank
            FROM chunks c
            JOIN chunks_fts f ON c.id = f.rowid
            WHERE chunks_fts MATCH ? {where_extra}
            {order_clause}
            LIMIT ? OFFSET ?
        """, fts_params).fetchall()

        for r in fts_rows:
            if r['id'] not in existing_ids:
                d = dict(r)
                d["match_channel"] = "fts"
                rows.append(d)
                existing_ids.add(r['id'])

        # 3. Sort by rank (exact matches get -100.0 so they appear first) and trim to limit
        if sort != "images":
            rows.sort(key=lambda x: x['rank'])
        rows = rows[:limit]

        # Optional: filter to only results with images
        if has_images:
            rows = [r for r in rows if '![' in (r['text'] or '')]

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
                "title": r["title"],
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
            if r["source"] == "gaokao":
                result_item["year"] = r["year"]
                result_item["category"] = r["category"]
            by_subject[s]["results"].append(result_item)
            by_subject[s]["count"] += 1

        subject_counts_counter = Counter(r["subject"] for r in rows)
        subject_counts = dict(
            sorted(subject_counts_counter.items(), key=lambda item: item[1], reverse=True)
        )
        total = len(rows)

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
        return {
            "concept_nodes": nodes,
            "subject_nodes": subject_nodes,
            "links": links,
            "clusters": clusters,
        }
    finally:
        con.close()


@app.get("/api/books")
def books():
    """List all textbooks grouped by subject."""
    con = get_db()
    try:
        rows = con.execute("""
            SELECT DISTINCT book_key, title, subject
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
            by_subject[s]["books"].append({
                "book_key": r["book_key"],
                "title": r["title"],
            })
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
def chat_context(payload: dict = Body(...)):
    """Build grounded context for AI chat before calling an external model service."""
    query = str(payload.get("query", "")).strip()
    user_message = str(payload.get("user_message", "")).strip()
    history = payload.get("history") or []

    con = get_db()
    try:
        return _build_chat_context_payload(con, query, user_message, history=history)
    finally:
        con.close()


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
def chat(payload: dict = Body(...)):
    """Server-side grounded AI chat orchestration."""
    query = str(payload.get("query", "")).strip()
    user_message = str(payload.get("user_message", "")).strip()
    history = payload.get("history") or []
    if not query or not user_message:
        raise HTTPException(400, "query and user_message are required")

    con = get_db()
    try:
        context_payload = _build_chat_context_payload(con, query, user_message, history=history)
    finally:
        con.close()

    prompt = _build_chat_prompt(query, user_message, context_payload, history=history)
    try:
        ai_data = _call_ai_service(prompt)
        log_ai_chat(query, user_message, context_payload, success=True)
    except HTTPException as e:
        log_ai_chat(query, user_message, context_payload, success=False, error=str(e.detail))
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
    """Match text against concept_map using exact whole-word matching."""
    has_map = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='concept_map'"
    ).fetchone()
    if not has_map:
        return []

    # Get all concepts from DB — only meaningful ones
    all_concepts = con.execute(
        "SELECT DISTINCT concept FROM concept_map WHERE length(concept) >= 2"
    ).fetchall()

    matched = []
    for row in all_concepts:
        concept = row["concept"]
        # Skip pure-English noise that isn't a known scientific acronym
        has_chinese = any('\u4e00' <= c <= '\u9fff' for c in concept)
        if not has_chinese and concept.upper() not in ('DNA', 'RNA', 'ATP', 'ADP', 'PCR'):
            continue
        # Require the exact concept string to appear in the text
        if concept in text:
            # Get which subjects this concept spans
            subj_rows = con.execute(
                "SELECT subject, count FROM concept_map WHERE concept = ?",
                [concept]
            ).fetchall()
            subjects = {r["subject"]: r["count"] for r in subj_rows}
            matched.append({
                "concept": concept,
                "subjects": subjects,
                "is_cross": len(subjects) >= 2,
                "is_same_subject": subject in subjects,
            })
    # Sort by specificity: longer concepts are more specific, prioritize them
    matched.sort(key=lambda x: len(x["concept"]), reverse=True)
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
                  matched_concepts: list[str], is_same_subject: bool) -> int:
    """Compute relevance score 0-100 with IDF-weighted term importance."""
    score = 0
    rt_lower = result_text.lower()
    
    # Term overlap — weight longer/rarer terms higher (max 35 points)
    term_hits = 0
    for t in query_terms[:15]:
        if t in rt_lower:
            # Longer terms are more specific and worth more
            weight = min(3, len(t) - 1)  # 2-char=1, 3-char=2, 4+=3
            term_hits += weight
    score += min(35, term_hits)
    
    # Concept matches — high-value signal (max 40 points)
    concept_hits = 0
    for c in matched_concepts:
        if c in rt_lower:
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
                    SELECT c.id, c.subject, c.title, c.book_key, c.section, c.logical_page,
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
                    if match_id != -1 and match_id not in seen_ids and score > 0.55:
                        faiss_ids.append(int(match_id))
                
                if faiss_ids:
                    placeholders = ','.join('?' * len(faiss_ids))
                    faiss_rows = con.execute(f"""
                        SELECT c.id, c.subject, c.title, c.book_key, c.section, c.logical_page,
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
            item["matched_concepts"] = [
                term for term in precomputed_terms[:5]
                if term and term in ((item.get("text") or "") + " " + (item.get("summary") or ""))
            ] or precomputed_terms[:3]
            if item["subject"] == q_subject:
                same_subject.append(item)
            else:
                cross_subject.append(item)

        scoring_terms = list(dict.fromkeys(precomputed_terms[:6] + top_terms[:10]))
        scoring_concepts = list(dict.fromkeys(precomputed_terms[:6] + concept_names[:8]))
        for r, link_type in all_results:
            r_text = r["text"] or ""
            # Find which concepts matched in this result
            r_matched = [c for c in scoring_concepts if c in r_text]
            score = _score_result(r_text, scoring_terms, scoring_concepts,
                                  r["subject"] == q_subject)
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
            r_matched = [c for c in concept_names if c in r_text]
            score = _score_result(r_text, top_terms, concept_names, False)
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
                    if match_id != -1 and match_id not in seen_ids and score > 0.6:
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
try:
    _bm_path = FRONTEND / "assets/pages/book_map.json"
    if _bm_path.exists():
        with open(_bm_path) as _f:
            _book_map = json.load(_f)
        _book_key_to_short = {bk: info["key"] for bk, info in _book_map.items()}
        print(f"Book map loaded: {len(_book_map)} books", flush=True)
except Exception as e:
    print(f"Book map load failed: {e}", flush=True)


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
    title = info["title"]

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
    return {bk: {"key": info["key"], "title": info["title"], "pages": info["pages"]}
            for bk, info in _book_map.items()}


# Serve frontend
if FRONTEND.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (FRONTEND / "index.html").read_text(encoding="utf-8")
