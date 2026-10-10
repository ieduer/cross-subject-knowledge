# AI guard — cost and abuse controls for the SUN conversation

Status: implemented on branch `claude/sun-ai-fixes-20261010` (base `035d2d4`), **not deployed**.
Tested with mocks only; no model endpoint was contacted while building or testing this.

Code: `backend/ai_guard.py` (all controls), `backend/main.py` (retrieval + prompt builders, router registration),
`frontend/assets/ai-client.js` (the only browser code that can start an AI request), `frontend/assets/app.js`, `frontend/assets/dict.js`.

## 1. What changed, in one picture

Before (production `035d2d4`):

```
search results rendered ──auto, 120 ms, precision queries──┐
subject / scope change  ──auto (re-fires)──────────────────┤
dictionary lookup, incl. ?q= page load ──auto──────────────┤
click "AI 教材解读" / chip / send ─────────────────────────┤
                                                           ▼
browser ──POST /api/chat ────────────► server: retrieval #1 ─► gateway (35 s × up to 2 attempts)
        ├─POST /api/chat/context ────► server: retrieval #2 (same work again)
        ├─after 8 s, never cancelled ► ai.bdfz.net ─(then)─► apis.bdfz.workers.dev   browser-authored prompt
        └─POST /api/chat/log ────────► server stores any caller-supplied text
click "AI 关联分析" (gaokao) ────────► ai.bdfz.net directly, browser-authored prompt
no auth · no rate limit · no dedup · no input cap · no budget · no output cap
```

After:

```
user clicks (button / chip / send / 重试) — nothing else can start AI
        │
        ▼
browser ──POST /api/ai/session (only when no valid session; cheap, never reaches a model)
        └─POST /api/chat | /api/dict/chat | /api/gaokao/chat      exactly one request, AbortController, no retry
                │
                ▼  backend/ai_guard.py, in this order
        1 enabled?            AI_ENABLED=1 + AI_SESSION_KEY + AI_DAILY_REQUEST_BUDGET   else 503 AI_DISABLED
        2 same-origin         Origin (required) + Sec-Fetch-Site (if present)            else 403
        3 session token       HMAC cookie bound to the client subject                    else 401
        4 rate limit          token bucket per subject                                   else 429 + Retry-After
        5 body + field caps   Content-Length required, streamed byte cap, lengths        else 411 / 413 / 400 / 415
        6 answer cache        identical normalised request within TTL                    → cached answer, no call
        7 in-flight dedup     identical request already running                          → join it, no new call
        8 concurrency slot    2 running, 4 queued ≤ 15 s                                 else 503 AI_BUSY
        9 retrieval + prompt  server-side, once                                           prompt > cap → 413
       10 daily budget        SQLite, atomic increment BEFORE the call                   else 503 AI_BUDGET_EXHAUSTED
       11 upstream            ONE attempt, total deadline ≤ 30 s, bounded read            504 / 502, never retried
       12 answer cap, citations from retrieval records, minimised server-side log
                │
                ▼
        one response: answer + context/citations (the client never runs a second retrieval)
```

Removed outright: every automatic trigger, the browser-direct gateway calls (main chat fallback **and** the gaokao
"AI 关联分析" button, which was a third browser-direct path with a browser-authored prompt), the `Promise.any` race,
the parallel `/api/chat/context` retrieval, the automatic second upstream attempt, and `/api/chat/log`.
`/api/chat/context` and `/api/chat/log` now answer `410` and read nothing from the caller.

## 2. Behaviour the user sees

- Main pages: the AI panel still appears with every non-empty result. For definition-style ("precision") queries the
  button reads 「让 AI 结合检索结果解答」 instead of the answer appearing by itself; otherwise it stays 「AI 教材解读」.
  Starter chips, follow-up chips, the follow-up box, copy, context tags and source chips are unchanged.
- Dictionary pages: after a lookup (including a `?q=` page load) the AI panel shows one button
  「让 AI 结合检索结果解答」; the follow-up box works as before.
- While a request is pending: send / button / chips are disabled and a 取消 button is shown. Cancel aborts the fetch.
- On failure: the message and a 重试 button. Nothing retries by itself.
- A new search, subject switch or dictionary lookup aborts the pending request; a late answer is never rendered
  into a different query. If only the scope filter changed while waiting, the answer is withheld with a note.
- When AI is disabled the first click shows 「AI 解读当前未开启。」; search, graph, gaokao, analytics and dictionary
  lookup do not depend on AI and are untouched.
- If the model writes a page reference that is not in the retrieved evidence, a one-line note says so. The answer is
  always escaped plain text; the only clickable page references are the source chips, which are built from the
  retrieval records (`book_key` / `section` / `logical_page` from the DB).

## 3. Controls, defaults and env vars

All values are read once at process start. A container env change needs the container to be re-created.

### Enablement (fail closed)

| Env | Default | Meaning |
| --- | --- | --- |
| `AI_ENABLED` | unset → **disabled** | Must be exactly `1`. |
| `AI_SESSION_KEY` | unset → **disabled** | HMAC key, at least 32 characters. Secret. Shorter values count as unset. |
| `AI_DAILY_REQUEST_BUDGET` | unset → **disabled** | Positive integer: upstream calls allowed per UTC day, all endpoints together. |

Any one missing → every AI endpoint returns `503 AI_DISABLED` before reading headers or body, and
`GET /api/ai/status` reports which names are missing (names only).

### Admission

| Control | Env | Default |
| --- | --- | --- |
| Allowed `Origin` values | `AI_ALLOWED_ORIGINS` | `https://sun.bdfz.net` |
| Session lifetime | `AI_SESSION_TTL_SEC` | 1800 |
| Cookie `Secure` flag | `AI_COOKIE_SECURE` | on (`0` only for local http testing) |
| Proxies whose `X-Real-IP` is believed | `AI_TRUSTED_PROXIES` | `127.0.0.0/8,::1/128` |
| Session issuance rate | `AI_SESSION_RATE_BURST` / `AI_SESSION_RATE_PER_MINUTE` | 10 / 30 per subject |

`POST /api/ai/session` sets `sun_ai_session` (`HttpOnly; Secure; SameSite=Strict; Path=/api`). The token is
`v1.<expiry>.<nonce>.<HMAC-SHA256>` over expiry, nonce and the client subject.

**This is abuse friction and a quota subject. It is not user authentication.** A script can forge `Origin`, call the
session endpoint and obtain a token. What the token buys is that every paid request is tied to a subject that the
rate limit, the optional per-subject cap and the global budget then bound. `Origin` is never treated as proof of
anything except "a browser on another site did not send this".

Client subject = the TCP peer address, or `X-Real-IP` **only when the peer is a trusted proxy**; IPv4 as the
address, IPv6 as its /64. Raw addresses are never stored; logs and the per-subject counter use a keyed hash.

### Request caps (reject, never silently truncate)

| Cap | Env | Default | Response |
| --- | --- | --- | --- |
| Body bytes (streamed; `Content-Length` required) | `AI_MAX_BODY_BYTES` | 49,152 | 411 / 413 |
| `Content-Type` | — | `application/json` only | 415 |
| Query / headword | `AI_MAX_QUERY_CHARS` | 200 (headword 40) | 413 |
| User message | `AI_MAX_MESSAGE_CHARS` | 800 | 413 |
| History items | `AI_MAX_HISTORY_ITEMS` | 6 | 413 |
| History item length | `AI_MAX_HISTORY_ITEM_CHARS` | 1,200 | 413 |
| Prompt after server-side assembly | `AI_MAX_PROMPT_CHARS` | 24,000 | 413, no call, no budget spent |

Only `role` and `content` of each history item are accepted. The dictionary endpoint ignores any client-supplied
context text; the gaokao endpoint accepts a question id and nothing else.

**Maximum prompt size sent upstream: 24,000 characters** (≤ 72,000 UTF-8 bytes if every character is CJK). Measured
through the real prompt builders with every user-controlled field at its cap: dictionary 21,253 (bounded by
construction: 5,000 + 5,000 + 4,000 evidence, 6,031 history, 800 message, template), main chat about 14,000–21,000
with deliberately oversized evidence; a typical main-chat prompt is about 4,000. Evidence assembly is bounded by
construction in most places (8 reranked items, 2 per subject, 180–600 character snippets) but a few DB fields
(`ai_summary`, relation descriptions) have no length limit, which is why the hard cap exists.

### Dedup, cache, concurrency, rate

| Control | Env | Default |
| --- | --- | --- |
| In-flight dedup | — | always on; key = SHA-256 of endpoint + NFKC/whitespace-normalised fields |
| Answer cache TTL | `AI_CACHE_TTL_SEC` | 600 s (`0` disables) |
| Answer cache size | `AI_CACHE_MAX_ENTRIES` / `AI_CACHE_MAX_ENTRY_BYTES` | 256 entries / 64 KiB each (LRU) |
| Concurrent upstream calls | `AI_MAX_CONCURRENT` | 2 |
| Wait queue / max wait | `AI_MAX_QUEUE` / `AI_QUEUE_WAIT_SEC` | 4 / 15 s → `503 AI_BUSY`, `Retry-After: 5` |
| Per-subject rate | `AI_RATE_BURST` / `AI_RATE_PER_MINUTE` | 5 / 10 → `429`, `Retry-After` |

Waiter-aware cancellation: a shared call is cancelled only when every waiter has gone, and it is removed from the
in-flight map synchronously at that moment, so an identical request arriving right after starts a fresh call rather
than joining a dying one. A request that is already disconnected starts nothing. The concurrency slot covers
retrieval as well as the upstream call (the host has one worker and about 1.5 GiB free RAM). Failures are never cached.

### Upstream

| Control | Env | Default |
| --- | --- | --- |
| Total deadline (connect + send + full body read) | `AI_SERVICE_TIMEOUT_SEC` | 30 s, hard maximum 30 |
| Retries | `AI_SERVICE_RETRIES` | 0; hard maximum 1, and only for a connection that was never established. Never after a timeout or any HTTP response. |
| Response bytes | `AI_MAX_UPSTREAM_BYTES` | 262,144 → `502` |
| Answer characters | `AI_MAX_ANSWER_CHARS` | 4,000, then cut with a visible 「已截断」 mark |

Unchanged and server-side only: `AI_SERVICE_URL`, `AI_SERVICE_MODEL`, `AI_SERVICE_ORIGIN`, `AI_SERVICE_PROJECT`,
`AI_SERVICE_TASK_TYPE`, `AI_SERVICE_THINKING_LEVEL`, `AI_INTERNAL_TOKEN`. Upstream error bodies are no longer
echoed to the browser.

### Budget

| Control | Env | Default |
| --- | --- | --- |
| Global daily stop | `AI_DAILY_REQUEST_BUDGET` | none (required) |
| Per-subject daily cap | `AI_SUBJECT_DAILY_CAP` | 0 = off |

Store: `STATE_ROOT/ai_guard/ai_budget.sqlite3`, one row per UTC date, `BEGIN IMMEDIATE` increment before the upstream
call. It survives a process restart, resets when the UTC date changes, and fails closed (`503 AI_BUDGET_UNAVAILABLE`)
if the file cannot be opened or written. A reserved call is not refunded when the upstream fails or times out,
because the gateway may still have billed it.

`GET /api/ai/status` (read-only, no user data) for the status system:

```json
{"enabled": true,
 "budget": {"date": "2026-10-10", "count": 37, "limit": 200, "remaining": 163, "resets_at": "2026-10-11T00:00:00Z", "available": true},
 "subject_daily_cap": 0, "limits": {...}, "runtime": {"inflight": 0, "active": 0, "queued": 0, "cached": 12},
 "counters": {"upstream_calls": 37, "cache_hits": 9, "joined": 2, "rate_limited": 4, "unauthorized": 1, ...}}
```

`counters` and `runtime` are per process and reset on restart; `budget` is persistent.

## 4. What is a hard spending limit and what is not

| Thing | Hard limit? |
| --- | --- |
| `AI_DAILY_REQUEST_BUDGET` in this service | **Yes**, for calls made by this service: at most N upstream calls per UTC day, each ≤ 24,000 prompt characters and one attempt. |
| Rate limit, concurrency, dedup, cache, session | No. They shape and reduce load; the budget is what stops it. |
| APIS-side quotas, key-pool limits, Google budget alerts | **Not ours and not a stop we control.** Alerts notify; they do not block. |
| Anything a browser sends straight to the gateway | **Outside this service entirely** — see residual risk R1. |

The budget counts calls to the gateway, not model requests and not tokens. If the gateway turns one call into
several model requests, the real multiple is theirs (question below).

## 5. Exact production env to keep today's functionality

Production currently sets no `AI_*` variable, so deploying this code as-is turns AI **off**. To keep it on, the
container needs (added to the `docker run` in the release procedure; values are not in the repository):

```
AI_ENABLED=1
AI_SESSION_KEY=<random, ≥ 32 chars, e.g. `openssl rand -base64 48`; keep it in the existing secret store>
AI_DAILY_REQUEST_BUDGET=<owner's number>
```

Strongly recommended at the same time:

```
AI_TRUSTED_PROXIES=<the address nginx connects from, as seen inside the container>   # see R3 — only after :8080 is bound to 127.0.0.1
AI_INTERNAL_TOKEN=<caller credential agreed with the APIS owner>                     # see R1
AI_SERVICE_PROJECT=sun.bdfz.net
```

nginx must overwrite (not append) the header: `proxy_set_header X-Real-IP $remote_addr;` and must not set
`proxy_ignore_client_abort on` (cancellation depends on nginx closing the upstream connection when the client leaves).

Rotating `AI_SESSION_KEY` invalidates open sessions (users get one "会话已过期" and their next click re-admits)
and changes the log hashes.

## 6. Logging

`ai_chat_logs` is written only by the server, once per shared upstream attempt, through `ai_guard.write_chat_log`.

- Nothing in the application reads this table (checked: the only readers are the release-manifest scripts, which list
  it as a mutable table excluded from fingerprints; 热门检索 reads `search_logs`, not this table). Raw text is therefore
  not needed and is no longer stored.
- Additive columns only: `kind`, `outcome`, `query_hash` (keyed HMAC, 16 hex), `query_len`, `message_len`,
  `prompt_chars`, `answer_chars`, `duration_ms`, `upstream_called`. The legacy `query` / `user_message` columns are
  `NOT NULL` and receive empty strings; `error` holds a coarse code such as `ai_timeout`, never upstream text.
  No column is dropped; existing rows are untouched.
- No answer text, no prompt, no IP, no cookie, no user agent is logged.
- The search term itself is still recorded by the ordinary search log when the user searches (unchanged, out of scope here).

Retention tools, **not scheduled anywhere** — an operator runs them on purpose, after the usual backup:

```
# dry run (default): how many rows are older than 180 days
python backend/ai_guard.py prune-logs --db /data/index/textbook_mineru_fts.db --older-than-days 180
# delete at most 5,000 of them (one bounded batch per run)
python backend/ai_guard.py prune-logs --db ... --older-than-days 180 --limit 5000 --apply
# optional: blank the raw text kept by the 593 rows written before the guard (lengths are kept)
python backend/ai_guard.py redact-legacy-text --db ... --apply
```

Both write to the live database and are an owner decision; neither has been run.

## 7. Tests

```
/private/tmp/cf-task-sun-consolidation-20261010/venv-ai/bin/python -m unittest discover -s tests   # 90 tests
node --test tests/ai_client.test.mjs                                                               # 9 tests
```

- `tests/test_ai_guard_core.py` — config, subject, token, limiter, budget (restart, date change, threads, fail closed),
  upstream (no retry after timeout, deadline, byte cap), dedup, waiter cancellation, concurrency, cache, logs, citations.
- `tests/test_ai_guard_http.py` — the router over ASGI with fake builders: 503 / 403 / 401 / 411 / 413 / 415 / 429,
  cookie attributes, subject binding, budget stop, retired endpoints.
- `tests/test_ai_main_integration.py` — the real `backend/main.py` app imported against an empty temp `DATA_ROOT`
  (its heavy dependencies are optional at import), synthetic retrieval, counting upstream: real prompt builders,
  real `ai_chat_logs` table, real budget file.
- `tests/test_ai_static.py` — no model host / race / auto-trigger in served JS and HTML; main.py wiring.
- `tests/ai_client.test.mjs` — the browser module under a fake `fetch`: one request, cancel, no retry, bounded history.

Only `fastapi` and `httpx` are installed in the test venv. The UI was also exercised in a browser against a local
stub that returns canned JSON (no auto request on a precision query or on `dict.html?q=`, duplicate clicks ignored,
cancel, manual retry, stale answer dropped after a new search).

## 8. Questions for the APIS owner

1. Does one call to the gateway fan out to more than one model request (hedging, lease retries, model fallback)?
   Our budget counts gateway calls; we need the worst-case multiple.
2. When we close the connection (user cancel, or our 30 s deadline), does the gateway stop the model request, or does
   generation run to completion and bill?
3. Can the gateway cap output tokens per request for the `sun.bdfz.net` project, or accept a per-request limit? We can
   only cap what we read back (256 KiB / 4,000 characters), not what is generated.
4. **Can the gateway stop accepting browser-origin calls for SUN?** i.e. require `X-Internal-Token` for
   `Origin: https://sun.bdfz.net` / project SUN and remove that origin from the browser allow-list. Until then R1 stands.
5. Is there a per-project daily quota on the gateway side that can be set as a second, independent stop?
6. The backend still sends a Chrome `User-Agent` and `Referer` to the gateway (unchanged legacy behaviour). Is that
   needed, or can it be replaced by an honest service identifier?
7. Which response shape is authoritative, `answer` or `data.answer`? The code accepts both.

## 9. Residual risks

- **R1 — the gateway still trusts the browser origin.** Removing the browser fallback from our pages does not stop
  anyone from POSTing to the gateway with `Origin: https://sun.bdfz.net`, exactly as the old page did, and none of the
  controls here see those calls. Tabs that still run the old cached script will also keep doing it — and because their
  `/api/chat` call now gets `401`, they fall straight through to the browser-direct path, auto-trigger included, until
  the page is reloaded (the HTML now references `?v=20261010a`, so a reload fixes it). Only the gateway can close this
  (question 4). Until it does, the daily budget here is a limit on this service, not on SUN-attributed spend.
- **R2 — admission is friction, not identity.** One address can obtain a token and spend up to the rate limit; many
  addresses can together spend the whole daily budget and deny AI to everyone until UTC midnight. That is the designed
  failure mode: bounded cost, degraded feature.
- **R3 — client address behind Docker.** nginx reaches the container through the Docker bridge, so the peer the app
  sees is probably the bridge gateway, not loopback. With the default `AI_TRUSTED_PROXIES` every visitor then shares
  one subject: one rate bucket (10 / min) for the whole site, which is safe for cost and bad for users. Trusting the
  bridge address is only safe once port 8080 is no longer reachable by raw IP (stage 5 binds it to 127.0.0.1);
  otherwise a direct caller that appears from that address could choose its own subject. Verify the peer address the
  container actually sees for nginx and for a raw-IP caller before setting it.
- **R4 — shared addresses.** A school network behind one public IPv4 address is one subject. The per-subject cap is
  therefore off by default and the rate limit is modest; both are owner decisions.
- **R5 — single process.** Dedup, cache, rate buckets and the concurrency limit live in memory. With
  `uvicorn --workers 1` (today) they are global. With N workers each has its own; the SQLite budget stays global.
- **R6 — budget is in calls, spent on reservation.** Not tokens, not model requests (question 1). Timeouts and failed
  calls still consume it.
- **R7 — `STATE_ROOT` loss.** Deleting `state/ai_guard/` resets the day's counter. It is on the mounted `/state` volume.
- **R8 — cancelled retrieval.** A cancelled request stops waiting immediately and never reaches the upstream, but a
  retrieval already running in the thread pool finishes its current query.
- **R9 — rate limit before cache.** A cache hit still consumes a rate token (conservative).
- **R10 — legacy rows.** The 593 rows written before this change still contain raw question text until the owner runs
  the redaction tool.
- **R11 — status endpoint is public.** It exposes counts and limits only; if that is unwanted, restrict it in nginx.
- **R12 — gaokao wording.** The gaokao prompt is now built on the server from the stored question text (first 600
  characters) instead of the text the browser rendered; the instructions are the same, the excerpt can differ slightly.
- **R13 — deploy script.** `scripts/deploy_vps.sh` warmed the reranker through `/api/chat/context`; that route is
  retired, so the warm-up now calls `/api/search`. The health gate on `reranker.loaded` is unchanged, but confirm in
  the stage-5 rehearsal that the first precision question after a cold start is acceptable.

## 10. Decisions for the owner

1. **Does AI stay enabled?** Code default is off. Turning it on is the three env values in §5.
2. **Daily budget number** (`AI_DAILY_REQUEST_BUDGET`). Evidence available: 593 logged server-side chats in total up
   to 2026-10-08 — a low single-digit daily average — but that log never counted browser-direct calls or automatic
   dictionary calls made by cached pages, so true past volume is unknown.
3. **Per-subject daily cap** (`AI_SUBJECT_DAILY_CAP`): off, or a number that a whole classroom behind one address can live with.
4. **Named cost owner** for SUN's gateway usage (the matrix contract requires one for anonymous AI).
5. **Rate limit values** if 10 / min, burst 5 per address is wrong for campus use.
6. **Ask the APIS owner to close the browser path (R1)** and agree a caller credential (`AI_INTERNAL_TOKEN`).
7. **Legacy log rows**: redact the raw text now, prune by age, or keep.
8. **`AI_TRUSTED_PROXIES`** value, after the stage-5 port change (R3).
9. Whether `/api/ai/status` may be public or should be limited to the status system.
