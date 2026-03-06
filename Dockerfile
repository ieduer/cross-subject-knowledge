FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PROJECT_ROOT=/app \
    DATA_ROOT=/data \
    STATE_ROOT=/state \
    PORT=8080 \
    HF_HOME=/state/cache/huggingface \
    HF_HUB_CACHE=/state/cache/huggingface/hub \
    SENTENCE_TRANSFORMERS_HOME=/state/cache/huggingface/hub \
    TRANSFORMERS_CACHE=/state/cache/huggingface/hub

COPY requirements.runtime.txt ./

# Force CPU-only torch wheels on Linux x86 to avoid pulling multi-GB CUDA deps
# into a no-GPU production image.
RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch==2.10.0+cpu" && \
    python -m pip install --no-cache-dir -r requirements.runtime.txt

COPY backend/ backend/
COPY frontend/ frontend/

# Runtime-mounted data/state directories
RUN mkdir -p \
    /data/index \
    /state/logs \
    /state/cache/huggingface/hub \
    /state/tmp \
    /state/batch

# Images served from Cloudflare R2 CDN (img.rdfzer.com)
# No longer baked into Docker image.

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=5)" || exit 1

CMD ["sh", "/app/backend/entrypoint.sh"]
