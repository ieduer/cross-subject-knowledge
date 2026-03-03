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

        # Order clause
        order_clause = "ORDER BY rank"
        if sort == "images":
            order_clause = "ORDER BY (LENGTH(c.text) - LENGTH(REPLACE(c.text, '![', ''))) DESC, rank"

        params.extend([limit, offset])

        rows = con.execute(f"""
            SELECT c.id, c.subject, c.title, c.book_key, c.section,
                   snippet(chunks_fts, 0, '<mark>', '</mark>', '…', 40) as snippet,
                   c.text
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
            by_subject[s]["results"].append({
                "id": r["id"],
                "title": r["title"],
                "book_key": r["book_key"],
                "section": r["section"],
                "snippet": r["snippet"],
                "text": text[:2000],
                "image_count": img_count,
            })
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
        dist = con.execute(
            "SELECT subject, COUNT(*) as cnt FROM chunks GROUP BY subject ORDER BY cnt DESC"
        ).fetchall()
        return {
            "total_chunks": total,
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


# Images served from Cloudflare R2 CDN
IMG_CDN = os.getenv("IMG_CDN", "https://img.rdfzer.com")

# Serve frontend
if FRONTEND.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (FRONTEND / "index.html").read_text(encoding="utf-8")
