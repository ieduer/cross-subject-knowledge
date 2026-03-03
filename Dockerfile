FROM python:3.13-slim

WORKDIR /app

# Install deps
RUN pip install --no-cache-dir fastapi uvicorn

# Copy app
COPY backend/ backend/
COPY frontend/ frontend/

# Copy database (will be mounted as volume in production)
# For initial deploy, bake it in
RUN mkdir -p /app/data/index
COPY data/textbook_mineru_fts.db /app/data/index/textbook_mineru_fts.db

# Images served from Cloudflare R2 CDN (img.rdfzer.com)
# No longer baked into Docker image

ENV DATA_ROOT=/app
ENV PORT=8080

EXPOSE 8080

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
