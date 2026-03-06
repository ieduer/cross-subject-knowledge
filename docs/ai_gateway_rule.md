# AI Gateway Rule

This project uses the Cloudflare Worker custom domain `https://ai.bdfz.net/` as the canonical external AI gateway.

## Canonical rule

- Public/browser/server entrypoint: `https://ai.bdfz.net/`
- Cloudflare binding: custom domain `ai.bdfz.net` -> Worker service `apis` / `production`
- Purpose: route this project to the largest Gemini API key pool while keeping the China-facing entrypoint on the Worker custom domain
- Internal fallback domain, when explicitly needed elsewhere: `https://apis.bdfz.workers.dev`

## Do not confuse

- `ai.bdfz.net` is the canonical runtime domain for this project
- `apis` is the Cloudflare Worker service name shown in the dashboard
- `apis.bdfz.net` may appear in older tooling or route-based docs, but it is not the canonical domain for this project

## Current implementation points

- Backend default: `backend/main.py` -> `AI_SERVICE_URL`
- Frontend fallback default: `frontend/assets/app.js` -> `AI_API`
- Project overview: `README.md`
