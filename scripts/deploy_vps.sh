#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="${RUNTIME_ROOT:-/root/cross-subject-knowledge}"
CONTAINER_NAME="textbook-knowledge"
REPO_NAME="textbook-knowledge"
EMBEDDER_NAME="${EMBEDDER_NAME:-BAAI/bge-m3}"
RERANKER_NAME="${RERANKER_NAME:-BAAI/bge-reranker-base}"
RERANKER_ENABLED="${RERANKER_ENABLED:-1}"
RUNTIME_DB_SYNC_MODE="${RUNTIME_DB_SYNC_MODE:-disabled}"
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
SUPPLEMENTAL_VECTOR_INDEX_SRC="${RUNTIME_ROOT}/data/index/supplemental_textbook_pages.index"
SUPPLEMENTAL_VECTOR_MANIFEST_SRC="${RUNTIME_ROOT}/data/index/supplemental_textbook_pages.vector.manifest.json"
SUPPLEMENTAL_VECTOR_BUNDLED="0"
if [ -f "${SOURCE_ROOT}/backend/supplemental_textbook_pages.index" ]; then
  SUPPLEMENTAL_VECTOR_INDEX_SRC="${SOURCE_ROOT}/backend/supplemental_textbook_pages.index"
fi
if [ -f "${SOURCE_ROOT}/backend/supplemental_textbook_pages.vector.manifest.json" ]; then
  SUPPLEMENTAL_VECTOR_MANIFEST_SRC="${SOURCE_ROOT}/backend/supplemental_textbook_pages.vector.manifest.json"
fi
SUPPLEMENTAL_VECTOR_INDEX_DST="${RUNTIME_ROOT}/data/index/supplemental_textbook_pages.index"
SUPPLEMENTAL_VECTOR_MANIFEST_DST="${RUNTIME_ROOT}/data/index/supplemental_textbook_pages.vector.manifest.json"
BOOK_MAP_SRC="${SOURCE_ROOT}/frontend/assets/pages/book_map.json"
FRONTEND_VERSION_SRC="${SOURCE_ROOT}/frontend/assets/version.json"
RELEASE_MANIFEST_PATH="${SOURCE_ROOT}/release_manifest.json"

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

if [ ! -s "${BOOK_MAP_SRC}" ]; then
  echo "ERROR: missing or empty frontend page map ${BOOK_MAP_SRC}"
  exit 1
fi

if [ ! -s "${FRONTEND_VERSION_SRC}" ]; then
  echo "ERROR: missing or empty frontend version ledger ${FRONTEND_VERSION_SRC}"
  exit 1
fi

if [ ! -s "${RELEASE_MANIFEST_PATH}" ]; then
  echo "ERROR: missing or empty release manifest ${RELEASE_MANIFEST_PATH}"
  exit 1
fi

if [ "${RUNTIME_DB_SYNC_MODE}" != "disabled" ]; then
  echo "ERROR: deploy_vps.sh requires RUNTIME_DB_SYNC_MODE=disabled"
  echo "Sync runtime DB assets to ${RUNTIME_ROOT}/data/index explicitly before deploy."
  exit 1
fi

# ── Fail-fast: textbook_config.py consistency ──────────────────────────
WORKSPACE_ROOT="$(dirname "${SOURCE_ROOT}")"
if [ -f "${WORKSPACE_ROOT}/scripts/textbook_config.py" ]; then
  if ! diff --quiet "${WORKSPACE_ROOT}/scripts/textbook_config.py" "${SOURCE_ROOT}/backend/textbook_config.py" 2>/dev/null; then
    echo "ERROR: platform/backend/textbook_config.py is out of sync with scripts/textbook_config.py"
    echo "Run: scripts/sync_shared_config.sh"
    exit 1
  fi
fi

install_if_distinct() {
  local src="${1:?src_required}"
  local dst="${2:?dst_required}"
  if [ -e "${src}" ] && [ -e "${dst}" ] && [ "$(readlink -f "${src}")" = "$(readlink -f "${dst}")" ]; then
    return 0
  fi
  install -m 0644 "${src}" "${dst}"
}

install_if_distinct "${SUPPLEMENTAL_INDEX_SRC}" "${SUPPLEMENTAL_INDEX_DST}"
install_if_distinct "${SUPPLEMENTAL_MANIFEST_SRC}" "${SUPPLEMENTAL_MANIFEST_DST}"
if [ -f "${SUPPLEMENTAL_VECTOR_INDEX_SRC}" ] && [ -f "${SUPPLEMENTAL_VECTOR_MANIFEST_SRC}" ]; then
  install_if_distinct "${SUPPLEMENTAL_VECTOR_INDEX_SRC}" "${SUPPLEMENTAL_VECTOR_INDEX_DST}"
  install_if_distinct "${SUPPLEMENTAL_VECTOR_MANIFEST_SRC}" "${SUPPLEMENTAL_VECTOR_MANIFEST_DST}"
  SUPPLEMENTAL_VECTOR_BUNDLED="1"
fi

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

verify_built_image_release_assets() {
  docker run --rm --entrypoint sh "${build_tag}" -lc '
    test -s /app/frontend/assets/pages/book_map.json &&
    test -s /app/frontend/assets/version.json &&
    test -s /app/frontend/index.html &&
    test -s /app/frontend/dict.html &&
    test -s /app/frontend/chuzhong.html &&
    test -s /app/frontend/chuzhong-dict.html
  ' >/dev/null
}

verify_release_manifest_alignment() {
  python3 "${SOURCE_ROOT}/scripts/verify_release_manifest.py" \
    --manifest "${RELEASE_MANIFEST_PATH}" \
    --source-root "${SOURCE_ROOT}" \
    --runtime-root "${RUNTIME_ROOT}" \
    --check source \
    --check runtime
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
if os.getenv("HEALTH_REQUIRE_SUPPLEMENTAL_VECTOR", "0").strip().lower() not in {"0", "false", "no"}:
    supplemental_vectors = payload.get("supplemental_vectors") or {}
    ok = ok and (not bool(supplemental_vectors.get("enabled")) or bool(supplemental_vectors.get("loaded")))
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

if ! verify_release_manifest_alignment; then
  echo "ERROR: release manifest alignment check failed"
  exit 1
fi

# ── Fail-fast: git HEAD must match release manifest ──────────────────────────
manifest_git_head="$(python3 -c "import json;print(json.load(open('${RELEASE_MANIFEST_PATH}'))['git_head'])" 2>/dev/null || echo "")"
if [ -n "${manifest_git_head}" ]; then
  actual_head="$(git rev-parse HEAD)"
  if [ "${manifest_git_head}" != "${actual_head}" ]; then
    echo "ERROR: git HEAD (${actual_head}) does not match release manifest git_head (${manifest_git_head})"
    echo "Rebuild release manifest or checkout the correct commit."
    exit 1
  fi
fi

if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  rollback_image="$(docker inspect --format '{{.Image}}' "${CONTAINER_NAME}")"
fi

if docker image inspect "${REPO_NAME}:latest" >/dev/null 2>&1; then
  docker tag "${REPO_NAME}:latest" "${backup_tag}" || true
fi

export DOCKER_BUILDKIT=1
docker build --pull -t "${build_tag}" -t "${REPO_NAME}:latest" .

if ! verify_built_image_release_assets; then
  echo "ERROR: built image is missing required frontend release assets"
  exit 1
fi

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
  -e RUNTIME_DB_SYNC_MODE="${RUNTIME_DB_SYNC_MODE}" \
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
        if [ "${warmed_precision}" = "true" ] && HEALTH_REQUIRE_RERANKER=1 HEALTH_REQUIRE_SUPPLEMENTAL_VECTOR="${SUPPLEMENTAL_VECTOR_BUNDLED}" health_json_ok; then
          healthy="true"
          break
        fi
      elif HEALTH_REQUIRE_SUPPLEMENTAL_VECTOR="${SUPPLEMENTAL_VECTOR_BUNDLED}" health_json_ok; then
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
      -e RUNTIME_DB_SYNC_MODE="${RUNTIME_DB_SYNC_MODE}" \
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

# ── Post-deploy: verify served frontend version matches expectation ──────────
expected_version="$(python3 -c "import json;print(json.load(open('${FRONTEND_VERSION_SRC}'))['frontend_refactor_version'])" 2>/dev/null || echo "")"
if [ -n "${expected_version}" ]; then
  served_version="$(curl -sf http://127.0.0.1:8080/assets/version.json 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)['frontend_refactor_version'])" 2>/dev/null || echo "")"
  if [ -n "${served_version}" ] && [ "${expected_version}" != "${served_version}" ]; then
    echo "WARNING: served frontend version (${served_version}) != expected (${expected_version})"
    echo "This may indicate stale Docker cache or incomplete build."
  elif [ -n "${served_version}" ]; then
    echo "=== frontend version verified: ${served_version} ==="
  fi
fi

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
