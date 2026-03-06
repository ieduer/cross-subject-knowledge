#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="${RUNTIME_ROOT:-/root/cross-subject-knowledge}"
CONTAINER_NAME="textbook-knowledge"
REPO_NAME="textbook-knowledge"
EMBEDDER_NAME="${EMBEDDER_NAME:-BAAI/bge-m3}"
HOST_HF_CACHE_ROOT="${RUNTIME_ROOT}/state/cache/huggingface"
HOST_HF_HUB_CACHE="${HOST_HF_CACHE_ROOT}/hub"
HOST_LEGACY_ST_CACHE="${RUNTIME_ROOT}/state/cache/sentence_transformers"
CONTAINER_HF_CACHE_ROOT="/state/cache/huggingface"
CONTAINER_HF_HUB_CACHE="/state/cache/huggingface/hub"
EMBEDDER_CACHE_KEY="models--${EMBEDDER_NAME//\//--}"

cd "$SOURCE_ROOT"

mkdir -p \
  "${RUNTIME_ROOT}/data/index" \
  "${RUNTIME_ROOT}/state/logs" \
  "${HOST_HF_HUB_CACHE}" \
  "${HOST_LEGACY_ST_CACHE}" \
  "${RUNTIME_ROOT}/state/tmp" \
  "${RUNTIME_ROOT}/state/batch"

if [ ! -f "${RUNTIME_ROOT}/data/index/textbook_mineru_fts.db" ]; then
  echo "ERROR: missing runtime DB ${RUNTIME_ROOT}/data/index/textbook_mineru_fts.db"
  exit 1
fi

if [ ! -f "${RUNTIME_ROOT}/data/index/textbook_chunks.index" ]; then
  echo "ERROR: missing runtime FAISS index ${RUNTIME_ROOT}/data/index/textbook_chunks.index"
  exit 1
fi

commit_sha="$(git rev-parse --short HEAD)"
build_stamp="$(date +%Y%m%d_%H%M%S)"
build_tag="${REPO_NAME}:build-${commit_sha}-${build_stamp}"
backup_tag="${REPO_NAME}:pre-${build_stamp}"
rollback_image=""

host_cache_has_model() {
  find "${HOST_HF_HUB_CACHE}/${EMBEDDER_CACHE_KEY}" -type f | grep -q .
}

migrate_legacy_st_cache() {
  if [ -d "${HOST_LEGACY_ST_CACHE}/${EMBEDDER_CACHE_KEY}" ] && [ ! -e "${HOST_HF_HUB_CACHE}/${EMBEDDER_CACHE_KEY}" ]; then
    echo "=== migrating legacy sentence-transformers cache into shared HF hub cache ==="
    mv "${HOST_LEGACY_ST_CACHE}/${EMBEDDER_CACHE_KEY}" "${HOST_HF_HUB_CACHE}/"
  fi
}

prune_legacy_st_cache() {
  if [ -d "${HOST_LEGACY_ST_CACHE}/${EMBEDDER_CACHE_KEY}" ] && host_cache_has_model; then
    echo "=== pruning duplicate legacy sentence-transformers cache ==="
    rm -rf "${HOST_LEGACY_ST_CACHE:?}/${EMBEDDER_CACHE_KEY}"
  fi

  if [ -d "${HOST_LEGACY_ST_CACHE}/.locks" ]; then
    find "${HOST_LEGACY_ST_CACHE}/.locks" -type f -delete || true
  fi
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

if ! host_cache_has_model; then
  echo "=== bootstrapping host model cache ==="
  if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
    docker cp "${CONTAINER_NAME}:/root/.cache/huggingface/." "${HOST_HF_CACHE_ROOT}/" >/dev/null 2>&1 || true
  fi
fi

if ! host_cache_has_model; then
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

docker stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true

docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  -p 8080:8080 \
  -e PROJECT_ROOT=/app \
  -e DATA_ROOT=/data \
  -e STATE_ROOT=/state \
  -e HF_HOME="${CONTAINER_HF_CACHE_ROOT}" \
  -e HF_HUB_CACHE="${CONTAINER_HF_HUB_CACHE}" \
  -e SENTENCE_TRANSFORMERS_HOME="${CONTAINER_HF_HUB_CACHE}" \
  -e TRANSFORMERS_CACHE="${CONTAINER_HF_HUB_CACHE}" \
  -v "${RUNTIME_ROOT}/data:/data" \
  -v "${RUNTIME_ROOT}/state:/state" \
  "${build_tag}" >/dev/null

healthy="false"
for _ in $(seq 1 24); do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${CONTAINER_NAME}" 2>/dev/null || echo "missing")"
  if [ "${status}" = "healthy" ] || [ "${status}" = "running" ]; then
    if curl -sf http://127.0.0.1:8080/api/health >/tmp/textbook_health.json; then
      healthy="true"
      break
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
