// Behavioural test of frontend/assets/ai-client.js with a fake fetch. No network.
// Run: node --test --test-timeout=60000 tests/ai_client.test.mjs
import baseTest from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// Every test has a hard timeout: a hang fails loudly instead of blocking the run.
const test = (name, fn) => baseTest(name, { timeout: 20000 }, fn);

const source = readFileSync(fileURLToPath(new URL('../frontend/assets/ai-client.js', import.meta.url)), 'utf8');

function load(handler) {
    const calls = [];
    const sandbox = {
        window: {}, AbortController, DOMException, setTimeout, clearTimeout, Date, Number, Object, Array, JSON, Error, Promise,
        fetch: (url, options = {}) => {
            calls.push({ url, options });
            return handler(url, options, calls);
        },
    };
    vm.runInNewContext(source, sandbox);
    return { SunAI: sandbox.window.SunAI, calls };
}

const json = (status, body, headers = {}) => ({
    ok: status >= 200 && status < 300, status, json: async () => body, headers: { get: name => headers[name] ?? null },
});
const sessionOk = () => json(200, { ok: true, expires_at: Date.now() / 1000 + 1800, limits: { max_history_items: 6, max_history_item_chars: 1200, max_message_chars: 800 } });
const abortable = (options) => new Promise((_, reject) => {
    options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
});

test('one user action = one admission call + exactly one AI request, same-origin only', async () => {
    const { SunAI, calls } = load(url => (url === '/api/ai/session' ? sessionOk() : json(200, { answer: '好' })));
    const data = await SunAI.ask('/api/chat', { query: 'q', user_message: 'm' }).promise;
    assert.equal(data.answer, '好');
    assert.deepEqual(calls.map(c => c.url), ['/api/ai/session', '/api/chat']);
    for (const call of calls) {
        assert.equal(call.options.method, 'POST');
        assert.equal(call.options.credentials, 'same-origin');
        assert.equal(call.options.headers['X-Sun-AI-Client'], '2', 'guarded-client marker on every request');
        assert.equal(call.options.headers['Content-Type'], 'application/json');
        assert.ok(!/^https?:/.test(call.url));
    }
    await SunAI.ask('/api/chat', { query: 'q2', user_message: 'm' }).promise;
    assert.deepEqual(calls.map(c => c.url), ['/api/ai/session', '/api/chat', '/api/chat'], 'session is reused');
});

test('a failure is surfaced once and never retried', async () => {
    for (const [status, body] of [[504, { detail: 'AI 服务响应超时', code: 'ai_timeout' }], [503, { detail: '额度已用完', code: 'AI_BUDGET_EXHAUSTED' }], [500, {}]]) {
        const { SunAI, calls } = load(url => (url === '/api/ai/session' ? sessionOk() : json(status, body)));
        await assert.rejects(SunAI.ask('/api/chat', {}).promise, error => error.status === status && !error.cancelled);
        await new Promise(resolve => setTimeout(resolve, 20));
        assert.equal(calls.filter(c => c.url === '/api/chat').length, 1);
    }
});

test('network error is not retried', async () => {
    const { SunAI, calls } = load(url => (url === '/api/ai/session' ? sessionOk() : Promise.reject(new TypeError('failed to fetch'))));
    await assert.rejects(SunAI.ask('/api/dict/chat', {}).promise, error => error.code === 'network');
    assert.equal(calls.filter(c => c.url === '/api/dict/chat').length, 1);
});

test('cancel aborts the in-flight fetch', async () => {
    const { SunAI, calls } = load((url, options) => (url === '/api/ai/session' ? sessionOk() : abortable(options)));
    const request = SunAI.ask('/api/chat', {});
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(calls.length, 2);
    assert.equal(calls[1].options.signal.aborted, false);
    request.cancel();
    await assert.rejects(request.promise, error => error.cancelled === true && error.code === 'cancelled');
    assert.equal(calls[1].options.signal.aborted, true);
    assert.equal(calls.length, 2);
});

test('cancel before admission finishes sends no AI request at all', async () => {
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    const { SunAI, calls } = load(url => (url === '/api/ai/session' ? gate.then(sessionOk) : json(200, { answer: 'x' })));
    const request = SunAI.ask('/api/chat', {});
    request.cancel();
    release();
    await assert.rejects(request.promise, error => error.cancelled === true);
    assert.deepEqual(calls.map(c => c.url), ['/api/ai/session']);
});

test('disabled or refused admission never reaches an AI endpoint', async () => {
    const { SunAI, calls } = load(() => json(503, { detail: 'AI 解读当前未开启。', code: 'AI_DISABLED' }));
    await assert.rejects(SunAI.ask('/api/chat', {}).promise, error => error.code === 'AI_DISABLED' && error.message === 'AI 解读当前未开启。');
    assert.deepEqual(calls.map(c => c.url), ['/api/ai/session']);
});

test('401 is reported (no silent resend); the next user action re-admits', async () => {
    let chatCalls = 0;
    const { SunAI, calls } = load(url => {
        if (url === '/api/ai/session') return sessionOk();
        chatCalls += 1;
        return chatCalls === 1 ? json(401, { detail: 'AI 会话已过期，请重试。', code: 'AI_SESSION_REQUIRED' }) : json(200, { answer: 'ok' });
    });
    await assert.rejects(SunAI.ask('/api/chat', {}).promise, error => error.status === 401);
    assert.deepEqual(calls.map(c => c.url), ['/api/ai/session', '/api/chat']);
    assert.equal((await SunAI.ask('/api/chat', {}).promise).answer, 'ok');
    assert.deepEqual(calls.map(c => c.url), ['/api/ai/session', '/api/chat', '/api/ai/session', '/api/chat']);
});

test('429 message carries the wait', async () => {
    const { SunAI } = load(url => (url === '/api/ai/session' ? sessionOk() : json(429, { detail: 'x', code: 'rate_limited', retry_after: 12 })));
    await assert.rejects(SunAI.ask('/api/chat', {}).promise, error => error.retryAfter === 12 && error.message.includes('12 秒'));
});

test('history sent is bounded in count and per-message length, without error/pending rows', () => {
    const { SunAI } = load(() => sessionOk());
    const messages = [];
    for (let i = 0; i < 20; i += 1) {
        messages.push({ role: i % 2 ? 'assistant' : 'user', content: `${i}:` + 'x'.repeat(5000), sources: [1, 2, 3], contextSummary: { big: 'y'.repeat(9999) } });
    }
    messages.push({ role: 'assistant', content: '请求失败：超时', isError: true });
    messages.push({ role: 'assistant', content: '思考中…', pending: true });
    messages.push({ role: 'system', content: 'nope' });
    const history = SunAI.boundedHistory(messages);
    assert.equal(history.length, 6);
    assert.ok(history.every(item => item.content.length <= 1200));
    assert.ok(history.every(item => Object.keys(item).join() === 'role,content'));
    assert.equal(history[5].content.startsWith('19:'), true);
    assert.deepEqual(JSON.parse(JSON.stringify(SunAI.boundedHistory(null))), []);
});
