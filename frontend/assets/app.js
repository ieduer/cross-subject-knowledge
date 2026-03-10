/* ── Cross-Subject Knowledge Platform · Frontend ── */

const API = '';  // same origin
// Canonical external AI gateway for this project: Worker custom domain -> service `apis` / production.
const AI_API = 'https://ai.bdfz.net/';
const AI_API_FALLBACK = 'https://apis.bdfz.workers.dev/';
const AI_CHAT_MODEL = 'gemini-flash-latest';
const AI_CONTEXT_TIMEOUT_MS = 15000;
const AI_SERVER_CHAT_TIMEOUT_MS = 45000;
const AI_DIRECT_REQUEST_TIMEOUT_MS = 25000;
const AI_BROWSER_FALLBACK_DELAY_MS = 8000;
const IMG_CDN = 'https://img.rdfzer.com';
const DEFAULT_FRONTEND_VERSION = '2026.03.08-r16';
const FRONTEND_VERSION_FILE = '/assets/version.json';
const AI_MEMORY_LIMIT = 12;
const DOWNLOADABLE_LIBRARY_BOOKS = 316;
let latestStats = null;
let aiProviderLabel = 'Gemini';

// ── AI Chat (Memory + Copy) ───────────────────────────────
const aiPanel = document.getElementById('ai-panel');
const aiBtn = document.getElementById('ai-btn');
const aiResult = document.getElementById('ai-result');
const aiContent = document.getElementById('ai-content');
const aiFollowupInput = document.getElementById('ai-followup-input');
const aiSendBtn = document.getElementById('ai-send-btn');
const aiCopyBtn = document.getElementById('ai-copy-btn');
const aiStarters = document.getElementById('ai-starters');

let aiConversation = [];
let aiRequestPending = false;

if (aiBtn) aiBtn.addEventListener('click', () => requestAISynthesis());
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
    const badge = document.querySelector('#ai-result .ai-model');
    if (badge) badge.textContent = aiProviderLabel;
}

function logClientAIChat({ query, userMessage, summary, provider, success, error }) {
    fetch(`${API}/api/chat/log`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            query,
            user_message: userMessage,
            summary: summary || {},
            provider: provider || aiProviderLabel,
            success: Boolean(success),
            error: error || '',
        }),
    }).catch(() => {});
}

async function fetchWithTimeout(url, options = {}, timeoutMs = AI_SERVER_CHAT_TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await fetch(url, { ...options, signal: controller.signal });
    } catch (error) {
        if (error.name === 'AbortError') {
            throw new Error(`请求超时 (${Math.round(timeoutMs / 1000)}s)`);
        }
        throw error;
    } finally {
        clearTimeout(timer);
    }
}

function delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function readJsonLike(response) {
    const raw = await response.text();
    if (!raw) return { data: {}, raw: '' };
    try {
        return { data: JSON.parse(raw), raw };
    } catch (_) {
        return { data: null, raw };
    }
}

function responseErrorMessage(response, data, raw, fallbackMessage) {
    if (data && typeof data === 'object') {
        if (typeof data.detail === 'string' && data.detail.trim()) return data.detail.trim();
        if (typeof data.error === 'string' && data.error.trim()) return data.error.trim();
        if (typeof data.message === 'string' && data.message.trim()) return data.message.trim();
    }

    const normalizedRaw = String(raw || '').replace(/\s+/g, ' ').trim();
    if (!normalizedRaw) return `${fallbackMessage} (${response.status})`;
    if (normalizedRaw.startsWith('<!DOCTYPE') || normalizedRaw.startsWith('<html')) {
        return `${fallbackMessage} (${response.status})`;
    }
    return normalizedRaw.slice(0, 180);
}

function renderAIStarters() {
    if (!aiStarters) return;
    const subjectCount = Object.keys(currentData?.subject_counts || {}).length;
    if (!currentQuery || subjectCount < 2) {
        aiStarters.innerHTML = '';
        aiStarters.classList.add('hidden');
        return;
    }
    const starters = [
        `请先解释「${currentQuery}」在不同学科里的共同核心。`,
        `「${currentQuery}」在高考里最常见的考法是什么？`,
        `围绕「${currentQuery}」最容易混淆的概念有哪些？`,
        `如果我要复习「${currentQuery}」，应该按什么顺序串起来学？`,
    ];
    aiStarters.innerHTML = starters.map(text => `
        <button class="ai-starter-chip" type="button" data-prompt="${escAttr(text)}">${escHtml(text)}</button>
    `).join('');
    aiStarters.classList.remove('hidden');
    aiStarters.querySelectorAll('.ai-starter-chip').forEach(btn => {
        btn.addEventListener('click', () => sendAIMessage(btn.dataset.prompt));
    });
}

async function fetchAIContext(userMessage, history) {
    const res = await fetchWithTimeout(`${API}/api/chat/context`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            query: currentQuery,
            user_message: userMessage,
            history,
        }),
    }, AI_CONTEXT_TIMEOUT_MS);
    const { data, raw } = await readJsonLike(res);
    if (!res.ok || !data) {
        throw new Error(responseErrorMessage(res, data, raw, '上下文构建失败'));
    }
    return data;
}

async function requestServerChat(payload) {
    const res = await fetchWithTimeout(`${API}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }, AI_SERVER_CHAT_TIMEOUT_MS);
    const { data, raw } = await readJsonLike(res);
    if (!res.ok || !data) {
        throw new Error(responseErrorMessage(res, data, raw, 'AI 对话失败'));
    }
    return data;
}

async function requestDirectChat(prompt, fallbackMessage = 'AI 服务错误') {
    const endpoints = [AI_API, AI_API_FALLBACK];
    let lastError = null;

    for (const endpoint of endpoints) {
        try {
            const aiRes = await fetchWithTimeout(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    prompt,
                    model: AI_CHAT_MODEL,
                    taskType: 'chat',
                    thinkingLevel: 'low',
                }),
            }, AI_DIRECT_REQUEST_TIMEOUT_MS);
            const { data: aiData, raw: aiRaw } = await readJsonLike(aiRes);
            if (!aiRes.ok || !aiData?.answer) {
                throw new Error(responseErrorMessage(aiRes, aiData, aiRaw, fallbackMessage));
            }
            return {
                answer: aiData.answer,
                providerLabel: endpoint === AI_API ? 'Gemini · 浏览器直连' : 'Gemini · 浏览器备用链路',
            };
        } catch (error) {
            lastError = error;
        }
    }

    throw lastError || new Error(fallbackMessage);
}

function buildClientAIPrompt(userMessage, contextPayload, history) {
    const historyText = (contextPayload.history_text || '').trim()
        || history.map(msg => `${msg.role === 'user' ? '用户' : '助手'}: ${msg.content}`).join('\n')
        || '（无）';

    return `你是一位资深跨学科教育专家。用户当前搜索词是「${contextPayload.query || currentQuery}」。

本轮检索关注词：
${(contextPayload.search_terms_used || [currentQuery]).join('、')}

检索扩展词（含别名）：
${(contextPayload.retrieval_terms_used || contextPayload.search_terms_used || [currentQuery]).join('、')}

概念别名 / 同义表达：
${contextPayload.alias_text || '（无）'}

概念关系提示：
${contextPayload.relation_text || '（无）'}

教材证据（多学科原文）：
${contextPayload.context_text || '（无）'}

高考证据（如有）：
${contextPayload.gaokao_text || '（无）'}

历史对话：
${historyText}

用户本轮问题：
${userMessage}

请按以下结构回答：
【核心结论】先用 1-2 句讲清本质。
【学科联动】分点说明不同学科如何描述同一概念，尽量标注出处，格式：[学科·书名·p页码]。
【高考考法】如果给定证据里有真题，再说明常见考法 / 易错点；没有就写“高考证据不足”。
【学习建议】给出面向高中生的复习顺序或追问方向。

规则：
1. 只根据给定证据回答，不要编造页码或教材内容。
2. 如果证据不足，必须明确说“证据不足”。
3. 若用户追问，保持连续回答，不重复整段前文。
4. 可以参考“概念别名 / 关系提示”组织答案，但不能把它们当成教材原文引用。
5. 语言简洁、具体，避免空泛套话。
6. 总长度尽量控制在 280 字以内。`;
}

function toPlainText(value) {
    return String(value || '')
        .replace(/<[^>]+>/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

function defaultFollowupPrompts() {
    if (!currentQuery) return [];
    return [
        `请用一个关键词概括「${currentQuery}」的核心。`,
        `「${currentQuery}」在高考里最常见的考法是什么？`,
        `围绕「${currentQuery}」最容易混淆的概念有哪些？`,
    ];
}

function buildFallbackContextPayload(userMessage, history) {
    const groups = Array.isArray(currentData?.groups) ? currentData.groups : [];
    const evidence = [];
    const textbookLines = [];
    const gaokaoLines = [];
    let textbookCount = 0;
    let gaokaoCount = 0;

    groups.slice(0, 6).forEach(group => {
        (group.results || []).slice(0, 2).forEach(result => {
            const plainSnippet = toPlainText(result.snippet || result.text).slice(0, 180);
            const title = result.title || group.subject || currentQuery;
            const line = `${group.subject}｜${title}｜${plainSnippet || '见当前检索结果'}`;

            if (result.source === 'gaokao') {
                gaokaoCount += 1;
                gaokaoLines.push(line);
            } else {
                textbookCount += 1;
                textbookLines.push(line);
            }

            evidence.push({
                title,
                subject: group.subject,
                source: result.source,
                book_key: result.book_key,
                page_num: result.page_num,
                logical_page: result.logical_page,
                page_url: result.page_url,
                snippet: plainSnippet,
                citation: `${group.subject}·${title}`,
            });
        });
    });

    const matchedConcepts = Array.from(new Set(
        groups.flatMap(group => (group.results || []).flatMap(result => result.matched_concepts || []))
    )).slice(0, 8);
    const coverageLine = `覆盖 ${Object.keys(currentData?.subject_counts || {}).length} 个学科，教材命中 ${textbookCount} 条，真题例子 ${gaokaoCount} 条`;

    return {
        query: currentQuery,
        user_message: userMessage,
        search_terms_used: [currentQuery],
        retrieval_terms_used: [currentQuery],
        alias_text: '（无）',
        relation_text: matchedConcepts.length ? `相关概念：${matchedConcepts.join('、')}` : '（无）',
        context_text: textbookLines.join('\n') || '（无）',
        gaokao_text: gaokaoLines.join('\n') || '（无）',
        history_text: history.map(msg => `${msg.role === 'user' ? '用户' : '助手'}: ${msg.content}`).join('\n') || '（无）',
        summary: {
            coverage_line: coverageLine,
            search_terms_used: [currentQuery],
            retrieval_terms_used: [currentQuery],
            top_subjects: groups.slice(0, 4).map(group => ({ subject: group.subject, count: group.count || 0 })),
            relation_hint_count: matchedConcepts.length,
            gaokao_hit_count: gaokaoCount,
        },
        suggested_questions: defaultFollowupPrompts().filter(prompt => prompt !== userMessage),
        evidence: evidence.slice(0, 6),
    };
}

function bindOpenPageButtons(root, selector = '.ai-source-chip') {
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

function renderAIContextSummary(summary) {
    if (!summary) return '';
    const searchTerms = summary.search_terms_used || [];
    const retrievalTerms = (summary.retrieval_terms_used || []).filter(term => !searchTerms.includes(term));
    const tags = [
        ...searchTerms.map(term =>
            `<span class="ai-context-tag search">检索 ${escHtml(term)}</span>`
        ),
        ...retrievalTerms.slice(0, 4).map(term =>
            `<span class="ai-context-tag retrieval">扩展 ${escHtml(term)}</span>`
        ),
        ...(summary.top_subjects || []).map(item =>
            `<span class="ai-context-tag">${escHtml(item.subject)} ${item.count}</span>`
        ),
        summary.relation_hint_count > 0
            ? `<span class="ai-context-tag relation">关系 ${summary.relation_hint_count}</span>`
            : '',
        summary.gaokao_hit_count > 0
            ? `<span class="ai-context-tag exam">真题 ${summary.gaokao_hit_count}</span>`
            : '',
    ].filter(Boolean);

    return `
        <div class="ai-msg-context">
            <div class="ai-context-line">${escHtml(summary.coverage_line || '')}</div>
            ${tags.length ? `<div class="ai-context-tags">${tags.join('')}</div>` : ''}
        </div>
    `;
}

function renderAISources(sources) {
    if (!sources || !sources.length) return '';
    return `
        <div class="ai-msg-sources">
            ${sources.slice(0, 6).map(src => `
                <button class="ai-source-chip" type="button"
                    data-book-key="${escAttr(src.book_key || '')}"
                    data-page="${src.section ?? 0}"
                    data-total-pages="${src.total_pages ?? 0}">
                    ${escHtml(src.citation || `${src.subject}·${src.title}`)}
                </button>
            `).join('')}
        </div>
    `;
}

function renderAIFollowups(prompts) {
    if (!prompts || !prompts.length) return '';
    return `
        <div class="ai-msg-followups">
            ${prompts.slice(0, 4).map(prompt => `
                <button class="ai-followup-chip" type="button" data-prompt="${escAttr(prompt)}">${escHtml(prompt)}</button>
            `).join('')}
        </div>
    `;
}

function renderAIConversation() {
    if (!aiContent) return;
    if (!aiConversation.length) {
        aiContent.innerHTML = '';
        return;
    }
    aiContent.innerHTML = aiConversation.map((msg, index) => `
        <div class="ai-msg ${msg.role === 'user' ? 'user' : 'assistant'}">
            <div class="ai-msg-role">${msg.role === 'user' ? '你' : 'AI'}</div>
            ${msg.role === 'assistant' ? renderAIContextSummary(msg.contextSummary) : ''}
            <div class="ai-msg-text">${escHtml(msg.content)}</div>
            ${msg.role === 'assistant' ? renderAISources(msg.sources) : ''}
            ${msg.role === 'assistant' && index === aiConversation.length - 1 ? renderAIFollowups(msg.followups) : ''}
        </div>
    `).join('') + `<span class="ai-source">📡 由 ${escHtml(aiProviderLabel)} 生成 · 数据来源：${Object.keys(currentData?.subject_counts || {}).length} 个学科教材</span>`;

    bindOpenPageButtons(aiContent, '.ai-source-chip');
    aiContent.querySelectorAll('.ai-followup-chip').forEach(btn => {
        btn.addEventListener('click', () => sendAIMessage(btn.dataset.prompt));
    });
}

function setAIBusy(isBusy) {
    aiRequestPending = isBusy;
    if (aiBtn) {
        aiBtn.disabled = isBusy;
        aiBtn.classList.toggle('loading', isBusy);
        const sparkle = aiBtn.querySelector('.ai-sparkle');
        if (sparkle) sparkle.textContent = isBusy ? '⏳' : '✨';
    }
    if (aiSendBtn) aiSendBtn.disabled = isBusy;
    if (aiFollowupInput) aiFollowupInput.disabled = isBusy;
}

function resetAIConversation() {
    aiConversation = [];
    if (aiContent) aiContent.innerHTML = '';
    if (aiResult) aiResult.classList.add('hidden');
    if (aiFollowupInput) aiFollowupInput.value = '';
    if (aiCopyBtn) aiCopyBtn.classList.add('hidden');
    renderAIStarters();
}

async function sendAIMessage(userMessage) {
    if (!currentData || !currentQuery || aiRequestPending) return;
    const groups = currentData.groups || [];
    if (groups.length < 2) return;
    const cleanMessage = (userMessage || '').trim();
    if (!cleanMessage) return;

    const history = aiConversation.slice(-8);

    aiConversation.push({ role: 'user', content: cleanMessage });
    if (aiConversation.length > AI_MEMORY_LIMIT) {
        aiConversation = aiConversation.slice(-AI_MEMORY_LIMIT);
    }
    if (aiResult) aiResult.classList.remove('hidden');
    renderAIConversation();
    if (aiCopyBtn) aiCopyBtn.classList.remove('hidden');
    if (aiFollowupInput) aiFollowupInput.value = '';

    setAIBusy(true);
    try {
        let answer = '';
        let contextPayload = null;
        let usedBrowserFallback = false;
        const contextPromise = fetchAIContext(cleanMessage, history);

        const payload = {
            query: currentQuery,
            user_message: cleanMessage,
            history,
        };

        const serverPromise = requestServerChat(payload).then(data => ({
            channel: 'server',
            answer: data.answer || '',
            contextPayload: data.context || {},
            providerLabel: data.provider || aiProviderLabel,
        }));

        const browserPromise = (async () => {
            await delay(AI_BROWSER_FALLBACK_DELAY_MS);
            let fullContext = null;
            try {
                fullContext = await contextPromise;
            } catch (_) {
                fullContext = buildFallbackContextPayload(cleanMessage, history);
            }
            const prompt = buildClientAIPrompt(cleanMessage, fullContext, history);
            const direct = await requestDirectChat(prompt, 'AI 服务错误');
            return {
                channel: 'browser',
                answer: direct.answer,
                contextPayload: {
                    summary: fullContext.summary || null,
                    suggested_questions: fullContext.suggested_questions || [],
                    evidence: fullContext.evidence || [],
                },
                providerLabel: direct.providerLabel,
            };
        })();

        const winner = await Promise.any([serverPromise, browserPromise]);
        answer = winner.answer;
        contextPayload = winner.contextPayload;
        usedBrowserFallback = winner.channel === 'browser';
        setSearchAIProviderLabel(winner.providerLabel);

        if (usedBrowserFallback) {
            logClientAIChat({
                query: currentQuery,
                userMessage: cleanMessage,
                summary: contextPayload?.summary || {},
                provider: aiProviderLabel,
                success: true,
            });
        }

        aiConversation.push({
            role: 'assistant',
            content: answer,
            contextSummary: contextPayload?.summary || null,
            followups: (contextPayload?.suggested_questions || []).filter(prompt => prompt !== cleanMessage),
            sources: (contextPayload?.evidence || []).map(src => ({
                ...src,
                total_pages: (window._bookPages && window._bookPages[src.book_key]?.pages) || 0,
            })),
        });
    } catch (e) {
        aiConversation.push({ role: 'assistant', content: `请求失败: ${e.message}` });
    } finally {
        if (aiConversation.length > AI_MEMORY_LIMIT) {
            aiConversation = aiConversation.slice(-AI_MEMORY_LIMIT);
        }
        renderAIConversation();
        setAIBusy(false);
    }
}

async function requestAISynthesis() {
    if (!currentData || !currentQuery) return;
    const groups = currentData.groups || [];
    if (groups.length < 2) return;
    await sendAIMessage(`请先综合解读「${currentQuery}」，突出跨学科联系，并给出学习建议。`);
}

async function requestAIFollowup() {
    if (!aiFollowupInput) return;
    await sendAIMessage(aiFollowupInput.value);
}

async function copyAIConversation() {
    if (!aiConversation.length || !navigator.clipboard) return;
    const text = aiConversation
        .map(msg => `${msg.role === 'user' ? '你' : 'AI'}: ${msg.content}`)
        .join('\n\n');
    try {
        await navigator.clipboard.writeText(text);
        if (aiCopyBtn) {
            const prev = aiCopyBtn.textContent;
            aiCopyBtn.textContent = '✓';
            setTimeout(() => { aiCopyBtn.textContent = prev; }, 1200);
        }
    } catch (_) {
        if (aiCopyBtn) {
            const prev = aiCopyBtn.textContent;
            aiCopyBtn.textContent = '!';
            setTimeout(() => { aiCopyBtn.textContent = prev; }, 1200);
        }
    }
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
        if (btn.dataset.view === 'gaokao') initGaokao();
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
        const res = await fetch(`${API}/api/books`);
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
                opt.textContent = b.title;
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
const crossHintEl = document.getElementById('cross-hint');
const queryAnalysisEl = document.getElementById('query-analysis');
const subjectTabsEl = document.getElementById('subject-tabs');
const relatedBarEl = document.getElementById('related-bar');

searchBtn.addEventListener('click', () => doSearch(searchInput.value));
searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(searchInput.value); });
// ── Dynamic Concept Carousel ──────────────────────────────
const FALLBACK_CONCEPTS = ['蛋白质', 'DNA', '能量守恒', '丝绸之路', '温室效应', '光合作用', '平衡', '电子',
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
        const res = await fetch(`${API}/api/keywords?limit=120`);
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
        const res = await fetch(`${API}/api/search/trending`);
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

    resultsEl.innerHTML = '<div class="loading">搜索中…</div>';
    crossHintEl.classList.add('hidden');
    queryAnalysisEl.classList.add('hidden');
    subjectTabsEl.classList.add('hidden');
    relatedBarEl.classList.add('hidden');

    // Build search URL with advanced filters
    const params = new URLSearchParams({ q, limit: 100 });
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
        currentData = data;
        if (!currentSubjectFilter) {
            currentOverviewData = data;
        }
        renderResults(
            currentData,
            currentSubjectFilter,
            currentOverviewData?.subject_counts || currentData.subject_counts || {},
        );
        // Load related concepts
        loadRelated(currentData, q);
        // Refresh trending after search (new query logged)
        setTimeout(() => loadTrending(), 500);
        // Show concept subgraph for the search term
        loadSearchGraph(q);
    } catch (e) {
        resultsEl.innerHTML = `<div class="loading">搜索出错: ${e.message}</div>`;
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
            const res = await fetch(`${API}/api/related?q=${encodeURIComponent(q)}&limit=10`);
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

function renderResults(data, filterSubject = null, subjectCountsOverride = null) {
    // Cross hint
    if (data.cross_hint && !filterSubject) {
        crossHintEl.textContent = data.cross_hint;
        crossHintEl.classList.remove('hidden');
    } else {
        crossHintEl.classList.add('hidden');
    }

    renderQueryAnalysis(data.query_analysis, filterSubject);

    // AI panel: show only when 2+ subjects found
    const counts = subjectCountsOverride || data.subject_counts || {};
    const subjectCount = Object.keys(counts).length;
    if (subjectCount >= 2 && !filterSubject) {
        aiPanel.classList.remove('hidden');
        renderAIStarters();
    } else {
        aiPanel.classList.add('hidden');
        if (aiStarters) {
            aiStarters.innerHTML = '';
            aiStarters.classList.add('hidden');
        }
    }

    // Subject tabs
    const totalForTabs = currentOverviewData?.total ?? data.total;
    const subjects = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    if (subjects.length > 1) {
        subjectTabsEl.innerHTML = `
            <div class="subject-tab ${!filterSubject ? 'active' : ''}" data-subj="">
                全部 <span class="tab-count">${totalForTabs}</span>
            </div>
            ${subjects.map(([s, c]) => `
                <div class="subject-tab ${filterSubject === s ? 'active' : ''}" data-subj="${s}">
                    ${getSubjectIcon(s)} ${s} <span class="tab-count">${c}</span>
                </div>
            `).join('')}
        `;
        subjectTabsEl.classList.remove('hidden');
        subjectTabsEl.querySelectorAll('.subject-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                const s = tab.dataset.subj || null;
                if (s) {
                    doSearch(currentQuery, { subject: s });
                } else {
                    if (currentOverviewData) {
                        currentSubjectFilter = null;
                        currentData = currentOverviewData;
                        renderResults(currentOverviewData, null, currentOverviewData.subject_counts || {});
                    } else {
                        doSearch(currentQuery);
                    }
                }
            });
        });
    } else {
        subjectTabsEl.classList.add('hidden');
    }

    // Result groups
    let groups = data.groups || [];
    if (filterSubject) {
        groups = groups.filter(g => g.subject === filterSubject);
    }
    const subjectBreadth = Object.keys(counts).length;

    if (groups.length === 0) {
        resultsEl.innerHTML = '<div class="loading">未找到相关结果</div>';
        return;
    }

    resultsEl.innerHTML = groups.map(g => `
        <div class="result-group">
            <div class="group-header">
                <span class="group-icon">${g.icon || '📚'}</span>
                <span>${g.subject}</span>
                <span class="group-count">${g.count} 条</span>
                <div class="group-bar" style="background: ${g.color || '#95a5a6'}"></div>
            </div>
            ${g.results.map(r => `
                <div class="result-card" onclick="this.classList.toggle('expanded')">
                    <div class="result-meta">
                        <span class="result-title">${escHtml(r.title)} · §${r.section}</span>
                        ${r.source === 'gaokao' ? '<span class="source-badge gaokao">📝 真题</span>' : '<span class="source-badge textbook">📚 教材</span>'}
                        ${renderMatchChannelBadge(r)}
                        ${r.image_count > 0 ? `<span class="img-badge">📷 ${r.image_count}</span>` : ''}
                        ${r.page_url ? `<span class="page-badge" title="第 ${r.page_num} 页 / 共 ${r.total_pages} 页">📄 p${r.logical_page ?? r.page_num}</span>` : ''}
                    </div>
                    ${renderEvidenceTrace(r, g.subject, subjectBreadth, data.query)}
                    <div class="result-snippet">${sanitizeSnippet(r.snippet)}</div>
                    <div class="result-text">${renderText(r.text, r.book_key)}</div>
                    ${r.page_url ? `<div class="result-actions">
                        <button class="view-page-btn" onclick="event.stopPropagation(); openPageViewer('${escAttr(r.book_key)}', ${r.page_num}, ${r.total_pages})">
                            📖 查看原文
                        </button>
                    </div>` : ''}
                </div>
            `).join('')}
        </div>
    `).join('');

    // Trigger Math rendering for search results
    if (typeof renderMath === 'function') {
        renderMath(resultsEl);
    }
    // Trigger KaTeX rendering if available
    if (typeof katex !== 'undefined') {
        renderKaTeX(resultsEl);
    }
}

function getSubjectIcon(s) {
    const map = { '语文': '📖', '数学': '📐', '英语': '🌍', '物理': '⚛️', '化学': '🧪', '生物学': '🧬', '历史': '📜', '地理': '🗺️', '思想政治': '⚖️' };
    return map[s] || '📚';
}

function renderMatchChannelBadge(result) {
    if (result.retrieval_source === 'supplemental' || result.match_channel === 'supplemental') {
        return '<span class="match-channel supplemental">🧩 备份教材兜底</span>';
    }
    if (result.match_channel === 'exact') {
        return '<span class="match-channel exact">🎯 精确命中</span>';
    }
    return '<span class="match-channel fts">🧠 语义召回</span>';
}

function renderQueryAnalysis(analysis, filterSubject = null) {
    if (!queryAnalysisEl || filterSubject || !analysis) {
        if (queryAnalysisEl) queryAnalysisEl.classList.add('hidden');
        return;
    }

    const conceptTerms = Array.isArray(analysis.concept_terms) ? analysis.concept_terms : [];
    const fallbackTerms = Array.isArray(analysis.fallback_terms) ? analysis.fallback_terms : [];
    const chips = [];

    conceptTerms.slice(0, 4).forEach(item => {
        const matchLabel = item.match_type === 'alias' ? '概念归并' : '标准术语';
        chips.push(`<span class="query-chip concept">${escHtml(item.term)} · ${escHtml(matchLabel)}</span>`);
    });
    fallbackTerms.slice(0, 4).forEach(item => {
        const hitLabel = [item.textbook_hits ? `主库 ${item.textbook_hits}` : '', item.supplemental_hits ? `备份 ${item.supplemental_hits}` : '']
            .filter(Boolean)
            .join(' / ');
        chips.push(`<span class="query-chip fallback">${escHtml(item.term)}${hitLabel ? ` · ${escHtml(hitLabel)}` : ''}</span>`);
    });

    const scopeLine = analysis.scope_label ? `<span class="query-analysis-scope">范围：${escHtml(analysis.scope_label)}</span>` : '';
    const fallbackFlag = analysis.used_supplemental_fallback
        ? '<span class="query-analysis-flag">当前结果含备份教材解析兜底</span>'
        : '';

    queryAnalysisEl.innerHTML = `
        <div class="query-analysis-header">
            <span class="query-analysis-title">术语解析</span>
            ${scopeLine}
        </div>
        <div class="query-analysis-summary">${escHtml(analysis.summary || '')}</div>
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
    } else {
        evidenceChips.push('<span class="evidence-chip semantic">FTS / 语义召回</span>');
    }

    if (result.page_url) {
        evidenceChips.push(`<span class="evidence-chip page">可回原页 p${result.logical_page ?? result.page_num}</span>`);
    } else {
        evidenceChips.push('<span class="evidence-chip neutral">仅片段证据</span>');
    }

    if (result.source === 'gaokao') {
        evidenceChips.push(
            `<span class="evidence-chip exam">${escHtml([result.year, result.category].filter(Boolean).join(' · ') || '高考真题')}</span>`
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
            <span class="relation-path">检索路径：${escHtml(query)} → ${escHtml(subject)} · 覆盖 ${subjectBreadth} 科</span>
        </div>
    `;
}

function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}

function escAttr(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Sanitize snippet: keep <mark> highlights, strip everything else
function sanitizeSnippet(html) {
    if (!html) return '';
    // Preserve <mark>...</mark> by replacing with placeholders
    const marks = [];
    html = html.replace(/<mark>(.*?)<\/mark>/gi, (_, inner) => {
        marks.push(inner);
        return `%%MARK${marks.length - 1}%%`;
    });
    // Strip all remaining HTML tags
    html = html.replace(/<[^>]+>/g, ' ');
    // Remove markdown image syntax ![...](...)  
    html = html.replace(/!\[.*?\]\(.*?\)/g, '');
    // Collapse whitespace
    html = html.replace(/\s+/g, ' ').trim();
    // Restore mark tags
    html = html.replace(/%%MARK(\d+)%%/g, (_, i) => `<mark>${marks[+i]}</mark>`);
    // Truncate to ~200 chars
    if (html.length > 250) {
        html = html.slice(0, 250) + '…';
    }
    return html;
}

// Render text with images, formulas, and rich formatting for expanded view
function renderText(text, bookKey) {
    if (!text) return '';
    // Strip HTML tags except preserve content
    let s = text.replace(/<[^>]+>/g, ' ');
    // Convert markdown images to <img> tags — use R2 CDN
    s = s.replace(/!\[([^\]]*)\]\(images\/([^)]+)\)/g, (_, alt, src) => {
        return `<img class="result-img" src="${IMG_CDN}/orig/${encodeURIComponent(bookKey)}/${src}" alt="${alt || '教材图片'}" loading="lazy">`;
    });

    // Wrap LaTeX formulas for KaTeX rendering
    // Display math: $$...$$
    s = s.replace(/\$\$([^$]+?)\$\$/g, '<span class="katex-display" data-formula="$1"></span>');
    // Inline math: $...$
    s = s.replace(/\$([^$\n]+?)\$/g, '<span class="katex-inline" data-formula="$1"></span>');

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

    return s;
}

// KaTeX rendering helper
function renderKaTeX(container) {
    if (typeof katex === 'undefined') return;
    container.querySelectorAll('.katex-display, .katex-inline').forEach(el => {
        try {
            const formula = el.dataset.formula;
            const isDisplay = el.classList.contains('katex-display');
            katex.render(formula, el, { displayMode: isDisplay, throwOnError: false });
        } catch (e) {
            el.textContent = el.dataset.formula; // fallback
        }
    });
}

// ── Page Image Viewer (Lightbox) ─────────────────────────
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
                <span class="page-viewer-page">p${page}</span>
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
        .then(r => r.json())
        .then(data => {
            window._pageViewerData = data;
            window._pageViewerIdx = data.pages.findIndex(p => p.current);

            // Update header
            viewer.querySelector('.page-viewer-title').textContent = data.title;
            viewer.querySelector('.page-viewer-info').textContent =
                `第 ${page + 1} 页 / 共 ${data.total_pages} 页`;

            // Build thumbnails
            const thumbs = viewer.querySelector('.page-viewer-thumbnails');
            thumbs.innerHTML = data.pages.map((p, i) => `
                <div class="page-thumb ${p.current ? 'active' : ''}" data-idx="${i}" onclick="pageViewerGo(${i})">
                    <img src="${p.url}" alt="p${p.page}" loading="lazy" />
                    <span>p${p.page + 1}</span>
                </div>
            `).join('');

            // Show current page
            showPageViewerImage(window._pageViewerIdx);
        })
        .catch(e => {
            viewer.querySelector('.page-viewer-title').textContent = '加载失败';
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
    img.onerror = () => {
        if (loading) loading.innerHTML = '<span>图片加载失败</span>';
    };
    img.src = page.url;
    img.alt = `第 ${page.page + 1} 页`;

    viewer.querySelector('.page-viewer-page').textContent = `p${page.page + 1}`;
    viewer.querySelector('.page-viewer-info').textContent =
        `第 ${page.page + 1} 页 / 共 ${data.total_pages} 页`;

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
        let url = `${API}/api/graph/overview?mode=${mode}&limit=80`;
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
        n.color = SUBJ_COLORS[n.id] || '#6c5ce7';
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

    // Labels
    node.append('text')
        .attr('class', 'graph-label')
        .attr('dy', d => d.r + 14)
        .attr('text-anchor', 'middle')
        .attr('font-size', d => d.type === 'subject' ? 13 : 10)
        .attr('font-weight', d => d.type === 'subject' ? 700 : 400)
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

    const heroSub = document.getElementById('hero-sub');
    if (heroSub) {
        const nSubjects = data.subjects_count || (data.subjects || []).length;
        const nTotal = (data.total_chunks || 0).toLocaleString();
        const nGaokao = (data.gaokao_chunks || 0).toLocaleString();
        heroSub.textContent = `覆盖高中 ${nSubjects} 科 · ${nTotal} 条结构化语料 · ${nGaokao} 道高考真题`;
    }

    const gaokaoHeroSub = document.getElementById('gk-hero-sub');
    if (gaokaoHeroSub) {
        const [startYear, endYear] = data.gaokao_year_range || [];
        const yearRange = startYear && endYear ? `${startYear}-${endYear}` : '历年';
        const nGaokao = (data.gaokao_chunks || 0).toLocaleString();
        const nTextbook = (data.textbook_chunks || 0).toLocaleString();
        const multimodal = data.gaokao_multimodal ? ` · ${data.gaokao_multimodal.toLocaleString()} 道含图题` : '';
        gaokaoHeroSub.textContent = `${yearRange} · ${nGaokao} 道真题${multimodal} · 与 ${nTextbook} 条教材语料联动检索`;
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
                `${(data.gaokao_chunks || 0).toLocaleString()} 道真题`,
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
        aboutCorpusText.innerHTML = `全文检索基于 SQLite FTS5，当前共有 <strong>${nTotal}</strong> 条结构化语料（教材 ${nTextbook} / 真题 ${nGaokao}），覆盖 <strong>${nSubjects}</strong> 个学科；真题时间范围为 <strong>${yearRange}</strong>${multimodal}。`;
    }

    const aboutAiText = document.getElementById('about-ai-text');
    if (aboutAiText) {
        const faissPart = data.faiss_enabled
            ? `FAISS 已启用（${(data.faiss_vectors || 0).toLocaleString()} 条教材向量）`
            : 'FAISS 当前未启用';
        aboutAiText.textContent = `AI 对话入口统一走 ai.bdfz.net；模型侧显示为 ${data.ai_model || aiProviderLabel}。检索底座为 SQLite FTS5 + ${faissPart}。`;
    }

    const aboutPrecomputeText = document.getElementById('about-precompute-text');
    if (aboutPrecomputeText) {
        const tables = data.ai_tables || {};
        aboutPrecomputeText.textContent = `AI 预计算已入库：解读 ${Number(tables.explanations || 0).toLocaleString()}、同义/别名 ${Number(tables.synonyms || 0).toLocaleString()}、关系 ${Number(tables.relations || 0).toLocaleString()}、教材摘要 ${Number(tables.summaries || 0).toLocaleString()}、真题关联 ${Number(tables.gaokao_links || 0).toLocaleString()}。`;
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
    try {
        const res = await fetch(`${API}/api/stats`);
        const data = await res.json();
        const el = document.getElementById('stats-bar');
        el.innerHTML = data.subjects.map(s =>
            `<div class="stat-chip">${s.icon} ${s.name} <span class="count">${s.count.toLocaleString()}</span></div>`
        ).join('');
        el.classList.remove('hidden');
        applyLiveStatsToUI(data);
    } catch (e) { /* silent */ }
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
                    <span class="gk-badge" style="background:${q.color || '#6c5ce7'}">${q.icon || '📚'} ${q.subject}</span>
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
                    <span class="gk-badge" style="background:${l.color || '#6c5ce7'}">${l.icon || '📚'} ${l.subject}</span>
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

function buildGaokaoSourceContext(items, limit = 3) {
    return (items || []).slice(0, limit).map(item =>
        `[${item.subject}·${item.title}·§${item.section}] ${item.summary || (item.text || '').slice(0, 320)}`
    ).join('\n\n');
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

async function requestGaokaoAI(questionId, btn) {
    const resultEl = document.getElementById(`gk-ai-${questionId}`);
    if (resultEl.innerHTML) {
        resultEl.classList.toggle('hidden');
        return;
    }

    btn.disabled = true;
    btn.textContent = '✨ 分析中…';
    resultEl.innerHTML = '<div class="loading">✨ AI 正在分析真题与教材的关联…</div>';
    resultEl.classList.remove('hidden');

    try {
        // First get the question and linked textbook content
        const linkRes = await fetch(`${API}/api/gaokao/link?question_id=${questionId}&limit=5`);
        if (!linkRes.ok) {
            throw new Error(`教材关联加载失败 (${linkRes.status})`);
        }
        const linkData = await linkRes.json();

        // Get question text from the card
        const card = document.querySelector(`.gaokao-card[data-id="${questionId}"]`);
        const questionText = card ? card.querySelector('.gk-question')?.textContent?.slice(0, 600) : '';
        const matchedConcepts = (linkData.matched_concepts || []).map(item => item.concept).join('、') || '（未识别）';
        const precomputedSummary = linkData.precomputed_analysis?.summary || '（无）';
        const precomputedKnowledgePoints = (linkData.precomputed_analysis?.knowledge_points || []).join('、') || '（无）';
        const precomputedRefs = (linkData.precomputed_analysis?.textbook_refs || []).join('；') || '（无）';
        const sameSubjectContext = buildGaokaoSourceContext(linkData.links, 3);
        const crossSubjectContext = buildGaokaoSourceContext(linkData.cross_links, 3);
        const sourceItems = [...(linkData.links || []).slice(0, 3), ...(linkData.cross_links || []).slice(0, 2)];

        const prompt = `你是一位高考命题研究专家。请分析以下高考真题与教材内容之间的关系。

【真题元数据】
标题：${linkData.question_title || ''}
学科：${linkData.question_subject || ''}
年份：${linkData.question_year || '未知'}
卷种：${linkData.question_category || '未知'}
题型：${linkData.question_type === 'objective' ? '客观题' : linkData.question_type === 'subjective' ? '主观题' : '未知'}
匹配概念：${matchedConcepts}
预计算知识点：${precomputedKnowledgePoints}
预计算结论：${precomputedSummary}
预计算教材锚点：${precomputedRefs}

【题干】
${questionText}

【同学科教材证据】
${sameSubjectContext || '（未找到直接同学科教材）'}

【跨学科教材证据】
${crossSubjectContext || '（未找到直接跨学科教材）'}

请严格按以下小标题输出：
显性关联：
隐性关联：
跨学科迁移：
易错提醒：

要求：
1. 220 字以内，语言简洁，面向高中生。
2. 只根据给定证据回答，不要编造教材内容。
3. 如果跨学科证据不足，必须明确写“跨学科证据不足”。`;

        const direct = await requestDirectChat(prompt, 'AI 服务错误');

        if (direct.answer) {
            resultEl.innerHTML = `
                <div class="gk-ai-answer">
                    <div class="gk-ai-header">✨ AI 关联分析 <span class="ai-model">Gemini</span></div>
                    <div class="gk-ai-meta">
                        <span class="gk-ai-meta-chip">${escHtml(linkData.question_subject || '未知学科')}</span>
                        <span class="gk-ai-meta-chip">${escHtml(String(linkData.question_year || '未知年份'))}</span>
                        ${linkData.question_category ? `<span class="gk-ai-meta-chip">${escHtml(linkData.question_category)}</span>` : ''}
                        <span class="gk-ai-meta-chip">同学科 ${linkData.links?.length || 0}</span>
                        <span class="gk-ai-meta-chip">跨学科 ${linkData.cross_links?.length || 0}</span>
                    </div>
                    <div class="gk-ai-text">${escHtml(direct.answer)}</div>
                    ${renderGaokaoAISources(sourceItems)}
                </div>
            `;
            bindOpenPageButtons(resultEl, '.gk-ai-source-chip');
            renderMath(resultEl);
        } else {
            resultEl.innerHTML = `<div class="loading">AI 服务暂时不可用</div>`;
        }
    } catch (e) {
        resultEl.innerHTML = `<div class="loading">请求失败: ${e.message}</div>`;
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
    if (window.renderMathInElement) {
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

const SUBJ_COLORS = {
    '数学': '#3498db', '物理': '#e74c3c', '化学': '#2ecc71', '生物': '#f39c12',
    '地理': '#1abc9c', '历史': '#9b59b6', '语文': '#e67e22', '英语': '#34495e',
    '思想政治': '#f1c40f', 'hanjia': '#95a5a6',
};

async function loadInsights() {
    if (insightsLoaded) return;
    insightsLoaded = true;
    // Populate subject selector from stats
    try {
        const sr = await fetch(`${API}/api/stats`);
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
        const url = `${API}/api/analytics/word-freq?source=${source}&limit=30${subject ? '&subject=' + encodeURIComponent(subject) : ''}`;
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

    // Labels
    g.selectAll('.bar-label').data(data).join('text')
        .attr('class', 'bar-label')
        .attr('x', d => x(d.count) + 5)
        .attr('y', d => y(d.term) + y.bandwidth() / 2)
        .attr('dy', '0.35em')
        .attr('fill', '#a0a0c0')
        .attr('font-size', '11px')
        .text(d => d.count.toLocaleString());

    // Y axis labels
    svg.selectAll('.term-label').data(data).join('text')
        .attr('class', 'term-label')
        .attr('x', margin.left - 6)
        .attr('y', d => y(d.term) + y.bandwidth() / 2)
        .attr('dy', '0.35em')
        .attr('text-anchor', 'end')
        .attr('fill', '#e0e0f0')
        .attr('font-size', '12px')
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
        const res = await fetch(`${API}/api/analytics/heatmap`);
        const data = await res.json();
        renderHeatmap(container, data);
    } catch (e) { container.innerHTML = `<div class="loading">加载失败: ${e.message}</div>`; }
}

function getHeatmapTextStyle(fillColor) {
    const color = d3.color(fillColor);
    if (!color) {
        return { fill: '#f8fbff', stroke: 'rgba(0,0,0,0.35)' };
    }
    const luminance = (0.299 * color.r + 0.587 * color.g + 0.114 * color.b) / 255;
    if (luminance >= 0.66) {
        return { fill: '#17202a', stroke: 'rgba(255,255,255,0.55)' };
    }
    return { fill: '#f8fbff', stroke: 'rgba(0,0,0,0.35)' };
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

    // Axis labels
    subjects.forEach((s, i) => {
        svg.append('text')
            .attr('x', margin.left + i * cellSize + cellSize / 2 - 1)
            .attr('y', margin.top - 8)
            .attr('text-anchor', 'middle')
            .attr('fill', SUBJ_COLORS[s] || '#ccc')
            .attr('font-size', '12px')
            .attr('font-weight', '500')
            .text(s);
        svg.append('text')
            .attr('x', margin.left - 8)
            .attr('y', margin.top + i * cellSize + cellSize / 2)
            .attr('dy', '0.35em')
            .attr('text-anchor', 'end')
            .attr('fill', SUBJ_COLORS[s] || '#ccc')
            .attr('font-size', '12px')
            .attr('font-weight', '500')
            .text(s);
    });

    // Caption
    svg.append('text')
        .attr('x', W / 2).attr('y', H + 5)
        .attr('text-anchor', 'middle')
        .attr('fill', '#888')
        .attr('font-size', '11px')
        .text(`共 ${data.total_concepts} 个跨学科学术术语`);
}

// ── Coverage Analysis ──
async function loadCoverage() {
    try {
        const res = await fetch(`${API}/api/analytics/coverage?limit=15`);
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
        const res = await fetch(`${API}/api/analytics/concept-breadth?limit=30`);
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

    g.selectAll('.breadth-val').data(concepts).join('text')
        .attr('class', 'breadth-val')
        .attr('x', d => x(d.subjects) + 5)
        .attr('y', d => y(d.term) + y.bandwidth() / 2)
        .attr('dy', '0.35em')
        .attr('fill', '#a0a0c0')
        .attr('font-size', '11px')
        .text(d => `${d.subjects} 科`);

    svg.selectAll('.breadth-label').data(concepts).join('text')
        .attr('class', 'breadth-label')
        .attr('x', margin.left - 6)
        .attr('y', d => y(d.term) + y.bandwidth() / 2)
        .attr('dy', '0.35em')
        .attr('text-anchor', 'end')
        .attr('fill', '#e0e0f0')
        .attr('font-size', '12px')
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
        const res = await fetch(`${API}/api/graph/search?q=${encodeURIComponent(term)}`);
        const data = await res.json();
        if (!data.nodes || data.nodes.length < 3) return;

        const section = document.createElement('div');
        section.id = 'search-graph-section';
        section.className = 'search-graph-section';
        section.innerHTML = '<h3 class="search-graph-title">🔗 关联知识图谱</h3>';

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
        .attr('stroke', d => SUBJ_COLORS[d.subject] || '#6c5ce7')
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
        .attr('fill', d => d.type === 'center' ? '#6c5ce7' : d.type === 'subject' ? (SUBJ_COLORS[d.id] || '#555') : 'rgba(108,92,231,0.5)')
        .attr('stroke', d => d.type === 'center' ? '#a29bfe' : 'none')
        .attr('stroke-width', 2);

    node.append('text')
        .attr('dy', d => d.type === 'center' ? 28 : 22)
        .attr('text-anchor', 'middle')
        .attr('fill', '#e0e0f0')
        .attr('font-size', d => d.type === 'center' ? '13px' : d.type === 'subject' ? '12px' : '10px')
        .attr('font-weight', d => d.type === 'center' ? '700' : '400')
        .text(d => d.id);

    sim.on('tick', () => {
        link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
            .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
        node.attr('transform', d => `translate(${Math.max(20, Math.min(W - 20, d.x))},${Math.max(20, Math.min(H - 20, d.y))})`);
    });
}
