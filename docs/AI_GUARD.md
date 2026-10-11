# AI guard — cost and abuse controls for the SUN conversation

Status: implemented, merged into `claude/sun-two-tabs-20261010` (base `035d2d4`), **not deployed**.
Revised after the independent review `REVIEW-DELIVERY-AI.md` (2026-10-10): old-tab handling (§11), release with AI
off (§12), enabling checklist (§13), review dispositions (§14).
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
        0 guarded client?     X-Sun-AI-Client: 2 (sent by ai-client.js)                  absent → fixed 200 "请刷新" reply, §11
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
`/api/chat/log` answers `410`; `/api/chat/context` runs no retrieval and returns a fixed reply meant for pages that
still run the old script (§11). Neither reads anything from the caller.

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
| Proxies whose `X-Real-IP` is believed | `AI_TRUSTED_PROXIES` | `127.0.0.0/8,::1/128` — inside the container this matches only the container's own health check, i.e. **no proxy is trusted** |
| Session issuance rate | `AI_SESSION_RATE_BURST` / `AI_SESSION_RATE_PER_MINUTE` | 10 / 30 per subject |

`POST /api/ai/session` sets `sun_ai_session` (`HttpOnly; Secure; SameSite=Strict; Path=/api`). The token is
`v1.<expiry>.<nonce>.<HMAC-SHA256>` over expiry, nonce and the client subject.

The token is bound to the client address: a phone that changes network gets `401` once and the next click
re-admits. Within its 30 minutes it can be replayed from the same address; that is the design.

**This is abuse friction and a quota subject. It is not user authentication.** A script can forge `Origin`, call the
session endpoint and obtain a token. What the token buys is that every paid request is tied to a subject that the
rate limit, the optional per-subject cap and the global budget then bound. `Origin` is never treated as proof of
anything except "a browser on another site did not send this".

Client subject = the TCP peer address, or `X-Real-IP` **only when the peer is a trusted proxy**; IPv4 as the
address, IPv6 as its /64. Raw addresses are never stored; logs and the per-subject counter use a keyed hash.

**Subject keying on the production host — what is and is not known.** Not verified on the host; the following is
expected Docker behaviour plus what `BASELINE-SUN-VPS.md` records, and must be confirmed there before any trust is set.

- nginx reaches the container through the Docker bridge, so the peer the app sees for every proxied visitor is the
  bridge gateway, not loopback. With the default (nothing trusted) **the whole site is one subject**: one rate bucket
  (10 / min, burst 5) and one session-issuance bucket shared by everybody, and one client can make everyone else see
  429. That is safe for cost and poor for availability. It is the default on purpose.
- **While port 8080 is published on all interfaces, no proxy may be trusted.** The baseline shows docker-proxy
  listening on `[::]:8080` as well as `0.0.0.0:8080`. A raw-IP caller — in particular an IPv6 one on a v4-only
  bridge — can arrive from the same gateway address as nginx. If that address were trusted, such a caller could put
  any value in `X-Real-IP`, mint a new subject per request and so escape the per-subject rate limit and the
  per-subject daily cap. Do not assume a direct caller always shows its real address.
- Only after the publish is changed to `-p 127.0.0.1:8080:8080` (stage 5) may `AI_TRUSTED_PROXIES` be set, to the
  exact gateway address as seen inside the container (`/32`), with nginx overwriting `X-Real-IP`.
- In every one of these states the per-subject controls only shape traffic. **The hard stop is the daily budget**
  (§4): it does not depend on the subject at all, so neither the shared bucket nor a spoofed subject can exceed it.

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

`counters` and `runtime` are per process and reset on restart; `budget` is persistent. The endpoint reads the
budget store at most once every 5 s however often it is polled; it is still public, so restrict it in nginx to the
status system if the numbers should not be visible.

Once the day's budget is spent a request is refused **before** retrieval (a read-only look at the counter); the
atomic reservation just before the upstream call remains the only thing that grants a call.

Ways the counter can start again within a day: deleting `state/ai_guard/` and restarting the process, or moving the
system clock to another UTC date. Deleting the file without a restart fails closed. Rotating `AI_SESSION_KEY`
resets the per-subject counts (the hash changes), never the global one.

## 4. What is a hard spending limit and what is not

| Thing | Hard limit? |
| --- | --- |
| `AI_DAILY_REQUEST_BUDGET` in this service | **Yes**, for calls made by this service: at most N upstream calls per UTC day, each ≤ 24,000 prompt characters and one attempt. |
| Rate limit, per-subject cap, concurrency, dedup, cache, session | No. They shape and reduce load and depend on the client subject, which is shared or spoofable until the host is fixed (§3 Admission). The budget is what stops it. |
| APIS-side quotas, key-pool limits, Google budget alerts | **Not ours and not a stop we control.** Alerts notify; they do not block. |
| Anything a browser sends straight to the gateway | **Outside this service entirely** — see residual risk R1. |

The budget counts calls to the gateway, not model requests and not tokens. If the gateway turns one call into
several model requests, the real multiple is theirs (question below).

## 5. Exact production env to keep today's functionality

Production currently sets no `AI_*` variable, so deploying this code as-is turns AI **off** — which is the
recommended first release (§12). `scripts/deploy_vps.sh` currently passes no `AI_*` variable and republishes
`-p 8080:8080` on every run (review M2; that script is owned by the release workstream), so anything set by hand is
lost at the next deploy until the script carries it. To turn AI on later (full checklist in §13), the container needs:

```
AI_ENABLED=1
AI_SESSION_KEY=<random, ≥ 32 chars, e.g. `openssl rand -base64 48`; keep it in the existing secret store>
AI_DAILY_REQUEST_BUDGET=<owner's number>
```

Strongly recommended at the same time:

```
AI_TRUSTED_PROXIES=<bridge gateway>/32   # ONLY after :8080 is bound to 127.0.0.1 — see §3 Admission; otherwise leave unset
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
cd <worktree>
AI_TEST_TIMEOUT_SEC=30 timeout 180 <venv>/bin/python -m unittest discover -s tests -p 'test_ai_*.py'      # 105 tests
node --test --test-timeout=60000 tests/ai_client.test.mjs tests/ai_legacy_client.test.mjs               # 15 tests
```

- `tests/test_ai_guard_core.py` — config, subject, token, limiter, budget (restart, date change, threads, fail closed,
  refusal before retrieval), upstream (no retry after timeout, deadline, byte cap), dedup, waiter cancellation,
  concurrency, cache, logs, citations, hostile inputs.
- `tests/test_ai_guard_http.py` — the router over ASGI with fake builders: 503 / 403 / 401 / 411 / 413 / 415 / 429,
  cookie attributes, subject binding, budget stop, old-client replies, the gaokao shim, clean 4xx / 500 bodies.
- `tests/test_ai_main_integration.py` — the real `backend/main.py` app against an empty temp `DATA_ROOT`, synthetic
  retrieval, counting upstream. It can never load a model: `faiss`, `sentence_transformers`, `torch` and
  `transformers` are made un-importable for the import of `main.py`, the vector and reranker switches are off, the
  Hugging Face libraries are set offline, and the module skips with a message if `main.py` still reports a loaded
  index or model. Verified under an interpreter that has all of those installed.
- `tests/test_ai_static.py` — no model host / race / auto-trigger in served JS and HTML; main.py wiring. Sources are
  scanned line by line; a file with a line over 20,000 characters is refused up front.
- `tests/ai_client.test.mjs` — the browser module under a fake `fetch`: one request, marker header, cancel, no retry,
  bounded history.
- `tests/ai_legacy_client.test.mjs` — the **real old scripts** (`git show 035d2d4:frontend/assets/{app,dict}.js`) run
  against the replies in `tests/fixtures/ai_legacy_replies.json`, which the Python tests assert the server produces.
- `tests/ai_test_timeout.py` — every Python test gets a per-test limit: stacks of all threads are dumped and the test
  errors at the limit (`SIGALRM`), with a process-exit backstop 15 s later. Node tests carry per-test timeouts.

Only `fastapi` and `httpx` are needed. No test opens a socket.

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

- **R1 — the gateway still trusts the browser origin.** Anyone can POST to the gateway with
  `Origin: https://sun.bdfz.net`, exactly as the old page did, and none of the controls here see those calls. Tabs
  opened before the release keep running the old script; §11 describes what the server now does about them and what
  it cannot do. **Only a gateway-side change closes this completely** (question 4); the gateway is a shared hub, so
  that change needs its own fan-out. Until then the daily budget here limits this service, not SUN-attributed spend.
- **R2 — admission is friction, not identity.** One address can obtain a token and spend up to the rate limit; many
  addresses can together spend the whole daily budget and deny AI to everyone until UTC midnight. That is the designed
  failure mode: bounded cost, degraded feature.
- **R3 — client address behind Docker.** See "Subject keying on the production host" in §3: nothing is trusted by
  default (whole site = one bucket, safe for cost); no proxy may be trusted while 8080 is published on all
  interfaces; the budget is the hard stop either way.
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
- **R11 — status endpoint is public.** It exposes counts and limits only and reads storage at most once per 5 s; if
  the numbers should not be public, restrict it in nginx.
- **R12 — gaokao wording.** The gaokao prompt is now built on the server from the stored question text (first 600
  characters) instead of the text the browser rendered; the instructions are the same, the excerpt can differ slightly.
- **R13 — deploy script.** `scripts/deploy_vps.sh` warmed the reranker through `/api/chat/context`; that route no
  longer runs retrieval, so the warm-up now calls `/api/search`. That writes one ordinary `search_logs` row per deploy
  and no longer exercises the reranker (review m9); the health gate on `reranker.loaded` is unchanged. The script
  belongs to the release workstream; confirm in the stage-5 rehearsal.
- **R14 — one shared rate bucket until the host is fixed.** With nothing trusted (the default) one client can use
  up the site-wide 10 / min and make other visitors see 429 on AI. Cost stays bounded; availability does not.
- **R15 — container restart.** While the app is down nginx answers 502 and an old tab's fallback fires (§11).

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

## 11. Tabs that were open before the release (old script)

The old page has no version check, no polling and no service worker, so the server cannot make it reload. What the
old script does on its own, read from `035d2d4`:

| Old path | What the old script does | Server reply now | Result (test) |
| --- | --- | --- | --- |
| Main chat, precision query — including the **automatic** trigger 120 ms after results | `POST /api/chat`; on any non-2xx it immediately calls the gateway from the browser | `200`, old response shape, answer 「页面已更新，请刷新后再使用 AI 解答。」 | Accepted as the server's answer; **no gateway call** |
| Main chat, other queries | `POST /api/chat` and `POST /api/chat/context` together; after 8 s it builds a prompt from the context and calls the gateway **regardless of the server's answer** | chat: as above. context: `200 {"history_text": 1, …}` | The old prompt builder starts with `(context.history_text \|\| '').trim()`; a number there throws inside the fallback before its gateway call. **No gateway call**; the page shows the fixed answer |
| Gaokao "AI 关联分析" button | `GET /api/gaokao/link?question_id=N&limit=5`, then a browser-authored prompt straight to the gateway. It throws on a non-2xx link reply | `410` for exactly that request when it comes from one of our pages | The button shows "请求失败"; **no gateway call** |
| Dictionary page (automatic on every lookup) | `POST /api/dict/chat` only; no gateway code exists in old `dict.js` | `200` with the same fixed answer | Shown as the AI answer; never reached the gateway anyway |
| `POST /api/chat/log` | Sent only after a browser-direct answer; result ignored | `410` (unchanged) | Cannot trigger anything |

How the server recognises the old script: the guarded client sends `X-Sun-AI-Client: 2`; a `POST` to `/api/chat` or
`/api/dict/chat` without it is treated as old. The reply is given before the enabled check — so it also holds in the
AI-off release, where a `503` would otherwise trigger the same fallback — and before origin, session and rate
checks. It reads no body, opens no storage, runs no retrieval, makes no model call and spends no budget. It is never
`429`: any refusal would re-open the fallback, so its cost is bounded by being constant-time, not by rejection
(nginx `limit_req` may still be put in front; a 429 from nginx to an old tab has the same effect as before the fix).
Counters `legacy_client` and `legacy_gaokao_blocked` in `/api/ai/status` show how many such requests arrive, which
tells when old tabs have died out.

The gaokao rule matches only `limit=5` (the live link panel asks for 10; the server-side AI builder does not use
HTTP) **and** a same-origin browser signal (`Sec-Fetch-Site: same-origin`, or a `Referer` on our origin). Scripts,
the address bar and other API consumers are not affected. `AI_LEGACY_GAOKAO_SHIM=0` turns the rule off. A static
test fails if the live page ever starts requesting `limit=5`.

Evidence: `tests/ai_legacy_client.test.mjs` runs the unmodified old scripts and records every `fetch`. With the
replies above: zero non-same-origin requests on all three main-page paths. The same harness with the un-mitigated
replies (401 / 410 / link 200) records `https://ai.bdfz.net/` on all three — the control that shows the test can see it.

**What remains uncovered — nothing on this server can stop these:**

1. An old tab whose request never receives our reply: the app is restarting (nginx 502), the request times out, the
   network drops, or nginx itself answers 429 / 5xx. For main chat that means the fallback fires; in non-precision
   mode it is enough for the `/api/chat/context` request alone to fail (test "NOT covered").
2. An old gaokao click from a browser that sends neither `Sec-Fetch-Site` nor a `Referer` (very old browsers, or a
   referrer-stripping extension).
3. Any script or person calling the gateway directly with the SUN origin — no page of ours is involved at all.
4. These replies are tied to what the `035d2d4` script does. They are worth keeping until `legacy_client` stays at
   zero, then can be removed.

Only a gateway-side change closes the browser path completely: the gateway (APIS, a shared hub) must stop answering
browser-origin calls for `https://sun.bdfz.net` — drop that origin's CORS allowance or require the internal caller
token for the SUN project. The old request is a JSON `POST`, so a failed preflight means the model call is never
sent. That change is outside this repository and needs the shared-hub fan-out.

## 12. Release with AI disabled (recommended first release)

Exact AI env for this state: **none.** Do not set `AI_ENABLED`, `AI_SESSION_KEY`, `AI_DAILY_REQUEST_BUDGET`,
`AI_TRUSTED_PROXIES` or any other `AI_*` variable.

What the site then does:

- Guarded client: `POST /api/ai/session`, `/api/chat`, `/api/dict/chat`, `/api/gaokao/chat` → `503 AI_DISABLED`; the
  first click on an AI button shows 「AI 解读当前未开启。」. No model call can be made by this service; the budget file
  is never created.
- Old tabs: the fixed "请刷新" replies and the gaokao rule of §11 are active (they do not depend on enablement).
- `GET /api/ai/status` → `{"enabled": false, "missing": ["AI_ENABLED", "AI_SESSION_KEY", "AI_DAILY_REQUEST_BUDGET"], "budget": null}`.
- Search, graph, gaokao lists and link panel, analytics, dictionary lookup: unchanged.

Acceptance for this state, none of which calls a model: status as above; a marked `POST /api/chat` returns 503; an
unmarked one returns 200 with the fixed text; `/assets/ai-client.js` is served and all four pages reference
`?v=20261010a`.

Not AI settings, but part of the same safe state per the review: the `-p 127.0.0.1:8080:8080` publish, the release
tooling fix (B1) and the deploy script carrying its configuration (M2). Those belong to the release workstream.

## 13. Enabling later — checklist

In this order; each line is a precondition for the next.

1. Owner decisions recorded: AI on, the daily budget number, the per-subject cap, the named cost owner (§10). For
   scale, the review puts the gateway's per-site share at about 204 calls a day.
2. Host: port 8080 published on loopback only (`-p 127.0.0.1:8080:8080` in both `docker run` blocks). Confirm on the
   host which peer address the container sees for nginx.
3. nginx: `proxy_set_header X-Real-IP $remote_addr;` (overwrite), no `proxy_ignore_client_abort on`, and `limit_req`
   on `/api/ai/`, `/api/chat`, `/api/dict/chat`, `/api/gaokao/chat` (none exists today).
4. Deploy script carries the variables (env file outside the repository or explicit pass-through); otherwise the
   next deploy silently turns AI off and drops the proxy trust.
5. Container env:
   - `AI_ENABLED=1`
   - `AI_SESSION_KEY=<random, 32+ characters, from the secret store>`
   - `AI_DAILY_REQUEST_BUDGET=<owner's number>`
   - `AI_SUBJECT_DAILY_CAP=<n>` — recommended; without it one address can spend the whole day's budget in
     budget ÷ 10 minutes
   - `AI_TRUSTED_PROXIES=<bridge gateway>/32` — only with step 2 done
   - `AI_INTERNAL_TOKEN=<agreed with the APIS owner>`, `AI_SERVICE_PROJECT=sun.bdfz.net`
   - leave `AI_SERVICE_RETRIES` unset (0)
6. Gateway owner: answers to §8, and the browser path closed for SUN (§11) or an explicit decision to accept it.
7. Bounded acceptance with the real model, by the owner: a handful of questions on each of the three endpoints,
   then check `/api/ai/status` (`budget.count` equals the number of answers that were not cache hits) and the new
   `ai_chat_logs` rows (empty `query` / `user_message`).
8. Status system reads `/api/ai/status`.

Turning AI off again: remove `AI_ENABLED` (or set it to anything but `1`) and re-create the container.

## 14. Review dispositions (`REVIEW-DELIVERY-AI.md`, AI items)

| Item | Disposition |
| --- | --- |
| Old tabs | Done and extended: precision path, the 8 s normal-mode path and the gaokao button are all stopped server-side for tabs that reach us (§11). Remainder documented; gateway change still required. |
| m1 a–d (four inputs → 500) | Fixed: ASCII-only digit checks for the token expiry, `Content-Length` and upstream `Retry-After`; `RecursionError` → 400 `bad_json`; lone surrogates → 400 `bad_request` (and the hashing helpers no longer raise); a final catch returns a clean `500 internal_error` body. Tests added. |
| m6 (retrieval before budget check) | Fixed: refused before retrieval once the budget is spent or the store is unavailable. |
| m7 (`ensure_chat_log_columns` at import) | Fixed: wrapped; startup no longer depends on altering the table. |
| m10 (public status endpoint) | Partly: storage read at most once per 5 s. Restricting access is an nginx / owner decision (R11). |
| N3 (dedup variants) | Partly: invisible format characters (zero-width, BOM, bidi marks) no longer make a new call. Case and trailing punctuation are left alone — folding them can change the question, and the budget bounds the effect. |
| N4, N5, N6 | Documented (§3 Budget, §2 / §3 Admission, §4). |
| M1 (subject keying) | AI side: safe default kept and documented (§3); no code change needed. The loopback publish is the release workstream's. |
| M2, m9 (deploy script) | Not changed here: `scripts/deploy_vps.sh` is owned by the release workstream. Noted in §5 and R13. |
| Hung-test hazards | Fixed: line-based scan, model-proof integration test, per-test timeouts (§7). |
| B1, m2–m5, m8, N1, N2, N7–N13 | Library / release items, outside the AI area. |
