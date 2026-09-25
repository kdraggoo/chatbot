// /srv/chatbot/app/dashboard.js

const API_KEY_STORAGE_KEY = 'admin_api_key';  // shared with admin.js
const DAYS_STORAGE_KEY = 'dashboard_days';
const SVG_NS = 'http://www.w3.org/2000/svg';

const $ = (id) => document.getElementById(id);
let days = 30;
let lastStats = null;

// localStorage can be missing or throw (private windows, blocked storage)
function storageGet(key) {
    try { return localStorage.getItem(key) || ''; } catch { return ''; }
}
function storageSet(key, value) {
    try {
        if (value) localStorage.setItem(key, value);
        else localStorage.removeItem(key);
    } catch { /* keep working without persistence */ }
}

let apiKey = storageGet(API_KEY_STORAGE_KEY);
days = [7, 30, 90, 365].includes(Number(storageGet(DAYS_STORAGE_KEY))) ? Number(storageGet(DAYS_STORAGE_KEY)) : 30;

function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === 'class') node.className = v;
        else node.setAttribute(k, v);
    }
    for (const child of children) {
        if (child == null) continue;
        node.append(child instanceof Node ? child : String(child));
    }
    return node;
}

function svg(tag, attrs = {}) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
}

const fmtInt = (n) => (n ?? 0).toLocaleString();
const fmtPct = (part, whole) => (whole ? `${Math.round((part / whole) * 100)}%` : '—');
function fmtMs(ms) {
    if (ms == null) return '—';
    return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}
function fmtWhen(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function fmtDay(iso, opts = { month: 'short', day: 'numeric' }) {
    return new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, { ...opts, timeZone: 'UTC' });
}

function table(headers, rows, emptyText) {
    if (!rows.length) return el('p', { class: 'empty' }, emptyText);
    const thead = el('thead', {}, el('tr', {}, ...headers.map(([label, cls]) => el('th', cls ? { class: cls } : {}, label))));
    const tbody = el('tbody', {}, ...rows.map((cells) => el('tr', {}, ...cells.map(([value, cls]) => el('td', cls ? { class: cls } : {}, value)))));
    return el('table', {}, thead, tbody);
}

function setRangeButtons() {
    document.querySelectorAll('[data-days]').forEach((b) => {
        b.setAttribute('aria-pressed', String(Number(b.dataset.days) === days));
    });
}

function showAuth(message) {
    $('auth').classList.add('show');
    $('content').hidden = true;
    $('message').textContent = message || '';
    $('updated').textContent = 'Locked';
    $('apiKey').focus();
}

async function load() {
    setRangeButtons();
    $('updated').textContent = 'Loading…';
    let res;
    try {
        res = await fetch(`/chatbot/api/admin/stats?days=${days}`, {
            headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
        });
    } catch (e) {
        $('updated').textContent = `Could not reach the API: ${e.message}`;
        return;
    }
    if (res.status === 401) {
        showAuth(apiKey ? 'That key was rejected.' : '');
        return;
    }
    if (!res.ok) {
        $('updated').textContent = `API error ${res.status}`;
        return;
    }
    lastStats = await res.json();
    $('auth').classList.remove('show');
    $('content').hidden = false;
    render(lastStats);
}

function render(s) {
    const u = s.usage;
    let since = s.logging_since ? `question log started ${fmtWhen(Date.parse(s.logging_since) / 1000)}` : 'no questions logged yet';
    if (s.backfilled_since) since += ` · earlier requests back to ${fmtDay(s.backfilled_since.slice(0, 10))} from nginx logs`;
    $('updated').textContent = `Updated ${new Date(s.generated_at).toLocaleTimeString()} · ${since}`;

    // Services
    const services = $('services');
    services.replaceChildren(...[['API', s.health.api], ['Qdrant', s.health.qdrant], ['Ollama', s.health.ollama]].map(([name, h]) =>
        el('div', { class: 'service' },
            el('div', { class: 'name' }, name),
            el('div', { class: `state ${h.ok ? 'ok' : 'bad'}` }, h.ok ? '✓ OK' : '✗ DOWN'),
            el('div', { class: 'detail' }, h.detail))));

    // Tiles
    $('usageTitle').textContent = `Usage · last ${s.days} days`;
    const tile = (label, value, note, hero) =>
        el('div', { class: `tile${hero ? ' hero' : ''}` }, el('div', { class: 'label' }, label), el('div', { class: 'value' }, value), note ? el('div', { class: 'note' }, note) : null);
    $('tiles').replaceChildren(
        tile('Questions', fmtInt(u.questions), `${fmtInt(u.answered)} answered`, true),
        tile('Median response', fmtMs(u.p50_ms), 'answered questions'),
        tile('95th percentile', fmtMs(u.p95_ms), 'slowest 5% take longer'),
        tile('No relevant context', fmtPct(u.no_context, u.context_known), `${fmtInt(u.no_context)} of ${fmtInt(u.context_known)} answered`),
        tile('Errors', fmtInt(u.errors), `${fmtPct(u.errors, u.questions)} · ${fmtInt(u.aborted)} abandoned`),
    );

    // Chart + table view
    const peak = u.daily.reduce((a, d) => (d.total > a.total ? d : a), u.daily[0]);
    $('chartSub').textContent = peak && peak.total
        ? `Busiest day: ${fmtDay(peak.date)} with ${fmtInt(peak.total)}. Days in UTC.`
        : 'No questions in this range yet. Days in UTC.';
    drawUsageChart(u.daily);
    $('dailyTable').replaceChildren(table(
        [['Day'], ['Questions', 'num'], ['Errors', 'num']],
        u.daily.slice().reverse().map((d) => [[fmtDay(d.date, { weekday: 'short', month: 'short', day: 'numeric' })], [fmtInt(d.total), 'num'], [fmtInt(d.errors), 'num']]),
        'No data.'));

    // Recent questions
    const statusCell = (r) => {
        if (r.status === 'ok') return [r.chunks_used === 0 ? '✓ no context' : '✓ answered', 'status-ok'];
        if (r.status === 'error') return ['✗ error', 'status-error'];
        return ['– abandoned', 'status-aborted'];
    };
    $('recent').replaceChildren(table(
        [['When'], ['Question'], ['Result'], ['Time', 'num'], ['Chunks', 'num'], ['Top score', 'num']],
        u.recent.map((r) => [
            [fmtWhen(r.ts), 'when'],
            [r.query, r.source === 'nginx' ? 'q muted' : 'q'],
            statusCell(r),
            [fmtMs(r.duration_ms), 'num'],
            [r.chunks_used ?? '—', 'num'],
            [r.top_score != null ? r.top_score.toFixed(3) : '—', 'num'],
        ]),
        'No questions in this range yet.'));

    $('top').replaceChildren(table(
        [['Question'], ['Asked', 'num']],
        u.top_questions.map((q) => [[q.query, 'q'], [fmtInt(q.count), 'num']]),
        'No questions in this range yet.'));

    renderMonitoring(s.monitoring);

    // Settings
    const c = s.config;
    $('config').replaceChildren(...[
        ['Answer model', c.gen_model],
        ['Embedding model', c.embed_model],
        ['Collection', c.collection],
        ['Min similarity', c.min_similarity_score],
        ['Max context chunks', c.max_context_chunks],
        ['Rate limit', c.rate_limit],
        ['Log kept for', `${c.retention_days} days`],
    ].flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, v)]));

    // Knowledge base
    const kb = s.knowledge_base;
    if (!kb.ok) {
        $('kbTitle').textContent = 'Knowledge base';
        $('kb').replaceChildren(el('p', { class: 'empty' }, `Could not read the collection: ${kb.detail}`));
    } else {
        $('kbTitle').textContent = `Knowledge base · ${fmtInt(kb.total_documents)} documents · ${fmtInt(kb.total_chunks)} chunks`;
        $('kb').replaceChildren(table(
            [['Document'], ['Source'], ['Chunks', 'num'], ['Updated']],
            kb.documents.map((d) => [
                [d.title, 'q'],
                [d.source_path],
                [fmtInt(d.chunk_count), 'num'],
                [d.updated_at && d.updated_at !== 'unknown' ? new Date(d.updated_at).toLocaleDateString() : '—', 'when'],
            ]),
            'The collection is empty.'));
    }
}

function niceMax(v) {
    if (v <= 4) return 4;
    const step = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * step >= v) return m * step;
    return 10 * step;
}

function drawUsageChart(daily) {
    drawChart($('chart'), daily, {
        label: 'Questions per day',
        value: (d) => d.total,
        tooltip: (d) => [
            `${fmtInt(d.total)} question${d.total === 1 ? '' : 's'}`,
            d.errors ? `${fmtInt(d.errors)} error${d.errors === 1 ? '' : 's'}` : null,
        ],
    });
}

function drawProbeChart(daily) {
    // Bar = median response time; a day with only failed runs gets a full-height red bar
    const maxMs = Math.max(0, ...daily.map((d) => d.median_ms || 0));
    drawChart($('probeChart'), daily, {
        label: 'Monitoring probe median response time per day',
        value: (d) => (d.runs && d.median_ms == null ? maxMs || 1000 : d.median_ms || 0),
        axis: (v) => fmtMs(v),
        bad: (d) => d.failures > 0,
        tooltip: (d) => d.runs ? [
            `${fmtInt(d.runs)} run${d.runs === 1 ? '' : 's'}, median ${fmtMs(d.median_ms)}`,
            d.failures ? `${fmtInt(d.failures)} failed` : null,
            d.no_context ? `${fmtInt(d.no_context)} found no context` : null,
        ] : ['No probe runs'],
    });
}

function renderMonitoring(p) {
    const tile = (label, value, note, cls = '') =>
        el('div', { class: `tile ${cls}` }, el('div', { class: 'label' }, label), el('div', { class: 'value' }, value), note ? el('div', { class: 'note' }, note) : null);
    const last = p.last;
    $('probeTiles').replaceChildren(
        last
            ? tile('Last probe', last.status === 'ok' ? '✓ OK' : '✗ FAILED', `${fmtWhen(last.ts)} · ${fmtMs(last.duration_ms)}`, last.status === 'ok' ? 'good' : 'badtile')
            : tile('Last probe', '—', 'No probe runs in this range'),
        tile('Success rate', fmtPct(p.runs - p.failures, p.runs), `${fmtInt(p.failures)} failed of ${fmtInt(p.runs)} runs`),
        tile('Median response', fmtMs(p.p50_ms), 'successful runs'),
        tile('95th percentile', fmtMs(p.p95_ms), 'successful runs'),
        tile('No context', fmtInt(p.no_context), 'probe answers without retrieved chunks'),
    );
    drawProbeChart(p.daily);
}

function drawChart(host, daily, opts) {
    const value = opts.value;
    const axis = opts.axis || ((v) => (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(1)));
    const width = Math.max(host.clientWidth, 280);
    const height = 220;
    const m = { top: 12, right: 8, bottom: 24, left: opts.axis ? 48 : 36 };
    const plotW = width - m.left - m.right;
    const plotH = height - m.top - m.bottom;
    const max = niceMax(Math.max(0, ...daily.map(value)));
    const y = (v) => m.top + plotH - (v / max) * plotH;
    const slot = plotW / daily.length;
    const barW = Math.max(2, Math.min(24, slot - 2));

    const root = svg('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': opts.label });

    for (let i = 0; i <= 4; i++) {
        const v = (max / 4) * i;
        const yy = Math.round(y(v)) + 0.5;
        root.append(svg('line', { class: i === 0 ? 'baseline' : 'grid', x1: m.left, x2: width - m.right, y1: yy, y2: yy }));
        const t = svg('text', { x: m.left - 6, y: yy + 4, 'text-anchor': 'end' });
        t.textContent = axis(v);
        root.append(t);
    }

    // Short ranges: label roughly six evenly spaced days, always including the last.
    // The 1-year range labels month starts ("Jan 2026"); "Jan 24" there read like a year.
    const every = Math.max(1, Math.ceil(daily.length / 6));
    const monthly = daily.length > 90;
    const monthStarts = monthly ? daily.flatMap((d, i) => (d.date.endsWith('-01') ? [i] : [])) : [];
    const monthEvery = Math.max(1, Math.ceil((monthStarts.length * 72) / plotW));  // ~72px per label
    const monthLabels = new Set(monthStarts.filter((_, k) => (monthStarts.length - 1 - k) % monthEvery === 0));
    const tooltip = el('div', { class: 'tooltip' });

    daily.forEach((d, i) => {
        const cx = m.left + slot * i + slot / 2;
        if (monthly ? monthLabels.has(i) : (daily.length - 1 - i) % every === 0) {
            const t = svg('text', { x: cx, y: height - 6, 'text-anchor': 'middle' });
            t.textContent = monthly ? fmtDay(d.date, { month: 'short', year: 'numeric' }) : fmtDay(d.date);
            root.append(t);
        }
        let bar = null;
        const v = value(d);
        if (v > 0) {
            // Rounded 4px data-end, square at the baseline
            const x0 = cx - barW / 2, x1 = cx + barW / 2, yTop = y(v), yBase = y(0);
            const r = Math.min(4, barW / 2, yBase - yTop);
            bar = svg('path', {
                class: opts.bad && opts.bad(d) ? 'bar bad' : 'bar',
                d: `M${x0},${yBase} V${yTop + r} Q${x0},${yTop} ${x0 + r},${yTop} H${x1 - r} Q${x1},${yTop} ${x1},${yTop + r} V${yBase} Z`,
            });
            root.append(bar);
        }
        // Full-height hit target, wider than the bar
        const hit = svg('rect', { class: 'slot', x: m.left + slot * i, y: m.top, width: slot, height: plotH });
        hit.addEventListener('mouseenter', () => {
            if (bar) bar.classList.add('active');
            tooltip.replaceChildren(
                el('div', { class: 't-date' }, fmtDay(d.date, { weekday: 'short', month: 'short', day: 'numeric' })),
                ...opts.tooltip(d).map((line) => (line == null ? null : el('div', {}, line))),
            );
            tooltip.style.left = `${Math.min(Math.max(cx, 70), width - 70)}px`;
            tooltip.style.top = `${y(v)}px`;
            tooltip.style.display = 'block';
        });
        hit.addEventListener('mouseleave', () => {
            if (bar) bar.classList.remove('active');
            tooltip.style.display = 'none';
        });
        root.append(hit);
    });

    host.replaceChildren(root, tooltip);
}

document.querySelectorAll('[data-days]').forEach((b) => b.addEventListener('click', () => {
    days = Number(b.dataset.days);
    storageSet(DAYS_STORAGE_KEY, String(days));
    load();
}));
$('refresh').addEventListener('click', load);
$('forget').addEventListener('click', () => {
    apiKey = '';
    storageSet(API_KEY_STORAGE_KEY, '');
    showAuth('Key removed from this browser.');
});
$('authForm').addEventListener('submit', (e) => {
    e.preventDefault();
    apiKey = $('apiKey').value.trim();
    storageSet(API_KEY_STORAGE_KEY, apiKey);
    $('apiKey').value = '';
    load();
});

let resizeTimer;
window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
        if (!lastStats) return;
        drawUsageChart(lastStats.usage.daily);
        drawProbeChart(lastStats.monitoring.daily);
    }, 150);
});

load();
setInterval(() => { if (!$('content').hidden) load(); }, 60000);
