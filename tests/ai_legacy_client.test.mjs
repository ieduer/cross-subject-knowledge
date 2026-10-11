// Runs the REAL pre-guard browser scripts (git show 035d2d4:frontend/assets/{app,dict}.js)
// against a stub that answers exactly as the guarded server now does (tests/fixtures/ai_legacy_replies.json),
// and proves that an old open tab no longer reaches any non-same-origin host for the covered paths.
// A control run with the un-mitigated replies (401 / 410 / link 200) shows the same harness DOES
// see the browser-direct gateway call, so the assertions are not vacuous.
// No network: fetch is a recorder. Run: node --test --test-timeout=60000 tests/ai_legacy_client.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const OLD_REV = '035d2d4';
const TIME_SCALE = 100;           // old script waits 8 s before its browser fallback → 80 ms here
const SETTLE_MS = 600;            // > (8 s fallback delay + 15 s context timeout… scaled) for the paths under test
const TEST_OPTS = { timeout: 30000 };

function oldSource(path) {
    try {
        return execFileSync('git', ['-C', ROOT, 'show', `${OLD_REV}:${path}`], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024, timeout: 15000 });
    } catch (_) {
        return null;
    }
}
const OLD_APP = oldSource('frontend/assets/app.js');
const OLD_DICT = oldSource('frontend/assets/dict.js');
const SKIP = OLD_APP && OLD_DICT ? false : `git object ${OLD_REV} is not available in this checkout`;
const REPLIES = JSON.parse(readFileSync(new URL('./fixtures/ai_legacy_replies.json', import.meta.url), 'utf8'));
const UNMITIGATED = {
    chat: { status: 401, body: { detail: 'AI 会话已过期，请重试。', code: 'AI_SESSION_REQUIRED' } },
    dict: { status: 401, body: { detail: 'AI 会话已过期，请重试。', code: 'AI_SESSION_REQUIRED' } },
    context: { status: 410, body: { detail: '该接口已停用。', code: 'retired' } },
    log: REPLIES.log,
    gaokao_link: null,            // i.e. the route answers normally
};

// A DOM stand-in: every property is another stub, every call returns a stub, strings are ''.
function stub() {
    const store = new Map();
    const target = function () {};
    return new Proxy(target, {
        get(_, key) {
            if (key === Symbol.toPrimitive) return () => '';
            if (key === Symbol.iterator) return function* () {};
            if (key === 'then' || key === 'toJSON') return undefined;
            if (key === 'length') return 0;
            if (!store.has(key)) store.set(key, stub());
            return store.get(key);
        },
        set(_, key, value) { store.set(key, value); return true; },
        has() { return true; },
        apply() { return stub(); },
        construct() { return stub(); },
    });
}

const SEARCH = {
    total: 2, cross_hint: '', query_analysis: null, subject_counts: { 物理: 1, 化学: 1 },
    groups: [
        { subject: '物理', count: 1, icon: '⚛', color: '#333', results: [{ id: 1, title: '必修第一册', subject: '物理', book_key: 'bk', section: 87, logical_page: 92, snippet: '牛顿第二定律', text: '牛顿第二定律', source: 'mineru', matched_concepts: [] }] },
        { subject: '化学', count: 1, icon: '⚗', color: '#444', results: [{ id: 2, title: '必修第一册', subject: '化学', book_key: 'bk2', section: 12, logical_page: 14, snippet: '能量', text: '能量', source: 'mineru', matched_concepts: [] }] },
    ],
};
const GAOKAO_LINK = { question_id: 12, question_title: 't', question_subject: '物理', question_year: 2024, matched_concepts: [], links: [], cross_links: [], precomputed_analysis: null };

function boot(source, replies) {
    const calls = [];
    const respond = (reply, fallbackBody = {}) => new Response(JSON.stringify(reply ? reply.body : fallbackBody), { status: reply ? reply.status : 200, headers: { 'Content-Type': 'application/json' } });
    const fetchStub = async (url, options = {}) => {
        url = String(url);
        const method = (options.method || 'GET').toUpperCase();
        calls.push({ url, method });
        if (/^https?:/i.test(url)) return new Response(JSON.stringify({ answer: 'MODEL ANSWER FROM THE GATEWAY' }), { status: 200 });
        const path = url.split('?')[0];
        if (path === '/api/chat') return respond(replies.chat);
        if (path === '/api/chat/context') {
            // A failing context reply is delivered after the (scaled) 8 s fallback delay: the old script only
            // awaits this promise then, and an earlier rejection would be reported by node as unhandled.
            if (replies.context === 'network-error') { await realDelay(150); throw new TypeError('network'); }
            if (replies.context.status >= 400) await realDelay(150);
            return respond(replies.context);
        }
        if (path === '/api/chat/log') return respond(replies.log);
        if (path === '/api/dict/chat') return respond(replies.dict);
        if (path === '/api/search') return respond(null, SEARCH);
        if (path === '/api/gaokao/link') return /[?&]limit=5(&|$)/.test(url) && replies.gaokao_link ? respond(replies.gaokao_link) : respond(null, GAOKAO_LINK);
        if (path.startsWith('/api/dict/')) return respond(null, { entries: [], results: [{ title: '必修上', text: '之，往也', snippet: '之', logical_page: 3 }], available: true });
        if (/\/api\/(books|keywords|related|search\/trending|gaokao\/years)$/.test(path)) return respond(null, []);
        return respond(null, {});
    };
    const sandbox = {
        document: stub(), window: stub(), navigator: stub(), location: stub(), history: stub(), localStorage: stub(),
        d3: stub(), katex: stub(), renderMathInElement: () => {}, IntersectionObserver: stub(), requestAnimationFrame: () => 0,
        fetch: fetchStub, AbortController, URLSearchParams, URL, Response, console: { log() {}, warn() {}, error() {}, info() {} },
        setTimeout: (fn, ms = 0, ...args) => setTimeout(fn, ms / TIME_SCALE, ...args), clearTimeout,
        setInterval: () => 0, clearInterval() {},
    };
    sandbox.globalThis = sandbox;
    const context = vm.createContext(sandbox);
    vm.runInContext(source, context, { filename: 'old-script.js' });
    return { calls, run: code => vm.runInContext(code, context) };
}

const realDelay = ms => new Promise(resolve => setTimeout(resolve, ms));
const settle = (ms = SETTLE_MS) => new Promise(resolve => setTimeout(resolve, ms));
const external = calls => calls.filter(call => /^https?:/i.test(call.url)).map(call => call.url);
const posts = (calls, path) => calls.filter(call => call.method === 'POST' && call.url.split('?')[0] === path).length;
const lastAnswer = page => page.run('aiConversation.filter(m => m.role === "assistant").map(m => m.content).pop() || ""');

test('old main page, precision query (auto-trigger): server reply is accepted, no browser-direct call', { ...TEST_OPTS, skip: SKIP }, async () => {
    const page = boot(OLD_APP, REPLIES);
    await page.run('doSearch("牛顿第二定律")');           // old script auto-fires AI 120 ms after results
    await settle();
    assert.equal(posts(page.calls, '/api/chat'), 1, 'the old auto-trigger really ran');
    assert.deepEqual(external(page.calls), []);
    assert.equal(lastAnswer(page), REPLIES.chat.body.answer);
    assert.equal(posts(page.calls, '/api/chat/log'), 0);
    assert.equal(page.run('aiRequestPending'), false);
});

test('old main page, normal query (8 s Promise.any fallback): poisoned context stops the direct call', { ...TEST_OPTS, skip: SKIP }, async () => {
    const page = boot(OLD_APP, REPLIES);
    await page.run('doSearch("能量")');
    await settle(50);
    assert.equal(posts(page.calls, '/api/chat'), 0, 'not a precision query: nothing automatic');
    await page.run('requestAISynthesis()');               // the old "AI 教材解读" button
    assert.equal(lastAnswer(page), REPLIES.chat.body.answer);
    await settle();                                        // well past the scaled 8 s fallback delay
    assert.equal(posts(page.calls, '/api/chat'), 1);
    assert.equal(posts(page.calls, '/api/chat/context'), 1);
    assert.deepEqual(external(page.calls), []);
    // follow-up typed into the old box behaves the same
    await page.run('sendAIMessage("再讲讲")');
    await settle();
    assert.equal(posts(page.calls, '/api/chat'), 2);
    assert.deepEqual(external(page.calls), []);
});

test('old gaokao "AI 关联分析" button: the limit=5 link request is refused, so the direct call never starts', { ...TEST_OPTS, skip: SKIP }, async () => {
    const page = boot(OLD_APP, REPLIES);
    await page.run('document.getElementById = () => ({ innerHTML: "", classList: { toggle() {}, add() {}, remove() {} }, querySelectorAll: () => [] }); requestGaokaoAI(12, { disabled: false, textContent: "" })');
    await settle();
    assert.equal(page.calls.filter(call => call.url.includes('/api/gaokao/link') && call.url.includes('limit=5')).length, 1);
    assert.deepEqual(external(page.calls), []);
    // the old link PANEL (limit=10) is a different request and still works
    assert.match(OLD_APP, /api\/gaokao\/link\?question_id=\$\{questionId\}&limit=10/);
    assert.equal((OLD_APP.match(/gaokao\/link\?question_id=\$\{questionId\}&limit=5/g) || []).length, 1);
});

test('control: with the un-mitigated replies the same harness sees the gateway calls', { ...TEST_OPTS, skip: SKIP }, async () => {
    const precision = boot(OLD_APP, UNMITIGATED);
    await precision.run('doSearch("牛顿第二定律")');
    await settle();
    assert.ok(external(precision.calls).some(url => url.startsWith('https://ai.bdfz.net')), 'precision: 401 falls straight through');

    const normal = boot(OLD_APP, UNMITIGATED);
    await normal.run('doSearch("能量")');
    await normal.run('requestAISynthesis()');
    await settle();
    assert.ok(external(normal.calls).some(url => url.startsWith('https://ai.bdfz.net')), 'normal: 8 s fallback fires');

    const gaokao = boot(OLD_APP, UNMITIGATED);
    await gaokao.run('document.getElementById = () => ({ innerHTML: "", classList: { toggle() {}, add() {}, remove() {} }, querySelectorAll: () => [] }); requestGaokaoAI(12, { disabled: false, textContent: "" })');
    await settle();
    assert.ok(external(gaokao.calls).some(url => url.startsWith('https://ai.bdfz.net')), 'gaokao: direct call after a 200 link reply');
});

test('NOT covered: old tab whose /api/chat/context request fails at network level (normal mode)', { ...TEST_OPTS, skip: SKIP }, async () => {
    // Documented residual: if the context request never gets our reply, the old script builds
    // its own context and calls the gateway after 8 s. Only the gateway can refuse that.
    const page = boot(OLD_APP, { ...REPLIES, context: 'network-error' });
    await page.run('doSearch("能量")');
    await page.run('requestAISynthesis()');
    await settle();
    assert.ok(external(page.calls).length >= 1, 'this path still reaches the gateway — stated in docs/AI_GUARD.md');
});

test('old dictionary page: no gateway host in the script at all; the fixed reply is shown as the answer', { ...TEST_OPTS, skip: SKIP }, async () => {
    assert.doesNotMatch(OLD_DICT, /https?:\/\/[^\s'"`]*(bdfz\.net|workers\.dev)/);
    assert.doesNotMatch(OLD_DICT, /requestDirectChat|Promise\.any|AI_API/);
    const page = boot(OLD_DICT, REPLIES);
    await page.run('runSearch("之")');                     // old script auto-sends /api/dict/chat after every lookup
    await settle(200);
    assert.equal(posts(page.calls, '/api/dict/chat'), 1);
    assert.deepEqual(external(page.calls), []);
    assert.equal(page.run('state.chatHistory.filter(m => m.role === "assistant").map(m => m.content).pop()'), REPLIES.dict.body.answer);
});
