# Maintenance Manual

This is the handoff runbook for keeping the textbook project healthy.

Read this file first for daily checks, incident triage, deploy verification, and rollback. For data lineage, asset meaning, and release-boundary background, continue into [data_layer_lineage_memory.md](./data_layer_lineage_memory.md) and [release_maintenance_design.md](./release_maintenance_design.md).

## 1. Scope

This manual covers:

- runtime health checks
- local and production-safe inspection
- deploy and rollback flow
- log and SQLite behavior analysis
- known failure modes that are easy to misread

This manual does not cover:

- OCR / MinerU rebuild details
- full textbook identity reconciliation
- large offline re-index operations

Use the deeper docs for those.

## 2. Entry Points

Current runtime entrypoints are:

- app server: `backend/main.py`
- container entrypoint: `backend/entrypoint.sh`
- deploy script: `scripts/deploy_vps.sh`
- release manifest tools:
  - `scripts/build_release_manifest.py`
  - `scripts/verify_release_manifest.py`
  - `scripts/stage_clean_release.py`
- GitHub deploy workflow: `.github/workflows/deploy.yml`

Current deploy behavior:

- `main` pushes deploy automatically
- `README.md` and `docs/**` are docs-only changes and do not deploy
- production deploy must come from a clean checkout
- runtime DB sync is disabled by default at container startup

## 3. Runtime Baseline

Before touching anything, confirm the following boundaries:

- main runtime assets live under `data/index/`
- container startup must use mounted runtime assets, not remote DB pull
- `RUNTIME_DB_SYNC_MODE` should stay `disabled`
- `scripts/deploy_vps.sh` is the source of truth for:
  - `RUNTIME_ROOT`
  - `CONTAINER_NAME`
  - required runtime assets
  - release-manifest validation

Runtime assets that must exist and stay aligned:

- `data/index/textbook_mineru_fts.db`
- `data/index/textbook_chunks.index`
- `data/index/textbook_chunks.manifest.json`
- `data/index/supplemental_textbook_pages.jsonl.gz`
- `data/index/supplemental_textbook_pages.manifest.json`
- `data/index/supplemental_textbook_pages.index`
- `data/index/supplemental_textbook_pages.vector.manifest.json`

## 4. Daily Check

Run these in repo root before any diagnosis or release:

```bash
git status --short
curl -fsS http://127.0.0.1:8080/api/health | jq
sqlite3 data/index/textbook_mineru_fts.db "SELECT COUNT(*) FROM chunks;"
sqlite3 data/index/textbook_mineru_fts.db "SELECT COUNT(*) FROM search_logs;"
sqlite3 data/index/textbook_mineru_fts.db "SELECT COUNT(*) FROM ai_chat_logs;"
```

Health must be interpreted field by field, not just by top-level `status`:

- `db.ok=true`
- `faiss.ok=true`
- `model.ok=true`
- `supplemental.ok=true`
- when `reranker.enabled=true`, `reranker.loaded=true`
- when `supplemental_vectors.enabled=true`, `supplemental_vectors.loaded=true`

## 5. Smoke Checks

Use a small fixed smoke set after deploy or when search relevance looks wrong:

```bash
curl -fsS "http://127.0.0.1:8080/api/search?q=蛋白质&limit=3" | jq '.total'
curl -fsS "http://127.0.0.1:8080/api/search?q=自然灾害&phase=高中&limit=3" | jq '.total'
curl -fsS "http://127.0.0.1:8080/api/search?q=以&subject=语文&source=textbook&limit=3" | jq '.total'
curl -fsS "http://127.0.0.1:8080/api/dict/search?q=画蛇添足&limit=3" | jq '.entries | length'
curl -fsS "http://127.0.0.1:8080/api/health" | jq
```

What these cover:

- `蛋白质`: common hybrid textbook search
- `自然灾害`: supplemental textbook page path
- `以`: single-character textbook retrieval path
- `画蛇添足`: dictionary / idiom path
- `/api/health`: runtime asset and model readiness

## 6. Logs And Runtime Evidence

Use the right evidence source for the right question:

- application startup / runtime log:
  - local: `server.log`
  - containerized runtime: `docker logs --tail 200 textbook-knowledge`
- runtime health and retrieval state:
  - `/api/health`
- search usage:
  - SQLite table `search_logs`
- AI usage:
  - SQLite table `ai_chat_logs`
- long offline batch logs:
  - `logs/`

Important distinction:

- `logs/` mostly contains historical offline jobs, downloads, OCR, and migration work
- `search_logs` and `ai_chat_logs` are the authoritative runtime behavior traces
- `textbook_mineru_fts.db` is a live file; its hash changes as logs grow

Useful queries:

```bash
sqlite3 -header -column data/index/textbook_mineru_fts.db \
  "SELECT date(ts,'unixepoch','localtime') AS day, COUNT(*) AS searches FROM search_logs GROUP BY day ORDER BY day DESC LIMIT 14;"

sqlite3 -header -column data/index/textbook_mineru_fts.db \
  "SELECT query_normalized, COUNT(*) AS freq, SUM(CASE WHEN result_count=0 THEN 1 ELSE 0 END) AS zero_hits FROM search_logs GROUP BY query_normalized ORDER BY freq DESC LIMIT 20;"

sqlite3 -header -column data/index/textbook_mineru_fts.db \
  "SELECT provider, COUNT(*) AS chats, SUM(success) AS success_count, SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failed_count FROM ai_chat_logs GROUP BY provider;"
```

## 7. Release Workflow

Normal release:

1. Validate code and runtime assets locally.
2. Rebuild and verify release manifest when runtime assets changed.
3. Push to `main`.
4. Let GitHub Actions clone a clean checkout and run `scripts/deploy_vps.sh`.
5. Verify `/api/health` and smoke queries after cutover.

Manual release:

1. Run `scripts/build_release_manifest.py`.
2. Run `scripts/verify_release_manifest.py`.
3. If needed, create a clean bundle with `scripts/stage_clean_release.py`.
4. Deploy from the clean release directory, never from a dirty runtime checkout.

Docs-only update:

- changing only `README.md` or `docs/**` should not deploy production
- if a docs-only change triggered deploy, inspect `.github/workflows/deploy.yml`

## 8. Rollback

Rollback rules:

- do not treat `latest` as a safe rollback anchor
- capture the running image digest before risky manual work
- prefer redeploying the previous verified image over ad-hoc file replacement
- if runtime data was changed, roll back code and data together

Minimum rollback sequence:

1. identify the currently running image
2. tag the digest if a human rollback point is needed
3. restore the previous verified code state
4. restore matching runtime assets if the release included DB / FAISS / supplemental changes
5. rerun `/api/health` and the smoke queries

## 9. Known Failure Modes

### A. Health says `degraded`

Likely causes:

- runtime DB missing or unreadable
- FAISS manifest mismatch
- model cache missing
- supplemental manifest unresolved
- reranker not preloaded after release

First checks:

```bash
curl -fsS http://127.0.0.1:8080/api/health | jq
python3 scripts/verify_release_manifest.py --manifest release_manifest.json --source-root . --check source
```

### B. Search returns `page_url=null`

Usually not a query bug. Check:

- `frontend/assets/pages/book_map.json` exists in source
- the built image contains it
- the mapped book is inside the public product scope
- page-image sync to CDN was done for the new mapping set

### C. Single-character textbook queries unexpectedly return `0`

This was a real bug path in `/api/search`: single-character queries could end up with an empty primary search plan and skip main textbook retrieval. The current code now forces a primary fallback for single-character textbook searches.

Keep this smoke test in the checklist:

```bash
curl -fsS "http://127.0.0.1:8080/api/search?q=以&subject=语文&source=textbook&limit=3" | jq '.total'
```

### D. AI chat failures are all timeouts

Interpretation:

- this usually points to AI gateway latency or cold runtime path
- it is not the same as textbook retrieval failure

Check:

- `AI_SERVICE_TIMEOUT_SEC`
- `AI_SERVICE_RETRIES`
- recent `ai_chat_logs.error`
- whether `reranker` and model warmup are complete

### E. Usage analysis looks too good

Check whether smoke traffic polluted the logs:

- `search_logs.source='smoke'`
- synthetic queries are now filtered, but historical rows may remain
- analytics reports should exclude smoke rows unless you are specifically auditing test activity

## 10. Safe Editing Rules

Before changing runtime behavior:

- inspect first
- keep the change minimal
- validate with `/api/health` and fixed smoke queries
- avoid mixing code, runtime data rebuild, and CDN page sync unless the issue actually spans them

When the issue is in search behavior, verify all four layers separately:

1. SQLite rows exist
2. `/api/search` returns them
3. page mapping is present if page URLs matter
4. AI answer quality is still acceptable after the search fix

## 11. Handoff Checklist

A new maintainer should be able to answer all of these before shipping changes:

- Which file starts the app?
- Which script deploys to production?
- Which assets are mounted runtime dependencies?
- Which checks decide if health is truly green?
- Which smoke queries cover textbook, supplemental, single-character, and dictionary paths?
- Which changes are docs-only and must not deploy?
- Which rollback anchor is valid for this release?

If any answer is missing, update this manual before the next release.
