// V25 dashboard SPA (Preact + htm, no build step). Read-only: every request is a GET except logout.
import { h, render } from './vendor/preact.module.js';
import { useState, useEffect, useRef, useMemo } from './vendor/hooks.module.js';
import htm from './vendor/htm.module.js';

const html = htm.bind(h);
const Chart = window.Chart;
const TZ = 'America/New_York';

// =================================================================== formatting
const fmtDT = new Intl.DateTimeFormat('en-US', { timeZone: TZ, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const fmtDay = new Intl.DateTimeFormat('en-US', { timeZone: TZ, month: 'short', day: 'numeric' });
const fmtClock = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const fmtTick = new Intl.DateTimeFormat('en-US', { timeZone: TZ, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const dash = '—';

const toDate = (iso) => (iso ? new Date(iso) : null);
const timeET = (iso) => (iso ? `${fmtDT.format(new Date(iso))} ET` : dash);
const clockET = (d) => `${fmtClock.format(d)} ET`;
function money(v, { sign = false, cents = true } = {}) {
  if (!isNum(v)) return dash;
  const abs = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0 });
  const s = v < 0 ? '−' : sign && v > 0 ? '+' : '';
  return `${s}$${abs}`;
}
const price = (v) => (isNum(v) ? v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : dash);
const pct = (v, digits = 2, sign = false) => (isNum(v) ? `${sign && v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(digits)}%` : dash);
function duration(sec) {
  if (!isNum(sec)) return dash;
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), hr = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (d) return `${d}d ${hr}h`;
  if (hr) return `${hr}h ${String(m).padStart(2, '0')}m`;
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}
function ago(sec) {
  if (!isNum(sec)) return 'never';
  if (sec < 0) return 'just now';
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}
const pnlClass = (v) => (!isNum(v) ? 'flat' : v > 0 ? 'pos' : v < 0 ? 'neg' : 'flat');

// =================================================================== data
async function api(path) {
  const r = await fetch(path, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
  if (r.status === 401) { location.href = '/login'; throw new Error('Session expired'); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${r.status}`);
  return body;
}

/** Poll `path` every `ms`; keeps the previous data on screen while refetching or after an error. */
function usePoll(path, ms, enabled = true) {
  const [state, setState] = useState({ data: null, error: null, at: null });
  useEffect(() => {
    if (!enabled || !path) return undefined;
    let alive = true, timer = null;
    const tick = async () => {
      clearTimeout(timer);
      if (document.hidden) { timer = setTimeout(tick, ms); return; }
      try {
        const data = await api(path);
        if (alive) setState({ data, error: null, at: new Date() });
      } catch (e) {
        if (alive) setState((s) => ({ ...s, error: e.message }));
      }
      if (alive) timer = setTimeout(tick, ms);
    };
    const onVis = () => { if (!document.hidden) tick(); };
    document.addEventListener('visibilitychange', onVis);
    tick();
    return () => { alive = false; clearTimeout(timer); document.removeEventListener('visibilitychange', onVis); };
  }, [path, ms, enabled]);
  return state;
}

function useStored(key, initial) {
  const [v, setV] = useState(() => { try { return localStorage.getItem(key) ?? initial; } catch { return initial; } });
  const set = (nv) => { setV(nv); try { localStorage.setItem(key, nv); } catch { /* storage unavailable */ } };
  return [v, set];
}

// =================================================================== icons
const P = {
  overview: 'M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z',
  activity: 'M22 12h-4l-3 9L9 3l-3 9H2',
  trades: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  analytics: 'M3 3v18h18M7 15l4-4 3 3 5-6',
  positions: 'M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5',
  health: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
  check: 'M20 6 9 17l-5-5',
  alert: 'M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01',
  x: 'M18 6 6 18M6 6l12 12',
  minus: 'M5 12h14',
  logout: 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9',
  download: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3',
  signal: 'M2 20h.01M7 20v-4M12 20v-8M17 20V8M22 4v16',
  up: 'M12 19V5M5 12l7-7 7 7',
  down: 'M12 5v14M19 12l-7 7-7-7',
  target: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z',
  stop: 'M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9zM15 9l-6 6M9 9l6 6',
  pause: 'M6 4h4v16H6zM14 4h4v16h-4z',
  info: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01',
  clock: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2',
  flat: 'M4 12h16',
};
const Icon = ({ name }) => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d=${P[name]} /></svg>`;
const STATUS_ICON = { good: 'check', warning: 'alert', serious: 'alert', critical: 'x', neutral: 'minus' };

const Pill = ({ status, label, value, pulse }) => html`
  <span class=${`pill st-${status}`} title=${`${label}: ${value}`}>
    <span class=${pulse ? 'pulse' : ''}><${Icon} name=${STATUS_ICON[status]} /></span>${label} <b>${value}</b>
  </span>`;

function Pnl({ value, chip = false, arrow = true }) {
  const cls = pnlClass(value);
  return html`<span class=${`pnl ${cls}${chip ? ' chip-pnl' : ''}`}>
    ${arrow && isNum(value) && value !== 0 ? html`<span class="arrow" aria-hidden="true">${value > 0 ? '▲' : '▼'}</span>` : null}
    ${money(value, { sign: true })}</span>`;
}

// =================================================================== status derivation
function engineStatus(health) {
  if (!health) return ['neutral', 'Loading'];
  if (!health.engine.online) return ['critical', 'Offline'];
  if (health.overall === 'healthy') return ['good', 'Healthy'];
  return ['warning', 'Degraded'];
}
function tvStatus(health) {
  const tv = health?.tradingview;
  if (!tv) return ['neutral', 'Unknown'];
  if (tv.state === 'receiving') return ['good', `Receiving · ${ago(tv.age_seconds)}`];
  if (tv.state === 'market_closed') return ['neutral', `Idle · market closed`];
  if (tv.state === 'never') return ['warning', 'No data yet'];
  return ['warning', `Silent · ${ago(tv.age_seconds)}`];
}

// =================================================================== charts
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const crosshair = {
  id: 'crosshair',
  afterDatasetsDraw(chart) {
    const active = chart.tooltip?.getActiveElements?.() || [];
    if (!active.length || chart.config.type !== 'line') return;
    const { ctx, chartArea } = chart;
    const x = active[0].element.x;
    ctx.save();
    ctx.strokeStyle = css('--axis');
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, chartArea.top); ctx.lineTo(x, chartArea.bottom); ctx.stroke();
    ctx.restore();
  },
};
if (Chart) {
  Chart.register(crosshair);
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 11;
  Chart.defaults.color = '#7d8597';
  Chart.defaults.animation.duration = 350;
}

function baseOptions(yFmt, xTicks) {
  return {
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#0e121a', borderColor: 'rgba(255,255,255,.12)', borderWidth: 1, padding: 10,
        titleColor: '#e8eaf0', bodyColor: '#a3a9b8', displayColors: false,
        callbacks: { label: (c) => yFmt(c.parsed.y ?? c.parsed.x) },
      },
    },
    scales: {
      x: { grid: { display: false }, border: { color: css('--axis') }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 7, ...xTicks } },
      y: { grid: { color: css('--grid') }, border: { display: false }, ticks: { callback: (v) => yFmt(v), maxTicksLimit: 6 } },
    },
  };
}

function ChartCanvas({ build, deps, empty }) {
  const ref = useRef(null), chartRef = useRef(null);
  useEffect(() => {
    if (!Chart || !ref.current || empty) return undefined;
    const cfg = build();
    if (chartRef.current && chartRef.current.config.type === cfg.type) {
      chartRef.current.data = cfg.data;
      chartRef.current.options = cfg.options;
      chartRef.current.update();
    } else {
      chartRef.current?.destroy();
      chartRef.current = new Chart(ref.current, cfg);
    }
    return undefined;
  }, deps);
  useEffect(() => () => chartRef.current?.destroy(), []);
  return html`<canvas ref=${ref} role="img"></canvas>`;
}

function lineChart(points, { yFmt = (v) => money(v, { cents: false }), fill = false, color } = {}) {
  const c = color || css('--accent');
  return {
    type: 'line',
    data: { datasets: [{ data: points, borderColor: c, borderWidth: 2, pointRadius: points.length < 40 ? 3 : 0,
      pointHoverRadius: 5, pointBackgroundColor: c, pointBorderColor: css('--surface'), pointBorderWidth: 2,
      tension: 0.15, fill: fill ? 'origin' : false, backgroundColor: fill ? `${c}22` : undefined, stepped: false }] },
    options: {
      ...baseOptions(yFmt, { callback: (v) => fmtTick.format(new Date(v)) }),
      parsing: false,
      scales: {
        ...baseOptions(yFmt).scales,
        x: { type: 'linear', grid: { display: false }, border: { color: css('--axis') },
          min: points.length ? points[0].x : undefined, max: points.length ? points[points.length - 1].x : undefined,
          ticks: { maxRotation: 0, maxTicksLimit: 6, callback: (v) => fmtTick.format(new Date(v)) } },
      },
      plugins: { ...baseOptions(yFmt).plugins,
        tooltip: { ...baseOptions(yFmt).plugins.tooltip,
          callbacks: { title: (items) => timeET(new Date(items[0].parsed.x).toISOString()), label: (ctx) => yFmt(ctx.parsed.y) } } },
    },
  };
}

function barChart(labels, values, { yFmt = (v) => money(v, { cents: false }), colors, tooltipExtra } = {}) {
  const pos = css('--pos-mark'), neg = css('--neg-mark');
  return {
    type: 'bar',
    data: { labels, datasets: [{ data: values, backgroundColor: colors || values.map((v) => (v >= 0 ? pos : neg)),
      borderRadius: 4, borderSkipped: false, maxBarThickness: 30, categoryPercentage: 0.8, barPercentage: 0.9 }] },
    options: {
      ...baseOptions(yFmt),
      plugins: { ...baseOptions(yFmt).plugins,
        tooltip: { ...baseOptions(yFmt).plugins.tooltip,
          callbacks: { label: (ctx) => [yFmt(ctx.parsed.y), ...(tooltipExtra ? tooltipExtra(ctx.dataIndex) : [])] } } },
    },
  };
}

function ChartCard({ title, sub, empty, emptyText, build, deps, columns, rows, tall }) {
  const [table, setTable] = useState(false);
  return html`<section class="card">
    <div class="card-head">
      <div><h3 class="card-title">${title}</h3>${sub ? html`<div class="card-sub">${sub}</div>` : null}</div>
      ${rows ? html`<button class="btn btn-ghost" onClick=${() => setTable(!table)} aria-pressed=${table}>${table ? 'Chart' : 'Table'}</button>` : null}
    </div>
    ${table ? html`<div class="mini-table"><table><thead><tr>${columns.map((c) => html`<th class=${c.num ? 'num' : ''}>${c.label}</th>`)}</tr></thead>
        <tbody>${rows.length ? rows.map((r) => html`<tr>${columns.map((c) => html`<td class=${c.num ? 'num' : ''}>${c.fmt ? c.fmt(r[c.key], r) : r[c.key]}</td>`)}</tr>`)
          : html`<tr><td colspan=${columns.length} class="muted">No data</td></tr>`}</tbody></table></div>`
      : html`<div class=${`chart-box${tall ? ' tall' : ''}`}>
          ${empty ? html`<div class="chart-empty">${emptyText || 'No closed trades in this range yet'}</div>`
            : html`<${ChartCanvas} build=${build} deps=${deps} />`}
        </div>`}
  </section>`;
}

// =================================================================== shared bits
const Stat = ({ label, children, note, cls = '' }) => html`<div class=${`card kpi ${cls}`}>
  <div class="stat-label">${label}</div><div class="stat-value">${children}</div>${note ? html`<div class="stat-note">${note}</div>` : null}</div>`;

function Banners({ ov }) {
  const hl = ov?.health;
  if (!hl) return null;
  const out = [];
  if (!hl.engine.online) out.push(['critical', 'Engine unreachable', `${hl.error || 'No response from the engine.'} Figures are hidden rather than shown stale.`]);
  else if (!ov.metrics) out.push(['critical', 'Engine state unavailable', hl.error || 'The engine responded but its status could not be read.']);
  if (hl.risk?.halt) out.push(['critical', `Trading halted (${hl.risk.halt.scope})`, hl.risk.halt.reason]);
  if (hl.reconciliation?.needs_reconcile) out.push(['warning', 'Reconciliation pending', 'The engine will not accept entries until broker state is reconciled.']);
  return out.map(([lvl, title, text]) => html`<div class=${`banner banner-${lvl}`} role="status"><${Icon} name=${lvl === 'critical' ? 'x' : 'alert'} /><div><b>${title}</b>${text}</div></div>`);
}

const CATS = {
  signal: ['signal', 'Signal'], entry: ['up', 'Entry'], exit: ['flat', 'Exit'], stop: ['stop', 'Stop'],
  target: ['target', 'Target'], rejected: ['x', 'Rejected'], risk: ['alert', 'Risk'], halt: ['pause', 'Halt'],
  error: ['x', 'Error'], warning: ['alert', 'Warning'], system: ['info', 'System'],
};
function Feed({ items }) {
  if (!items?.length) return html`<div class="empty"><${Icon} name="activity" />No activity recorded yet.</div>`;
  return html`<ul class="feed">${items.map((it) => {
    const [icon, label] = CATS[it.category] || CATS.system;
    return html`<li class=${`cat-${it.category}`} key=${`${it.ts}-${it.ref}-${it.title}`}>
      <span class="feed-icon" title=${label}><${Icon} name=${icon} /></span>
      <div><div class="feed-title">${it.title}</div>${it.detail ? html`<div class="feed-detail">${it.detail}</div>` : null}</div>
      <div class="feed-time">${timeET(it.ts)}<br /><span class="muted">${label} · ${it.source}</span></div>
    </li>`;
  })}</ul>`;
}

// =================================================================== pages
function Overview({ ov, go }) {
  const m = ov?.metrics;
  const pos = ov?.positions || [];
  const spark = useMemo(() => {
    if (!ov?.equity_spark?.length) return [];
    const pts = ov.equity_spark.map((p) => ({ x: +new Date(p.t), y: p.equity }));
    if (isNum(m?.equity)) pts.push({ x: +new Date(ov.now), y: m.equity });
    return pts;
  }, [ov?.equity_spark, m?.equity]);
  const cvdKey = document.documentElement.dataset.cvd;
  return html`
    <${Banners} ov=${ov} />
    <div class="grid hero-grid">
      <div class="card hero">
        <div class="stat-label">Equity <span class="muted">(broker balance + unrealized)</span></div>
        <div class="stat-value">${money(m?.equity)}</div>
        <div class="stat-note"><span class=${`chip-pnl ${pnlClass(m?.total_return_pct)}`}>${pct(m?.total_return_pct, 2, true)}</span> total return on ${money(m?.starting_balance, { cents: false })} start</div>
      </div>
      <${Stat} label="Account balance">${money(m?.balance)}<//>
      <${Stat} label="Today's realized P&L" note="CME trading day (rolls 17:00 ET)"><${Pnl} value=${m?.today_realized_pnl} /><//>
      <${Stat} label="Unrealized P&L" note=${m ? `${m.open_positions} open position${m.open_positions === 1 ? '' : 's'}` : null}><${Pnl} value=${m?.unrealized_pnl} /><//>
      <${Stat} label="Total realized P&L" note=${m ? `${m.total_trades} closed trades · ${money(m.commissions)} fees` : null}><${Pnl} value=${m?.total_realized_pnl} /><//>
    </div>

    <div class="grid two-col section-gap">
      <${ChartCard} title="Equity curve" sub="Closed-trade equity, plus current broker equity"
        empty=${spark.length < 2} emptyText="The curve appears after the first closed trade"
        build=${() => lineChart(spark, { fill: true })} deps=${[spark, cvdKey]} />
      <section class="card">
        <div class="card-head"><h3 class="card-title">Current position</h3>
          <button class="btn btn-ghost" onClick=${() => go('positions')}>Details</button></div>
        ${pos.length ? pos.map((p) => html`<div class="position-live">
            <div class="pos-head"><span class=${`side-tag side-${p.direction}`}>${p.direction.toUpperCase()}</span>
              <b>${p.contracts} × ${p.symbol}</b><span class="spacer"></span><${Pnl} value=${p.unrealized_pnl} chip /></div>
            <dl class="kv">
              <dt>Avg entry</dt><dd>${price(p.avg_price)}</dd>
              <dt>Current price</dt><dd>${price(p.current_price)} <span class="muted">${p.price_age_seconds != null ? `· ${ago(p.price_age_seconds)}` : ''}</span></dd>
              <dt>Stop-loss</dt><dd>${price(p.stop_price)}</dd>
              <dt>Take-profit</dt><dd>${price(p.target_price)}</dd>
              <dt>In trade for</dt><dd>${duration(p.duration_seconds)}</dd>
            </dl></div>`)
          : html`<div class="empty"><${Icon} name="flat" /><b class="sec">Flat</b>No open positions. ${m ? `${m.pending_orders} pending order${m.pending_orders === 1 ? '' : 's'}.` : ''}</div>`}
      </section>
    </div>

    <div class="grid kpi-grid section-gap">
      <${Stat} label="Starting balance" note=${m?.configured_starting_balance != null && Math.abs(m.configured_starting_balance - m.starting_balance) > 0.01 ? `Configured ${money(m.configured_starting_balance)}; shown value implied by broker history` : 'From broker history'}>${money(m?.starting_balance)}<//>
      <${Stat} label="Trades today" note=${ov?.health?.risk ? `Limit ${ov.health.risk.max_trades_per_day}/day` : null}>${m?.trades_today ?? dash}<//>
      <${Stat} label="Total trades" note=${m ? `${m.wins}W · ${m.losses}L${m.open_trades ? ` · ${m.open_trades} open` : ''}` : null}>${m?.total_trades ?? dash}<//>
      <${Stat} label="Win rate">${pct(m?.win_rate, 1)}<//>
      <${Stat} label="Profit factor" note=${m?.profit_factor_infinite ? 'No losing trades yet' : null}>${m?.profit_factor_infinite ? '∞' : isNum(m?.profit_factor) ? m.profit_factor.toFixed(2) : dash}<//>
      <${Stat} label="Average win"><span class="pos">${money(m?.avg_win)}</span><//>
      <${Stat} label="Average loss"><span class="neg">${money(m?.avg_loss)}</span><//>
      <${Stat} label="Largest win"><span class="pos">${money(m?.largest_win)}</span><//>
      <${Stat} label="Largest loss"><span class="neg">${money(m?.largest_loss)}</span><//>
      <${Stat} label="Max drawdown" note=${m ? pct(m.max_drawdown_pct) + ' of peak' : null}>${money(m?.max_drawdown)}<//>
      <${Stat} label="Current drawdown" note=${m ? `Peak ${money(m.peak_equity, { cents: false })}` : null}>${money(m?.current_drawdown)}<//>
      <${Stat} label="Open positions">${m?.open_positions ?? dash}<//>
      <${Stat} label="Pending orders">${m?.pending_orders ?? dash}<//>
      <${Stat} label="Engine uptime" note="Observed by this dashboard">${duration(ov?.health?.engine?.observed_uptime_seconds)}<//>
    </div>

    <section class="card section-gap">
      <div class="card-head"><h3 class="card-title">Recent activity</h3><button class="btn btn-ghost" onClick=${() => go('activity')}>View all</button></div>
      <${Feed} items=${ov?.recent} />
    </section>`;
}

const ACTIVITY_FILTERS = [
  ['all', 'All', null], ['signal', 'Signals', ['signal']], ['trades', 'Entries & exits', ['entry', 'exit']],
  ['stop', 'Stops', ['stop']], ['target', 'Targets', ['target']], ['rejected', 'Rejections', ['rejected', 'risk']],
  ['halt', 'Halts', ['halt']], ['error', 'Errors & warnings', ['error', 'warning']], ['system', 'System', ['system']],
];
function Activity() {
  const { data, error } = usePoll('/api/activity?limit=400', 5000);
  const [f, setF] = useState('all');
  const cats = ACTIVITY_FILTERS.find((x) => x[0] === f)[2];
  const items = (data?.items || []).filter((i) => !cats || cats.includes(i.category));
  return html`
    ${error ? html`<div class="banner banner-warning"><${Icon} name="alert" /><div><b>Could not refresh</b>${error}</div></div>` : null}
    <div class="filters"><div class="chips">${ACTIVITY_FILTERS.map(([k, label]) => html`<button class="chip" aria-pressed=${f === k} onClick=${() => setF(k)}>${label}</button>`)}</div></div>
    <section class="card"><div class="card-head"><h3 class="card-title">Live activity</h3><span class="card-sub">Times in Eastern Time · refreshes every 5s</span></div>
      <${Feed} items=${items} /></section>`;
}

const RANGES = [['today', 'Today'], ['7d', '7D'], ['30d', '30D'], ['all', 'All']];
const RangeSeg = ({ value, set }) => html`<div class="seg" role="group" aria-label="Time range">${RANGES.map(([k, label]) =>
  html`<button aria-pressed=${value === k} onClick=${() => set(k)}>${label}</button>`)}</div>`;

function Trades() {
  const [range, setRange] = useStored('v25.trades.range', 'all');
  const [q, setQ] = useState('');
  const [side, setSide] = useState('');
  const [outcome, setOutcome] = useState('');
  const [reason, setReason] = useState('');
  const params = new URLSearchParams({ range, q, side, outcome, reason }).toString();
  const { data, error } = usePoll(`/api/trades?${params}`, 15000);
  const s = data?.summary;
  return html`
    ${error ? html`<div class="banner banner-warning"><${Icon} name="alert" /><div><b>Could not load trades</b>${error}</div></div>` : null}
    <div class="filters">
      <${RangeSeg} value=${range} set=${setRange} />
      <input class="input search" type="search" placeholder="Search symbol, reason, id…" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Search trades" />
      <select class="select" value=${side} onChange=${(e) => setSide(e.target.value)} aria-label="Side"><option value="">All sides</option><option value="long">Long</option><option value="short">Short</option></select>
      <select class="select" value=${outcome} onChange=${(e) => setOutcome(e.target.value)} aria-label="Outcome"><option value="">All outcomes</option><option value="win">Winners</option><option value="loss">Losers</option><option value="open">Open</option></select>
      <select class="select" value=${reason} onChange=${(e) => setReason(e.target.value)} aria-label="Exit reason"><option value="">All exit reasons</option>${(data?.reasons || []).map((r) => html`<option value=${r}>${r}</option>`)}</select>
      <span class="spacer"></span>
      <a class="btn" href=${`/api/trades.csv?${params}`} download><${Icon} name="download" />Export CSV</a>
    </div>
    <section class="card">
      ${s ? html`<div class="summary-strip"><span><b>${data.trades.length}</b> shown</span><span>Net <${Pnl} value=${s.net_pnl} arrow=${false} /></span>
        <span>Win rate <b>${pct(s.win_rate, 1)}</b></span><span>Profit factor <b>${s.profit_factor_infinite ? '∞' : isNum(s.profit_factor) ? s.profit_factor.toFixed(2) : dash}</b></span>
        <span>Fees <b>${money(s.commissions)}</b></span></div>` : null}
      <div class="table-wrap"><table class="responsive">
        <thead><tr><th>Entry (ET)</th><th>Exit (ET)</th><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Entry</th><th class="num">Exit</th>
          <th class="num">Stop</th><th class="num">Target</th><th class="num">P&L</th><th>Exit reason</th><th class="num">Duration</th></tr></thead>
        <tbody>${data?.trades?.length ? data.trades.map((t) => html`<tr key=${t.id}>
            <td data-label="Entry">${timeET(t.entry_time)}</td><td data-label="Exit">${t.exit_time ? timeET(t.exit_time) : html`<span class="muted">open</span>`}</td>
            <td data-label="Symbol"><b>${t.symbol}</b></td><td data-label="Side"><span class=${`side-tag side-${t.side}`}>${t.side.toUpperCase()}</span></td>
            <td data-label="Qty" class="num">${t.qty}</td><td data-label="Entry price" class="num">${price(t.entry_price)}</td>
            <td data-label="Exit price" class="num">${price(t.exit_price)}</td><td data-label="Stop" class="num">${price(t.stop_price)}</td>
            <td data-label="Target" class="num">${price(t.target_price)}</td>
            <td data-label="P&L" class="num">${t.status === 'closed' ? html`<${Pnl} value=${t.net_pnl} />` : html`<span class="muted">open</span>`}</td>
            <td data-label="Exit reason">${t.exit_reason || dash}</td><td data-label="Duration" class="num">${duration(t.duration_seconds)}</td></tr>`)
          : html`<tr><td colspan="12" class="wide"><div class="empty"><${Icon} name="trades" />${data ? 'No trades match these filters.' : 'Loading…'}</div></td></tr>`}
        </tbody></table></div>
    </section>`;
}

function Analytics() {
  const [range, setRange] = useStored('v25.analytics.range', 'all');
  const { data, error } = usePoll(`/api/analytics?range=${range}`, 15000);
  const s = data?.summary;
  const cvd = document.documentElement.dataset.cvd;
  const series = (arr) => (arr || []).map((p) => ({ x: +new Date(p.t), y: p.v }));
  const eq = useMemo(() => series(data?.equity), [data]);
  const cum = useMemo(() => series(data?.cumulative), [data]);
  const dd = useMemo(() => series(data?.drawdown), [data]);
  const daily = data?.daily || [];
  const dist = data?.distribution || [];
  const wk = data?.weekday || [];
  const sym = data?.by_symbol || [];
  const none = !s || s.total_trades === 0;
  const moneyCol = (key, label) => ({ key, label, num: true, fmt: (v) => money(v) });
  return html`
    ${error ? html`<div class="banner banner-warning"><${Icon} name="alert" /><div><b>Could not load analytics</b>${error}</div></div>` : null}
    <div class="filters"><${RangeSeg} value=${range} set=${setRange} /><span class="card-sub">Scoped by trade exit time (ET). All figures from recorded broker fills.</span></div>
    <div class="grid kpi-grid">
      <${Stat} label="Net P&L"><${Pnl} value=${s?.net_pnl} /><//>
      <${Stat} label="Closed trades">${s?.total_trades ?? dash}<//>
      <${Stat} label="Win rate">${pct(s?.win_rate, 1)}<//>
      <${Stat} label="Profit factor">${s?.profit_factor_infinite ? '∞' : isNum(s?.profit_factor) ? s.profit_factor.toFixed(2) : dash}<//>
      <${Stat} label="Avg win / loss"><span class="pos">${money(s?.avg_win, { cents: false })}</span> <span class="muted">/</span> <span class="neg">${money(s?.avg_loss, { cents: false })}</span><//>
      <${Stat} label="Fees">${money(s?.commissions)}<//>
    </div>
    <div class="grid half section-gap">
      <${ChartCard} title="Equity curve" sub="Account equity after each closed trade" empty=${eq.length < 2} tall
        build=${() => lineChart(eq)} deps=${[eq, cvd]} rows=${data?.equity || []}
        columns=${[{ key: 't', label: 'Time (ET)', fmt: timeET }, moneyCol('v', 'Equity')]} />
      <${ChartCard} title="Cumulative P&L" sub="Net of commissions" empty=${cum.length < 2} tall
        build=${() => lineChart(cum, { fill: true })} deps=${[cum, cvd]} rows=${data?.cumulative || []}
        columns=${[{ key: 't', label: 'Time (ET)', fmt: timeET }, moneyCol('v', 'Cumulative P&L')]} />
      <${ChartCard} title="Daily profit / loss" sub="By exit date (ET)" empty=${!daily.length}
        build=${() => barChart(daily.map((d) => fmtDay.format(new Date(`${d.date}T12:00:00Z`))), daily.map((d) => d.pnl),
          { tooltipExtra: (i) => [`${daily[i].trades} trades · ${daily[i].wins} wins`] })} deps=${[daily, cvd]}
        rows=${daily} columns=${[{ key: 'date', label: 'Date' }, moneyCol('pnl', 'Net P&L'), { key: 'trades', label: 'Trades', num: true }, { key: 'wins', label: 'Wins', num: true }]} />
      <${ChartCard} title="Win / loss distribution" sub="Count of trades by net P&L bucket" empty=${!dist.length}
        build=${() => barChart(dist.map((b) => `${money(b.from, { cents: false })}…${money(b.to, { cents: false })}`), dist.map((b) => b.count),
          { yFmt: (v) => `${v} trades`, colors: dist.map((b) => (b.sign === 'loss' ? css('--neg-mark') : css('--pos-mark'))) })} deps=${[dist, cvd]}
        rows=${dist} columns=${[{ key: 'from', label: 'From', num: true, fmt: (v) => money(v) }, { key: 'to', label: 'To', num: true, fmt: (v) => money(v) }, { key: 'count', label: 'Trades', num: true }]} />
      <${ChartCard} title="Drawdown" sub="Distance below the running equity peak" empty=${dd.length < 2}
        build=${() => lineChart(dd, { fill: true })} deps=${[dd, cvd]} rows=${data?.drawdown || []}
        columns=${[{ key: 't', label: 'Time (ET)', fmt: timeET }, moneyCol('v', 'Drawdown')]} />
      <${ChartCard} title="Performance by weekday" sub="Net P&L by exit day (ET)" empty=${none}
        build=${() => barChart(wk.map((d) => d.day), wk.map((d) => d.pnl), { tooltipExtra: (i) => [`${wk[i].trades} trades · win rate ${pct(wk[i].win_rate, 0)}`] })}
        deps=${[wk, cvd]} rows=${wk} columns=${[{ key: 'day', label: 'Day' }, moneyCol('pnl', 'Net P&L'), { key: 'trades', label: 'Trades', num: true }, { key: 'win_rate', label: 'Win rate', num: true, fmt: (v) => pct(v, 1) }]} />
    </div>
    <section class="card section-gap">
      <div class="card-head"><div><h3 class="card-title">Performance by symbol</h3><div class="card-sub">Contract months are separate symbols (e.g. MNQZ6, MNQH7)</div></div></div>
      ${sym.length >= 2 ? html`<div class="chart-box short"><${ChartCanvas} build=${() => barChart(sym.map((x) => x.symbol), sym.map((x) => x.net_pnl))} deps=${[sym, cvd]} /></div>` : null}
      <div class="table-wrap"><table class="responsive"><thead><tr><th>Symbol</th><th class="num">Trades</th><th class="num">Win rate</th><th class="num">Net P&L</th><th class="num">Profit factor</th><th class="num">Avg win</th><th class="num">Avg loss</th></tr></thead>
        <tbody>${sym.length ? sym.map((x) => html`<tr><td data-label="Symbol"><b>${x.symbol}</b></td><td data-label="Trades" class="num">${x.total_trades}</td>
          <td data-label="Win rate" class="num">${pct(x.win_rate, 1)}</td><td data-label="Net P&L" class="num"><${Pnl} value=${x.net_pnl} /></td>
          <td data-label="Profit factor" class="num">${x.profit_factor_infinite ? '∞' : isNum(x.profit_factor) ? x.profit_factor.toFixed(2) : dash}</td>
          <td data-label="Avg win" class="num">${money(x.avg_win)}</td><td data-label="Avg loss" class="num">${money(x.avg_loss)}</td></tr>`)
          : html`<tr><td colspan="7" class="wide muted">No closed trades in this range yet</td></tr>`}</tbody></table></div>
    </section>`;
}

function Positions({ ov }) {
  const pos = ov?.positions || [], orders = ov?.orders || [];
  return html`
    <${Banners} ov=${ov} />
    <section class="card"><div class="card-head"><h3 class="card-title">Open positions</h3><span class="card-sub">Updates every ${ov?.refresh_seconds ?? 4}s</span></div>
      <div class="table-wrap"><table class="responsive"><thead><tr><th>Symbol</th><th>Direction</th><th class="num">Contracts</th><th class="num">Avg entry</th>
        <th class="num">Current</th><th class="num">Unrealized</th><th class="num">Stop-loss</th><th class="num">Take-profit</th><th class="num">Duration</th></tr></thead>
      <tbody>${pos.length ? pos.map((p) => html`<tr><td data-label="Symbol"><b>${p.symbol}</b></td>
          <td data-label="Direction"><span class=${`side-tag side-${p.direction}`}>${p.direction.toUpperCase()}</span></td>
          <td data-label="Contracts" class="num">${p.contracts}</td><td data-label="Avg entry" class="num">${price(p.avg_price)}</td>
          <td data-label="Current" class="num">${price(p.current_price)}<div class="muted tiny">${p.price_source ? `${p.price_source} · ${ago(p.price_age_seconds)}` : 'no price'}</div></td>
          <td data-label="Unrealized" class="num"><${Pnl} value=${p.unrealized_pnl} /></td>
          <td data-label="Stop-loss" class="num">${price(p.stop_price)}${p.stop_price != null ? html`<div class="muted tiny">${p.stop_qty} covered</div>` : html`<div class="neg tiny">no working stop</div>`}</td>
          <td data-label="Take-profit" class="num">${price(p.target_price)}</td><td data-label="Duration" class="num">${duration(p.duration_seconds)}</td></tr>`)
        : html`<tr><td colspan="9" class="wide"><div class="empty"><${Icon} name="positions" />Flat — no open positions</div></td></tr>`}</tbody></table></div>
    </section>
    <section class="card section-gap"><div class="card-head"><h3 class="card-title">Pending orders</h3><span class="card-sub">Working at the broker</span></div>
      <div class="table-wrap"><table class="responsive"><thead><tr><th>Order</th><th>Role</th><th>Symbol</th><th>Side</th><th>Type</th><th class="num">Qty</th><th class="num">Price</th><th>Status</th><th>Placed (ET)</th></tr></thead>
      <tbody>${orders.length ? orders.map((o) => html`<tr><td data-label="Order" class="tnum">${o.id}</td><td data-label="Role">${o.role.replace('_', ' ')}</td>
          <td data-label="Symbol">${o.symbol}</td><td data-label="Side">${o.side}</td><td data-label="Type">${o.type}</td>
          <td data-label="Qty" class="num">${o.qty - (o.filled_qty || 0)}</td><td data-label="Price" class="num">${price(o.stop_price ?? o.limit_price)}</td>
          <td data-label="Status">${o.status}</td><td data-label="Placed">${timeET(o.created_at)}</td></tr>`)
        : html`<tr><td colspan="9" class="wide muted">No working orders</td></tr>`}</tbody></table></div>
    </section>`;
}

function HealthCard({ title, status, state, children }) {
  return html`<section class=${`card health-card st-${status}`}><h3 class="card-title">${title}</h3>
    <div class="hc-state"><${Icon} name=${STATUS_ICON[status]} />${state}</div>${children}</section>`;
}
function Health({ ov }) {
  const hl = ov?.health;
  if (!hl) return html`<div class="empty">Loading…</div>`;
  const [es, el] = engineStatus(hl);
  const [ts, tl] = tvStatus(hl);
  const tv = hl.tradingview, md = hl.market_data, rec = hl.reconciliation, risk = hl.risk, mode = hl.mode;
  const halted = !!risk?.halt;
  const dayUsed = isNum(risk?.day_pnl) && risk.day_pnl < 0 ? Math.abs(risk.day_pnl) : 0;
  return html`
    <${Banners} ov=${ov} />
    <div class="grid health-grid">
      <${HealthCard} title="Bot engine" status=${es} state=${el}>
        <p>${hl.engine.online ? `Responding · monitor ${hl.engine.monitor_ok ? 'running' : 'NOT running'} · broker ${hl.checks.broker_connected ? 'connected' : 'disconnected'}` : hl.error}</p>
        <p class="muted">Monitor last cycle ${timeET(hl.engine.monitor_last_cycle)}${hl.engine.monitor_last_error ? ` · last error ${hl.engine.monitor_last_error}` : ''}</p>
        <p class="muted">Observed online ${duration(hl.engine.observed_uptime_seconds)} (since ${timeET(hl.engine.observed_up_since)})</p>
      <//>
      <${HealthCard} title="Docker" status=${hl.docker.engine_reachable ? 'good' : 'critical'} state=${hl.docker.engine_reachable ? 'Engine container reachable' : 'Engine container unreachable'}>
        <p>${hl.docker.dashboard_in_container ? 'Dashboard is running in its own container.' : 'Dashboard is running outside Docker.'} Checked over ${hl.docker.engine_url}.</p>
        <p class="muted">The dashboard has no access to the Docker API by design; status is inferred from the engine responding.</p>
      <//>
      <${HealthCard} title="TradingView feed" status=${ts} state=${tl}>
        <p>Last bar/heartbeat ${tv.last_seen ? `${timeET(tv.last_seen)} (${ago(tv.age_seconds)})` : 'never received'}.</p>
        <p class="muted">"Receiving" only when data arrived within ${Math.round(tv.live_threshold_seconds)}s. CME equity futures are ${tv.market_open ? 'open' : 'closed'} now. A past timestamp alone is never treated as a live connection.</p>
      <//>
      <${HealthCard} title="Last TradingView signal" status="neutral" state=${tv.last_signal ? `${tv.last_signal.kind} · ${tv.last_signal.status}` : 'None received'}>
        <p>${tv.last_signal ? `${timeET(tv.last_signal.ts)} (${ago(tv.last_signal.age_seconds)})` : 'No entry, exit or flatten alert has reached the engine yet.'}</p>
        ${tv.last_signal ? html`<p class="muted tnum">${tv.last_signal.event_id}</p>` : null}
      <//>
      <${HealthCard} title="Market data" status=${!md ? 'warning' : md.age_seconds <= 90 ? 'good' : 'warning'} state=${!md ? 'No prices yet' : md.age_seconds <= 90 ? 'Fresh' : `Stale · ${ago(md.age_seconds)}`}>
        <p>${md ? `${md.symbol} ${price(md.last)} from ${md.source} at ${timeET(md.ts)}` : 'The engine has not received a price since it started.'}</p>
      <//>
      <${HealthCard} title="Reconciliation" status=${rec.needs_reconcile ? 'warning' : rec.last ? 'good' : 'warning'} state=${rec.needs_reconcile ? 'Pending' : rec.last ? `OK · ${ago(rec.age_seconds)}` : 'Not yet run'}>
        <p>Last successful: ${timeET(rec.last)}</p>
      <//>
      <${HealthCard} title="Broker mode" status=${mode.paper_only && !mode.live_enabled ? 'good' : 'critical'} state=${mode.paper_only ? 'PAPER ONLY' : String(mode.mode || 'unknown')}>
        <p>Broker: ${mode.broker || dash} · live trading ${mode.live_enabled ? 'ENABLED' : 'disabled'}</p>
      <//>
      <${HealthCard} title="Risk management" status=${halted ? 'critical' : 'good'} state=${halted ? 'Blocking new entries' : 'Active'}>
        <p>Trades today ${risk.trades_today ?? dash}/${risk.max_trades_per_day} · day P&L <${Pnl} value=${risk.day_pnl} arrow=${false} /> of −${money(risk.daily_loss_limit, { cents: false })} limit${dayUsed ? ` (${pct((dayUsed / risk.daily_loss_limit) * 100, 0)} used)` : ''}</p>
        <p class="muted">Drawdown floor ${money(risk.drawdown_floor)} · room ${money(risk.drawdown_room)} (buffer ${money(risk.drawdown_buffer, { cents: false })}) · max ${risk.max_contracts} contracts, ${money(risk.max_trade_risk, { cents: false })}/trade · entries ${risk.entry_window}, flat ${risk.flatten_time}</p>
      <//>
      <${HealthCard} title="Trading halt" status=${halted ? 'critical' : 'good'} state=${halted ? `Halted · ${risk.halt.scope}` : 'Not halted'}>
        <p>${halted ? risk.halt.reason : 'New entries are allowed when the risk checks pass.'}</p>
        ${halted && risk.halt.since ? html`<p class="muted">Since ${timeET(risk.halt.since)}</p>` : null}
      <//>
    </div>
    <section class="card section-gap"><div class="card-head"><h3 class="card-title">Recent system errors</h3><span class="card-sub">From the engine's audit log</span></div>
      ${hl.errors?.length ? html`<ul class="err-list">${hl.errors.map((e) => html`<li><span class=${`lvl lvl-${e.level}`}>${e.level}</span><b>${e.kind}</b> · <span class="muted">${timeET(e.ts)}</span><div class="sec">${e.message}</div></li>`)}</ul>`
        : html`<div class="empty"><${Icon} name="check" />No errors recorded.</div>`}
    </section>`;
}

// =================================================================== shell
const PAGES = [
  ['overview', 'Overview', 'overview'], ['activity', 'Activity', 'activity'], ['trades', 'Trades', 'trades'],
  ['analytics', 'Analytics', 'analytics'], ['positions', 'Positions', 'positions'], ['health', 'Health', 'health'],
];
const pageFromHash = () => { const p = location.hash.replace('#/', ''); return PAGES.some((x) => x[0] === p) ? p : 'overview'; };

function App() {
  const [page, setPage] = useState(pageFromHash());
  const [cvd, setCvd] = useStored('v25.cvd', 'off');
  const [refreshMs, setRefreshMs] = useState(4000);
  const ov = usePoll('/api/overview', refreshMs);
  useEffect(() => { const f = () => setPage(pageFromHash()); addEventListener('hashchange', f); return () => removeEventListener('hashchange', f); }, []);
  // Set before children render so charts read the new palette in the same pass (idempotent).
  document.documentElement.dataset.cvd = cvd;
  useEffect(() => { const s = ov.data?.refresh_seconds; if (isNum(s)) setRefreshMs(s * 1000); }, [ov.data?.refresh_seconds]);
  useEffect(() => { document.title = `${PAGES.find((p) => p[0] === page)[1]} · V25`; }, [page]);
  const go = (p) => { location.hash = `#/${p}`; scrollTo(0, 0); };
  const logout = async () => { try { await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' }); } finally { location.href = '/login'; } };

  const hl = ov.data?.health;
  const [es, el] = engineStatus(hl);
  const [ts, tl] = tvStatus(hl);
  const modeVal = hl?.mode?.paper_only ? 'PAPER' : hl?.mode?.mode || dash;
  const view = { overview: Overview, activity: Activity, trades: Trades, analytics: Analytics, positions: Positions, health: Health }[page];
  return html`<div class="shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">V25</div><div><div class="brand-name">V25 Monitor</div><div class="brand-sub">ICT FVG · MNQ</div></div></div>
      <nav class="nav" aria-label="Sections">${PAGES.map(([k, label, icon]) => html`<button aria-current=${page === k ? 'page' : null} onClick=${() => go(k)}><${Icon} name=${icon} />${label}</button>`)}</nav>
      <div class="sidebar-foot">
        <div class="mode-badge"><${Icon} name="info" /><span><strong>PAPER ONLY</strong> · read-only monitor</span></div>
        <div class="toggle-row"><span>Colour-blind palette</span><button class="switch" role="switch" aria-checked=${cvd === 'on'} aria-label="Colour-blind safe palette" onClick=${() => setCvd(cvd === 'on' ? 'off' : 'on')}></button></div>
        <button class="btn btn-ghost" onClick=${logout}><${Icon} name="logout" />Sign out</button>
      </div>
    </aside>
    <main class="main">
      <header class="topbar">
        <h1 class="page-title">${PAGES.find((p) => p[0] === page)[1]}</h1>
        <div class="top-pills">
          <${Pill} status=${es} label="Engine" value=${el} />
          <${Pill} status=${ts} label="TradingView" value=${tl} pulse=${ts === 'good'} />
          <${Pill} status=${hl?.mode?.live_enabled ? 'critical' : 'neutral'} label="Mode" value=${modeVal} />
          <button class="btn btn-ghost mobile-only" onClick=${logout} aria-label="Sign out"><${Icon} name="logout" /></button>
        </div>
        <span class="updated">${ov.error ? html`<span class="neg">Update failed: ${ov.error}</span>` : ov.at ? `Updated ${clockET(ov.at)}` : 'Connecting…'}</span>
      </header>
      <div class="content" key=${page}><${view} ov=${ov.data} go=${go} /></div>
    </main>
    <nav class="tabbar" aria-label="Sections">${PAGES.map(([k, label, icon]) => html`<button aria-current=${page === k ? 'page' : null} onClick=${() => go(k)}><${Icon} name=${icon} />${label}</button>`)}</nav>
  </div>`;
}

render(html`<${App} />`, document.getElementById('app'));
