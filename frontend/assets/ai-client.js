/* ── Guarded AI client (shared by app.js and dict.js) ──
 *
 * One explicit user action  →  exactly one same-origin POST to this site's own server.
 * The browser never talks to a model gateway, never races a second request,
 * never retries by itself, and can always cancel.
 */
(function () {
    'use strict';

    const SESSION_PATH = '/api/ai/session';
    // Marks requests from this (guarded) client. The server answers marker-less chat
    // requests — pages still running the pre-guard script — with a fixed "please reload" reply.
    const CLIENT_HEADERS = { 'Content-Type': 'application/json', 'X-Sun-AI-Client': '2' };
    const CLIENT_TIMEOUT_MS = 75000;
    const SESSION_REFRESH_MARGIN_SEC = 60;
    const limits = {
        max_message_chars: 800,
        max_history_items: 6,
        max_history_item_chars: 1200,
    };
    let sessionExpiresAt = 0;
    let sessionPromise = null;

    class AIError extends Error {
        constructor(message, { code = '', status = 0, retryAfter = 0, cancelled = false } = {}) {
            super(message);
            this.name = 'AIError';
            this.code = code;
            this.status = status;
            this.retryAfter = retryAfter;
            this.cancelled = cancelled;
        }
    }

    async function errorFromResponse(response) {
        let data = null;
        try {
            data = await response.json();
        } catch (_) {
            data = null;
        }
        const code = data && typeof data.code === 'string' ? data.code : '';
        const retryAfter = Number((data && data.retry_after) || response.headers.get('Retry-After') || 0) || 0;
        let message = data && typeof data.detail === 'string' && data.detail.trim()
            ? data.detail.trim()
            : `AI 解读暂不可用 (${response.status})`;
        if (response.status === 429 && retryAfter > 0 && code === 'rate_limited') {
            message = `请求过于频繁，请 ${retryAfter} 秒后再试。`;
        }
        return new AIError(message, { code, status: response.status, retryAfter });
    }

    // Admission is a cheap same-origin call that sets an HttpOnly cookie. It never reaches a model.
    async function ensureSession() {
        if (Date.now() / 1000 < sessionExpiresAt - SESSION_REFRESH_MARGIN_SEC) return;
        if (!sessionPromise) {
            sessionPromise = (async () => {
                const response = await fetch(SESSION_PATH, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: CLIENT_HEADERS,
                    body: '{}',
                });
                if (!response.ok) throw await errorFromResponse(response);
                const data = await response.json();
                sessionExpiresAt = Number(data.expires_at) || 0;
                if (data.limits && typeof data.limits === 'object') Object.assign(limits, data.limits);
            })().finally(() => {
                sessionPromise = null;
            });
        }
        await sessionPromise;
    }

    /**
     * Start one AI request. Returns { promise, cancel }.
     * A failure is reported to the caller and is never retried here;
     * a new request only happens when the user asks again.
     */
    function ask(path, payload) {
        const controller = new AbortController();
        let timedOut = false;
        const timer = setTimeout(() => {
            timedOut = true;
            controller.abort();
        }, CLIENT_TIMEOUT_MS);

        const promise = (async () => {
            try {
                await ensureSession();
                if (controller.signal.aborted) {
                    throw new DOMException('aborted', 'AbortError');
                }
                const response = await fetch(path, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: CLIENT_HEADERS,
                    body: JSON.stringify(payload),
                    signal: controller.signal,
                });
                if (response.status === 401) sessionExpiresAt = 0;
                if (!response.ok) throw await errorFromResponse(response);
                return await response.json();
            } catch (error) {
                if (error && error.name === 'AbortError') {
                    throw timedOut
                        ? new AIError('等待超时，已停止本次请求。', { code: 'client_timeout' })
                        : new AIError('已取消本次 AI 解答。', { code: 'cancelled', cancelled: true });
                }
                if (error instanceof AIError) throw error;
                throw new AIError('网络异常，AI 解读未完成。', { code: 'network' });
            } finally {
                clearTimeout(timer);
            }
        })();

        return { promise, cancel: () => controller.abort() };
    }

    /** History actually sent: capped in count AND per-message length; error / pending rows never go upstream. */
    function boundedHistory(messages) {
        return (Array.isArray(messages) ? messages : [])
            .filter(item => item
                && !item.isError
                && !item.pending
                && (item.role === 'user' || item.role === 'assistant')
                && typeof item.content === 'string'
                && item.content.trim())
            .slice(-limits.max_history_items)
            .map(item => ({
                role: item.role,
                content: item.content.trim().slice(0, limits.max_history_item_chars),
            }));
    }

    window.SunAI = {
        ask,
        boundedHistory,
        AIError,
        get maxMessageChars() {
            return limits.max_message_chars;
        },
    };
})();
