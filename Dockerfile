FROM python:3.13-slim

WORKDIR /app

# Install system deps for faiss
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps: web framework + AI/NLP stack
RUN pip install --no-cache-dir \
    fastapi uvicorn \
    faiss-cpu \
    sentence-transformers \
    jieba

# Copy app
COPY backend/ backend/
COPY frontend/ frontend/

# Copy database + FAISS vector index
RUN mkdir -p /app/data/index
COPY data/textbook_mineru_fts.db /app/data/index/textbook_mineru_fts.db
COPY data/textbook_chunks.index /app/data/index/textbook_chunks.index

# Images served from Cloudflare R2 CDN (img.rdfzer.com)
# No longer baked into Docker image

ENV DATA_ROOT=/app
ENV PORT=8080
# Pre-download the BGE model at build time so startup is fast
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"

EXPOSE 8080

# Single worker to keep memory usage low with FAISS loaded
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
