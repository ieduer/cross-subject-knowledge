"""
跨学科教材知识平台 · FastAPI 后端
"""
import sqlite3, json, os, re
from collections import Counter
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# ── paths ─────────────────────────────────────────────────────────────
ROOT = Path(os.getenv("DATA_ROOT", "/home/suen/.openclaw/workspace/textbook_ai"))
DB_PATH = ROOT / "data/index/textbook_mineru_fts.db"
FRONTEND = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="跨学科教材知识平台", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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

        # Build WHERE filters
        params = [clean_q]
        where_extra = ""
        if subject:
            where_extra += " AND c.subject = ?"
            params.append(subject)
        if book_key:
            where_extra += " AND c.book_key = ?"
            params.append(book_key)
        if source == 'textbook':
            where_extra += " AND c.source = 'mineru'"
        elif source == 'gaokao':
            where_extra += " AND c.source = 'gaokao'"

        # Order clause
        order_clause = "ORDER BY rank"
        if sort == "images":
            order_clause = "ORDER BY (LENGTH(c.text) - LENGTH(REPLACE(c.text, '![', ''))) DESC, rank"

        params.extend([limit, offset])

        rows = con.execute(f"""
            SELECT c.id, c.subject, c.title, c.book_key, c.section,
                   snippet(chunks_fts, 0, '<mark>', '</mark>', '…', 40) as snippet,
                   c.text, c.source, c.year, c.category
            FROM chunks c
            JOIN chunks_fts f ON c.id = f.rowid
            WHERE chunks_fts MATCH ? {where_extra}
            {order_clause}
            LIMIT ? OFFSET ?
        """, params).fetchall()

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

        # Get total counts per subject
        count_params = [clean_q]
        count_where = ""
        if book_key:
            count_where += " AND c.book_key = ?"
            count_params.append(book_key)
        count_rows = con.execute(f"""
            SELECT c.subject, COUNT(*) as cnt
            FROM chunks c
            JOIN chunks_fts f ON c.id = f.rowid
            WHERE chunks_fts MATCH ? {count_where}
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

        return {
            "query": q,
            "total": total,
            "subject_counts": subject_counts,
            "cross_hint": hint,
            "groups": groups,
        }
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


@app.get("/api/cross-links")
def cross_links():
    """Pre-computed cross-subject concept links for the graph."""
    con = get_db()
    try:
        concepts = [
            "蛋白质", "DNA", "电子", "光", "溶液", "细胞", "向量", "函数",
            "温室效应", "碳循环", "土壤", "生态系统", "能量", "平衡",
            "丝绸之路", "改革", "民主", "人口", "概率", "氧化",
            "光合作用", "进化", "水循环", "正弦", "坐标", "统计",
            "可持续发展", "原子结构", "全球化", "战争", "电", "市场",
        ]
        nodes = []
        links = []
        for concept in concepts:
            try:
                rows = con.execute("""
                    SELECT c.subject, COUNT(*) as cnt
                    FROM chunks c JOIN chunks_fts f ON c.id = f.rowid
                    WHERE chunks_fts MATCH ?
                    GROUP BY c.subject ORDER BY cnt DESC
                """, [concept]).fetchall()
            except:
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

        # Subject nodes
        subject_nodes = [
            {"id": s, "type": "subject", **SUBJECT_META.get(s, {"icon": "📚", "color": "#95a5a6"})}
            for s in SUBJECT_META
        ]
        return {
            "concept_nodes": nodes,
            "subject_nodes": subject_nodes,
            "links": links,
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


@app.get("/api/gaokao/link")
def gaokao_link(
    question_id: int = Query(..., description="ID of the gaokao question"),
    limit: int = Query(10, ge=1, le=30),
):
    """Find textbook chunks most related to a gaokao question via FTS."""
    con = get_db()
    try:
        # Get the question
        q_row = con.execute(
            "SELECT * FROM chunks WHERE id = ? AND source = 'gaokao'",
            [question_id]
        ).fetchone()
        if not q_row:
            raise HTTPException(404, "Question not found")

        # Extract key terms from question text for FTS search
        text = q_row["text"] or ""
        q_subject = q_row["subject"]
        # Extract Chinese terms (2-4 chars) for search
        terms = re.findall(r'[\u4e00-\u9fff]{2,4}', text)
        if not terms:
            return {"question_id": question_id, "links": [], "cross_links": []}

        # Use the most frequent meaningful terms
        from collections import Counter as Ctr
        term_counts = Ctr(terms)
        # Expanded stop words: exam boilerplate + generic terms
        stop_words = {
            # Exam boilerplate
            '选择', '问题', '下列', '以下', '关于', '其中', '正确', '错误',
            '不正确', '说法', '叙述', '表述', '选项', '答案', '分析', '解答',
            '已知', '求解', '设有', '如图', '所示', '可以', '可能', '区域',
            '不能', '属于', '不属于', '一定', '不一定', '解答', '详解',
            '根据', '由此', '可知', '因此', '所以', '由于', '如果', '那么',
            '题目', '材料', '文中', '图中', '表中', '实验', '方案', '含量',
            '条件', '下面', '上面', '哪个', '哪些', '什么', '为什么',
            '判断', '推断', '分别', '同时', '以及', '或者', '而且',
            # Generic verbs/adj
            '进行', '使用', '利用', '通过', '发生', '产生', '得到', '变化',
            '增大', '减小', '增加', '减少', '提高', '降低', '保持', '影响',
            '表示', '反映', '说明', '体现', '指出', '认为', '表明',
            '主要', '一般', '通常', '特别', '特殊', '基本', '重要',
            '正确', '合理', '适当', '必要', '需要', '应该', '能够',
            # Numbers/units
            '过程', '结果', '作用', '功能', '特点', '特征', '方法',
            '大小', '多少', '高低', '长短', '快慢', '强弱',
        }
        top_terms = [t for t, _ in term_counts.most_common(20)
                     if t not in stop_words and len(t) >= 2]
        if not top_terms:
            return {"question_id": question_id, "links": [], "cross_links": []}

        # Search textbook chunks — get more results to split
        search_q = ' OR '.join(top_terms[:10])
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
            """, [search_q, limit * 2]).fetchall()
        except Exception:
            return {"question_id": question_id, "links": [], "cross_links": []}

        # Split into same-subject and cross-subject
        same_subject = []
        cross_subject = []
        for r in rows:
            item = {
                "id": r["id"],
                "subject": r["subject"],
                "title": r["title"],
                "book_key": r["book_key"],
                "section": r["section"],
                "snippet": r["snippet"],
                "text": (r["text"] or "")[:1500],
                "link_type": "same" if r["subject"] == q_subject else "cross",
                **SUBJECT_META.get(r["subject"], {"icon": "📚", "color": "#95a5a6"}),
            }
            if r["subject"] == q_subject:
                if len(same_subject) < limit:
                    same_subject.append(item)
            else:
                if len(cross_subject) < limit:
                    cross_subject.append(item)

        return {
            "question_id": question_id,
            "question_title": q_row["title"],
            "question_subject": q_subject,
            "search_terms": top_terms[:10],
            "links": same_subject,
            "cross_links": cross_subject,
        }
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
