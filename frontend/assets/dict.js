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
    tbCount: document.getElementById('tb-count'),
    tbResults: document.getElementById('tb-results'),
    dictCount: document.getElementById('dict-count'),
    dictEntries: document.getElementById('dict-entries'),
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
    const d = document.createElement('div');
    d.textContent = value;
    return d.innerHTML;
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

async function loadStatus() {
    try {
        const payload = await fetchJson(`${API}/api/dict/status`);
        state.statusPayload = payload;
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
            ? '馆藏辞典页图索引尚未就绪。'
            : state.query.length > 1
                ? `「${state.query}」未命中馆藏辞典，建议拆成关键单字继续查。`
                : `该字暂未命中已核页图索引。`;
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
    setLoading(el.gkResults);
    el.tbCount.textContent = '0';
    el.dictCount.textContent = '0';
    el.gkCount.textContent = '0';
    state.chatHistory = [];
    state.dictEntries = [];
    renderChatMessages();
    setChatPending(true);
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
    } catch (error) {
        if (queryKey !== state.queryKey || expectedQuery !== state.query) {
            return;
        }
        state.chatHistory.pop();
        state.chatHistory.push({ role: 'assistant', content: `请求失败：${error.message}` });
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
    history.replaceState({}, '', `/dict.html?q=${encodeURIComponent(query)}`);

    try {
        const [textbookResult, dictResult, gaokaoResult] = await Promise.allSettled([
            fetchJson(`${API}/api/dict/textbook?q=${encodeURIComponent(query)}&limit=30`),
            fetchJson(`${API}/api/dict/search?q=${encodeURIComponent(query)}&limit=20`),
            fetchJson(`${API}/api/dict/gaokao?q=${encodeURIComponent(query)}&limit=20`),
        ]);

        const textbookFailed = textbookResult.status === 'rejected';
        const dictFailed = dictResult.status === 'rejected';
        const gaokaoFailed = gaokaoResult.status === 'rejected';

        const textbook = textbookFailed ? { results: [] } : textbookResult.value;
        const dictPayload = dictFailed ? { entries: [], available: false, source_mode: 'unavailable' } : dictResult.value;
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

        if (gaokaoFailed) {
            el.gkCount.textContent = '0';
            setEmptyLine(el.gkResults, `真题检索失败：${gaokaoResult.reason.message}`);
        } else {
            renderGaokaoResults(gaokao.results || []);
        }

        state.dictContext = buildDictContext(dictPayload.entries || []);
        state.textbookContext = buildTextbookContext(textbook.results || []);
        state.gaokaoContext = buildGaokaoContext(gaokao.results || []);

        el.chatInput.placeholder = `继续追问「${query}」…`;
        const evidenceReady = !textbookFailed || !dictFailed || !gaokaoFailed;
        state.searchInFlight = false;
        if (evidenceReady) {
            setChatPending(false);
            void sendChatMessage(`请分析「${query}」这个字或词：先抓教材中的核心义项，再对应真题拿分点。`, { silentUser: true, queryKey, expectedQuery: query });
        } else {
            setChatPending(true);
        }
    } catch (error) {
        if (queryKey !== state.queryKey || query !== state.query) {
            return;
        }
        setEmptyLine(el.tbResults, `教材检索失败：${error.message}`);
        setEmptyLine(el.dictEntries, `词典检索失败：${error.message}`);
        setEmptyLine(el.gkResults, `真题检索失败：${error.message}`);
        setChatPending(true);
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
loadStatus();
if (q) {
    runSearch(q);
}
