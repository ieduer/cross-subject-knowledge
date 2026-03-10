# Runtime And Operations Overview

This document records the current runtime, data, and deployment boundaries of the textbook project.

Before any data rebuild, deploy, rollback, or search-debugging pass, read [data_layer_lineage_memory.md](./data_layer_lineage_memory.md) first. That file is the canonical long-term memory for data lineage, count meanings, runtime asset boundaries, and release rules.

## Machine roles

- Local development machine: code editing, data inspection, lightweight validation
- Offline processing/build machine: OCR, FAISS rebuild, batch backfill, and large model downloads
- Production VPS: serves search, graph, gaokao linkage, and AI chat APIs; does not run OCR or FAISS rebuild
- Cloudflare R2: image and page asset delivery
- Cloudflare Worker custom domain `ai.bdfz.net`: external AI gateway bound to Worker service `apis` / `production`

## Runtime assets

Production runtime depends on host-mounted directories instead of baking data into the image:

- `/root/cross-subject-knowledge/data/index/textbook_mineru_fts.db`
- `/root/cross-subject-knowledge/data/index/textbook_chunks.index`
- `/root/cross-subject-knowledge/data/index/textbook_chunks.manifest.json`
- `/root/cross-subject-knowledge/data/index/supplemental_textbook_pages.jsonl.gz`
- `/root/cross-subject-knowledge/data/index/supplemental_textbook_pages.manifest.json`
- `/root/cross-subject-knowledge/data/index/supplemental_textbook_pages.index`
- `/root/cross-subject-knowledge/data/index/supplemental_textbook_pages.vector.manifest.json`
- `/root/cross-subject-knowledge/state/cache/huggingface/hub`
- `/root/cross-subject-knowledge/state/logs`
- `/root/cross-subject-knowledge/state/batch`

The repository also keeps bundled fallback copies of the supplemental index under `backend/`, but production should rely on the synced `data/index/` copies and their manifest.

## Current runtime profile

As of 2026-03-10:

- DB chunks: `21925`
- Textbook chunks: `17896`
- Gaokao chunks: `4029`
- FAISS vectors: `17896`
- Searchable textbooks: `193` (`69` primary books + `124` supplemental-only visible books)
- Supplemental textbook pages: `22844` from `251` indexed OCR source files
- Supplemental vectors: `22844`
- Embedder: `BAAI/bge-m3`
- Reranker: `BAAI/bge-reranker-base`
- Production image size: about `2.07 GB`
- Host-side model cache: shared HF hub snapshot for `BAAI/bge-m3`; pre-dedupe size was about `8.6 GB`
- Steady-state container memory: about `1.2 GiB`

## Search runtime profile

Current textbook search is no longer lexical-only. Runtime query handling now has two public-facing paths:

- `/api/search`: primary list search, using `lexical + dense + supplemental + rerank` for textbook queries
- `/api/chat`: AI explanation path, where precision queries prefer the server-side precision-agent flow before any browser fallback

For textbook precision queries such as definition / distinction / process lookups, the retrieval chain is:

1. intent detection and query-term planning
2. lexical recall from the main SQLite corpus
3. dense recall from FAISS (`BAAI/bge-m3`)
4. page-level supplemental fallback from `supplemental_textbook_pages.jsonl.gz`
5. CrossEncoder rerank before final response / list ordering

GraphRAG remains a relation-hint layer only; it does not replace textbook evidence retrieval in production.

## Resource guidance

Recommended separation:

- OCR, MinerU, FAISS rebuild, and large batch enrichment stay on offline machines
- Production VPS only handles runtime retrieval and API serving

VPS sizing:

- Minimum usable: `2 vCPU / 4 GB RAM / 25 GB SSD`
- Recommended production: `4 vCPU / 8 GB RAM / 60 GB SSD`
- If the same host also performs Docker builds for deployment or runs other services: `4 vCPU / 8 GB RAM / 80 GB SSD`

## Deployment flow

Current production deploy is:

1. Push to GitHub `main`
2. GitHub Actions creates a clean temporary checkout on the VPS
3. `scripts/deploy_vps.sh` builds a new CPU-only image
4. Runtime search assets, including the supplemental textbook page source, manifests, and supplemental vector files, are synced into `data/index/`
5. Host model cache is reused or warmed before cutover; reranker cache is also prewarmed when enabled
6. Old container is replaced only after image build succeeds
7. `/api/health` must pass or the script rolls back to the previous image
8. When reranker is required for this rollout, health gating also checks `reranker.loaded=true`
9. Dangling images are pruned; only recent rollback tags are retained and old `build-*` tags are removed

Large artifact transport is not hardcoded to a single path. For assets larger than about `50M`, benchmark the current-session route first:

- direct workstation -> VPS over SSH / `scp` / `rsync`
- workstation -> R2, then VPS -> `curl`

Choose the faster and more reliable path for that session, then verify remote size and SHA256 before cutover.

Docs-only pushes should not reach this deploy path. The workflow is expected to ignore `README.md` and `docs/**` changes so GitHub documentation sync does not mutate production.

## Operational watchpoints

- Disk pressure now mainly comes from host-side model cache and rollback images, not from the SQLite/FAISS runtime assets
- Production should not be used for FAISS rebuild or MinerU OCR
- The runtime DB and FAISS manifest must stay aligned; dense retrieval should remain gated if manifest validation fails
- The supplemental textbook manifest must report `unresolved_books=0` and `unresolved_pages=0` before release
- Production health must treat supplemental availability and reranker readiness as first-class rollout gates, not optional debug fields
- The main corpus and supplemental index should be regenerated together whenever textbook mappings or book-key normalization changes
- The production repo can contain hotfix history or manual changes, so deployment should always build from a clean release checkout rather than `git pull` into the runtime directory
- Page-image sync to R2 must stage textbook page maps together with dictionary page trees such as `pages/dict_xuci/` and `pages/dict_changyong/`; a root-level `rclone sync` from textbook-only sources will delete existing dictionary page assets from R2
