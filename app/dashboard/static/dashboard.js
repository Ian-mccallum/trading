/* quantplatform operator dashboard — renderer.
   The server does the math and the wording (app/dashboard/{charts,copy,service}.py);
   this file only draws and handles interaction. No framework, no build step. */

const KEY = 'qp_admin_token';
const ACTOR_KEY = 'qp_actor';
const REFRESH_MS = 30_000;
const RANGE_LABELS = { '1D': '1 day', '1W': '1 week', '1M': '1 month', '3M': '3 months', '1Y': '1 year', 'ALL': 'all time' };
const FILTERS = [
  { id: 'all', label: 'All', test: () => true },
  { id: 'paper', label: 'Paper', test: (d) => d.mode === 'paper' || d.mode === 'live' },
  { id: 'shadow', label: 'Shadow', test: (d) => d.mode === 'shadow' },
  { id: 'blocked', label: 'Blocked', test: (d) => d.tone === 'down' },
];

const $ = (id) => document.getElementById(id);
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

let state = { range: '1M', filter: 'all', data: null, open: new Set(), timer: null };

/* ---------------------------------------------------------------- formatting */

const money = (n, dp = 2) =>
  n == null ? '—' : n.toLocaleString(undefined, { style: 'currency', currency: 'USD', minimumFractionDigits: dp, maximumFractionDigits: dp });
const pct = (n) => (n == null ? '—' : `${(n * 100).toFixed(2)}%`);
const signed = (n) => (n > 0 ? '+' : n < 0 ? '−' : '') + money(Math.abs(n));

function relative(iso) {
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${Math.floor(secs)}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}
const clockOf = (iso) =>
  new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/* Status marks pair a glyph with the label so state never depends on hue. */
const GLYPH = { up: '●', ok: '●', down: '✕', neutral: '◦' };

function badge(label, tone) {
  const node = el('span', 'badge');
  node.dataset.tone = tone;
  node.append(el('span', 'badge__glyph', GLYPH[tone] || '◦'), document.createTextNode(label));
  return node;
}

/* ---------------------------------------------------------------- fetching */

async function load(range) {
  const token = sessionStorage.getItem(KEY);
  const res = await fetch(`/dashboard/data?range=${encodeURIComponent(range)}`, {
    headers: { 'X-Admin-Token': token || '' },
  });
  if (res.status === 401 || res.status === 403) {
    sessionStorage.removeItem(KEY);
    throw new Error('unauthorized');
  }
  if (!res.ok) throw new Error(`Request failed (${res.status})`);
  return res.json();
}

/* ---------------------------------------------------------------- chart */

// Two points is a line segment, not a trend. Below this the informative
// empty state tells the operator more than a chart of nothing would.
const MIN_CHART_POINTS = 3;

function renderChart(chart) {
  const host = $('chart');
  host.textContent = '';

  if (!chart || !chart.points || chart.points.length < MIN_CHART_POINTS) {
    const empty = el('div', 'chart__empty');
    const count = chart?.points?.length ?? 0;
    empty.textContent = count
      ? `Only ${count} snapshot${count === 1 ? '' : 's'} so far. The chart appears once there are a few more; snapshots are recorded every 15 minutes.`
      : 'No history yet. Snapshots are recorded every 15 minutes.';
    host.append(empty);
    return;
  }

  const W = 1000, H = 300, PAD = 6;
  const px = (p) => PAD + p.x * (W - PAD * 2);
  const py = (p) => H - PAD - p.y * (H - PAD * 2);

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  // The viewBox is stretched to the container, so the stroke must opt out of
  // scaling or a 2px line renders thicker vertically than horizontally.
  svg.setAttribute('preserveAspectRatio', 'none');

  const line = chart.points.map((p, i) => `${i ? 'L' : 'M'}${px(p).toFixed(2)} ${py(p).toFixed(2)}`).join(' ');
  const colour = chart.direction === 'up' ? 'var(--up)' : chart.direction === 'down' ? 'var(--down)' : 'var(--muted)';

  // A single gradient gives the line visual mass without becoming a slab: a
  // flat series fills the lower half, so the top stop stays low.
  const gid = 'grad';
  svg.innerHTML = `
    <defs>
      <linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${colour}" stop-opacity="0.55"/>
        <stop offset="70%" stop-color="${colour}" stop-opacity="0.06"/>
        <stop offset="100%" stop-color="${colour}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <path class="chart__area" fill="url(#${gid})"
          d="${line} L${px(chart.points.at(-1)).toFixed(2)} ${H} L${px(chart.points[0]).toFixed(2)} ${H} Z"/>
    <path class="chart__line" data-dir="${chart.direction}" vector-effect="non-scaling-stroke" d="${line}"/>`;

  const cursor = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  cursor.setAttribute('class', 'chart__cursor');
  cursor.setAttribute('y1', 0); cursor.setAttribute('y2', H);
  cursor.style.display = 'none';
  const marker = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  marker.setAttribute('class', 'chart__marker');
  marker.setAttribute('fill', colour);
  marker.style.display = 'none';
  svg.append(cursor, marker);
  host.append(svg);

  // Scrubbing: no easing, it must feel attached to the pointer.
  const scrub = (clientX) => {
    const box = host.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (clientX - box.left) / box.width));
    const index = Math.round(ratio * (chart.points.length - 1));
    const point = chart.points[index];
    cursor.setAttribute('x1', px(point)); cursor.setAttribute('x2', px(point));
    marker.setAttribute('cx', px(point)); marker.setAttribute('cy', py(point));
    cursor.style.display = ''; marker.style.display = '';
    showValue(point.value, point.value - chart.first, chart.first, relative(point.ts));
  };
  const release = () => {
    cursor.style.display = 'none'; marker.style.display = 'none';
    showValue(chart.last, chart.change_abs, chart.first, RANGE_LABELS[state.range]);
  };

  host.onpointermove = (e) => scrub(e.clientX);
  host.onpointerleave = release;
  host.onpointerdown = (e) => { host.setPointerCapture(e.pointerId); scrub(e.clientX); };
  host.onpointerup = release;
}

function showValue(value, changeAbs, base, periodLabel) {
  $('equity').textContent = money(value);
  const dir = changeAbs > 0.005 ? 'up' : changeAbs < -0.005 ? 'down' : 'flat';
  const change = $('change');
  change.dataset.dir = dir;
  change.textContent = '';
  const arrow = dir === 'up' ? '↑' : dir === 'down' ? '↓' : '·';
  const ratio = base ? changeAbs / base : 0;
  change.append(
    el('span', null, `${arrow} ${signed(changeAbs)}`),
    el('span', null, pct(Math.abs(ratio))),
    el('span', 'period', periodLabel || ''),
  );
}

/* ---------------------------------------------------------------- sections */

function renderRanges(ranges) {
  const host = $('ranges');
  if (host.querySelector('input')) return; // built once
  ranges.forEach((r) => {
    const input = el('input');
    input.type = 'radio'; input.name = 'range'; input.id = `range-${r}`; input.value = r;
    input.checked = r === state.range;
    input.onchange = () => { state.range = r; refresh(); };
    const label = el('label', null, r);
    label.setAttribute('for', `range-${r}`);
    host.append(input, label);
  });
}

function renderFilters() {
  const host = $('filters');
  if (host.querySelector('input')) return;
  FILTERS.forEach((f) => {
    const input = el('input');
    input.type = 'radio'; input.name = 'filter'; input.id = `filter-${f.id}`;
    input.checked = f.id === state.filter;
    input.onchange = () => { state.filter = f.id; renderActivity(state.data.activity); };
    const label = el('label', null, f.label);
    label.setAttribute('for', `filter-${f.id}`);
    host.append(input, label);
  });
}

function renderStrip(system) {
  const strip = $('strip');
  const body = $('strip-body');
  body.textContent = '';
  $('strip-env').textContent = system.environment === 'live' ? 'LIVE' : 'Paper trading';

  if (system.healthy) {
    strip.dataset.state = 'ok';
    body.append(el('span', null, 'All systems running'));
    return;
  }
  strip.dataset.state = system.blocks.some((b) => b.severity === 'halted') ? 'halted' : 'stalled';
  system.blocks.forEach((block) => {
    const wrap = el('span', 'strip__block');
    wrap.append(el('strong', null, block.control), el('span', null, block.detail));
    body.append(wrap);
  });
}

function renderAllocation(slices) {
  const host = $('allocation');
  host.textContent = '';
  if (!slices.length) return;
  const bar = el('div', 'allocation');
  const legend = el('div', 'allocation__legend');
  slices.forEach((s, i) => {
    const colour = s.is_cash ? 'var(--surface-2)' : `oklch(${0.72 - i * 0.06} 0.14 ${260 - i * 34})`;
    const seg = el('div', 'allocation__slice');
    seg.style.width = `${(s.fraction * 100).toFixed(2)}%`;
    seg.style.background = colour;
    bar.append(seg);
    const key = el('span', 'allocation__key');
    const swatch = el('span', 'allocation__swatch');
    swatch.style.background = colour;
    key.append(swatch, document.createTextNode(`${s.label} ${(s.fraction * 100).toFixed(1)}%`));
    legend.append(key);
  });
  host.append(bar, legend);
}

function renderHoldings(holdings) {
  const host = $('holdings');
  host.textContent = '';
  $('holdings-meta').textContent = holdings.available ? `${holdings.rows.length} positions` : '';

  if (!holdings.available) {
    const notice = el('div', 'notice');
    notice.append(el('strong', null, 'Positions unavailable. '), document.createTextNode(holdings.error || ''));
    host.append(notice);
    return;
  }
  if (!holdings.rows.length) {
    const empty = el('div', 'empty');
    empty.append(el('strong', null, 'No open positions'));
    empty.append(document.createTextNode('Nothing is held right now.'));
    host.append(empty);
    return;
  }
  renderAllocation(holdings.allocation);
  holdings.rows.forEach((r) => {
    const row = el('div', 'row');
    const left = el('div');
    left.append(el('div', 'row__symbol', r.symbol));
    const dir = r.unrealized > 0 ? 'up' : r.unrealized < 0 ? 'down' : 'flat';
    const delta = el('div', 'row__delta', `${signed(r.unrealized)}`);
    delta.dataset.dir = dir;
    row.append(
      left,
      el('div', 'row__sub', `${r.qty} @ ${money(r.avg_entry)}`),
      el('div', 'row__value', money(r.value)),
      delta,
    );
    host.append(row);
  });
}

function renderVolume(volume) {
  const host = $('volume');
  host.textContent = '';
  if (!volume.some((v) => v.total > 0)) { host.style.display = 'none'; return; }
  host.style.display = '';
  volume.forEach((day) => {
    const col = el('div', 'volume__day');
    col.title = `${day.label}: ${day.executed} executed, ${day.rejected} blocked`;
    if (!day.total) {
      col.append(el('div', 'volume__empty'));
    } else {
      const height = Math.max(0.12, day.fraction) * 34;
      const okShare = day.total ? day.executed / day.total : 0;
      if (day.rejected) {
        const seg = el('div', 'volume__seg volume__seg--no');
        seg.style.height = `${height * (1 - okShare)}px`;
        col.append(seg);
      }
      if (day.executed) {
        const seg = el('div', 'volume__seg volume__seg--ok');
        seg.style.height = `${height * okShare}px`;
        col.append(seg);
      }
    }
    host.append(col);
  });
}

function renderDecision(d) {
  const wrap = el('details', 'decision');
  wrap.open = state.open.has(d.id);
  wrap.ontoggle = () => (wrap.open ? state.open.add(d.id) : state.open.delete(d.id));

  const summary = el('summary', 'decision__summary');
  const what = el('div', 'decision__what');
  what.append(el('span', 'row__symbol', d.symbol), el('span', 'decision__action', `${d.action} ${d.qty}`));
  summary.append(
    el('span', 'decision__time', clockOf(d.ts)),
    el('span', 'chip', d.mode_label),
    what,
    badge(d.status_label, d.tone),
  );
  wrap.append(summary);

  const detail = el('div', 'decision__detail');
  const r = d.reasoning;

  r.rejections.forEach((sentence) => detail.append(el('p', 'reason', sentence)));

  if (r.criteria.length) {
    const list = el('ul', 'criteria');
    r.criteria.forEach((c) => {
      const li = el('li');
      li.dataset.passed = String(c.passed);
      li.append(el('span', 'criteria__mark', c.passed ? '✓' : '✕'), el('span', null, c.label));
      list.append(li);
    });
    detail.append(list);
  }

  if (r.rs_skipped) {
    detail.append(el('p', 'hint', 'Relative-strength check skipped: no benchmark data was available.'));
  }

  const facts = [...r.facts];
  if (r.regime) facts.unshift({ label: 'Market regime', value: r.regime });
  facts.unshift({ label: 'Strategy', value: d.strategy });
  if (facts.length) {
    const dl = el('dl', 'facts');
    facts.forEach((f) => {
      const cell = el('div');
      cell.append(el('dt', null, f.label), el('dd', null, f.value));
      dl.append(cell);
    });
    detail.append(dl);
  }
  wrap.append(detail);
  return wrap;
}

function renderActivity(activity) {
  renderVolume(activity.volume);
  const host = $('activity');
  host.textContent = '';
  const test = (FILTERS.find((f) => f.id === state.filter) || FILTERS[0]).test;
  const rows = activity.rows.filter(test);

  if (!rows.length) {
    const empty = el('div', 'empty');
    empty.append(el('strong', null, activity.rows.length ? 'Nothing matches this filter' : 'No decisions yet'));
    empty.append(document.createTextNode(
      activity.rows.length ? 'Try a different filter.' : 'The strategy loop runs every 15 minutes.',
    ));
    host.append(empty);
    return;
  }
  const frag = document.createDocumentFragment();
  rows.forEach((d) => frag.append(renderDecision(d)));
  host.append(frag);
}

/* Who is making configuration changes. Captured once per tab and sent as the
   `actor` on every audited admin call, so the promotion_events trail names a
   person rather than "dashboard". */
function currentActor() {
  let actor = sessionStorage.getItem(ACTOR_KEY);
  if (!actor) {
    actor = (window.prompt('Your name, for the audit trail:') || '').trim();
    if (actor) sessionStorage.setItem(ACTOR_KEY, actor);
  }
  return actor;
}

async function personaAction(id, action, actor, reason) {
  const res = await fetch(`/admin/personas/${id}/${action}`, {
    method: 'POST',
    headers: {
      'X-Admin-Token': sessionStorage.getItem(KEY) || '',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ actor, reason }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

function renderPersonas(personas) {
  const host = $('personas');
  host.textContent = '';
  if (!personas.length) {
    host.append(el('div', 'empty', 'No personas registered.'));
    return;
  }
  personas.forEach((p) => {
    const card = el('div', 'persona');
    card.dataset.active = String(p.active);

    const name = el('div');
    name.append(el('div', 'persona__name', p.name.replace(/_/g, ' ')));
    name.append(
      el('div', 'persona__meta',
         `${p.kind} · ${p.members} ${p.cooperative ? 'legs' : 'strategies'}`),
    );

    const button = el('button', `btn ${p.active ? 'btn--stop' : 'btn--start'}`,
                      p.active ? 'Stop' : 'Start');
    button.type = 'button';

    card.append(el('span', 'persona__dot'), name, button);
    if (p.fidelity_note) card.append(el('div', 'persona__note', p.fidelity_note));
    host.append(card);

    button.onclick = () => {
      const actor = currentActor();
      if (!actor) return;
      if (p.active) {
        // Stopping is the safe direction: no confirmation, same reasoning that
        // makes the kill switch a single call.
        button.disabled = true;
        personaAction(p.id, 'deactivate', actor, 'stopped from dashboard')
          .then(refresh)
          .catch((err) => {
            button.disabled = false;
            card.append(el('div', 'confirm__error', err.message));
          });
      } else {
        openStartConfirm(card, button, p, actor);
      }
    };
  });
}

/* Activation requires a typed reason. The endpoint demands one and the UI must
   not fabricate it — an audit row saying "activated from dashboard" would be
   worthless six weeks later. */
function openStartConfirm(card, button, persona, actor) {
  if (card.querySelector('.confirm')) return;
  button.disabled = true;

  const box = el('div', 'confirm');
  box.append(el('div', 'confirm__hint',
                `Activating approves ${persona.members} member strategy` +
                `${persona.members === 1 ? '' : 'ies'} and records it as ${actor}.`));

  const row = el('div', 'confirm__row');
  const input = el('input');
  input.type = 'text';
  input.placeholder = 'Why are you starting this? (recorded in the audit trail)';
  const go = el('button', 'btn btn--start', 'Confirm start');
  const cancel = el('button', 'btn', 'Cancel');
  go.type = cancel.type = 'button';
  row.append(input, go, cancel);
  box.append(row);
  card.append(box);
  input.focus();

  const close = () => { box.remove(); button.disabled = false; };
  cancel.onclick = close;
  input.onkeydown = (e) => {
    if (e.key === 'Escape') close();
    if (e.key === 'Enter') go.click();
  };
  go.onclick = () => {
    const reason = input.value.trim();
    if (!reason) {
      input.focus();
      if (!box.querySelector('.confirm__error')) {
        box.append(el('div', 'confirm__error', 'A reason is required.'));
      }
      return;
    }
    go.disabled = true;
    personaAction(persona.id, 'activate', actor, reason)
      .then(refresh)
      .catch((err) => {
        go.disabled = false;
        box.append(el('div', 'confirm__error', err.message));
      });
  };
}

function renderRejections(rejections) {
  const section = $('rejections-section');
  const host = $('rejections');
  host.textContent = '';
  if (!rejections.length) { section.hidden = true; return; }
  section.hidden = false;
  rejections.forEach((r) => {
    const row = el('div', 'row');
    row.append(
      el('div', 'row__value', String(r.count)),
      el('div', null, r.label),
      el('div', 'row__sub', r.explanation),
      el('div'),
    );
    host.append(row);
  });
}

function renderHints(hints) {
  document.querySelectorAll('.hint[data-hint]').forEach((n) => n.remove());
  const anchor = $('activity').parentElement;
  hints.forEach((text) => {
    const hint = el('div', 'hint');
    hint.dataset.hint = 'true';
    hint.textContent = text;
    anchor.insertBefore(hint, $('volume'));
  });
}

/* ---------------------------------------------------------------- orchestration */

function render(data) {
  state.data = data;
  renderStrip(data.system);
  renderRanges(data.portfolio.ranges);
  renderFilters();

  const chart = data.portfolio.chart;
  if (chart) {
    showValue(data.portfolio.equity ?? chart.last, chart.change_abs, chart.first, RANGE_LABELS[state.range]);
  } else {
    $('equity').textContent = money(data.portfolio.equity);
    $('change').dataset.dir = 'flat';
    $('change').textContent = '';
  }
  renderChart(chart);
  renderHoldings(data.holdings);
  renderActivity(data.activity);
  renderPersonas(data.personas);
  renderRejections(data.rejections);
  renderHints(data.hints);

  $('updated').textContent = `Updated ${relative(data.generated_at)}`;
  $('counts').textContent =
    `${data.counts.tradeable_strategies} strategies tradeable · ${data.counts.open_orders} orders working`;
}

async function refresh() {
  try {
    render(await load(state.range));
  } catch (err) {
    if (err.message === 'unauthorized') { showGate('That token was not accepted.'); return; }
    $('updated').textContent = `Update failed: ${err.message}`;
  }
}

function showGate(message) {
  clearInterval(state.timer);
  $('app').hidden = true;
  $('gate').hidden = false;
  const error = $('gate-error');
  error.hidden = !message;
  if (message) error.textContent = message;
  $('token').focus();
}

async function start() {
  $('gate').hidden = true;
  $('app').hidden = false;
  await refresh();
  clearInterval(state.timer);
  state.timer = setInterval(refresh, REFRESH_MS);
}

$('gate-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const value = $('token').value.trim();
  if (!value) return;
  sessionStorage.setItem(KEY, value);
  start();
});

// Keyboard: j/k move through activity rows, matching the list-navigation
// convention the operator already knows from mail and issue trackers.
document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, textarea')) return;
  if (e.key !== 'j' && e.key !== 'k') return;
  const rows = [...document.querySelectorAll('.decision__summary')];
  if (!rows.length) return;
  const index = rows.indexOf(document.activeElement);
  const next = e.key === 'j' ? Math.min(rows.length - 1, index + 1) : Math.max(0, index - 1);
  rows[next]?.focus();
  e.preventDefault();
});

sessionStorage.getItem(KEY) ? start() : showGate();
