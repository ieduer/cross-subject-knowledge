/* ── SUN library tabs: 「PDF 预览」 + 「教材下载」 ──────────────────────────
 * Loaded after app.js on index.html / chuzhong.html.  Plain script, no build
 * step, no inline handlers, no eval (CSP-compatible).  Talks only to
 * /api/library/*, /files/pdf/* and the existing /api/search (in-book search).
 * Never calls any AI endpoint.
 *
 * Page numbers: this file shows 「PDF 第 N/M 页」 everywhere.  A printed page
 * (「书页 p.X · 已核对」) is shown only for rows the catalog marks
 * printed_verified.  No offset is ever computed here.
 */
(function () {
    'use strict';

    const viewPdf = document.getElementById('view-pdf');
    const viewDownloads = document.getElementById('view-downloads');
    if (!viewPdf || !viewDownloads) return;

    const PAGE_PHASE = document.body.dataset.phase || '高中';
    const REGION = 'CN'; // carried through API and state; no selector while only CN exists
    const PDFJS_VERSION = '4.10.38';
    const PDFJS_BASE = '/vendor/pdfjs/';
    const GRID_PAGE_SIZE = 60;
    const DOWNLOAD_PAGE_SIZE = 30;
    const RESOLVE_BATCH = 40;
    const ROW_SPAN = 200;
    // Canvas backing-store cap (total pixels, whatever the zoom or device pixel ratio):
    // 5,000,000 px (about 20 MB RGBA) on phones and low-memory devices, 8,000,000 px
    // (about 32 MB) elsewhere.  The ratio is reduced to fit; the CSS size is unchanged.
    const MAX_CANVAS_PIXELS_SMALL = 5000000;
    const MAX_CANVAS_PIXELS = 8000000;
    // Same-origin downloads: only files of known size up to 25 MB are fetched in the page
    // (progress + cancel, one at a time).  Anything larger or of unknown size is a plain
    // browser download of the ?download=1 URL: nothing is buffered in JavaScript.
    const INLINE_DOWNLOAD_MAX_BYTES = 25 * 1024 * 1024;
    const COVER_CONCURRENCY = 3;
    const COVER_TIMEOUT_MS = 20000;
    const OTHER_PRINTING = 'page-aligned-other-printing';
    const OTHER_PRINTING_NOTE = '检索文字来自较早印次，可能与此 PDF 的文字略有出入，以 PDF 为准';
    const RANGE_CHUNK = 65536;   // PDF.js default; a flat page tree costs about one chunk per page on open
    const OTHER_VIEWS = ['gaokao', 'insights', 'graph'];

    // ── tiny DOM helpers ────────────────────────────────────────────────────
    function el(tag, attrs, children) {
        const node = document.createElement(tag);
        if (attrs) {
            for (const key of Object.keys(attrs)) {
                const value = attrs[key];
                if (value === null || value === undefined || value === false) continue;
                if (key === 'class') node.className = value;
                else if (key === 'text') node.textContent = value;
                else if (key === 'dataset') Object.assign(node.dataset, value);
                else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
                else node.setAttribute(key, value === true ? '' : String(value));
            }
        }
        for (const child of [].concat(children || [])) {
            if (child === null || child === undefined || child === false) continue;
            node.append(child.nodeType ? child : document.createTextNode(String(child)));
        }
        return node;
    }

    // Defence in depth: the server already allow-lists hosts; never put a non-https URL in an href.
    function externalUrl(url) {
        return typeof url === 'string' && /^https:\/\/[^\s"'<>]+$/.test(url) ? url : null;
    }

    function monthOf(stamp) {
        const match = /^(\d{4})-(\d{2})/.exec(String(stamp || ''));
        return match ? `${match[1]}-${match[2]}` : '';
    }

    function dayOf(stamp) {
        const match = /^(\d{4}-\d{2}-\d{2})/.exec(String(stamp || ''));
        return match ? match[1] : '';
    }

    // Which file of a book this is, in words.  Two held files of one work are different
    // versions; they are told apart by "is it the platform's current file" and by when the
    // file was obtained.
    function roleText(platformCurrent, acquiredAt) {
        if (platformCurrent === true) return '平台现行文件';
        if (platformCurrent === false) {
            const day = dayOf(acquiredAt);
            return '较早取得' + (day ? `（取得于 ${day}）` : '');
        }
        return '';
    }

    function canvasBudget() {
        const small = Math.min(window.screen.width || 9999, window.screen.height || 9999) <= 820
            || (navigator.deviceMemory && navigator.deviceMemory <= 4);
        return small ? MAX_CANVAS_PIXELS_SMALL : MAX_CANVAS_PIXELS;
    }

    // Give canvas memory back at once (older iOS Safari reclaims dropped canvases slowly).
    function releaseCanvases(container) {
        container.querySelectorAll('canvas').forEach(canvas => { canvas.width = 0; canvas.height = 0; });
    }

    function clear(node) {
        while (node.firstChild) node.removeChild(node.firstChild);
    }

    function formatBytes(bytes) {
        if (!Number.isFinite(bytes) || bytes <= 0) return '';
        if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + ' KB';
        if (bytes < 1024 * 1024 * 1024) return (bytes / 1048576).toFixed(bytes < 10485760 ? 1 : 0) + ' MB';
        return (bytes / 1073741824).toFixed(2) + ' GB';
    }

    function debounce(fn, ms) {
        let timer = 0;
        return function () {
            const args = arguments;
            clearTimeout(timer);
            timer = setTimeout(() => fn.apply(null, args), ms);
        };
    }

    // Upstream snippets contain <mark>…</mark>.  Everything else is dropped and
    // the result is built from text nodes + <mark> elements only (no innerHTML).
    function snippetNodes(html, limit) {
        let text = String(html == null ? '' : html)
            .replace(/<mark>/gi, '').replace(/<\/mark>/gi, '')
            .replace(/<[^>]*>/g, ' ').replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
            .replace(/\s+/g, ' ').trim();
        if (text.length > limit) text = text.slice(0, limit) + '…';
        const out = [];
        let open = false;
        for (const part of text.split(/([])/)) {
            if (part === '') open = true;
            else if (part === '') open = false;
            else if (part) out.push(open ? el('mark', { text: part }) : document.createTextNode(part));
        }
        return out;
    }

    function queryTerms(query) {
        const seen = new Set();
        return String(query || '').split(/\s+/).map(t => t.trim().toLowerCase())
            .filter(t => t && !seen.has(t) && seen.add(t)).slice(0, 8);
    }

    // ── API ─────────────────────────────────────────────────────────────────
    function LibError(kind, message, retryAfter) {
        this.kind = kind;
        this.message = message;
        this.retryAfter = retryAfter || 0;
    }

    function statusError(status, retryAfterHeader, what) {
        const retry = Math.min(600, Math.max(1, parseInt(retryAfterHeader, 10) || 5));
        if (status === 429) return new LibError('rate', `请求过于频繁，请 ${retry} 秒后重试。`, retry);
        if (status === 503) return new LibError('unavailable', '教材目录尚未发布或暂不可用。');
        if (status === 404) return new LibError('notfound', what === 'file' ? '文件缺失：本站没有找到这册 PDF。' : '没有找到对应的教材版次。');
        if (status === 400) return new LibError('bad', '请求参数不被接受。');
        return new LibError('server', `服务器返回错误（${status}）。`);
    }

    async function api(path, params, signal) {
        const query = new URLSearchParams();
        if (params) {
            for (const key of Object.keys(params)) {
                const value = params[key];
                if (value !== null && value !== undefined && value !== '') query.set(key, value);
            }
        }
        const text = query.toString();
        let response;
        try {
            response = await fetch(path + (text ? '?' + text : ''), {
                signal, credentials: 'omit', headers: { Accept: 'application/json' },
            });
        } catch (error) {
            if (error && error.name === 'AbortError') throw new LibError('aborted', '已取消');
            throw new LibError('network', '网络连接失败，请检查网络后重试。');
        }
        if (!response.ok) throw statusError(response.status, response.headers.get('Retry-After'));
        try {
            return await response.json();
        } catch (_) {
            throw new LibError('server', '服务器返回了无法解析的内容。');
        }
    }

    function notice(kind, message, actions) {
        return el('div', { class: 'lib-notice lib-notice-' + kind, role: kind === 'error' ? 'alert' : 'status' }, [
            el('span', { class: 'lib-notice-text', text: message }),
        ].concat(actions || []));
    }

    function errorNotice(error, retry) {
        if (error && error.kind === 'aborted') return null;
        const message = (error && error.message) || '发生未知错误。';
        return notice('error', message, retry ? [el('button', { class: 'lib-btn', type: 'button', text: '重试', onclick: retry })] : null);
    }

    let releaseCache = null;
    let releaseCacheAt = 0;
    async function getRelease(signal) {
        if (releaseCache && Date.now() - releaseCacheAt < 60000) return releaseCache;
        const data = await api('/api/library/release', null, signal);
        releaseCache = data;
        releaseCacheAt = Date.now();
        return data;
    }

    // ── URL state ───────────────────────────────────────────────────────────
    function setUrl(params, push) {
        const query = new URLSearchParams();
        for (const key of Object.keys(params)) {
            const value = params[key];
            if (value !== null && value !== undefined && value !== '') query.set(key, value);
        }
        const text = query.toString();
        const url = location.pathname + (text ? '?' + text : '');
        if (url === location.pathname + location.search && !location.hash) return;
        try {
            history[push ? 'pushState' : 'replaceState'](null, '', url);
        } catch (_) { /* e.g. file:// */ }
    }

    function pdfHref(editionId, page, query) {
        const q = new URLSearchParams({ view: 'pdf', edition: editionId });
        if (page && page > 1) q.set('p', page);
        if (query) q.set('q', query);
        return location.pathname + '?' + q.toString();
    }

    let silentNav = false;
    function activateView(name) {
        const button = document.querySelector('.nav-btn[data-view="' + name + '"]');
        if (!button) return false;
        if (!button.classList.contains('active')) {
            silentNav = true;
            try { button.click(); } finally { silentNav = false; }
        }
        return true;
    }

    function currentView() {
        const active = document.querySelector('.view.active');
        return active ? active.id.replace(/^view-/, '') : 'search';
    }

    // ════════════════════════════════════════════════════════════════════════
    // PDF 预览
    // ════════════════════════════════════════════════════════════════════════
    const pdf = {
        built: false, mode: 'grid',
        subject: '', title: '', items: [], total: 0, listCtl: null,
        edition: null, editionId: '', page: 1, query: '', pendingNotice: null,
        doc: null, loadingTask: null, openCtl: null, openSeq: 0,
        rows: null, printedIndex: null, hasVerified: false,
        renderSeq: 0, renderTask: null, textLayer: null,
        zoomMode: 'fit', zoom: 1, lastStageWidth: 0,
        searchCtl: null, outlineLoaded: false,
        dom: {},
    };
    let pdfjsPromise = null;

    function loadPdfjs() {
        if (!pdfjsPromise) {
            pdfjsPromise = import(PDFJS_BASE + 'legacy/build/pdf.min.mjs?v=' + PDFJS_VERSION).then(lib => {
                lib.GlobalWorkerOptions.workerSrc = PDFJS_BASE + 'legacy/build/pdf.worker.min.mjs?v=' + PDFJS_VERSION;
                return lib;
            }).catch(() => {
                pdfjsPromise = null;
                throw new LibError('network', 'PDF 阅读组件加载失败，请重试。');
            });
        }
        return pdfjsPromise;
    }

    function badgeNodes(badges, statusValue, platformCurrent) {
        const out = [];
        if (platformCurrent === false) {
            out.push(el('span', {
                class: 'lib-badge lib-badge-warn', text: '非平台现行版',
                title: '本站保存的这一版次不是官方平台目前提供的文件',
            }));
        }
        if (badges.searchable) {
            out.push(el('span', { class: 'lib-badge lib-badge-ok', text: badges.corpus_relation === 'partial' ? '部分可检索' : '可检索' }));
        } else {
            out.push(el('span', { class: 'lib-badge', text: '未接入检索', title: '本册 PDF 与站内语料的对应关系未获证明，不能在册内检索定位' }));
        }
        out.push(badges.printed_verified
            ? el('span', { class: 'lib-badge lib-badge-ok', text: '页码已核对' })
            : el('span', { class: 'lib-badge', text: '书页未核对', title: '本册只显示 PDF 页序' }));
        if (badges.has_text_layer === false) out.push(el('span', { class: 'lib-badge lib-badge-warn', text: '无文字层' }));
        else if (badges.has_text_layer === true) out.push(el('span', { class: 'lib-badge', text: '有文字层' }));
        if (statusValue === 'superseded') out.push(el('span', { class: 'lib-badge lib-badge-warn', text: '旧版次' }));
        if (statusValue === 'pending-verification') out.push(el('span', { class: 'lib-badge lib-badge-warn', text: '待核对' }));
        return out;
    }

    function editionLine(item) {
        return [item.publisher, item.edition_label].filter(Boolean).join(' · ') || '版次信息未标注';
    }

    // ── grid ────────────────────────────────────────────────────────────────
    function buildPdfGrid() {
        const d = pdf.dom;
        d.gridRoot = el('div', { class: 'lib-grid-root' });
        d.heroSub = el('p', { class: 'gaokao-hero-sub', text: '目录加载中…' });
        d.titleInput = el('input', {
            type: 'search', class: 'lib-input', id: 'lib-pdf-title', maxlength: '80', autocomplete: 'off',
            placeholder: '按书名、出版社筛选…', 'aria-label': '按书名筛选',
        });
        d.chips = el('div', { class: 'subject-tabs lib-chips', role: 'group', 'aria-label': '学科' });
        d.gridNotices = el('div', { class: 'lib-notices' });
        d.gridStatus = el('div', { class: 'lib-status', 'aria-live': 'polite' });
        d.grid = el('div', { class: 'lib-grid' });
        d.more = el('button', { class: 'gk-search-btn lib-more-btn hidden', type: 'button', text: '加载更多' });
        d.gridRoot.append(
            el('section', { class: 'gaokao-hero lib-hero' }, [el('h2', { text: 'PDF 预览' }), d.heroSub]),
            el('div', { class: 'gaokao-filters lib-filters' }, [
                el('div', { class: 'filter-group lib-grow' }, [el('label', { for: 'lib-pdf-title', text: PAGE_PHASE + '教材 · 书名' }), d.titleInput]),
            ]),
            d.chips, d.gridNotices, d.gridStatus, d.grid, el('div', { class: 'lib-more' }, d.more));
        d.titleInput.addEventListener('input', debounce(() => {
            pdf.title = d.titleInput.value.trim();
            syncPdfUrl(false);
            loadGrid(false);
        }, 300));
        d.more.addEventListener('click', () => loadGrid(true));
    }

    async function loadGrid(append) {
        const d = pdf.dom;
        if (pdf.listCtl) pdf.listCtl.abort();
        const ctl = pdf.listCtl = new AbortController();
        if (!append) { pdf.items = []; clear(d.grid); }
        d.gridStatus.textContent = '加载中…';
        d.more.classList.add('hidden');
        try {
            const data = await api('/api/library/editions', {
                phase: PAGE_PHASE, subject: pdf.subject, q: pdf.title, region: REGION,
                offset: pdf.items.length, limit: GRID_PAGE_SIZE,
            }, ctl.signal);
            if (ctl.signal.aborted) return;
            pdf.total = data.total;
            renderChips(data.subjects || []);
            for (const item of data.editions) {
                pdf.items.push(item);
                d.grid.append(editionCard(item));
            }
            d.gridStatus.textContent = pdf.total
                ? `共 ${pdf.total} 册${pdf.items.length < pdf.total ? `，已显示 ${pdf.items.length} 册` : ''}`
                : (pdf.subject || pdf.title ? '没有符合条件的教材。' : `目录中暂时没有可预览的${PAGE_PHASE}教材。`);
            d.more.classList.toggle('hidden', pdf.items.length >= pdf.total);
            getRelease().then(release => {
                if (!release.available) return;
                const n = (release.coverage.previewable_by_stage || {})[PAGE_PHASE] || 0;
                d.heroSub.textContent = `${PAGE_PHASE}教材 ${n} 册可在线预览 · 按需读取页面，不整本下载 · 目录版本 ${release.release_id}`;
            }).catch(() => { /* hero text is optional */ });
        } catch (error) {
            if (error.kind === 'aborted') return;
            d.gridStatus.textContent = '';
            clear(d.gridNotices);
            d.heroSub.textContent = error.kind === 'unavailable' ? '目录尚未发布' : '目录加载失败';
            const box = errorNotice(error, () => { clear(d.gridNotices); loadGrid(false); });
            if (box) d.gridNotices.append(box);
        }
    }

    function renderChips(subjects) {
        const d = pdf.dom;
        clear(d.chips);
        if (!subjects.length) return;
        const total = subjects.reduce((sum, s) => sum + s.count, 0);
        const make = (name, label, count) => el('button', {
            class: 'subject-tab' + (pdf.subject === name ? ' active' : ''), type: 'button',
            'aria-pressed': pdf.subject === name ? 'true' : 'false',
            onclick: () => { pdf.subject = name; syncPdfUrl(false); loadGrid(false); },
        }, [label, el('span', { class: 'tab-count', text: String(count) })]);
        d.chips.append(make('', '全部', total));
        for (const s of subjects) d.chips.append(make(s.name, s.name, s.count));
    }

    function editionCard(item) {
        const href = pdfHref(item.edition_id, 1, '');
        const card = el('a', {
            class: 'lib-card', href,
            onclick: event => {
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
                event.preventDefault();
                openEdition(item.edition_id, 1, { push: true });
            },
        }, [
            el('span', { class: 'lib-card-subject', text: item.subject || '' }),
            el('strong', { class: 'lib-card-title', text: item.title }),
            el('span', { class: 'lib-card-meta', text: editionLine(item) }),
            el('span', { class: 'lib-card-meta', text: `共 ${item.pages} 页 · ${formatBytes(item.bytes)}` }),
            el('span', { class: 'lib-badges' }, badgeNodes(item.badges, item.status, item.platform_current)),
        ]);
        const others = item.other_held_editions || [];
        if (!others.length) return card;
        // One card per book.  Another file held for the same book is a different version:
        // it is named here and opened on its own, never mixed into this card.
        const cell = el('div', { class: 'lib-card-cell' }, card);
        for (const other of others) {
            const day = dayOf(other.acquired_at);
            const label = other.platform_current === false
                ? `另有较早取得的版本${day ? `（${day}）` : ''}`
                : '另有其他版本';
            cell.append(el('a', {
                class: 'lib-link lib-card-other', href: pdfHref(other.edition_id, 1, ''),
                text: `${label} · 共 ${other.pages} 页 · 打开`,
                onclick: event => {
                    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
                    event.preventDefault();
                    openEdition(other.edition_id, 1, { push: true });
                },
            }));
        }
        return cell;
    }

    function showGrid(push) {
        closeDocument();
        pdf.mode = 'grid';
        pdf.editionId = '';
        pdf.edition = null;
        pdf.query = '';
        const d = pdf.dom;
        d.viewerRoot.classList.add('hidden');
        d.gridRoot.classList.remove('hidden');
        clear(d.gridNotices);
        if (pdf.pendingNotice) {
            d.gridNotices.append(notice('warn', pdf.pendingNotice));
            pdf.pendingNotice = null;
        }
        d.titleInput.value = pdf.title;
        syncPdfUrl(push);
        loadGrid(false);
    }

    function syncPdfUrl(push) {
        if (currentView() !== 'pdf') return;
        if (pdf.mode === 'viewer' && pdf.editionId) {
            setUrl({ view: 'pdf', edition: pdf.editionId, p: pdf.page > 1 ? pdf.page : '', q: pdf.query }, push);
        } else {
            setUrl({ view: 'pdf', subject: pdf.subject, t: pdf.title }, push);
        }
    }

    // ── viewer ──────────────────────────────────────────────────────────────
    function buildPdfViewer() {
        const d = pdf.dom;
        d.viewerRoot = el('div', { class: 'lib-viewer hidden' });
        d.vTitle = el('strong', { class: 'lib-viewer-name' });
        d.vMeta = el('span', { class: 'lib-viewer-meta' });
        d.vBadges = el('span', { class: 'lib-badges' });
        d.vDownload = el('a', { class: 'lib-btn lib-btn-primary lib-download-main hidden', download: true }, '⬇ 下载本册 PDF');
        d.vSources = el('div', { class: 'lib-source-list' });
        d.vNotices = el('div', { class: 'lib-notices' });
        d.sideToggle = el('button', { class: 'lib-btn', type: 'button', 'aria-expanded': 'false', text: '目录 / 检索结果' });
        d.searchInput = el('input', {
            type: 'search', class: 'lib-input', maxlength: '80', autocomplete: 'off',
            placeholder: '在本册中检索…（按 / 聚焦）', 'aria-label': '在本册中检索',
        });
        d.searchBtn = el('button', { class: 'lib-btn', type: 'submit', text: '检索' });
        d.searchForm = el('form', { class: 'lib-book-search', role: 'search' }, [d.searchInput, d.searchBtn]);
        d.zoomLabel = el('span', { class: 'lib-zoom-label', 'aria-live': 'polite', text: '适合宽度' });
        d.sideTabOutline = el('button', { class: 'lib-side-tab active', type: 'button', text: '目录' });
        d.sideTabHits = el('button', { class: 'lib-side-tab', type: 'button', text: '检索结果' });
        d.outline = el('div', { class: 'lib-outline' });
        d.hits = el('div', { class: 'lib-hits hidden' });
        d.side = el('aside', { class: 'lib-side', 'aria-label': '目录与检索结果' }, [
            el('div', { class: 'lib-side-tabs' }, [d.sideTabOutline, d.sideTabHits]), d.outline, d.hits]);
        d.pageWrap = el('div', { class: 'lib-page-wrap' });
        d.stageStatus = el('div', { class: 'lib-stage-status', 'aria-live': 'polite' });
        d.stage = el('div', { class: 'lib-stage' }, [d.stageStatus, d.pageWrap]);
        d.pageInput = el('input', { type: 'text', inputmode: 'numeric', class: 'lib-page-input', 'aria-label': 'PDF 页序', maxlength: '5' });
        d.pageTotal = el('span', { class: 'lib-page-total' });
        d.printedLabel = el('span', { class: 'lib-printed hidden' });
        d.printedInput = el('input', { type: 'text', class: 'lib-page-input', 'aria-label': '按书页跳转', maxlength: '8', placeholder: '书页' });
        d.printedForm = el('form', { class: 'lib-printed-jump hidden' }, [
            el('label', { class: 'lib-printed-jump-label' }, ['跳到书页 ', d.printedInput]),
            el('button', { class: 'lib-btn', type: 'submit', text: '跳转' })]);
        d.prev = el('button', { class: 'lib-btn lib-nav-prev', type: 'button', 'aria-label': '上一页', text: '‹ 上一页' });
        d.next = el('button', { class: 'lib-btn lib-nav-next', type: 'button', 'aria-label': '下一页', text: '下一页 ›' });
        d.pageForm = el('form', { class: 'lib-page-jump' }, [el('span', { text: 'PDF 第' }), d.pageInput, d.pageTotal]);
        d.pagebarMsg = el('span', { class: 'lib-pagebar-msg', 'aria-live': 'polite' });

        d.viewerRoot.append(
            el('div', { class: 'lib-viewer-head' }, [
                el('button', { class: 'lib-btn', type: 'button', text: '← 书目', onclick: () => showGrid(true) }),
                el('div', { class: 'lib-viewer-title' }, [d.vTitle, d.vMeta, d.vBadges]),
                el('div', { class: 'lib-viewer-download' }, [d.vDownload, d.vSources]),
            ]),
            d.vNotices,
            el('div', { class: 'lib-toolbar' }, [
                d.sideToggle, d.searchForm,
                el('div', { class: 'lib-zoom', role: 'group', 'aria-label': '缩放' }, [
                    el('button', { class: 'lib-btn', type: 'button', 'aria-label': '缩小', text: '－', onclick: () => zoomBy(1 / 1.25) }),
                    d.zoomLabel,
                    el('button', { class: 'lib-btn', type: 'button', 'aria-label': '放大', text: '＋', onclick: () => zoomBy(1.25) }),
                    el('button', { class: 'lib-btn', type: 'button', text: '适合宽度', onclick: () => { pdf.zoomMode = 'fit'; renderPage(); } }),
                ]),
            ]),
            el('div', { class: 'lib-viewer-body' }, [d.side, d.stage]),
            el('div', { class: 'lib-pagebar' }, [d.prev, d.pageForm, d.printedLabel, d.printedForm, d.next, d.pagebarMsg]));

        d.prev.addEventListener('click', () => goPage(pdf.page - 1));
        d.next.addEventListener('click', () => goPage(pdf.page + 1));
        d.pageForm.addEventListener('submit', event => {
            event.preventDefault();
            const text = d.pageInput.value.trim();
            const total = pdf.doc ? pdf.doc.numPages : 0;
            if (!/^\d{1,5}$/.test(text) || +text < 1 || +text > total) {
                d.pagebarMsg.textContent = `PDF 页序范围是 1 – ${total}`;
                d.pageInput.value = String(pdf.page);
                return;
            }
            d.pageInput.blur();
            goPage(+text);
        });
        d.printedForm.addEventListener('submit', event => {
            event.preventDefault();
            const label = d.printedInput.value.trim().replace(/^p\.?\s*/i, '');
            const target = pdf.printedIndex && label ? pdf.printedIndex.get(label) : undefined;
            if (!target) {
                d.pagebarMsg.textContent = label ? `本册已核对的书页中没有「${label}」` : '请输入书页';
                return;
            }
            d.printedInput.value = '';
            d.printedInput.blur();
            goPage(target);
        });
        d.searchForm.addEventListener('submit', event => {
            event.preventDefault();
            runBookSearch(d.searchInput.value.trim());
        });
        d.sideToggle.addEventListener('click', () => toggleSide());
        d.sideTabOutline.addEventListener('click', () => showSideTab('outline'));
        d.sideTabHits.addEventListener('click', () => showSideTab('hits'));
    }

    function toggleSide(force) {
        const d = pdf.dom;
        const open = force === undefined ? !d.viewerRoot.classList.contains('lib-side-open') : force;
        d.viewerRoot.classList.toggle('lib-side-open', open);
        d.sideToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (pdf.zoomMode === 'fit' && pdf.doc) setTimeout(renderPage, 0);
    }

    function showSideTab(name) {
        const d = pdf.dom;
        d.sideTabOutline.classList.toggle('active', name === 'outline');
        d.sideTabHits.classList.toggle('active', name === 'hits');
        d.outline.classList.toggle('hidden', name !== 'outline');
        d.hits.classList.toggle('hidden', name !== 'hits');
    }

    function cancelRender() {
        pdf.renderSeq++;
        if (pdf.renderTask) { try { pdf.renderTask.cancel(); } catch (_) { /* already done */ } pdf.renderTask = null; }
        if (pdf.textLayer) { try { pdf.textLayer.cancel(); } catch (_) { /* already done */ } pdf.textLayer = null; }
    }

    function closeDocument() {
        pdf.openSeq++;
        cancelRender();
        if (pdf.openCtl) { pdf.openCtl.abort(); pdf.openCtl = null; }
        if (pdf.searchCtl) { pdf.searchCtl.abort(); pdf.searchCtl = null; }
        const task = pdf.loadingTask;
        pdf.loadingTask = null;
        pdf.doc = null;
        if (task) { try { task.destroy(); } catch (_) { /* ignore */ } }
        pdf.rows = null;
        pdf.printedIndex = null;
        pdf.hasVerified = false;
        pdf.outlineLoaded = false;
        if (pdf.dom.pageWrap) releaseCanvases(pdf.dom.pageWrap);
    }

    function pdfLoadError(error, status) {
        const name = error && error.name;
        const code = (error && error.status) || status || 0;
        if (name === 'MissingPDFException' || code === 404) return new LibError('notfound', '文件缺失：本站没有找到这册 PDF。');
        if (code === 429) return new LibError('rate', '请求过于频繁，请稍后重试。', 5);
        if (code === 503) return new LibError('unavailable', '教材目录尚未发布或暂不可用。');
        if (name === 'InvalidPDFException') return new LibError('server', 'PDF 文件无法解析（文件可能损坏）。');
        if (name === 'UnexpectedResponseException' && code) return new LibError('server', `读取 PDF 时服务器返回错误（${code}）。`);
        return new LibError('network', '读取 PDF 时网络中断，请重试。');
    }

    async function openEdition(editionId, page, options) {
        options = options || {};
        activateView('pdf');
        ensurePdfBuilt();
        closeDocument();
        const seq = pdf.openSeq;
        const d = pdf.dom;
        pdf.mode = 'viewer';
        pdf.editionId = editionId;
        pdf.page = Math.max(1, page || 1);
        pdf.query = options.query || '';
        pdf.edition = null;
        d.gridRoot.classList.add('hidden');
        d.viewerRoot.classList.remove('hidden');
        d.viewerRoot.classList.add('lib-no-doc');
        d.viewerRoot.classList.remove('lib-side-open');
        d.vTitle.textContent = '加载中…';
        d.vMeta.textContent = '';
        clear(d.vBadges); clear(d.vSources); clear(d.vNotices); clear(d.pageWrap); clear(d.outline); clear(d.hits);
        d.vDownload.classList.add('hidden');
        d.stageStatus.textContent = '正在读取版次信息…';
        d.searchInput.value = pdf.query;
        d.pagebarMsg.textContent = '';
        d.printedLabel.classList.add('hidden');
        d.printedForm.classList.add('hidden');
        d.pageInput.value = String(pdf.page);
        d.pageTotal.textContent = '';
        showSideTab('outline');
        syncPdfUrl(Boolean(options.push));
        if (options.notice) d.vNotices.append(notice('warn', options.notice));
        pdf.via = options.via || '';

        const ctl = pdf.openCtl = new AbortController();
        const retry = () => openEdition(editionId, pdf.page, { query: pdf.query });
        const fail = error => {
            if (seq !== pdf.openSeq || (error && error.kind === 'aborted')) return;
            d.stageStatus.textContent = '';
            if (error && error.kind === 'notfound') d.vDownload.classList.add('hidden');
            const box = errorNotice(error, retry);
            if (box) d.vNotices.append(box);
        };

        let edition;
        try {
            edition = (await api('/api/library/editions/' + encodeURIComponent(editionId), null, ctl.signal)).edition;
        } catch (error) {
            if (seq === pdf.openSeq) d.vTitle.textContent = '无法打开';
            return fail(error);
        }
        if (seq !== pdf.openSeq) return;
        pdf.edition = edition;
        d.vTitle.textContent = edition.title;
        d.vMeta.textContent = `${[edition.stage, edition.subject].filter(Boolean).join(' · ')} · ${editionLine(edition)} · 共 ${edition.pages} 页`;
        d.vBadges.append.apply(d.vBadges, badgeNodes(edition.badges, edition.status, edition.platform_current));
        renderViewerSources(edition, pdf.via);
        if (edition.status === 'superseded') d.vNotices.append(notice('warn', '此版次已被较新的版次取代，内容与页码可能与现行教材不同。'));
        if (edition.status === 'pending-verification') d.vNotices.append(notice('warn', '此版次尚待核对，信息可能不完整。'));
        d.searchInput.disabled = d.searchBtn.disabled = !edition.searchable;
        d.searchInput.placeholder = edition.searchable ? '在本册中检索…（按 / 聚焦）' : '本册未接入全文检索';
        if (!edition.previewable || !edition.file) {
            d.stageStatus.textContent = '';
            d.vNotices.append(notice('warn', '本版次的 PDF 未存放在本站，无法在线预览。', [
                el('a', { class: 'lib-btn', href: location.pathname + '?view=downloads&q=' + encodeURIComponent(edition.title), text: '到「教材下载」查找' })]));
            return;
        }

        // Page-map rows (labels, printed jump).  Loaded alongside the PDF.
        const rowsPromise = loadRows(edition, ctl.signal).catch(error => {
            if (error.kind !== 'aborted' && seq === pdf.openSeq) {
                d.vNotices.append(notice('warn', '页码对应表加载失败，本次只显示 PDF 页序。'));
            }
            return null;
        });

        // Probe before handing the URL to PDF.js: explicit errors, and refuse to
        // fall back to downloading the whole book when Range is not supported.
        d.stageStatus.textContent = '正在连接文件…';
        try {
            let probe;
            try {
                probe = await fetch(edition.file.url, { method: 'HEAD', signal: ctl.signal, credentials: 'omit' });
            } catch (error) {
                if (error && error.name === 'AbortError') return;
                throw new LibError('network', '网络连接失败，无法读取 PDF，请重试。');
            }
            if (seq !== pdf.openSeq) return;
            if (!probe.ok) throw statusError(probe.status, probe.headers.get('Retry-After'), 'file');
            if (!/bytes/i.test(probe.headers.get('Accept-Ranges') || '')) {
                throw new LibError('norange', '服务器不支持分段读取（Range），为避免整本下载，已停止在线预览。可以改为下载本册 PDF。');
            }
            const lib = await loadPdfjs();
            if (seq !== pdf.openSeq) return;
            d.stageStatus.textContent = '正在打开 PDF…';
            const task = pdf.loadingTask = lib.getDocument({
                url: edition.file.url,
                rangeChunkSize: RANGE_CHUNK,
                disableAutoFetch: true,   // never fetch the rest of the book in the background
                disableStream: true,      // page data comes from Range requests only
                cMapUrl: PDFJS_BASE + 'cmaps/', cMapPacked: true,
                standardFontDataUrl: PDFJS_BASE + 'standard_fonts/',
                isEvalSupported: false, enableXfa: false, withCredentials: false,
            });
            let doc;
            try {
                doc = await task.promise;
            } catch (error) {
                if (seq !== pdf.openSeq) return;
                throw pdfLoadError(error);
            }
            if (seq !== pdf.openSeq) { try { task.destroy(); } catch (_) { /* ignore */ } return; }
            pdf.doc = doc;
            pdf.lib = lib;
            d.viewerRoot.classList.remove('lib-no-doc');
            const rows = await rowsPromise;
            if (seq !== pdf.openSeq) return;
            if (rows && doc.numPages === edition.pages) {
                applyRows(rows);
            } else if (doc.numPages !== edition.pages) {
                // The page map is bound to the catalogued file; a different page count voids it.
                d.vNotices.append(notice('warn', `文件页数（${doc.numPages}）与目录记录（${edition.pages}）不一致，书页与检索定位已停用。`));
                d.searchInput.disabled = d.searchBtn.disabled = true;
            }
            d.pageTotal.textContent = `/ ${doc.numPages} 页`;
            pdf.page = Math.min(Math.max(1, pdf.page), doc.numPages);
            loadOutline(doc, seq);
            renderPage();
            if (pdf.query && edition.searchable && !d.searchInput.disabled) runBookSearch(pdf.query, true);
        } catch (error) {
            fail(error instanceof LibError ? error : pdfLoadError(error));
        }
    }

    function downloadsRowHref(rowId) {
        return location.pathname + '?view=downloads&stage=all&row=' + encodeURIComponent(rowId);
    }

    function differentEditionText(other) {
        return [other.edition_label || '版次未标注', other.pages ? `${other.pages} 页` : '',
            other.update_time ? `平台更新于 ${monthOf(other.update_time) || other.update_time}` : ''].filter(Boolean).join('，');
    }

    function renderViewerSources(edition, via) {
        const d = pdf.dom;
        clear(d.vSources);
        const notCurrent = edition.platform_current === false;
        const acquired = edition.acquired_at || (edition.source && edition.source.retrieved_at);
        const siblings = edition.other_held_editions || [];
        const other = siblings[0] || null;
        const mine = roleText(edition.platform_current, acquired);
        if (edition.file) {
            // This button always delivers THIS file from this site, nothing else.
            const tag = other && mine ? mine.replace(/（.*$/, '') + ' · ' : (notCurrent ? '此版次 · ' : '');
            d.vDownload.href = edition.file.download_url;
            d.vDownload.setAttribute('download', edition.file.filename);
            d.vDownload.textContent = `⬇ 下载本册 PDF（本站 · ${tag}${formatBytes(edition.file.bytes)}）`;
            d.vDownload.classList.remove('hidden');
        }
        const label = edition.edition_label || '本版次';
        if (other) {
            // Two files of this book are held here.  Say which one is open and offer the other.
            // There is no page-to-page map between them: switching always starts at page 1.
            const theirs = roleText(other.platform_current, other.acquired_at) || '另一版本';
            let text = '';
            if (via && edition.preview_default === false) {
                text += `${via === 'search' ? '此检索结果' : '此旧链接'}打开的是${mine || '非默认显示的版本'}：`
                    + `${via === 'search' ? '检索结果' : '旧链接'}的页码只在这一份文件上有逐页对应。本书默认显示的是另一份：${theirs}。`;
            } else {
                text += `本书在本站有两份文件，是不同版本。当前打开的是：${mine || '其中一份'}，共 ${edition.pages} 页。另一份：${theirs}，共 ${other.pages} 页。`;
            }
            text += '两份文件之间没有逐页对应，页码不能互用。';
            const target = other.platform_current === true ? '平台现行文件' : (other.platform_current === false ? '较早取得的版本' : '另一版本');
            d.vNotices.append(notice(edition.preview_default === false || notCurrent ? 'warn' : 'info', text, [
                el('a', {
                    class: 'lib-btn', href: pdfHref(other.edition_id, 1, ''), text: `切换到${target}（从第 1 页）`,
                    onclick: event => {
                        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
                        event.preventDefault();
                        openEdition(other.edition_id, 1, {
                            push: true,
                            notice: '已切换到另一份文件。两份文件之间没有逐页对应，页码不沿用，已从第 1 页打开。',
                        });
                    },
                })]));
        } else if (notCurrent) {
            const got = monthOf(acquired);
            const newer = edition.newer_edition && edition.newer_edition.holding !== 'learn-only' ? edition.newer_edition : null;
            const actions = [];
            if (newer && newer.download_row_id) {
                actions.push(el('a', {
                    class: 'lib-btn', href: downloadsRowHref(newer.download_row_id),
                    text: '查看另一版次的下载条目',
                    onclick: event => {
                        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
                        event.preventDefault();
                        openDownloadRow(newer.download_row_id);
                    },
                }));
            }
            d.vNotices.append(notice('warn',
                `非平台现行版：本站保存的这一版次${got ? `取得于 ${got}` : ''}，不是官方平台目前提供的文件。`
                + (newer ? `官方平台已有更新版本（不同版次：${differentEditionText(newer)}），`
                    + (newer.pages && newer.pages !== edition.pages ? `页数 ${edition.pages}→${newer.pages}，两者页码不能互用；` : '')
                    + '上方按钮只下载本站保存的这一版次。' : '上方按钮只下载本站保存的这一版次。'),
                actions));
        }
        for (const source of edition.sources || []) {
            const url = externalUrl(source.url);
            if (!url) continue;
            d.vSources.append(el('a', {
                class: 'lib-link', href: url, target: '_blank', rel: 'noopener noreferrer',
                text: `${source.label} · ${label}（新标签页）`,
            }));
        }
        const shown = new Set(siblings.map(item => item.edition_id));
        for (const item of edition.other_editions || []) {
            if (shown.has(item.edition_id)) continue;
            if (edition.newer_edition && item.edition_id === edition.newer_edition.edition_id) continue;
            const text = `其他版次：${item.edition_label || item.title}${item.status === 'superseded' ? '（旧版）' : ''}`;
            d.vSources.append(item.previewable
                ? el('a', {
                    class: 'lib-link', href: pdfHref(item.edition_id, 1, ''), text: text + ' · 预览',
                    onclick: event => { event.preventDefault(); openEdition(item.edition_id, 1, { push: true }); },
                })
                : el('span', { class: 'lib-muted', text }));
        }
    }

    async function loadRows(edition, signal) {
        const rows = new Array(edition.pages + 1);
        for (let from = 1; from <= edition.pages; from += ROW_SPAN) {
            const data = await api(`/api/library/editions/${encodeURIComponent(edition.edition_id)}/pages`,
                { from, to: Math.min(edition.pages, from + ROW_SPAN - 1) }, signal);
            for (const row of data.rows) rows[row.pdf_page] = row;
        }
        return rows;
    }

    function applyRows(rows) {
        pdf.rows = rows;
        pdf.printedIndex = new Map();
        for (const row of rows) {
            if (row && row.printed_verified === true && row.printed_label && !pdf.printedIndex.has(row.printed_label)) {
                pdf.printedIndex.set(row.printed_label, row.pdf_page);
            }
        }
        pdf.hasVerified = pdf.printedIndex.size > 0;
        pdf.dom.printedForm.classList.toggle('hidden', !pdf.hasVerified);
    }

    function updatePageLabels() {
        const d = pdf.dom;
        const total = pdf.doc ? pdf.doc.numPages : 0;
        d.pageInput.value = String(pdf.page);
        d.prev.disabled = pdf.page <= 1;
        d.next.disabled = !total || pdf.page >= total;
        const row = pdf.rows ? pdf.rows[pdf.page] : null;
        // A printed page is shown only when this row is verified.
        if (row && row.printed_verified === true && row.printed_label) {
            d.printedLabel.textContent = `书页 p.${row.printed_label} · 已核对`;
            d.printedLabel.classList.remove('hidden');
        } else {
            d.printedLabel.textContent = '';
            d.printedLabel.classList.add('hidden');
        }
    }

    function goPage(target) {
        if (!pdf.doc) return;
        const page = Math.min(Math.max(1, target), pdf.doc.numPages);
        if (page === pdf.page && pdf.dom.pageWrap.firstChild) return;
        pdf.page = page;
        pdf.dom.pagebarMsg.textContent = '';
        syncPdfUrl(false);
        renderPage();
        const top = pdf.dom.stage.getBoundingClientRect().top;
        if (top < 0) pdf.dom.stage.scrollIntoView({ block: 'start' });
    }

    function zoomBy(factor) {
        const current = pdf.zoomMode === 'fit' ? (pdf.fitScale || 1) : pdf.zoom;
        pdf.zoom = Math.min(4, Math.max(0.4, current * factor));
        pdf.zoomMode = 'custom';
        renderPage();
    }

    async function renderPage() {
        if (!pdf.doc) return;
        cancelRender();
        const seq = pdf.renderSeq;
        const doc = pdf.doc;
        const lib = pdf.lib;
        const d = pdf.dom;
        const number = pdf.page;
        updatePageLabels();
        d.stageStatus.textContent = `正在读取第 ${number} 页…`;
        d.pageWrap.classList.add('lib-page-stale');
        try {
            const page = await doc.getPage(number);
            if (seq !== pdf.renderSeq) return;
            const base = page.getViewport({ scale: 1 });
            const available = Math.max(240, d.stage.clientWidth - 2);
            pdf.lastStageWidth = d.stage.clientWidth;
            pdf.fitScale = available / base.width;
            const cssScale = pdf.zoomMode === 'fit' ? pdf.fitScale : pdf.zoom;
            d.zoomLabel.textContent = pdf.zoomMode === 'fit' ? '适合宽度' : Math.round(cssScale * 100) + '%';
            let ratio = Math.min(window.devicePixelRatio || 1, 2);
            const pixels = base.width * base.height * cssScale * cssScale * ratio * ratio;
            const budget = canvasBudget();
            if (pixels > budget) ratio *= Math.sqrt(budget / pixels);
            const cssViewport = page.getViewport({ scale: cssScale });
            const renderViewport = page.getViewport({ scale: cssScale * ratio });
            const canvas = el('canvas', { class: 'lib-canvas', role: 'img', 'aria-label': `PDF 第 ${number} 页` });
            canvas.width = Math.floor(renderViewport.width);
            canvas.height = Math.floor(renderViewport.height);
            canvas.style.width = Math.floor(cssViewport.width) + 'px';
            canvas.style.height = Math.floor(cssViewport.height) + 'px';
            const task = pdf.renderTask = page.render({ canvasContext: canvas.getContext('2d'), viewport: renderViewport });
            await task.promise;
            if (seq !== pdf.renderSeq) return;
            pdf.renderTask = null;
            const textDiv = el('div', { class: 'textLayer lib-text-layer' });
            d.pageWrap.style.setProperty('--scale-factor', String(cssScale));
            d.pageWrap.style.width = canvas.style.width;
            d.pageWrap.style.height = canvas.style.height;
            releaseCanvases(d.pageWrap);
            clear(d.pageWrap);
            d.pageWrap.append(canvas, textDiv);
            d.pageWrap.classList.remove('lib-page-stale');
            d.stageStatus.textContent = '';

            const content = await page.getTextContent();
            if (seq !== pdf.renderSeq) return;
            const hasText = content.items.some(item => item.str && item.str.trim());
            if (hasText) {
                const layer = pdf.textLayer = new lib.TextLayer({ textContentSource: content, container: textDiv, viewport: cssViewport });
                await layer.render();
                if (seq !== pdf.renderSeq) return;
                if (pdf.query) {
                    const count = highlightLayer(textDiv, pdf.query);
                    const otherPrinting = pdf.edition && pdf.edition.corpus && pdf.edition.corpus.text_identity === OTHER_PRINTING;
                    d.stageStatus.textContent = count
                        ? `本页 PDF 文字层中高亮 ${count} 处「${pdf.query}」`
                        : `本页 PDF 文字层中没有找到「${pdf.query}」。`
                            + (otherPrinting ? OTHER_PRINTING_NOTE + '。' : '检索依据的是站内检索库，与 PDF 文字层可能不同。');
                }
            } else if (pdf.query) {
                d.stageStatus.textContent = '此页无文字层，无法高亮。';
            }
            prefetchNeighbours(doc, number, seq);
        } catch (error) {
            if (seq !== pdf.renderSeq) return;
            if (error && error.name === 'RenderingCancelledException') return;
            d.pageWrap.classList.remove('lib-page-stale');
            clear(d.stageStatus);
            const mapped = pdfLoadError(error);
            d.stageStatus.append(errorNotice(new LibError(mapped.kind, `第 ${number} 页读取失败：${mapped.message}`), () => renderPage()));
        }
    }

    // Lazy neighbours: once the current page is on screen, warm the next and
    // previous page (bounded to two pages; never the whole document).
    function prefetchNeighbours(doc, number, seq) {
        const warm = target => {
            if (target < 1 || target > doc.numPages) return;
            doc.getPage(target).then(page => {
                if (seq !== pdf.renderSeq || doc !== pdf.doc) return null;
                return page.getOperatorList();
            }).catch(() => { /* a failed warm-up is reported when the page is opened */ });
        };
        const start = () => { if (seq === pdf.renderSeq && doc === pdf.doc) { warm(number + 1); warm(number - 1); } };
        if ('requestIdleCallback' in window) window.requestIdleCallback(start, { timeout: 1500 });
        else setTimeout(start, 400);
    }

    // Highlight query terms across text-layer spans (whitespace-insensitive).
    function highlightLayer(container, query) {
        const terms = queryTerms(query).map(t => t.replace(/\s+/g, '')).filter(Boolean);
        if (!terms.length) return 0;
        const spans = Array.from(container.querySelectorAll('span')).filter(s => !s.children.length && s.textContent);
        const owner = [];   // owner[i] = [spanIndex, charOffset] for the i-th non-space character
        let joined = '';
        spans.forEach((span, spanIndex) => {
            const text = span.textContent;
            for (let i = 0; i < text.length; i++) {
                if (/\s/.test(text[i])) continue;
                owner.push([spanIndex, i]);
                joined += text[i].toLowerCase();
            }
        });
        const ranges = new Map();   // spanIndex -> [[start, end)]
        let count = 0;
        for (const term of terms) {
            let at = joined.indexOf(term);
            while (at !== -1 && count < 500) {
                count++;
                for (let i = at; i < at + term.length; i++) {
                    const [spanIndex, offset] = owner[i];
                    if (!ranges.has(spanIndex)) ranges.set(spanIndex, []);
                    ranges.get(spanIndex).push(offset);
                }
                at = joined.indexOf(term, at + term.length);
            }
        }
        let first = null;
        ranges.forEach((offsets, spanIndex) => {
            const span = spans[spanIndex];
            const text = span.textContent;
            const marked = new Set(offsets);
            clear(span);
            let buffer = '';
            let inMark = false;
            const flush = () => {
                if (!buffer) return;
                if (inMark) {
                    const mark = el('mark', { class: 'lib-hl', text: buffer });
                    span.append(mark);
                    if (!first) first = mark;
                } else {
                    span.append(document.createTextNode(buffer));
                }
                buffer = '';
            };
            for (let i = 0; i < text.length; i++) {
                const hit = marked.has(i) || (inMark && /\s/.test(text[i]) && marked.has(i + 1));
                if (hit !== inMark) { flush(); inMark = hit; }
                buffer += text[i];
            }
            flush();
        });
        if (first && first.scrollIntoView) {
            const rect = first.getBoundingClientRect();
            if (rect.top < 80 || rect.bottom > window.innerHeight - 120) first.scrollIntoView({ block: 'center' });
        }
        return count;
    }

    // ── outline ─────────────────────────────────────────────────────────────
    async function loadOutline(doc, seq) {
        const d = pdf.dom;
        clear(d.outline);
        d.outline.append(el('p', { class: 'lib-muted', text: '目录加载中…' }));
        let outline = null;
        try {
            outline = await doc.getOutline();
        } catch (_) { outline = null; }
        if (seq !== pdf.openSeq) return;
        clear(d.outline);
        if (!outline || !outline.length) {
            d.outline.append(el('p', { class: 'lib-muted', text: '本册 PDF 没有内嵌目录（书签）。' }));
            return;
        }
        let budget = 600;
        const build = (items, depth) => {
            const list = el('ul', { class: 'lib-outline-list' });
            for (const item of items) {
                if (budget-- <= 0) break;
                const entry = el('li', null, el('button', {
                    class: 'lib-outline-item', type: 'button', text: String(item.title || '（无标题）').slice(0, 120),
                    onclick: () => jumpToDest(doc, item.dest),
                }));
                if (item.items && item.items.length && depth < 4) entry.append(build(item.items, depth + 1));
                list.append(entry);
            }
            return list;
        };
        d.outline.append(build(outline, 1));
        if (window.matchMedia('(min-width: 901px)').matches && !pdf.query) toggleSide(true);
    }

    async function jumpToDest(doc, dest) {
        try {
            let target = dest;
            if (typeof target === 'string') target = await doc.getDestination(target);
            if (!Array.isArray(target) || doc !== pdf.doc) return;
            const ref = target[0];
            const index = (ref && typeof ref === 'object') ? await doc.getPageIndex(ref) : (Number.isInteger(ref) ? ref : null);
            if (index === null || doc !== pdf.doc) return;
            goPage(index + 1);
            if (window.matchMedia('(max-width: 900px)').matches) toggleSide(false);
        } catch (_) {
            pdf.dom.pagebarMsg.textContent = '这条目录没有可用的页面位置。';
        }
    }

    // ── in-book search (existing SUN search API, scoped by book_key) ────────
    async function runBookSearch(query, silent) {
        const d = pdf.dom;
        const edition = pdf.edition;
        if (!edition || !edition.searchable || !edition.search_book_key) return;
        if (pdf.searchCtl) pdf.searchCtl.abort();
        pdf.query = query;
        syncPdfUrl(false);
        if (!query) {
            clear(d.hits);
            if (pdf.doc) renderPage();
            return;
        }
        const ctl = pdf.searchCtl = new AbortController();
        const seq = pdf.openSeq;
        showSideTab('hits');
        if (!silent) toggleSide(true);
        clear(d.hits);
        d.hits.append(el('p', { class: 'lib-muted', text: '检索中…' }));
        if (pdf.doc) renderPage();   // highlight on the current page right away
        try {
            const params = { q: query, book_key: edition.search_book_key, source: 'textbook', limit: RESOLVE_BATCH };
            if (edition.stage === '高中' || edition.stage === '初中') params.phase = edition.stage;
            const data = await api('/api/search', params, ctl.signal);
            if (seq !== pdf.openSeq || ctl.signal.aborted) return;
            const results = [];
            for (const group of data.groups || []) {
                for (const r of group.results || []) {
                    if (r.book_key === edition.search_book_key && Number.isInteger(r.section) && r.section >= 0) results.push(r);
                }
            }
            if (!results.length) {
                clear(d.hits);
                d.hits.append(el('p', { class: 'lib-muted', text: `本册中没有找到「${query}」。` }));
                return;
            }
            const sections = Array.from(new Set(results.map(r => r.section))).slice(0, RESOLVE_BATCH);
            const resolved = await api('/api/library/resolve', { edition_id: edition.edition_id, sections: sections.join(',') }, ctl.signal);
            if (seq !== pdf.openSeq || ctl.signal.aborted) return;
            const pageOf = new Map();
            for (const item of resolved.results) if (item.available) pageOf.set(item.section, item.pdf_page);
            results.sort((a, b) => (pageOf.get(a.section) || 1e9) - (pageOf.get(b.section) || 1e9));
            clear(d.hits);
            const mapped = results.filter(r => pageOf.has(r.section)).length;
            d.hits.append(el('p', { class: 'lib-muted', text: `检索库中找到 ${results.length} 处，其中 ${mapped} 处可定位到本 PDF 的页面。下列文字是检索库片段，不是 PDF 页面的原文引用。` }));
            if (edition.corpus && edition.corpus.text_identity === OTHER_PRINTING) {
                d.hits.append(el('p', { class: 'lib-hit-note', text: OTHER_PRINTING_NOTE + '。' }));
            }
            const list = el('ul', { class: 'lib-hit-list' });
            for (const r of results) {
                const page = pageOf.get(r.section);
                const row = pdf.rows && page ? pdf.rows[page] : null;
                const where = page
                    ? `PDF 第 ${page} 页` + (row && row.printed_verified && row.printed_label ? ` · 书页 p.${row.printed_label}` : '')
                    : '页码未能对应到本 PDF';
                const body = [el('span', { class: 'lib-hit-page', text: where }),
                    el('span', { class: 'lib-hit-snippet' }, [el('span', { class: 'lib-hit-kind', text: '检索库片段 ' })].concat(snippetNodes(r.snippet, 140)))];
                list.append(el('li', null, page
                    ? el('button', {
                        class: 'lib-hit', type: 'button',
                        onclick: () => { goPage(page); if (window.matchMedia('(max-width: 900px)').matches) toggleSide(false); },
                    }, body)
                    : el('div', { class: 'lib-hit lib-hit-off' }, body)));
            }
            d.hits.append(list);
        } catch (error) {
            if (error.kind === 'aborted' || seq !== pdf.openSeq) return;
            clear(d.hits);
            const box = errorNotice(error, () => runBookSearch(query));
            if (box) d.hits.append(box);
        }
    }

    function ensurePdfBuilt() {
        if (pdf.built) return;
        pdf.built = true;
        buildPdfGrid();
        buildPdfViewer();
        viewPdf.append(pdf.dom.gridRoot, pdf.dom.viewerRoot);
    }

    function enterPdf(fromUrl) {
        ensurePdfBuilt();
        if (fromUrl) {
            const q = new URLSearchParams(location.search);
            const edition = q.get('edition') || '';
            if (/^[a-z0-9][a-z0-9._-]{2,95}$/.test(edition)) {
                const page = /^\d{1,5}$/.test(q.get('p') || '') ? +q.get('p') : 1;
                openEdition(edition, page, { query: (q.get('q') || '').slice(0, 80) });
                return;
            }
            pdf.subject = (q.get('subject') || '').slice(0, 60);
            pdf.title = (q.get('t') || '').slice(0, 80);
            showGrid(false);
            return;
        }
        if (pdf.mode === 'viewer' && pdf.editionId) {
            syncPdfUrl(true);
            if (!pdf.doc && !pdf.loadingTask) openEdition(pdf.editionId, pdf.page, { query: pdf.query });
            else if (pdf.doc && (!pdf.dom.pageWrap.firstChild || pdf.dom.pageWrap.classList.contains('lib-page-stale'))) renderPage();
        } else {
            pdf.mode = 'grid';
            pdf.dom.viewerRoot.classList.add('hidden');
            pdf.dom.gridRoot.classList.remove('hidden');
            syncPdfUrl(true);
            if (!pdf.items.length) loadGrid(false);
        }
    }

    // ════════════════════════════════════════════════════════════════════════
    // 教材下载
    // ════════════════════════════════════════════════════════════════════════
    const dl = {
        built: false, stage: null, subject: '', publisher: '', q: '', row: '', page: 1, total: 0,
        listCtl: null, facetCtl: null, active: 0, dom: {},
    };

    function buildDownloads() {
        const d = dl.dom;
        const select = (id, label) => {
            const node = el('select', { id });
            return [el('div', { class: 'filter-group' }, [el('label', { for: id, text: label }), node]), node];
        };
        const [stageGroup, stageSel] = select('lib-dl-stage', '学段');
        const [subjectGroup, subjectSel] = select('lib-dl-subject', '学科');
        const [publisherGroup, publisherSel] = select('lib-dl-publisher', '出版社 / 版别');
        d.stage = stageSel; d.subject = subjectSel; d.publisher = publisherSel;
        d.q = el('input', { type: 'search', id: 'lib-dl-q', class: 'lib-input', maxlength: '80', autocomplete: 'off', placeholder: '书名，例如：必修 上册' });
        d.heroSub = el('p', { class: 'gaokao-hero-sub', text: '目录加载中…' });
        d.notices = el('div', { class: 'lib-notices' });
        d.status = el('div', { class: 'lib-status', 'aria-live': 'polite' });
        d.list = el('div', { class: 'lib-dl-list' });
        d.prev = el('button', { class: 'lib-btn', type: 'button', text: '‹ 上一页' });
        d.next = el('button', { class: 'lib-btn', type: 'button', text: '下一页 ›' });
        d.pageInfo = el('span', { class: 'lib-pager-info' });
        d.pager = el('nav', { class: 'lib-pager hidden', 'aria-label': '分页' }, [d.prev, d.pageInfo, d.next]);
        viewDownloads.append(
            el('section', { class: 'gaokao-hero lib-hero' }, [el('h2', { text: '教材下载' }), d.heroSub]),
            el('form', { class: 'gaokao-filters lib-filters', role: 'search' }, [
                stageGroup, subjectGroup, publisherGroup,
                el('div', { class: 'filter-group lib-grow' }, [el('label', { for: 'lib-dl-q', text: '书名' }), d.q]),
            ]),
            d.notices, d.status, d.list, d.pager);
        viewDownloads.querySelector('form').addEventListener('submit', event => event.preventDefault());
        d.stage.addEventListener('change', () => { dl.stage = d.stage.value; dl.subject = ''; dl.publisher = ''; dl.page = 1; dl.row = ''; refreshDownloads(true); });
        d.subject.addEventListener('change', () => { dl.subject = d.subject.value; dl.publisher = ''; dl.page = 1; dl.row = ''; refreshDownloads(true); });
        d.publisher.addEventListener('change', () => { dl.publisher = d.publisher.value; dl.page = 1; dl.row = ''; refreshDownloads(false); });
        d.q.addEventListener('input', debounce(() => { dl.q = d.q.value.trim(); dl.page = 1; dl.row = ''; refreshDownloads(false); }, 300));
        d.prev.addEventListener('click', () => { if (dl.page > 1) { dl.page--; refreshDownloads(false, true); } });
        d.next.addEventListener('click', () => { if (dl.page * DOWNLOAD_PAGE_SIZE < dl.total) { dl.page++; refreshDownloads(false, true); } });
    }

    function fillSelect(node, allLabel, options, value) {
        clear(node);
        node.append(el('option', { value: '', text: allLabel }));
        let found = false;
        for (const option of options) {
            const selected = option.name === value;
            found = found || selected;
            node.append(el('option', { value: option.name, selected, text: `${option.name}（${option.count}）` }));
        }
        node.value = found ? value : '';
        return found ? value : '';
    }

    function syncDownloadsUrl(push) {
        if (currentView() !== 'downloads') return;
        setUrl({
            view: 'downloads', stage: dl.stage === '' ? 'all' : dl.stage, subject: dl.subject,
            publisher: dl.publisher, q: dl.q, row: dl.row, page: dl.page > 1 ? dl.page : '',
        }, push);
    }

    async function refreshDownloads(reloadFacets, scroll) {
        const d = dl.dom;
        if (reloadFacets || dl.stage === null) {
            if (dl.facetCtl) dl.facetCtl.abort();
            const ctl = dl.facetCtl = new AbortController();
            try {
                let facets = await api('/api/library/downloads/facets', { stage: dl.stage || '', subject: dl.subject, region: REGION }, ctl.signal);
                if (dl.stage === null) {
                    // First visit without ?stage=: start on this page's 学段 when the catalog has it.
                    dl.stage = facets.stages.some(s => s.name === PAGE_PHASE) ? PAGE_PHASE : '';
                    if (dl.stage) facets = await api('/api/library/downloads/facets', { stage: dl.stage, subject: dl.subject, region: REGION }, ctl.signal);
                }
                if (ctl.signal.aborted) return;
                dl.stage = fillSelect(d.stage, '全部学段', facets.stages, dl.stage);
                dl.subject = fillSelect(d.subject, '全部学科', facets.subjects, dl.subject);
                dl.publisher = fillSelect(d.publisher, '全部出版社', facets.publishers, dl.publisher);
                d.heroSub.textContent = `共收录 ${facets.total} 册教材 PDF · 选好学段、学科后直接下载`;
            } catch (error) {
                if (error.kind === 'aborted') return;
                if (dl.stage === null) dl.stage = '';
                clear(d.notices); clear(d.list);
                d.status.textContent = '';
                d.pager.classList.add('hidden');
                d.heroSub.textContent = error.kind === 'unavailable' ? '目录尚未发布' : '目录加载失败';
                const box = errorNotice(error, () => { clear(d.notices); dl.stage = dl.stage || null; refreshDownloads(true); });
                if (box) d.notices.append(box);
                return;
            }
        }
        syncDownloadsUrl(false);
        if (dl.listCtl) dl.listCtl.abort();
        const ctl = dl.listCtl = new AbortController();
        d.status.textContent = '加载中…';
        try {
            const data = await api('/api/library/downloads', {
                stage: dl.stage, subject: dl.subject, publisher: dl.publisher, q: dl.q, region: REGION, row: dl.row,
                offset: (dl.page - 1) * DOWNLOAD_PAGE_SIZE, limit: DOWNLOAD_PAGE_SIZE,
            }, ctl.signal);
            if (ctl.signal.aborted) return;
            dl.total = data.total;
            clear(d.notices); clear(d.list);
            resetCoverQueue();
            if (dl.row) {
                d.notices.append(notice('warn', dl.total ? '只显示指定的这一个条目。' : '没有找到指定的条目（目录可能已更新）。', [
                    el('button', { class: 'lib-btn', type: 'button', text: '显示全部', onclick: () => { dl.row = ''; dl.page = 1; refreshDownloads(true); } })]));
            }
            const pages = Math.max(1, Math.ceil(dl.total / DOWNLOAD_PAGE_SIZE));
            if (dl.page > pages) { dl.page = pages; return refreshDownloads(false); }
            for (const row of data.downloads) d.list.append(downloadCard(row));
            d.status.textContent = dl.total ? `共 ${dl.total} 册` : '没有符合条件的教材，请调整筛选条件。';
            d.pageInfo.textContent = `第 ${dl.page} / ${pages} 页`;
            d.prev.disabled = dl.page <= 1;
            d.next.disabled = dl.page >= pages;
            d.pager.classList.toggle('hidden', pages <= 1);
            if (scroll) viewDownloads.scrollIntoView({ block: 'start' });
        } catch (error) {
            if (error.kind === 'aborted') return;
            d.status.textContent = '';
            clear(d.notices);
            const box = errorNotice(error, () => { clear(d.notices); refreshDownloads(false); });
            if (box) d.notices.append(box);
        }
    }

    // Covers are decoration.  The official cover files are large (about 1 MB each) and slow
    // to start, so: the subject placeholder is always shown first; images start only when a
    // card nears the viewport; at most COVER_CONCURRENCY load at once; one that has not
    // arrived within COVER_TIMEOUT_MS is abandoned (the placeholder stays, marked as such);
    // nothing is loaded when the browser asks to save data.
    const coverQueue = { waiting: [], active: 0, generation: 0, observer: null };

    function resetCoverQueue() {
        coverQueue.generation++;
        coverQueue.waiting = [];
        coverQueue.active = 0;
        if (coverQueue.observer) coverQueue.observer.disconnect();
        coverQueue.observer = ('IntersectionObserver' in window) ? new IntersectionObserver(entries => {
            for (const entry of entries) {
                if (!entry.isIntersecting) continue;
                coverQueue.observer.unobserve(entry.target);
                coverQueue.waiting.push(entry.target);
            }
            pumpCovers();
        }, { rootMargin: '200px' }) : null;
    }

    function pumpCovers() {
        while (coverQueue.active < COVER_CONCURRENCY && coverQueue.waiting.length) {
            const box = coverQueue.waiting.shift();
            const url = externalUrl(box.dataset.cover);
            if (!url || !box.isConnected) continue;
            const generation = coverQueue.generation;
            coverQueue.active++;
            box.dataset.coverState = 'loading';
            const img = el('img', { alt: '', decoding: 'async', referrerpolicy: 'no-referrer' });
            let settled = false;
            const done = ok => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                if (ok) {
                    box.append(img);
                    box.dataset.coverState = 'loaded';
                } else {
                    img.removeAttribute('src');   // stop the transfer
                    box.dataset.coverState = 'none';
                    box.title = '封面未能加载';
                }
                if (generation === coverQueue.generation) { coverQueue.active--; pumpCovers(); }
            };
            const timer = setTimeout(() => done(false), COVER_TIMEOUT_MS);
            img.addEventListener('load', () => done(true), { once: true });
            img.addEventListener('error', () => done(false), { once: true });
            img.src = url;
        }
    }

    function coverNode(row) {
        const box = el('div', { class: 'lib-cover', 'aria-hidden': 'true' }, el('span', { text: (row.subject || '教材').slice(0, 2) }));
        const saveData = navigator.connection && navigator.connection.saveData;
        if (externalUrl(row.cover) && !saveData && coverQueue.observer) {
            box.dataset.cover = row.cover;
            box.dataset.coverState = 'waiting';
            coverQueue.observer.observe(box);
        } else {
            box.dataset.coverState = 'none';
        }
        return box;
    }

    function openDownloadRow(rowId) {
        activateView('downloads');
        if (!dl.built) { dl.built = true; buildDownloads(); }
        dl.stage = ''; dl.subject = ''; dl.publisher = ''; dl.q = ''; dl.page = 1; dl.row = rowId;
        dl.dom.q.value = '';
        syncDownloadsUrl(true);
        refreshDownloads(true, true);
    }

    function previewLink(editionId, text) {
        return el('a', {
            class: 'lib-link', href: pdfHref(editionId, 1, ''), text,
            onclick: event => {
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
                event.preventDefault();
                openEdition(editionId, 1, { push: true });
            },
        });
    }

    function sourceRole(source) {
        if (source.platform_current === true) return '平台现行文件';
        if (source.platform_current === false) {
            const day = dayOf(source.acquired_at);
            return '较早取得' + (day ? ` ${day}` : '');
        }
        return '';
    }

    function downloadCard(row) {
        const state = el('div', { class: 'lib-dl-state', 'aria-live': 'polite' });
        const actions = el('div', { class: 'lib-dl-actions' });
        const sources = (row.sources || []).filter(s => s.same_origin || externalUrl(s.url));
        const usable = sources.filter(s => s.available !== false);
        // Every file held on this site gets its OWN labelled button (平台现行文件 / 较早取得):
        // no single button ever picks between two files.
        const local = usable.filter(s => s.kind === 'vps');
        const primary = local.length ? null : usable[0];
        const others = row.other_editions || [];
        const notCurrent = row.platform_current === false && local.length <= 1;
        const describe = s => s.label + (sourceRole(s) ? `（${sourceRole(s)}）` : '') + (s.available === false ? '（暂不可用）' : '');
        if (!local.length && !primary) {
            actions.append(el('button', { class: 'lib-btn lib-btn-primary', type: 'button', disabled: true, text: '暂无下载来源' }));
            state.textContent = sources.length ? '已登记的来源目前都不可用。' : '本册尚未取得文件，暂时无法下载。';
        } else {
            for (const source of local) {
                const group = el('div', { class: 'lib-dl-source' }, [sourceControl(row, source, state, true)]);
                const facts = [formatBytes(source.bytes), source.pages ? `${source.pages} 页` : ''].filter(Boolean).join(' · ');
                if (facts) group.append(el('span', { class: 'lib-dl-source-facts', text: facts }));
                if (source.previewable !== false && source.edition_id) group.append(previewLink(source.edition_id, '预览'));
                actions.append(group);
            }
            if (primary) actions.append(sourceControl(row, primary, state, true));
            const rest = sources.filter(s => s !== primary && local.indexOf(s) === -1);
            if (rest.length) {
                actions.append(el('details', { class: 'lib-menu' }, [
                    el('summary', { class: 'lib-btn', text: '其他来源' }),
                    el('div', { class: 'lib-menu-list' }, rest.map(s => s.available === false
                        ? el('span', { class: 'lib-muted', text: describe(s) })
                        : sourceControl(row, s, state, false))),
                ]));
            }
            state.textContent = '来源：' + sources.map(describe).join('、')
                + (local.length ? '' : ' · 将在新标签页打开，由浏览器显示下载进度');
        }

        // Which file is this?  Two files of one book never share a card, and each says what it is.
        const chips = [];
        if (notCurrent) chips.push(el('span', { class: 'lib-badge lib-badge-warn', text: '非平台现行版', title: '本站保存的这一版次不是官方平台目前提供的文件' }));
        if (!row.edition_id && others.length) chips.push(el('span', { class: 'lib-badge', text: '官方平台现行文件 · 未存于本站' }));
        const lines = [];
        if (local.length >= 2) {
            lines.push(el('p', { class: 'lib-dl-meta lib-dl-edition-note', text: '本站保存了这本书的两份文件，是不同版本，页数与页码不能互用；请按按钮上的标注选择。' }));
        }
        if (notCurrent) {
            const got = monthOf(row.retrieved_at);
            const newer = row.newer_edition;
            const line = el('p', { class: 'lib-dl-meta lib-dl-edition-note' },
                `本条目只提供本站保存的这一版次${got ? `（取得于 ${got}）` : ''}。`);
            if (newer) {
                line.append('官方平台已有更新版本');
                if (newer.download_row_id) {
                    line.append('：', el('a', {
                        class: 'lib-link', href: downloadsRowHref(newer.download_row_id),
                        text: `另一版次的条目（${differentEditionText(newer)}）`,
                        onclick: event => {
                            if (event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
                            event.preventDefault();
                            openDownloadRow(newer.download_row_id);
                        },
                    }));
                } else {
                    line.append(`（不同版次：${differentEditionText(newer)}）。`);
                }
            }
            lines.push(line);
        }
        for (const other of others) {
            // A different edition held on this site: its own entry, never merged into this one.
            const got = monthOf(other.retrieved_at);
            const label = `${other.edition_label || '版次未标注'}${other.platform_current === false ? '，非平台现行版' : ''}`
                + `${other.status === 'superseded' ? '，旧版' : ''}${got ? `，取得于 ${got}` : ''}`;
            lines.push(el('p', { class: 'lib-dl-meta lib-dl-edition-note' }, ['本站另存不同版次（与本条目不是同一份文件）：',
                other.previewable ? previewLink(other.edition_id, label + ' · 预览') : label]));
        }
        return el('article', { class: 'lib-dl-card' }, [
            coverNode(row),
            el('div', { class: 'lib-dl-body' }, [
                el('h3', { class: 'lib-dl-title', text: row.title }),
                chips.length ? el('p', { class: 'lib-badges' }, chips) : null,
                el('p', { class: 'lib-dl-meta', text: [row.stage, row.subject, row.publisher].filter(Boolean).join(' · ') }),
                el('p', { class: 'lib-dl-meta', text: `版次：${row.edition_label || '未标注'} · 大小：${formatBytes(row.size_bytes) || '未知'}` }),
                actions, state,
            ].concat(lines)),
        ]);
    }

    function sourceControl(row, source, state, isPrimary) {
        const cls = isPrimary ? 'lib-btn lib-btn-primary lib-dl-btn' : 'lib-btn';
        const role = sourceRole(source);
        const tag = source.label + (role ? ' · ' + role : '');
        const label = isPrimary ? '⬇ 下载' : `从${source.label}下载${role ? `（${role}）` : ''}`;
        if (!source.same_origin) {
            return el('a', { class: cls, href: externalUrl(source.url) || '#', target: '_blank', rel: 'noopener noreferrer' },
                [label, isPrimary ? el('small', { text: tag }) : null]);
        }
        const button = el('button', { class: cls, type: 'button' }, [label, isPrimary ? el('small', { text: tag }) : null]);
        button.addEventListener('click', () => startInlineDownload(row, source, state, button));
        return button;
    }

    function filenameFrom(response, fallback) {
        const header = response.headers.get('Content-Disposition') || '';
        const match = /filename\*=UTF-8''([^;]+)/i.exec(header);
        if (match) {
            try { return decodeURIComponent(match[1]).replace(/[\\/:*?"<>|\x00-\x1f]/g, ' ').trim() || fallback; } catch (_) { /* fall through */ }
        }
        return fallback;
    }

    function browserDownload(url) {
        const link = el('a', { href: url, download: true, class: 'hidden' });
        document.body.append(link);
        link.click();
        link.remove();
    }

    async function startInlineDownload(row, source, state, button) {
        const size = source.bytes || row.size_bytes || 0;
        // Default: let the browser download the ?download=1 URL itself (no JS buffering).
        // The in-page progress view is used only for small files of known size, one at a time.
        if (!size || size > INLINE_DOWNLOAD_MAX_BYTES || !window.ReadableStream || !window.Blob || dl.active >= 1) {
            browserDownload(source.url);
            state.textContent = '已交给浏览器下载，进度请看浏览器的下载列表。';
            return;
        }
        dl.active++;
        button.disabled = true;
        const ctl = new AbortController();
        const bar = el('progress', { class: 'lib-progress', max: '100', value: '0' });
        const text = el('span', { class: 'lib-progress-text', text: '正在连接…' });
        clear(state);
        state.append(bar, text, el('button', { class: 'lib-btn', type: 'button', text: '取消', onclick: () => ctl.abort() }));
        const finish = nodes => { clear(state); state.append.apply(state, nodes); button.disabled = false; dl.active--; };
        try {
            let response;
            try {
                response = await fetch(source.url, { signal: ctl.signal, credentials: 'omit' });
            } catch (error) {
                if (error && error.name === 'AbortError') throw new LibError('aborted', '已取消');
                throw new LibError('network', '网络连接失败。');
            }
            if (!response.ok) throw statusError(response.status, response.headers.get('Retry-After'), 'file');
            const total = parseInt(response.headers.get('Content-Length'), 10) || size || 0;
            const reader = response.body.getReader();
            const chunks = [];
            let received = 0;
            let shown = 0;
            for (;;) {
                let step;
                try {
                    step = await reader.read();
                } catch (error) {
                    if (ctl.signal.aborted) throw new LibError('aborted', '已取消');
                    throw new LibError('network', '下载中断：网络连接失败。');
                }
                if (step.done) break;
                chunks.push(step.value);
                received += step.value.length;
                const now = Date.now();
                if (now - shown > 150) {
                    shown = now;
                    if (total) {
                        bar.value = Math.min(100, received / total * 100);
                        text.textContent = `已下载 ${formatBytes(received)} / ${formatBytes(total)}（${Math.floor(received / total * 100)}%）`;
                    } else {
                        bar.removeAttribute('value');
                        text.textContent = `已下载 ${formatBytes(received)}`;
                    }
                }
            }
            if (total && received !== total) throw new LibError('network', '下载不完整，请重试。');
            const name = filenameFrom(response, (row.title || '教材') + '.pdf');
            const blobUrl = URL.createObjectURL(new Blob(chunks, { type: 'application/pdf' }));
            const link = el('a', { href: blobUrl, download: name, class: 'hidden' });
            document.body.append(link);
            link.click();
            link.remove();
            setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
            finish([el('span', { class: 'lib-ok', text: `下载完成：${name}（${formatBytes(received)}）` })]);
        } catch (error) {
            const retry = el('button', { class: 'lib-btn', type: 'button', text: '重试', onclick: () => startInlineDownload(row, source, state, button) });
            const direct = el('a', { class: 'lib-link', href: source.url, download: true, text: '改用浏览器直接下载' });
            if (error && error.kind === 'aborted') finish([el('span', { text: '已取消下载。' }), retry]);
            else finish([el('span', { class: 'lib-err', text: '下载失败：' + ((error && error.message) || '未知错误') }), retry, direct]);
        }
    }

    function enterDownloads(fromUrl) {
        if (!dl.built) { dl.built = true; buildDownloads(); }
        if (fromUrl) {
            const q = new URLSearchParams(location.search);
            const stage = q.get('stage');
            dl.stage = stage === null ? null : (stage === 'all' ? '' : stage.slice(0, 60));
            dl.subject = (q.get('subject') || '').slice(0, 60);
            dl.publisher = (q.get('publisher') || '').slice(0, 60);
            dl.q = (q.get('q') || '').slice(0, 80);
            dl.row = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$/.test(q.get('row') || '') ? q.get('row') : '';
            dl.page = /^\d{1,4}$/.test(q.get('page') || '') ? Math.max(1, +q.get('page')) : 1;
            dl.dom.q.value = dl.q;
            refreshDownloads(true);
            return;
        }
        syncDownloadsUrl(true);
        if (!dl.dom.list.firstChild && !dl.dom.notices.firstChild) refreshDownloads(true);
    }

    // ════════════════════════════════════════════════════════════════════════
    // Search-result integration: 「在 PDF 中打开」
    // ════════════════════════════════════════════════════════════════════════
    let decorateCtl = null;
    async function decorateSearchResults(root, groups) {
        if (decorateCtl) decorateCtl.abort();
        const ctl = decorateCtl = new AbortController();
        try {
            const cards = root.querySelectorAll('.result-card');
            const flat = [];
            for (const group of groups || []) for (const r of group.results || []) flat.push(r);
            if (!cards.length || cards.length !== flat.length) return;
            const wanted = new Map();   // item -> [card indexes]
            flat.forEach((r, index) => {
                if (r.source === 'gaokao' || !r.page_url || !Number.isInteger(r.section)) return;
                const match = /\/pages\/([0-9a-f]{12})\/p(\d+)\.webp$/.exec(r.page_url);
                if (!match || +match[2] !== r.section) return;
                const item = match[1] + '.' + r.section;
                if (!wanted.has(item)) {
                    if (wanted.size >= RESOLVE_BATCH) return;
                    wanted.set(item, []);
                }
                wanted.get(item).push(index);
            });
            if (!wanted.size) return;
            const release = await getRelease(ctl.signal);
            if (!release.available || ctl.signal.aborted) return;
            // One request per results page.
            const data = await api('/api/library/resolve', { items: Array.from(wanted.keys()).join(',') }, ctl.signal);
            if (ctl.signal.aborted) return;
            const query = currentQueryText();
            for (const result of data.results || []) {
                if (!result.available) continue;
                for (const index of wanted.get(result.item) || []) {
                    const card = cards[index];
                    if (!card || !card.isConnected || card.querySelector('.lib-open-pdf')) continue;
                    let actions = card.querySelector('.result-actions');
                    if (!actions) { actions = el('div', { class: 'result-actions' }); card.append(actions); }
                    const otherPrinting = result.text_identity === OTHER_PRINTING;
                    actions.append(el('a', {
                        class: 'view-page-btn lib-open-pdf', href: pdfHref(result.edition_id, result.pdf_page, query),
                        title: `在 PDF 预览中打开（PDF 第 ${result.pdf_page} 页）` + (otherPrinting ? '。' + OTHER_PRINTING_NOTE : ''),
                        text: otherPrinting ? '📕 在 PDF 中打开对应页' : '📕 在 PDF 中打开',
                        onclick: event => {
                            event.stopPropagation();
                            if (event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
                            event.preventDefault();
                            openEdition(result.edition_id, result.pdf_page, { push: true, query, via: 'search' });
                        },
                    }));
                    if (otherPrinting) actions.append(el('span', { class: 'lib-open-note', text: OTHER_PRINTING_NOTE }));
                }
            }
        } catch (_) { /* the existing result list keeps working without the extra link */ }
    }

    function currentQueryText() {
        const input = document.getElementById('search-input');
        return input ? input.value.trim().slice(0, 80) : '';
    }

    // ════════════════════════════════════════════════════════════════════════
    // Routing: ?view=, legacy JC / JKS links, keyboard
    // ════════════════════════════════════════════════════════════════════════
    async function openLegacy(kind, ref, page) {
        activateView('pdf');
        ensurePdfBuilt();
        try {
            const data = await api('/api/library/legacy', { kind, ref, page });
            if (data.available && data.exact) {
                openEdition(data.edition_id, data.pdf_page, { via: 'legacy' });
            } else if (data.available) {
                openEdition(data.edition_id, 1, {
                    via: 'legacy',
                    notice: data.reason === 'no-page'
                        ? '已从旧链接打开本册。'
                        : '旧链接中的页码无法确切对应到这份 PDF，已打开第 1 页。请用目录或册内检索定位。',
                });
            } else {
                pdf.pendingNotice = data.reason === 'ambiguous'
                    ? '旧链接对应多个版次，无法确定是哪一册，请在下方书目中选择。'
                    : '旧链接指向的教材目前没有 PDF 预览，可以在下方书目或「教材下载」中查找。';
                showGrid(false);
            }
        } catch (error) {
            pdf.pendingNotice = '旧链接无法解析：' + ((error && error.message) || '未知错误');
            showGrid(false);
        }
    }

    function route() {
        const q = new URLSearchParams(location.search);
        // Old JC links: #book=<book id>&page=<0-based image index>
        if (location.hash.length > 1 && location.hash.indexOf('book=') !== -1) {
            const hash = new URLSearchParams(location.hash.slice(1));
            const book = hash.get('book');
            if (book) return openLegacy('jc', book.slice(0, 400), hash.get('page') || '');
        }
        // Old JKS links, query form: ?jks=<slug>&page=<1-based PDF position>
        const jks = q.get('jks');
        if (jks && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(jks)) return openLegacy('jks', jks, q.get('page') || '');
        const view = q.get('view');
        if (view === 'pdf') { if (activateView('pdf')) enterPdf(true); return; }
        if (view === 'downloads') { if (activateView('downloads')) enterDownloads(true); return; }
        if (OTHER_VIEWS.indexOf(view) !== -1) { activateView(view); return; }
        if (!view && (currentView() === 'pdf' || currentView() === 'downloads')) activateView('search');
    }

    document.querySelectorAll('.nav-btn[data-view]').forEach(button => {
        button.addEventListener('click', () => {
            const name = button.dataset.view;
            if (name !== 'pdf') cancelRender();
            if (silentNav) return;
            if (name === 'pdf') enterPdf(false);
            else if (name === 'downloads') enterDownloads(false);
            else setUrl(name === 'search' ? {} : { view: name }, true);
        });
    });

    // In-page links to one of the two tabs (e.g. 「教材 PDF 下载」 on the 真题 view): switch
    // without a reload.  The href (?view=…) still works on its own.
    document.addEventListener('click', event => {
        if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
        const link = event.target && event.target.closest ? event.target.closest('a[data-lib-view]') : null;
        const name = link && link.dataset.libView;
        if (name !== 'pdf' && name !== 'downloads') return;
        if (!activateView(name)) return;
        event.preventDefault();
        if (name === 'pdf') enterPdf(false); else enterDownloads(false);
        window.scrollTo(0, 0);
    });

    window.addEventListener('popstate', route);

    document.addEventListener('keydown', event => {
        if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
        if (document.getElementById('page-viewer')) return;   // SUN's page-image viewer owns the keys
        const view = currentView();
        if (view !== 'pdf' && view !== 'downloads') return;
        const target = event.target;
        const typing = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable);
        if (event.key === 'Escape') {
            if (typing) { target.blur(); return; }
            if (view === 'pdf' && pdf.mode === 'viewer') {
                if (pdf.dom.viewerRoot.classList.contains('lib-side-open')) toggleSide(false);
                else showGrid(true);
            }
            return;
        }
        if (typing) return;
        if (event.key === '/') {
            const input = view === 'downloads' ? dl.dom.q
                : (pdf.mode === 'viewer' ? (pdf.dom.searchInput.disabled ? pdf.dom.pageInput : pdf.dom.searchInput) : pdf.dom.titleInput);
            if (input) { event.preventDefault(); input.focus(); if (input.select) input.select(); }
            return;
        }
        if (view === 'pdf' && pdf.mode === 'viewer' && pdf.doc) {
            if (event.key === 'ArrowLeft') { event.preventDefault(); goPage(pdf.page - 1); }
            else if (event.key === 'ArrowRight') { event.preventDefault(); goPage(pdf.page + 1); }
        }
    });

    window.addEventListener('resize', debounce(() => {
        if (currentView() === 'pdf' && pdf.mode === 'viewer' && pdf.doc && pdf.zoomMode === 'fit'
            && Math.abs(pdf.dom.stage.clientWidth - pdf.lastStageWidth) > 8) renderPage();
    }, 200));

    window.SunLibrary = {
        decorateSearchResults,
        openEdition: (editionId, page, query) => openEdition(editionId, page, { push: true, query }),
    };

    route();
})();
