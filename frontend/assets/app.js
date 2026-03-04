/* ── Cross-Subject Knowledge Platform · Frontend ── */

const API = '';  // same origin
const AI_API = 'https://ai.bdfz.net/';
const IMG_CDN = 'https://img.rdfzer.com';

// ── AI Synthesis ──────────────────────────────────────────
const aiPanel = document.getElementById('ai-panel');
const aiBtn = document.getElementById('ai-btn');
const aiResult = document.getElementById('ai-result');
const aiContent = document.getElementById('ai-content');

aiBtn.addEventListener('click', () => requestAISynthesis());

async function requestAISynthesis() {
    if (!currentData || !currentQuery) return;
    const groups = currentData.groups || [];
    if (groups.length < 2) return;

    // Build context from search results (top 3 per subject)
    const context = groups.map(g => {
        const snippets = g.results.slice(0, 3).map(r =>
            `[${g.subject}·${r.title}·§${r.section}] ${r.text.slice(0, 300)}`
        ).join('\n');
        return `【${g.subject}】（${g.count}条）\n${snippets}`;
    }).join('\n\n');

    const prompt = `你是一位资深跨学科教育专家。用户搜索了「${currentQuery}」，以下是来自高中不同学科教材的相关内容：

${context}

请完成以下任务：
1. 用 200-300 字综合解释「${currentQuery}」这个概念如何在这些学科中体现，重点挖掘学生不容易发现但应该看到的跨学科联系。
2. 说明不同学科对同一概念的不同视角如何互补。
3. 在解释中标注出处，格式：[学科·书名]。
4. 最后给出一个「学习建议」，帮助学生建立跨学科思维。

要求：语言简洁有力，面向高中生，避免重复原文。`;

    // Show loading
    aiBtn.disabled = true;
    aiBtn.classList.add('loading');
    aiBtn.querySelector('.ai-sparkle').textContent = '⏳';
    aiResult.classList.add('hidden');

    try {
        const res = await fetch(AI_API, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt }),
        });
        const data = await res.json();

        if (data.answer) {
            aiContent.innerHTML = escHtml(data.answer) +
                `<span class="ai-source">📡 由 Gemini 生成 · 数据来源：${groups.length} 个学科的教材原文</span>`;
            aiResult.classList.remove('hidden');
        } else if (data.error) {
            aiContent.textContent = `AI 服务暂时不可用: ${data.error}`;
            aiResult.classList.remove('hidden');
        }
    } catch (e) {
        aiContent.textContent = `请求失败: ${e.message}`;
        aiResult.classList.remove('hidden');
    } finally {
        aiBtn.disabled = false;
        aiBtn.classList.remove('loading');
        aiBtn.querySelector('.ai-sparkle').textContent = '✨';
    }
}

// ── Navigation ────────────────────────────────────────────
document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
        btn.classList.add('active');
        const view = document.getElementById('view-' + btn.dataset.view);
        view.classList.add('active');
        if (btn.dataset.view === 'graph') loadGraph();
        if (btn.dataset.view === 'gaokao') initGaokao();
    });
});

// ── Advanced Search Panel ─────────────────────────────────
const advToggle = document.getElementById('advanced-toggle');
const advPanel = document.getElementById('advanced-panel');
const filterBook = document.getElementById('filter-book');
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
        filterBook.innerHTML = '<option value="">全部教材</option>';
        subjects.forEach(subj => {
            const optgroup = document.createElement('optgroup');
            optgroup.label = `${subj.icon} ${subj.subject}`;
            subj.books.forEach(b => {
                const opt = document.createElement('option');
                opt.value = b.book_key;
                opt.textContent = b.title;
                optgroup.appendChild(opt);
            });
            filterBook.appendChild(optgroup);
        });
        booksLoaded = true;
    } catch (e) { /* silent */ }
}

// Load books on page load
loadBooks();

// Re-search when filters change
filterSort.addEventListener('change', () => { if (currentQuery) doSearch(currentQuery); });
filterBook.addEventListener('change', () => { if (currentQuery) doSearch(currentQuery); });
filterImages.addEventListener('change', () => { if (currentQuery) doSearch(currentQuery); });

// ── Search ────────────────────────────────────────────────
const searchInput = document.getElementById('search-input');
const searchBtn = document.getElementById('search-btn');
const resultsEl = document.getElementById('results');
const crossHintEl = document.getElementById('cross-hint');
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
        const recentGroup = document.getElementById('trending-recent');
        const popularTags = document.getElementById('trending-popular-tags');
        const recentTags = document.getElementById('trending-recent-tags');

        let hasContent = false;

        // Popular searches
        if (data.popular && data.popular.length > 0) {
            popularTags.innerHTML = '';
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
            hasContent = true;
        }

        // Recent searches
        if (data.recent && data.recent.length > 0) {
            recentTags.innerHTML = '';
            data.recent.slice(0, 8).forEach(item => {
                const btn = document.createElement('button');
                btn.className = 'trending-tag recent';
                btn.textContent = item.query;
                btn.addEventListener('click', () => {
                    searchInput.value = item.query;
                    doSearch(item.query);
                });
                recentTags.appendChild(btn);
            });
            recentGroup.classList.remove('hidden');
            hasContent = true;
        }

        if (hasContent) section.classList.remove('hidden');
    } catch (_) { /* silent */ }
}

// Load trending on page init
loadTrending();

let currentQuery = '';
let currentData = null;

async function doSearch(q) {
    q = q.trim();
    if (!q) return;
    currentQuery = q;

    resultsEl.innerHTML = '<div class="loading">搜索中…</div>';
    crossHintEl.classList.add('hidden');
    subjectTabsEl.classList.add('hidden');
    relatedBarEl.classList.add('hidden');

    // Build search URL with advanced filters
    const params = new URLSearchParams({ q, limit: 100 });

    const bookKey = filterBook.value;
    if (bookKey) params.set('book_key', bookKey);

    const sort = filterSort.value;
    if (sort && sort !== 'relevance') params.set('sort', sort);

    if (filterImages.checked) params.set('has_images', 'true');

    try {
        const res = await fetch(`${API}/api/search?${params}`);
        if (!res.ok) throw new Error('Search failed');
        currentData = await res.json();
        renderResults(currentData);
        // Load related concepts
        loadRelated(q);
        // Refresh trending after search (new query logged)
        setTimeout(() => loadTrending(), 500);
    } catch (e) {
        resultsEl.innerHTML = `<div class="loading">搜索出错: ${e.message}</div>`;
    }
}

// ── Related Concepts ──────────────────────────────────────
async function loadRelated(q) {
    try {
        const res = await fetch(`${API}/api/related?q=${encodeURIComponent(q)}&limit=10`);
        const data = await res.json();
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

function renderResults(data, filterSubject = null) {
    // Cross hint
    if (data.cross_hint && !filterSubject) {
        crossHintEl.textContent = data.cross_hint;
        crossHintEl.classList.remove('hidden');
    } else {
        crossHintEl.classList.add('hidden');
    }

    // AI panel: show only when 2+ subjects found
    const subjectCount = Object.keys(data.subject_counts || {}).length;
    if (subjectCount >= 2 && !filterSubject) {
        aiPanel.classList.remove('hidden');
        aiResult.classList.add('hidden'); // reset
    } else {
        aiPanel.classList.add('hidden');
    }

    // Subject tabs
    const counts = data.subject_counts || {};
    const subjects = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    if (subjects.length > 1) {
        subjectTabsEl.innerHTML = `
            <div class="subject-tab ${!filterSubject ? 'active' : ''}" data-subj="">
                全部 <span class="tab-count">${data.total}</span>
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
                    renderResults(currentData, s);
                } else {
                    renderResults(currentData);
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
                        ${r.image_count > 0 ? `<span class="img-badge">📷 ${r.image_count}</span>` : ''}
                    </div>
                    <div class="result-snippet">${sanitizeSnippet(r.snippet)}</div>
                    <div class="result-text">${renderText(r.text, r.book_key)}</div>
                </div>
            `).join('')}
        </div>
    `).join('');

    // Trigger Math rendering for search results
    if (typeof renderMath === 'function') {
        renderMath(resultsEl);
    }
}

function getSubjectIcon(s) {
    const map = { '语文': '📖', '数学': '📐', '英语': '🌍', '物理': '⚛️', '化学': '🧪', '生物学': '🧬', '历史': '📜', '地理': '🗺️', '思想政治': '⚖️' };
    return map[s] || '📚';
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

// Clean raw text for expanded view
function cleanText(s) {
    if (!s) return '';
    s = s.replace(/<[^>]+>/g, ' ');
    s = s.replace(/!\[.*?\]\(.*?\)/g, '[图片]');
    s = s.replace(/\s+/g, ' ').trim();
    return s;
}

// Render text with images for expanded view
function renderText(text, bookKey) {
    if (!text) return '';
    // Strip HTML tags except preserve content
    let s = text.replace(/<[^>]+>/g, ' ');
    // Convert markdown images to <img> tags — use R2 CDN
    s = s.replace(/!\[([^\]]*)\]\(images\/([^)]+)\)/g, (_, alt, src) => {
        return `<img class="result-img" src="${IMG_CDN}/orig/${encodeURIComponent(bookKey)}/${src}" alt="${alt || '教材图片'}" loading="lazy">`;
    });
    // Escape remaining HTML-like content (but preserve our img tags)
    const parts = s.split(/(<img[^>]+>)/g);
    s = parts.map(p => p.startsWith('<img') ? p : p.replace(/</g, '&lt;').replace(/>/g, '&gt;')).join('');
    return s;
}

// ── Knowledge Graph ───────────────────────────────────────
let graphLoaded = false;

async function loadGraph() {
    if (graphLoaded) return;
    const container = document.getElementById('graph-container');
    container.innerHTML = '<div class="loading" style="padding-top:40vh">加载知识图谱…</div>';

    try {
        const res = await fetch(`${API}/api/cross-links`);
        const data = await res.json();
        renderGraph(data, container);
        graphLoaded = true;
    } catch (e) {
        container.innerHTML = `<div class="loading">加载失败: ${e.message}</div>`;
    }
}

function renderGraph(data, container) {
    const W = container.clientWidth;
    const H = container.clientHeight;

    // Build nodes and links
    const subjectNodes = data.subject_nodes.map(s => ({
        ...s, type: 'subject', r: 30, fx: null, fy: null,
    }));

    const conceptNodes = data.concept_nodes.map(c => ({
        ...c, type: 'concept', r: Math.max(6, Math.min(20, c.subjects * 4)),
    }));

    const allNodes = [...subjectNodes, ...conceptNodes];
    const nodeById = {};
    allNodes.forEach(n => nodeById[n.id] = n);

    // Aggregate links between subjects
    const linkMap = {};
    data.links.forEach(l => {
        const key = [l.source, l.target].sort().join('|');
        if (!linkMap[key]) linkMap[key] = { source: l.source, target: l.target, concepts: [], weight: 0 };
        linkMap[key].concepts.push(l.concept);
        linkMap[key].weight += l.weight;
    });
    const links = Object.values(linkMap).filter(l => nodeById[l.source] && nodeById[l.target]);

    // Simple force simulation (no D3 dependency for MVP)
    // Place subject nodes in a circle
    const cx = W / 2, cy = H / 2, radius = Math.min(W, H) * 0.32;
    subjectNodes.forEach((n, i) => {
        const angle = (i / subjectNodes.length) * Math.PI * 2 - Math.PI / 2;
        n.x = cx + Math.cos(angle) * radius;
        n.y = cy + Math.sin(angle) * radius;
    });

    // Place concept nodes near related subjects
    conceptNodes.forEach(n => {
        const relatedLinks = data.links.filter(l => l.concept === n.id);
        if (relatedLinks.length > 0) {
            const subjs = [...new Set(relatedLinks.flatMap(l => [l.source, l.target]))];
            let sx = 0, sy = 0, count = 0;
            subjs.forEach(s => {
                const sn = nodeById[s];
                if (sn) { sx += sn.x; sy += sn.y; count++; }
            });
            if (count > 0) {
                n.x = sx / count + (Math.random() - 0.5) * 80;
                n.y = sy / count + (Math.random() - 0.5) * 80;
            } else {
                n.x = cx + (Math.random() - 0.5) * radius;
                n.y = cy + (Math.random() - 0.5) * radius;
            }
        } else {
            n.x = cx + (Math.random() - 0.5) * radius * 1.5;
            n.y = cy + (Math.random() - 0.5) * radius * 1.5;
        }
    });

    // Render SVG
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

    // Links
    links.forEach(l => {
        const s = nodeById[l.source], t = nodeById[l.target];
        if (!s || !t) return;
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', s.x); line.setAttribute('y1', s.y);
        line.setAttribute('x2', t.x); line.setAttribute('y2', t.y);
        line.setAttribute('stroke', 'rgba(108,92,231,0.15)');
        line.setAttribute('stroke-width', Math.max(1, Math.min(4, l.weight / 10)));
        svg.appendChild(line);
    });

    // Concept-to-subject links
    conceptNodes.forEach(n => {
        const relatedLinks = data.links.filter(l => l.concept === n.id);
        const subjs = [...new Set(relatedLinks.flatMap(l => [l.source, l.target]))];
        subjs.forEach(s => {
            const sn = nodeById[s];
            if (!sn) return;
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', n.x); line.setAttribute('y1', n.y);
            line.setAttribute('x2', sn.x); line.setAttribute('y2', sn.y);
            line.setAttribute('stroke', sn.color || '#555');
            line.setAttribute('stroke-opacity', '0.2');
            line.setAttribute('stroke-width', '1');
            svg.appendChild(line);
        });
    });

    // Nodes
    allNodes.forEach(n => {
        const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        g.setAttribute('class', 'graph-node');

        const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        circle.setAttribute('cx', n.x); circle.setAttribute('cy', n.y);
        circle.setAttribute('r', n.r);
        circle.setAttribute('fill', n.type === 'subject' ? (n.color || '#6c5ce7') : 'rgba(108,92,231,0.5)');
        circle.setAttribute('stroke', n.type === 'subject' ? 'rgba(255,255,255,0.3)' : 'none');
        circle.setAttribute('stroke-width', '2');
        g.appendChild(circle);

        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('x', n.x); text.setAttribute('y', n.y + n.r + 14);
        text.setAttribute('class', 'graph-label');
        text.setAttribute('font-size', n.type === 'subject' ? '14' : '11');
        text.setAttribute('font-weight', n.type === 'subject' ? '600' : '400');
        text.textContent = n.type === 'subject' ? `${n.icon} ${n.id}` : n.id;
        g.appendChild(text);

        // Click to search
        g.addEventListener('click', () => {
            const q = n.type === 'concept' ? n.id : '';
            if (q) {
                document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
                document.querySelector('[data-view="search"]').classList.add('active');
                document.getElementById('view-search').classList.add('active');
                searchInput.value = q;
                doSearch(q);
            }
        });

        svg.appendChild(g);
    });

    container.innerHTML = '';
    container.appendChild(svg);
}

// ── Init: Load stats ──────────────────────────────────────
(async function init() {
    try {
        const res = await fetch(`${API}/api/stats`);
        const data = await res.json();
        const el = document.getElementById('stats-bar');
        el.innerHTML = data.subjects.map(s =>
            `<div class="stat-chip">${s.icon} ${s.name} <span class="count">${s.count.toLocaleString()}</span></div>`
        ).join('');
        el.classList.remove('hidden');
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
            const crossIcon = c.is_cross ? '🌐' : '📘';
            const subjs = (c.subjects || []).join('·');
            return `<span class="matched-concept ${c.is_cross ? 'cross' : 'same'}" title="${subjs}">${crossIcon} ${escHtml(c.concept)}</span>`;
        }).join('');

        const expandedInfo = (data.expanded_terms || []).length > 0
            ? `<div class="gk-expanded-terms">🔮 隐性扩展：${data.expanded_terms.map(t => `<span class="expanded-term">${escHtml(t)}</span>`).join(' ')}</div>`
            : '';

        content.innerHTML = `
            <div class="gk-link-question">
                <strong>${escHtml(data.question_title)}</strong>
                ${conceptOverview ? `<div class="gk-matched-concepts">知识点匹配：${conceptOverview}</div>` : ''}
                <span class="gk-link-terms">搜索关键词：${data.search_terms.map(t => `<span class="term-tag">${escHtml(t)}</span>`).join(' ')}</span>
                ${expandedInfo}
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
        const linkData = await linkRes.json();

        // Build context
        const textbookContext = (linkData.links || []).map(l =>
            `[【${l.subject}・${l.title}・§${l.section}】] ${(l.text || '').slice(0, 400)}`
        ).join('\n\n');

        // Get question text from the card
        const card = document.querySelector(`.gaokao-card[data-id="${questionId}"]`);
        const questionText = card ? card.querySelector('.gk-question')?.textContent?.slice(0, 600) : '';

        const prompt = `你是一位高考命题研究专家。请分析以下高考真题与教材内容之间的关系。

【真题】
${questionText}
年份: ${linkData.question_subject || ''}

【相关教材内容】
${textbookContext || '（未找到直接相关教材）'}

请分析：
1. 这道题直接考查了哪些教材知识点（显性关联）
2. 解题还需要哪些容易忽视的知识（隐性关联）
3. 相同知识点在不同学科教材中的表述差异

要求：200字以内，语言简洁，面向高中生。`;

        const aiRes = await fetch(AI_API, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt }),
        });
        const aiData = await aiRes.json();

        if (aiData.answer) {
            resultEl.innerHTML = `
                <div class="gk-ai-answer">
                    <div class="gk-ai-header">✨ AI 关联分析 <span class="ai-model">Gemini</span></div>
                    <div class="gk-ai-text">${escHtml(aiData.answer)}</div>
                </div>
            `;
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
            errorColor: '#e74c3c'
        });
    }
}
