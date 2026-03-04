"""
跨学科教材知识平台 · FastAPI 后端
"""
import sqlite3, json, math, os, re, time
from collections import Counter
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

try:
    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False

# ── paths ─────────────────────────────────────────────────────────────
ROOT = Path(os.getenv("DATA_ROOT", "/home/suen/.openclaw/workspace/textbook_ai"))
DB_PATH = ROOT / "data/index/textbook_mineru_fts.db"
FRONTEND = Path(__file__).parent.parent / "frontend"
FAISS_INDEX_PATH = ROOT / "data/index/textbook_chunks.index"

app = FastAPI(title="跨学科教材知识平台", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Global AI Models ──────────────────────────────────────────────────
faiss_index = None
embedder = None

if FAISS_AVAILABLE and FAISS_INDEX_PATH.exists():
    try:
        print(f"Loading FAISS index from {FAISS_INDEX_PATH}...", flush=True)
        faiss_index = faiss.read_index(str(FAISS_INDEX_PATH))
        embedder = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        print(f"FAISS index loaded with {faiss_index.ntotal} vectors.", flush=True)
    except Exception as e:
        print(f"Failed to load FAISS index: {e}", flush=True)


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
            SELECT c.id, c.subject, c.title, c.book_key, c.section,
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
            SELECT c.id, c.subject, c.title, c.book_key, c.section,
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
                rows.append(dict(r))
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
            result_item = {
                "id": r["id"],
                "title": r["title"],
                "book_key": r["book_key"],
                "section": r["section"],
                "snippet": r["snippet"],
                "text": text[:2000],
                "image_count": img_count,
                "source": r["source"] or "mineru",
            }
            if r["source"] == "gaokao":
                result_item["year"] = r["year"]
                result_item["category"] = r["category"]
            by_subject[s]["results"].append(result_item)
            by_subject[s]["count"] += 1

        # Get total counts per subject (include all active filters)
        count_params = [f"%{clean_q}%"]
        count_where = ""
        if subject:
            count_where += " AND c.subject = ?"
            count_params.append(subject)
        if book_key:
            count_where += " AND c.book_key = ?"
            count_params.append(book_key)
        if source == 'textbook':
            count_where += " AND c.source = 'mineru'"
        elif source == 'gaokao':
            count_where += " AND c.source = 'gaokao'"
        count_rows = con.execute(f"""
            SELECT c.subject, COUNT(*) as cnt
            FROM chunks c
            WHERE c.text LIKE ? {count_where}
            GROUP BY c.subject
            ORDER BY cnt DESC
        """, count_params).fetchall()

        subject_counts = {r["subject"]: r["cnt"] for r in count_rows}
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
    """Database statistics."""
    con = get_db()
    try:
        total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        textbook_count = con.execute("SELECT COUNT(*) FROM chunks WHERE source='mineru' OR source IS NULL").fetchone()[0]
        gaokao_count = con.execute("SELECT COUNT(*) FROM chunks WHERE source='gaokao'").fetchone()[0]
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

        return {
            "total_chunks": total,
            "textbook_chunks": textbook_count,
            "gaokao_chunks": gaokao_count,
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
    finally:
        con.close()


@app.get("/api/keywords")
def keywords(limit: int = Query(120, ge=1, le=500)):
    """Return curated academic keywords for the concept carousel."""
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
            return {"keywords": [{"term": r["term"], "subjects": r["subject_count"], "count": r["total_count"]} for r in rows]}
        else:
            # Fallback
            fallback = ["蛋白质", "DNA", "光合作用", "细胞呼吸", "牛顿第二定律", "勒夏特列原理",
                        "氧化还原", "基因表达", "丝绸之路", "全球变暖", "元素周期表", "椭圆",
                        "自然选择", "分离定律", "盖斯定律", "平衡移动", "文艺复兴", "电磁波"]
            return {"keywords": [{"term": t, "subjects": 0, "count": 0} for t in fallback]}
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
    """Find concepts that co-occur with the query term."""
    con = get_db()
    try:
        clean_q = re.sub(r'[^\w\u4e00-\u9fff\s]', '', q).strip()
        if not clean_q:
            return []

        # Get text chunks matching the query
        rows = con.execute("""
            SELECT c.text
            FROM chunks c
            JOIN chunks_fts f ON c.id = f.rowid
            WHERE chunks_fts MATCH ?
            LIMIT 100
        """, [clean_q]).fetchall()

        if not rows:
            return []

        # Extract Chinese word candidates (2-4 char sequences) from matching chunks
        word_counter = Counter()
        query_chars = set(clean_q)
        for r in rows:
            text = r["text"] or ""
            # Find Chinese word-like sequences (2-4 chars)
            words = re.findall(r'[\u4e00-\u9fff]{2,4}', text)
            for w in words:
                # Skip if the word is part of the query or too generic
                if w == clean_q or w in clean_q or clean_q in w:
                    continue
                if len(w) < 2:
                    continue
                word_counter[w] += 1

        # Return top co-occurring terms (appearing in multiple chunks)
        candidates = [
            {"term": term, "count": count}
            for term, count in word_counter.most_common(limit * 3)
            if count >= 2  # must appear in at least 2 chunks
        ][:limit]

        return candidates
    finally:
        con.close()


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

        if not re.search(r'[\u4e00-\u9fff]', text):
            return {"question_id": question_id, "links": [], "cross_links": [],
                    "matched_concepts": [], "search_terms": []}

        # ── Layer 1: Concept graph matching ────────────────────────────
        matched_concepts = _match_concepts(text, q_subject, con)
        concept_names = [c["concept"] for c in matched_concepts]

        # ── Layer 2: IDF-weighted term extraction ─────────────────────
        weighted_terms = _extract_weighted_terms(text, con)
        top_terms = [t for t, _ in weighted_terms[:15]]

        if not top_terms and not concept_names:
            return {"question_id": question_id, "links": [], "cross_links": [],
                    "matched_concepts": [], "search_terms": []}

        # ── Layer 3: Cross-subject expansion ──────────────────────────
        expanded_terms = _expand_cross_subject(matched_concepts, con)

        # ── Combined FTS search ───────────────────────────────────────
        # Primary: top IDF terms + matched concepts
        search_terms = list(dict.fromkeys(top_terms[:8] + concept_names[:5]))
        if not search_terms:
            search_terms = top_terms[:10]

        search_q = ' OR '.join(search_terms[:12])

        # Secondary: expanded cross-subject terms
        expanded_q = ' OR '.join(expanded_terms[:8]) if expanded_terms else None

        seen_ids = set()
        all_results = []

        # Primary search
        try:
            rows = con.execute("""
                SELECT c.id, c.subject, c.title, c.book_key, c.section,
                       snippet(chunks_fts, 0, '<mark>', '</mark>', '…', 40) as snippet,
                       c.text
                FROM chunks c
                JOIN chunks_fts f ON c.id = f.rowid
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
        if faiss_index and embedder:
            try:
                # Encode text to 512D vector
                query_vec = embedder.encode([text[:512]], normalize_embeddings=True).astype('float32')
                D, I = faiss_index.search(query_vec, limit * 2)
                
                faiss_ids = []
                for score, match_id in zip(D[0], I[0]):
                    if match_id != -1 and match_id not in seen_ids and score > 0.55:
                        faiss_ids.append(int(match_id))
                
                if faiss_ids:
                    placeholders = ','.join('?' * len(faiss_ids))
                    faiss_rows = con.execute(f"""
                        SELECT c.id, c.subject, c.title, c.book_key, c.section,
                               substr(c.text, 1, 100) as snippet,
                               c.text
                        FROM chunks c
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
        for r, link_type in all_results:
            r_text = r["text"] or ""
            # Find which concepts matched in this result
            r_matched = [c for c in concept_names if c in r_text]
            score = _score_result(r_text, top_terms, concept_names,
                                  r["subject"] == q_subject)
            # Skip results below minimum quality threshold
            if score < 15:
                continue
            item = {
                "id": r["id"],
                "subject": r["subject"],
                "title": r["title"],
                "book_key": r["book_key"],
                "section": r["section"],
                "snippet": r["snippet"],
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

        return {
            "question_id": question_id,
            "question_title": q_row["title"],
            "question_subject": q_subject,
            "search_terms": top_terms[:10],
            "matched_concepts": [
                {"concept": c["concept"], "is_cross": c["is_cross"],
                 "subjects": list(c["subjects"].keys())}
                for c in matched_concepts[:10]
            ],
            "expanded_terms": expanded_terms[:8],
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
    """Rank curated concepts by cross-subject breadth."""
    con = get_db()
    try:
        rows = con.execute("""
            SELECT ck.term, ck.subject_count, ck.total_count
            FROM curated_keywords ck
            ORDER BY ck.subject_count DESC, ck.total_count DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return {
            "concepts": [
                {"term": r["term"], "subjects": r["subject_count"], "count": r["total_count"]}
                for r in rows
            ]
        }
    finally:
        con.close()


@app.get("/api/graph/search")
def graph_search(q: str = Query(..., min_length=1)):
    """Return a concept subgraph centered on the search term."""
    con = get_db()
    try:
        q_clean = q.strip()
        curated = {r["term"] for r in con.execute("SELECT term FROM curated_keywords").fetchall()}

        # Find the search term's subject distribution
        center_dist = con.execute("""
            SELECT subject, COUNT(*) as cnt FROM chunks
            WHERE text LIKE ? GROUP BY subject ORDER BY cnt DESC
        """, [f"%{q_clean}%"]).fetchall()

        if not center_dist:
            return {"center": q_clean, "nodes": [], "links": []}

        center_subjects = {r["subject"] for r in center_dist}

        # Find related curated concepts that share subjects with the search term
        related = []
        for term in curated:
            if term == q_clean:
                continue
            term_subjects_row = con.execute("""
                SELECT DISTINCT subject FROM concept_map WHERE concept = ?
            """, (term,)).fetchall()
            term_subjects = {r["subject"] for r in term_subjects_row}
            overlap = center_subjects & term_subjects
            if len(overlap) >= 1:
                related.append({
                    "term": term,
                    "shared_subjects": list(overlap),
                    "overlap": len(overlap),
                })

        related.sort(key=lambda x: x["overlap"], reverse=True)
        related = related[:20]  # Top 20 related concepts

        # Build nodes and links
        nodes = [{"id": q_clean, "type": "center", "subjects": [r["subject"] for r in center_dist]}]
        links = []

        for r in related:
            nodes.append({"id": r["term"], "type": "related", "overlap": r["overlap"]})
            for s in r["shared_subjects"]:
                links.append({"source": q_clean, "target": r["term"], "subject": s})

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
            # Per-subject mode: show concepts within one subject, linked by co-occurrence
            rows = con.execute("""
                SELECT concept, count FROM concept_map
                WHERE subject = ? ORDER BY count DESC LIMIT ?
            """, (subject, limit)).fetchall()
            concepts = [{"term": r["concept"], "count": r["count"]} for r in rows]
            for c in concepts:
                nodes.append({"id": c["term"], "type": "concept", "weight": c["count"]})

            # Link concepts that co-occur in the same chunk
            terms = [c["term"] for c in concepts]
            for i, t1 in enumerate(terms[:30]):  # limit link computation
                for t2 in terms[i+1:30]:
                    co = con.execute("""
                        SELECT COUNT(*) as cnt FROM chunks
                        WHERE subject = ? AND text LIKE ? AND text LIKE ?
                    """, (subject, f"%{t1}%", f"%{t2}%")).fetchone()
                    if co and co["cnt"] >= 2:
                        links.append({"source": t1, "target": t2, "weight": co["cnt"]})

        else:
            # Cross-subject mode: show concepts spanning multiple subjects
            rows = con.execute("""
                SELECT concept, COUNT(DISTINCT subject) as subj_count, SUM(count) as total
                FROM concept_map GROUP BY concept
                HAVING subj_count >= 2
                ORDER BY subj_count DESC, total DESC LIMIT ?
            """, (limit,)).fetchall()
            concepts = [{"term": r["concept"], "subjects": r["subj_count"], "total": r["total"]} for r in rows]

            # Add concept nodes
            for c in concepts:
                subjs = con.execute(
                    "SELECT DISTINCT subject FROM concept_map WHERE concept = ?", (c["term"],)
                ).fetchall()
                nodes.append({
                    "id": c["term"], "type": "concept",
                    "weight": c["subjects"], "total": c["total"],
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

            # Links: concept -> subject
            for n in nodes:
                if n["type"] == "concept":
                    for s in n.get("subjects", []):
                        links.append({"source": n["id"], "target": s})

        # Get available subjects for mode selector
        subjects = [r["subject"] for r in con.execute(
            "SELECT DISTINCT subject FROM concept_map ORDER BY subject"
        ).fetchall()]

        return {"mode": mode, "subject": subject, "nodes": nodes, "links": links, "subjects": subjects}
    finally:
        con.close()


# Images served from Cloudflare R2 CDN
IMG_CDN = os.getenv("IMG_CDN", "https://img.rdfzer.com")

# Serve frontend
if FRONTEND.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (FRONTEND / "index.html").read_text(encoding="utf-8")
