const API = '';

const state = {
    query: '',
    queryKey: '',
    chatHistory: [],
    dictContext: '',
    textbookContext: '',
    gaokaoContext: '',
    statusPayload: null,
    modalPages: [],
    modalIndex: 0,
    searchInFlight: false,
    chatInFlight: false,
    dictEntries: [],
    searchSeq: 0,
};

const el = {
    input: document.getElementById('dict-input'),
    searchBtn: document.getElementById('dict-search-btn'),
    empty: document.getElementById('dict-empty'),
    results: document.getElementById('dict-results'),
    statusNote: document.getElementById('dict-status-note'),
    flowQuery: document.getElementById('dict-flow-query'),
    flowTextbook: document.getElementById('flow-step-textbook'),
    flowTextbookBody: document.getElementById('flow-step-textbook-body'),
    flowTextbookFoot: document.getElementById('flow-step-textbook-foot'),
    flowDict: document.getElementById('flow-step-dict'),
    flowDictBody: document.getElementById('flow-step-dict-body'),
    flowDictFoot: document.getElementById('flow-step-dict-foot'),
    flowGaokao: document.getElementById('flow-step-gaokao'),
    flowGaokaoBody: document.getElementById('flow-step-gaokao-body'),
    flowGaokaoFoot: document.getElementById('flow-step-gaokao-foot'),
    flowAi: document.getElementById('flow-step-ai'),
    flowAiBody: document.getElementById('flow-step-ai-body'),
    flowAiFoot: document.getElementById('flow-step-ai-foot'),
    sourceChipXuci: document.getElementById('source-chip-xuci'),
    sourceChipChangyong: document.getElementById('source-chip-changyong'),
    sourceChipExternal: document.getElementById('source-chip-external'),
    tbCount: document.getElementById('tb-count'),
    tbResults: document.getElementById('tb-results'),
    dictCount: document.getElementById('dict-count'),
    dictEntries: document.getElementById('dict-entries'),
    refCount: document.getElementById('ref-count'),
    refResults: document.getElementById('ref-results'),
    gkCount: document.getElementById('gk-count'),
    gkResults: document.getElementById('gk-results'),
    chatMessages: document.getElementById('chat-messages'),
    chatInput: document.getElementById('chat-input'),
    chatSendBtn: document.getElementById('chat-send-btn'),
    chatCopyBtn: document.getElementById('chat-copy-btn'),
    modal: document.getElementById('page-modal'),
    modalClose: document.getElementById('page-modal-close'),
    modalPrev: document.getElementById('page-modal-prev'),
    modalNext: document.getElementById('page-modal-next'),
    modalImage: document.getElementById('page-modal-image'),
    modalMeta: document.getElementById('page-modal-meta'),
};

const suggestionButtons = Array.from(document.querySelectorAll('[data-dict-suggestion]'));

function escHtml(value) {
    return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function highlightText(text, query) {
    const safe = escHtml(text || '');
    if (!query) return safe;
    const pattern = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return safe.replace(new RegExp(`(${pattern})`, 'g'), '<mark>$1</mark>');
}

function setLoading(panel, message = '加载中…') {
    panel.innerHTML = `<div class="dict-loading">${escHtml(message)}</div>`;
}

function setEmptyLine(panel, message) {
    panel.innerHTML = `<div class="dict-empty-line">${escHtml(message)}</div>`;
}

function formatSourceChip(label, summary) {
    if (!summary) return label;
    const verified = Number(summary.verified_headwords || 0);
    const coverage = Number(summary.coverage_ratio || 0);
    const coverageText = coverage > 0 ? ` · 覆盖 ${Math.round(coverage * 100)}%` : '';
    if (verified > 0) {
        return `${label} · 已核 ${verified}${coverageText}`;
    }
    if (summary.has_candidates) {
        return `${label} · 待核页`;
    }
    return `${label} · 未导入`;
}

function setStatusNoteText(text) {
    if (el.statusNote) {
        el.statusNote.textContent = text;
    }
}

function setFlowStep(stepEl, bodyEl, footEl, { statusClass, body, foot }) {
    if (!stepEl || !bodyEl || !footEl) return;
    stepEl.classList.remove('is-ready', 'is-active', 'is-pending', 'is-empty', 'is-warning');
    stepEl.classList.add(statusClass || 'is-ready');
    bodyEl.textContent = body;
    footEl.textContent = foot;
}

function renderIdleFlow() {
    const summaries = state.statusPayload && state.statusPayload.source_summaries
        ? state.statusPayload.source_summaries
        : {};
    const xuci = summaries.xuci || null;
    const changyong = summaries.changyong || null;
    const xuciPart = xuci && xuci.verified_headwords > 0
        ? `《古代汉语虚词词典》已核 ${xuci.verified_headwords} 条`
        : xuci && xuci.has_candidates
            ? '《古代汉语虚词词典》候选页待复核'
            : '《古代汉语虚词词典》尚未导入';
    const changyongPart = changyong && changyong.verified_headwords > 0
        ? `王力本已核 ${changyong.verified_headwords} 条${changyong.coverage_ratio ? `，覆盖 ${Math.round(Number(changyong.coverage_ratio) * 100)}%` : ''}`
        : changyong && changyong.has_candidates
            ? '王力本候选页待复核'
            : '王力本尚未导入';

    if (el.flowQuery) {
        el.flowQuery.textContent = '等待输入字词';
    }
    setFlowStep(el.flowTextbook, el.flowTextbookBody, el.flowTextbookFoot, {
        statusClass: 'is-ready',
        body: '先把教材结果收敛到古文、古诗词范围，尽量避开现代文噪音。',
        foot: '检索后优先展开教材证据',
    });
    setFlowStep(el.flowDict, el.flowDictBody, el.flowDictFoot, {
        statusClass: 'is-pending',
        body: `馆藏辞典只展示已核字头页图；当前状态：${xuciPart}，${changyongPart}。`,
        foot: '学生端不直接展示 OCR 文字',
    });
    setFlowStep(el.flowGaokao, el.flowGaokaoBody, el.flowGaokaoFoot, {
        statusClass: 'is-ready',
        body: '同步汇总语文真题中的古文、古诗词命中，方便对照常考方式。',
        foot: '检索后展示历年相关题目',
    });
    setFlowStep(el.flowAi, el.flowAiBody, el.flowAiFoot, {
        statusClass: 'is-ready',
        body: '把教材、辞典与真题证据一起发给 Worker AI，自动生成首轮学习建议。',
        foot: '支持多轮追问与全文复制',
    });
}

function renderSearchFlow(summary) {
    if (el.flowQuery) {
        el.flowQuery.textContent = `当前检索：${summary.query}`;
    }
    setFlowStep(el.flowTextbook, el.flowTextbookBody, el.flowTextbookFoot, {
        statusClass: summary.textbookCount > 0 ? 'is-active' : (summary.textbookFailed ? 'is-warning' : 'is-empty'),
        body: summary.textbookFailed
            ? `教材古典文本检索暂时失败：${summary.textbookError}`
            : summary.textbookCount > 0
                ? `教材古文 / 古诗词命中 ${summary.textbookCount} 条，先从课内语境定位这个字或词。`
                : '本次没有命中教材中的古文或古诗词内容。',
        foot: summary.textbookTitle || (summary.textbookCount > 0 ? '结果已限定在教材古典文本范围' : '可尝试换同义字或更常见的单字'),
    });

    setFlowStep(el.flowDict, el.flowDictBody, el.flowDictFoot, {
        statusClass: summary.dictCount > 0 ? 'is-active' : (summary.dictFailed ? 'is-warning' : 'is-pending'),
        body: summary.dictFailed
            ? `馆藏辞典暂时不可用：${summary.dictError}`
            : summary.dictCount > 0
                ? `馆藏辞典命中 ${summary.dictCount} 条页图结果，外部参考同步给出 ${summary.referenceCount} 个入口。`
                : summary.referenceCount > 0
                    ? '馆藏辞典暂未返回页图，但右侧仍保留官方与外部参考入口。'
                    : '馆藏辞典和外部参考都没有返回可用结果。',
        foot: summary.dictFoot,
    });

    setFlowStep(el.flowGaokao, el.flowGaokaoBody, el.flowGaokaoFoot, {
        statusClass: summary.gaokaoCount > 0 ? 'is-active' : (summary.gaokaoFailed ? 'is-warning' : 'is-empty'),
        body: summary.gaokaoFailed
            ? `真题检索暂时失败：${summary.gaokaoError}`
            : summary.gaokaoCount > 0
                ? `真题中的古文 / 古诗词命中 ${summary.gaokaoCount} 条，可直接对照高考考法。`
                : '本次没有命中真题中的古文或古诗词内容。',
        foot: summary.gaokaoMeta || (summary.gaokaoCount > 0 ? '结果已优先限定文言文与古诗文阅读' : '没有真题命中时，先回教材稳住义项'),
    });
}

function updateAiFlow(stateClass, body, foot) {
    setFlowStep(el.flowAi, el.flowAiBody, el.flowAiFoot, { statusClass: stateClass, body, foot });
}

function buildDictFoot(dictPayload, referenceCount) {
    const kind = state.query.length === 1 ? '单字' : '词语';
    if (!dictPayload) {
        return `当前查询是${kind}；右侧仍可保留 ${referenceCount} 个参考入口。`;
    }
    if (dictPayload.source_mode === 'headword_page_index') {
        return dictPayload.entries && dictPayload.entries.length
            ? '优先返回已核字头页图；学生端只看原页。'
            : `当前查询是${kind}；馆藏页图按已核字头返回。`;
    }
    if (dictPayload.source_mode === 'dict_db') {
        return '当前回退到运行库定位结果；学生端仍只显示页图。';
    }
    return state.query.length > 1
        ? '多字词未命中时，建议优先看右侧参考，或拆成关键单字继续查。'
        : '该字尚未进入已核页图索引时，可先看右侧官方参考。';
}

function applySearchStatusNote(summary) {
    const ok = [];
    const failed = [];
    if (!summary.textbookFailed) ok.push('教材');
    else failed.push(`教材：${summary.textbookError}`);
    if (!summary.dictFailed) ok.push('馆藏辞典');
    else failed.push(`馆藏辞典：${summary.dictError}`);
    if (!summary.referenceFailed) ok.push('外部参考');
    else failed.push(`外部参考：${summary.referenceError}`);
    if (!summary.gaokaoFailed) ok.push('真题');
    else failed.push(`真题：${summary.gaokaoError}`);

    if (!failed.length) {
        setStatusNoteText(summary.dictCount > 0
            ? '当前检索已汇总教材、馆藏页图、真题与外部参考；学生端继续只展示已核页图。'
            : '当前检索已完成教材、真题与参考层汇总；馆藏辞典未命中时可先依赖右侧官方参考。');
        return;
    }

    const okText = ok.length ? `${ok.join('、')}已加载` : '当前没有证据源加载成功';
    setStatusNoteText(`${okText}；${failed.join('；')}。学生端仍只展示已核页图。`);
}

function historyToCopyText() {
    return state.chatHistory
        .map(item => `${item.role === 'user' ? '我' : 'AI'}：${item.content}`)
        .join('\n\n');
}

function updateCopyButton() {
    el.chatCopyBtn.classList.toggle('hidden', state.chatHistory.length === 0);
}

function renderChatMessages() {
    if (!state.chatHistory.length) {
        el.chatMessages.innerHTML = '';
        updateCopyButton();
        return;
    }
    el.chatMessages.innerHTML = state.chatHistory.map(item => `
        <div class="dict-chat-message ${item.role === 'user' ? 'user' : 'assistant'}">
            <div class="dict-chat-role">${item.role === 'user' ? '我' : 'AI'}</div>
            <div class="dict-chat-bubble">${escHtml(item.content)}</div>
        </div>
    `).join('');
    el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
    updateCopyButton();
}

function setChatPending(pending) {
    state.chatInFlight = pending;
    el.chatSendBtn.disabled = pending || !state.query;
    el.chatInput.disabled = pending || !state.query;
}

async function fetchJson(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
        let detail = `${response.status}`;
        try {
            const payload = await response.json();
            detail = payload.detail || payload.error || detail;
        } catch (_) {
            // ignore
        }
        throw new Error(detail);
    }
    return response.json();
}

function renderStatus(payload) {
    state.statusPayload = payload;
    const summaries = payload && payload.source_summaries ? payload.source_summaries : {};
    const xuci = summaries.xuci || null;
    const changyong = summaries.changyong || null;

    if (el.sourceChipXuci) {
        el.sourceChipXuci.textContent = formatSourceChip('《古代汉语虚词词典》馆藏页图', xuci);
    }
    if (el.sourceChipChangyong) {
        el.sourceChipChangyong.textContent = formatSourceChip('王力《古汉语常用字字典》馆藏页图', changyong);
    }
    if (el.sourceChipExternal) {
        el.sourceChipExternal.textContent = '教育部辞典 / zi.tools / 汉语多功能字库 · 已接通';
    }

    if (!state.query && el.statusNote) {
        const parts = [];
        if (xuci) {
            parts.push(xuci.verified_headwords > 0
                ? `《古代汉语虚词词典》已核 ${xuci.verified_headwords} 条`
                : xuci.has_candidates
                    ? '《古代汉语虚词词典》候选页待复核'
                    : '《古代汉语虚词词典》尚未导入');
        }
        if (changyong) {
            parts.push(changyong.verified_headwords > 0
                ? `王力本已核 ${changyong.verified_headwords} 条${changyong.coverage_ratio ? `，覆盖 ${Math.round(Number(changyong.coverage_ratio) * 100)}%` : ''}`
                : changyong.has_candidates
                    ? '王力本候选页待复核'
                    : '王力本尚未导入');
        }
        if (parts.length) {
            setStatusNoteText(`${parts.join('；')}。学生端只展示已核定的辞典原页。`);
        }
    }
    if (!state.query) {
        renderIdleFlow();
    }
}

async function loadStatus() {
    try {
        const payload = await fetchJson(`${API}/api/dict/status`);
        renderStatus(payload);
    } catch (_) {
        // ignore status failures
    }
}

function formatPageLabel(entry) {
    const numbers = Array.isArray(entry.page_numbers) ? entry.page_numbers : [];
    if (numbers.length > 1) {
        return `页 ${numbers[0]}-${numbers[numbers.length - 1]}`;
    }
    if (numbers.length === 1) {
        return `页 ${numbers[0]}`;
    }
    if (entry.page_start && entry.page_end && entry.page_start !== entry.page_end) {
        return `页 ${entry.page_start}-${entry.page_end}`;
    }
    if (entry.page_start) {
        return `页 ${entry.page_start}`;
    }
    return '页码待补';
}

function buildDictContext(entries) {
    return (entries || []).slice(0, 8).map(item => {
        const pageLabel = formatPageLabel(item);
        const text = item.entry_text
            ? item.entry_text.slice(0, 420)
            : `学生端仅展示馆藏原页图片，当前条目定位到 ${pageLabel}。`;
        const trad = item.headword_trad && item.headword_trad !== item.headword
            ? `（${item.headword_trad}）`
            : '';
        return `[${item.dict_label}·${pageLabel}] ${item.headword}${trad}\n${text}`;
    }).join('\n\n');
}

function buildTextbookContext(results) {
    return (results || []).slice(0, 8).map(item => {
        const page = item.logical_page != null ? `p${item.logical_page}` : '';
        return `[教材·${item.display_title || item.title}·${page}] ${(item.text || '').slice(0, 260)}`;
    }).join('\n\n');
}

function buildGaokaoContext(results) {
    return (results || []).slice(0, 6).map(item => {
        const meta = [item.year, item.category, item.title].filter(Boolean).join(' · ');
        return `[真题·${meta}] ${(item.text || '').slice(0, 260)}`;
    }).join('\n\n');
}

function renderTextbookResults(results) {
    el.tbCount.textContent = String(results.length);
    if (!results.length) {
        setEmptyLine(el.tbResults, '没有找到教材中的古文或古诗词命中。');
        return;
    }
    el.tbResults.innerHTML = results.map(item => {
        const meta = [];
        const displayTitle = item.display_title || item.title || '';
        if (displayTitle) meta.push(`<span class="dict-card-title">${escHtml(displayTitle)}</span>`);
        if (item.title && item.title !== displayTitle) meta.push(`<span>${escHtml(item.title)}</span>`);
        if (item.classical_kind) meta.push(`<span>${escHtml(item.classical_kind)}</span>`);
        if (item.logical_page != null) meta.push(`<span>p${item.logical_page}</span>`);
        return `
            <article class="dict-card">
                <div class="dict-card-meta">${meta.join('')}</div>
                <div class="dict-card-text">${highlightText(item.snippet || item.text || '', state.query)}</div>
                <div class="dict-card-actions">
                    ${item.page_url ? `<button class="dict-action-link" type="button" data-book-key="${escHtml(item.book_key)}" data-page="${item.section}">查看原文</button>` : ''}
                </div>
            </article>
        `;
    }).join('');
    el.tbResults.querySelectorAll('[data-book-key]').forEach(button => {
        button.addEventListener('click', () => openBookModal(button.dataset.bookKey, Number(button.dataset.page || '0')));
    });
}

function buildModalPagesFromEntry(entry) {
    const pageUrls = Array.isArray(entry.page_urls) ? entry.page_urls : [];
    const pageNumbers = Array.isArray(entry.page_numbers) ? entry.page_numbers : [];
    return pageUrls.map((url, index) => ({
        page: pageNumbers[index] || (entry.page_start || 1) + index,
        url,
    }));
}

function openDictionaryEntry(entry) {
    const pages = buildModalPagesFromEntry(entry);
    if (pages.length) {
        openModalWithPages(pages);
        return;
    }
    if (entry.dict_source && entry.page_start) {
        openDictModal(entry.dict_source, entry.page_start, entry.page_end || entry.page_start);
    }
}

function renderDictionaryEntries(payload) {
    const entries = payload.entries || [];
    state.dictEntries = entries;
    el.dictCount.textContent = String(entries.length);

    if (!entries.length) {
        const hasRuntimeIndex = Boolean(state.statusPayload && state.statusPayload.available);
        const message = !hasRuntimeIndex
            ? '馆藏辞典页图索引尚未就绪。上线后，这里只会展示字头原页图片与页码。'
            : state.query.length > 1
                ? `馆藏辞典当前按已核字头返回页图；「${state.query}」未命中时，建议先看右侧参考，或拆成关键单字继续查。`
                : `该字暂未命中已核页图索引，可先看右侧官方参考。`;
        setEmptyLine(el.dictEntries, message);
        return;
    }

    el.dictEntries.innerHTML = entries.map((item, index) => {
        const thumbs = (item.page_urls || []).slice(0, 3).map((url, thumbIndex) => `
            <button class="dict-thumb" type="button" data-entry-index="${index}" data-thumb-index="${thumbIndex}">
                <img src="${escHtml(url)}" alt="${escHtml(item.headword)} 第 ${thumbIndex + 1} 张页图">
            </button>
        `).join('');
        const trad = item.headword_trad && item.headword_trad !== item.headword
            ? `<span class="dict-entry-trad">${escHtml(item.headword_trad)}</span>`
            : '';
        const verified = item.verified
            ? '<span class="dict-verified">已核页图</span>'
            : '<span class="dict-verified is-soft">待复核</span>';
        return `
            <article class="dict-entry-card dict-page-entry">
                <div class="dict-entry-source ${escHtml(item.dict_source)}">${escHtml(item.dict_label)}</div>
                <div class="dict-entry-top">
                    <span class="dict-entry-headword">${escHtml(item.headword)}</span>
                    ${trad}
                    ${verified}
                </div>
                <div class="dict-page-meta">
                    <span>${escHtml(formatPageLabel(item))}</span>
                    <span>${escHtml(String(item.page_count || (item.page_urls || []).length || 1))} 张页图</span>
                    <span>${item.match_mode === 'exact_headword' ? '字头精确命中' : '相关字头命中'}</span>
                </div>
                ${thumbs ? `<div class="dict-thumb-grid">${thumbs}</div>` : '<div class="dict-empty-line">页图待导入</div>'}
                <div class="dict-entry-actions">
                    <button class="dict-action-link" type="button" data-entry-index="${index}">查看全部页</button>
                </div>
            </article>
        `;
    }).join('');

    el.dictEntries.querySelectorAll('[data-entry-index]').forEach(button => {
        button.addEventListener('click', () => {
            const entry = state.dictEntries[Number(button.dataset.entryIndex || '0')];
            if (entry) openDictionaryEntry(entry);
        });
    });
}

function renderReferenceCards(payload) {
    const references = payload.references || [];
    el.refCount.textContent = String(references.length);
    if (!references.length) {
        setEmptyLine(el.refResults, state.query.length > 1
            ? '当前没有可用的整词参考；可尝试拆成关键单字继续查。'
            : '当前没有可用的外部参考。');
        return;
    }

    el.refResults.innerHTML = references.map(item => {
        const tagClass = item.category === 'official' ? 'official' : 'supplementary';
        const action = item.url
            ? `<a class="dict-ref-link" href="${escHtml(item.url)}" target="_blank" rel="noreferrer">${escHtml(item.action_label || '打开')}</a>`
            : '';
        const splitItems = Array.isArray(item.items) && item.items.length
            ? `<div class="dict-ref-split">${item.items.map(split => `
                <a class="dict-ref-chip" href="${escHtml(split.url)}" target="_blank" rel="noreferrer">${escHtml(split.char)}</a>
            `).join('')}</div>`
            : '';
        return `
            <article class="dict-ref-card">
                <div class="dict-ref-meta">
                    <span class="dict-ref-tag ${tagClass}">${item.category === 'official' ? '官方' : '补充'}</span>
                    <span>${escHtml(item.scope || '')}</span>
                    <span>${item.match_mode === 'split_chars' ? '拆字查看' : '整词直达'}</span>
                </div>
                <h3>${escHtml(item.label)}</h3>
                <p>${escHtml(item.summary || '')}</p>
                ${splitItems}
                <div class="dict-ref-actions">${action}</div>
            </article>
        `;
    }).join('');
}

function renderGaokaoResults(results) {
    el.gkCount.textContent = String(results.length);
    if (!results.length) {
        setEmptyLine(el.gkResults, '没有找到真题中的古文或古诗词命中。');
        return;
    }
    el.gkResults.innerHTML = results.map(item => {
        const meta = [item.year, item.category, item.title]
            .filter(Boolean)
            .map(value => `<span>${escHtml(value)}</span>`)
            .join('');
        return `
            <article class="dict-gk-card">
                <div class="dict-gk-meta">${meta}</div>
                <div class="dict-gk-text">${highlightText(item.snippet || item.text || '', state.query)}</div>
            </article>
        `;
    }).join('');
}

function showResults() {
    el.empty.classList.add('hidden');
    el.results.classList.remove('hidden');
}

function resetPanelsForSearch() {
    showResults();
    setLoading(el.tbResults);
    setLoading(el.dictEntries);
    setLoading(el.refResults);
    setLoading(el.gkResults);
    el.tbCount.textContent = '0';
    el.dictCount.textContent = '0';
    el.refCount.textContent = '0';
    el.gkCount.textContent = '0';
    state.chatHistory = [];
    state.dictEntries = [];
    renderChatMessages();
    setChatPending(true);
    updateAiFlow('is-pending', '等待教材、辞典与真题证据加载完成，再自动生成首轮分析。', '证据加载完成后会自动触发 AI');
}

async function sendChatMessage(message, { silentUser = false, queryKey = state.queryKey, expectedQuery = state.query } = {}) {
    const userMessage = String(message || '').trim();
    if (!userMessage || !state.query || state.chatInFlight) return;
    if (queryKey !== state.queryKey || expectedQuery !== state.query) return;

    const historyForRequest = [...state.chatHistory];
    if (!silentUser) {
        state.chatHistory.push({ role: 'user', content: userMessage });
        renderChatMessages();
    }

    state.chatHistory.push({ role: 'assistant', content: '思考中…' });
    renderChatMessages();
    setChatPending(true);
    updateAiFlow('is-active', '正在综合教材、馆藏辞典与真题证据，生成本轮学习建议。', 'AI 正在整理首轮或追问回答');

    try {
        const data = await fetchJson(`${API}/api/dict/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                headword: state.query,
                user_message: userMessage,
                dict_context: state.dictContext,
                textbook_context: state.textbookContext,
                gaokao_context: state.gaokaoContext,
                history: historyForRequest,
            }),
        });
        if (queryKey !== state.queryKey || expectedQuery !== state.query) {
            return;
        }
        state.chatHistory.pop();
        if (silentUser) {
            state.chatHistory.push({ role: 'user', content: `请系统梳理「${state.query}」的教材、词典与真题要点。` });
        }
        state.chatHistory.push({ role: 'assistant', content: String(data.answer || 'AI 没有返回内容。') });
        updateAiFlow('is-active', 'AI 已根据教材、辞典与真题给出首轮分析，可继续追问具体失分点、翻译题和背诵方法。', '多轮上下文已保留，可直接继续追问');
    } catch (error) {
        if (queryKey !== state.queryKey || expectedQuery !== state.query) {
            return;
        }
        state.chatHistory.pop();
        state.chatHistory.push({ role: 'assistant', content: `请求失败：${error.message}` });
        updateAiFlow('is-warning', `AI 请求失败：${error.message}`, '可稍后重试，教材与真题结果仍可继续查看');
    } finally {
        if (queryKey !== state.queryKey || expectedQuery !== state.query) {
            return;
        }
        renderChatMessages();
        setChatPending(false);
    }
}

async function runSearch(initialQuery) {
    const query = String(initialQuery || el.input.value || '').trim();
    if (!query) return;

    const queryKey = `${++state.searchSeq}:${query}`;
    state.searchInFlight = true;
    state.query = query;
    state.queryKey = queryKey;
    el.input.value = query;
    resetPanelsForSearch();
    if (el.flowQuery) {
        el.flowQuery.textContent = `当前检索：${query}`;
    }
    setFlowStep(el.flowTextbook, el.flowTextbookBody, el.flowTextbookFoot, {
        statusClass: 'is-pending',
        body: '正在筛选教材中的古文与古诗词命中。',
        foot: '教材证据加载中',
    });
    setFlowStep(el.flowDict, el.flowDictBody, el.flowDictFoot, {
        statusClass: 'is-pending',
        body: '正在定位馆藏辞典页图，并同步准备官方与外部参考。',
        foot: '馆藏辞典与参考层加载中',
    });
    setFlowStep(el.flowGaokao, el.flowGaokaoBody, el.flowGaokaoFoot, {
        statusClass: 'is-pending',
        body: '正在筛选语文真题中的古文与古诗词命中。',
        foot: '真题证据加载中',
    });
    history.replaceState({}, '', `/dict.html?q=${encodeURIComponent(query)}`);

    try {
        const [textbookResult, dictResult, referenceResult, gaokaoResult] = await Promise.allSettled([
            fetchJson(`${API}/api/dict/textbook?q=${encodeURIComponent(query)}&limit=30`),
            fetchJson(`${API}/api/dict/search?q=${encodeURIComponent(query)}&limit=20`),
            fetchJson(`${API}/api/dict/references?q=${encodeURIComponent(query)}`),
            fetchJson(`${API}/api/dict/gaokao?q=${encodeURIComponent(query)}&limit=20`),
        ]);

        const textbookFailed = textbookResult.status === 'rejected';
        const dictFailed = dictResult.status === 'rejected';
        const referenceFailed = referenceResult.status === 'rejected';
        const gaokaoFailed = gaokaoResult.status === 'rejected';

        const textbook = textbookFailed ? { results: [] } : textbookResult.value;
        const dictPayload = dictFailed ? { entries: [], available: false, source_mode: 'unavailable' } : dictResult.value;
        const referencePayload = referenceFailed ? { references: [] } : referenceResult.value;
        const gaokao = gaokaoFailed ? { results: [] } : gaokaoResult.value;

        if (queryKey !== state.queryKey || query !== state.query) {
            return;
        }

        if (textbookFailed) {
            el.tbCount.textContent = '0';
            setEmptyLine(el.tbResults, `教材检索失败：${textbookResult.reason.message}`);
        } else {
            renderTextbookResults(textbook.results || []);
        }

        if (dictFailed) {
            el.dictCount.textContent = '0';
            setEmptyLine(el.dictEntries, `馆藏辞典检索失败：${dictResult.reason.message}`);
        } else {
            renderDictionaryEntries(dictPayload);
        }

        if (referenceFailed) {
            el.refCount.textContent = '0';
            setEmptyLine(el.refResults, `外部参考加载失败：${referenceResult.reason.message}`);
        } else {
            renderReferenceCards(referencePayload);
        }

        if (gaokaoFailed) {
            el.gkCount.textContent = '0';
            setEmptyLine(el.gkResults, `真题检索失败：${gaokaoResult.reason.message}`);
        } else {
            renderGaokaoResults(gaokao.results || []);
        }

        state.dictContext = buildDictContext(dictPayload.entries || []);
        state.textbookContext = buildTextbookContext(textbook.results || []);
        state.gaokaoContext = buildGaokaoContext(gaokao.results || []);

        const firstTextbook = (textbook.results || [])[0] || null;
        const firstGaokao = (gaokao.results || [])[0] || null;
        const summary = {
            query,
            textbookCount: (textbook.results || []).length,
            textbookFailed,
            textbookError: textbookFailed ? textbookResult.reason.message : '',
            textbookTitle: firstTextbook ? (firstTextbook.display_title || firstTextbook.title || '') : '',
            dictCount: (dictPayload.entries || []).length,
            dictFailed,
            dictError: dictFailed ? dictResult.reason.message : '',
            dictFoot: buildDictFoot(dictPayload, (referencePayload.references || []).length),
            referenceCount: (referencePayload.references || []).length,
            referenceFailed,
            referenceError: referenceFailed ? referenceResult.reason.message : '',
            gaokaoCount: (gaokao.results || []).length,
            gaokaoFailed,
            gaokaoError: gaokaoFailed ? gaokaoResult.reason.message : '',
            gaokaoMeta: firstGaokao
                ? [firstGaokao.year, firstGaokao.category, firstGaokao.title].filter(Boolean).join(' · ')
                : '',
        };
        renderSearchFlow(summary);
        applySearchStatusNote(summary);

        el.chatInput.placeholder = `继续追问「${query}」，例如：这个字在翻译题里怎么稳拿分？`;
        const evidenceReady = !textbookFailed || !dictFailed || !gaokaoFailed;
        state.searchInFlight = false;
        if (evidenceReady) {
            setChatPending(false);
            void sendChatMessage(`请分析「${query}」这个字或词：先抓教材中的核心义项，再对应真题拿分点。`, { silentUser: true, queryKey, expectedQuery: query });
        } else {
            setChatPending(true);
            updateAiFlow('is-warning', '当前教材、馆藏辞典和真题证据都未成功加载，暂不自动生成 AI 分析。', '先修复接口或网络，再重新检索');
        }
    } catch (error) {
        if (queryKey !== state.queryKey || query !== state.query) {
            return;
        }
        setEmptyLine(el.tbResults, `教材检索失败：${error.message}`);
        setEmptyLine(el.dictEntries, `词典检索失败：${error.message}`);
        setEmptyLine(el.refResults, `参考源加载失败：${error.message}`);
        setEmptyLine(el.gkResults, `真题检索失败：${error.message}`);
        setChatPending(true);
        setStatusNoteText(`本次检索失败：${error.message}`);
        updateAiFlow('is-warning', `检索流程中断：${error.message}`, '修复后重新检索即可恢复 AI 总结');
    } finally {
        if (queryKey === state.queryKey && query === state.query) {
            state.searchInFlight = false;
        }
    }
}

function openModalWithPages(pages) {
    if (!pages.length) return;
    state.modalPages = pages;
    state.modalIndex = 0;
    renderModal();
    el.modal.classList.remove('hidden');
    el.modal.setAttribute('aria-hidden', 'false');
}

function renderModal() {
    const current = state.modalPages[state.modalIndex];
    if (!current) return;
    el.modalImage.src = current.url;
    el.modalMeta.textContent = `第 ${current.page} 页`;
    el.modalPrev.disabled = state.modalIndex === 0;
    el.modalNext.disabled = state.modalIndex >= state.modalPages.length - 1;
}

function closeModal() {
    el.modal.classList.add('hidden');
    el.modal.setAttribute('aria-hidden', 'true');
    state.modalPages = [];
    state.modalIndex = 0;
    el.modalImage.src = '';
}

async function openBookModal(bookKey, page) {
    try {
        const data = await fetchJson(`${API}/api/page-image?book_key=${encodeURIComponent(bookKey)}&page=${page}&context=2`);
        openModalWithPages((data.pages || []).map(item => ({ page: item.page, url: item.url })));
    } catch (error) {
        window.alert(`无法加载教材页图：${error.message}`);
    }
}

async function openDictModal(dictSource, pageStart, pageEnd) {
    const context = Math.max(2, Math.min(8, Math.max(0, pageEnd - pageStart)));
    try {
        const data = await fetchJson(`${API}/api/dict/page-images?dict_source=${encodeURIComponent(dictSource)}&page=${pageStart}&context=${context}`);
        const relevant = (data.pages || [])
            .filter(item => item.page >= pageStart && item.page <= Math.max(pageStart, pageEnd))
            .map(item => ({ page: item.page, url: item.url }));
        openModalWithPages(relevant.length ? relevant : (data.pages || []).map(item => ({ page: item.page, url: item.url })));
    } catch (error) {
        window.alert(`无法加载词典页图：${error.message}`);
    }
}

el.searchBtn.addEventListener('click', () => runSearch(el.input.value));
el.input.addEventListener('keydown', event => {
    if (event.key === 'Enter') runSearch(el.input.value);
});
suggestionButtons.forEach(button => {
    button.addEventListener('click', () => {
        const value = button.dataset.dictSuggestion || '';
        el.input.value = value;
        runSearch(value);
    });
});

el.chatSendBtn.addEventListener('click', () => {
    const message = el.chatInput.value.trim();
    if (!message) return;
    el.chatInput.value = '';
    sendChatMessage(message);
});

el.chatInput.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        el.chatSendBtn.click();
    }
});

el.chatCopyBtn.addEventListener('click', async () => {
    try {
        await navigator.clipboard.writeText(historyToCopyText());
        const previous = el.chatCopyBtn.textContent;
        el.chatCopyBtn.textContent = '已复制';
        setTimeout(() => {
            el.chatCopyBtn.textContent = previous;
        }, 1200);
    } catch (_) {
        // ignore clipboard failures
    }
});

el.modalClose.addEventListener('click', closeModal);
el.modal.addEventListener('click', event => {
    if (event.target === el.modal) closeModal();
});
document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !el.modal.classList.contains('hidden')) {
        closeModal();
    }
});
el.modalPrev.addEventListener('click', () => {
    if (state.modalIndex > 0) {
        state.modalIndex -= 1;
        renderModal();
    }
});
el.modalNext.addEventListener('click', () => {
    if (state.modalIndex < state.modalPages.length - 1) {
        state.modalIndex += 1;
        renderModal();
    }
});

const q = new URLSearchParams(window.location.search).get('q');
renderIdleFlow();
loadStatus();
if (q) {
    runSearch(q);
}
