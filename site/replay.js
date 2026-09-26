// Replay viewer for exported test runs (experiments/export_replay.py -> site/replays/).
// Plain JS + SVG, no dependencies. State lives in the URL hash so a view can be shared.

const IFACES = {
  wifi0: { label: "Wi-Fi", color: "var(--net-wifi0)" },
  cell0: { label: "Cellular", color: "var(--net-cell0)" },
  sat0: { label: "Satellite", color: "var(--net-sat0)" },
};
const SOURCES = {
  push: { label: "Prefetched", color: "var(--src-push)" },
  revalidated: { label: "Cached copy", color: "var(--src-cache)" },
  stale: { label: "Cached copy", color: "var(--src-cache)" },
  origin: { label: "Downloaded", color: "var(--src-net)" },
  pending: { label: "Still waiting at the end", color: "var(--ink-muted)" },
};
// The same names and descriptions as on the home page.
const SETUPS = {
  "1": { name: "Ordinary (TCP)", about: "Reconnects only after the link has failed, as most apps do" },
  "2": { name: "Modern (QUIC)", about: "Survives network changes, but doesn't prefetch" },
  "3": { name: "Always-on prefetch", about: "A few likely pages pushed after every page view" },
  "4": { name: "Hover prefetch", about: "The clicked page is fetched 200 ms before the click" },
  "5": { name: "Shajarah", about: "Predicts the dropout and prefetches ahead of it; switches network early when it can" },
  "5a": { name: "Shajarah, switching only", about: "Switches network early, no prefetching" },
  "5b": { name: "Shajarah, prefetch only", about: "Prefetches on a dropout warning, no early switching" },
};
// Opens on the clearest tunnel example: the reader Shajarah saved the most waiting for.
const DEFAULT = { scenario: "car_tunnel_45s", session: "5", a: "5", b: "1" };
const SCENARIO_NAMES = {
  car_tunnel_45s: "Car tunnel (45 s without coverage)",
  wifi_to_5g_walk: "Walking out of Wi-Fi range",
};
const DBM_MIN = -140, DBM_MAX = -40;
const PAD = { l: 70, r: 12 };
const SPEEDS = [1, 5, 10, 20];

const app = document.getElementById("app");
const tooltip = document.getElementById("tooltip");
const cache = new Map(); // file -> run JSON
const state = { runs: [], scenario: null, session: null, a: null, b: null, t: 0, speed: 5, playing: false };
let lanes = []; // [{ run, root, width }]

// ---------------------------------------------------------------------------- helpers

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmtT = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const setupName = (c) => (SETUPS[c] || { name: `Setup ${c}` }).name;
const setupAbout = (c, fallback = "") => (SETUPS[c] || { about: fallback }).about;
const configLabel = (r) => setupName(r.config);

function lastAtOrBefore(arr, t, key = (x) => x.t) {
  let lo = 0, hi = arr.length - 1, best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (key(arr[mid]) <= t) { best = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return best;
}

function activeAt(run, t) {
  let iface = run.start_iface;
  for (const s of run.switches) if (s.t <= t) iface = s.to;
  return iface;
}

function signalAt(run, iface, t) {
  const i = lastAtOrBefore(run.signal.t, t, (x) => x);
  return i < 0 ? null : run.signal.dbm[iface][i];
}

function warningOn(run, iface, t) {
  let on = false;
  for (const w of run.warnings) if (w.iface === iface && w.t <= t) on = w.on;
  return on;
}

function readHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  return {
    scenario: h.get("scenario"), session: h.get("reader"), a: h.get("a"), b: h.get("b"),
    t: parseFloat(h.get("t") || "0") || 0,
  };
}

function writeHash() {
  const h = new URLSearchParams({
    scenario: state.scenario, reader: state.session, a: state.a, b: state.b || "none",
    t: state.t.toFixed(1),
  });
  history.replaceState(null, "", `#${h}`);
}

// ---------------------------------------------------------------------------- loading

async function load() {
  let index;
  try {
    const r = await fetch("replays/index.json", { cache: "no-cache" });
    if (!r.ok) throw new Error(r.status);
    index = await r.json();
  } catch {
    index = { runs: [] };
  }
  state.runs = index.runs || [];
  if (!state.runs.length) {
    app.innerHTML = `<div class="empty"><p><b>No runs exported yet.</b></p>
      <p>On the machine with the results, run
      <code>python -m experiments.export_replay main</code>, then commit <code>site/replays/</code>.</p></div>`;
    return;
  }
  const want = readHash();
  const scenarios = [...new Set(state.runs.map((r) => r.scenario))];
  state.scenario = scenarios.includes(want.scenario) ? want.scenario
    : (scenarios.includes(DEFAULT.scenario) ? DEFAULT.scenario : scenarios[0]);
  const sessions = sessionsFor(state.scenario);
  state.session = sessions.includes(want.session) ? want.session
    : (sessions.includes(DEFAULT.session) ? DEFAULT.session : sessions[0]);
  const configs = configsFor(state.scenario, state.session);
  state.a = configs.includes(want.a) ? want.a : (configs.includes(DEFAULT.a) ? DEFAULT.a : configs[0]);
  state.b = want.b === "none" ? null : configs.includes(want.b) ? want.b
    : (configs.includes(DEFAULT.b) && state.a !== DEFAULT.b ? DEFAULT.b : null);
  state.t = want.t;
  buildControls();
  await showRuns();
}

const sessionsFor = (sc) => [...new Set(state.runs.filter((r) => r.scenario === sc).map((r) => String(r.session)))]
  .sort((x, y) => x - y);
const configsFor = (sc, se) => state.runs.filter((r) => r.scenario === sc && String(r.session) === se)
  .map((r) => r.config).sort();
const entry = (config) => state.runs.find((r) => r.scenario === state.scenario
  && String(r.session) === state.session && r.config === config);

async function getRun(e) {
  if (!cache.has(e.file)) {
    const r = await fetch(`replays/${e.file}`);
    cache.set(e.file, await r.json());
  }
  return cache.get(e.file);
}

// ---------------------------------------------------------------------------- controls

function options(values, selected, label = (v) => v) {
  return values.map((v) => `<option value="${esc(v)}"${v === selected ? " selected" : ""}>${esc(label(v))}</option>`).join("");
}

function buildControls() {
  const scenarios = [...new Set(state.runs.map((r) => r.scenario))];
  const sessions = sessionsFor(state.scenario);
  const configs = configsFor(state.scenario, state.session);
  const about = (c) => setupName(c);
  app.innerHTML = `
    <div class="controls">
      <label>Scenario<select id="scenario">${options(scenarios, state.scenario, (s) => SCENARIO_NAMES[s] || s)}</select></label>
      <label>Reader<select id="session">${options(sessions, state.session, (s) => `Reader ${sessions.indexOf(s) + 1}`)}</select></label>
      <label>Setup<select id="a">${options(configs, state.a, about)}</select></label>
      <label>Compare with<select id="b"><option value="none">(none)</option>${options(configs, state.b, about)}</select></label>
      <div class="timebar">
        <button class="primary" id="play">Play</button>
        <select id="speed" aria-label="Playback speed">${options(SPEEDS.map(String), String(state.speed), (s) => `${s}×`)}</select>
        <input type="range" id="time" min="0" max="120" step="0.1" value="0" aria-label="Time">
        <span class="clock" id="clock">0:00</span>
      </div>
    </div>
    <div class="lanes" id="lanes"></div>`;
  const on = (id, ev, fn) => document.getElementById(id).addEventListener(ev, fn);
  on("scenario", "change", (e) => { state.scenario = e.target.value; resetSelection(); });
  on("session", "change", (e) => { state.session = e.target.value; resetSelection(); });
  on("a", "change", (e) => { state.a = e.target.value; showRuns(); });
  on("b", "change", (e) => { state.b = e.target.value === "none" ? null : e.target.value; showRuns(); });
  on("speed", "change", (e) => { state.speed = +e.target.value; });
  on("time", "input", (e) => { setTime(+e.target.value); });
  on("play", "click", togglePlay);
}

function resetSelection() {
  const sessions = sessionsFor(state.scenario);
  if (!sessions.includes(state.session)) state.session = sessions[0];
  const configs = configsFor(state.scenario, state.session);
  if (!configs.includes(state.a)) state.a = configs.includes("5") ? "5" : configs[0];
  if (state.b && !configs.includes(state.b)) state.b = null;
  state.t = 0;
  buildControls();
  showRuns();
}

// ---------------------------------------------------------------------------- playback

let lastFrame = null;
function togglePlay() {
  state.playing = !state.playing;
  if (state.playing && state.t >= duration() - 0.05) setTime(0);
  document.getElementById("play").textContent = state.playing ? "Pause" : "Play";
  lastFrame = null;
  if (state.playing) requestAnimationFrame(tick);
}

function tick(now) {
  if (!state.playing) return;
  if (lastFrame !== null) setTime(state.t + ((now - lastFrame) / 1000) * state.speed);
  lastFrame = now;
  if (state.t >= duration()) { togglePlay(); return; }
  requestAnimationFrame(tick);
}

const duration = () => Math.max(1, ...lanes.map((l) => l.run.duration));

function setTime(t) {
  state.t = clamp(t, 0, duration());
  const slider = document.getElementById("time");
  if (slider) slider.value = state.t;
  const clock = document.getElementById("clock");
  if (clock) clock.textContent = `${fmtT(state.t)} / ${fmtT(duration())}`;
  for (const lane of lanes) updateLane(lane);
  writeHash();
}

// ---------------------------------------------------------------------------- lanes

async function showRuns() {
  const picks = [state.a, state.b].filter((c, i, all) => c && all.indexOf(c) === i);
  const runs = await Promise.all(picks.map((c) => getRun(entry(c))));
  const box = document.getElementById("lanes");
  box.className = `lanes${runs.length > 1 ? " two" : ""}`;
  box.innerHTML = "";
  lanes = runs.map((run) => {
    const root = document.createElement("section");
    root.className = "lane";
    box.appendChild(root);
    return { run, root, width: 0 };
  });
  const slider = document.getElementById("time");
  slider.max = duration();
  for (const lane of lanes) drawLane(lane);
  setTime(state.t);
}

const resizer = new ResizeObserver(() => {
  for (const lane of lanes) {
    if (Math.abs(lane.root.clientWidth - lane.width) > 4) { drawLane(lane); updateLane(lane); }
  }
});

function drawLane(lane) {
  const { run, root } = lane;
  lane.width = root.clientWidth;
  const W = Math.max(280, root.clientWidth - 28);
  const x = (t) => PAD.l + (t / run.duration) * (W - PAD.l - PAD.r);
  lane.x = x;
  lane.W = W;
  const shown = run.interfaces.filter((i) => run.zones[i].some((z) => z[2] !== "dead")
    || run.signal.dbm[i].some((v) => v !== null && v > DBM_MIN + 1));
  lane.shown = shown;

  root.innerHTML = `
    <h2>${esc(configLabel(run))}</h2>
    <p class="sub">${esc(setupAbout(run.config, run.about || ""))}</p>
    <div class="tiles" data-role="tiles"></div>
    <div class="chart"><div class="title">Coverage along the way (brighter = stronger signal; outline = network in use)</div>${sceneSvg(run, shown, x, W)}</div>
    <div class="chart"><div class="title">Signal strength (dBm)</div>${signalSvg(run, shown, x, W)}
      <div class="legend">${shown.map((i) => `<span><i class="swatch" style="background:${IFACES[i].color}"></i>${IFACES[i].label}</span>`).join("")}
        <span><i class="swatch" style="background:var(--ink-muted)"></i>unusable below (dashed)</span></div></div>
    <div class="chart"><div class="title">Latency: ping round trip through the tunnel (ms)</div>${latencySvg(run, x, W)}</div>
    <div class="chart"><div class="title">Pages: time spent waiting after each click, and pages the server pushed</div>${pagesSvg(run, x, W)}
      <div class="legend">${legendSources(run)}</div></div>
    <details><summary>Page table</summary>${pageTable(run)}</details>`;
  root.querySelectorAll("svg").forEach((svg) => {
    svg.addEventListener("pointerdown", (e) => scrub(e, lane));
    svg.addEventListener("pointermove", (e) => { if (e.buttons) scrub(e, lane); });
  });
  root.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("pointerenter", (e) => showTip(e, el.dataset.tip));
    el.addEventListener("pointermove", (e) => moveTip(e));
    el.addEventListener("pointerleave", hideTip);
  });
  resizer.observe(root);
}

function scrub(e, lane) {
  const svg = e.currentTarget;
  const box = svg.getBoundingClientRect();
  const px = ((e.clientX - box.left) / box.width) * lane.W;
  const t = ((px - PAD.l) / (lane.W - PAD.l - PAD.r)) * lane.run.duration;
  setTime(t);
}

function showTip(e, html) { tooltip.innerHTML = html; tooltip.style.display = "block"; moveTip(e); }
function moveTip(e) {
  const pad = 14, w = tooltip.offsetWidth, h = tooltip.offsetHeight;
  tooltip.style.left = `${Math.min(e.clientX + pad, innerWidth - w - 8)}px`;
  tooltip.style.top = `${Math.max(8, e.clientY - h - pad)}px`;
}
function hideTip() { tooltip.style.display = "none"; }

// Shared frame: outage band, grid of 10 s ticks, cursor.
function frame(run, x, W, H, top = 0, bottom = H, axis = false) {
  let s = "";
  if (run.outage) s += `<rect class="outage" x="${x(run.outage[0])}" y="${top}" width="${x(run.outage[1]) - x(run.outage[0])}" height="${bottom - top}"/>`;
  const step = run.duration > 90 ? 20 : 10;
  for (let t = 0; t <= run.duration + 0.01; t += step) {
    s += `<line class="grid" x1="${x(t)}" x2="${x(t)}" y1="${top}" y2="${bottom}"/>`;
    if (axis) s += `<text x="${x(t)}" y="${bottom + 13}" text-anchor="middle">${fmtT(t)}</text>`;
  }
  return s;
}
const cursor = (H) => `<line class="cursor" data-role="cursor" x1="0" x2="0" y1="0" y2="${H}"/>`;

function sceneSvg(run, shown, x, W) {
  const row = 22, H = shown.length * row + 18;
  let s = frame(run, x, W, H, 0, H - 18, true);
  shown.forEach((iface, k) => {
    const y = k * row + 3, h = row - 8, thr = run.thresholds[iface];
    s += `<text x="${PAD.l - 8}" y="${y + h - 3}" text-anchor="end" class="ink">${IFACES[iface].label}</text>`;
    // Coverage: one cell per signal sample, opacity from how far above the usable limit it is.
    const ts = run.signal.t;
    for (let j = 0; j < ts.length; j++) {
      const t1 = j + 1 < ts.length ? ts[j + 1] : run.duration;
      const dbm = run.signal.dbm[iface][j];
      const dead = zoneAt(run, iface, ts[j]) === "dead";
      if (dbm === null || dead || dbm <= thr) continue;
      const q = clamp((dbm - thr) / 30, 0.12, 1);
      s += `<rect x="${x(ts[j])}" y="${y}" width="${Math.max(0.5, x(t1) - x(ts[j]) + 0.3)}" height="${h}" fill="${IFACES[iface].color}" fill-opacity="${q.toFixed(2)}"/>`;
    }
    // Network in use: outline segments.
    for (const [a, b] of activeSpans(run, iface)) {
      s += `<rect x="${x(a)}" y="${y - 2}" width="${Math.max(1, x(b) - x(a))}" height="${h + 4}" rx="3" fill="none" stroke="var(--fg)" stroke-width="1.5"/>`;
    }
  });
  for (const sw of run.switches) {
    const tip = `<b>${fmtT(sw.t)}</b> switched ${IFACES[sw.from].label} → ${IFACES[sw.to].label}<br>${sw.reason === "proactive" ? "early, before the link died" : "after the link stopped answering"}`;
    s += `<g class="hit" data-tip="${esc(tip)}"><line x1="${x(sw.t)}" x2="${x(sw.t)}" y1="0" y2="${H - 18}" stroke="var(--fg)" stroke-dasharray="3 3"/><rect x="${x(sw.t) - 6}" y="0" width="12" height="${H - 18}" fill="transparent"/></g>`;
  }
  s += `<g data-role="device"><circle r="6" cy="${shown.length * row / 2}" fill="var(--fg)" stroke="var(--card)" stroke-width="2"/></g>`;
  return `<svg viewBox="0 0 ${W} ${H}" height="${H}">${s}${cursor(H - 18)}</svg>`;
}

function zoneAt(run, iface, t) {
  for (const [a, b, p] of run.zones[iface]) if (t >= a && t < b) return p;
  return "dead";
}

function activeSpans(run, iface) {
  const spans = [];
  let cur = run.start_iface, since = 0;
  for (const s of run.switches) {
    if (cur === iface) spans.push([since, s.t]);
    cur = s.to; since = s.t;
  }
  if (cur === iface) spans.push([since, run.duration]);
  return spans;
}

function signalSvg(run, shown, x, W) {
  const H = 130, top = 6, bottom = H - 18;
  const y = (dbm) => top + ((DBM_MAX - clamp(dbm, DBM_MIN, DBM_MAX)) / (DBM_MAX - DBM_MIN)) * (bottom - top);
  let s = frame(run, x, W, H, top, bottom, true);
  for (const v of [-60, -80, -100, -120]) {
    s += `<line class="grid" x1="${PAD.l}" x2="${W - PAD.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${PAD.l - 8}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  }
  const thresholds = [...new Set(shown.map((i) => run.thresholds[i]))];
  for (const v of thresholds) s += `<line x1="${PAD.l}" x2="${W - PAD.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--ink-muted)" stroke-dasharray="4 4"/>`;
  for (const iface of shown) {
    const pts = run.signal.t.map((t, j) => [t, run.signal.dbm[iface][j]]).filter(([, v]) => v !== null);
    s += `<polyline fill="none" stroke="${IFACES[iface].color}" stroke-width="2" stroke-linejoin="round" points="${pts.map(([t, v]) => `${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ")}"/>`;
  }
  // Dropout warnings (the phone's own prediction) and warnings sent to the server.
  for (const w of run.warnings.filter((w) => w.on && shown.includes(w.iface))) {
    const tip = `<b>${fmtT(w.t)}</b> ${IFACES[w.iface].label}: dropout predicted from the fading signal`;
    s += `<g class="hit" data-tip="${esc(tip)}"><path d="M${x(w.t)},${top + 2} l-6,10 h12 z" fill="var(--warning)" stroke="var(--card)"/><rect x="${x(w.t) - 8}" y="${top}" width="16" height="14" fill="transparent"/></g>`;
  }
  for (const h of run.hints.filter((h) => h.on)) {
    const tip = `<b>${fmtT(h.t)}</b> server told an outage is coming: it prefetches more`;
    s += `<g class="hit" data-tip="${esc(tip)}"><line x1="${x(h.t)}" x2="${x(h.t)}" y1="${top}" y2="${bottom}" stroke="var(--warning)" stroke-width="1.5"/><rect x="${x(h.t) - 5}" y="${top}" width="10" height="${bottom - top}" fill="transparent"/></g>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" height="${H}">${s}${cursor(bottom)}</svg>`;
}

function latencySvg(run, x, W) {
  const H = 96, top = 6, bottom = H - 30;
  const ok = run.pings.filter((p) => p[1] !== null).map((p) => p[1]).sort((a, b) => a - b);
  if (!run.pings.length) {
    return `<svg viewBox="0 0 ${W} 28" height="28"><text x="${PAD.l}" y="18">Not recorded for this run (older log format).</text></svg>`;
  }
  const p95 = ok.length ? ok[Math.floor(ok.length * 0.95)] : 100;
  const max = Math.max(50, Math.ceil((p95 * 1.4) / 50) * 50);
  const y = (ms) => bottom - (clamp(ms, 0, max) / max) * (bottom - top);
  let s = frame(run, x, W, H, top, H - 16, true);
  for (const v of [0, max / 2, max]) s += `<line class="grid" x1="${PAD.l}" x2="${W - PAD.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${PAD.l - 8}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  // Line broken wherever a ping went unanswered.
  let seg = [], path = "";
  const flush = () => { if (seg.length > 1) path += `M${seg.join(" L")}`; seg = []; };
  for (const [t, ms] of run.pings) {
    if (ms === null) { flush(); continue; }
    seg.push(`${x(t).toFixed(1)},${y(ms).toFixed(1)}`);
  }
  flush();
  s += `<path d="${path}" fill="none" stroke="var(--fg)" stroke-width="1.5" stroke-linejoin="round"/>`;
  s += `<text x="${PAD.l - 8}" y="${bottom + 13}" text-anchor="end">no reply</text>`;
  for (const [t, ms] of run.pings) if (ms === null) s += `<rect x="${x(t) - 0.75}" y="${bottom + 5}" width="1.5" height="8" fill="var(--critical)"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" height="${H}">${s}${cursor(H - 16)}</svg>`;
}

function pagesSvg(run, x, W) {
  const H = 92, rowPages = 8, hPages = 26, rowPush = 46, hPush = 18;
  let s = frame(run, x, W, H, 0, H - 16, true);
  s += `<text x="${PAD.l - 8}" y="${rowPages + 17}" text-anchor="end" class="ink">clicks</text>`;
  s += `<text x="${PAD.l - 8}" y="${rowPush + 13}" text-anchor="end" class="ink">pushed</text>`;
  for (const p of run.pages) {
    const src = SOURCES[p.source] || SOURCES.origin;
    const x0 = x(p.t), w = Math.max(3, x(p.t + p.wait) - x0);
    const tip = `<b>${esc(p.title)}</b><br>clicked at ${fmtT(p.t)} · ${src.label}<br>waited ${p.wait < 1 ? `${Math.round(p.wait * 1000)} ms` : `${p.wait.toFixed(1)} s`}`;
    s += `<g class="hit" data-tip="${esc(tip)}"><rect x="${x0}" y="${rowPages}" width="${w}" height="${hPages}" rx="3" fill="${src.color}"/><rect x="${x0 - 3}" y="${rowPages - 2}" width="${w + 6}" height="${hPages + 4}" fill="transparent"/></g>`;
    if (w > 70) s += `<text x="${x0 + 5}" y="${rowPages + 17}" style="fill:#fff">${esc(p.title.slice(0, Math.floor(w / 7)))}</text>`;
  }
  for (const p of run.pushes) {
    const tip = `<b>${esc(p.title)}</b><br>pushed at ${fmtT(p.t)}${p.depth > 1 ? " (two clicks ahead)" : ""} · ${p.kb} KB${p.prob != null ? ` · Jev ${Math.round(p.prob * 100)}%` : ""}<br>${p.opened ? "the reader opened it" : "not opened"}`;
    const h = p.depth > 1 ? hPush * 0.55 : hPush;
    s += `<g class="hit" data-tip="${esc(tip)}"><rect x="${x(p.t) - 1}" y="${rowPush + hPush - h}" width="2" height="${h}" fill="${p.opened ? "var(--src-push)" : "var(--ink-muted)"}"/><rect x="${x(p.t) - 4}" y="${rowPush}" width="8" height="${hPush}" fill="transparent"/></g>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" height="${H}">${s}${cursor(H - 16)}</svg>`;
}

function legendSources(run) {
  const used = [...new Set(run.pages.map((p) => (SOURCES[p.source] || SOURCES.origin).label))];
  const items = Object.values(SOURCES).filter((s, i, all) => used.includes(s.label) && all.findIndex((o) => o.label === s.label) === i);
  let html = items.map((s) => `<span><i class="swatch block" style="background:${s.color}"></i>${s.label}</span>`).join("");
  if (run.pushes.length) {
    html += `<span><i class="swatch block" style="width:2px;background:var(--src-push)"></i>pushed, opened</span>`;
    html += `<span><i class="swatch block" style="width:2px;background:var(--ink-muted)"></i>pushed, not opened (short = two clicks ahead)</span>`;
  }
  return html + `<span><i class="swatch block" style="background:var(--outage)"></i>outage window</span>`;
}

function pageTable(run) {
  const rows = run.pages.map((p) => `<tr><td class="num">${fmtT(p.t)}</td><td>${esc(p.title)}</td>
    <td>${esc((SOURCES[p.source] || SOURCES.origin).label)}</td><td class="num">${p.wait.toFixed(2)} s</td></tr>`).join("");
  return `<table><thead><tr><th class="num">Clicked</th><th>Page</th><th>Served from</th><th class="num">Wait</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// ---------------------------------------------------------------------------- per-frame update

function updateLane(lane) {
  const { run, root, x } = lane;
  const t = Math.min(state.t, run.duration);
  root.querySelectorAll('[data-role="cursor"]').forEach((c) => { c.setAttribute("x1", x(t)); c.setAttribute("x2", x(t)); });
  const device = root.querySelector('[data-role="device"]');
  if (device) device.setAttribute("transform", `translate(${x(t)},0)`);

  const iface = activeAt(run, t);
  const pi = lastAtOrBefore(run.pings, t, (p) => p[0]);
  const lastPing = pi >= 0 ? run.pings[pi] : null;
  const ok = [...run.pings.slice(0, pi + 1)].reverse().find((p) => p[1] !== null);
  let status;
  if (lastPing && lastPing[1] === null && t - lastPing[0] < 1.5) status = ["Offline", "var(--critical)", "✕"];
  else if (!run.pings.length && run.outage && t >= run.outage[0] && t < run.outage[1]) status = ["Outage", "var(--critical)", "✕"];
  else if (warningOn(run, iface, t)) status = ["Dropout predicted", "var(--warning)", "!"];
  else status = ["Online", "var(--good)", "✓"];
  const latency = ok && t - ok[0] < 1.5 && status[0] !== "Offline" ? `${Math.round(ok[1])} <small>ms</small>` : "–";
  const dbm = signalAt(run, iface, t);

  let waited = 0, opened = 0, cached = 0, waitingNow = false;
  for (const p of run.pages) {
    if (p.t > t) continue;
    waited += Math.min(p.wait, t - p.t);
    if (p.t + p.wait <= t) { opened += 1; if (p.source !== "origin" && p.source !== "pending") cached += 1; }
    else waitingNow = true;
  }
  let pushedKb = 0, readKb = 0;
  for (const p of run.pushes) if (p.t <= t) { pushedKb += p.kb; if (p.opened) readKb += p.kb; }

  root.querySelector('[data-role="tiles"]').innerHTML = [
    tile("Status", `<span class="status"><i class="dot" style="background:${status[1]}"></i>${status[2]} ${status[0]}</span>`),
    tile("Network in use", `<span class="status"><i class="dot" style="background:${IFACES[iface].color}"></i>${IFACES[iface].label}</span>`),
    tile("Signal", dbm === null ? "–" : `${Math.round(dbm)} <small>dBm</small>`),
    tile("Latency", latency),
    tile("Pages opened", `${opened}${cached ? ` <small>${cached} offline-ready</small>` : ""}`),
    tile("Time waiting", `${waited.toFixed(1)} <small>s${waitingNow ? " · waiting now" : ""}</small>`),
    tile("Prefetched", `${(pushedKb / 1000).toFixed(2)} <small>MB · ${(readKb / 1000).toFixed(2)} opened</small>`),
  ].join("");
}

const tile = (k, v) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`;

load();
