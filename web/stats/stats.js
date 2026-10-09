// /srv/chatbot/web/stats/stats.js: public stats page; the look comes from the --dash-* tokens in themes/<name>.css

const STATS_URL = '/chatbot/api/public/stats';
const REFRESH_MS = 60000;
const SVG_NS = 'http://www.w3.org/2000/svg';

// Theme-specific text, like THEMES in ../app.js
const THEMES = {
    retro: { title: '// SYSTEM STATUS', subtitle: 'Live telemetry for the resume chatbot.', avatar: '', ok: 'ALL SYSTEMS NOMINAL', bad: 'FAULT DETECTED' },
    ios: { title: 'Resume Bot Stats', subtitle: 'How the resume chatbot is doing.', avatar: 'KD', ok: 'All systems normal', bad: 'Something is wrong' },
    android: { title: 'Resume Bot stats', subtitle: 'How the resume chatbot is doing.', avatar: 'K', ok: 'All systems normal', bad: 'Something is wrong' },
    contrast: { title: 'Chatbot Statistics', subtitle: 'Usage and health of the resume chatbot.', avatar: '', ok: 'Everything is working', bad: 'There is a problem' },
    ironman: { title: 'J.A.R.V.I.S. Diagnostics', subtitle: 'Systems report on the resume chatbot.', avatar: '', ok: 'All systems online, sir', bad: 'Systems compromised' },
    american: { title: '★ State of the Bot ★', subtitle: 'A report to the people on the resume chatbot.', avatar: '', ok: 'The bot is strong', bad: 'The bot needs help' },
    canadian: { title: "How's the Bot Doing, Eh?", subtitle: 'Usage and health of the resume chatbot.', avatar: '🍁', ok: 'All good, eh', bad: 'Sorry, something broke' },
    synthwave: { title: 'HIGH SCORES', subtitle: 'Resume chatbot stats.', avatar: '', ok: 'ALL SYSTEMS GO', bad: 'GAME OVER' },
    halloween: { title: 'The Haunted Ledger', subtitle: 'A spooky report on the resume chatbot.', avatar: '🎃', ok: 'No ghosts in the machine', bad: 'Something wicked this way comes' },
    christmas: { title: "Santa's Workshop Report", subtitle: 'How the resume chatbot has been this year.', avatar: '🎄', ok: 'Nice list: all systems merry', bad: "Naughty list: something's broken" },
};

const COMPONENTS = { api: 'Web API', qdrant: 'Search index', ollama: 'Language model' };

const themeSelect = document.getElementById('theme');
const tip = document.getElementById('tip');
let currentTheme = THEMES[document.documentElement.dataset.theme] ? document.documentElement.dataset.theme : 'ios';
let days = 30;
let lastData = null;

function applyTheme(name, save = true) {
    if (!THEMES[name]) {
        name = 'ios';
    }
    currentTheme = name;
    const t = THEMES[name];
    document.documentElement.dataset.theme = name;
    themeSelect.value = name;
    document.getElementById('title').textContent = t.title;
    document.getElementById('subtitle').textContent = t.subtitle;
    document.getElementById('avatar').textContent = t.avatar;
    if (lastData) {
        renderStatus(lastData);
    }
    if (save) {
        try {
            localStorage.setItem('chatbot-theme', name);
        } catch (e) {
            // Storage unavailable (private mode etc.); the theme still applies for this visit
        }
    }
}

// ---- Formatting ----

function el(tag, attrs = {}, text) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        node.setAttribute(k, v);
    }
    if (text !== undefined) {
        node.textContent = text;
    }
    return node;
}

function svg(tag, attrs = {}) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) {
        node.setAttribute(k, v);
    }
    return node;
}

function seconds(ms) {
    if (ms === null || ms === undefined) {
        return null;
    }
    return ms >= 120000 ? { value: (ms / 60000).toFixed(1), unit: 'min' } : { value: Math.round(ms / 1000), unit: 's' };
}

function secondsText(ms) {
    const s = seconds(ms);
    return s ? `${s.value}\u00a0${s.unit}` : 'n/a';
}

function dayLabel(iso, withYear = false) {
    const options = { month: 'short', day: 'numeric', timeZone: 'UTC' };
    if (withYear) {
        options.year = 'numeric';
    }
    return new Date(iso + 'T00:00:00Z').toLocaleDateString(undefined, options);
}

function ago(iso) {
    const minutes = Math.round((Date.now() - Date.parse(iso)) / 60000);
    if (minutes < 1) {
        return 'just now';
    }
    if (minutes < 60) {
        return `${minutes} min ago`;
    }
    const hours = Math.round(minutes / 60);
    return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`;
}

// Whole percent, except near 100 where one decimal keeps a rare failure from reading as 100%
function percent(part, whole) {
    if (!whole) {
        return null;
    }
    const p = (part / whole) * 100;
    return p >= 99.5 && p < 100 ? Math.floor(p * 10) / 10 : Math.round(p);
}

// ---- Sections ----

function renderStatus(data) {
    const t = THEMES[currentTheme];
    const allOk = Object.values(data.status).every(Boolean);
    const section = document.getElementById('status');
    section.classList.toggle('ok', allOk);
    section.classList.toggle('bad', !allOk);
    document.getElementById('statusIcon').textContent = allOk ? '✓' : '✕';
    document.getElementById('statusText').textContent = allOk ? t.ok : t.bad;

    const chips = document.getElementById('chips');
    chips.replaceChildren();
    for (const [key, label] of Object.entries(COMPONENTS)) {
        if (!(key in data.status)) {
            continue;
        }
        const up = data.status[key];
        const chip = el('li', { class: up ? 'up' : 'down' });
        chip.append(el('span', { class: 'mark', 'aria-hidden': 'true' }, up ? '✓' : '✕'), `${label}: ${up ? 'up' : 'down'}`);
        chips.append(chip);
    }

    const last = data.monitoring.last;
    document.getElementById('lastCheck').textContent = last
        ? `Last health check ${ago(last.at)}: ${last.ok ? `passed in ${secondsText(last.duration_ms)}` : 'failed'}.`
        : 'No health checks recorded yet.';
}

function statusError(message) {
    const section = document.getElementById('status');
    section.classList.remove('ok');
    section.classList.add('bad');
    document.getElementById('statusIcon').textContent = '✕';
    document.getElementById('statusText').textContent = message;
    document.getElementById('chips').replaceChildren();
    document.getElementById('lastCheck').textContent = 'Retrying in a minute.';
}

function kpi(label, value, unit, note) {
    const card = el('div', { class: 'panel kpi' });
    card.append(el('div', { class: 'label' }, label));
    const v = el('div', { class: 'value' }, value === null ? '—' : String(value));
    if (unit && value !== null) {
        v.append(el('span', { class: 'unit' }, unit));
    }
    card.append(v, el('div', { class: 'muted small' }, note));
    return card;
}

function renderKpis(data) {
    const u = data.usage;
    const m = data.monitoring;
    const answerTime = seconds(u.p50_ms);
    const matched = percent(u.matched, u.context_known);
    const checks = percent(m.runs - m.failures, m.runs);
    document.getElementById('kpis').replaceChildren(
        kpi('Questions asked', u.questions.toLocaleString(), '',
            u.errors ? `${u.errors} ended in an error` : 'No errors'),
        kpi('Found in the resume', matched, '%',
            matched === null ? 'No answered questions yet' : 'Share of questions that matched a passage; the rest were off-topic or not covered'),
        kpi('Typical answer time', answerTime && answerTime.value, answerTime && answerTime.unit,
            u.p95_ms ? `The slowest 5% took over ${secondsText(u.p95_ms)}` : 'No timed answers yet'),
        kpi('Health checks passed', checks, '%',
            m.runs ? `${m.runs.toLocaleString()} hourly checks` : 'No checks in this range'),
    );
}

// Rounds a chart maximum up to 1, 2 or 5 × a power of ten
function niceMax(max) {
    if (max <= 0) {
        return 1;
    }
    const power = 10 ** Math.floor(Math.log10(max));
    return [1, 2, 5, 10].map(f => f * power).find(v => v >= max);
}

// Bar path with 4px rounded top corners, square at the baseline
function barPath(x, y, w, h) {
    const r = Math.min(4, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

/**
 * Single-series daily bar chart.
 * rows: [{date, value, flag, tip}] where value may be null (no data), flag marks a failed check.
 */
function barChart(container, rows, { format, ariaLabel, emptyText }) {
    container.replaceChildren();
    const width = Math.max(container.clientWidth, 280);
    const height = 190;
    const pad = { top: 14, right: 4, bottom: 22, left: 40 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const max = niceMax(Math.max(0, ...rows.map(r => r.value || 0)));
    const chart = svg('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': ariaLabel });

    // Recessive grid: 0, half and max
    for (const f of [0, 0.5, 1]) {
        const y = pad.top + plotH * (1 - f);
        chart.append(svg('line', { class: f === 0 ? 'baseline' : 'grid', x1: pad.left, x2: width - pad.right, y1: y, y2: y }));
        const label = svg('text', { x: pad.left - 6, y: y + 4, 'text-anchor': 'end' });
        label.textContent = format(max * f);
        chart.append(label);
    }

    const slot = plotW / rows.length;
    const gap = slot >= 6 ? 2 : 0;
    const barW = Math.max(1, slot - gap);
    rows.forEach((row, i) => {
        const x = pad.left + i * slot + gap / 2;
        const col = svg('g', { class: 'col' });
        if (row.value) {
            const h = Math.max(1, (row.value / max) * plotH);
            col.append(svg('path', { class: 'bar', d: barPath(x, pad.top + plotH - h, barW, h) }));
        }
        if (row.flag) {
            col.append(svg('circle', { class: 'fail', cx: x + barW / 2, cy: pad.top - 6, r: Math.min(4, Math.max(2, barW / 2)) }));
        }
        // Hit target spans the full column, wider than the bar
        const hit = svg('rect', { class: 'hit', x: pad.left + i * slot, y: 0, width: slot, height: height });
        hit.addEventListener('mouseenter', e => { col.classList.add('active'); showTip(e, row); });
        hit.addEventListener('mousemove', e => moveTip(e));
        hit.addEventListener('mouseleave', () => { col.classList.remove('active'); hideTip(); });
        col.append(hit);
        chart.append(col);
    });

    // Date labels at the start, middle and end
    const picks = rows.length > 2 ? [0, Math.floor((rows.length - 1) / 2), rows.length - 1] : rows.map((_, i) => i);
    picks.forEach((i, n) => {
        const anchor = n === 0 ? 'start' : n === picks.length - 1 ? 'end' : 'middle';
        const x = anchor === 'start' ? pad.left : anchor === 'end' ? width - pad.right : pad.left + (i + 0.5) * slot;
        const label = svg('text', { x, y: height - 6, 'text-anchor': anchor });
        label.textContent = dayLabel(rows[i].date, days > 90);
        chart.append(label);
    });

    if (!rows.some(r => r.value)) {
        const empty = svg('text', { class: 'empty', x: pad.left + plotW / 2, y: pad.top + plotH / 2, 'text-anchor': 'middle' });
        empty.textContent = emptyText;
        chart.append(empty);
    }
    container.append(chart);
}

function showTip(event, row) {
    tip.replaceChildren(el('strong', {}, dayLabel(row.date, true)), ...row.tip.map(line => el('div', {}, line)));
    tip.hidden = false;
    moveTip(event);
}

function moveTip(event) {
    const margin = 12;
    const rect = tip.getBoundingClientRect();
    let x = event.clientX + margin;
    if (x + rect.width > window.innerWidth - 8) {
        x = event.clientX - rect.width - margin;
    }
    tip.style.left = `${Math.max(8, x)}px`;
    tip.style.top = `${Math.max(8, event.clientY - rect.height - margin)}px`;
}

function hideTip() {
    tip.hidden = true;
}

function table(container, headers, rows) {
    const t = el('table');
    const head = el('tr');
    headers.forEach(h => head.append(el('th', { scope: 'col' }, h)));
    t.append(el('thead'), el('tbody'));
    t.tHead.append(head);
    // Newest first, as people look for recent days
    [...rows].reverse().forEach(cells => {
        const tr = el('tr');
        cells.forEach(c => tr.append(el('td', {}, c)));
        t.tBodies[0].append(tr);
    });
    container.replaceChildren(t);
}

function renderCharts(data) {
    const daily = data.usage.daily;
    barChart(document.getElementById('questionsChart'), daily.map(d => ({
        date: d.date,
        value: d.total,
        tip: [`${d.total} question${d.total === 1 ? '' : 's'}`, ...(d.errors ? [`${d.errors} error${d.errors === 1 ? '' : 's'}`] : [])],
    })), {
        format: v => (Number.isInteger(v) ? String(v) : ''),
        ariaLabel: `Questions per day over the last ${days} days`,
        emptyText: 'No questions in this range',
    });
    table(document.getElementById('questionsTable'), ['Date (UTC)', 'Questions', 'Errors'],
        daily.map(d => [d.date, d.total, d.errors]));

    const probes = data.monitoring.daily;
    barChart(document.getElementById('probeChart'), probes.map(d => ({
        date: d.date,
        value: d.median_ms === null ? null : d.median_ms / 1000,
        flag: d.failures > 0,
        tip: d.runs
            ? [`Median ${secondsText(d.median_ms)}`, `${d.runs - d.failures} of ${d.runs} checks passed`]
            : ['No checks'],
    })), {
        format: v => `${Math.round(v)}s`,
        ariaLabel: `Median health check answer time per day over the last ${days} days`,
        emptyText: 'No health checks in this range',
    });
    table(document.getElementById('probeTable'), ['Date (UTC)', 'Median', 'Checks', 'Failed'],
        probes.map(d => [d.date, d.median_ms === null ? '—' : secondsText(d.median_ms), d.runs, d.failures]));
}

function renderAbout(data) {
    const kb = data.knowledge_base;
    const about = document.getElementById('about');
    about.textContent =
        `Questions are answered by ${data.models.generation}, a small open language model running on this server's CPU. ` +
        `Each question is first matched against ${kb ? `${kb.documents} career documents, split into ${kb.chunks} passages` : 'Kevin\'s career documents'} ` +
        `using ${data.models.embedding} embeddings, and the model answers only from the passages it finds. ` +
        'Nothing is sent to an outside AI service.';
    const since = data.backfilled_since || data.logging_since;
    document.getElementById('updated').textContent =
        `Updated ${new Date(data.generated_at).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}` +
        (since ? ` · Usage recorded since ${dayLabel(since.slice(0, 10), true)}` : '');
}

function render(data) {
    lastData = data;
    renderStatus(data);
    renderKpis(data);
    renderCharts(data);
    renderAbout(data);
}

async function load() {
    try {
        const res = await fetch(`${STATS_URL}?days=${days}`);
        if (!res.ok) {
            throw new Error(`HTTP ${res.status}`);
        }
        render(await res.json());
    } catch (error) {
        console.error('Stats request failed:', error);
        statusError("Couldn't load stats");
    }
}

// ---- Wiring ----

themeSelect.addEventListener('change', () => applyTheme(themeSelect.value));

document.querySelectorAll('.range button').forEach(button => {
    button.addEventListener('click', () => {
        days = Number(button.dataset.days);
        document.querySelectorAll('.range button').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
        load();
    });
});

let resizeTimer = null;
window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => lastData && renderCharts(lastData), 150);
});

applyTheme(currentTheme, false);
load();
setInterval(load, REFRESH_MS);
