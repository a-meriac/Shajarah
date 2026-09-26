// Bird's-eye demo of recorded test runs (experiments/export_replay.py -> site/replays/).
// One reader, two setups, the same trip: the map is drawn from the recorded signal and link
// conditions (position along the road = time), the phones from the recorded page events.
// Plain JS + SVG, no dependencies. The selection lives in the URL hash so a view can be shared.

const IFACES = {
  sat0: { label: "Satellite", color: "var(--net-sat0)" },
  cell0: { label: "Cellular", color: "var(--net-cell0)" },
  wifi0: { label: "Wi-Fi", color: "var(--net-wifi0)" },
};
const SETUPS = {
  "1": { name: "Ordinary connection", sub: "TCP: notices a dropout only when it happens, then reconnects (how most apps work)" },
  "2": { name: "Modern connection", sub: "QUIC: survives network changes, but doesn't see dropouts coming" },
  "3": { name: "Always-on prefetch", sub: "QUIC plus a few likely pages pushed all the time" },
  "4": { name: "Hover prefetch", sub: "QUIC plus the clicked page fetched 200 ms before the click" },
  "5": { name: "Shajarah", sub: "Sees the dropout coming: switches network early, prefetches what you'll read next" },
  "5a": { name: "Shajarah, switching only", sub: "Switches network early, no prefetching" },
  "5b": { name: "Shajarah, prefetch only", sub: "Prefetches on a dropout warning, no early switching" },
};
const SCENARIOS = {
  car_tunnel_45s: {
    title: "Drive through a tunnel, with and without Shajarah",
    lede: "A recorded test drive from our emulated network. The same reader, browsing Wikipedia, rides through the same stretch of road twice: once with an ordinary connection and once with Shajarah. Cellular fades before a 45-second tunnel with no signal at all. Press play and watch what each phone shows.",
    mover: "car", gap: "Tunnel: no signal",
  },
  wifi_to_5g_walk: {
    title: "Walk out of Wi-Fi range, with and without Shajarah",
    lede: "A recorded walk out of a building: Wi-Fi fades and drops, cellular is available throughout. Page loads barely differ here, because the switch is quick either way; the live-call test below is where switching early shows.",
    mover: "walker", gap: "No coverage",
  },
};
const DEFAULT_LEFT = "1", DEFAULT_RIGHT = "5";
const SPEEDS = [2, 5, 10, 20];
const W = 1000, X0 = 96, X1 = 984;
const LANE_Y = { sat0: 16, cell0: 48, wifi0: 80 }, LANE_H = 24;
const ROAD_Y = 136, ROAD_H = 30;
const INSTANT = new Set(["push", "stale", "revalidated"]);
const RECENT_S = 3; // how long an "opened instantly" badge stays up
const LONG_WAIT_S = 1; // a wait worth marking (the same bar as the "Waits over 1 s" tile)

const app = document.getElementById("app");
const tooltip = document.getElementById("tooltip");
const files = new Map();
const S = { index: [], voip: null, scenario: null, session: null, left: null, right: null, runs: {}, t: 0, speed: 5, playing: false, last: 0 };
let refs = {};

// ----------------------------------------------------------------------------- helpers

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const fmtT = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
const fmtS = (s) => (s < 10 ? s.toFixed(1) : Math.round(s)) + " s";
const fmtMB = (kb) => (kb >= 1000 ? (kb / 1000).toFixed(1) + " MB" : Math.round(kb) + " KB");
const svg = (tag, attrs = {}, text) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text !== undefined) el.textContent = text;
  return el;
};
const setupName = (c) => (SETUPS[c] || { name: `Setup ${c}` }).name;

function lastAtOrBefore(arr, t, key = (x) => x) {
  let lo = 0, hi = arr.length - 1, best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (key(arr[mid]) <= t) { best = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return best;
}

const duration = () => Math.max(...Object.values(S.runs).map((r) => r.duration));
const xOf = (t) => X0 + ((X1 - X0) * clamp(t, 0, duration())) / duration();
const tOf = (x) => ((clamp(x, X0, X1) - X0) / (X1 - X0)) * duration();

function profileAt(run, iface, t) {
  for (const [a, b, p] of run.zones[iface] || []) if (t >= a && t < b) return p;
  return "dead";
}
function signalAt(run, iface, t) {
  const i = lastAtOrBefore(run.signal.t, t);
  return i < 0 ? null : run.signal.dbm[iface][i];
}
function activeAt(run, t) {
  let iface = run.start_iface;
  for (const s of run.switches) if (s.t <= t) iface = s.to;
  return iface;
}
function hintOn(run, t) {
  let on = false;
  for (const h of run.hints) if (h.t <= t) on = h.on;
  return on;
}
function barsFor(run, iface, dbm) {
  const thr = run.thresholds[iface];
  if (dbm == null || dbm <= thr) return 0;
  return dbm >= thr + 30 ? 4 : dbm >= thr + 20 ? 3 : dbm >= thr + 10 ? 2 : 1;
}
// Stretches where no interface has a link: the tunnel (or any total outage).
function gaps(run) {
  const edges = new Set([0, run.duration]);
  for (const segs of Object.values(run.zones)) for (const [a, b] of segs) { edges.add(a); edges.add(b); }
  const ts = [...edges].filter((t) => t <= run.duration).sort((a, b) => a - b);
  const out = [];
  for (let i = 0; i + 1 < ts.length; i++) {
    const mid = (ts[i] + ts[i + 1]) / 2;
    const dead = Object.keys(run.zones).every((f) => profileAt(run, f, mid) === "dead");
    if (dead) {
      if (out.length && Math.abs(out[out.length - 1][1] - ts[i]) < 1e-6) out[out.length - 1][1] = ts[i + 1];
      else out.push([ts[i], ts[i + 1]]);
    }
  }
  return out;
}

// ----------------------------------------------------------------------------- the story at time t

function stateAt(run, t) {
  const pages = run.pages;
  let waited = 0, opened = 0, stuck = 0;
  for (const p of pages) {
    if (p.t > t) break;
    const w = Math.min(p.wait, t - p.t);
    waited += w;
    if (t >= p.t + p.wait) opened++;
    if (w > LONG_WAIT_S) stuck++;
  }
  const idx = lastAtOrBefore(pages, t, (p) => p.t);
  const cur = idx >= 0 ? pages[idx] : null;
  let screen;
  if (!cur) screen = { kind: "start" };
  else if (t < cur.t + cur.wait) {
    // Clicked while there was no signal, and still loading although the signal is back.
    const recovering = gaps(run).some(([a, b]) => cur.t >= a && cur.t < b && t >= b);
    screen = { kind: "wait", elapsed: t - cur.t, recovering };
  }
  else if (t - (cur.t + cur.wait) < RECENT_S && (INSTANT.has(cur.source) || cur.wait > 1)) screen = { kind: "opened", page: cur };
  else screen = { kind: "read" };
  const pushed = run.pushes.filter((p) => p.t <= t);
  const openedPushes = new Set(pages.filter((p) => p.source === "push" && p.t <= t).map((p) => p.title));
  const readKb = pushed.filter((p) => openedPushes.has(p.title)).reduce((s, p) => s + p.kb, 0);
  const iface = activeAt(run, t);
  const linkUp = profileAt(run, iface, t) !== "dead";
  return {
    cur, screen, waited, opened, stuck,
    pushedN: pushed.length, pushedKb: pushed.reduce((s, p) => s + p.kb, 0), readKb,
    iface, linkUp, dbm: signalAt(run, iface, t), warning: hintOn(run, t),
  };
}

// Human-readable events for the log under each phone.
function events(run) {
  const ev = [];
  for (const [a, b] of gaps(run)) {
    ev.push({ t: a, text: "Lost all signal" });
    if (b < run.duration - 0.5) ev.push({ t: b, text: "Signal back" });
  }
  for (const h of run.hints) ev.push({ t: h.t, text: h.on ? "Dropout predicted: telling the server" : "All clear again" });
  for (const s of run.switches) {
    const how = s.reason === "proactive" ? "before the link died" : "after the link died";
    ev.push({ t: s.t, text: `Switched to ${IFACES[s.to].label} ${how}` });
  }
  for (const p of run.pages) {
    ev.push({ t: p.t, text: `Clicked "${p.title}"` });
    const end = p.t + p.wait;
    if (end > run.duration) continue;
    const how = p.source === "push" ? "instantly (prefetched)"
      : p.source === "stale" ? `from the saved copy after ${fmtS(p.wait)}`
      : p.wait > 1 ? `after waiting ${fmtS(p.wait)}` : "";
    if (how) ev.push({ t: end, text: `Opened ${how}` });
  }
  // Pushes, grouped into bursts.
  let burst = null;
  for (const p of run.pushes) {
    if (burst && p.t - burst.last < 1.5) { burst.n++; burst.kb += p.kb; burst.last = p.t; continue; }
    if (burst) ev.push(burst.ev());
    burst = { t: p.t, last: p.t, n: 1, kb: p.kb, ev() { return { t: this.t, text: `Server prefetched ${this.n} page${this.n > 1 ? "s" : ""} (${fmtMB(this.kb)})` }; } };
  }
  if (burst) ev.push(burst.ev());
  return ev.sort((a, b) => a.t - b.t);
}

// ----------------------------------------------------------------------------- loading

async function fetchJSON(path) {
  const r = await fetch(path, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

async function runFile(entry) {
  if (!files.has(entry.file)) files.set(entry.file, fetchJSON(`replays/${entry.file}`));
  return files.get(entry.file);
}

const entriesFor = (scenario) => S.index.filter((r) => r.scenario === scenario);
const sessionsFor = (scenario) => [...new Set(entriesFor(scenario).map((r) => r.session))].sort((a, b) => a - b);
const configsFor = (scenario, session) => entriesFor(scenario).filter((r) => r.session === session).map((r) => r.config);
const entry = (scenario, session, config) => entriesFor(scenario).find((r) => r.session === session && r.config === config);

// The reader where the right-hand setup saved the most waiting: a clear example. The summary
// below the phones gives the averages over every reader.
function showcaseReader(scenario, left, right) {
  let best = null, bestGain = -Infinity;
  for (const s of sessionsFor(scenario)) {
    const a = entry(scenario, s, left), b = entry(scenario, s, right);
    if (!a || !b) continue;
    const gain = a.metrics.wait_s - b.metrics.wait_s;
    if (gain > bestGain) { bestGain = gain; best = s; }
  }
  return best ?? sessionsFor(scenario)[0];
}

async function load() {
  try {
    S.index = (await fetchJSON("replays/index.json")).runs || [];
  } catch {
    S.index = [];
  }
  try { S.voip = await fetchJSON("replays/voip.json"); } catch { S.voip = null; }
  if (!S.index.length) {
    app.innerHTML = `<div class="empty"><p><b>No recorded runs yet.</b></p><p>Export them with <code>python -m experiments.export_replay &lt;batch&gt;</code> and commit <code>site/replays/</code>.</p></div>`;
    return;
  }
  const h = new URLSearchParams(location.hash.slice(1));
  const scenarios = [...new Set(S.index.map((r) => r.scenario))];
  S.scenario = scenarios.includes(h.get("scenario")) ? h.get("scenario") : scenarios.includes("car_tunnel_45s") ? "car_tunnel_45s" : scenarios[0];
  pickConfigs(h.get("left"), h.get("right"));
  const sessions = sessionsFor(S.scenario);
  const want = parseInt(h.get("reader"), 10);
  S.session = sessions.includes(want) ? want : showcaseReader(S.scenario, S.left, S.right);
  S.t = parseFloat(h.get("t")) || 0;
  build();
  await select();
}

function pickConfigs(left, right) {
  const all = [...new Set(entriesFor(S.scenario).map((r) => r.config))];
  S.left = all.includes(left) ? left : all.includes(DEFAULT_LEFT) ? DEFAULT_LEFT : all[0];
  S.right = all.includes(right) ? right : all.includes(DEFAULT_RIGHT) ? DEFAULT_RIGHT : all[all.length - 1];
}

function writeHash() {
  const h = new URLSearchParams({ scenario: S.scenario, reader: S.session, left: S.left, right: S.right, t: S.t.toFixed(1) });
  history.replaceState(null, "", `#${h}`);
}

// ----------------------------------------------------------------------------- page structure

function build() {
  const scenarios = [...new Set(S.index.map((r) => r.scenario))];
  const opt = (v, label, sel) => `<option value="${esc(v)}"${v === sel ? " selected" : ""}>${esc(label)}</option>`;
  app.innerHTML = `
    <div class="card controls">
      <label>Trip<select id="scenario">${scenarios.map((s) => opt(s, SCENARIOS[s]?.title.split(",")[0] || s, S.scenario)).join("")}</select></label>
      <label>Reader<select id="reader"></select></label>
      <label>Left phone<select id="left"></select></label>
      <label>Right phone<select id="right"></select></label>
      <button class="primary" id="play" type="button">Play</button>
      <span class="speeds" role="group" aria-label="Playback speed">${SPEEDS.map((s) => `<button type="button" data-speed="${s}" aria-pressed="${s === S.speed}">${s}×</button>`).join("")}</span>
      <div class="timebar"><input id="scrub" type="range" min="0" max="120" step="0.1" value="0" aria-label="Time"><span class="clock" id="clock">0:00</span></div>
    </div>
    <div class="card map">
      <h2>Bird's-eye view</h2>
      <div class="scroll"><svg id="map" viewBox="0 0 ${W} 224" role="img" aria-label="Map of the trip: network coverage along the road and the traveller's position"></svg></div>
      <div class="legend">
        ${Object.entries(IFACES).map(([k, v]) => `<span><i class="swatch" style="background:${v.color}"></i>${v.label} coverage (stronger = darker)</span>`).join("")}
        <span><i class="swatch" style="background:var(--tunnel)"></i>No signal on any network</span>
        <span><i class="swatch" style="background:var(--good);border-radius:50%"></i>Page prefetched to the right phone</span>
      </div>
      <p class="map-note small muted">Position along the road stands for time: the tests ran in an emulated network with no real map. Coverage strength, the tunnel, warnings, switches and every page event are exactly as recorded.</p>
    </div>
    <div class="phones">${["left", "right"].map(phoneHTML).join("")}</div>
    <div class="card timeline section">
      <h2>Waiting for pages</h2>
      <p class="small muted" id="tl-sub">Red bars: the reader waiting more than a second for a page. Green marks: a page opened from the phone's cache. Click to jump.</p>
      <div class="scroll"><svg id="timeline" viewBox="0 0 ${W} 118" role="img" aria-label="Timeline of page waits for both phones"></svg></div>
    </div>
    <div class="card summary section">
      <h2 id="sum-title">All readers</h2>
      <p class="small muted" id="sum-sub"></p>
      <div class="scroll"><svg id="summary" role="img" aria-label="Average waiting per setup across all readers"></svg></div>
      <p class="callout" id="sum-callout"></p>
      <p class="callout" id="voip-callout" hidden></p>
      <details><summary>Table</summary><div id="sum-table"></div></details>
    </div>`;
  refs = {
    scenario: document.getElementById("scenario"), reader: document.getElementById("reader"),
    leftSel: document.getElementById("left"), rightSel: document.getElementById("right"),
    play: document.getElementById("play"), scrub: document.getElementById("scrub"), clock: document.getElementById("clock"),
    map: document.getElementById("map"), timeline: document.getElementById("timeline"),
  };
  for (const side of ["left", "right"]) {
    const q = (k) => document.getElementById(`${side}-${k}`);
    refs[side] = { name: q("name"), sub: q("sub"), net: q("net"), flag: q("flag"), title: q("title"), state: q("state"), offline: q("offline"), wait: q("wait"), opened: q("opened"), stuck: q("stuck"), data: q("data"), log: q("log") };
  }
  refs.scenario.onchange = async () => {
    S.scenario = refs.scenario.value; pickConfigs(S.left, S.right);
    S.session = showcaseReader(S.scenario, S.left, S.right); S.t = 0; await select();
  };
  refs.reader.onchange = async () => { S.session = parseInt(refs.reader.value, 10); await select(); };
  refs.leftSel.onchange = async () => { S.left = refs.leftSel.value; await select(); };
  refs.rightSel.onchange = async () => { S.right = refs.rightSel.value; await select(); };
  refs.play.onclick = () => togglePlay();
  refs.scrub.oninput = () => { S.t = parseFloat(refs.scrub.value); render(); };
  refs.scrub.onchange = writeHash;
  for (const b of document.querySelectorAll("[data-speed]")) {
    b.onclick = () => {
      S.speed = parseInt(b.dataset.speed, 10);
      for (const o of document.querySelectorAll("[data-speed]")) o.setAttribute("aria-pressed", String(o === b));
    };
  }
  for (const el of [refs.map, refs.timeline]) {
    el.addEventListener("pointerdown", (e) => seekFromPointer(el, e));
    el.addEventListener("pointermove", (e) => { if (e.buttons) seekFromPointer(el, e); });
  }
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("select, input, button")) return;
    if (e.key === " ") { e.preventDefault(); togglePlay(); }
  });
}

function phoneHTML(side) {
  return `
    <section class="card" aria-labelledby="${side}-name">
      <div class="phone-head"><span class="who" id="${side}-name"></span></div>
      <div class="small muted" id="${side}-sub"></div>
      <div class="screen" aria-live="off">
        <div class="statusbar"><span class="net" id="${side}-net"></span><span class="flag" id="${side}-flag"></span></div>
        <div class="page-kind">Wikipedia article</div>
        <div class="page-title" id="${side}-title"></div>
        <div class="state" id="${side}-state"></div>
        <div class="offline" id="${side}-offline"></div>
      </div>
      <div class="tiles">
        <div class="tile"><div class="k">Time spent waiting</div><div class="v" id="${side}-wait"></div></div>
        <div class="tile"><div class="k">Pages opened</div><div class="v" id="${side}-opened"></div></div>
        <div class="tile"><div class="k">Waits over 1 s</div><div class="v" id="${side}-stuck"></div></div>
        <div class="tile"><div class="k">Data prefetched</div><div class="v" id="${side}-data"></div></div>
      </div>
      <ul class="log" id="${side}-log"></ul>
    </section>`;
}

async function select() {
  stop();
  const sessions = sessionsFor(S.scenario);
  if (!sessions.includes(S.session)) S.session = sessions[0];
  const configs = configsFor(S.scenario, S.session);
  if (!configs.includes(S.left)) S.left = configs[0];
  if (!configs.includes(S.right)) S.right = configs[configs.length - 1];
  refs.reader.innerHTML = sessions.map((s, i) => `<option value="${s}"${s === S.session ? " selected" : ""}>Reader ${i + 1}</option>`).join("");
  const cfgOpts = (sel) => configs.map((c) => `<option value="${esc(c)}"${c === sel ? " selected" : ""}>${esc(setupName(c))}</option>`).join("");
  refs.leftSel.innerHTML = cfgOpts(S.left);
  refs.rightSel.innerHTML = cfgOpts(S.right);
  const sc = SCENARIOS[S.scenario] || { title: S.scenario, lede: "", mover: "car", gap: "No signal" };
  document.getElementById("headline").textContent = sc.title;
  document.getElementById("lede").textContent = sc.lede;
  const [a, b] = await Promise.all([runFile(entry(S.scenario, S.session, S.left)), runFile(entry(S.scenario, S.session, S.right))]);
  S.runs = { left: a, right: b };
  for (const side of ["left", "right"]) {
    const run = S.runs[side];
    run._events = run._events || events(run);
    refs[side].name.textContent = setupName(run.config);
    refs[side].sub.textContent = (SETUPS[run.config] || {}).sub || run.about;
  }
  S.t = clamp(S.t, 0, duration());
  refs.scrub.max = duration();
  drawMap(sc);
  drawTimeline();
  drawSummary();
  render();
  writeHash();
}

// ----------------------------------------------------------------------------- bird's-eye map

function drawMap(sc) {
  const m = refs.map;
  m.innerHTML = "";
  const run = S.runs.right; // the same trip for both phones; the right run has the full signal log
  const defs = svg("defs");
  const pat = svg("pattern", { id: "nocover", width: 8, height: 8, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
  pat.append(svg("line", { x1: 0, y1: 0, x2: 0, y2: 8, stroke: "var(--grid)", "stroke-width": 2 }));
  defs.append(pat);
  m.append(defs);

  // Coverage lanes, one per network, shaded by recorded signal while the link was up.
  for (const [iface, spec] of Object.entries(IFACES)) {
    const y = LANE_Y[iface];
    m.append(svg("rect", { x: X0, y, width: X1 - X0, height: LANE_H, fill: "url(#nocover)", rx: 4 }));
    const ts = run.signal.t, thr = run.thresholds[iface];
    for (let i = 0; i < ts.length; i++) {
      const a = ts[i], b = i + 1 < ts.length ? ts[i + 1] : run.duration;
      const dbm = run.signal.dbm[iface][i];
      if (profileAt(run, iface, (a + b) / 2) === "dead" || dbm == null || dbm <= thr) continue;
      const strength = clamp((dbm - thr) / 45, 0.12, 1);
      m.append(svg("rect", { x: xOf(a), y, width: Math.max(0.6, xOf(b) - xOf(a) + 0.4), height: LANE_H, fill: spec.color, "fill-opacity": (0.18 + 0.72 * strength).toFixed(2) }));
    }
    m.append(svg("text", { x: X0 - 8, y: y + 16, "text-anchor": "end", "font-size": 12, fill: "var(--muted)" }, spec.label));
  }

  // Ground, road, and stretches with no signal on any network.
  m.append(svg("rect", { x: X0, y: ROAD_Y - 8, width: X1 - X0, height: ROAD_H + 16, fill: "var(--ground)", rx: 6 }));
  m.append(svg("rect", { x: X0, y: ROAD_Y, width: X1 - X0, height: ROAD_H, fill: "var(--road)", rx: 4 }));
  m.append(svg("line", { x1: X0 + 6, x2: X1 - 6, y1: ROAD_Y + ROAD_H / 2, y2: ROAD_Y + ROAD_H / 2, stroke: "var(--road-line)", "stroke-width": 2, "stroke-dasharray": "14 10" }));
  m.append(svg("text", { x: X0 - 8, y: ROAD_Y + 20, "text-anchor": "end", "font-size": 12, fill: "var(--muted)" }, sc.mover === "car" ? "Road" : "Path"));
  for (const [a, b] of gaps(run)) {
    const g = svg("g");
    g.append(svg("rect", { x: xOf(a), y: ROAD_Y - 12, width: xOf(b) - xOf(a), height: ROAD_H + 24, fill: "var(--tunnel)", rx: 8 }));
    g.append(svg("text", { x: (xOf(a) + xOf(b)) / 2, y: ROAD_Y - 16, "text-anchor": "middle", "font-size": 12, "font-weight": 600, fill: "var(--fg)" }, sc.gap));
    g.append(svg("title", {}, `${sc.gap}: ${fmtT(a)} to ${fmtT(b)}`));
    m.append(g);
  }

  // Markers under the road: the moments each phone acted.
  const rows = { left: 194, right: 214 };
  for (const side of ["left", "right"]) {
    const r = S.runs[side], y = rows[side];
    m.append(svg("text", { x: X0 - 8, y: y + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, side === "left" ? "Left" : "Right"));
    for (const h of r.hints.filter((h) => h.on)) marker(m, h.t, y, "▲", "var(--warning)", `${setupName(r.config)}: dropout predicted at ${fmtT(h.t)}; server told to prefetch`, "Dropout predicted");
    for (const s of r.switches) {
      const early = s.reason === "proactive";
      marker(m, s.t, y, "◆", IFACES[s.to].color, `${setupName(r.config)}: switched to ${IFACES[s.to].label} at ${fmtT(s.t)} ${early ? "(before the link died)" : "(after the link died)"}`, `Switched ${early ? "early" : "after failure"}`);
    }
  }

  // Dynamic layer: prefetch dots, cursor, traveller.
  refs.pushes = svg("g");
  refs.cursor = svg("line", { y1: LANE_Y.sat0 - 4, y2: ROAD_Y + ROAD_H + 10, stroke: "var(--fg)", "stroke-width": 1, "stroke-dasharray": "3 3", opacity: 0.5 });
  refs.mover = moverShape(sc.mover);
  m.append(refs.pushes, refs.cursor, refs.mover);
}

function marker(m, t, y, glyph, color, title, label) {
  const g = svg("g", { class: "marker" });
  g.append(svg("text", { x: xOf(t), y: y + 5, "text-anchor": "middle", "font-size": 13, fill: color }, glyph));
  g.append(svg("text", { x: xOf(t) + 9, y: y + 4, "font-size": 11, fill: "var(--fg)" }, label));
  g.append(svg("title", {}, title));
  m.append(g);
}

function moverShape(kind) {
  const g = svg("g");
  if (kind === "car") {
    g.append(svg("rect", { x: -17, y: -8, width: 34, height: 16, rx: 5, fill: "var(--fg)" }));
    g.append(svg("rect", { x: -7, y: -5.5, width: 14, height: 11, rx: 2.5, fill: "var(--bg)", opacity: 0.85 }));
    g.append(svg("rect", { x: 13, y: -6, width: 3, height: 4, rx: 1, fill: "var(--warning)" }));
    g.append(svg("rect", { x: 13, y: 2, width: 3, height: 4, rx: 1, fill: "var(--warning)" }));
  } else {
    g.append(svg("circle", { r: 9, fill: "var(--fg)" }));
    g.append(svg("circle", { r: 4, fill: "var(--bg)" }));
  }
  g.append(svg("title", {}, "The traveller"));
  return g;
}

function renderMap() {
  const x = xOf(S.t), y = ROAD_Y + ROAD_H / 2;
  refs.mover.setAttribute("transform", `translate(${x},${y})`);
  refs.cursor.setAttribute("x1", x);
  refs.cursor.setAttribute("x2", x);
  // On narrow screens the map scrolls sideways: keep the traveller in view.
  const box = refs.map.parentElement;
  if (box.scrollWidth > box.clientWidth + 1) {
    const px = (x / W) * box.scrollWidth;
    if (px < box.scrollLeft + 40 || px > box.scrollLeft + box.clientWidth - 40) box.scrollLeft = px - box.clientWidth / 2;
  }
  // Pages being prefetched to the right phone fall from the cellular lane onto the traveller.
  refs.pushes.innerHTML = "";
  const flight = 0.9;
  const inFlight = S.runs.right.pushes.filter((p) => p.t <= S.t && S.t - p.t < flight).slice(-14);
  inFlight.forEach((p, i) => {
    const k = (S.t - p.t) / flight;
    const px = x + ((i % 7) - 3) * 5 * (1 - k);
    const py = LANE_Y.cell0 + LANE_H / 2 + (y - LANE_Y.cell0 - LANE_H / 2) * k;
    refs.pushes.append(svg("circle", { cx: px, cy: py, r: 3.2, fill: "var(--good)", stroke: "var(--card)", "stroke-width": 1.2 }));
  });
}

// ----------------------------------------------------------------------------- phones

function renderPhone(side) {
  const run = S.runs[side], r = refs[side], st = stateAt(run, S.t);
  const bars = st.linkUp ? barsFor(run, st.iface, st.dbm) : 0;
  const barsHTML = `<span class="bars" aria-hidden="true">${[4, 7, 10, 12].map((h, i) => `<i style="height:${h}px" class="${i < bars ? "on" : ""}"></i>`).join("")}</span>`;
  const color = IFACES[st.iface].color;
  r.net.innerHTML = st.linkUp
    ? `<span style="color:${color}">${barsHTML}</span>${IFACES[st.iface].label}`
    : `<span style="color:var(--critical)">${barsHTML}</span>No signal`;
  r.flag.innerHTML = st.warning ? `<span class="ico" aria-hidden="true">▲</span>Dropout predicted` : "";

  r.title.textContent = st.cur ? st.cur.title : "";
  const s = st.screen;
  r.state.innerHTML = s.kind === "start" ? `<span class="muted">Opening the first page…</span>`
    : s.kind === "wait" ? `<span class="badge wait"><span class="spinner" aria-hidden="true"></span>Loading… ${fmtS(s.elapsed)}</span>${s.recovering ? `<span class="small muted">Signal is back, but the connection is still recovering</span>` : ""}`
    : s.kind === "opened" ? openedBadge(s.page)
    : `<span class="badge read"><span class="ico" aria-hidden="true">●</span>Reading</span>`;
  r.offline.textContent = st.pushedN ? `${st.pushedN} page${st.pushedN > 1 ? "s" : ""} saved on the phone for offline reading` : "";

  r.wait.innerHTML = fmtS(st.waited);
  r.opened.innerHTML = `${st.opened}`;
  r.stuck.innerHTML = `${st.stuck}`;
  r.data.innerHTML = st.pushedKb ? `${fmtMB(st.pushedKb)} <small>${Math.round((100 * st.readKb) / st.pushedKb)}% read</small>` : `0 <small>none</small>`;
  const recent = run._events.filter((e) => e.t <= S.t).slice(-5).reverse();
  r.log.innerHTML = recent.map((e) => `<li><span class="lt">${fmtT(e.t)}</span><span>${esc(e.text)}</span></li>`).join("");
}

function openedBadge(p) {
  if (p.source === "push") return `<span class="badge instant"><span class="ico" aria-hidden="true">✓</span>Opened instantly: prefetched before the dropout</span>`;
  if (p.source === "stale") return `<span class="badge instant"><span class="ico" aria-hidden="true">✓</span>Showed the copy saved earlier (offline)</span>`;
  if (p.source === "revalidated") return `<span class="badge instant"><span class="ico" aria-hidden="true">✓</span>Opened from the phone's cache</span>`;
  return `<span class="badge wait"><span class="ico" aria-hidden="true">!</span>Loaded after waiting ${fmtS(p.wait)}</span>`;
}

// ----------------------------------------------------------------------------- timeline

function drawTimeline() {
  const el = refs.timeline;
  el.innerHTML = "";
  const run = S.runs.right, dur = duration();
  for (const [a, b] of gaps(run)) el.append(svg("rect", { x: xOf(a), y: 6, width: xOf(b) - xOf(a), height: 88, fill: "var(--tunnel)", opacity: 0.12 }));
  for (let t = 0; t <= dur + 0.01; t += 20) {
    el.append(svg("line", { x1: xOf(t), x2: xOf(t), y1: 6, y2: 94, stroke: "var(--grid)" }));
    el.append(svg("text", { x: xOf(t), y: 110, "text-anchor": "middle", "font-size": 11, fill: "var(--muted)" }, fmtT(t)));
  }
  const rows = { left: 24, right: 64 };
  for (const side of ["left", "right"]) {
    const r = S.runs[side], y = rows[side];
    el.append(svg("text", { x: X0 - 8, y: y + 13, "text-anchor": "end", "font-size": 12, fill: "var(--fg)" }, side === "left" ? "Left" : "Right"));
    el.append(svg("line", { x1: X0, x2: X1, y1: y + 9, y2: y + 9, stroke: "var(--grid)" }));
    for (const p of r.pages) {
      const end = Math.min(p.t + p.wait, dur);
      if (p.wait > LONG_WAIT_S) {
        const bar = svg("rect", { x: xOf(p.t), y, width: Math.max(2, xOf(end) - xOf(p.t)), height: 18, rx: 4, fill: "var(--critical)" });
        bar.append(svg("title", {}, `${setupName(r.config)}: waited ${fmtS(p.wait)} for "${p.title}" (clicked at ${fmtT(p.t)})`));
        el.append(bar);
      }
      if (INSTANT.has(p.source)) {
        const tick = svg("rect", { x: xOf(end) - 1.5, y: y - 3, width: 3, height: 24, rx: 1.5, fill: "var(--good)" });
        tick.append(svg("title", {}, `${setupName(r.config)}: "${p.title}" opened from the phone's cache at ${fmtT(end)}`));
        el.append(tick);
      }
    }
  }
  refs.tlCursor = svg("line", { y1: 4, y2: 96, stroke: "var(--fg)", "stroke-width": 1.5 });
  el.append(refs.tlCursor);
}

// ----------------------------------------------------------------------------- summary across readers

function drawSummary() {
  const rows = entriesFor(S.scenario);
  const configs = Object.keys(SETUPS).filter((c) => rows.some((r) => r.config === c));
  const stats = configs.map((c) => {
    const rs = rows.filter((r) => r.config === c);
    const waits = rs.map((r) => r.metrics.wait_s).sort((a, b) => a - b);
    const mean = waits.reduce((s, w) => s + w, 0) / waits.length;
    const median = waits.length % 2 ? waits[(waits.length - 1) / 2] : (waits[waits.length / 2 - 1] + waits[waits.length / 2]) / 2;
    const pushedMB = rs.reduce((s, r) => s + r.metrics.pushed_kb, 0) / rs.length / 1000;
    const cached = rs.reduce((s, r) => s + r.metrics.outage_cached, 0), outage = rs.reduce((s, r) => s + r.metrics.outage_pages, 0);
    const mine = rs.find((r) => r.session === S.session);
    return { c, n: rs.length, mean, median, pushedMB, cached, outage, mine: mine ? mine.metrics.wait_s : null };
  });
  const n = Math.max(...stats.map((s) => s.n));
  document.getElementById("sum-title").textContent = `All ${n} readers on this trip`;
  document.getElementById("sum-sub").textContent = "Average total time spent waiting for pages per reader (bar); the dot is the reader shown above. The page opens on the reader with the biggest difference: pick others to see trips where the prediction missed and both phones wait.";

  const el = document.getElementById("summary");
  const rowH = 30, H = stats.length * rowH + 30, L = 230, R = W - 140;
  el.setAttribute("viewBox", `0 0 ${W} ${H}`);
  el.innerHTML = "";
  const max = Math.max(...stats.map((s) => Math.max(s.mean, s.mine ?? 0))) * 1.05 || 1;
  const x = (v) => L + ((R - L) * v) / max;
  const step = [0.5, 1, 2, 5, 10, 20, 50].find((st) => max / st <= 7) || 100;
  for (let v = 0; v <= max; v += step) {
    el.append(svg("line", { x1: x(v), x2: x(v), y1: 4, y2: H - 22, stroke: "var(--grid)" }));
    el.append(svg("text", { x: x(v), y: H - 6, "text-anchor": "middle", "font-size": 11, fill: "var(--muted)" }, `${+v.toFixed(1)} s`));
  }
  stats.forEach((s, i) => {
    const y = 8 + i * rowH, chosen = s.c === S.left || s.c === S.right;
    el.append(svg("text", { x: L - 10, y: y + 15, "text-anchor": "end", "font-size": 12.5, fill: "var(--fg)", "font-weight": chosen ? 650 : 400 }, setupName(s.c)));
    const bar = svg("rect", { x: L, y: y + 3, width: Math.max(2, x(s.mean) - L), height: 16, rx: 4, fill: "var(--accent)", "fill-opacity": chosen ? 1 : 0.35 });
    bar.append(svg("title", {}, `${setupName(s.c)}: mean ${fmtS(s.mean)}, median ${fmtS(s.median)} over ${s.n} readers`));
    el.append(bar);
    el.append(svg("text", { x: x(s.mean) + 6, y: y + 15, "font-size": 12, fill: "var(--fg)" }, `${fmtS(s.mean)} avg`));
    if (s.mine != null) {
      const dot = svg("circle", { cx: x(s.mine), cy: y + 11, r: 4.5, fill: "var(--card)", stroke: "var(--fg)", "stroke-width": 1.8 });
      dot.append(svg("title", {}, `This reader: ${fmtS(s.mine)}`));
      el.append(dot);
    }
  });

  const L1 = stats.find((s) => s.c === S.left), R1 = stats.find((s) => s.c === S.right);
  const callout = document.getElementById("sum-callout");
  if (L1 && R1 && L1.mean > 0) {
    const cut = Math.round(100 * (1 - R1.mean / L1.mean));
    callout.textContent = `Across all ${n} readers, ${setupName(R1.c)} readers waited ${fmtS(R1.mean)} on average against ${fmtS(L1.mean)} with the ${setupName(L1.c).toLowerCase()} (${cut >= 0 ? `${cut}% less` : `${-cut}% more`}; median ${fmtS(R1.median)} vs ${fmtS(L1.median)}). ${R1.outage ? `${R1.cached} of ${R1.outage} clicks during the outage opened from the phone's cache, against ${L1.cached} of ${L1.outage}. ` : ""}${R1.pushedMB > 0.05 ? `The cost: ${R1.pushedMB.toFixed(1)} MB prefetched per trip on average, most of it never read.` : ""}`;
  } else callout.textContent = "";

  const voip = document.getElementById("voip-callout");
  const calls = S.voip ? S.voip.runs.filter((r) => r.scenario === S.scenario) : [];
  if (calls.length) {
    const med = (xs) => { const s = [...xs].sort((a, b) => a - b); return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2; };
    const by = (mode) => calls.filter((r) => r.mode === mode);
    const react = by("reactive"), early = by("switch_early");
    voip.hidden = false;
    voip.textContent = `Live call on the same walk (a separate test, ${react.length} runs each): switching after the link died left a ${med(react.map((r) => r.longest_silence_s)).toFixed(2)} s silence and ${med(react.map((r) => r.lost_pct)).toFixed(1)}% of the audio lost; switching early left ${Math.round(1000 * med(early.map((r) => r.longest_silence_s)))} ms and ${med(early.map((r) => r.lost_pct)).toFixed(1)}%, the network's normal loss.`;
  } else voip.hidden = true;

  document.getElementById("sum-table").innerHTML = `<table><thead><tr><th>Setup</th><th class="num">Readers</th><th class="num">Mean wait</th><th class="num">Median wait</th><th class="num">Outage clicks from cache</th><th class="num">Prefetched per trip</th></tr></thead><tbody>${stats.map((s) => `<tr><td>${esc(setupName(s.c))}</td><td class="num">${s.n}</td><td class="num">${fmtS(s.mean)}</td><td class="num">${fmtS(s.median)}</td><td class="num">${s.outage ? `${s.cached} of ${s.outage}` : "–"}</td><td class="num">${s.pushedMB > 0.005 ? s.pushedMB.toFixed(2) + " MB" : "–"}</td></tr>`).join("")}</tbody></table>`;
}

// ----------------------------------------------------------------------------- playback

function render() {
  refs.scrub.value = S.t;
  refs.clock.textContent = `${fmtT(S.t)} / ${fmtT(duration())}`;
  renderMap();
  renderPhone("left");
  renderPhone("right");
  refs.tlCursor.setAttribute("x1", xOf(S.t));
  refs.tlCursor.setAttribute("x2", xOf(S.t));
}

function frame(now) {
  if (!S.playing) return;
  const dt = (now - S.last) / 1000;
  S.last = now;
  S.t = Math.min(duration(), S.t + dt * S.speed);
  render();
  if (S.t >= duration()) { stop(); writeHash(); return; }
  requestAnimationFrame(frame);
}

function togglePlay() {
  if (S.playing) { stop(); writeHash(); return; }
  if (S.t >= duration() - 0.05) S.t = 0;
  S.playing = true;
  refs.play.textContent = "Pause";
  S.last = performance.now();
  requestAnimationFrame(frame);
}

function stop() {
  S.playing = false;
  if (refs.play) refs.play.textContent = "Play";
}

function seekFromPointer(el, e) {
  const box = el.getBoundingClientRect();
  const x = ((e.clientX - box.left) / box.width) * W;
  S.t = tOf(x);
  render();
  writeHash();
}

load();
