/* ── Cross-Subject Knowledge Platform · Frontend ── */

const API = '';  // same origin
const PHASE = document.body.dataset.phase || '高中';
const DISPLAY_PHASE = PHASE;
window._subjectMeta = {};
// AI runs only on an explicit user action and only through this site's own server
// (assets/ai-client.js → POST /api/chat). The browser never contacts a model gateway.
const IMG_CDN = 'https://img.rdfzer.com';
const DEFAULT_FRONTEND_VERSION = '2026.11.01-r46';
const FRONTEND_VERSION_FILE = '/assets/version.json';
const AI_MEMORY_LIMIT = 12;
const DOWNLOADABLE_LIBRARY_BOOKS = 316;
let latestStats = null;
let aiProviderLabel = 'Gemini';

// ── AI: overview first, then conversation ─────────────────
// One explicit search → one guarded POST /api/overview through assets/ai-client.js.
// After that only the user's own send / chip / 重试 starts a request (POST /api/chat).
// The browser never contacts a model gateway. Answers are escaped first; the small
// Markdown subset and the citation buttons are built on the escaped text, and a
// citation only becomes clickable when it names a record the server retrieved.
const aiPanel = document.getElementById('ai-panel');
const aiContent = document.getElementById('ai-content');
const aiFollowupInput = document.getElementById('ai-followup-input');
const aiSendBtn = document.getElementById('ai-send-btn');
const aiCopyBtn = document.getElementById('ai-copy-btn');
const aiStarters = document.getElementById('ai-starters');
const aiPanelQuery = document.getElementById('ai-panel-query');
const aiFootnote = document.getElementById('ai-footnote');

let aiConversation = [];
let aiRequestPending = false;
let aiPendingKind = '';
let aiActiveRequest = null;      // { promise, cancel } of the single in-flight request
let aiConversationEpoch = 0;     // bumped on every reset; answers from an older epoch are dropped
let aiSearchStage = '';          // 'searching' while /api/search runs for the current conversation
let aiLocalDigest = '';          // retrieval-only summary (no AI), shown while the overview loads or fails
if (aiFollowupInput) aiFollowupInput.maxLength = 800;

if (aiSendBtn) aiSendBtn.addEventListener('click', () => requestAIFollowup());
if (aiCopyBtn) aiCopyBtn.addEventListener('click', () => copyAIConversation());
if (aiFollowupInput) {
    aiFollowupInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            requestAIFollowup();
        }
    });
}

function setSearchAIProviderLabel(label) {
    aiProviderLabel = label || 'Gemini';
    renderAIFootnote();
}

function renderAIFootnote() {
    if (!aiFootnote) return;
    aiFootnote.textContent = `AI 依据下方检索到的${getAISourceLabel()}原文生成，每段回答标出所用模型。点带下划线的出处可核对原文；AI 可能出错，以教材原文为准。`;
}

const AI_PRECISION_PATTERNS = [
    /什么是.+/,
    /.+是什么/,
    /.+的(?:定义|概念|本质|特点|条件|作用|过程|原因)/,
    /.+指什么/,
    /.+(?:定律|定理|法则|原理|公式)$/,
    /.+(?:有什么)?区别/,
    /.+和.+的区别/,
    /.+的(?:例子|实例)/,
    /.+为什么.+/,
];

function compactQueryText(text) {
    return String(text || '').replace(/\s+/g, '');
}

function trimPrecisionTarget(text) {
    let target = String(text || '').trim();
    if (!target) return '';
    const patterns = [
        /^请(?:直接)?(?:根据教材)?(?:回答|解释|说明)?/,
        /^(?:帮我|请你)?(?:只)?(?:在课本中|在教材中)?(?:找|查|说明|解释)?/,
        /(?:的定义|的概念|的本质)$/,
        /(?:是什么|指什么|是什么意思)$/,
    ];
    patterns.forEach(pattern => {
        target = target.replace(pattern, '');
    });
    target = target.replace(/^什么是/, '');
    target = target.replace(/[「」“”"'？?。！，,：:；;（）()\[\]{}]+/g, ' ');
    return target.replace(/\s+/g, ' ').trim();
}

function inferAIQueryProfile(query, userMessage = '') {
    const cleanQuery = String(query || '').trim();
    const cleanMessage = String(userMessage || '').trim();
    const queryLooksPrecision = AI_PRECISION_PATTERNS.some(pattern => pattern.test(compactQueryText(cleanQuery)));
    const messageLooksPrecision = AI_PRECISION_PATTERNS.some(pattern => pattern.test(compactQueryText(cleanMessage)));
    const combined = cleanMessage && (!cleanQuery || (!queryLooksPrecision && messageLooksPrecision)) ? cleanMessage : cleanQuery;
    const compact = compactQueryText(combined);
    let intent = 'lookup';
    let target = cleanQuery;

    const patterns = [
        ['definition', /^(?<target>.+?)的定义$/],
        ['definition', /^(?<target>.+?)的概念$/],
        ['definition', /^(?<target>.+?)是什么$/],
        ['definition', /^什么是(?<target>.+)$/],
        ['definition', /^(?<target>.+?)指什么$/],
        ['comparison', /^(?<target>.+?)(?:有什么)?区别$/],
        ['comparison', /^(?<target>.+?)和(?<other>.+?)的区别$/],
        ['example', /^(?<target>.+?)的例子$/],
        ['reason', /^(?<target>.+?)为什么.+$/],
        ['process', /^(?<target>.+?)的过程$/],
    ];
    for (const [nextIntent, pattern] of patterns) {
        const matched = compact.match(pattern);
        if (!matched) continue;
        intent = nextIntent;
        if (nextIntent === 'comparison' && matched.groups && matched.groups.other) {
            target = [trimPrecisionTarget(matched.groups.target || cleanQuery), trimPrecisionTarget(matched.groups.other || '')]
                .filter(Boolean)
                .join('和');
        } else {
            target = trimPrecisionTarget((matched.groups && matched.groups.target) || cleanQuery);
        }
        break;
    }

    if (intent === 'lookup' && compact.includes('定义')) {
        intent = 'definition';
        target = trimPrecisionTarget(compact.replace('定义', ''));
    } else if (intent === 'lookup' && (compact.includes('什么是') || compact.endsWith('是什么'))) {
        intent = 'definition';
        target = trimPrecisionTarget(compact.replace('什么是', '').replace('是什么', ''));
    } else if (intent === 'lookup' && /(?:定律|定理|法则|原理|公式)$/.test(compact)) {
        intent = 'definition';
        target = trimPrecisionTarget(cleanQuery);
    } else if (intent === 'lookup' && (compact.includes('区别') || compact.includes('不同'))) {
        intent = 'comparison';
        target = trimPrecisionTarget(cleanQuery);
    } else if (intent === 'lookup' && (compact.includes('例子') || compact.includes('实例'))) {
        intent = 'example';
        target = trimPrecisionTarget(cleanQuery);
    }

    const precisionMode = AI_PRECISION_PATTERNS.some(pattern => pattern.test(compact));
    return {
        query: cleanQuery,
        user_message: userMessage,
        intent,
        target: trimPrecisionTarget(target || cleanQuery) || cleanQuery,
        mode: precisionMode ? 'precision_agent' : 'cross_subject',
    };
}

function getCurrentAIScopePayload() {
    const scope = { scope_subject: '', book_key: '' };
    if (currentSubjectFilter) {
        scope.scope_subject = currentSubjectFilter;
    }
    const scopeValue = filterScope?.value || '';
    if (scopeValue.startsWith('book:')) {
        scope.book_key = scopeValue.slice('book:'.length);
        scope.scope_subject = '';
    } else if (scopeValue.startsWith('subject:') && !scope.book_key) {
        scope.scope_subject = scopeValue.slice('subject:'.length);
    }
    return scope;
}

function getCurrentAISubjectCount() {
    const counts = currentData?.subject_counts || currentOverviewData?.subject_counts || {};
    return Object.keys(counts).length;
}

function hasSearchResults() {
    return Boolean(currentData && Array.isArray(currentData.groups) && currentData.groups.length);
}

function getAISourceLabel() {
    const scope = getCurrentAIScopePayload();
    if (scope.book_key) return '当前单本教材';
    if (scope.scope_subject) return `${scope.scope_subject} · 全部教材`;
    const subjectCount = getCurrentAISubjectCount();
    return subjectCount >= 2 ? `${subjectCount} 个学科教材` : '教材原文';
}

function getAIExecutionKey() {
    const scope = getCurrentAIScopePayload();
    return [currentQuery, currentSubjectFilter || '', scope.scope_subject || '', scope.book_key || ''].join('|');
}

// The records the overview actually cited (else the results on screen): a follow-up keeps them as evidence.
function followupEvidenceIds() {
    const overview = aiConversation.find(msg => msg.kind === 'overview' && !msg.isError && Array.isArray(msg.sources));
    const ids = overview ? overview.sources.map(src => src.id) : overviewResultIds(currentData);
    return Array.from(new Set(ids))
        .filter(id => (Number.isInteger(id) && id > 0) || (typeof id === 'string' && /^supp:[0-9a-f]{16}$/.test(id)))
        .slice(0, 24);
}

function buildAIServerPayload(userMessage, history) {
    const scope = getCurrentAIScopePayload();
    return {
        query: currentQuery,
        user_message: userMessage,
        history,
        ids: followupEvidenceIds(),
        phase: PHASE,
        ...(scope.scope_subject ? { scope_subject: scope.scope_subject } : {}),
        ...(scope.book_key ? { book_key: scope.book_key } : {}),
    };
}

function describePrecisionIntent(intent) {
    return {
        definition: '定义',
        comparison: '区别',
        example: '典型例子',
        reason: '原因',
        process: '过程',
    }[intent] || '核心';
}

function isStructuralFallbackTerm(term, precisionProfile = null) {
    const compact = compactQueryText(term);
    if (!compact) return true;
    const target = compactQueryText(precisionProfile?.target || '');
    const structuralPatterns = [
        /^的(?:定义|概念|本质|特点|条件|作用|过程|原因)$/,
        /^定义$/,
        /^概念$/,
        /^本质$/,
        /^特点$/,
        /^条件$/,
        /^作用$/,
        /^过程$/,
        /^原因$/,
        /^什么是$/,
        /^是什么$/,
    ];
    if (structuralPatterns.some(pattern => pattern.test(compact))) return true;
    if (target && compact !== target && (compact === `${target}的` || compact === `的${target}`)) return true;
    return false;
}

// Ids of the results on screen, in display order: the top textbook passages of up to six
// subjects and the first exam rows. The server reloads exactly these rows for the overview.
function overviewResultIds(data) {
    const groups = sortedResultGroups(data);
    const ids = [];
    groups.slice(0, 6).forEach(g => {
        (g.results || []).filter(r => r.source !== 'gaokao').slice(0, 3).forEach(r => ids.push(r.id));
    });
    groups.forEach(g => {
        (g.results || []).filter(r => r.source === 'gaokao').forEach(r => ids.push(r.id));
    });
    return ids
        .filter(id => (Number.isInteger(id) && id > 0) || (typeof id === 'string' && /^supp:[0-9a-f]{16}$/.test(id)))
        .slice(0, 24);
}

function buildOverviewPayload() {
    const scope = getCurrentAIScopePayload();
    return {
        query: currentQuery,
        ids: overviewResultIds(currentData),
        phase: PHASE,
        ...(scope.scope_subject ? { scope_subject: scope.scope_subject } : {}),
        ...(scope.book_key ? { book_key: scope.book_key } : {}),
    };
}

function shortBookTitle(title) {
    return String(title || '').replace(/^(?:普通高中|义务教育)教科书[·・]/, '').trim();
}

// Retrieval-only digest of one search (no model call): what was found, where.
function buildLocalDigest(data) {
    const counts = data.subject_counts || {};
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    if (!entries.length) return '';
    const total = entries.reduce((sum, [, count]) => sum + count, 0);
    const groups = data.groups || [];
    const items = entries.slice(0, 6).map(([subject, count]) => {
        const group = groups.find(g => g.subject === subject);
        const results = group ? group.results || [] : [];
        const books = Array.from(new Set(results.filter(r => r.source !== 'gaokao').map(r => shortBookTitle(r.display_title || r.title)))).slice(0, 2);
        const exam = results.filter(r => r.source === 'gaokao').length;
        return `<li><strong>${escHtml(subject)}</strong> ${count} 段${books.length ? ` · ${escHtml(books.join('、'))}` : ''}${exam ? ` · 含真题 ${exam} 条` : ''}</li>`;
    });
    return `<div class="ai-digest"><p>「${escHtml(data.query || currentQuery)}」在 ${entries.length} 个学科共检索到 ${total} 段：</p><ul>${items.join('')}</ul></div>`;
}

function setAISearchStage(stage) {
    aiSearchStage = stage || '';
    renderAIConversation();
}

function renderAIContextSummary(msg) {
    const parts = [msg.provider ? `由 ${msg.provider} 撰写` : '', msg.contextSummary?.coverage_line || '', msg.cached ? '已缓存' : ''].filter(Boolean);
    if (!parts.length) return '';
    return `<div class="ai-msg-context">${escHtml(parts.join(' · '))}</div>`;
}

// Models sometimes double the bracket ("[[物理·…·p60]"); the extra one is absorbed.
const AI_CITATION_RE = /\[{1,2}([^\[\]\n]{2,120}?)\]{1,2}/g;

// Index of the retrieved record a citation label names, or -1. The label is compared
// in its escaped form with the server's own citation strings (and, for a textbook page,
// with subject + page), so a citation the model made up never becomes a button.
function matchAISource(escapedLabel, sources) {
    const list = Array.isArray(sources) ? sources : [];
    const exact = list.findIndex(src => src && src.citation && escHtml(src.citation) === `[${escapedLabel}]`);
    if (exact >= 0) return exact;
    const page = /[·・]\s*(?:p|P|第)\s*(\d{1,5})\s*页?$/.exec(escapedLabel);
    if (!page) return -1;
    const subject = escapedLabel.split(/[·・]/)[0].trim();
    const pageNum = Number(page[1]);
    return list.findIndex(src => src && src.source !== 'gaokao' && src.book_key
        && Number(src.logical_page) === pageNum && escHtml(src.subject || '') === subject);
}

function renderAIInline(escapedText, sources) {
    return escapedText
        .replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>')
        .replace(AI_CITATION_RE, (whole, label) => {
            if (!/[·・]/.test(label)) return whole;
            const index = matchAISource(label, sources);
            if (index < 0) {
                return `<span class="ai-cite unverified" title="未在本次检索证据中找到，请以原文为准">[${label}]</span>`;
            }
            return `<button type="button" class="ai-cite" data-src="${index}" title="点开核对原文">${label}</button>`;
        });
}

// Escaped text first; then headings (###), "- " lists, "1." lists, **bold** and citations.
function renderAIRichText(text, sources = []) {
    const lines = escHtml(String(text || '')).split(/\r?\n/);
    const out = [];
    let list = '';
    const closeList = () => {
        if (list) out.push(`</${list}>`);
        list = '';
    };
    for (const raw of lines) {
        const line = raw.trim();
        let m;
        if (!line) {
            closeList();
        } else if ((m = /^#{1,4}\s*(.+)$/.exec(line))) {
            closeList();
            out.push(`<h4 class="ai-h">${renderAIInline(m[1].replace(/\*\*/g, ''), sources)}</h4>`);
        } else if ((m = /^[-*•]\s+(.+)$/.exec(line))) {
            if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
            out.push(`<li>${renderAIInline(m[1], sources)}</li>`);
        } else if ((m = /^\d{1,2}\s*[.、．)]\s*(.+)$/.exec(line))) {
            if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; }
            out.push(`<li>${renderAIInline(m[1], sources)}</li>`);
        } else {
            closeList();
            out.push(`<p>${renderAIInline(line, sources)}</p>`);
        }
    }
    closeList();
    return out.join('');
}

// The numbered questions under 「值得追问」 become follow-up chips.
function parseFollowupQuestions(text) {
    const lines = String(text || '').split(/\r?\n/);
    const start = lines.findIndex(line => /^#{1,4}\s*值得追问/.test(line.trim()));
    if (start < 0) return [];
    const questions = [];
    for (const raw of lines.slice(start + 1)) {
        const line = raw.trim();
        if (/^#{1,4}\s/.test(line)) break;
        const m = /^(?:\d{1,2}\s*[.、．)]|[-*•])\s*(.+)$/.exec(line);
        if (!m) continue;
        const question = m[1].replace(/\*\*/g, '').replace(AI_CITATION_RE, '').trim();
        if (question && question.length <= 60) questions.push(question);
    }
    return questions.slice(0, 3);
}

function renderAISources(sources) {
    if (!sources || !sources.length) return '';
    return `
        <details class="ai-msg-sources">
            <summary>本次依据的原文 ${sources.length} 段</summary>
            <div class="ai-source-list">
                ${sources.map((src, index) => `
                    <button class="ai-source-chip ${src.source === 'gaokao' ? 'exam' : ''}" type="button" data-src="${index}">
                        ${escHtml(src.citation || `${src.subject || ''}·${src.title || ''}`)}
                    </button>
                `).join('')}
            </div>
        </details>
    `;
}

function renderAICitationNote(check) {
    if (!check || !check.unverified_count) return '';
    return `<div class="ai-msg-context warn">有 ${Number(check.unverified_count)} 处出处未能在本次检索证据中核对（灰色标出），请以原文为准。</div>`;
}

function renderAIMessage(msg, index, isLast) {
    if (msg.role === 'user') {
        return `<div class="ai-msg user"><div class="ai-msg-text">${escHtml(msg.content)}</div></div>`;
    }
    if (msg.isError) {
        const digest = msg.kind === 'overview' && aiLocalDigest ? aiLocalDigest : '';
        const retry = msg.retry && isLast && !aiRequestPending
            ? '<div class="ai-msg-actions"><button class="ai-followup-chip ai-retry-btn" type="button">重试</button></div>'
            : '';
        return `<div class="ai-msg assistant error"><div class="ai-msg-text">${escHtml(msg.content)}</div>${digest}${retry}</div>`;
    }
    return `
        <article class="ai-msg assistant ${msg.kind === 'overview' ? 'overview' : ''}" data-index="${index}">
            ${renderAIContextSummary(msg.kind === 'overview' ? msg : { provider: msg.provider, cached: msg.cached })}
            <div class="ai-msg-text ai-rich">${renderAIRichText(msg.content, msg.sources)}</div>
            ${renderAICitationNote(msg.citationCheck)}
            <div class="ai-quote hidden"></div>
            ${renderAISources(msg.sources)}
        </article>
    `;
}

function renderAIPending() {
    const cancel = '<div class="ai-msg-actions"><button class="ai-followup-chip ai-cancel-btn" type="button">取消</button></div>';
    if (aiPendingKind === 'overview') {
        return `
            <div class="ai-msg assistant pending">
                <div class="ai-msg-text"><span class="ai-dots" aria-hidden="true"></span>AI 正在通读这些原文，整合导读（约 10–60 秒）…</div>
                ${aiLocalDigest}
                ${cancel}
            </div>
        `;
    }
    return `<div class="ai-msg assistant pending"><div class="ai-msg-text"><span class="ai-dots" aria-hidden="true"></span>正在结合原文思考…</div>${cancel}</div>`;
}

function renderAIStarters() {
    if (!aiStarters) return;
    const last = aiConversation[aiConversation.length - 1];
    const prompts = !aiRequestPending && last && last.role === 'assistant' && !last.isError ? (last.followups || []) : [];
    if (!prompts.length) {
        aiStarters.innerHTML = '';
        aiStarters.classList.add('hidden');
        return;
    }
    aiStarters.innerHTML = `<span class="ai-starters-label">接着问</span>` + prompts.map(text => `
        <button class="ai-starter-chip" type="button" data-prompt="${escAttr(text)}">${escHtml(text)}</button>
    `).join('');
    aiStarters.classList.remove('hidden');
    aiStarters.querySelectorAll('.ai-starter-chip').forEach(btn => {
        btn.addEventListener('click', () => sendAIMessage(btn.dataset.prompt));
    });
}

function showAIQuote(messageEl, src) {
    const box = messageEl && messageEl.querySelector('.ai-quote');
    if (!box || !src) return;
    const isExam = src.source === 'gaokao';
    const head = isExam
        ? `${String(src.citation || '').replace(/^\[|\]$/g, '')} · 题干摘录（不含解析）`
        : [src.subject, shortBookTitle(src.display_title || src.title), src.logical_page != null ? `书页 p${src.logical_page}` : ''].filter(Boolean).join(' · ');
    box.innerHTML = `
        <div class="ai-quote-head">${escHtml(head)}</div>
        <blockquote>${escHtml(src.snippet || '（无摘录）')}</blockquote>
        ${!isExam && src.book_key ? `<button class="ai-quote-open" type="button"
            data-book-key="${escAttr(src.book_key)}" data-page="${Number(src.section) || 0}"
            data-total-pages="${Number(src.total_pages) || 0}">📖 打开教材原页</button>` : ''}
        <button class="ai-quote-close" type="button" aria-label="收起">收起</button>
    `;
    box.classList.remove('hidden');
    renderMath(box.querySelector('blockquote'));
    bindOpenPageButtons(box, '.ai-quote-open');
    const close = box.querySelector('.ai-quote-close');
    if (close) close.addEventListener('click', () => box.classList.add('hidden'));
}

function bindOpenPageButtons(root, selector) {
    if (!root) return;
    root.querySelectorAll(selector).forEach(btn => {
        btn.addEventListener('click', () => {
            const bookKey = btn.dataset.bookKey;
            const page = Number(btn.dataset.page || 0);
            const totalPages = Number(btn.dataset.totalPages || 0);
            if (bookKey) {
                openPageViewer(bookKey, page, totalPages);
            }
        });
    });
}

function renderAIConversation() {
    if (!aiContent) return;
    if (aiPanelQuery) aiPanelQuery.textContent = currentQuery ? `「${currentQuery}」` : '';
    renderAIFootnote();
    const lastIndex = aiConversation.length - 1;
    let html = aiConversation.map((msg, index) => renderAIMessage(msg, index, index === lastIndex)).join('');
    if (aiRequestPending) {
        html += renderAIPending();
    } else if (!aiConversation.length && aiSearchStage === 'searching') {
        html = `<div class="ai-msg assistant pending"><div class="ai-msg-text"><span class="ai-dots" aria-hidden="true"></span>正在检索${escHtml(DISPLAY_PHASE)}各科教材原文…</div></div>`;
    }
    aiContent.innerHTML = html;
    // Formulas in answers ($…$) are typeset from the escaped text nodes; KaTeX runs with trust off.
    aiContent.querySelectorAll('.ai-rich').forEach(el => renderMath(el));

    aiContent.querySelectorAll('article.ai-msg[data-index]').forEach(el => {
        const msg = aiConversation[Number(el.dataset.index)];
        if (!msg) return;
        el.querySelectorAll('.ai-cite[data-src], .ai-source-chip[data-src]').forEach(btn => {
            btn.addEventListener('click', () => showAIQuote(el, (msg.sources || [])[Number(btn.dataset.src)]));
        });
    });
    const cancelBtn = aiContent.querySelector('.ai-cancel-btn');
    if (cancelBtn) cancelBtn.addEventListener('click', () => cancelAIRequest());
    const retryBtn = aiContent.querySelector('.ai-retry-btn');
    if (retryBtn) retryBtn.addEventListener('click', () => retryLastAIMessage());
    if (aiCopyBtn) aiCopyBtn.classList.toggle('hidden', !aiConversation.some(msg => !msg.isError));
    renderAIStarters();
}

function setAIBusy(isBusy, kind = '') {
    aiRequestPending = isBusy;
    aiPendingKind = isBusy ? kind : '';
    if (aiSendBtn) aiSendBtn.disabled = isBusy;
    if (aiFollowupInput) aiFollowupInput.disabled = isBusy;
    if (aiStarters) {
        aiStarters.querySelectorAll('.ai-starter-chip').forEach(btn => { btn.disabled = isBusy; });
    }
}

function cancelAIRequest() {
    if (aiActiveRequest) aiActiveRequest.cancel();
}

function retryLastAIMessage() {
    if (aiRequestPending) return;
    const last = aiConversation[aiConversation.length - 1];
    if (!last || !last.isError || !last.retry) return;
    const { kind, userMessage } = last.retry;
    aiConversation.pop();
    const previous = aiConversation[aiConversation.length - 1];
    if (kind === 'chat' && previous && previous.role === 'user' && previous.content === userMessage) aiConversation.pop();
    runAIRequest(kind, userMessage);
}

function resetAIConversation() {
    // A new search is a new conversation: abort the in-flight request and
    // make sure its answer can never be rendered into the new one.
    aiConversationEpoch += 1;
    if (aiActiveRequest) {
        aiActiveRequest.cancel();
        aiActiveRequest = null;
    }
    setAIBusy(false);
    aiConversation = [];
    aiLocalDigest = '';
    aiSearchStage = '';
    if (aiContent) aiContent.innerHTML = '';
    if (aiFollowupInput) aiFollowupInput.value = '';
    if (aiCopyBtn) aiCopyBtn.classList.add('hidden');
    renderAIStarters();
}

function trimAIConversation() {
    if (aiConversation.length > AI_MEMORY_LIMIT) {
        // Keep the overview: it is what later turns refer back to.
        const head = aiConversation[0] && aiConversation[0].kind === 'overview' ? [aiConversation[0]] : [];
        aiConversation = head.concat(aiConversation.slice(-(AI_MEMORY_LIMIT - head.length)));
    }
}

function describeAIError(error, kind) {
    const code = error && error.code;
    if (error && error.cancelled) return kind === 'overview' ? '已取消 AI 整合。下面是检索概览：' : error.message;
    if (code === 'AI_DISABLED') return 'AI 整合当前未开启。下面是检索概览，原文见「检索原文备查」：';
    if (code === 'AI_BUDGET_EXHAUSTED') return '今天的 AI 额度已用完（北京时间早上 8 点恢复）。下面是检索概览，原文见「检索原文备查」：';
    const message = (error && error.message) || '未知错误';
    return kind === 'overview' ? `AI 整合这次没有完成：${message} 下面是检索概览：` : `请求失败：${message}`;
}

function assistantReplyFrom(data, kind, userMessage) {
    const contextPayload = data.context || {};
    setSearchAIProviderLabel(data.provider || aiProviderLabel);
    const content = String(data.answer || '');
    const asked = parseFollowupQuestions(content);
    const followups = (asked.length ? asked : (contextPayload.suggested_questions || []))
        .filter(prompt => prompt && prompt !== userMessage)
        .slice(0, 3);
    return {
        role: 'assistant',
        kind,
        content,
        provider: String(data.provider || ''),
        cached: Boolean(data.cached),
        contextSummary: contextPayload.summary || null,
        citationCheck: data.citation_check || null,
        followups,
        // Citation buttons and source chips point only at these server retrieval records.
        sources: (contextPayload.evidence || []).map(src => ({
            ...src,
            total_pages: (window._bookPages && window._bookPages[src.book_key]?.pages) || 0,
        })),
    };
}

// The single place that starts an AI request: exactly one server request,
// no fallback, no race, no automatic retry.
async function runAIRequest(kind, userMessage = '') {
    if (!currentData || !currentQuery || aiRequestPending || !window.SunAI || !hasSearchResults()) return;
    let path = `${API}/api/overview`;
    let payload = buildOverviewPayload();
    let cleanMessage = '';
    if (kind !== 'overview') {
        cleanMessage = String(userMessage || '').trim();
        if (!cleanMessage) return;
        const history = window.SunAI.boundedHistory(aiConversation);
        aiConversation.push({ role: 'user', content: cleanMessage });
        trimAIConversation();
        if (aiFollowupInput) aiFollowupInput.value = '';
        path = `${API}/api/chat`;
        payload = buildAIServerPayload(cleanMessage, history);
    }
    const epoch = aiConversationEpoch;
    const executionKey = getAIExecutionKey();

    const request = window.SunAI.ask(path, payload);
    aiActiveRequest = request;
    setAIBusy(true, kind);
    renderAIConversation();

    let reply = null;
    try {
        const data = await request.promise;
        if (epoch !== aiConversationEpoch) return;
        if (executionKey !== getAIExecutionKey()) {
            reply = { role: 'assistant', kind, isError: true, content: '检索范围已变化，本次回答未显示。', retry: { kind, userMessage: cleanMessage } };
        } else {
            reply = assistantReplyFrom(data, kind, cleanMessage);
        }
    } catch (e) {
        if (epoch !== aiConversationEpoch) return;
        reply = { role: 'assistant', kind, isError: true, content: describeAIError(e, kind), retry: { kind, userMessage: cleanMessage } };
    } finally {
        if (epoch === aiConversationEpoch) {
            if (aiActiveRequest === request) aiActiveRequest = null;
            if (reply) aiConversation.push(reply);
            trimAIConversation();
            setAIBusy(false);
            renderAIConversation();
        }
    }
}

// Started once by doSearch(), after the results of that explicit search are on screen.
async function startSearchOverview() {
    await runAIRequest('overview');
}

async function sendAIMessage(userMessage) {
    await runAIRequest('chat', userMessage);
}

async function requestAIFollowup() {
    if (!aiFollowupInput) return;
    await sendAIMessage(aiFollowupInput.value);
}

async function copyAIConversation() {
    if (!aiConversation.length || !navigator.clipboard) return;
    const text = [`检索：${currentQuery}`]
        .concat(aiConversation.filter(msg => !msg.isError).map(msg => `${msg.role === 'user' ? '你' : 'AI'}：${msg.content}`))
        .join('\n\n');
    const label = aiCopyBtn ? aiCopyBtn.textContent : '';
    try {
        await navigator.clipboard.writeText(text);
        if (aiCopyBtn) aiCopyBtn.textContent = '已复制';
    } catch (_) {
        if (aiCopyBtn) aiCopyBtn.textContent = '复制失败';
    }
    if (aiCopyBtn) setTimeout(() => { aiCopyBtn.textContent = label; }, 1200);
}

// ── Footer Version ────────────────────────────────────────
async function loadFrontendVersion() {
    const footer = document.getElementById('footer-version-line');
    if (!footer) return;

    let version = DEFAULT_FRONTEND_VERSION;
    try {
        const res = await fetch(`${FRONTEND_VERSION_FILE}?v=${Date.now()}`, { cache: 'no-store' });
        if (res.ok) {
            const data = await res.json();
            version = data.frontend_refactor_version || version;
        }
    } catch (_) {
        // fallback to default version
    }

    footer.textContent = `重构版本 ${version}`;
}

loadFrontendVersion();

// ── Navigation ────────────────────────────────────────────
document.querySelectorAll('.nav-btn[data-view]').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
        btn.classList.add('active');
        const view = document.getElementById('view-' + btn.dataset.view);
        view.classList.add('active');
        if (btn.dataset.view === 'graph') loadGraph();
        if (btn.dataset.view === 'gaokao' && PHASE === '高中') initGaokao();
        if (btn.dataset.view === 'insights') loadInsights();
    });
});

// ── Advanced Search Panel ─────────────────────────────────
const advToggle = document.getElementById('advanced-toggle');
const advPanel = document.getElementById('advanced-panel');
const filterScope = document.getElementById('filter-scope');
const filterSort = document.getElementById('filter-sort');
const filterImages = document.getElementById('filter-images');

advToggle.addEventListener('click', () => {
    advPanel.classList.toggle('hidden');
    advToggle.classList.toggle('active');
});

// Load books into dropdown on first toggle open
let booksLoaded = false;
async function loadBooks() {
    if (booksLoaded) return;
    try {
        const res = await fetch(`${API}/api/books?phase=${encodeURIComponent(PHASE)}`);
        const subjects = await res.json();
        filterScope.innerHTML = '<option value="">全部教材</option>';

        const subjectGroup = document.createElement('optgroup');
        subjectGroup.label = '按学科检索';
        subjects.forEach(subj => {
            const subjectOption = document.createElement('option');
            subjectOption.value = `subject:${subj.subject}`;
            subjectOption.textContent = `${subj.icon} ${subj.subject} · 全部教材`;
            subjectGroup.appendChild(subjectOption);
        });
        filterScope.appendChild(subjectGroup);

        subjects.forEach(subj => {
            const optgroup = document.createElement('optgroup');
            optgroup.label = `${subj.icon} ${subj.subject} · 单本教材`;
            subj.books.forEach(b => {
                const opt = document.createElement('option');
                opt.value = `book:${b.book_key}`;
                opt.textContent = b.display_title || b.title;
                optgroup.appendChild(opt);
            });
            filterScope.appendChild(optgroup);
        });
        booksLoaded = true;
    } catch (e) { /* silent */ }
}

// Load books on page load
loadBooks();

// Re-search when filters change
filterSort.addEventListener('change', () => { if (currentQuery) doSearch(currentQuery); });
filterScope.addEventListener('change', () => { if (currentQuery) doSearch(currentQuery); });
filterImages.addEventListener('change', () => { if (currentQuery) doSearch(currentQuery); });

// ── Search ────────────────────────────────────────────────
const searchInput = document.getElementById('search-input');
const searchBtn = document.getElementById('search-btn');
const resultsEl = document.getElementById('results');
const queryAnalysisEl = document.getElementById('query-analysis');
const relatedBarEl = document.getElementById('related-bar');
const evidenceSectionEl = document.getElementById('evidence-section');
const evidenceSummaryEl = document.getElementById('evidence-summary');
const evidenceToggleAllEl = document.getElementById('evidence-toggle-all');
const GROUP_PREVIEW_COUNT = 6;

if (evidenceToggleAllEl) {
    evidenceToggleAllEl.addEventListener('click', () => {
        const groups = Array.from(resultsEl.querySelectorAll('details.result-group'));
        const open = !groups.every(group => group.open);
        groups.forEach(group => { group.open = open; });
        evidenceToggleAllEl.textContent = open ? '全部收起' : '全部展开';
    });
}

searchBtn.addEventListener('click', () => doSearch(searchInput.value));
searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(searchInput.value); });
// ── Dynamic Concept Carousel ──────────────────────────────
const FALLBACK_CONCEPTS = PHASE === '初中'
    ? ['一次函数', '化学方程式', '光合作用', '丝绸之路', '勾股定理', '欧姆定律', '细胞', '地球运动',
       '法治', '光的折射', '二元一次方程', '生态系统', '物态变化', '辛亥革命', '全等三角形', '酸碱盐', '大洲大洋', '文言文']
    : ['蛋白质', 'DNA', '能量守恒', '丝绸之路', '温室效应', '光合作用', '平衡', '电子',
       '氧化还原', '细胞分裂', '力学', '概率', '函数', '文艺复兴', '全球化', '化学键', '自然选择', '电磁波'];
let allConcepts = [...FALLBACK_CONCEPTS];
const carousel = document.getElementById('concept-carousel');
const CAROUSEL_SIZE = 4;
let carouselIdx = 0;

function renderCarouselBatch() {
    if (!carousel) return;
    carousel.classList.add('fading');
    setTimeout(() => {
        carousel.innerHTML = '';
        for (let i = 0; i < CAROUSEL_SIZE; i++) {
            const concept = allConcepts[(carouselIdx + i) % allConcepts.length];
            const btn = document.createElement('button');
            btn.className = 'quick-tag';
            btn.textContent = concept;
            btn.addEventListener('click', () => {
                searchInput.value = concept;
                doSearch(concept);
            });
            carousel.appendChild(btn);
        }
        carouselIdx = (carouselIdx + CAROUSEL_SIZE) % allConcepts.length;
        carousel.classList.remove('fading');
    }, 300);
}

// Load curated academic keywords from API
(async () => {
    try {
        const res = await fetch(`${API}/api/keywords?limit=120&phase=${encodeURIComponent(PHASE)}`);
        if (res.ok) {
            const data = await res.json();
            const kws = data.keywords || [];
            if (kws.length > 6) {
                allConcepts = kws.map(k => k.term).filter(Boolean);
                // Shuffle for variety
                for (let i = allConcepts.length - 1; i > 0; i--) {
                    const j = Math.floor(Math.random() * (i + 1));
                    [allConcepts[i], allConcepts[j]] = [allConcepts[j], allConcepts[i]];
                }
            }
        }
    } catch (_) { /* fallback concepts already set */ }
    renderCarouselBatch();
    setInterval(renderCarouselBatch, 3000);
})();

// ── Trending Searches ─────────────────────────────────────
async function loadTrending() {
    try {
        const res = await fetch(`${API}/api/search/trending?phase=${encodeURIComponent(PHASE)}`);
        if (!res.ok) return;
        const data = await res.json();
        const section = document.getElementById('trending-section');
        const popularGroup = document.getElementById('trending-popular');
        const popularTags = document.getElementById('trending-popular-tags');
        if (!section || !popularGroup || !popularTags) return;

        popularTags.innerHTML = '';
        if (data.popular && data.popular.length > 0) {
            data.popular.slice(0, 10).forEach(item => {
                const btn = document.createElement('button');
                btn.className = 'trending-tag popular';
                btn.innerHTML = `${item.query} <span class="freq">${item.freq > 1 ? '×' + item.freq : ''}</span>`;
                btn.addEventListener('click', () => {
                    searchInput.value = item.query;
                    doSearch(item.query);
                });
                popularTags.appendChild(btn);
            });
            popularGroup.classList.remove('hidden');
            section.classList.remove('hidden');
        } else {
            popularGroup.classList.add('hidden');
            section.classList.add('hidden');
        }
    } catch (_) { /* silent */ }
}

// Load trending on page init
loadTrending();

let currentQuery = '';
let currentData = null;
let currentOverviewData = null;
let currentSubjectFilter = null;

async function doSearch(q, { subject = null } = {}) {
    q = q.trim();
    if (!q) return;
    currentQuery = q;
    currentSubjectFilter = subject || null;
    resetAIConversation();
    document.body.classList.add('has-search');

    resultsEl.innerHTML = '<div class="loading">检索中…</div>';
    if (evidenceSummaryEl) evidenceSummaryEl.textContent = '';
    if (evidenceSectionEl) evidenceSectionEl.classList.remove('hidden');
    queryAnalysisEl.classList.add('hidden');
    relatedBarEl.classList.add('hidden');
    if (aiPanel) aiPanel.classList.remove('hidden');
    setAISearchStage('searching');
    const epoch = aiConversationEpoch;

    // Build search URL with advanced filters
    const params = new URLSearchParams({ q, limit: 100, phase: PHASE });
    if (currentSubjectFilter) params.set('subject', currentSubjectFilter);

    const scopeValue = filterScope.value;
    if (scopeValue.startsWith('subject:')) {
        params.set('scope_subject', scopeValue.slice('subject:'.length));
    } else if (scopeValue.startsWith('book:')) {
        params.set('book_key', scopeValue.slice('book:'.length));
    }

    const sort = filterSort.value;
    if (sort && sort !== 'relevance') params.set('sort', sort);

    if (filterImages.checked) params.set('has_images', 'true');

    try {
        const res = await fetch(`${API}/api/search?${params}`);
        if (!res.ok) throw new Error('Search failed');
        const data = await res.json();
        if (epoch !== aiConversationEpoch) return;   // a newer search has started
        currentData = data;
        if (!currentSubjectFilter) {
            currentOverviewData = data;
        }
        aiLocalDigest = buildLocalDigest(data);
        setAISearchStage('');
        renderResults(currentData);
        if (hasSearchResults()) {
            // The one AI request this search makes: the overview of what it found.
            startSearchOverview();
        } else if (aiPanel) {
            aiPanel.classList.add('hidden');
        }
        // Load related concepts
        loadRelated(currentData, q);
        // Refresh trending after search (new query logged)
        setTimeout(() => loadTrending(), 500);
        // Show concept subgraph for the search term
        loadSearchGraph(q);
    } catch (e) {
        if (epoch !== aiConversationEpoch) return;
        setAISearchStage('');
        if (aiPanel) aiPanel.classList.add('hidden');
        resultsEl.innerHTML = `<div class="loading">搜索出错: ${escHtml(e.message)}</div>`;
    }
}

// ── Related Concepts ──────────────────────────────────────
async function loadRelated(searchData, q) {
    try {
        const conceptCounts = new Map();
        const groups = Array.isArray(searchData?.groups) ? searchData.groups : [];
        for (const item of groups) {
            const concepts = Array.isArray(item?.matched_concepts) ? item.matched_concepts : [];
            for (const concept of concepts) {
                const term = String(concept || '').trim();
                if (!term || term === q || term.includes(q) || q.includes(term)) continue;
                conceptCounts.set(term, (conceptCounts.get(term) || 0) + 1);
            }
        }

        let data = Array.from(conceptCounts.entries())
            .sort((a, b) => b[1] - a[1] || a[0].length - b[0].length)
            .slice(0, 10)
            .map(([term, count]) => ({ term, count }));

        if (data.length === 0) {
            const res = await fetch(`${API}/api/related?q=${encodeURIComponent(q)}&limit=10&phase=${encodeURIComponent(PHASE)}`);
            data = await res.json();
        }

        if (data.length > 0) {
            relatedBarEl.innerHTML = `
                <span class="related-label">🔗 相关概念：</span>
                ${data.map(r => `<button class="related-tag" data-q="${escAttr(r.term)}">${escHtml(r.term)}<span class="related-count">${r.count}</span></button>`).join('')}
            `;
            relatedBarEl.classList.remove('hidden');
            relatedBarEl.querySelectorAll('.related-tag').forEach(tag => {
                tag.addEventListener('click', () => {
                    searchInput.value = tag.dataset.q;
                    doSearch(tag.dataset.q);
                    window.scrollTo({ top: 0, behavior: 'smooth' });
                });
            });
        } else {
            relatedBarEl.classList.add('hidden');
        }
    } catch (e) {
        relatedBarEl.classList.add('hidden');
    }
}

function renderResultCard(r, subject, subjectBreadth, query, isExtra) {
    return `
                <div class="result-card${isExtra ? ' is-extra' : ''}" onclick="this.classList.toggle('expanded')">
                    <div class="result-meta">
                        <span class="result-title">${escHtml(r.title)} · §${r.section}</span>
                        ${r.source === 'gaokao' ? '<span class="source-badge gaokao">' + (PHASE === '高中' ? '📝 真题' : '📝 考试') + '</span>' : '<span class="source-badge textbook">📚 教材</span>'}
                        ${r.image_count > 0 ? `<span class="img-badge">📷 ${r.image_count}</span>` : ''}
                        ${r.page_url ? `<span class="page-badge" title="${escAttr(pageRefTitle(r))}">📄 ${escHtml(pageRefLabel(r))}</span>` : ''}
                    </div>
                    ${renderEvidenceTrace(r, subject, subjectBreadth, query)}
                    <div class="result-snippet">${sanitizeSnippet(r.snippet)}</div>
                    <div class="result-text">${renderText(r.text, r.book_key)}</div>
                    ${r.page_url ? `<div class="result-actions">
                        <button class="view-page-btn" onclick="event.stopPropagation(); openPageViewer('${escAttr(r.book_key)}', ${r.page_num}, ${r.total_pages})">
                            📖 查看原文
                        </button>
                    </div>` : (String(r.book_key || '').startsWith('suppbook:') ? `<div class="result-actions">
                        <span class="view-page-note" title="该结果来自补充教材 OCR，当前没有对应的页图映射，因此不能打开页图原文。">
                            📄 补充教材暂无页图
                        </span>
                    </div>` : '')}
                </div>`;
}

function renderEvidenceGroup(g, subjectBreadth, query) {
    const results = g.results || [];
    const books = Array.from(new Set(results.filter(r => r.source !== 'gaokao').map(r => shortBookTitle(r.display_title || r.title)))).slice(0, 2);
    const examCount = results.filter(r => r.source === 'gaokao').length;
    const extra = Math.max(0, results.length - GROUP_PREVIEW_COUNT);
    return `
        <details class="result-group" data-subject="${escAttr(g.subject)}" style="--group-color: ${escAttr(g.color || '#7a8a89')}">
            <summary class="group-header">
                <span class="group-icon" aria-hidden="true">${g.icon || '📚'}</span>
                <span class="group-name">${escHtml(g.subject)}</span>
                <span class="group-count">${results.length} 段</span>
                <span class="group-books">${escHtml(books.join('、'))}${books.length && examCount ? ' · ' : ''}${examCount ? `真题 ${examCount} 条` : ''}</span>
                <span class="group-chevron" aria-hidden="true"></span>
            </summary>
            <div class="group-body">
                ${results.map((r, i) => renderResultCard(r, g.subject, subjectBreadth, query, i >= GROUP_PREVIEW_COUNT)).join('')}
                ${extra ? `<button class="group-more" type="button">再显示 ${extra} 段</button>` : ''}
            </div>
        </details>
    `;
}

function sortedResultGroups(data) {
    return ((data && data.groups) || []).slice().sort((a, b) => (b.results || []).length - (a.results || []).length);
}

function renderResults(data) {
    renderQueryAnalysis(data.query_analysis);

    const counts = data.subject_counts || {};
    // Largest subject first; the same order is handed to the library decorator below.
    const groups = sortedResultGroups(data);
    if (groups.length === 0) {
        if (evidenceSummaryEl) evidenceSummaryEl.textContent = '';
        resultsEl.innerHTML = '<div class="loading">未找到相关结果，换个说法或更短的关键词试试。</div>';
        return;
    }
    const shown = groups.reduce((sum, g) => sum + (g.results || []).length, 0);
    const subjectBreadth = Object.keys(counts).length || groups.length;
    if (evidenceSummaryEl) {
        evidenceSummaryEl.textContent = `${groups.length} 个学科 · ${shown} 段原文 · 按学科收起，点开核对`;
    }
    if (evidenceToggleAllEl) evidenceToggleAllEl.textContent = '全部展开';

    resultsEl.innerHTML = groups.map(g => renderEvidenceGroup(g, subjectBreadth, data.query)).join('');
    resultsEl.querySelectorAll('.group-more').forEach(btn => {
        btn.addEventListener('click', () => {
            const body = btn.closest('.group-body');
            if (body) body.querySelectorAll('.result-card.is-extra').forEach(card => card.classList.remove('is-extra'));
            btn.remove();
        });
    });

    // Search result text is tokenized by renderText(); render only those tokens here.
    if (typeof renderKaTeX === 'function') {
        renderKaTeX(resultsEl);
    }
    if (window.SunLibrary) window.SunLibrary.decorateSearchResults(resultsEl, groups); // library-tabs
}

function getSubjectIcon(s) {
    return (window._subjectMeta[s] && window._subjectMeta[s].icon) || '📚';
}

function getSubjectColor(s) {
    return (window._subjectMeta[s] && window._subjectMeta[s].color) || '#666';
}

function renderQueryAnalysis(analysis, filterSubject = null) {
    if (!queryAnalysisEl || filterSubject || !analysis) {
        if (queryAnalysisEl) queryAnalysisEl.classList.add('hidden');
        return;
    }

    const conceptTerms = Array.isArray(analysis.concept_terms) ? analysis.concept_terms : [];
    const fallbackTerms = Array.isArray(analysis.fallback_terms) ? analysis.fallback_terms : [];
    const precisionProfile = inferAIQueryProfile(analysis.query || currentQuery);
    const chips = [];

    if (precisionProfile.mode === 'precision_agent' && precisionProfile.target) {
        chips.push(`<span class="query-chip concept">${escHtml(precisionProfile.target)} · ${escHtml(describePrecisionIntent(precisionProfile.intent))}</span>`);
    }

    conceptTerms.slice(0, 4).forEach(item => {
        // Expansion terms come from machine-built synonym tables; they are not claimed to be equivalent.
        const matchLabel = item.match_type === 'alias' ? '扩展检索' : '标准术语';
        chips.push(`<span class="query-chip concept">${escHtml(item.term)} · ${escHtml(matchLabel)}</span>`);
    });
    fallbackTerms
        .filter(item => !isStructuralFallbackTerm(item.term, precisionProfile))
        .slice(0, 4)
        .forEach(item => {
        const hitLabel = [item.textbook_hits ? `主库 ${item.textbook_hits}` : '', item.supplemental_hits ? `备份 ${item.supplemental_hits}` : '']
            .filter(Boolean)
            .join(' / ');
        chips.push(`<span class="query-chip fallback">${escHtml(item.term)}${hitLabel ? ` · ${escHtml(hitLabel)}` : ''}</span>`);
    });

    const scopeLine = analysis.scope_label ? `<span class="query-analysis-scope">范围：${escHtml(analysis.scope_label)}</span>` : '';
    const fallbackFlag = analysis.used_supplemental_fallback
        ? '<span class="query-analysis-flag">当前结果含备份教材解析兜底</span>'
        : '';
    const summaryText = precisionProfile.mode === 'precision_agent'
        ? `已识别为${describePrecisionIntent(precisionProfile.intent)}型问题，AI 将优先围绕「${precisionProfile.target || analysis.display_term || analysis.query || currentQuery}」做精准检索。`
        : (analysis.summary || '');

    queryAnalysisEl.innerHTML = `
        <div class="query-analysis-header">
            <span class="query-analysis-title">检索扩展</span>
            ${scopeLine}
        </div>
        <div class="query-analysis-summary">${escHtml(summaryText)}</div>
        ${chips.length ? `<div class="query-analysis-chips">${chips.join('')}</div>` : ''}
        ${fallbackFlag}
    `;
    queryAnalysisEl.classList.remove('hidden');
}

function renderEvidenceTrace(result, subject, subjectBreadth, query) {
    const evidenceChips = [];
    if (result.retrieval_source === 'supplemental' || result.match_channel === 'supplemental') {
        evidenceChips.push('<span class="evidence-chip supplemental">备份教材页</span>');
    } else if (result.match_channel === 'exact') {
        evidenceChips.push('<span class="evidence-chip strong">原文精确命中</span>');
    } else if (result.match_channel === 'semantic') {
        evidenceChips.push('<span class="evidence-chip semantic">向量语义召回</span>');
    } else if (result.match_channel === 'fts') {
        evidenceChips.push('<span class="evidence-chip semantic">全文检索命中</span>');
    } else {
        evidenceChips.push('<span class="evidence-chip semantic">混合召回</span>');
    }

    if (result.page_url) {
        evidenceChips.push(`<span class="evidence-chip page">可回原页 ${escHtml(pageRefLabel(result))}</span>`);
    } else {
        evidenceChips.push('<span class="evidence-chip neutral">仅片段证据</span>');
    }

    if (result.snippet_source === 'sentence') {
        evidenceChips.push('<span class="evidence-chip strong">句级证据</span>');
    }

    if (result.source === 'gaokao') {
        evidenceChips.push(
            `<span class="evidence-chip exam">${escHtml([result.year, result.category].filter(Boolean).join(' · ') || (PHASE === '高中' ? '高考真题' : '考试题'))}</span>`
        );
    } else {
        evidenceChips.push(
            result.retrieval_source === 'supplemental'
                ? '<span class="evidence-chip textbook">教材原文兜底</span>'
                : '<span class="evidence-chip textbook">教材原文</span>'
        );
    }

    if (result.image_count > 0) {
        evidenceChips.push(`<span class="evidence-chip media">含 ${result.image_count} 张图</span>`);
    }

    if (result.matched_term) {
        evidenceChips.push(`<span class="evidence-chip neutral">术语：${escHtml(result.matched_term)}</span>`);
    }

    return `
        <div class="result-trace">
            <div class="evidence-chips">${evidenceChips.join('')}</div>
        </div>
    `;
}

function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
}

function escAttr(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Sanitize snippet: keep <mark> highlights, strip everything else
function sanitizeSnippet(html) {
    if (!html) return '';
    // Preserve <mark>...</mark> by replacing with placeholders
    const marks = [];
    html = html.replace(/<mark>(.*?)<\/mark>/gi, (_, inner) => {
        marks.push(escHtml(String(inner || '').replace(/<[^>]+>/g, ' ')));
        return `%%MARK${marks.length - 1}%%`;
    });
    // Strip all remaining HTML tags
    html = html.replace(/<[^>]+>/g, ' ');
    // Remove markdown image syntax ![...](...)  
    html = html.replace(/!\[.*?\]\(.*?\)/g, '');
    // Collapse whitespace
    html = html.replace(/\s+/g, ' ').trim();
    // Truncate to ~200 chars
    if (html.length > 250) {
        html = html.slice(0, 250) + '…';
    }
    html = escHtml(html);
    // Restore mark tags
    html = html.replace(/%%MARK(\d+)%%/g, (_, i) => `<mark>${marks[+i]}</mark>`);
    return html;
}

// Render text with images, formulas, and rich formatting for expanded view
function renderText(text, bookKey) {
    if (!text) return '';
    const htmlTokens = [];
    const stashHtml = (html) => {
        const key = `%%HTML_TOKEN_${htmlTokens.length}%%`;
        htmlTokens.push(html);
        return key;
    };
    const restoreHtmlTokens = (html) => html.replace(/%%HTML_TOKEN_(\d+)%%/g, (_, i) => htmlTokens[Number(i)] || '');
    const stashFormula = (formula, displayMode) => {
        const normalized = String(formula || '').trim();
        if (!normalized) return '';
        return stashHtml(`<span class="${displayMode ? 'katex-display' : 'katex-inline'}" data-formula="${escAttr(normalized)}"></span>`);
    };

    // Strip HTML tags except preserve content
    let s = String(text).replace(/<[^>]+>/g, ' ');
    // Convert markdown images to <img> tags — use R2 CDN
    s = s.replace(/!\[([^\]]*)\]\(images\/([^)]+)\)/g, (_, alt, src) => {
        return stashHtml(`<img class="result-img" src="${IMG_CDN}/orig/${encodeURIComponent(bookKey)}/${encodeURI(src)}" alt="${escAttr(alt || '教材图片')}" loading="lazy">`);
    });

    // Wrap LaTeX formulas for KaTeX rendering
    s = s.replace(/\$\$([\s\S]+?)\$\$/g, (_, formula) => stashFormula(formula, true));
    s = s.replace(/\\\[([\s\S]+?)\\\]/g, (_, formula) => stashFormula(formula, true));
    s = s.replace(/\\\(([\s\S]+?)\\\)/g, (_, formula) => stashFormula(formula, false));
    s = s.replace(/\$([^$\n]+?)\$/g, (_, formula) => stashFormula(formula, false));
    s = escHtml(s);

    // Render markdown tables as HTML tables
    s = s.replace(/((?:\|.+\|\n?)+)/g, (tableBlock) => {
        const rows = tableBlock.trim().split('\n').filter(r => r.trim());
        if (rows.length < 2) return tableBlock;
        // Check if second row is separator (|---|---|
        const isSep = /^[\|\s:-]+$/.test(rows[1]);
        let html = '<table class="result-table">';
        rows.forEach((row, i) => {
            if (isSep && i === 1) return; // skip separator
            const cells = row.split('|').filter(c => c.trim() !== '');
            const tag = (i === 0 && isSep) ? 'th' : 'td';
            html += '<tr>' + cells.map(c => `<${tag}>${c.trim()}</${tag}>`).join('') + '</tr>';
        });
        html += '</table>';
        return html;
    });

    // Single line breaks → <br> within text
    s = s.replace(/(?<!\n)\n(?!\n)/g, '<br>');

    // Split paragraphs by double newline
    const parts = s.split(/\n\n+/);
    if (parts.length > 1) {
        s = parts.map(p => {
            const trimmed = p.trim();
            if (!trimmed) return '';
            if (trimmed.startsWith('<table') || trimmed.startsWith('<img')) return trimmed;
            return `<p class="result-para">${trimmed}</p>`;
        }).join('');
    }

    return restoreHtmlTokens(s);
}

// KaTeX rendering helper
function shouldRenderKaTeXFormula(formula) {
    // KaTeX logs noisy metric warnings for a few OCR/unit glyphs even with strict: ignore.
    // Keep those formulas readable as plain text instead of letting a renderer warning break UX.
    return !/[‰]/.test(String(formula || ''));
}

function renderKaTeX(container) {
    if (!container || typeof katex === 'undefined') return;
    container.querySelectorAll('.katex-display, .katex-inline').forEach(el => {
        if (el.dataset.rendered === '1') return;
        try {
            const formula = el.dataset.formula || '';
            if (!shouldRenderKaTeXFormula(formula)) {
                el.textContent = formula;
                el.dataset.rendered = '1';
                return;
            }
            const isDisplay = el.classList.contains('katex-display');
            katex.render(formula, el, { displayMode: isDisplay, throwOnError: false, strict: 'ignore' });
            el.dataset.rendered = '1';
        } catch (e) {
            el.textContent = el.dataset.formula; // fallback
            el.dataset.rendered = '1';
        }
    });
}

// ── Page Image Viewer (Lightbox) ─────────────────────────
// Page labels: the image is a page of the held PDF. Always "PDF 第 N 页"; the printed page
// ("书页 p.X") only when the catalog verified it. The legacy image index is never shown.
function pageRefLabel(item) {
    const ref = item && (item.pdf_ref || item);
    if (!ref || !ref.pdf_page) return '页图';
    return `PDF 第 ${ref.pdf_page} 页` + (ref.printed_label ? ` · 书页 p.${ref.printed_label}` : '');
}

function pageRefTitle(item) {
    const ref = item && item.pdf_ref;
    if (!ref || !ref.pdf_page) return '页图';
    return `PDF 第 ${ref.pdf_page} 页 / 共 ${ref.pdf_pages} 页` + (ref.printed_label ? ` · 书页 p.${ref.printed_label}（已核对）` : '');
}

function openPageViewer(bookKey, page, totalPages) {
    // Remove existing viewer
    const existing = document.getElementById('page-viewer');
    if (existing) existing.remove();

    const context = 4; // ±4 pages -> total up to 9 pages
    const startPage = Math.max(0, page - context);
    const endPage = Math.min(totalPages - 1, page + context);
    let currentPage = page;

    // Find the short_key from the API (we'll use the book_map loaded at startup if available)
    const viewer = document.createElement('div');
    viewer.id = 'page-viewer';
    viewer.className = 'page-viewer-overlay';
    viewer.innerHTML = `
        <div class="page-viewer-modal">
            <div class="page-viewer-header">
                <span class="page-viewer-title">加载中...</span>
                <span class="page-viewer-page"></span>
                <button class="page-viewer-close" onclick="closePageViewer()">✕</button>
            </div>
            <div class="page-viewer-body">
                <button class="page-nav-btn prev" onclick="pageViewerNav(-1)">‹</button>
                <div class="page-viewer-image-wrap">
                    <div class="page-viewer-loading">
                        <div class="page-loading-spinner"></div>
                        <span>加载页面中...</span>
                    </div>
                    <img class="page-viewer-img" src="" alt="" style="display:none" />
                </div>
                <button class="page-nav-btn next" onclick="pageViewerNav(1)">›</button>
            </div>
            <div class="page-viewer-thumbnails"></div>
            <div class="page-viewer-footer">
                <span class="page-viewer-info"></span>
            </div>
        </div>
    `;
    document.body.appendChild(viewer);
    document.body.style.overflow = 'hidden';

    // Close on overlay click
    viewer.addEventListener('click', (e) => {
        if (e.target === viewer) closePageViewer();
    });

    // Keyboard navigation
    window._pageViewerKeyHandler = (e) => {
        if (e.key === 'Escape') closePageViewer();
        if (e.key === 'ArrowLeft') pageViewerNav(-1);
        if (e.key === 'ArrowRight') pageViewerNav(1);
    };
    document.addEventListener('keydown', window._pageViewerKeyHandler);

    // Touch swipe support for mobile
    let touchStartX = 0, touchStartY = 0;
    const body = viewer.querySelector('.page-viewer-body');
    body.addEventListener('touchstart', (e) => {
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
    }, { passive: true });
    body.addEventListener('touchend', (e) => {
        const dx = e.changedTouches[0].clientX - touchStartX;
        const dy = e.changedTouches[0].clientY - touchStartY;
        if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) {
            if (dx > 0) pageViewerNav(-1); // swipe right = prev
            else pageViewerNav(1);         // swipe left = next
        }
    }, { passive: true });

    // Fetch page data from API
    fetch(`${API}/api/page-image?book_key=${encodeURIComponent(bookKey)}&page=${page}&context=${context}`)
        .then(r => {
            if (!r.ok) throw new Error(r.status === 404 ? '该页暂无页图' : '加载失败');
            return r.json();
        })
        .then(data => {
            window._pageViewerData = data;
            window._pageViewerIdx = Math.max(0, data.pages.findIndex(p => p.current));

            // Update header
            viewer.querySelector('.page-viewer-title').textContent = data.title;

            // Build thumbnails (small renditions; the label is the PDF page)
            const thumbs = viewer.querySelector('.page-viewer-thumbnails');
            thumbs.innerHTML = data.pages.map((p, i) => `
                <div class="page-thumb ${p.current ? 'active' : ''}" data-idx="${i}" onclick="pageViewerGo(${i})">
                    <img src="${escAttr(p.thumb_url || p.url)}" alt="PDF 第 ${Number(p.pdf_page) || ''} 页" loading="lazy" />
                    <span>${Number(p.pdf_page) || ''}</span>
                </div>
            `).join('');

            // Show current page
            showPageViewerImage(window._pageViewerIdx);
        })
        .catch(e => {
            viewer.querySelector('.page-viewer-title').textContent = e && e.message ? e.message : '加载失败';
            const loading = viewer.querySelector('.page-viewer-loading');
            if (loading) loading.innerHTML = '<span>没有可显示的页图</span>';
        });
}

function showPageViewerImage(idx) {
    const data = window._pageViewerData;
    if (!data || idx < 0 || idx >= data.pages.length) return;

    window._pageViewerIdx = idx;
    const page = data.pages[idx];
    const viewer = document.getElementById('page-viewer');
    if (!viewer) return;

    const img = viewer.querySelector('.page-viewer-img');
    const loading = viewer.querySelector('.page-viewer-loading');

    // Show loading, hide image
    img.style.display = 'none';
    img.style.opacity = '0';
    if (loading) loading.style.display = 'flex';

    // Fade in on load
    img.onload = () => {
        if (loading) loading.style.display = 'none';
        img.style.display = 'block';
        requestAnimationFrame(() => { img.style.opacity = '1'; });
    };
    // The image is rendered on demand the first time; when the renderer is busy (503/429)
    // try again a few times before giving up.
    let attempts = 0;
    img.onerror = () => {
        if (window._pageViewerIdx !== idx || !document.getElementById('page-viewer')) return;
        attempts += 1;
        if (attempts <= 3) {
            setTimeout(() => {
                if (window._pageViewerIdx === idx) img.src = page.url + (page.url.includes('?') ? '&' : '?') + 'retry=' + attempts;
            }, 2500 * attempts);
            return;
        }
        if (loading) loading.innerHTML = '<span>图片加载失败</span>';
    };
    img.src = page.url;
    img.alt = pageRefLabel(page);

    viewer.querySelector('.page-viewer-page').textContent = pageRefLabel(page);
    const info = viewer.querySelector('.page-viewer-info');
    info.textContent = `${pageRefLabel(page)} / 共 ${data.pdf_total_pages} 页`
        + (page.legacy_content_revised ? ' · 此页位置对应，现行版内容已修订' : '')
        + (data.text_identity === 'page-aligned-other-printing' ? ' · 检索文字来自较早印次，以 PDF 为准' : '');
    if (page.viewer_url) {
        const link = document.createElement('a');
        link.className = 'page-viewer-open-pdf';
        link.href = page.viewer_url;
        link.textContent = '📕 在 PDF 中打开';
        link.addEventListener('click', event => {
            if (!window.SunLibrary || event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
            const samePage = new URL(link.href, location.href).pathname === location.pathname;
            if (!samePage) return;
            event.preventDefault();
            closePageViewer();
            window.SunLibrary.openEdition(page.edition_id || data.edition_id, page.pdf_page, '', 'search');
        });
        info.append(' · ', link);
    }

    // Update thumbnail active state
    viewer.querySelectorAll('.page-thumb').forEach((t, i) => {
        t.classList.toggle('active', i === idx);
    });

    // Update nav button state
    viewer.querySelector('.page-nav-btn.prev').disabled = idx === 0;
    viewer.querySelector('.page-nav-btn.next').disabled = idx === data.pages.length - 1;

    // Preload adjacent images
    [idx - 1, idx + 1].forEach(pi => {
        if (pi >= 0 && pi < data.pages.length) {
            const preImg = new Image();
            preImg.src = data.pages[pi].url;
        }
    });
}

function pageViewerNav(delta) {
    const newIdx = (window._pageViewerIdx || 0) + delta;
    showPageViewerImage(newIdx);
}

function pageViewerGo(idx) {
    showPageViewerImage(idx);
}

function closePageViewer() {
    const viewer = document.getElementById('page-viewer');
    if (viewer) {
        viewer.classList.add('closing');
        setTimeout(() => viewer.remove(), 200);
    }
    document.body.style.overflow = '';
    if (window._pageViewerKeyHandler) {
        document.removeEventListener('keydown', window._pageViewerKeyHandler);
        delete window._pageViewerKeyHandler;
    }
}

// ── Knowledge Graph ───────────────────────────────────────
let currentGraphMode = 'cross';
let currentGraphSubject = '';

// Mode tab switching
document.querySelectorAll('.graph-mode').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.graph-mode').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentGraphMode = btn.dataset.mode;
        const sel = document.getElementById('graph-subject-select');
        sel.style.display = currentGraphMode === 'subject' ? 'inline-block' : 'none';
        if (currentGraphMode === 'cross') {
            loadGraph('cross');
        } else if (sel.value) {
            loadGraph('subject', sel.value);
        }
    });
});
document.getElementById('graph-subject-select').addEventListener('change', (e) => {
    if (e.target.value) loadGraph('subject', e.target.value);
});

async function loadGraph(mode = 'cross', subject = '') {
    const container = document.getElementById('graph-container');
    container.innerHTML = '<div class="loading" style="padding-top:40vh">加载知识图谱…</div>';

    try {
        let url = `${API}/api/graph/overview?mode=${mode}&limit=80&phase=${encodeURIComponent(PHASE)}`;
        if (subject) url += `&subject=${encodeURIComponent(subject)}`;
        const res = await fetch(url);
        const data = await res.json();

        // Populate subject selector if not done
        const sel = document.getElementById('graph-subject-select');
        if (sel.options.length <= 1 && data.subjects) {
            data.subjects.forEach(s => {
                const o = document.createElement('option');
                o.value = s; o.textContent = s;
                sel.appendChild(o);
            });
        }

        renderGraphNew(data, container, mode);
    } catch (e) {
        container.innerHTML = `<div class="loading">加载失败: ${e.message}</div>`;
    }
}

function renderGraphNew(data, container, mode) {
    const W = Math.max(container.clientWidth, 600);
    const H = Math.max(container.clientHeight, 500);
    const rawNodes = data.nodes || [];
    const rawLinks = data.links || [];

    if (rawNodes.length === 0) {
        container.innerHTML = '<div class="loading">暂无数据</div>';
        return;
    }

    container.innerHTML = '';

    // Prepare nodes with computed properties
    const nodeMap = {};
    const subjectNodes = rawNodes.filter(n => n.type === 'subject');
    const conceptNodes = rawNodes.filter(n => n.type === 'concept');

    subjectNodes.forEach(n => {
        n.r = 28;
        n.color = getSubjColors()[n.id] || '#6c5ce7';
        n.fx = null; n.fy = null;  // not fixed initially
        nodeMap[n.id] = n;
    });

    conceptNodes.forEach(n => {
        if (mode === 'cross') {
            n.r = Math.max(6, Math.min(22, (n.weight || 1) * 3));
            n.color = `hsl(${(n.weight || 1) * 40}, 70%, 55%)`;
        } else {
            n.r = Math.max(6, Math.min(20, Math.log2((n.weight || 1) + 1) * 3));
            n.color = '#6c5ce7';
        }
        nodeMap[n.id] = n;
    });

    // Prepare links (D3 needs source/target as node objects or IDs)
    const linkData = rawLinks.map(l => ({
        source: l.source,
        target: l.target,
        weight: l.weight || 1,
        relation: l.relation || '',
        description: l.description || ''
    })).filter(l => nodeMap[l.source] && nodeMap[l.target]);

    const allNodes = [...subjectNodes, ...conceptNodes];

    // Create SVG
    const svg = d3.select(container).append('svg')
        .attr('width', '100%')
        .attr('height', '100%')
        .attr('viewBox', `0 0 ${W} ${H}`)
        .attr('style', 'background: transparent; cursor: grab;');

    // Zoom behavior
    const zoomGroup = svg.append('g');
    const zoom = d3.zoom()
        .scaleExtent([0.3, 5])
        .on('zoom', (event) => {
            zoomGroup.attr('transform', event.transform);
        });
    svg.call(zoom);

    // Tooltip
    const tooltip = d3.select(container).append('div')
        .attr('class', 'graph-tooltip')
        .style('position', 'absolute')
        .style('display', 'none')
        .style('background', 'rgba(20, 20, 40, 0.95)')
        .style('color', '#fff')
        .style('padding', '8px 12px')
        .style('border-radius', '8px')
        .style('font-size', '12px')
        .style('pointer-events', 'none')
        .style('box-shadow', '0 4px 20px rgba(0,0,0,0.3)')
        .style('z-index', '100')
        .style('max-width', '250px')
        .style('backdrop-filter', 'blur(10px)');

    // Force simulation
    const simulation = d3.forceSimulation(allNodes)
        .force('link', d3.forceLink(linkData).id(d => d.id).distance(d => {
            const s = nodeMap[d.source?.id || d.source];
            const t = nodeMap[d.target?.id || d.target];
            if (!s || !t) return 120;
            if (s.type === 'subject' || t.type === 'subject') return 150;
            return 80;
        }).strength(0.3))
        .force('charge', d3.forceManyBody()
            .strength(d => d.type === 'subject' ? -400 : -100))
        .force('center', d3.forceCenter(W / 2, H / 2))
        .force('collision', d3.forceCollide().radius(d => d.r + 8))
        .force('x', d3.forceX(W / 2).strength(0.03))
        .force('y', d3.forceY(H / 2).strength(0.03));

    // Draw links
    const link = zoomGroup.append('g')
        .attr('class', 'links')
        .selectAll('line')
        .data(linkData)
        .join('line')
        .attr('stroke', d => {
            if (mode === 'cross') {
                const t = nodeMap[d.target?.id || d.target];
                return t && t.type === 'subject' ? (t.color || '#666') : 'rgba(108,92,231,0.25)';
            }
            return 'rgba(108,92,231,0.3)';
        })
        .attr('stroke-width', d => {
            if (mode === 'cross') return 1;
            return Math.max(1, Math.min(4, Math.log2((d.weight || 1) + 1)));
        })
        .attr('stroke-opacity', 0.3);

    link.on('mouseenter', function (event, d) {
        if (!d.relation && !d.description) return;
        const sourceId = d.source?.id || d.source;
        const targetId = d.target?.id || d.target;
        const info = [
            `<strong>${escHtml(sourceId)} ↔ ${escHtml(targetId)}</strong>`,
            d.relation ? `<div class="graph-tooltip-meta">${escHtml(d.relation)}</div>` : '',
            d.description ? `<div>${escHtml(d.description)}</div>` : ''
        ].filter(Boolean).join('');
        tooltip.html(info)
            .style('display', 'block')
            .style('left', (event.offsetX + 15) + 'px')
            .style('top', (event.offsetY - 10) + 'px');
        d3.select(this)
            .attr('stroke-opacity', 0.85)
            .attr('stroke-width', Math.max(2, Math.min(5, (d.weight || 1) + 1)));
    }).on('mousemove', function (event, d) {
        if (!d.relation && !d.description) return;
        tooltip.style('left', (event.offsetX + 15) + 'px')
            .style('top', (event.offsetY - 10) + 'px');
    }).on('mouseleave', function (event, d) {
        if (!d.relation && !d.description) return;
        tooltip.style('display', 'none');
        d3.select(this)
            .attr('stroke-opacity', 0.3)
            .attr('stroke-width', () => {
                if (mode === 'cross') return 1;
                return Math.max(1, Math.min(4, Math.log2((d.weight || 1) + 1)));
            });
    });

    // Draw nodes
    const node = zoomGroup.append('g')
        .attr('class', 'nodes')
        .selectAll('g')
        .data(allNodes)
        .join('g')
        .attr('class', d => `graph-node ${d.type}`)
        .style('cursor', d => d.type === 'concept' ? 'pointer' : 'grab');

    // Circles
    node.append('circle')
        .attr('r', d => d.r)
        .attr('fill', d => d.color || '#6c5ce7')
        .attr('fill-opacity', d => d.type === 'subject' ? 0.9 : 0.6)
        .attr('stroke', d => d.type === 'subject' ? 'rgba(255,255,255,0.5)' : 'rgba(108,92,231,0.5)')
        .attr('stroke-width', d => d.type === 'subject' ? 2.5 : 1)
        .style('transition', 'filter 0.2s, fill-opacity 0.2s');

    // Labels — fill comes from the .graph-label CSS rule (theme-aware),
    // stroke halo keeps them legible on both dark and light backgrounds.
    node.append('text')
        .attr('class', 'graph-label')
        .attr('dy', d => d.r + 14)
        .attr('text-anchor', 'middle')
        .attr('font-size', d => d.type === 'subject' ? 13 : 10)
        .attr('font-weight', d => d.type === 'subject' ? 700 : 500)
        .text(d => d.id);

    // Weight badges for concepts
    node.filter(d => d.type === 'concept' && d.weight > 1)
        .append('text')
        .attr('text-anchor', 'middle')
        .attr('dy', 4)
        .attr('font-size', 9)
        .attr('fill', '#fff')
        .text(d => mode === 'cross' ? `${d.weight}科` : `${d.weight}次`);

    // Hover interaction
    node.on('mouseenter', function (event, d) {
        // Highlight connected nodes/links
        const connectedIds = new Set();
        connectedIds.add(d.id);
        linkData.forEach(l => {
            const sid = l.source?.id || l.source;
            const tid = l.target?.id || l.target;
            if (sid === d.id) connectedIds.add(tid);
            if (tid === d.id) connectedIds.add(sid);
        });

        // Dim non-connected
        node.selectAll('circle')
            .attr('fill-opacity', n => connectedIds.has(n.id) ? (n.type === 'subject' ? 0.95 : 0.85) : 0.15);
        node.selectAll('.graph-label')
            .attr('fill-opacity', n => connectedIds.has(n.id) ? 1 : 0.2);
        link.attr('stroke-opacity', l => {
            const sid = l.source?.id || l.source;
            const tid = l.target?.id || l.target;
            return (sid === d.id || tid === d.id) ? 0.7 : 0.05;
        }).attr('stroke-width', l => {
            const sid = l.source?.id || l.source;
            const tid = l.target?.id || l.target;
            return (sid === d.id || tid === d.id) ? 3 : 1;
        });

        // Tooltip
        const subjects = d.subjects ? d.subjects.join(' · ') : (d.type === 'subject' ? d.id : '');
        const info = d.type === 'concept'
            ? `<strong>${d.id}</strong><br>${mode === 'cross' ? `跨${d.weight || 1}个学科` : `出现${d.weight || 1}次`}<br>${subjects}`
            : `<strong>${d.id}</strong>`;
        tooltip.html(info)
            .style('display', 'block')
            .style('left', (event.offsetX + 15) + 'px')
            .style('top', (event.offsetY - 10) + 'px');

        // Glow on hovered circle
        d3.select(this).select('circle')
            .attr('filter', 'drop-shadow(0 0 8px rgba(108,92,231,0.6))');
    })
        .on('mousemove', function (event) {
            tooltip.style('left', (event.offsetX + 15) + 'px')
                .style('top', (event.offsetY - 10) + 'px');
        })
        .on('mouseleave', function () {
            node.selectAll('circle')
                .attr('fill-opacity', d => d.type === 'subject' ? 0.9 : 0.6)
                .attr('filter', null);
            node.selectAll('.graph-label')
                .attr('fill-opacity', 1);
            link.attr('stroke-opacity', 0.3)
                .attr('stroke-width', d => {
                    if (mode === 'cross') return 1;
                    return Math.max(1, Math.min(4, Math.log2((d.weight || 1) + 1)));
                });
            tooltip.style('display', 'none');
        });

    // Click to search
    node.filter(d => d.type === 'concept')
        .on('click', (event, d) => {
            event.stopPropagation();
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
            document.querySelector('[data-view="search"]').classList.add('active');
            document.getElementById('view-search').classList.add('active');
            searchInput.value = d.id;
            doSearch(d.id);
        });

    // Drag behavior
    const drag = d3.drag()
        .on('start', (event, d) => {
            if (!event.active) simulation.alphaTarget(0.3).restart();
            d.fx = d.x; d.fy = d.y;
        })
        .on('drag', (event, d) => {
            d.fx = event.x; d.fy = event.y;
        })
        .on('end', (event, d) => {
            if (!event.active) simulation.alphaTarget(0);
            d.fx = null; d.fy = null;
        });
    node.call(drag);

    // Tick function
    simulation.on('tick', () => {
        link.attr('x1', d => d.source.x)
            .attr('y1', d => d.source.y)
            .attr('x2', d => d.target.x)
            .attr('y2', d => d.target.y);

        node.attr('transform', d => `translate(${d.x},${d.y})`);
    });

    // Reset zoom button
    const resetBtn = document.createElement('button');
    resetBtn.textContent = '🔄 重置';
    resetBtn.className = 'graph-reset-btn';
    resetBtn.style.cssText = 'position:absolute;top:10px;right:10px;padding:6px 12px;border:1px solid rgba(108,92,231,0.3);border-radius:8px;background:rgba(20,20,40,0.7);color:#fff;cursor:pointer;font-size:12px;backdrop-filter:blur(5px);z-index:10;';
    resetBtn.addEventListener('click', () => {
        svg.transition().duration(500).call(zoom.transform, d3.zoomIdentity);
        simulation.alpha(0.5).restart();
    });
    container.style.position = 'relative';
    container.appendChild(resetBtn);
}

// ── Init: Load stats ──────────────────────────────────────
function applyLiveStatsToUI(data) {
    latestStats = data;
    setSearchAIProviderLabel(data.ai_model || aiProviderLabel);

    // Textbook passages and exam rows are counted separately: total_chunks already
    // contains the exam rows, and an exam row is a record that may hold several items.
    const nSubjects = data.subjects_count || (data.subjects || []).length;
    const nBooks = Number(data.textbook_books || 0);
    const nTextbook = Number(data.textbook_chunks || 0);
    const nGaokao = PHASE === '高中' ? Number(data.gaokao_chunks || 0) : 0;
    const heroSub = document.getElementById('hero-sub');
    if (heroSub) {
        heroSub.textContent = `${DISPLAY_PHASE} ${nSubjects} 科 · ${nBooks.toLocaleString()} 册教材 ${nTextbook.toLocaleString()} 段原文`
            + (nGaokao > 0 ? ` · 高考题库 ${nGaokao.toLocaleString()} 条` : '');
    }
    const regions = (data.gaokao_by_region || []).map(item => {
        const [from, to] = item.year_range || [];
        const years = from && to ? (from === to ? `${from}` : `${from}–${to}`) : '年份不详';
        const subjects = (item.subjects || []).length === 1 ? `仅${item.subjects[0]}` : `${(item.subjects || []).length} 科`;
        const name = item.region === '全国' ? '全国卷' : (item.region === '未标注' ? '未标注地区' : `${item.region}卷`);
        return { text: `${name} ${years} · ${subjects} · ${Number(item.count || 0).toLocaleString()} 条`, item };
    });
    const dataNote = document.getElementById('data-note');
    const dataNoteBody = document.getElementById('data-note-body');
    if (dataNote && dataNoteBody) {
        const lines = [
            `教材：${nBooks.toLocaleString()} 册，扫描版经 OCR 后按页切成 ${nTextbook.toLocaleString()} 段原文；部分学科同时收了两个版本（例如数学 A 版与 B 版），检索结果会标出版本。`,
        ];
        if (nGaokao > 0) {
            const coverage = regions.length ? `：${regions.map(r => r.text).join('；')}` : '';
            lines.push(`高考题库 ${nGaokao.toLocaleString()} 条记录${coverage}。一条记录可能是一道题，也可能是含几道小题的一组；题库序号不是原卷题号，近两年的卷子尚不完整。`);
        }
        lines.push('AI 导读只依据每次检索到的原文生成，出处可点开核对。');
        dataNoteBody.innerHTML = lines.map(line => `<p>${escHtml(line)}</p>`).join('');
        dataNote.classList.remove('hidden');
    }

    const gaokaoHeroSub = document.getElementById('gk-hero-sub');
    if (gaokaoHeroSub && PHASE === '高中') {
        const multimodal = data.gaokao_multimodal ? ` · 含图 ${Number(data.gaokao_multimodal).toLocaleString()} 条` : '';
        gaokaoHeroSub.textContent = `${regions.map(r => r.text).join(' ｜ ') || `${nGaokao.toLocaleString()} 条`}${multimodal} · 一条记录可能含多道小题`;
    }

    const aboutScaleText = document.getElementById('about-scale-text');
    const aboutMetrics = document.getElementById('about-metrics');
    if (aboutScaleText) {
        const nBooks = (data.textbook_books || 0).toLocaleString();
        aboutScaleText.innerHTML = `当前线上索引已入库 <strong>${nBooks}</strong> 本教材语料；完整教材 PDF 下载区独立提供 <strong>${DOWNLOADABLE_LIBRARY_BOOKS}</strong> 本。`;
        if (aboutMetrics) {
            const chips = [
                `${nBooks} 本已入库教材`,
                `${DOWNLOADABLE_LIBRARY_BOOKS} 本 PDF 下载`,
                `${(data.total_chunks || 0).toLocaleString()} 条语料`,
                ...(PHASE === '高中' && (data.gaokao_chunks || 0) > 0 ? [`${(data.gaokao_chunks || 0).toLocaleString()} 道真题`] : []),
                data.faiss_enabled
                    ? `FAISS ${(data.faiss_vectors || 0).toLocaleString()} 向量`
                    : 'FAISS 未启用',
                `${data.ai_model || aiProviderLabel} 对话`,
            ];
            aboutMetrics.innerHTML = chips.map(text => `<span class="about-metric-chip">${escHtml(text)}</span>`).join('');
        }
    }

    const aboutCorpusText = document.getElementById('about-corpus-text');
    if (aboutCorpusText) {
        const nTotal = (data.total_chunks || 0).toLocaleString();
        const nTextbook = (data.textbook_chunks || 0).toLocaleString();
        const nGaokao = (data.gaokao_chunks || 0).toLocaleString();
        const nSubjects = data.subjects_count || (data.subjects || []).length;
        const [startYear, endYear] = data.gaokao_year_range || [];
        const yearRange = startYear && endYear ? `${startYear}-${endYear}` : '历年';
        const multimodal = data.gaokao_multimodal ? `，其中 <strong>${data.gaokao_multimodal.toLocaleString()}</strong> 道含图题` : '';
        const gaokaoCorpusPart = PHASE === '高中' && Number(String(nGaokao).replace(/,/g, '')) > 0
            ? ` / 真题 ${nGaokao}），覆盖 <strong>${nSubjects}</strong> 个学科；真题时间范围为 <strong>${yearRange}</strong>${multimodal}。`
            : `），覆盖 <strong>${nSubjects}</strong> 个学科。`;
        aboutCorpusText.innerHTML = `全文检索基于 SQLite FTS5，当前共有 <strong>${nTotal}</strong> 条结构化语料（教材 ${nTextbook}${gaokaoCorpusPart}`;
    }

    const aboutAiText = document.getElementById('about-ai-text');
    if (aboutAiText) {
        const faissPart = data.faiss_enabled
            ? `FAISS 已启用（${(data.faiss_vectors || 0).toLocaleString()} 条教材向量）`
            : 'FAISS 当前未启用';
        aboutAiText.textContent = `AI 对话只在你点击后经本站服务器发起，浏览器不直连模型服务；模型侧显示为 ${data.ai_model || aiProviderLabel}。检索底座为 SQLite FTS5 + ${faissPart}。`;
    }

    const aboutPrecomputeText = document.getElementById('about-precompute-text');
    if (aboutPrecomputeText) {
        const tables = data.ai_tables || {};
        const gaokaoLinksPart = PHASE === '高中' && Number(tables.gaokao_links || 0) > 0 ? `、真题关联 ${Number(tables.gaokao_links || 0).toLocaleString()}` : '';
        aboutPrecomputeText.textContent = `AI 预计算已入库：解读 ${Number(tables.explanations || 0).toLocaleString()}、同义/别名 ${Number(tables.synonyms || 0).toLocaleString()}、关系 ${Number(tables.relations || 0).toLocaleString()}、教材摘要 ${Number(tables.summaries || 0).toLocaleString()}${gaokaoLinksPart}。`;
    }

    const aboutRuntimeText = document.getElementById('about-runtime-text');
    if (aboutRuntimeText) {
        aboutRuntimeText.textContent = '线上运行只依赖 Docker 容器、/data/index 检索资产和 /state/cache 模型缓存；生产宿主机不承担 OCR、批处理和 FAISS 重建。';
    }

    const aboutDeployText = document.getElementById('about-deploy-text');
    if (aboutDeployText) {
        aboutDeployText.textContent = '发布由 GitHub Actions 触发，在 VPS 上使用临时干净 release checkout 构建 CPU-only 镜像，通过 /api/health 后切换；失败会自动回滚。';
    }
}

(async function init() {
    // Fetch subject metadata for this phase
    try {
        const metaRes = await fetch(`${API}/api/subject-meta?phase=${encodeURIComponent(PHASE)}`);
        if (metaRes.ok) window._subjectMeta = await metaRes.json();
    } catch (_) { /* silent */ }

    try {
        const res = await fetch(`${API}/api/stats?phase=${encodeURIComponent(PHASE)}`);
        const data = await res.json();
        const el = document.getElementById('stats-bar');
        const examBySubject = new Map((PHASE === '高中' ? data.gaokao_by_subject || [] : []).map(item => [item.name, item.count]));
        el.innerHTML = data.subjects.map(s => {
            const exam = examBySubject.get(s.name) || 0;
            const textbook = Math.max(0, s.count - exam);
            return `<div class="stat-chip">${s.icon} ${escHtml(s.name)} <span class="count">教材 ${textbook.toLocaleString()} 段${exam ? ` · 真题 ${exam.toLocaleString()} 条` : ''}</span></div>`;
        }).join('');
        el.classList.remove('hidden');
        applyLiveStatsToUI(data);
    } catch (e) { /* silent */ }

    // Hide gaokao nav for non-高中 phases
    if (PHASE !== '高中') {
        const gkBtn = document.querySelector('[data-view="gaokao"]');
        if (gkBtn) gkBtn.style.display = 'none';
    }
})();

// ── Gaokao View ─────────────────────────────────────────────────
let gaokaoInited = false;

async function initGaokao() {
    if (gaokaoInited) return;
    gaokaoInited = true;

    try {
        const res = await fetch(`${API}/api/gaokao/years`);
        const data = await res.json();

        // Populate subject dropdown
        const subEl = document.getElementById('gk-subject');
        data.subjects.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.name;
            opt.textContent = `${s.icon} ${s.name} (${s.count})`;
            subEl.appendChild(opt);
        });

        // Populate year dropdown
        const yearEl = document.getElementById('gk-year');
        data.years.forEach(y => {
            const opt = document.createElement('option');
            opt.value = y;
            opt.textContent = y + '年';
            yearEl.appendChild(opt);
        });

        // Populate category dropdown
        const catEl = document.getElementById('gk-category');
        data.categories.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c;
            opt.textContent = c;
            catEl.appendChild(opt);
        });
    } catch (e) {
        console.error('Failed to load gaokao filters:', e);
    }

    // Wire up search button
    document.getElementById('gk-search-btn').addEventListener('click', doGaokaoSearch);

    // Wire up link panel close
    document.getElementById('gk-link-close').addEventListener('click', () => {
        document.getElementById('gk-link-panel').classList.add('hidden');
    });

    // Auto-load initial results
    doGaokaoSearch();
}

async function doGaokaoSearch() {
    const params = new URLSearchParams({ limit: 50 });
    const subject = document.getElementById('gk-subject').value;
    const year = document.getElementById('gk-year').value;
    const category = document.getElementById('gk-category').value;
    const qtype = document.getElementById('gk-type').value;

    if (subject) params.set('subject', subject);
    if (year) params.set('year', year);
    if (category) params.set('category', category);
    if (qtype) params.set('question_type', qtype);

    const resultsEl = document.getElementById('gk-results');
    const countEl = document.getElementById('gk-count');
    resultsEl.innerHTML = '<div class="loading">加载中…</div>';

    try {
        const res = await fetch(`${API}/api/gaokao?${params}`);
        const data = await res.json();

        countEl.textContent = `共 ${data.total} 道题目`;
        countEl.classList.remove('hidden');

        if (data.questions.length === 0) {
            resultsEl.innerHTML = '<div class="loading">未找到符合条件的真题</div>';
            return;
        }

        renderGaokaoResults(data.questions);
        renderMath(resultsEl);
    } catch (e) {
        resultsEl.innerHTML = `<div class="loading">加载失败: ${e.message}</div>`;
    }
}

function renderGaokaoResults(questions) {
    const resultsEl = document.getElementById('gk-results');
    resultsEl.innerHTML = questions.map(q => {
        // Split text into question and analysis/answer parts
        const parts = (q.text || '').split(/\n【解析】\n|【解析】/);
        const questionText = parts[0] || '';
        const analysisParts = (q.text || '').split(/\n【答案】/);
        const hasAnalysis = q.text && q.text.includes('【解析】');

        return `
        <div class="gaokao-card" data-id="${q.id}">
            <div class="gk-card-header" onclick="this.parentElement.classList.toggle('expanded')">
                <div class="gk-card-meta">
                    <span class="gk-badge" style="--badge-color:${q.color || '#6c5ce7'}">${q.icon || '📚'} ${q.subject}</span>
                    <span class="gk-year">${q.year}年</span>
                    <span class="gk-category">${escHtml(q.category)}</span>
                    <span class="gk-type">${q.question_type === 'objective' ? '客观题' : '主观题'}</span>
                    ${q.score ? `<span class="gk-score">${q.score}分</span>` : ''}
                </div>
                <div class="gk-card-title">${escHtml(q.title)}</div>
                <div class="gk-card-preview">${sanitizeSnippet(questionText.slice(0, 200))}</div>
            </div>
            <div class="gk-card-body">
                <div class="gk-question">${renderGaokaoText(questionText)}</div>
                ${hasAnalysis ? `
                    <details class="gk-analysis">
                        <summary>💡 查看解析和答案</summary>
                        <div class="gk-analysis-content">${escHtml(q.text.split('【解析】').slice(1).join('【解析】'))}</div>
                    </details>
                ` : ''}
                ${q.answer ? `<div class="gk-answer">【答案】${escHtml(q.answer)}</div>` : ''}
                <div class="gk-actions">
                    <button class="gk-link-btn" onclick="event.stopPropagation(); findTextbookLinks(${q.id})">📚 查找教材关联</button>
                    <button class="gk-ai-btn" onclick="event.stopPropagation(); requestGaokaoAI(${q.id}, this)">✨ AI 关联分析</button>
                </div>
                <div class="gk-ai-result" id="gk-ai-${q.id}"></div>
            </div>
        </div>
        `;
    }).join('');
}

async function findTextbookLinks(questionId) {
    const panel = document.getElementById('gk-link-panel');
    const content = document.getElementById('gk-link-content');
    panel.classList.remove('hidden');
    content.innerHTML = '<div class="loading">搜索教材关联中…</div>';

    try {
        const res = await fetch(`${API}/api/gaokao/link?question_id=${questionId}&limit=10`);
        const data = await res.json();

        if ((!data.links || data.links.length === 0) && (!data.cross_links || data.cross_links.length === 0)) {
            content.innerHTML = '<div class="loading">未找到直接相关的教材内容</div>';
            return;
        }

        const renderLinkCard = (l) => {
            const scoreColor = l.relevance_score >= 70 ? '#27ae60' :
                l.relevance_score >= 40 ? '#f39c12' : '#95a5a6';
            const typeLabel = l.link_type === 'implicit'
                ? '<span class="link-type-tag implicit">🔮 隐性关联</span>'
                : l.link_type === 'precomputed'
                    ? '<span class="link-type-tag precomputed">⚡ 预计算</span>'
                    : '<span class="link-type-tag explicit">📌 显性关联</span>';
            const conceptTags = (l.matched_concepts || []).map(c =>
                `<span class="concept-tag">${escHtml(c)}</span>`
            ).join('');

            return `
            <div class="gk-link-card">
                <div class="gk-link-meta">
                    <span class="gk-badge" style="--badge-color:${l.color || '#6c5ce7'}">${l.icon || '📚'} ${l.subject}</span>
                    ${typeLabel}
                    <span>${escHtml(l.title)} · §${l.section}</span>
                </div>
                <div class="gk-link-score-row">
                    <div class="gk-link-score-bar">
                        <div class="gk-link-score-fill" style="width:${l.relevance_score || 0}%;background:${scoreColor}"></div>
                    </div>
                    <span class="gk-link-score-text" style="color:${scoreColor}">${l.relevance_score || 0}%</span>
                </div>
                ${conceptTags ? `<div class="gk-concept-tags">${conceptTags}</div>` : ''}
                <div class="gk-link-snippet">${sanitizeSnippet(l.snippet)}</div>
            </div>`;
        };

        // Render matched concepts overview
        const conceptOverview = (data.matched_concepts || []).map(c => {
            const crossIcon = c.source === 'precomputed' ? '⚡' : (c.is_cross ? '🌐' : '📘');
            const subjs = (c.subjects || []).join('·');
            return `<span class="matched-concept ${c.is_cross ? 'cross' : 'same'}" title="${subjs}">${crossIcon} ${escHtml(c.concept)}</span>`;
        }).join('');

        const expandedInfo = (data.expanded_terms || []).length > 0
            ? `<div class="gk-expanded-terms">🔮 隐性扩展：${data.expanded_terms.map(t => `<span class="expanded-term">${escHtml(t)}</span>`).join(' ')}</div>`
            : '';
        const precomputed = data.precomputed_analysis || null;
        const precomputedInfo = precomputed ? `
            <div class="gk-precomputed-analysis">
                ${precomputed.summary ? `<div class="gk-precomputed-summary">⚡ 预计算结论：${escHtml(precomputed.summary)}</div>` : ''}
                ${(precomputed.knowledge_points || []).length > 0 ? `<div class="gk-precomputed-tags">知识点：${precomputed.knowledge_points.map(t => `<span class="term-tag">${escHtml(t)}</span>`).join(' ')}</div>` : ''}
                ${(precomputed.textbook_refs || []).length > 0 ? `<div class="gk-precomputed-refs">教材锚点：${precomputed.textbook_refs.map(t => `<span class="term-tag">${escHtml(t)}</span>`).join(' ')}</div>` : ''}
            </div>
        ` : '';

        content.innerHTML = `
            <div class="gk-link-question">
                <strong>${escHtml(data.question_title)}</strong>
                ${conceptOverview ? `<div class="gk-matched-concepts">知识点匹配：${conceptOverview}</div>` : ''}
                <span class="gk-link-terms">搜索关键词：${(data.search_terms || []).map(t => `<span class="term-tag">${escHtml(t)}</span>`).join(' ') || '（无）'}</span>
                ${expandedInfo}
                ${precomputedInfo}
            </div>
            ${data.links && data.links.length > 0 ? `
                <h4 class="gk-section-title">📚 同学科关联（${data.question_subject}）</h4>
                <div class="gk-link-results">${data.links.map(renderLinkCard).join('')}</div>
            ` : ''}
            ${data.cross_links && data.cross_links.length > 0 ? `
                <h4 class="gk-section-title">🔗 跨学科关联</h4>
                <div class="gk-link-results">${data.cross_links.map(renderLinkCard).join('')}</div>
            ` : ''}
        `;
        renderMath(content);
    } catch (e) {
        content.innerHTML = `<div class="loading">加载失败: ${e.message}</div>`;
    }
}

function renderGaokaoAISources(items) {
    if (!items || !items.length) return '';
    return `
        <div class="gk-ai-sources">
            ${items.slice(0, 5).map(item => `
                <button class="ai-source-chip gk-ai-source-chip" type="button"
                    data-book-key="${escAttr(item.book_key || '')}"
                    data-page="${item.section ?? 0}"
                    data-total-pages="${(window._bookPages && window._bookPages[item.book_key]?.pages) || 0}">
                    ${escHtml(`${item.subject}·${item.title}·§${item.section}`)}
                </button>
            `).join('')}
        </div>
    `;
}

// Explicit click only. The browser sends just the question id; the server builds the
// prompt from its own retrieval and makes the single model call.
async function requestGaokaoAI(questionId, btn) {
    const resultEl = document.getElementById(`gk-ai-${questionId}`);
    if (!resultEl || !window.SunAI) return;
    if (resultEl.dataset.aiState === 'pending') return;
    if (resultEl.dataset.aiState === 'done') {
        resultEl.classList.toggle('hidden');
        return;
    }

    resultEl.dataset.aiState = 'pending';
    btn.disabled = true;
    btn.textContent = '✨ 分析中…';
    resultEl.innerHTML = '<div class="loading">✨ AI 正在分析真题与教材的关联… <button class="ai-followup-chip gk-ai-cancel" type="button">取消</button></div>';
    resultEl.classList.remove('hidden');

    const request = window.SunAI.ask(`${API}/api/gaokao/chat`, { question_id: Number(questionId), phase: PHASE });
    const cancelBtn = resultEl.querySelector('.gk-ai-cancel');
    if (cancelBtn) cancelBtn.addEventListener('click', event => { event.stopPropagation(); request.cancel(); });

    try {
        const data = await request.promise;
        const question = data.question || {};
        resultEl.innerHTML = `
            <div class="gk-ai-answer">
                <div class="gk-ai-header">✨ AI 关联分析 <span class="ai-model">${escHtml(data.provider || 'Gemini')}</span></div>
                <div class="gk-ai-meta">
                    <span class="gk-ai-meta-chip">${escHtml(question.subject || '未知学科')}</span>
                    <span class="gk-ai-meta-chip">${escHtml(String(question.year || '未知年份'))}</span>
                    ${question.category ? `<span class="gk-ai-meta-chip">${escHtml(question.category)}</span>` : ''}
                    <span class="gk-ai-meta-chip">同学科 ${Number(question.link_count) || 0}</span>
                    <span class="gk-ai-meta-chip">跨学科 ${Number(question.cross_link_count) || 0}</span>
                </div>
                <div class="gk-ai-text">${escHtml(String(data.answer || ''))}</div>
                ${renderGaokaoAISources(data.sources || [])}
            </div>
        `;
        resultEl.dataset.aiState = 'done';
        bindOpenPageButtons(resultEl, '.gk-ai-source-chip');
        renderMath(resultEl);
    } catch (e) {
        resultEl.dataset.aiState = 'error';
        const message = e && e.cancelled ? e.message : `请求失败：${(e && e.message) || '未知错误'}`;
        resultEl.innerHTML = `<div class="loading">${escHtml(message)} <button class="ai-followup-chip gk-ai-retry" type="button">重试</button></div>`;
        const retryBtn = resultEl.querySelector('.gk-ai-retry');
        if (retryBtn) {
            retryBtn.addEventListener('click', event => {
                event.stopPropagation();
                requestGaokaoAI(questionId, btn);
            });
        }
    } finally {
        btn.disabled = false;
        btn.textContent = '✨ AI 关联分析';
    }
}

// Render gaokao question text with images from R2 CDN
function renderGaokaoText(text) {
    if (!text) return '';
    // Escape HTML
    let s = escHtml(text);
    // Convert markdown images to <img> tags using R2 CDN
    s = s.replace(/!\[([^\]]*)\]\(https:\/\/img\.rdfzer\.com\/gaokao\/([^)]+)\)/g, (_, alt, src) => {
        return `<img class="gk-question-img" src="https://img.rdfzer.com/gaokao/${src}" alt="${alt || '题目图片'}" loading="lazy">`;
    });
    return s;
}

// ── Math Rendering ──────────────────────────────────────────
function renderMath(el) {
    if (el && window.renderMathInElement && window.katex) {
        try {
            renderMathInElement(el, {
            delimiters: [
                { left: '$$', right: '$$', display: true },
                { left: '$', right: '$', display: false },
                { left: '\\(', right: '\\)', display: false },
                { left: '\\[', right: '\\]', display: true }
            ],
            throwOnError: false,
            strict: 'ignore',
            errorColor: '#e74c3c'
            });
        } catch (_) { /* Keep UI usable if third-party auto-render trips on malformed OCR math. */ }
    }
}

// ── Data Insights ─────────────────────────────────────────────────
let insightsLoaded = false;
const panelLoaded = {};
const panelLoaders = { freq: loadFreqChart, heatmap: loadHeatmap, coverage: loadCoverage, breadth: loadBreadth };

// Insight tab switching — lazy-load each panel when first shown
document.querySelectorAll('.insight-tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.insight-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.insight-panel').forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        const panelName = tab.dataset.panel;
        document.getElementById('panel-' + panelName).classList.add('active');
        // Lazy load: only fetch data when panel is first shown
        if (!panelLoaded[panelName] && panelLoaders[panelName]) {
            panelLoaded[panelName] = true;
            panelLoaders[panelName]();
        }
    });
});

function getSubjColors() {
    const colors = { 'hanjia': '#95a5a6' };
    for (const [k, v] of Object.entries(window._subjectMeta)) {
        colors[v.display_subject || k] = v.color;
        colors[k] = v.color;
    }
    return colors;
}

// Theme-aware chart colors. Reads CSS variables so charts stay readable
// whether the page is on the dark base palette or the BDFZ light parchment refresh.
function getChartColors() {
    const cs = getComputedStyle(document.documentElement);
    const text = (cs.getPropertyValue('--text') || '').trim() || '#3a3530';
    const dim = (cs.getPropertyValue('--text-dim') || '').trim() || '#7a716a';
    const muted = (cs.getPropertyValue('--text-muted') || '').trim() || '#a89d96';
    return { text, dim, muted };
}

async function loadInsights() {
    if (insightsLoaded) return;
    insightsLoaded = true;
    // Populate subject selector from stats
    try {
        const sr = await fetch(`${API}/api/stats?phase=${encodeURIComponent(PHASE)}`);
        const sd = await sr.json();
        const sel = document.getElementById('freq-subject');
        sd.subjects.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.name; opt.textContent = `${s.icon} ${s.name}`;
            sel.appendChild(opt);
        });
    } catch (_) { }

    // Only load the initially visible panel (freq)
    panelLoaded['freq'] = true;
    loadFreqChart();

    // Listen for filter changes
    document.getElementById('freq-source').addEventListener('change', loadFreqChart);
    document.getElementById('freq-subject').addEventListener('change', loadFreqChart);
}

// ── Word Frequency Chart (D3 Horizontal Bars) ──
async function loadFreqChart() {
    const container = document.getElementById('freq-chart');
    container.innerHTML = '<div class="loading">加载词频数据…</div>';
    const source = document.getElementById('freq-source').value;
    const subject = document.getElementById('freq-subject').value;
    try {
        const url = `${API}/api/analytics/word-freq?source=${source}&limit=30&phase=${encodeURIComponent(PHASE)}${subject ? '&subject=' + encodeURIComponent(subject) : ''}`;
        const res = await fetch(url);
        const data = await res.json();
        const freqs = data.frequencies || [];
        if (freqs.length === 0) { container.innerHTML = '<div class="loading">暂无数据</div>'; return; }
        renderFreqBars(container, freqs);
    } catch (e) { container.innerHTML = `<div class="loading">加载失败: ${e.message}</div>`; }
}

function renderFreqBars(container, data) {
    container.innerHTML = '';
    const margin = { top: 10, right: 40, bottom: 30, left: 100 };
    const W = Math.min(container.clientWidth, 700);
    const barH = 26;
    const H = margin.top + margin.bottom + data.length * barH;

    const svg = d3.select(container).append('svg')
        .attr('width', W).attr('height', H);

    const maxVal = d3.max(data, d => d.count);
    const x = d3.scaleLinear().domain([0, maxVal]).range([0, W - margin.left - margin.right]);
    const y = d3.scaleBand().domain(data.map(d => d.term)).range([margin.top, H - margin.bottom]).padding(0.25);

    const g = svg.append('g').attr('transform', `translate(${margin.left},0)`);

    // Gradient
    const grad = svg.append('defs').append('linearGradient').attr('id', 'bar-grad');
    grad.append('stop').attr('offset', '0%').attr('stop-color', '#6c5ce7');
    grad.append('stop').attr('offset', '100%').attr('stop-color', '#a29bfe');

    g.selectAll('rect').data(data).join('rect')
        .attr('y', d => y(d.term))
        .attr('height', y.bandwidth())
        .attr('x', 0)
        .attr('width', 0)
        .attr('fill', 'url(#bar-grad)')
        .attr('rx', 4)
        .transition().duration(600).delay((d, i) => i * 20)
        .attr('width', d => x(d.count));

    const chartColors = getChartColors();

    // Labels
    g.selectAll('.bar-label').data(data).join('text')
        .attr('class', 'bar-label')
        .attr('x', d => x(d.count) + 5)
        .attr('y', d => y(d.term) + y.bandwidth() / 2)
        .attr('dy', '0.35em')
        .attr('fill', chartColors.dim)
        .attr('font-size', '11px')
        .attr('font-weight', '600')
        .text(d => d.count.toLocaleString());

    // Y axis labels
    svg.selectAll('.term-label').data(data).join('text')
        .attr('class', 'term-label')
        .attr('x', margin.left - 6)
        .attr('y', d => y(d.term) + y.bandwidth() / 2)
        .attr('dy', '0.35em')
        .attr('text-anchor', 'end')
        .attr('fill', chartColors.text)
        .attr('font-size', '12px')
        .attr('font-weight', '600')
        .attr('cursor', 'pointer')
        .text(d => d.term)
        .on('click', (e, d) => {
            searchInput.value = d.term; doSearch(d.term);
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
            document.querySelector('[data-view="search"]').classList.add('active');
            document.getElementById('view-search').classList.add('active');
        });
}

// ── Heatmap ──
async function loadHeatmap() {
    const container = document.getElementById('heatmap-chart');
    container.innerHTML = '<div class="loading">加载学科关联矩阵…</div>';
    try {
        const res = await fetch(`${API}/api/analytics/heatmap?phase=${encodeURIComponent(PHASE)}`);
        const data = await res.json();
        renderHeatmap(container, data);
    } catch (e) { container.innerHTML = `<div class="loading">加载失败: ${e.message}</div>`; }
}

function getHeatmapTextStyle(fillColor) {
    const color = d3.color(fillColor);
    if (!color) {
        return { fill: '#3a3530', stroke: 'rgba(255,255,255,0.85)' };
    }
    const luminance = (0.299 * color.r + 0.587 * color.g + 0.114 * color.b) / 255;
    if (luminance >= 0.55) {
        return { fill: '#1f2933', stroke: 'rgba(255,255,255,0.85)' };
    }
    return { fill: '#fbfaf5', stroke: 'rgba(0,0,0,0.45)' };
}

function renderHeatmap(container, data) {
    container.innerHTML = '';
    const subjects = data.subjects;
    const matrix = data.matrix;
    const n = subjects.length;
    const cw = Math.max(container.clientWidth, 450);
    const cellSize = Math.min(55, (Math.min(cw, 600) - 80) / n);
    const margin = { top: 80, left: 80 };
    const W = margin.left + n * cellSize + 20;
    const H = margin.top + n * cellSize + 20;

    const svg = d3.select(container).append('svg')
        .attr('width', W).attr('height', H);

    const maxVal = d3.max(matrix.flat().filter(v => v > 0));
    const color = d3.scaleSequential(d3.interpolateYlOrRd).domain([0, maxVal]);

    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    // Cells
    for (let i = 0; i < n; i++) {
        for (let j = 0; j < n; j++) {
            const val = matrix[i][j];
            const fillColor = val > 0 ? color(val) : 'rgba(255,255,255,0.03)';
            const cell = g.append('rect')
                .attr('x', j * cellSize).attr('y', i * cellSize)
                .attr('width', cellSize - 2).attr('height', cellSize - 2)
                .attr('fill', fillColor)
                .attr('rx', 4)
                .attr('opacity', 0)
                .transition().duration(400).delay((i + j) * 30)
                .attr('opacity', 1);

            if (val > 0) {
                const textStyle = getHeatmapTextStyle(fillColor);
                g.append('text')
                    .attr('x', j * cellSize + cellSize / 2 - 1)
                    .attr('y', i * cellSize + cellSize / 2)
                    .attr('dy', '0.35em')
                    .attr('text-anchor', 'middle')
                    .attr('fill', textStyle.fill)
                    .attr('stroke', textStyle.stroke)
                    .attr('stroke-width', 2)
                    .attr('paint-order', 'stroke')
                    .attr('font-size', '11px')
                    .attr('font-weight', '600')
                    .text(val);
            }
        }
    }

    // Axis labels — subject-tinted text with stroke halo so it stays
    // legible against any background luminance.
    const axisFallback = getChartColors().text;
    subjects.forEach((s, i) => {
        const tint = getSubjColors()[s] || axisFallback;
        svg.append('text')
            .attr('x', margin.left + i * cellSize + cellSize / 2 - 1)
            .attr('y', margin.top - 8)
            .attr('text-anchor', 'middle')
            .attr('fill', tint)
            .attr('stroke', 'rgba(255, 255, 255, 0.7)')
            .attr('stroke-width', 3)
            .attr('paint-order', 'stroke')
            .attr('font-size', '12px')
            .attr('font-weight', '700')
            .text(s);
        svg.append('text')
            .attr('x', margin.left - 8)
            .attr('y', margin.top + i * cellSize + cellSize / 2)
            .attr('dy', '0.35em')
            .attr('text-anchor', 'end')
            .attr('fill', tint)
            .attr('stroke', 'rgba(255, 255, 255, 0.7)')
            .attr('stroke-width', 3)
            .attr('paint-order', 'stroke')
            .attr('font-size', '12px')
            .attr('font-weight', '700')
            .text(s);
    });

    // Caption
    svg.append('text')
        .attr('x', W / 2).attr('y', H + 5)
        .attr('text-anchor', 'middle')
        .attr('fill', getChartColors().dim)
        .attr('font-size', '11px')
        .attr('font-weight', '600')
        .text(`共 ${data.total_concepts} 个跨学科学术术语`);
}

// ── Coverage Analysis ──
async function loadCoverage() {
    try {
        const res = await fetch(`${API}/api/analytics/coverage?limit=15&phase=${encodeURIComponent(PHASE)}`);
        const data = await res.json();
        renderCoverageList('coverage-hidden', data.hidden_exam_focus, 'exam');
        renderCoverageList('coverage-low', data.low_exam_focus, 'textbook');
    } catch (e) { }
}

function renderCoverageList(containerId, items, highlight) {
    const el = document.getElementById(containerId);
    el.innerHTML = items.map(item => `
        <div class="coverage-item" data-term="${item.term}">
            <span class="coverage-term">${item.term}</span>
            <div class="coverage-bars">
                <span class="cov-bar cov-textbook" style="width:${Math.min(100, item.textbook / 5)}%"
                    title="教材 ${item.textbook} 次">📚 ${item.textbook}</span>
                <span class="cov-bar cov-gaokao" style="width:${Math.min(100, item.gaokao * 5)}%"
                    title="真题 ${item.gaokao} 次">📝 ${item.gaokao}</span>
            </div>
            <span class="coverage-ratio ${highlight === 'exam' ? 'ratio-hot' : 'ratio-cool'}">${item.ratio}%</span>
        </div>
    `).join('');

    el.querySelectorAll('.coverage-item').forEach(item => {
        item.addEventListener('click', () => {
            searchInput.value = item.dataset.term;
            doSearch(item.dataset.term);
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
            document.querySelector('[data-view="search"]').classList.add('active');
            document.getElementById('view-search').classList.add('active');
        });
    });
}

// ── Concept Breadth ──
async function loadBreadth() {
    const container = document.getElementById('breadth-chart');
    container.innerHTML = '<div class="loading">加载概念广度排名…</div>';
    try {
        const res = await fetch(`${API}/api/analytics/concept-breadth?limit=30&phase=${encodeURIComponent(PHASE)}`);
        const data = await res.json();
        renderBreadth(container, data.concepts || []);
    } catch (e) { container.innerHTML = `<div class="loading">加载失败</div>`; }
}

function renderBreadth(container, concepts) {
    container.innerHTML = '';
    const margin = { top: 10, right: 40, bottom: 30, left: 120 };
    const W = Math.min(Math.max(container.clientWidth, 450), 700);
    const barH = 28;
    const H = margin.top + margin.bottom + concepts.length * barH;

    const svg = d3.select(container).append('svg')
        .attr('width', W).attr('height', H);

    const maxSubj = d3.max(concepts, d => d.subjects);
    const x = d3.scaleLinear().domain([0, maxSubj]).range([0, W - margin.left - margin.right]);
    const y = d3.scaleBand().domain(concepts.map(d => d.term)).range([margin.top, H - margin.bottom]).padding(0.2);

    const g = svg.append('g').attr('transform', `translate(${margin.left},0)`);

    const colorScale = d3.scaleSequential(d3.interpolatePlasma).domain([2, maxSubj]);

    g.selectAll('rect').data(concepts).join('rect')
        .attr('y', d => y(d.term))
        .attr('height', y.bandwidth())
        .attr('x', 0)
        .attr('width', 0)
        .attr('fill', d => colorScale(d.subjects))
        .attr('rx', 4)
        .transition().duration(500).delay((d, i) => i * 15)
        .attr('width', d => x(d.subjects));

    const breadthColors = getChartColors();

    g.selectAll('.breadth-val').data(concepts).join('text')
        .attr('class', 'breadth-val')
        .attr('x', d => x(d.subjects) + 5)
        .attr('y', d => y(d.term) + y.bandwidth() / 2)
        .attr('dy', '0.35em')
        .attr('fill', breadthColors.dim)
        .attr('font-size', '11px')
        .attr('font-weight', '600')
        .text(d => `${d.subjects} 科`);

    svg.selectAll('.breadth-label').data(concepts).join('text')
        .attr('class', 'breadth-label')
        .attr('x', margin.left - 6)
        .attr('y', d => y(d.term) + y.bandwidth() / 2)
        .attr('dy', '0.35em')
        .attr('text-anchor', 'end')
        .attr('fill', breadthColors.text)
        .attr('font-size', '12px')
        .attr('font-weight', '600')
        .attr('cursor', 'pointer')
        .text(d => d.term)
        .on('click', (e, d) => {
            searchInput.value = d.term; doSearch(d.term);
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
            document.querySelector('[data-view="search"]').classList.add('active');
            document.getElementById('view-search').classList.add('active');
        });
}

// ── Search Result Concept Subgraph ──────────────────────────────
async function loadSearchGraph(term) {
    const existing = document.getElementById('search-graph-section');
    if (existing) existing.remove();

    try {
        const res = await fetch(`${API}/api/graph/search?q=${encodeURIComponent(term)}&phase=${encodeURIComponent(PHASE)}`);
        const data = await res.json();
        if (!data.nodes || data.nodes.length < 3) return;

        const section = document.createElement('details');
        section.id = 'search-graph-section';
        section.className = 'result-group search-graph-section';
        section.innerHTML = '<summary class="group-header"><span class="group-icon" aria-hidden="true">🔗</span><span class="group-name">关联知识图谱</span><span class="group-books">术语共现关系，点节点可检索</span><span class="group-chevron" aria-hidden="true"></span></summary>';

        const graphDiv = document.createElement('div');
        graphDiv.className = 'search-graph-container';
        section.appendChild(graphDiv);

        const resultArea = document.getElementById('results');
        if (resultArea) resultArea.parentElement.insertBefore(section, resultArea.nextSibling);

        renderSearchSubgraph(graphDiv, data);
    } catch (_) { }
}

function renderSearchSubgraph(container, data) {
    const W = Math.min(container.clientWidth || 500, 600);
    const H = 320;

    const svg = d3.select(container).append('svg')
        .attr('width', W).attr('height', H)
        .attr('viewBox', `0 0 ${W} ${H}`);

    const nodes = data.nodes.map(n => ({ ...n }));
    const links = data.links.map(l => ({ ...l }));

    const sim = d3.forceSimulation(nodes)
        .force('link', d3.forceLink(links).id(d => d.id).distance(80))
        .force('charge', d3.forceManyBody().strength(-200))
        .force('center', d3.forceCenter(W / 2, H / 2))
        .force('collision', d3.forceCollide(25));

    const link = svg.append('g').selectAll('line').data(links).join('line')
        .attr('stroke', d => getSubjColors()[d.subject] || '#6c5ce7')
        .attr('stroke-opacity', 0.4)
        .attr('stroke-width', 1.5);

    const node = svg.append('g').selectAll('g').data(nodes).join('g')
        .attr('cursor', 'pointer')
        .on('click', (e, d) => { if (d.type !== 'subject') { searchInput.value = d.id; doSearch(d.id); } })
        .call(d3.drag()
            .on('start', (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
            .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
            .on('end', (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; })
        );

    node.append('circle')
        .attr('r', d => d.type === 'center' ? 18 : d.type === 'subject' ? 14 : 10)
        .attr('fill', d => d.type === 'center' ? '#6c5ce7' : d.type === 'subject' ? (getSubjColors()[d.id] || '#555') : 'rgba(108,92,231,0.5)')
        .attr('stroke', d => d.type === 'center' ? '#a29bfe' : 'none')
        .attr('stroke-width', 2);

    const subgraphColors = getChartColors();
    node.append('text')
        .attr('dy', d => d.type === 'center' ? 28 : 22)
        .attr('text-anchor', 'middle')
        .attr('fill', subgraphColors.text)
        .attr('stroke', 'rgba(255, 255, 255, 0.9)')
        .attr('stroke-width', 2.4)
        .attr('paint-order', 'stroke')
        .attr('font-size', d => d.type === 'center' ? '13px' : d.type === 'subject' ? '12px' : '10px')
        .attr('font-weight', d => d.type === 'center' ? '700' : '500')
        .text(d => d.id);

    sim.on('tick', () => {
        link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
            .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
        node.attr('transform', d => `translate(${Math.max(20, Math.min(W - 20, d.x))},${Math.max(20, Math.min(H - 20, d.y))})`);
    });
}
