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
    jieba \
    cachetools

# Copy app
COPY backend/ backend/
COPY frontend/ frontend/

# Runtime-mounted data/state directories
RUN mkdir -p /data/index /state/logs /state/cache /state/tmp /state/batch

# Images served from Cloudflare R2 CDN (img.rdfzer.com)
# No longer baked into Docker image

ENV PROJECT_ROOT=/app
ENV DATA_ROOT=/data
ENV STATE_ROOT=/state
ENV PORT=8080
# Pre-download the embedding model at build time so startup is fast
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"

EXPOSE 8080

# Health check: auto-restart if backend is unresponsive
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

CMD ["sh", "/app/backend/entrypoint.sh"]
