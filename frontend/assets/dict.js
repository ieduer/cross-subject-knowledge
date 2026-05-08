const API = '';
const PHASE = document.body.dataset.phase || '高中';

const state = {
    mode: 'lookup',
    query: '',
    queryKey: '',
    chatHistory: [],
    dictContext: '',
    textbookContext: '',
    gaokaoContext: '',
    statusPayload: null,
    modalPages: [],
    modalIndex: 0,
    modalTitle: '',
    searchInFlight: false,
    chatInFlight: false,
    dictEntries: [],
    moeEntries: [],
    idiomEntries: [],
    searchSeq: 0,
    examData: {
        xuci: null,
        shici: null,
    },
    examSelected: {
        xuci: '',
        shici: '',
    },
    examXuciDetailCache: {},
    examTermContextCache: {},
    examExpandedYears: {},
};

const el = {
    input: document.getElementById('dict-input'),
    searchBtn: document.getElementById('dict-search-btn'),
    modeButtons: Array.from(document.querySelectorAll('[data-dict-mode]')),
    empty: document.getElementById('dict-empty'),
    results: document.getElementById('dict-results'),
    examXuciView: document.getElementById('exam-xuci-view'),
    examShiciView: document.getElementById('exam-shici-view'),
    tbCount: document.getElementById('tb-count'),
    tbResults: document.getElementById('tb-results'),
    dictCount: document.getElementById('dict-count'),
    dictEntries: document.getElementById('dict-entries'),
    moeCount: document.getElementById('moe-count'),
    moeResults: document.getElementById('moe-results'),
    idiomCount: document.getElementById('idiom-count'),
    idiomResults: document.getElementById('idiom-results'),
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
    modalTitle: document.getElementById('page-modal-title'),
    modalMeta: document.getElementById('page-modal-meta'),
    examXuciCount: document.getElementById('exam-xuci-count'),
    examXuciCoverage: document.getElementById('exam-xuci-coverage'),
    examXuciStats: document.getElementById('exam-xuci-stats'),
    examXuciList: document.getElementById('exam-xuci-list'),
    examXuciDetail: document.getElementById('exam-xuci-detail'),
    examShiciCount: document.getElementById('exam-shici-count'),
    examShiciCoverage: document.getElementById('exam-shici-coverage'),
    examShiciStats: document.getElementById('exam-shici-stats'),
    examShiciList: document.getElementById('exam-shici-list'),
    examShiciDetail: document.getElementById('exam-shici-detail'),
};

const suggestionButtons = Array.from(document.querySelectorAll('[data-dict-suggestion]'));
const EXAM_MODE_TO_KIND = {
    'exam-xuci': 'xuci',
    'exam-shici': 'shici',
};
const EXAM_KIND_TO_MODE = {
    xuci: 'exam-xuci',
    shici: 'exam-shici',
};

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

function formatReadOnlyText(text) {
    const lines = String(text || '')
        .split(/\n{2,}/)
        .map(item => item.trim())
        .filter(Boolean);
    if (!lines.length) return '';
    return lines.map(item => `<p>${escHtml(item).replace(/\n/g, '<br>')}</p>`).join('');
}

function examModeToKind(mode) {
    return EXAM_MODE_TO_KIND[mode] || '';
}

function examKindToMode(kind) {
    return EXAM_KIND_TO_MODE[kind] || 'lookup';
}

function buildExamHash(kind, headword) {
    const cleanHeadword = String(headword || '').trim();
    if (!cleanHeadword || !EXAM_KIND_TO_MODE[kind]) return '';
    return `#${kind}-${cleanHeadword}`;
}

function parseExamHash(hash = window.location.hash) {
    const clean = decodeURIComponent(String(hash || '').replace(/^#/, '')).trim();
    if (!clean) return null;
    for (const kind of Object.keys(EXAM_KIND_TO_MODE)) {
        const prefix = `${kind}-`;
        if (!clean.startsWith(prefix)) continue;
        const headword = clean.slice(prefix.length).trim();
        if (!headword) return null;
        return { kind, headword };
    }
    return null;
}

function replaceLocationHash(hash) {
    const nextHash = hash || '';
    const currentHash = window.location.hash || '';
    if (currentHash === nextHash) return;
    history.replaceState({}, '', `${window.location.pathname}${window.location.search}${nextHash}`);
}

function syncExamHash(kind, headword) {
    replaceLocationHash(buildExamHash(kind, headword));
}

function clearExamHash() {
    if (!parseExamHash()) return;
    replaceLocationHash('');
}

function examTermCacheKey(kind, headword) {
    return `${kind}:${String(headword || '').trim()}`;
}

function getExamExpandedYear(kind, headword, years) {
    const cleanYears = (years || []).map(value => Number(value)).filter(Number.isFinite);
    if (!cleanYears.length) return 0;
    const key = examTermCacheKey(kind, headword);
    const current = Number(state.examExpandedYears[key] || 0);
    if (cleanYears.includes(current)) return current;
    return Math.max(...cleanYears);
}

function setExamExpandedYear(kind, headword, year) {
    const cleanYear = Number(year || 0);
    if (!cleanYear) return;
    state.examExpandedYears[examTermCacheKey(kind, headword)] = cleanYear;
}

function isCompactViewport() {
    return window.matchMedia('(max-width: 960px)').matches;
}

function scrollExamDetailIntoView(kind) {
    const detailEl = kind === 'xuci' ? el.examXuciDetail : el.examShiciDetail;
    if (!detailEl || !isCompactViewport()) return;
    requestAnimationFrame(() => {
        detailEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
}

function buildExamShareUrl(kind, headword) {
    return `${window.location.origin}${window.location.pathname}${window.location.search}${buildExamHash(kind, headword)}`;
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

function updateModeButtons() {
    el.modeButtons.forEach(button => {
        button.classList.toggle('active', button.dataset.dictMode === state.mode);
    });
}

function updateModeVisibility() {
    const lookupActive = state.mode === 'lookup';
    const examXuciActive = state.mode === 'exam-xuci';
    const examShiciActive = state.mode === 'exam-shici';

    el.empty.classList.toggle('hidden', !lookupActive || Boolean(state.query));
    el.results.classList.toggle('hidden', !lookupActive || !state.query);
    el.examXuciView.classList.toggle('hidden', !examXuciActive);
    el.examShiciView.classList.toggle('hidden', !examShiciActive);
    updateModeButtons();
}

function examLabel(kind) {
    return kind === 'xuci' ? '真题虚词' : '真题实词';
}

function trimExamText(text, limit = 900) {
    const raw = String(text || '').trim();
    if (!raw) return '';
    return raw.length > limit ? `${raw.slice(0, limit)}…` : raw;
}

function isExamMeaningfulText(text) {
    const raw = String(text || '').trim();
    if (!raw) return false;
    const compact = raw.replace(/\s+/g, '');
    const hanCount = (compact.match(/[\u4e00-\u9fff]/g) || []).length;
    const latinCount = (compact.match(/[A-Za-z0-9]/g) || []).length;
    return hanCount >= 2 || latinCount >= 4;
}

function summarizeExamText(text, limit = 140) {
    return trimExamText(String(text || '').replace(/\s+/g, ' ').trim(), limit);
}

const EXAM_XUCI_SPECIAL_USAGES = new Set(['固定格式', '惯用词组']);

function normalizeExamXuciPatternLabel(text) {
    return String(text || '')
        .replace(/\s+/g, '')
        .replace(/……/g, '......')
        .replace(/[.·•]{2,}/g, '......')
        .replace(/([一-龥])(?:[.·•]|\.{1,6}|[-_/]){1,}([一-龥])/g, '$1......$2');
}

function describeExamXuciSection(section) {
    const usage = String(section && section.usage || '').trim();
    const senses = Array.isArray(section && section.senses)
        ? section.senses.filter(item => item && item.label).slice(0, 8)
        : [];
    const primarySense = senses[0] || null;
    const special = EXAM_XUCI_SPECIAL_USAGES.has(usage);
    const primaryLabel = special && primarySense ? normalizeExamXuciPatternLabel(primarySense.label || '') : '';
    const title = primaryLabel ? `${usage} · ${primaryLabel}` : (usage || '未归类');

    let summary = summarizeExamText(section && section.summary || '', 120);
    if (special && primarySense) {
        const primarySummary = summarizeExamText(primarySense.summary || '', 120);
        if (!isExamMeaningfulText(summary) || summary === primaryLabel) {
            summary = primarySummary || summary;
        }
    }

    const displaySenses = senses.map((sense, index) => {
        const label = normalizeExamXuciPatternLabel(sense.label || '');
        return {
            label,
            summary: summarizeExamText(sense.summary || '', 86),
            hidden: Boolean(primaryLabel && index === 0 && label === primaryLabel),
        };
    }).filter(item => !item.hidden);

    return {
        usage,
        title,
        summary,
        senses: displaySenses,
    };
}

function describeExamMindmapBranch(branch) {
    const label = String(branch && branch.label || '').trim() || '用法';
    const children = Array.isArray(branch && branch.children) ? branch.children.slice(0, 8) : [];
    const primaryChild = children[0] || null;
    const special = EXAM_XUCI_SPECIAL_USAGES.has(label);
    const primaryLabel = special && primaryChild ? normalizeExamXuciPatternLabel(primaryChild.label || '') : '';
    let summary = summarizeExamText(branch && branch.summary || '', 72);
    if (special && primaryChild) {
        const primarySummary = summarizeExamText(primaryChild.summary || '', 72);
        if (!isExamMeaningfulText(summary) || summary === primaryLabel) {
            summary = primarySummary || summary;
        }
    }
    return {
        title: primaryLabel ? `${label} · ${primaryLabel}` : label,
        summary,
        leaves: children.map((child, index) => ({
            label: normalizeExamXuciPatternLabel(child.label || ''),
            summary: summarizeExamText(child.summary || '', 56),
            hidden: Boolean(primaryLabel && index === 0 && normalizeExamXuciPatternLabel(child.label || '') === primaryLabel),
        })).filter(item => !item.hidden),
    };
}

function renderExamYearHistogram(kind, term) {
    const occurrences = Array.isArray(term.occurrences) ? term.occurrences : [];
    const counts = new Map();
    occurrences.forEach(item => {
        const year = Number(item.year || 0);
        if (!year) return;
        counts.set(year, (counts.get(year) || 0) + 1);
    });
    const years = Array.from(counts.entries()).sort((left, right) => left[0] - right[0]);
    if (!years.length) return '';
    const maxCount = Math.max(...years.map(([, count]) => count));
    const selectedYear = getExamExpandedYear(kind, term.headword, years.map(([year]) => year));
    return `
        <section class="dict-exam-detail-section">
            <h3 class="dict-exam-detail-title">年份分布</h3>
            <div class="dict-exam-year-grid">
                ${years.map(([year, count]) => `
                    <button
                        class="dict-exam-year-item ${selectedYear === year ? 'active' : ''}"
                        type="button"
                        data-exam-year-kind="${escHtml(kind)}"
                        data-exam-year-headword="${escHtml(term.headword || '')}"
                        data-exam-year="${escHtml(String(year))}"
                    >
                        <div class="dict-exam-year-meta">
                            <span>${escHtml(String(year))}</span>
                            <span>${escHtml(String(count))}</span>
                        </div>
                        <div class="dict-exam-year-bar">
                            <div class="dict-exam-year-fill" style="width: ${Math.max(8, Math.round((count / maxCount) * 100))}%"></div>
                        </div>
                    </button>
                `).join('')}
            </div>
        </section>
    `;
}

function renderExamXuciSections(sections) {
    if (!Array.isArray(sections) || !sections.length) {
        return '<div class="dict-empty-line">用法义项待补。</div>';
    }
    return `
        <div class="dict-exam-usage-grid">
            ${sections.slice(0, 8).map(section => {
                const display = describeExamXuciSection(section);
                return `
                    <article class="dict-exam-usage-card">
                        <div class="dict-exam-usage-head">
                            <div class="dict-exam-usage-title">${escHtml(display.title)}</div>
                            ${isExamMeaningfulText(display.summary) ? `<div class="dict-exam-usage-summary">${escHtml(display.summary)}</div>` : ''}
                        </div>
                        <div class="dict-exam-sense-list">
                            ${display.senses.length ? display.senses.map(sense => {
                                const label = summarizeExamText(sense.label || '', 48);
                                const senseSummary = summarizeExamText(sense.summary || '', 86);
                                return `
                                    <div class="dict-exam-sense">
                                        <div class="dict-exam-sense-label">${escHtml(label)}</div>
                                        ${isExamMeaningfulText(senseSummary) ? `<div class="dict-exam-sense-summary">${escHtml(senseSummary)}</div>` : ''}
                                    </div>
                                `;
                            }).join('') : '<div class="dict-empty-line">该用法下暂无更多细分义项。</div>'}
                        </div>
                    </article>
                `;
            }).join('')}
        </div>
    `;
}

function renderExamXuciMindmap(mindmap) {
    const children = Array.isArray(mindmap && mindmap.children) ? mindmap.children : [];
    if (!children.length) {
        return '<div class="dict-empty-line">思维导图待补。</div>';
    }
    return `
        <div class="dict-exam-mindmap">
            <div class="dict-exam-mindmap-root">${escHtml(String(mindmap.label || '虚词'))}</div>
            <div class="dict-exam-mindmap-branches">
                ${children.slice(0, 8).map(branch => {
                    const display = describeExamMindmapBranch(branch);
                    return `
                        <article class="dict-exam-mindmap-branch">
                            <div class="dict-exam-mindmap-node level-1">${escHtml(display.title)}</div>
                            ${isExamMeaningfulText(display.summary) ? `<div class="dict-exam-mindmap-summary">${escHtml(display.summary)}</div>` : ''}
                            <div class="dict-exam-mindmap-leaves">
                                ${display.leaves.length ? display.leaves.map(leaf => {
                                    const leafSummary = summarizeExamText(leaf.summary || '', 56);
                                    return `
                                        <div class="dict-exam-mindmap-node level-2">
                                            <div>${escHtml(String(leaf.label || '义项'))}</div>
                                            ${isExamMeaningfulText(leafSummary) ? `<div class="dict-exam-mindmap-summary">${escHtml(leafSummary)}</div>` : ''}
                                        </div>
                                    `;
                                }).join('') : '<div class="dict-empty-line">暂无细分义项。</div>'}
                            </div>
                        </article>
                    `;
                }).join('')}
            </div>
        </div>
    `;
}

function renderExamCoverage(kind, payload) {
    const coverageEl = kind === 'xuci' ? el.examXuciCoverage : el.examShiciCoverage;
    const statsEl = kind === 'xuci' ? el.examXuciStats : el.examShiciStats;
    const countEl = kind === 'xuci' ? el.examXuciCount : el.examShiciCount;

    countEl.textContent = '';
    countEl.hidden = true;
    coverageEl.innerHTML = '';
    coverageEl.hidden = true;
    statsEl.innerHTML = '';
    statsEl.hidden = true;
}

function renderExamDetail(kind, term) {
    const detailEl = kind === 'xuci' ? el.examXuciDetail : el.examShiciDetail;
    if (!term) {
        detailEl.innerHTML = `<div class="dict-empty-line">暂无${examLabel(kind)}数据。</div>`;
        return;
    }
    detailEl.innerHTML = `
        <div class="dict-exam-detail-head">
            <div class="dict-exam-detail-headword">${escHtml(term.display_headword || term.headword || '')}</div>
        </div>
        ${renderExamYearHistogram(kind, term)}
        <div id="exam-${escHtml(kind)}-async"></div>
    `;
    detailEl.querySelectorAll('[data-exam-year-kind][data-exam-year-headword][data-exam-year]').forEach(button => {
        button.addEventListener('click', () => {
            setExamExpandedYear(kind, term.headword, Number(button.dataset.examYear || '0'));
            renderExamDetail(kind, term);
        });
    });
    void loadExamTermContext(kind, term.headword);
}

function renderExamList(kind, payload) {
    const terms = Array.isArray(payload.terms) ? payload.terms : [];
    const listEl = kind === 'xuci' ? el.examXuciList : el.examShiciList;
    const selected = state.examSelected[kind] && terms.some(item => item.headword === state.examSelected[kind])
        ? state.examSelected[kind]
        : (terms[0] && terms[0].headword) || '';
    state.examSelected[kind] = selected;
    const maxCount = Math.max(1, ...terms.map(item => Number(item.total_occurrences || 0)));

    if (!terms.length) {
        listEl.innerHTML = `<div class="dict-empty-line">暂无${examLabel(kind)}数据。</div>`;
        renderExamDetail(kind, null);
        return;
    }

    listEl.innerHTML = terms.map(item => {
        const active = item.headword === selected;
        const width = Math.max(6, Math.round((Number(item.total_occurrences || 0) / maxCount) * 100));
        const glosses = (item.sample_glosses || []).slice(0, 2);
        const scopeCounts = [];
        if (Number(item.beijing_occurrences || 0) > 0) scopeCounts.push(`北京 ${item.beijing_occurrences}`);
        if (Number(item.national_occurrences || 0) > 0) scopeCounts.push(`全国 ${item.national_occurrences}`);
        return `
            <button class="dict-exam-item ${active ? 'active' : ''}" type="button" data-exam-kind="${kind}" data-headword="${escHtml(item.headword)}">
                <div class="dict-exam-item-top">
                    <span class="dict-exam-headword">${escHtml(item.display_headword || item.headword)}</span>
                    <span class="dict-exam-total">${escHtml(String(item.total_occurrences || 0))} 次</span>
                </div>
                <div class="dict-exam-bar">
                    <div class="dict-exam-bar-fill" style="width: ${width}%"></div>
                </div>
                <div class="dict-exam-item-meta">
                    <span>题目 ${escHtml(String(item.question_count || 0))}</span>
                    <span>${escHtml((item.year_labels || []).join(' / ') || '—')}</span>
                    <span>${escHtml(scopeCounts.join(' / ') || (item.source_tags || []).join(' / ') || '北京')}</span>
                </div>
                ${glosses.length ? `<div class="dict-exam-item-glosses">${glosses.map(item => escHtml(item)).join(' / ')}</div>` : ''}
            </button>
        `;
    }).join('');

    listEl.querySelectorAll('[data-exam-kind][data-headword]').forEach(button => {
        button.addEventListener('click', () => {
            state.examSelected[kind] = button.dataset.headword || '';
            renderExamList(kind, payload);
            scrollExamDetailIntoView(kind);
        });
    });

    renderExamDetail(kind, terms.find(item => item.headword === selected) || terms[0]);
    if (state.mode === examKindToMode(kind) && selected) {
        syncExamHash(kind, selected);
    }
}

function renderExamDataset(kind, payload) {
    state.examData[kind] = payload;
    renderExamCoverage(kind, payload);
    renderExamList(kind, payload);
}

function selectExamDictionaryEntries(payload, headword) {
    const entries = Array.isArray(payload && payload.entries) ? payload.entries : [];
    const bySource = new Map();
    for (const item of entries) {
        const source = String(item && item.dict_source || '').trim();
        if (!source || bySource.has(source)) continue;
        if (item.headword === headword && (source === 'xuci' || source === 'changyong')) {
            bySource.set(source, item);
        }
    }
    for (const item of entries) {
        const source = String(item && item.dict_source || '').trim();
        if (!source || bySource.has(source)) continue;
        if (source === 'xuci' || source === 'changyong') {
            bySource.set(source, item);
        }
    }
    return ['xuci', 'changyong']
        .map(source => bySource.get(source))
        .filter(Boolean);
}

function selectExamMoeEntries(payload, headword) {
    const entries = Array.isArray(payload && payload.entries) ? payload.entries : [];
    const exact = entries.filter(item => item && item.headword === headword);
    return (exact.length ? exact : entries).slice(0, 2);
}

function selectExamTextbookResults(payload) {
    const results = Array.isArray(payload && payload.results) ? payload.results : [];
    const picked = [];
    const seen = new Set();
    for (const item of results) {
        const key = `${item.book_key || ''}:${item.section || ''}:${item.display_title || item.title || ''}`;
        if (seen.has(key)) continue;
        seen.add(key);
        picked.push(item);
        if (picked.length >= 6) break;
    }
    return picked;
}

function renderExamQuestionSection(kind, headword, questionsPayload) {
    const questions = Array.isArray(questionsPayload && questionsPayload.questions)
        ? questionsPayload.questions
        : [];
    const years = Array.from(new Set(questions.map(item => Number(item.year || 0)).filter(Number.isFinite)));
    const selectedYear = getExamExpandedYear(kind, headword, years);
    const yearQuestions = questions.filter(item => Number(item.year || 0) === selectedYear);
    return `
        <section class="dict-exam-detail-section">
            <h3 class="dict-exam-detail-title">本年度真题</h3>
            <div class="dict-exam-evidence-list">
                ${yearQuestions.length ? yearQuestions.map(item => `
                    <article class="dict-exam-question-card">
                        <div class="dict-exam-evidence-meta">
                            ${item.year ? `<span>${escHtml(String(item.year))}</span>` : ''}
                            ${item.category ? `<span>${escHtml(item.category)}</span>` : ''}
                            ${item.title ? `<span>${escHtml(item.title)}</span>` : ''}
                            ${(item.question_numbers || []).length ? `<span>题号 ${escHtml(item.question_numbers.join(' / '))}</span>` : ''}
                        </div>
                        <div class="dict-exam-question-text">${highlightText(item.text || '', headword)}</div>
                        ${item.answer ? `
                            <details class="dict-exam-detail-disclosure">
                                <summary>查看答案</summary>
                                <div class="dict-exam-detail-text">${highlightText(item.answer, headword)}</div>
                            </details>
                        ` : ''}
                    </article>
                `).join('') : '<div class="dict-empty-line">当前年份暂无真题全文。</div>'}
            </div>
        </section>
    `;
}

function renderExamTextbookSection(kind, headword, textbookResults) {
    return `
        <section class="dict-exam-detail-section">
            <h3 class="dict-exam-detail-title">教材例句</h3>
            <div class="dict-exam-resource-grid">
                ${textbookResults.length ? textbookResults.map((item, index) => `
                    <article class="dict-exam-resource-card">
                        <div class="dict-exam-evidence-meta">
                            <span>${escHtml(item.display_title || item.title || '')}</span>
                            ${item.classical_kind ? `<span>${escHtml(item.classical_kind)}</span>` : ''}
                            ${item.logical_page != null ? `<span>p${escHtml(String(item.logical_page))}</span>` : ''}
                        </div>
                        ${item.page_url ? `
                            <button
                                class="dict-exam-media"
                                type="button"
                                data-exam-book-kind="${escHtml(kind)}"
                                data-exam-book-headword="${escHtml(headword)}"
                                data-exam-book-index="${escHtml(String(index))}"
                            >
                                <img src="${escHtml(item.page_url)}" alt="${escHtml(item.display_title || item.title || headword)} 原图">
                            </button>
                        ` : ''}
                        <div class="dict-exam-evidence-text">${highlightText(item.snippet || item.text || '', headword)}</div>
                    </article>
                `).join('') : '<div class="dict-empty-line">暂无教材例句。</div>'}
            </div>
        </section>
    `;
}

function renderExamDictionarySection(kind, headword, dictEntries) {
    return `
        <section class="dict-exam-detail-section">
            <h3 class="dict-exam-detail-title">馆藏辞典原图</h3>
            <div class="dict-exam-resource-grid">
                ${dictEntries.length ? dictEntries.map((item, index) => `
                    <article class="dict-entry-card dict-page-entry dict-exam-resource-card">
                        <div class="dict-entry-source ${escHtml(item.dict_source || '')}">${escHtml(item.dict_label || '馆藏辞典')}</div>
                        <div class="dict-page-meta">
                            <span>${escHtml(formatPageLabel(item))}</span>
                            <span>${escHtml(String(item.page_count || (item.page_urls || []).length || 1))} 张页图</span>
                        </div>
                        ${(item.page_urls || []).length ? `
                            <div class="dict-thumb-grid">
                                ${(item.page_urls || []).slice(0, 2).map((url, thumbIndex) => `
                                    <button
                                        class="dict-thumb"
                                        type="button"
                                        data-exam-dict-kind="${escHtml(kind)}"
                                        data-exam-dict-headword="${escHtml(headword)}"
                                        data-exam-dict-index="${escHtml(String(index))}"
                                        data-exam-dict-thumb="${escHtml(String(thumbIndex))}"
                                    >
                                        <img src="${escHtml(url)}" alt="${escHtml(item.headword || headword)} 页图">
                                    </button>
                                `).join('')}
                            </div>
                        ` : '<div class="dict-empty-line">页图待补。</div>'}
                        <div class="dict-entry-actions">
                            <button
                                class="dict-action-link"
                                type="button"
                                data-exam-dict-kind="${escHtml(kind)}"
                                data-exam-dict-headword="${escHtml(headword)}"
                                data-exam-dict-index="${escHtml(String(index))}"
                            >查看全部页</button>
                        </div>
                    </article>
                `).join('') : '<div class="dict-empty-line">当前未命中两本馆藏辞典页图。</div>'}
            </div>
        </section>
    `;
}

function renderExamMoeSection(moePayload, moeEntries) {
    const label = String(moePayload && moePayload.label || '教育部《重編國語辭典修訂本》');
    return `
        <section class="dict-exam-detail-section">
            <h3 class="dict-exam-detail-title">教育部修订本</h3>
            <div class="dict-exam-resource-grid">
                ${moeEntries.length ? moeEntries.map(item => `
                    <article class="dict-entry-card dict-moe-entry dict-exam-resource-card">
                        <div class="dict-entry-source moe-revised">${escHtml(label)}</div>
                        <div class="dict-entry-top">
                            <span class="dict-entry-headword">${escHtml(item.headword || '')}</span>
                            ${item.bopomofo ? `<span class="dict-entry-pinyin">${escHtml(item.bopomofo)}</span>` : ''}
                            ${item.pinyin ? `<span class="dict-entry-pinyin">${escHtml(item.pinyin)}</span>` : ''}
                        </div>
                        <div class="dict-entry-text dict-moe-text">${formatReadOnlyText(item.content_text || '') || '<p>暂无可展示释义。</p>'}</div>
                        <div class="dict-entry-actions">
                            <a class="dict-action-link" href="${escHtml(item.source_url || '#')}" target="_blank" rel="noopener noreferrer">打开教育部原站</a>
                        </div>
                    </article>
                `).join('') : '<div class="dict-empty-line">当前未命中教育部修订本。</div>'}
            </div>
        </section>
    `;
}

function bindExamTermContextActions(kind, headword) {
    const cacheKey = examTermCacheKey(kind, headword);
    const mount = document.getElementById(`exam-${kind}-async`);
    const context = state.examTermContextCache[cacheKey];
    if (!mount || !context) return;

    mount.querySelectorAll('[data-exam-dict-kind][data-exam-dict-headword][data-exam-dict-index]').forEach(button => {
        button.addEventListener('click', () => {
            const entry = context.dictEntries[Number(button.dataset.examDictIndex || '0')];
            if (entry) openDictionaryEntry(entry);
        });
    });
    mount.querySelectorAll('[data-exam-book-kind][data-exam-book-headword][data-exam-book-index]').forEach(button => {
        button.addEventListener('click', () => {
            const item = context.textbookResults[Number(button.dataset.examBookIndex || '0')];
            if (item && item.book_key && item.section != null) {
                openBookModal(item.book_key, Number(item.section));
            }
        });
    });
}

function renderExamTermContext(kind, headword, context) {
    const mount = document.getElementById(`exam-${kind}-async`);
    if (!mount) return;
    if (state.mode !== examKindToMode(kind) || state.examSelected[kind] !== headword) return;

    mount.innerHTML = [
        renderExamQuestionSection(kind, headword, context.questionPayload),
        renderExamTextbookSection(kind, headword, context.textbookResults),
        renderExamDictionarySection(kind, headword, context.dictEntries),
        renderExamMoeSection(context.moePayload, context.moeEntries),
    ].join('');
    bindExamTermContextActions(kind, headword);
}

async function loadExamTermContext(kind, headword) {
    const mount = document.getElementById(`exam-${kind}-async`);
    if (!mount || !headword) return;
    const cacheKey = examTermCacheKey(kind, headword);
    if (state.examTermContextCache[cacheKey]) {
        renderExamTermContext(kind, headword, state.examTermContextCache[cacheKey]);
        return;
    }

    mount.innerHTML = `
        <section class="dict-exam-detail-section">
            <div class="dict-loading">加载中…</div>
        </section>
    `;

    const [dictResult, moeResult, textbookResult, questionResult] = await Promise.allSettled([
        fetchJson(`${API}/api/dict/search?q=${encodeURIComponent(headword)}&limit=20`),
        fetchJson(`${API}/api/dict/moe-revised?q=${encodeURIComponent(headword)}&limit=6`),
        fetchJson(`${API}/api/dict/textbook?q=${encodeURIComponent(headword)}&limit=12`),
        fetchJson(`${API}/api/dict/exam/questions?kind=${encodeURIComponent(kind)}&headword=${encodeURIComponent(headword)}`),
    ]);

    const dictPayload = dictResult.status === 'fulfilled' ? dictResult.value : { entries: [] };
    const moePayload = moeResult.status === 'fulfilled' ? moeResult.value : { entries: [] };
    const textbookPayload = textbookResult.status === 'fulfilled' ? textbookResult.value : { results: [] };
    const questionPayload = questionResult.status === 'fulfilled' ? questionResult.value : { questions: [] };

    const context = {
        dictEntries: selectExamDictionaryEntries(dictPayload, headword),
        moePayload,
        moeEntries: selectExamMoeEntries(moePayload, headword),
        textbookResults: selectExamTextbookResults(textbookPayload),
        questionPayload,
    };
    state.examTermContextCache[cacheKey] = context;
    renderExamTermContext(kind, headword, context);
}

function renderExamXuciDetailSupplement(headword, payload) {
    const mount = document.getElementById('exam-xuci-supplement');
    if (!mount) return;
    if (state.mode !== 'exam-xuci' || state.examSelected.xuci !== headword) return;
    if (!payload || !payload.available || !payload.detail) {
        mount.innerHTML = '';
        return;
    }

    const detail = payload.detail;
    const xuciDict = detail.xuci_dict || {};
    const changyongDict = detail.changyong_dict || {};
    const textbookExamples = Array.isArray(detail.textbook_examples) ? detail.textbook_examples : [];
    const outline = Array.isArray(xuciDict.outline) ? xuciDict.outline.slice(0, 8) : [];
    const sections = Array.isArray(xuciDict.sections) ? xuciDict.sections : [];
    const overview = summarizeExamText(xuciDict.overview || '', 360);
    const excerpt = trimExamText(xuciDict.excerpt || '', 1600);
    const changyongExcerpt = trimExamText(changyongDict.excerpt || '', 800);
    const mindmap = xuciDict.mindmap || { label: headword, children: [] };

    mount.innerHTML = `
        <section class="dict-exam-detail-section">
            <h3 class="dict-exam-detail-title">辞典提要</h3>
            <div class="dict-exam-detail-meta">
                ${(xuciDict.pages || []).length ? `<span class="dict-exam-stat">虚词词典 ${escHtml((xuciDict.pages || []).join(' / '))}</span>` : ''}
                ${(changyongDict.pages || []).length ? `<span class="dict-exam-stat">常用字字典 ${escHtml((changyongDict.pages || []).join(' / '))}</span>` : ''}
                ${outline.map(item => `<span class="dict-exam-stat">${escHtml(item)}</span>`).join('')}
            </div>
            ${isExamMeaningfulText(overview) ? `<div class="dict-exam-detail-text">${escHtml(overview)}</div>` : '<div class="dict-empty-line">《古代汉语虚词词典》提要待补。</div>'}
            ${excerpt ? `
                <details class="dict-exam-detail-disclosure">
                    <summary>查看《古代汉语虚词词典》原摘</summary>
                    <div class="dict-exam-detail-text">${escHtml(excerpt)}</div>
                </details>
            ` : ''}
            ${changyongExcerpt ? `
                <details class="dict-exam-detail-disclosure">
                    <summary>查看《古汉语常用字字典》页内摘录</summary>
                    <div class="dict-exam-detail-text">${escHtml(changyongExcerpt)}</div>
                </details>
            ` : (changyongDict.skipped ? '<div class="dict-empty-line">《古汉语常用字字典》页区过长，暂不预生成整页摘录。</div>' : '')}
        </section>
        <section class="dict-exam-detail-section">
            <h3 class="dict-exam-detail-title">用法与义项</h3>
            ${renderExamXuciSections(sections)}
        </section>
        <section class="dict-exam-detail-section">
            <h3 class="dict-exam-detail-title">思维导图</h3>
            ${renderExamXuciMindmap(mindmap)}
        </section>
        <section class="dict-exam-detail-section">
            <h3 class="dict-exam-detail-title">教材例句</h3>
            <div class="dict-exam-evidence-list">
                ${textbookExamples.length ? textbookExamples.map(item => `
                    <article class="dict-exam-evidence">
                        <div class="dict-exam-evidence-meta">
                            <span>${escHtml(item.title || '')}</span>
                            ${item.kind ? `<span>${escHtml(item.kind)}</span>` : ''}
                            ${item.logical_page ? `<span>p${escHtml(String(item.logical_page))}</span>` : ''}
                        </div>
                        <div class="dict-exam-evidence-text">${highlightText(item.snippet || '', headword)}</div>
                    </article>
                `).join('') : '<div class="dict-empty-line">教材例句待补。</div>'}
            </div>
        </section>
    `;
}

async function loadExamXuciDetail(headword) {
    const mount = document.getElementById('exam-xuci-supplement');
    if (!mount || !headword) return;
    if (state.examXuciDetailCache[headword]) {
        renderExamXuciDetailSupplement(headword, state.examXuciDetailCache[headword]);
        return;
    }
    mount.innerHTML = `
        <section class="dict-exam-detail-section">
            <h3 class="dict-exam-detail-title">辞典提要</h3>
            <div class="dict-loading">加载中…</div>
        </section>
    `;
    try {
        const payload = await fetchJson(`${API}/api/dict/exam/xuci-detail?headword=${encodeURIComponent(headword)}`);
        state.examXuciDetailCache[headword] = payload;
        renderExamXuciDetailSupplement(headword, payload);
    } catch (error) {
        if (state.mode !== 'exam-xuci' || state.examSelected.xuci !== headword) return;
        mount.innerHTML = `
            <section class="dict-exam-detail-section">
                <h3 class="dict-exam-detail-title">辞典提要</h3>
                <div class="dict-empty-line">详情加载失败：${escHtml(error.message)}</div>
            </section>
        `;
    }
}

async function loadExamDataset(kind) {
    if (state.examData[kind]) {
        renderExamDataset(kind, state.examData[kind]);
        return;
    }
    const coverageEl = kind === 'xuci' ? el.examXuciCoverage : el.examShiciCoverage;
    const listEl = kind === 'xuci' ? el.examXuciList : el.examShiciList;
    const detailEl = kind === 'xuci' ? el.examXuciDetail : el.examShiciDetail;
    coverageEl.innerHTML = `<span class="dict-exam-chip">加载中…</span>`;
    listEl.innerHTML = `<div class="dict-loading">加载中…</div>`;
    detailEl.innerHTML = '';
    try {
        const payload = await fetchJson(`${API}/api/dict/exam/${kind}`);
        renderExamDataset(kind, payload);
    } catch (error) {
        coverageEl.innerHTML = `<span class="dict-exam-chip">加载失败</span>`;
        listEl.innerHTML = `<div class="dict-empty-line">${escHtml(error.message)}</div>`;
        detailEl.innerHTML = '';
    }
}

async function applyExamHashSelection(hash = window.location.hash) {
    const parsed = parseExamHash(hash);
    if (!parsed) return false;
    state.examSelected[parsed.kind] = parsed.headword;
    setMode(examKindToMode(parsed.kind), { syncHash: false });
    if (!state.examData[parsed.kind]) {
        await loadExamDataset(parsed.kind);
    } else {
        renderExamDataset(parsed.kind, state.examData[parsed.kind]);
    }
    return true;
}

function setMode(mode, { syncHash = true } = {}) {
    state.mode = mode;
    updateModeVisibility();
    if (mode === 'exam-xuci') {
        if (syncHash && state.examSelected.xuci) {
            syncExamHash('xuci', state.examSelected.xuci);
        }
        void loadExamDataset('xuci');
    } else if (mode === 'exam-shici') {
        if (syncHash && state.examSelected.shici) {
            syncExamHash('shici', state.examSelected.shici);
        }
        void loadExamDataset('shici');
    } else if (syncHash) {
        clearExamHash();
    }
}

function formatPageLabel(entry) {
    const bookBounds = getPageBounds(
        Array.isArray(entry.page_numbers) ? entry.page_numbers : [],
        entry.page_start,
        entry.page_end,
    );
    const pdfBounds = getPageBounds(
        Array.isArray(entry.pdf_page_numbers) ? entry.pdf_page_numbers : [],
    );
    const bookLabel = formatPageBounds(bookBounds, '书页');
    const pdfLabel = formatPageBounds(pdfBounds, 'PDF');
    if (bookLabel && pdfLabel && !samePageBounds(bookBounds, pdfBounds)) {
        return `${bookLabel}（${pdfLabel}）`;
    }
    return bookLabel || pdfLabel || '页码待补';
}

function getPageBounds(numbers, fallbackStart = null, fallbackEnd = null) {
    const cleanNumbers = (numbers || [])
        .map(value => Number(value))
        .filter(value => Number.isFinite(value) && value > 0);
    if (cleanNumbers.length) {
        return { start: cleanNumbers[0], end: cleanNumbers[cleanNumbers.length - 1] };
    }
    const start = Number(fallbackStart);
    if (!Number.isFinite(start) || start <= 0) return null;
    const end = Number(fallbackEnd);
    if (Number.isFinite(end) && end >= start) {
        return { start, end };
    }
    return { start, end: start };
}

function formatPageBounds(bounds, label) {
    if (!bounds) return '';
    if (bounds.start === bounds.end) {
        return `${label} ${bounds.start}`;
    }
    return `${label} ${bounds.start}-${bounds.end}`;
}

function samePageBounds(left, right) {
    if (!left || !right) return false;
    return left.start === right.start && left.end === right.end;
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

function buildMoeRevisedContext(entries) {
    return (entries || []).slice(0, 4).map(item => {
        const pronunciation = [item.bopomofo, item.pinyin].filter(Boolean).join(' · ');
        const meta = pronunciation ? ` · ${pronunciation}` : '';
        return `[教育部《重编国语辞典修订本》${meta}] ${item.headword}\n${String(item.content_text || '').slice(0, 420)}`;
    }).join('\n\n');
}

function buildMoeIdiomContext(entries) {
    return (entries || []).slice(0, 4).map(item => {
        const pronunciation = [item.bopomofo, item.pinyin].filter(Boolean).join(' · ');
        const meta = pronunciation ? ` · ${pronunciation}` : '';
        return `[教育部《成语典》${meta}] ${item.headword}\n${String(item.content_text || '').slice(0, 420)}`;
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
    const pdfPageNumbers = Array.isArray(entry.pdf_page_numbers) ? entry.pdf_page_numbers : [];
    return pageUrls.map((url, index) => ({
        page: pageNumbers[index] || (entry.page_start || 1) + index,
        pdfPage: pdfPageNumbers[index] || null,
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

function renderMoeRevisedEntries(payload) {
    const entries = Array.isArray(payload && payload.entries) ? payload.entries : [];
    const count = Number(payload && payload.entries ? entries.length : 0);
    state.moeEntries = entries;
    el.moeCount.textContent = String(count);

    const description = String(payload && payload.description || '').trim();
    const metaChips = [
        payload && payload.license ? `<span>${escHtml(String(payload.license))}</span>` : '',
        payload && payload.term_count ? `<span>${escHtml(String(payload.term_count))} 词条</span>` : '',
        payload && payload.built_at ? `<span>更新 ${escHtml(String(payload.built_at).slice(0, 10))}</span>` : '',
    ].filter(Boolean).join('');

    if (!entries.length) {
        const intro = description ? `
            <article class="dict-entry-card dict-moe-intro">
                <div class="dict-entry-source moe-revised">${escHtml(String(payload && payload.label || '教育部《重编国语辞典修订本》'))}</div>
                <div class="dict-entry-text dict-moe-intro-text">${escHtml(description)}</div>
                ${metaChips ? `<div class="dict-page-meta">${metaChips}</div>` : ''}
            </article>
        ` : '';
        const message = payload && payload.source_mode === 'unavailable'
            ? '教育部修订本本地结果区尚未导入。'
            : state.query.length > 1
                ? `「${state.query}」未命中教育部修订本字头，可改查关键单字或繁体写法。`
                : '该字暂未命中教育部修订本。';
        el.moeResults.innerHTML = `${intro}<div class="dict-empty-line">${escHtml(message)}</div>`;
        return;
    }

    const intro = `
        <article class="dict-entry-card dict-moe-intro">
            <div class="dict-entry-source moe-revised">${escHtml(String(payload && payload.label || '教育部《重编国语辞典修订本》'))}</div>
            ${description ? `<div class="dict-entry-text dict-moe-intro-text">${escHtml(description)}</div>` : ''}
            <div class="dict-page-meta">
                ${metaChips || '<span>只读结果区</span>'}
                <span>原文授权展示</span>
            </div>
        </article>
    `;

    el.moeResults.innerHTML = intro + entries.map(item => {
        const pronunciation = [item.bopomofo, item.pinyin].filter(Boolean).join(' · ');
        const matchLabel = item.match_mode === 'exact_headword'
            ? '字头精确命中'
            : item.match_mode === 'prefix_headword'
                ? '字头前缀命中'
                : '相关字头命中';
        return `
            <article class="dict-entry-card dict-moe-entry">
                <div class="dict-entry-source moe-revised">教育部修订本</div>
                <div class="dict-entry-top">
                    <span class="dict-entry-headword">${escHtml(item.headword || state.query)}</span>
                    ${pronunciation ? `<span class="dict-entry-pinyin">${escHtml(pronunciation)}</span>` : ''}
                    <span class="dict-verified is-soft">官方授权</span>
                </div>
                <div class="dict-page-meta">
                    <span>${escHtml(matchLabel)}</span>
                    <span>${escHtml(String(item.license || 'CC BY-ND 3.0 TW'))}</span>
                </div>
                <div class="dict-entry-text dict-moe-text">${formatReadOnlyText(item.content_text || '') || '<p>暂无可展示释义。</p>'}</div>
                <div class="dict-entry-actions">
                    <a class="dict-action-link" href="${escHtml(item.source_url || '#')}" target="_blank" rel="noopener noreferrer">打开教育部原站</a>
                </div>
            </article>
        `;
    }).join('');
}

function renderIdiomSections(sections) {
    if (!sections || typeof sections !== 'object') return '';
    const parts = [];
    if (sections.definition) {
        parts.push(`<div class="idiom-section"><h4 class="idiom-section-title">释义</h4><p>${escHtml(sections.definition)}</p></div>`);
    }
    if (sections.story) {
        parts.push(`<div class="idiom-section"><h4 class="idiom-section-title">典故说明</h4><p>${escHtml(sections.story)}</p></div>`);
    }
    if (sections.source_text) {
        const sourceLabel = sections.source_name ? escHtml(sections.source_name) : '典源';
        parts.push(`<div class="idiom-section"><h4 class="idiom-section-title">典源 · ${sourceLabel}</h4><p class="idiom-source-text">${escHtml(sections.source_text).replace(/\n/g, '<br>')}</p></div>`);
    }
    if (sections.source_notes) {
        parts.push(`<div class="idiom-section"><h4 class="idiom-section-title">注解</h4><p class="idiom-notes">${escHtml(sections.source_notes).replace(/\n/g, '<br>')}</p></div>`);
    }
    if (sections.usage_description) {
        parts.push(`<div class="idiom-section"><h4 class="idiom-section-title">语义</h4><p>${escHtml(sections.usage_description)}</p></div>`);
    }
    if (sections.usage_category) {
        parts.push(`<div class="idiom-section"><h4 class="idiom-section-title">使用类别</h4><p>${escHtml(sections.usage_category)}</p></div>`);
    }
    if (sections.usage_examples) {
        const examples = sections.usage_examples.split('\n').filter(Boolean);
        parts.push(`<div class="idiom-section"><h4 class="idiom-section-title">例句</h4><ul class="idiom-examples">${examples.map(e => `<li>${escHtml(e.trim())}</li>`).join('')}</ul></div>`);
    }
    if (sections.citations) {
        const cites = sections.citations.split('\n').filter(Boolean);
        parts.push(`<div class="idiom-section"><h4 class="idiom-section-title">书证</h4><ol class="idiom-citations">${cites.map(c => `<li>${escHtml(c.replace(/^\d+\.\s*/, '').trim())}</li>`).join('')}</ol></div>`);
    }
    if (sections.discrimination) {
        parts.push(`<div class="idiom-section"><h4 class="idiom-section-title">辨似</h4><p>${escHtml(sections.discrimination).replace(/\n/g, '<br>')}</p></div>`);
    }
    if (sections.synonyms || sections.antonyms) {
        const relParts = [];
        if (sections.synonyms) relParts.push(`<span class="idiom-rel"><b>近义：</b>${escHtml(sections.synonyms)}</span>`);
        if (sections.antonyms) relParts.push(`<span class="idiom-rel"><b>反义：</b>${escHtml(sections.antonyms)}</span>`);
        parts.push(`<div class="idiom-section idiom-rel-section">${relParts.join('')}</div>`);
    }
    if (sections.ref_words) {
        parts.push(`<div class="idiom-section"><h4 class="idiom-section-title">参考词语</h4><p>${escHtml(sections.ref_words)}</p></div>`);
    }
    return parts.join('');
}

function renderMoeIdiomEntries(payload) {
    const entries = Array.isArray(payload && payload.entries) ? payload.entries : [];
    const count = Number(payload && payload.entries ? entries.length : 0);
    state.idiomEntries = entries;
    el.idiomCount.textContent = String(count);

    if (!entries.length) {
        const description = String(payload && payload.description || '').trim();
        const metaChips = [
            payload && payload.license ? `<span>${escHtml(String(payload.license))}</span>` : '',
            payload && payload.term_count ? `<span>${escHtml(String(payload.term_count))} 成语</span>` : '',
            payload && payload.built_at ? `<span>更新 ${escHtml(String(payload.built_at).slice(0, 10))}</span>` : '',
        ].filter(Boolean).join('');
        const intro = description ? `
            <article class="dict-entry-card dict-moe-intro">
                <div class="dict-entry-source moe-idiom">${escHtml(String(payload && payload.label || '教育部《成语典》'))}</div>
                <div class="dict-entry-text dict-moe-intro-text">${escHtml(description)}</div>
                ${metaChips ? `<div class="dict-page-meta">${metaChips}</div>` : ''}
            </article>
        ` : '';
        const message = payload && payload.source_mode === 'unavailable'
            ? '教育部成语典本地结果区尚未导入。'
            : '该查询暂未命中教育部成语典。';
        el.idiomResults.innerHTML = `${intro}<div class="dict-empty-line">${escHtml(message)}</div>`;
        return;
    }

    el.idiomResults.innerHTML = entries.map(item => {
        const pronunciation = [item.bopomofo, item.pinyin].filter(Boolean).join(' · ');
        const matchLabel = item.match_mode === 'exact_headword'
            ? '精确命中'
            : item.match_mode === 'prefix_headword'
                ? '前缀命中'
                : '相关命中';
        const sectionsHtml = item.sections ? renderIdiomSections(item.sections) : '';
        const bodyHtml = sectionsHtml || formatReadOnlyText(item.content_text || '') || '<p>暂无可展示释义。</p>';
        return `
            <article class="dict-entry-card dict-moe-entry dict-idiom-entry">
                <div class="dict-entry-top">
                    <span class="dict-entry-headword">${escHtml(item.headword || state.query)}</span>
                    ${pronunciation ? `<span class="dict-entry-pinyin">${escHtml(pronunciation)}</span>` : ''}
                </div>
                <div class="dict-page-meta">
                    <span class="dict-entry-source moe-idiom">${escHtml(matchLabel)}</span>
                    <span>${escHtml(String(item.license || 'CC BY-ND 3.0 TW'))}</span>
                </div>
                <div class="dict-entry-text dict-idiom-body">${bodyHtml}</div>
                <div class="dict-entry-actions">
                    <a class="dict-action-link" href="${escHtml(item.source_url || '#')}" target="_blank" rel="noopener noreferrer">打开教育部成语典原站</a>
                </div>
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
    setMode('lookup');
}

function resetPanelsForSearch() {
    showResults();
    setLoading(el.tbResults);
    setLoading(el.dictEntries);
    setLoading(el.moeResults);
    setLoading(el.idiomResults);
    setLoading(el.gkResults);
    el.tbCount.textContent = '0';
    el.dictCount.textContent = '0';
    el.moeCount.textContent = '0';
    el.idiomCount.textContent = '0';
    el.gkCount.textContent = '0';
    state.chatHistory = [];
    state.dictEntries = [];
    state.moeEntries = [];
    state.idiomEntries = [];
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
                phase: PHASE,
            }),
        });
        if (queryKey !== state.queryKey || expectedQuery !== state.query) {
            return;
        }
        state.chatHistory.pop();
        if (silentUser) {
            state.chatHistory.push({ role: 'user', content: `请系统梳理「${state.query}」的教材、词典${PHASE === '高中' ? '与真题' : ''}要点。` });
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
    history.replaceState({}, '', `${PHASE === '初中' ? '/chuzhong-dict.html' : '/dict.html'}?q=${encodeURIComponent(query)}`);

    try {
        const fetches = [
            fetchJson(`${API}/api/dict/textbook?q=${encodeURIComponent(query)}&limit=30&phase=${encodeURIComponent(PHASE)}`),
            fetchJson(`${API}/api/dict/search?q=${encodeURIComponent(query)}&limit=20`),
            fetchJson(`${API}/api/dict/moe-revised?q=${encodeURIComponent(query)}&limit=6`),
            PHASE === '高中'
                ? fetchJson(`${API}/api/dict/gaokao?q=${encodeURIComponent(query)}&limit=20`)
                : Promise.resolve({ results: [] }),
            fetchJson(`${API}/api/dict/moe-idioms?q=${encodeURIComponent(query)}&limit=6`),
        ];
        const [textbookResult, dictResult, moeResult, gaokaoResult, idiomResult] = await Promise.allSettled(fetches);

        const textbookFailed = textbookResult.status === 'rejected';
        const dictFailed = dictResult.status === 'rejected';
        const moeFailed = moeResult.status === 'rejected';
        const gaokaoFailed = gaokaoResult.status === 'rejected';
        const idiomFailed = idiomResult.status === 'rejected';

        const textbook = textbookFailed ? { results: [] } : textbookResult.value;
        const dictPayload = dictFailed ? { entries: [], available: false, source_mode: 'unavailable' } : dictResult.value;
        const moePayload = moeFailed ? { entries: [], available: false, source_mode: 'unavailable' } : moeResult.value;
        const gaokao = gaokaoFailed ? { results: [] } : gaokaoResult.value;
        const idiomPayload = idiomFailed ? { entries: [], available: false, source_mode: 'unavailable' } : idiomResult.value;

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

        if (moeFailed) {
            el.moeCount.textContent = '0';
            setEmptyLine(el.moeResults, `教育部修订本检索失败：${moeResult.reason.message}`);
        } else {
            renderMoeRevisedEntries(moePayload);
        }

        if (gaokaoFailed) {
            el.gkCount.textContent = '0';
            setEmptyLine(el.gkResults, `真题检索失败：${gaokaoResult.reason.message}`);
        } else {
            renderGaokaoResults(gaokao.results || []);
        }

        if (idiomFailed) {
            el.idiomCount.textContent = '0';
            setEmptyLine(el.idiomResults, `成语典检索失败：${idiomResult.reason.message}`);
        } else {
            renderMoeIdiomEntries(idiomPayload);
        }

        state.dictContext = [buildDictContext(dictPayload.entries || []), buildMoeRevisedContext(moePayload.entries || []), buildMoeIdiomContext(idiomPayload.entries || [])]
            .filter(Boolean)
            .join('\n\n');
        state.textbookContext = buildTextbookContext(textbook.results || []);
        state.gaokaoContext = buildGaokaoContext(gaokao.results || []);

        el.chatInput.placeholder = `继续追问「${query}」…`;
        const evidenceReady = !textbookFailed || !dictFailed || !gaokaoFailed;
        state.searchInFlight = false;
        if (evidenceReady) {
            setChatPending(false);
            void sendChatMessage(`请分析「${query}」这个字或词：先抓教材中的核心义项，再对应${PHASE === '高中' ? '真题拿分点' : '考点'}。`, { silentUser: true, queryKey, expectedQuery: query });
        } else {
            setChatPending(true);
        }
    } catch (error) {
        if (queryKey !== state.queryKey || query !== state.query) {
            return;
        }
        setEmptyLine(el.tbResults, `教材检索失败：${error.message}`);
        setEmptyLine(el.dictEntries, `词典检索失败：${error.message}`);
        setEmptyLine(el.moeResults, `教育部修订本检索失败：${error.message}`);
        setEmptyLine(el.idiomResults, `成语典检索失败：${error.message}`);
        setEmptyLine(el.gkResults, `真题检索失败：${error.message}`);
        setChatPending(true);
    } finally {
        if (queryKey === state.queryKey && query === state.query) {
            state.searchInFlight = false;
        }
    }
}

function openModalWithPages(pages, title = '') {
    if (!pages.length) return;
    state.modalPages = pages;
    state.modalIndex = 0;
    state.modalTitle = title || '';
    renderModal();
    el.modal.classList.remove('hidden');
    el.modal.setAttribute('aria-hidden', 'false');
}

function renderModal() {
    const current = state.modalPages[state.modalIndex];
    if (!current) return;
    el.modalImage.src = current.url;
    el.modalTitle.textContent = state.modalTitle || '原文页图';
    el.modalMeta.textContent = current.pdfPage && current.pdfPage !== current.page
        ? `书页 ${current.page}（PDF ${current.pdfPage}）`
        : `第 ${current.page} 页`;
    el.modalPrev.disabled = state.modalIndex === 0;
    el.modalNext.disabled = state.modalIndex >= state.modalPages.length - 1;
}

function closeModal() {
    el.modal.classList.add('hidden');
    el.modal.setAttribute('aria-hidden', 'true');
    state.modalPages = [];
    state.modalIndex = 0;
    state.modalTitle = '';
    el.modalTitle.textContent = '原文页图';
    el.modalImage.src = '';
}

async function openBookModal(bookKey, page) {
    try {
        const data = await fetchJson(`${API}/api/page-image?book_key=${encodeURIComponent(bookKey)}&page=${page}&context=2`);
        openModalWithPages(
            (data.pages || []).map(item => ({ page: item.page, pdfPage: null, url: item.url })),
            data.title || '教材原文',
        );
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
            .map(item => ({ page: item.page, pdfPage: item.pdf_page || null, url: item.url }));
        openModalWithPages(
            relevant.length
                ? relevant
                : (data.pages || []).map(item => ({ page: item.page, pdfPage: item.pdf_page || null, url: item.url })),
            data.dict_label || data.title || '馆藏原页',
        );
    } catch (error) {
        window.alert(`无法加载词典页图：${error.message}`);
    }
}

el.searchBtn.addEventListener('click', () => runSearch(el.input.value));
el.input.addEventListener('keydown', event => {
    if (event.key === 'Enter') runSearch(el.input.value);
});
el.modeButtons.forEach(button => {
    button.addEventListener('click', () => {
        const mode = button.dataset.dictMode || 'lookup';
        setMode(mode);
    });
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
window.addEventListener('hashchange', () => {
    const parsed = parseExamHash();
    if (parsed) {
        void applyExamHashSelection();
        return;
    }
    if (state.mode === 'exam-xuci' || state.mode === 'exam-shici') {
        setMode('lookup', { syncHash: false });
    }
});

// ── Scroll affordance ─────────────────────────────────────
// Long entry lists in 教材古文/古诗词, 馆藏辞典原页, 教育部修订本/成语典
// and 真题中的古文 are scrollable but the cut is not obvious to readers.
// We attach a fade gradient + animated "向下滑动" pill (driven by CSS)
// to the panel as soon as scrollHeight exceeds clientHeight, and clear
// it once the user scrolls to the bottom.
const SCROLL_BODY_SELECTOR = '.dict-panel-body, .dict-panel-body-wide';
const SCROLL_BOTTOM_SLACK = 6;

function ensureScrollHintEl(panel) {
    let hint = panel.querySelector(':scope > .dict-scroll-hint');
    if (!hint) {
        hint = document.createElement('div');
        hint.className = 'dict-scroll-hint';
        hint.setAttribute('aria-hidden', 'true');
        hint.textContent = '向下滑动查看更多 ↓';
        panel.appendChild(hint);
    }
    return hint;
}

function refreshPanelOverflowState(body) {
    const panel = body.closest('.dict-panel');
    if (!panel) return;
    const overflowing = body.scrollHeight - body.clientHeight > SCROLL_BOTTOM_SLACK;
    panel.classList.toggle('has-overflow', overflowing);
    if (!overflowing) {
        panel.classList.remove('scrolled-bottom');
        const existing = panel.querySelector(':scope > .dict-scroll-hint');
        if (existing) existing.remove();
        return;
    }
    ensureScrollHintEl(panel);
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight <= SCROLL_BOTTOM_SLACK;
    panel.classList.toggle('scrolled-bottom', atBottom);
}

function setupScrollAffordances(root = document) {
    const bodies = root.querySelectorAll(SCROLL_BODY_SELECTOR);
    bodies.forEach(body => {
        if (body.dataset.scrollAffordance === '1') {
            refreshPanelOverflowState(body);
            return;
        }
        body.dataset.scrollAffordance = '1';
        body.addEventListener('scroll', () => refreshPanelOverflowState(body), { passive: true });
        const observer = new MutationObserver(() => refreshPanelOverflowState(body));
        observer.observe(body, { childList: true, subtree: true, characterData: true });
        refreshPanelOverflowState(body);
    });
}

window.addEventListener('resize', () => {
    document.querySelectorAll(SCROLL_BODY_SELECTOR).forEach(refreshPanelOverflowState);
}, { passive: true });

async function initializeDictPage() {
    setupScrollAffordances();
    loadStatus();
    const hashApplied = await applyExamHashSelection();
    if (hashApplied) return;
    if (q) {
        runSearch(q);
    } else {
        updateModeVisibility();
    }
}

void initializeDictPage();
