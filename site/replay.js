// Replay page: the 20 runs of the outage test behind the results (summary charts from
// replays/summary.json, made by experiments/site_data.py), and a second-by-second player for any
// run (replays/index.json and one file per run, made by experiments/export_replay.py).
// The page's language (replay.html in English, replay-ar.html in Arabic) picks the strings;
// charts stay left to right. Plain JS + SVG; the player's state lives in the URL hash.

const AR = document.documentElement.lang === "ar";
const L = (en, ar) => (AR ? ar : en);
// Words mixed with numbers inside left-to-right SVG: browsers lay them out left to right (bidi
// isolation is ignored there), so for Arabic the words are passed in reading order and placed
// right to left.
const words = (...w) => (AR ? w.reverse() : w).join(" ");

const BATCH = "tunnel20";
const SCENARIO = "car_tunnel_45s";
const READERS = Array.from({ length: 20 }, (_, i) => String(i));
const DEFAULT = { reader: "5", a: "5", b: "1" };
const SETUPS = {
  "1": { name: L("TCP+TLS", "TCP+TLS"), short: "TCP+TLS", quic: false,
         about: L("Reconnects from scratch after a dropout, like most apps", "يعيد الاتصال من البداية بعد كل انقطاع، كمعظم التطبيقات") },
  "2": { name: L("QUIC", "QUIC"), short: "QUIC", quic: true,
         about: L("Keeps the connection across the outage, no prefetching", "يحافظ على الاتصال عبر الانقطاع، من دون جلب مسبق") },
  "3": { name: L("QUIC + fixed prefetch", "QUIC مع جلب مسبق ثابت"), short: L("Fixed prefetch", "جلب ثابت"), quic: true,
         about: L("Jev prefetching that ignores outage warnings", "جلب مسبق بـJev يتجاهل تنبيهات الانقطاع") },
  "5": { name: L("Shajarah", "شجرة"), short: L("Shajarah", "شجرة"), quic: true,
         about: L("QUIC tunnel, compression, and more prefetching before an outage", "نفق QUIC وضغط، وجلب مسبق أكثر قبل الانقطاع") },
};
const ORDER = ["1", "2", "3", "5"];
const color = (c) => (SETUPS[c].quic ? "var(--series-quic)" : "var(--series-tcp)");

const IFACES = {
  wifi0: { label: L("Wi-Fi", "واي فاي"), color: "var(--net-wifi0)" },
  cell0: { label: L("Cellular", "خلوية"), color: "var(--net-cell0)" },
  sat0: { label: L("Satellite", "قمر صناعي"), color: "var(--net-sat0)" },
};
const SOURCES = {
  push: { label: L("Prefetched", "مُحمّلة مسبقًا") },
  revalidated: { label: L("Saved copy, checked with the server", "نسخة محفوظة تحقّق منها الخادم") },
  stale: { label: L("Saved copy (no signal)", "نسخة محفوظة (لا إشارة)") },
  origin: { label: L("Loaded over the network", "حُمّلت عبر الشبكة") },
  error: { label: L("Failed to load", "تعذّر تحميلها") },
  pending: { label: L("Still waiting at the end", "ما زال الانتظار مستمرًا عند النهاية") },
};
const FROM_PHONE = new Set(["push", "stale", "revalidated"]);
const DBM_MIN = -140, DBM_MAX = -40;
const PAD = { l: 70, r: 12 };
const SPEEDS = [1, 5, 10, 20];
const SEC = L("s", "ث");

const app = document.getElementById("app");
const tooltip = document.getElementById("tooltip");
const cache = new Map(); // file -> run JSON
const state = { runs: [], summary: null, reader: 0, a: null, b: null, t: 0, speed: 5, playing: false };
let lanes = []; // [{ run, root, width }]

// ---------------------------------------------------------------------------- helpers

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmtT = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const setupName = (c) => (SETUPS[c] || { name: c }).name;
const setupAbout = (c) => (SETUPS[c] || { about: "" }).about;
const fmtWait = (w) => (w < 1 ? `${Math.round(w * 1000)} ${L("ms", "ملّي ث")}` : `${w.toFixed(1)} ${SEC}`);
const num = (v) => (v < 1 ? v.toFixed(2) : v.toFixed(1));
const fmtSec = (v) => `${num(v)} s`; // inside charts (left to right): Latin units in both languages
const secText = (v) => `${num(v)} ${L("s", "ثانية")}`; // in page text
const mbText = (v) => `${v} ${L("MB", "ميغابايت")}`;
const readerName = (session) => L(`Reader ${+session + 1}`, `القارئ ${+session + 1}`);

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

// When the link in use was dead, from the scenario's zones: [start, end] or null.
function deadSpan(run) {
  const z = (run.zones[run.start_iface] || []).find((s) => s[2] === "dead" && s[0] > 0);
  return z ? [z[0], Math.min(z[1], run.duration)] : null;
}

const summaryOf = (config, session) => state.summary?.runs.find((r) => r.config === config && String(r.session) === String(session));

function readHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  return { reader: h.get("reader"), a: h.get("a"), b: h.get("b"), t: parseFloat(h.get("t") || "0") || 0 };
}

function writeHash() {
  const h = new URLSearchParams({ reader: state.reader, a: state.a, b: state.b || "none", t: state.t.toFixed(1) });
  history.replaceState(null, "", `#${h}`);
}

// Keep the current view when switching language.
document.querySelectorAll("[data-keep-hash]").forEach((a) => {
  a.addEventListener("click", () => { a.href = a.getAttribute("href").split("#")[0] + location.hash; });
});

// ---------------------------------------------------------------------------- summary

function drawSummary(summary) {
  const runs = summary.runs;
  const of = (c) => runs.filter((r) => r.config === c);
  const med = (xs) => Charts.median(xs.filter((x) => x != null));

  // Headline numbers.
  const rec = (c) => med(of(c).map((r) => r.recovery_s));
  const sent = (c) => of(c).reduce((s, r) => s + r.pages_sent_kb + r.pushed_sent_kb, 0);
  const raw = (c) => of(c).reduce((s, r) => s + r.pages_kb + r.pushed_kb, 0);
  const wasted = of("5").reduce((s, r) => s + r.wasted_sent_kb, 0), pushed = of("5").reduce((s, r) => s + r.pushed_sent_kb, 0);
  document.getElementById("summary-stats").innerHTML = [
    [num(rec("2")), L("s", "ثانية"), L(`QUIC recovery after the outage (median), against ${secText(rec("1"))} for TCP+TLS`, `زمن تعافي QUIC بعد الانقطاع (القيمة الوسيطة)، مقابل ${secText(rec("1"))} لـTCP+TLS`), true],
    [`${(raw("2") / sent("2")).toFixed(1)}×`, "", L(`less data sent by QUIC over its 20 runs: ${mbText((sent("2") / 1000).toFixed(1))} instead of ${mbText((raw("2") / 1000).toFixed(1))}`, `بيانات أقل أرسلها QUIC في تشغيلاته العشرين: ${mbText((sent("2") / 1000).toFixed(1))} بدلًا من ${mbText((raw("2") / 1000).toFixed(1))}`)],
    [`${(100 * wasted / pushed).toFixed(1)}%`, "", L(`of Shajarah's prefetched data was never opened: ${mbText((wasted / 1000 / of("5").length).toFixed(2))} per run`, `من البيانات التي جلبتها شجرة مسبقًا لم يُفتح قط: ${mbText((wasted / 1000 / of("5").length).toFixed(2))} لكل تشغيل`)],
  ].map(([n, unit, p, dark]) => `<div class="stat${dark ? " dark" : ""}"><div class="num">${esc(n)}${unit ? ` <small>${esc(unit)}</small>` : ""}</div><p>${esc(p)}</p></div>`).join("");

  const pick = (id) => { const [c, s] = id.split(":"); openRun(s, c); };
  const rows = (key) => ORDER.map((c) => ({
    label: SETUPS[c].short,
    color: color(c),
    median: med(of(c).map((r) => r[key])),
    points: of(c).filter((r) => r[key] != null).map((r) => ({
      v: r[key], id: `${c}:${r.session}`,
      tip: `<b>${esc(readerName(r.session))}</b>${L(",", "،")} ${esc(setupName(c))}<br>${esc(secText(r[key]))}<br><span class="muted">${esc(L("Click to play this run", "انقر لعرض هذا التشغيل"))}</span>`,
    })),
  }));
  const medianLabel = (v) => words(L("median", "الوسيط"), fmtSec(v));
  Charts.strip(document.getElementById("chart-recovery"), {
    rows: rows("recovery_s"), max: 30, ticks: [0, 10, 20, 30], fmt: (v) => `${v} s`, medianLabel, onPick: pick, labelWidth: 120,
    label: L("Recovery time per reader and setup", "زمن التعافي لكل قارئ وإعداد"),
  });
  Charts.strip(document.getElementById("chart-wait"), {
    rows: rows("wait_s"), max: 64, ticks: [0, 20, 40, 60], fmt: (v) => `${v} s`, medianLabel, onPick: pick, labelWidth: 120,
    label: L("Total waiting per reader and setup", "إجمالي الانتظار لكل قارئ وإعداد"),
  });
  Charts.columns(document.getElementById("chart-data"), {
    series: [{ label: L("Uncompressed", "غير مضغوطة"), color: "var(--series-tcp)" }, { label: L("Sent through the tunnel", "المرسَلة عبر النفق"), color: "var(--series-quic)" }],
    groups: ORDER.map((c) => ({ label: SETUPS[c].short, values: [raw(c) / 1000 / of(c).length, sent(c) / 1000 / of(c).length] })),
    fmt: (v) => `${v.toFixed(v < 1 ? 2 : 1)} MB`, height: 230,
    label: L("Average data per run, uncompressed and sent", "متوسط البيانات لكل تشغيل، غير مضغوطة ومرسَلة"),
  });
  const loads = summary.page_loads, link = { wifi: L("Wi-Fi", "واي فاي"), "5g": "5G" };
  const loadRows = [];
  for (const k of ["wifi", "5g"]) {
    for (const c of ["1", "2"]) {
      const xs = loads[c][k];
      loadRows.push({
        label: AR ? `${SETUPS[c].short} · ${link[k]}` : `${link[k]} · ${SETUPS[c].short}`, color: color(c), median: Charts.median(xs),
        points: xs.map((ms) => ({ v: ms, tip: `${esc(link[k])}${L(",", "،")} ${esc(setupName(c))}: ${ms} ${L("ms", "ملّي ثانية")}` })),
      });
    }
  }
  Charts.strip(document.getElementById("chart-loads"), {
    rows: loadRows, max: 800, ticks: [0, 200, 400, 600, 800], fmt: (v) => `${v} ms`, labelWidth: 130,
    medianLabel: (v) => words(L("median", "الوسيط"), `${Math.round(v)} ms`),
    label: L("Page load times away from any drop", "أزمنة تحميل الصفحات بعيدًا عن أي انقطاع"),
  });

  // Every run as a table.
  const head = [L("Reader", "القارئ"), L("Setup", "الإعداد"), L("Recovery", "التعافي"), L("Waiting", "الانتظار"),
    L("Outage clicks from the phone", "نقرات الانقطاع من الهاتف"), L("Sent", "المرسَل"), L("Uncompressed", "غير مضغوط"), L("Prefetch never opened", "مجلوب لم يُفتح")];
  const body = [...runs].sort((a, b) => a.session - b.session || ORDER.indexOf(a.config) - ORDER.indexOf(b.config)).map((r) => `<tr>
    <td>${esc(readerName(r.session))}</td><td>${esc(setupName(r.config))}</td>
    <td class="num">${r.recovery_s == null ? "–" : secText(r.recovery_s)}</td><td class="num">${secText(r.wait_s)}</td>
    <td class="num">${r.outage_cached} / ${r.outage_pages}</td>
    <td class="num">${mbText(((r.pages_sent_kb + r.pushed_sent_kb) / 1000).toFixed(2))}</td><td class="num">${mbText(((r.pages_kb + r.pushed_kb) / 1000).toFixed(2))}</td>
    <td class="num">${r.wasted_sent_kb ? mbText((r.wasted_sent_kb / 1000).toFixed(2)) : "–"}</td></tr>`).join("");
  document.getElementById("summary-table").innerHTML = `<table><thead><tr>${head.map((h, i) => `<th${i > 1 ? ' class="num"' : ""}>${esc(h)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>`;
}

// ---------------------------------------------------------------------------- loading

async function load() {
  let index = { runs: [] };
  try {
    const [ri, rs] = await Promise.all([fetch("replays/index.json", { cache: "no-cache" }), fetch("replays/summary.json", { cache: "no-cache" })]);
    if (ri.ok) index = await ri.json();
    if (rs.ok) state.summary = await rs.json();
  } catch { /* shown below */ }
  if (state.summary) drawSummary(state.summary);
  state.runs = (index.runs || []).filter((r) => r.batch === BATCH && r.scenario === SCENARIO && SETUPS[r.config]);
  if (!state.runs.length) {
    app.innerHTML = `<div class="callout"><p><b>${L("The recorded runs are missing.", "التشغيلات المسجّلة غير موجودة.")}</b></p>
      <p><code>python -m experiments.export_replay tunnel20 && python -m experiments.site_data</code></p></div>`;
    return;
  }
  const want = readHash();
  state.reader = READERS.includes(want.reader) ? want.reader : DEFAULT.reader;
  const configs = configsFor();
  state.a = configs.includes(want.a) ? want.a : DEFAULT.a;
  state.b = want.b === "none" ? null : configs.includes(want.b) && want.b !== state.a ? want.b
    : (state.a !== DEFAULT.b ? DEFAULT.b : null);
  state.t = want.t;
  buildControls();
  await showRuns();
}

const configsFor = () => ORDER.filter((c) => entry(c));
const entry = (config) => state.runs.find((r) => String(r.session) === state.reader && r.config === config);

async function getRun(e) {
  if (!cache.has(e.file)) {
    const r = await fetch(`replays/${e.file}`);
    cache.set(e.file, await r.json());
  }
  return cache.get(e.file);
}

// A dot in the summary charts opens that run, compared with a sensible second setup.
async function openRun(session, config) {
  if (!state.runs.length) return;
  state.reader = String(session);
  state.a = config;
  state.b = config === "1" ? "2" : "1";
  state.t = 0;
  buildControls();
  await showRuns();
  document.getElementById("player").scrollIntoView({ behavior: "smooth" });
}

// ---------------------------------------------------------------------------- controls

function options(values, selected, label = (v) => v) {
  return values.map((v) => `<option value="${esc(v)}"${v === selected ? " selected" : ""}>${esc(label(v))}</option>`).join("");
}

function buildControls() {
  const configs = configsFor();
  app.innerHTML = `
    <div class="controls">
      <label>${L("Reader", "القارئ")}<select id="reader">${options(READERS, state.reader, readerName)}</select></label>
      <label>${L("Setup", "الإعداد")}<select id="a">${options(configs, state.a, setupName)}</select></label>
      <label>${L("Compare with", "مقارنة مع")}<select id="b"><option value="none">${L("(none)", "(لا شيء)")}</option>${options(configs, state.b, setupName)}</select></label>
      <div class="timebar">
        <button class="primary" id="play">${L("Play", "تشغيل")}</button>
        <select id="speed" aria-label="${L("Playback speed", "سرعة التشغيل")}">${options(SPEEDS.map(String), String(state.speed), (s) => `${s}×`)}</select>
        <input type="range" id="time" min="0" max="120" step="0.1" value="0" aria-label="${L("Time", "الوقت")}" dir="ltr">
        <span class="clock" id="clock" dir="ltr">0:00</span>
      </div>
    </div>
    <div class="lanes" id="lanes"></div>`;
  const on = (id, ev, fn) => document.getElementById(id).addEventListener(ev, fn);
  on("reader", "change", (e) => { state.reader = e.target.value; state.t = 0; showRuns(); });
  on("a", "change", (e) => { state.a = e.target.value; showRuns(); });
  on("b", "change", (e) => { state.b = e.target.value === "none" ? null : e.target.value; showRuns(); });
  on("speed", "change", (e) => { state.speed = +e.target.value; });
  on("time", "input", (e) => { setTime(+e.target.value); });
  on("play", "click", togglePlay);
}

// ---------------------------------------------------------------------------- playback

let lastFrame = null;
function togglePlay() {
  state.playing = !state.playing;
  if (state.playing && state.t >= duration() - 0.05) setTime(0);
  document.getElementById("play").textContent = state.playing ? L("Pause", "إيقاف مؤقت") : L("Play", "تشغيل");
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
  const picks = [state.a, state.b].filter((c, i, all) => c && all.indexOf(c) === i && entry(c));
  const runs = await Promise.all(picks.map((c) => getRun(entry(c))));
  const box = document.getElementById("lanes");
  box.className = `lanes${runs.length > 1 ? " two" : ""}`;
  box.innerHTML = "";
  lanes = runs.map((run) => {
    const root = document.createElement("section");
    root.className = "lane";
    box.appendChild(root);
    return { run, root, width: 0, summary: summaryOf(run.config, run.session) };
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
    <h3>${esc(setupName(run.config))}</h3>
    <p class="sub">${esc(setupAbout(run.config))}</p>
    <div class="tiles" data-role="tiles"></div>
    <div class="chart"><div class="title">${L("Signal strength (dBm)", "قوة الإشارة (dBm)")}</div>${signalSvg(run, shown, x, W)}
      <div class="legend">${shown.map((i) => `<span><i class="key line" style="background:${IFACES[i].color}"></i>${IFACES[i].label}</span>`).join("")}
        <span><i class="key line" style="background:var(--muted)"></i>${L("unusable below (dashed)", "غير صالحة تحت الخط المتقطع")}</span></div></div>
    <div class="chart"><div class="title">${L("Round trip through the tunnel (ms), and when it came back", "زمن الذهاب والإياب عبر النفق (ملّي ثانية)، ومتى عاد")}</div>${latencySvg(run, x, W, lane.summary)}</div>
    <div class="chart"><div class="title">${L("Wikipedia articles clicked: each bar is one click, its length how long the reader waited. Below: pages the server sent ahead.", "مقالات ويكيبيديا التي نُقرت: كل شريط نقرة واحدة، وطوله مدة انتظار القارئ. في الأسفل: الصفحات التي أرسلها الخادم مسبقًا.")}</div>${pagesSvg(run, x, W)}
      <div class="legend">${legendSources(run)}</div></div>
    <details class="data"><summary>${L("Wikipedia articles opened", "مقالات ويكيبيديا التي فُتحت")}</summary>${pageTable(run)}</details>`;
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
  tooltip.style.left = `${Math.max(8, Math.min(e.clientX + pad, innerWidth - w - 8))}px`;
  tooltip.style.top = `${Math.max(8, e.clientY - h - pad)}px`;
}
function hideTip() { tooltip.style.display = "none"; }

// Shared frame: outage band, time grid, cursor.
function frame(run, x, W, H, top = 0, bottom = H, axis = false) {
  let s = "";
  const dead = deadSpan(run);
  if (dead) s += `<rect class="outage" x="${x(dead[0])}" y="${top}" width="${x(dead[1]) - x(dead[0])}" height="${bottom - top}"/>`;
  const step = run.duration > 90 ? 20 : 10;
  for (let t = 0; t <= run.duration + 0.01; t += step) {
    s += `<line class="grid" x1="${x(t)}" x2="${x(t)}" y1="${top}" y2="${bottom}"/>`;
    if (axis) s += `<text x="${x(t)}" y="${bottom + 13}" text-anchor="middle">${fmtT(t)}</text>`;
  }
  return s;
}
const cursor = (H) => `<line class="cursor" data-role="cursor" x1="0" x2="0" y1="0" y2="${H}"/>`;

function signalSvg(run, shown, x, W) {
  const H = 130, top = 6, bottom = H - 18;
  const y = (dbm) => top + ((DBM_MAX - clamp(dbm, DBM_MIN, DBM_MAX)) / (DBM_MAX - DBM_MIN)) * (bottom - top);
  let s = frame(run, x, W, H, top, bottom, true);
  for (const v of [-60, -80, -100, -120]) {
    s += `<line class="grid" x1="${PAD.l}" x2="${W - PAD.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${PAD.l - 8}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  }
  const thresholds = [...new Set(shown.map((i) => run.thresholds[i]))];
  for (const v of thresholds) s += `<line x1="${PAD.l}" x2="${W - PAD.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--muted)" stroke-dasharray="4 4"/>`;
  for (const iface of shown) {
    const pts = run.signal.t.map((t, j) => [t, run.signal.dbm[iface][j]]).filter(([, v]) => v !== null);
    s += `<polyline fill="none" stroke="${IFACES[iface].color}" stroke-width="2" stroke-linejoin="round" points="${pts.map(([t, v]) => `${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ")}"/>`;
  }
  // Dropout warnings (the phone's own prediction) and warnings sent to the server.
  for (const w of run.warnings.filter((w) => w.on && shown.includes(w.iface))) {
    const tip = `<b>${fmtT(w.t)}</b> ${IFACES[w.iface].label}: ${L("dropout predicted from the fading signal", "انقطاع متوقع بسبب ضعف الإشارة")}`;
    s += `<g class="hit" data-tip="${esc(tip)}"><path d="M${x(w.t)},${top + 2} l-6,10 h12 z" fill="var(--warning)" stroke="var(--bg)"/><rect x="${x(w.t) - 8}" y="${top}" width="16" height="14" fill="transparent"/></g>`;
  }
  for (const h of run.hints.filter((h) => h.on)) {
    const tip = `<b>${fmtT(h.t)}</b> ${L("server told a dropout is coming: it starts prefetching", "أُبلغ الخادم بانقطاع وشيك: فبدأ الجلب المسبق")}`;
    s += `<g class="hit" data-tip="${esc(tip)}"><line x1="${x(h.t)}" x2="${x(h.t)}" y1="${top}" y2="${bottom}" stroke="var(--warning)" stroke-width="1.5"/><rect x="${x(h.t) - 5}" y="${top}" width="10" height="${bottom - top}" fill="transparent"/></g>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" height="${H}">${s}${cursor(bottom)}</svg>`;
}

function latencySvg(run, x, W, summary) {
  const H = 96, top = 6, bottom = H - 30;
  const ok = run.pings.filter((p) => p[1] !== null).map((p) => p[1]).sort((a, b) => a - b);
  if (!run.pings.length) {
    return `<svg viewBox="0 0 ${W} 28" height="28"><text x="${PAD.l}" y="18">${L("Not recorded for this run.", "لم يُسجَّل في هذا التشغيل.")}</text></svg>`;
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
  s += `<path d="${path}" fill="none" stroke="var(--ink)" stroke-width="1.5" stroke-linejoin="round"/>`;
  s += `<text x="${PAD.l - 8}" y="${bottom + 13}" text-anchor="end">${L("no reply", "لا رد")}</text>`;
  for (const [t, ms] of run.pings) if (ms === null) s += `<rect x="${x(t) - 0.75}" y="${bottom + 5}" width="1.5" height="8" fill="var(--critical)"/>`;
  // Recovery: from the link coming back to the first reply, as on the results slide.
  const dead = deadSpan(run);
  if (dead && summary?.recovery_s != null) {
    const x0 = x(dead[1]), x1 = x(Math.min(dead[1] + summary.recovery_s, run.duration));
    const label = words(L("back after", "عاد بعد"), fmtSec(summary.recovery_s));
    s += `<rect x="${x0}" y="${top}" width="${Math.max(2, x1 - x0)}" height="${bottom - top}" fill="var(--orange)" fill-opacity="0.18"/>`
      + `<line x1="${x1}" x2="${x1}" y1="${top}" y2="${bottom}" stroke="var(--orange)" stroke-width="2"/>`
      + `<text x="${x1 + 5}" y="${top + 11}" class="ink" style="font-weight:700">${esc(label)}</text>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" height="${H}">${s}${cursor(H - 16)}</svg>`;
}

// Where a clicked article came from, as three outcomes (the page table keeps the finer detail).
function outcome(p) {
  if (p.source === "error") return { key: "failed", label: L("Failed to load", "تعذّر تحميلها"), color: "var(--critical)" };
  if (FROM_PHONE.has(p.source)) return { key: "phone", label: L("Opened from the phone (prefetched or saved)", "فُتحت من الهاتف (مُحمّلة مسبقًا أو محفوظة)"), color: "var(--orange)" };
  if (p.source === "pending") return { key: "pending", label: L("Still loading when the run ended", "ما زالت تُحمَّل عند نهاية التشغيل"), color: "var(--series-tcp)" };
  return { key: "network", label: L("Loaded over the network", "حُمّلت عبر الشبكة"), color: "var(--series-tcp)" };
}

// Prefetched pages arrive in bursts; one bar per burst reads far better than one line per page.
// Bursts a few seconds apart (a warning, then pushes for the pages after those) count as one.
const BURST_GAP_S = 4;
function pushBursts(run) {
  const out = [];
  for (const p of run.pushes) {
    const b = out[out.length - 1];
    if (b && p.t - b.end < BURST_GAP_S) { b.end = p.t; b.n += 1; b.kb += p.kb; if (p.opened) b.opened += 1; continue; }
    out.push({ start: p.t, end: p.t, n: 1, kb: p.kb, opened: p.opened ? 1 : 0 });
  }
  return out;
}

function pagesSvg(run, x, W) {
  const H = 104, rowA = 6, hA = 26, rowB = 56, hB = 22;
  let s = frame(run, x, W, H, 0, H - 16, true);
  s += `<text x="${PAD.l - 8}" y="${rowA + 17}" text-anchor="end" class="ink">${L("clicked", "النقرات")}</text>`;
  s += `<text x="${PAD.l - 8}" y="${rowB + 15}" text-anchor="end" class="ink">${L("sent ahead", "أُرسلت مسبقًا")}</text>`;
  for (const p of run.pages) {
    const o = outcome(p);
    const x0 = x(p.t), w = Math.max(5, x(Math.min(p.t + p.wait, run.duration)) - x0);
    const tip = `<b>${esc(p.title)}</b><br>${L("Wikipedia article, clicked at", "مقالة ويكيبيديا، نُقرت عند")} ${fmtT(p.t)}<br>${(SOURCES[p.source] || SOURCES.origin).label} · ${L("waited", "الانتظار")} ${fmtWait(p.wait)}`;
    s += `<g class="hit" data-tip="${esc(tip)}"><rect x="${x0}" y="${rowA}" width="${w}" height="${hA}" rx="4" fill="${o.color}"/><rect x="${x0 - 3}" y="${rowA - 2}" width="${w + 6}" height="${hA + 4}" fill="transparent"/></g>`;
    if (o.key === "failed") s += `<text x="${x0 + w / 2}" y="${rowA + 18}" text-anchor="middle" style="fill:#fff;font-weight:700">✕</text>`;
    if (p.wait >= 1) {
      const n = String(Math.round(p.wait));
      const label = AR ? words("انتظار", n, SEC) : words(n, SEC, "wait");
      const inside = w > 70;
      s += `<text x="${inside ? x0 + 6 : x0 + w + 4}" y="${rowA + 17}" style="fill:${inside ? "#fff" : "var(--ink)"};font-weight:600">${label}</text>`;
    }
  }
  let labelEnd = -Infinity; // x where the previous burst label ends, to avoid overlaps
  for (const b of pushBursts(run)) {
    const x0 = x(b.start), w = Math.max(4, x(b.end) - x0);
    const tip = `${L("Server sent", "أرسل الخادم")} ${b.n} ${L(b.n > 1 ? "pages ahead" : "page ahead", "صفحة مسبقًا")} (${(b.kb / 1000).toFixed(2)} MB), ${fmtT(b.start)}–${fmtT(b.end)}<br>${L(`${b.opened} of them opened by the reader`, `فتح القارئ ${b.opened} منها`)}`;
    s += `<g class="hit" data-tip="${esc(tip)}"><rect x="${x0}" y="${rowB}" width="${w}" height="${hB}" rx="3" fill="var(--push-burst)"/><rect x="${x0 - 3}" y="${rowB - 2}" width="${w + 6}" height="${hB + 4}" fill="transparent"/></g>`;
    const label = words(String(b.n), L("pages", "صفحة"));
    if (b.n >= 3 && x0 + w + 4 > labelEnd + 6) {
      s += `<text x="${x0 + w + 4}" y="${rowB + 15}" class="ink">${label}</text>`;
      labelEnd = x0 + w + 4 + label.length * 6.5;
    }
  }
  return `<svg viewBox="0 0 ${W} ${H}" height="${H}">${s}${cursor(H - 16)}</svg>`;
}

function legendSources(run) {
  const seen = [];
  for (const p of run.pages) { const o = outcome(p); if (!seen.some((x) => x.key === o.key)) seen.push(o); }
  let html = seen.map((o) => `<span><i class="key" style="background:${o.color}"></i>${o.label}</span>`).join("");
  if (run.pushes.length) html += `<span><i class="key" style="background:var(--push-burst)"></i>${L("pages the server sent ahead", "صفحات أرسلها الخادم مسبقًا")}</span>`;
  return html + `<span><i class="key" style="background:var(--outage);outline:1px solid var(--line)"></i>${L("no signal", "لا إشارة")}</span>`;
}

function pageTable(run) {
  const rows = run.pages.map((p) => `<tr><td class="num" dir="ltr">${fmtT(p.t)}</td><td lang="en" dir="ltr">${esc(p.title)}</td>
    <td>${esc((SOURCES[p.source] || SOURCES.origin).label)}</td><td class="num">${fmtWait(p.wait)}</td></tr>`).join("");
  return `<p class="muted small">${L("The reader browses English Wikipedia; each row is an article they clicked, in order.", "يتصفح القارئ ويكيبيديا الإنجليزية؛ كل صف مقالة نقر عليها، بالترتيب.")}</p>
    <div class="table-wrap"><table><thead><tr><th class="num">${L("Clicked at", "وقت النقر")}</th><th>${L("Wikipedia article", "مقالة ويكيبيديا")}</th><th>${L("Opened from", "المصدر")}</th><th class="num">${L("Wait", "الانتظار")}</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

// ---------------------------------------------------------------------------- per-frame update

function updateLane(lane) {
  const { run, root, x, summary } = lane;
  const t = Math.min(state.t, run.duration);
  root.querySelectorAll('[data-role="cursor"]').forEach((c) => { c.setAttribute("x1", x(t)); c.setAttribute("x2", x(t)); });

  const iface = activeAt(run, t);
  const pi = lastAtOrBefore(run.pings, t, (p) => p[0]);
  const lastPing = pi >= 0 ? run.pings[pi] : null;
  const dead = deadSpan(run);
  let status;
  if (lastPing && lastPing[1] === null && t - lastPing[0] < 1.5) status = [L("Offline", "غير متصل"), "var(--critical)", "✕"];
  else if (!run.pings.length && dead && t >= dead[0] && t < dead[1]) status = [L("Outage", "انقطاع"), "var(--critical)", "✕"];
  else if (warningOn(run, iface, t)) status = [L("Dropout predicted", "انقطاع متوقع"), "var(--warning)", "!"];
  else status = [L("Online", "متصل"), "var(--good)", "✓"];
  const dbm = signalAt(run, iface, t);

  let waited = 0, opened = 0, fromPhone = 0, waitingNow = false;
  const read = new Set(); // prefetched articles the reader has opened so far
  for (const p of run.pages) {
    if (p.t > t) continue;
    waited += Math.min(p.wait, t - p.t);
    if (p.t + p.wait > t) { waitingNow = true; continue; }
    if (p.source === "error") continue; // failed, never opened
    opened += 1;
    if (FROM_PHONE.has(p.source)) fromPhone += 1;
    if (p.source === "push") read.add(p.title);
  }
  let pushedKb = 0, readKb = 0;
  const counted = new Set();
  for (const p of run.pushes) {
    if (p.t > t) continue;
    pushedKb += p.kb;
    if (read.has(p.title) && !counted.has(p.title)) { readKb += p.kb; counted.add(p.title); }
  }
  const prefetched = pushedKb
    ? `${(pushedKb / 1000).toFixed(2)} <small>${L("MB", "ميغابايت")} · ${L(`${Math.round((100 * readKb) / pushedKb)}% opened`, `فُتح ${Math.round((100 * readKb) / pushedKb)}%`)}</small>`
    : `0 <small>${L("none", "لا شيء")}</small>`;
  let recovery = "–";
  if (dead && summary?.recovery_s != null) {
    const back = dead[1] + summary.recovery_s;
    recovery = t < dead[0] ? `– <small>${L("before the outage", "قبل الانقطاع")}</small>`
      : t < back ? `… <small>${L("no traffic yet", "لا حركة بعد")}</small>`
        : `${secText(summary.recovery_s)} <small>${L("after 5G returned", "بعد عودة 5G")}</small>`;
  }

  root.querySelector('[data-role="tiles"]').innerHTML = [
    tile(L("Status", "الحالة"), `<span class="status"><i class="key dot" style="background:${status[1]}"></i>${status[2]} ${status[0]}</span>`),
    tile(L("Signal", "الإشارة"), dbm === null ? "–" : `<span dir="ltr">${Math.round(dbm)}</span> <small>dBm</small>`),
    tile(L("Recovery", "التعافي"), recovery),
    tile(L("Articles opened", "المقالات المفتوحة"), `${opened}${fromPhone ? ` <small>${L(`${fromPhone} from the phone`, `${fromPhone} من الهاتف`)}</small>` : ""}`),
    tile(L("Time waiting", "وقت الانتظار"), `${waited.toFixed(1)} <small>${SEC}${waitingNow ? L(" · waiting now", " · ينتظر الآن") : ""}</small>`),
    tile(L("Prefetched", "المُحمّل مسبقًا"), prefetched),
  ].join("");
}

const tile = (k, v) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`;

load();
