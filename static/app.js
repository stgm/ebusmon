// FIELDS, CHART_GROUPS are injected by the server as inline <script> in the HTML template.

// Client-side overrides — room_temp provides unit (not available from ebusd)
const DERIVED_FIELDS = {
  "room_temp": [null, "Room temp", "°C"],
};

// All fields for UI building
const ALL_FIELDS = { ...FIELDS, ...DERIVED_FIELDS };

// Chart colour palette
const PALETTE = [
  '#00d4ff','#ff6b35','#39ff14','#f7c59f',
  '#b388ff','#4dd0e1','#ffb300','#e91e63',
  '#ff3b3b','#a0e0ff','#ffe066','#b0ffb0'
];

// ── State ────────────────────────────────────────────────────────────────────
const chartMap   = {};   // primary_key → Chart.js instance
const kpiMap     = {};   // key → DOM element for KPI value
const keyToChart = {};   // any key → primary_key (for pushPoint lookup)
let   viewingDate = null;

// ── Build KPI strip & chart grid ─────────────────────────────────────────────
function buildUI() {
  const strip = document.getElementById('kpi-strip');
  const grid  = document.getElementById('charts-grid');
  let ci = 0;

  for (const group of CHART_GROUPS) {
    const primaryKey   = group[0];
    const secondaryKey = group[1] || null;

    const [, primaryLabel, primaryUnit] = ALL_FIELDS[primaryKey] || [null, primaryKey, ''];
    const secondaryInfo = secondaryKey ? ALL_FIELDS[secondaryKey] : null;
    const [, secLabelFull] = secondaryInfo || [null, ''];
    const secLabel = secLabelFull ? secLabelFull.split(' ')[0] : '';

    const color1 = PALETTE[ci++ % PALETTE.length];
    const color2 = secondaryKey ? PALETTE[ci++ % PALETTE.length] : null;

    // ── KPI tile ──────────────────────────────────────────────────────────────
    const kpi = document.createElement('div');
    kpi.className = 'kpi';
    if (secondaryKey) {
      kpi.innerHTML = `
        <div class="kpi-label">${primaryLabel} <span style="opacity:.5">/ ${secLabel}</span></div>
        <div style="display:flex;align-items:baseline;gap:.4rem">
          <span class="kpi-value" id="kpi-${primaryKey}">—</span>
          <span style="color:var(--muted);font-size:.8rem;margin-left:.1rem">
            / <span id="kpi-${secondaryKey}" style="font-family:var(--mono);font-size:1rem;color:var(--accent)">—</span>
          </span>
          <span class="kpi-unit">${primaryUnit}</span>
        </div>`;
    } else {
      kpi.innerHTML = `
        <div class="kpi-label">${primaryLabel}</div>
        <span class="kpi-value" id="kpi-${primaryKey}">—</span>
        <span class="kpi-unit">${primaryUnit}</span>`;
    }
    strip.appendChild(kpi);
    kpiMap[primaryKey] = kpi.querySelector(`#kpi-${primaryKey}`);
    if (secondaryKey) kpiMap[secondaryKey] = kpi.querySelector(`#kpi-${secondaryKey}`);

    // ── Chart card ────────────────────────────────────────────────────────────
    const card = document.createElement('div');
    card.className = 'chart-card';
    const chartTitle = secondaryKey
      ? `${primaryLabel} <span style="opacity:.5">/ ${secLabel}</span>${primaryUnit ? ' (' + primaryUnit + ')' : ''}`
      : `${primaryLabel}${primaryUnit ? ' (' + primaryUnit + ')' : ''}`;
    card.innerHTML = `<div class="chart-title">${chartTitle}</div>
      <canvas id="chart-${primaryKey}" height="120"></canvas>`;
    grid.appendChild(card);

    const ctx = card.querySelector(`#chart-${primaryKey}`).getContext('2d');
    const dayStart = new Date(); dayStart.setHours(0,0,0,0);
    const dayEnd   = new Date(); dayEnd.setHours(23,59,59,999);

    const datasets = [{
      data: [],
      borderColor: color1,
      backgroundColor: color1 + '18',
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0.3,
      fill: true,
      spanGaps: 10 * 60 * 1000,
      label: primaryLabel,
    }];

    if (secondaryKey) {
      datasets.push({
        data: [],
        borderColor: color2,
        backgroundColor: 'transparent',
        borderWidth: 1.5,
        borderDash: [4, 3],
        pointRadius: 0,
        tension: 0.3,
        fill: false,
        spanGaps: 10 * 60 * 1000,
        label: secLabel,
      });
    }

    chartMap[primaryKey] = new Chart(ctx, {
      type: 'line',
      data: { datasets },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: {
            display: !!secondaryKey,
            labels: {
              color: '#4a6070',
              font: { family: "'Share Tech Mono'", size: 9 },
              boxWidth: 20, boxHeight: 1,
            }
          }
        },
        scales: {
          x: {
            type: 'time',
            min: dayStart, max: dayEnd,
            time: { unit: 'hour', displayFormats: { hour: 'HH:mm' }, tooltipFormat: 'HH:mm:ss' },
            ticks: { color: '#4a6070', font: { family: "'Share Tech Mono'", size: 9 },
                     maxTicksLimit: window.innerWidth <= 600 ? 4 : 12, maxRotation: 0 },
            grid: { color: '#0f1e2e' }
          },
          y: {
            ticks: { color: '#4a6070', font: { family: "'Share Tech Mono'", size: 9 } },
            grid:  { color: '#0f1e2e' }
          }
        }
      }
    });

    keyToChart[primaryKey] = primaryKey;
    if (secondaryKey) keyToChart[secondaryKey] = primaryKey;
  }
}

// ── Mode indicators ───────────────────────────────────────────────────────────
function updateIndicators(active) {
  document.querySelectorAll('[data-indicator]').forEach((el, idx) => {
    el.className = 'badge' + (active.includes(el.dataset.indicator) ? ` active-${idx}` : '');
  });
}

// ── Retroactively patch an out-of-bounds point in the chart ──────────────────
function fixPoint(key, ts, value) {
  const primaryKey = keyToChart[key];
  const chart = chartMap[primaryKey];
  if (!chart) return;
  const dsIdx = primaryKey === key ? 0 : 1;
  // Replace 'T' with space: ISO strings with 'T' and no timezone are parsed as
  // UTC by browsers, but we store local timestamps — space forces local parsing.
  const t = new Date(ts.replace('T', ' ')).getTime();
  const data = chart.data.datasets[dsIdx].data;
  for (let i = data.length - 1; i >= 0; i--) {
    if (data[i].x.getTime() === t) {
      data[i] = { x: data[i].x, y: value };
      chart.update('none');
      break;
    }
  }
}

// ── Push a data point into chart + KPI ──────────────────────────────────────
function pushPoint(key, ts, value) {
  const primaryKey = keyToChart[key];
  const chart = chartMap[primaryKey];
  if (!chart) return;
  const dsIdx = primaryKey === key ? 0 : 1;
  // Replace 'T' with space: ISO strings with 'T' and no timezone are parsed as
  // UTC by browsers, but we store local timestamps — space forces local parsing.
  const t = new Date(ts.replace('T', ' '));
  chart.data.datasets[dsIdx].data.push({ x: t, y: value });
  chart.update('none');

  // Update KPI
  const el = kpiMap[key];
  if (el) {
    el.textContent = Number.isInteger(value) ? value : value.toFixed(1);
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 600);
  }
}

// ── Log formatter ────────────────────────────────────────────────────────────
function appendLog(line) {
  const box = document.getElementById('log-box');
  const m = line.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\S*\s+(.+?received\s+)(.+)$/);
  const span = document.createElement('span');
  span.className = 'ln';
  if (m) {
    span.innerHTML =
      `<span class="ts">${m[1]}</span> ` +
      `<span class="msg">${m[2]}</span>` +
      `<span class="val">${m[3]}</span>`;
  } else {
    span.textContent = line;
  }
  box.appendChild(span);
  box.scrollTop = box.scrollHeight;
  while (box.children.length > 200) box.removeChild(box.firstChild);
}

// ── Date picker ───────────────────────────────────────────────────────────────
async function populateDatePicker() {
  try {
    const r = await fetch('/api/dates');
    const dates = await r.json();
    const sel = document.getElementById('date-select');
    const today = new Date().toISOString().substring(0, 10);
    for (const d of dates.reverse()) {
      if (d === today) continue;
      const opt = document.createElement('option');
      opt.value = d;
      opt.textContent = d;
      sel.appendChild(opt);
    }
  } catch(e) { console.warn('dates load failed', e); }
}

function clearCharts(dayStr) {
  const d     = new Date(dayStr);
  const start = new Date(d); start.setHours(0,0,0,0);
  const end   = new Date(d); end.setHours(23,59,59,999);
  for (const chart of Object.values(chartMap)) {
    chart.data.datasets.forEach(ds => ds.data = []);
    chart.options.scales.x.min = start;
    chart.options.scales.x.max = end;
    chart.update('none');
  }
}

async function onDateChange(dateStr) {
  if (!dateStr) return;
  viewingDate = dateStr;
  document.getElementById('btn-live').style.display = 'inline-block';
  document.getElementById('status-dot').style.display = 'none';
  clearCharts(dateStr);
  await loadHistory(dateStr);
}

async function returnToLive() {
  viewingDate = null;
  document.getElementById('btn-live').style.display = 'none';
  document.getElementById('status-dot').style.display = 'block';
  document.getElementById('date-select').value = '';
  const today = new Date().toISOString().substring(0, 10);
  clearCharts(today);
  const start = new Date(); start.setHours(0,0,0,0);
  const end   = new Date(); end.setHours(23,59,59,999);
  for (const chart of Object.values(chartMap)) {
    chart.options.scales.x.min = start;
    chart.options.scales.x.max = end;
  }
  await loadHistory();
}

// ── SSE connection ────────────────────────────────────────────────────────────
function connect() {
  const es = new EventSource('/api/stream');

  es.onopen = () => {
    document.getElementById('status-dot').classList.remove('offline');
  };

  es.onerror = () => {
    document.getElementById('status-dot').classList.add('offline');
  };

  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    const now = new Date().toISOString().replace('T',' ').substring(0,19);
    document.getElementById('last-update').textContent = 'last update: ' + now;

    if (msg.type === 'midnight') {
      if (!viewingDate) {
        const newStart = new Date(); newStart.setHours(0,0,0,0);
        const newEnd   = new Date(); newEnd.setHours(23,59,59,999);
        for (const chart of Object.values(chartMap)) {
          chart.data.datasets.forEach(ds => ds.data = []);
          chart.options.scales.x.min = newStart;
          chart.options.scales.x.max = newEnd;
          chart.update('none');
        }
      }
      return;
    }

    if (msg.indicators) updateIndicators(msg.indicators);

    if (viewingDate) return;

    if (msg.type === 'snapshot' || msg.type === 'update') {
      for (const [key, point] of Object.entries(msg.data || {})) {
        if (key === '_fixes') continue;
        if (point) pushPoint(key, point.ts, point.value);
      }
      for (const fix of (msg.data?._fixes || [])) {
        fixPoint(fix.key, fix.ts, fix.value);
      }
    }
    if (msg.type === 'log') {
      appendLog(msg.line);
    }
  };
}

// ── Load history ─────────────────────────────────────────────────────────────
async function loadHistory(dateStr) {
  try {
    const url = dateStr ? `/api/history?date=${dateStr}` : '/api/history';
    const r = await fetch(url);
    const h = await r.json();

    for (const [key, points] of Object.entries(h)) {
      if (key === 'latest' || key === 'logs' || key === 'indicators' || !Array.isArray(points)) continue;
      const primaryKey = keyToChart[key];
      if (!primaryKey) continue;
      const chart = chartMap[primaryKey];
      if (!chart) continue;
      const dsIdx = primaryKey === key ? 0 : 1;
      const data  = chart.data.datasets[dsIdx].data;
      for (const p of points) {
        data.push({ x: new Date(p.ts.replace('T', ' ')), y: p.value });
      }
    }

    for (const chart of Object.values(chartMap)) chart.update('none');

    (h.logs || []).forEach(appendLog);
    if (h.indicators) updateIndicators(h.indicators);

    if (h.latest) {
      for (const [key, meta] of Object.entries(h.latest)) {
        const el = kpiMap[key];
        if (el && meta.value != null) {
          el.textContent = Number.isInteger(meta.value) ? meta.value : meta.value.toFixed(1);
        }
      }
    }
  } catch(e) { console.warn('history load failed', e); }
}

// ── Init ─────────────────────────────────────────────────────────────────────
buildUI();
populateDatePicker();
loadHistory().then(() => connect());
