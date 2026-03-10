#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="${RUNTIME_ROOT:-/root/cross-subject-knowledge}"
CONTAINER_NAME="textbook-knowledge"
REPO_NAME="textbook-knowledge"
EMBEDDER_NAME="${EMBEDDER_NAME:-BAAI/bge-m3}"
RERANKER_NAME="${RERANKER_NAME:-BAAI/bge-reranker-base}"
RERANKER_ENABLED="${RERANKER_ENABLED:-1}"
HOST_HF_CACHE_ROOT="${RUNTIME_ROOT}/state/cache/huggingface"
HOST_HF_HUB_CACHE="${HOST_HF_CACHE_ROOT}/hub"
HOST_LEGACY_ST_CACHE="${RUNTIME_ROOT}/state/cache/sentence_transformers"
CONTAINER_HF_CACHE_ROOT="/state/cache/huggingface"
CONTAINER_HF_HUB_CACHE="/state/cache/huggingface/hub"
EMBEDDER_CACHE_KEY="models--${EMBEDDER_NAME//\//--}"
RERANKER_CACHE_KEY="models--${RERANKER_NAME//\//--}"
SUPPLEMENTAL_INDEX_SRC="${SOURCE_ROOT}/backend/supplemental_textbook_pages.jsonl.gz"
SUPPLEMENTAL_MANIFEST_SRC="${SOURCE_ROOT}/backend/supplemental_textbook_pages.manifest.json"
SUPPLEMENTAL_INDEX_DST="${RUNTIME_ROOT}/data/index/supplemental_textbook_pages.jsonl.gz"
SUPPLEMENTAL_MANIFEST_DST="${RUNTIME_ROOT}/data/index/supplemental_textbook_pages.manifest.json"

cd "$SOURCE_ROOT"

mkdir -p \
  "${RUNTIME_ROOT}/data/index" \
  "${RUNTIME_ROOT}/state/logs" \
  "${HOST_HF_HUB_CACHE}" \
  "${HOST_LEGACY_ST_CACHE}" \
  "${RUNTIME_ROOT}/state/tmp" \
  "${RUNTIME_ROOT}/state/batch"

if [ ! -f "${SUPPLEMENTAL_INDEX_SRC}" ]; then
  echo "ERROR: missing bundled supplemental index ${SUPPLEMENTAL_INDEX_SRC}"
  exit 1
fi

if [ ! -f "${SUPPLEMENTAL_MANIFEST_SRC}" ]; then
  echo "ERROR: missing bundled supplemental manifest ${SUPPLEMENTAL_MANIFEST_SRC}"
  exit 1
fi

if [ ! -f "${RUNTIME_ROOT}/data/index/textbook_mineru_fts.db" ]; then
  echo "ERROR: missing runtime DB ${RUNTIME_ROOT}/data/index/textbook_mineru_fts.db"
  exit 1
fi

if [ ! -f "${RUNTIME_ROOT}/data/index/textbook_chunks.index" ]; then
  echo "ERROR: missing runtime FAISS index ${RUNTIME_ROOT}/data/index/textbook_chunks.index"
  exit 1
fi

install -m 0644 "${SUPPLEMENTAL_INDEX_SRC}" "${SUPPLEMENTAL_INDEX_DST}"
install -m 0644 "${SUPPLEMENTAL_MANIFEST_SRC}" "${SUPPLEMENTAL_MANIFEST_DST}"

commit_sha="$(git rev-parse --short HEAD)"
build_stamp="$(date +%Y%m%d_%H%M%S)"
build_tag="${REPO_NAME}:build-${commit_sha}-${build_stamp}"
backup_tag="${REPO_NAME}:pre-${build_stamp}"
rollback_image=""

env_true() {
  case "${1:-}" in
    0|false|FALSE|False|no|NO|No) return 1 ;;
    *) return 0 ;;
  esac
}

host_cache_has_model() {
  local cache_key="${1:?cache_key_required}"
  find "${HOST_HF_HUB_CACHE}/${cache_key}" -type f | grep -q .
}

migrate_legacy_st_cache() {
  if [ -d "${HOST_LEGACY_ST_CACHE}/${EMBEDDER_CACHE_KEY}" ] && [ ! -e "${HOST_HF_HUB_CACHE}/${EMBEDDER_CACHE_KEY}" ]; then
    echo "=== migrating legacy sentence-transformers cache into shared HF hub cache ==="
    mv "${HOST_LEGACY_ST_CACHE}/${EMBEDDER_CACHE_KEY}" "${HOST_HF_HUB_CACHE}/"
  fi
}

prune_legacy_st_cache() {
  if [ -d "${HOST_LEGACY_ST_CACHE}/${EMBEDDER_CACHE_KEY}" ] && host_cache_has_model "${EMBEDDER_CACHE_KEY}"; then
    echo "=== pruning duplicate legacy sentence-transformers cache ==="
    rm -rf "${HOST_LEGACY_ST_CACHE:?}/${EMBEDDER_CACHE_KEY}"
  fi

  if [ -d "${HOST_LEGACY_ST_CACHE}/.locks" ]; then
    find "${HOST_LEGACY_ST_CACHE}/.locks" -type f -delete || true
  fi
}

health_json_ok() {
  python3 - <<'PY'
import json
import os
import sys

try:
    with open("/tmp/textbook_health.json", "r", encoding="utf-8") as fh:
        payload = json.load(fh)
except Exception:
    sys.exit(1)

supplemental = payload.get("supplemental") or {}
ok = (
    payload.get("status") == "ok"
    and bool((payload.get("db") or {}).get("ok"))
    and bool((payload.get("faiss") or {}).get("ok"))
    and bool((payload.get("model") or {}).get("ok"))
    and bool(supplemental.get("ok"))
)
if os.getenv("HEALTH_REQUIRE_RERANKER", "0").strip().lower() not in {"0", "false", "no"}:
    reranker = payload.get("reranker") or {}
    ok = ok and (not bool(reranker.get("enabled")) or bool(reranker.get("loaded")))
sys.exit(0 if ok else 1)
PY
}

warm_precision_agent() {
  curl -sf --max-time 120 \
    -H 'Content-Type: application/json' \
    -X POST \
    -d '{"query":"晶体的定义","user_message":"请只根据教材原文说明「晶体」的定义，并给出最相关出处。","history":[]}' \
    http://127.0.0.1:8080/api/chat/context >/tmp/textbook_precision_warmup.json
}

echo "=== deploy start $(date -u '+%Y-%m-%d %H:%M:%S UTC') commit=${commit_sha} ==="
df -h /

if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  rollback_image="$(docker inspect --format '{{.Image}}' "${CONTAINER_NAME}")"
fi

if docker image inspect "${REPO_NAME}:latest" >/dev/null 2>&1; then
  docker tag "${REPO_NAME}:latest" "${backup_tag}" || true
fi

export DOCKER_BUILDKIT=1
docker build --pull -t "${build_tag}" -t "${REPO_NAME}:latest" .

migrate_legacy_st_cache

if ! host_cache_has_model "${EMBEDDER_CACHE_KEY}"; then
  echo "=== bootstrapping host model cache ==="
  if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
    docker cp "${CONTAINER_NAME}:/root/.cache/huggingface/." "${HOST_HF_CACHE_ROOT}/" >/dev/null 2>&1 || true
  fi
fi

if ! host_cache_has_model "${EMBEDDER_CACHE_KEY}"; then
  echo "=== warming model cache with ${EMBEDDER_NAME} ==="
  docker run --rm \
    -e HF_HOME="${CONTAINER_HF_CACHE_ROOT}" \
    -e HF_HUB_CACHE="${CONTAINER_HF_HUB_CACHE}" \
    -e SENTENCE_TRANSFORMERS_HOME="${CONTAINER_HF_HUB_CACHE}" \
    -e TRANSFORMERS_CACHE="${CONTAINER_HF_HUB_CACHE}" \
    -v "${RUNTIME_ROOT}/state:/state" \
    "${build_tag}" \
    python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDER_NAME}')"
fi

if env_true "${RERANKER_ENABLED}" && ! host_cache_has_model "${RERANKER_CACHE_KEY}"; then
  echo "=== warming reranker cache with ${RERANKER_NAME} ==="
  docker run --rm \
    -e HF_HOME="${CONTAINER_HF_CACHE_ROOT}" \
    -e HF_HUB_CACHE="${CONTAINER_HF_HUB_CACHE}" \
    -e SENTENCE_TRANSFORMERS_HOME="${CONTAINER_HF_HUB_CACHE}" \
    -e TRANSFORMERS_CACHE="${CONTAINER_HF_HUB_CACHE}" \
    -v "${RUNTIME_ROOT}/state:/state" \
    "${build_tag}" \
    python -c "from sentence_transformers import CrossEncoder; CrossEncoder('${RERANKER_NAME}')"
fi

docker stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true

docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  -p 8080:8080 \
  -e PROJECT_ROOT=/app \
  -e DATA_ROOT=/data \
  -e STATE_ROOT=/state \
  -e EMBEDDER="${EMBEDDER_NAME}" \
  -e RERANKER="${RERANKER_NAME}" \
  -e RERANKER_ENABLED="${RERANKER_ENABLED}" \
  -e RERANKER_PRELOAD=1 \
  -e HF_HOME="${CONTAINER_HF_CACHE_ROOT}" \
  -e HF_HUB_CACHE="${CONTAINER_HF_HUB_CACHE}" \
  -e SENTENCE_TRANSFORMERS_HOME="${CONTAINER_HF_HUB_CACHE}" \
  -e TRANSFORMERS_CACHE="${CONTAINER_HF_HUB_CACHE}" \
  -v "${RUNTIME_ROOT}/data:/data" \
  -v "${RUNTIME_ROOT}/state:/state" \
  "${build_tag}" >/dev/null

healthy="false"
warmed_precision="false"
for _ in $(seq 1 24); do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${CONTAINER_NAME}" 2>/dev/null || echo "missing")"
  if [ "${status}" = "healthy" ] || [ "${status}" = "running" ]; then
    if env_true "${RERANKER_ENABLED}" && [ "${warmed_precision}" != "true" ]; then
      if warm_precision_agent; then
        warmed_precision="true"
      fi
    fi
    if curl -sf http://127.0.0.1:8080/api/health >/tmp/textbook_health.json; then
      if env_true "${RERANKER_ENABLED}"; then
        if [ "${warmed_precision}" = "true" ] && HEALTH_REQUIRE_RERANKER=1 health_json_ok; then
          healthy="true"
          break
        fi
      elif health_json_ok; then
        healthy="true"
        break
      fi
    fi
  fi
  sleep 5
done

if [ "${healthy}" != "true" ]; then
  echo "ERROR: new container failed health checks"
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  docker stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  if [ -n "${rollback_image}" ]; then
    echo "=== rolling back to previous image ${rollback_image} ==="
    docker run -d \
      --name "${CONTAINER_NAME}" \
      --restart unless-stopped \
      -p 8080:8080 \
      -e PROJECT_ROOT=/app \
      -e DATA_ROOT=/data \
      -e STATE_ROOT=/state \
      -e EMBEDDER="${EMBEDDER_NAME}" \
      -e RERANKER="${RERANKER_NAME}" \
      -e RERANKER_ENABLED="${RERANKER_ENABLED}" \
      -e RERANKER_PRELOAD=1 \
      -e HF_HOME="${CONTAINER_HF_CACHE_ROOT}" \
      -e HF_HUB_CACHE="${CONTAINER_HF_HUB_CACHE}" \
      -e SENTENCE_TRANSFORMERS_HOME="${CONTAINER_HF_HUB_CACHE}" \
      -e TRANSFORMERS_CACHE="${CONTAINER_HF_HUB_CACHE}" \
      -v "${RUNTIME_ROOT}/data:/data" \
      -v "${RUNTIME_ROOT}/state:/state" \
      "${rollback_image}" >/dev/null
  fi
  exit 1
fi

echo "=== health ==="
cat /tmp/textbook_health.json
echo

prune_legacy_st_cache

docker image prune -f >/dev/null 2>&1 || true

old_pre_tags="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^textbook-knowledge:pre-' | sort -r | tail -n +4 || true)"
if [ -n "${old_pre_tags}" ]; then
  while IFS= read -r image_ref; do
    [ -n "${image_ref}" ] || continue
    docker rmi "${image_ref}" >/dev/null 2>&1 || true
  done <<< "${old_pre_tags}"
fi

old_build_tags="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^textbook-knowledge:build-' | grep -v "^${build_tag}$" || true)"
if [ -n "${old_build_tags}" ]; then
  while IFS= read -r image_ref; do
    [ -n "${image_ref}" ] || continue
    docker rmi "${image_ref}" >/dev/null 2>&1 || true
  done <<< "${old_build_tags}"
fi

echo "=== disk after deploy ==="
df -h /
docker system df
echo "=== deploy ok $(date -u '+%Y-%m-%d %H:%M:%S UTC') tag=${build_tag} ==="
