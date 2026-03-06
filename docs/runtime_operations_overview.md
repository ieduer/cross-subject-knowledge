# Runtime And Operations Overview

This document records the current runtime, data, and deployment boundaries of the textbook project.

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
- `/root/cross-subject-knowledge/state/cache/huggingface`
- `/root/cross-subject-knowledge/state/cache/sentence_transformers`
- `/root/cross-subject-knowledge/state/logs`
- `/root/cross-subject-knowledge/state/batch`

## Current runtime profile

As of 2026-03-06:

- DB chunks: `20553`
- Textbook chunks: `16524`
- Gaokao chunks: `4029`
- FAISS vectors: `16524`
- Embedder: `BAAI/bge-m3`
- Production image size: about `2.07 GB`
- Host-side model cache: about `8.6 GB`
- Steady-state container memory: about `1.2 GiB`

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
4. Host model cache is reused or warmed before cutover
5. Old container is replaced only after image build succeeds
6. `/api/health` must pass or the script rolls back to the previous image
7. Dangling images are pruned; only recent rollback tags are retained

## Operational watchpoints

- Disk pressure now mainly comes from host-side model cache and rollback images, not from the SQLite/FAISS runtime assets
- Production should not be used for FAISS rebuild or MinerU OCR
- The runtime DB and FAISS manifest must stay aligned; dense retrieval should remain gated if manifest validation fails
- The production repo can contain hotfix history or manual changes, so deployment should always build from a clean release checkout rather than `git pull` into the runtime directory
