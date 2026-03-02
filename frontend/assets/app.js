/* ── Cross-Subject Knowledge Platform · Frontend ── */

const API = '';  // same origin
const AI_API = 'https://ai.bdfz.net/';

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
    });
});

// ── Search ────────────────────────────────────────────────
const searchInput = document.getElementById('search-input');
const searchBtn = document.getElementById('search-btn');
const resultsEl = document.getElementById('results');
const crossHintEl = document.getElementById('cross-hint');
const subjectTabsEl = document.getElementById('subject-tabs');

searchBtn.addEventListener('click', () => doSearch(searchInput.value));
searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(searchInput.value); });
document.querySelectorAll('.quick-tag').forEach(tag => {
    tag.addEventListener('click', () => {
        searchInput.value = tag.dataset.q;
        doSearch(tag.dataset.q);
    });
});

let currentQuery = '';
let currentData = null;

async function doSearch(q) {
    q = q.trim();
    if (!q) return;
    currentQuery = q;

    resultsEl.innerHTML = '<div class="loading">搜索中…</div>';
    crossHintEl.classList.add('hidden');
    subjectTabsEl.classList.add('hidden');

    try {
        const res = await fetch(`${API}/api/search?q=${encodeURIComponent(q)}&limit=100`);
        if (!res.ok) throw new Error('Search failed');
        currentData = await res.json();
        renderResults(currentData);
    } catch (e) {
        resultsEl.innerHTML = `<div class="loading">搜索出错: ${e.message}</div>`;
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
                    // Client-side filter
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
                    <div class="result-title">${escHtml(r.title)} · §${r.section}</div>
                    <div class="result-snippet">${r.snippet}</div>
                    <div class="result-text">${escHtml(r.text)}</div>
                </div>
            `).join('')}
        </div>
    `).join('');
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
