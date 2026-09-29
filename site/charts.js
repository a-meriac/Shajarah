// Small SVG charts for the Shajarah site, shared by the English and Arabic pages. Drawn at the
// container's real pixel width (redrawn on resize), so marks keep their specified sizes. Charts
// stay left to right on Arabic pages too; only their words are translated.
//
//   Charts.columns(el, { series, groups, fmt })   grouped columns, value on each cap
//   Charts.strip(el, { rows, max, ticks, fmt, unit, onPick })   one dot per run, median marked

(() => {
  const NS = "http://www.w3.org/2000/svg";
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  let tip = null;

  function tooltip() {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "tooltip";
      tip.setAttribute("role", "tooltip");
      document.body.appendChild(tip);
    }
    return tip;
  }
  function showTip(e, html) { const t = tooltip(); t.innerHTML = html; t.style.display = "block"; moveTip(e); }
  function moveTip(e) {
    const t = tooltip(), pad = 14;
    t.style.left = `${Math.max(8, Math.min(e.clientX + pad, innerWidth - t.offsetWidth - 8))}px`;
    t.style.top = `${Math.max(8, e.clientY - t.offsetHeight - pad)}px`;
  }
  function hideTip() { if (tip) tip.style.display = "none"; }

  function wireTips(root) {
    root.querySelectorAll("[data-tip]").forEach((el) => {
      el.addEventListener("pointerenter", (e) => showTip(e, el.dataset.tip));
      el.addEventListener("pointermove", moveTip);
      el.addEventListener("pointerleave", hideTip);
    });
  }

  // Redraw whenever the container's width changes.
  function responsive(el, draw) {
    let width = 0;
    const render = () => {
      const w = Math.floor(el.clientWidth);
      if (!w || Math.abs(w - width) < 2) return;
      width = w;
      el.innerHTML = draw(w);
      wireTips(el);
    };
    new ResizeObserver(render).observe(el);
    render();
  }

  // A column whose data end is rounded (4px) and whose baseline end is square.
  function column(x, y, w, h) {
    const r = Math.min(4, h, w / 2);
    return `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h} Z`;
  }

  // A value on each cap. Two bars side by side have labels wider than the bars, so the first
  // label ends at its bar's right edge and the second starts at its bar's left edge.
  function label(x, y, si, n, text) {
    const [ax, anchor] = n === 2 ? (si === 0 ? [x + 24, "end"] : [x, "start"]) : [x + 12, "middle"];
    return `<text class="val" x="${ax}" y="${y - 6}" text-anchor="${anchor}">${esc(text)}</text>`;
  }

  function columns(el, cfg) {
    const H = cfg.height || 210, top = 24, bottom = H - 30, BAR = 24, GAP = 2;
    const max = Math.max(...cfg.groups.flatMap((g) => g.values.map((v) => v ?? 0))) * 1.08;
    responsive(el, (W) => {
      const slot = W / cfg.groups.length;
      let s = `<line class="axis" x1="0" x2="${W}" y1="${bottom}" y2="${bottom}"/>`;
      cfg.groups.forEach((g, gi) => {
        const n = g.values.length, width = n * BAR + (n - 1) * GAP;
        const x0 = gi * slot + (slot - width) / 2;
        g.values.forEach((v, si) => {
          if (v == null) return;
          const se = cfg.series[si], x = x0 + si * (BAR + GAP);
          const h = Math.max(1, (v / max) * (bottom - top)), y = bottom - h;
          const text = cfg.fmt(v);
          const tipHtml = `<b>${esc(g.label)}</b><br>${esc(se.label)}: ${esc(text)}`;
          s += `<g class="hit" data-tip="${esc(tipHtml)}" tabindex="0" aria-label="${esc(`${g.label}, ${se.label}: ${text}`)}">`
            + `<rect x="${x - 6}" y="${top - 16}" width="${BAR + 12}" height="${bottom - top + 16}" fill="transparent"/>`
            + `<path d="${column(x, y, BAR, h)}" fill="${se.color}"/></g>`
            + label(x, y, si, n, text);
        });
        s += `<text x="${gi * slot + slot / 2}" y="${bottom + 20}" text-anchor="middle" class="ink">${esc(g.label)}</text>`;
      });
      return `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(cfg.label || "")}">${s}</svg>`;
    });
  }

  // One row per setup, one dot per run, the median marked with a tick. The row's label carries its
  // name and median, so nothing is written over the dots. Dots with near-equal values spread up and
  // down within their row (a small beeswarm), overlapping a little once the row is full.
  function strip(el, cfg) {
    const ROW = 48, top = 8, LABEL = cfg.labelWidth || 150, R = 5, SPREAD = [0, -7, 7, -14, 14];
    const H = top + cfg.rows.length * ROW + 26;
    responsive(el, (W) => {
      const left = Math.min(LABEL, W * 0.38), right = 16;
      const x = (v) => left + (Math.min(v, cfg.max) / cfg.max) * (W - left - right);
      let s = "";
      for (const t of cfg.ticks) {
        s += `<line class="grid" x1="${x(t)}" x2="${x(t)}" y1="${top}" y2="${H - 26}"/>`
          + `<text x="${x(t)}" y="${H - 8}" text-anchor="middle">${esc(cfg.fmt(t))}</text>`;
      }
      cfg.rows.forEach((row, ri) => {
        const cy = top + ri * ROW + ROW / 2;
        s += `<text x="${left - 12}" y="${row.median != null ? cy - 2 : cy + 4}" text-anchor="end" class="ink">${esc(row.label)}</text>`;
        if (row.median != null) s += `<text x="${left - 12}" y="${cy + 13}" text-anchor="end">${esc(cfg.medianLabel(row.median))}</text>`;
        s += `<line class="axis" x1="${left}" x2="${W - right}" y1="${cy}" y2="${cy}"/>`;
        if (row.median != null) {
          const mx = x(row.median);
          s += `<line x1="${mx}" x2="${mx}" y1="${cy - 20}" y2="${cy + 20}" stroke="var(--ink)" stroke-width="2"/>`;
        }
        const placed = [];
        for (const p of [...row.points].sort((a, b) => a.v - b.v)) {
          const px = x(p.v);
          const clash = (dy) => placed.filter(([qx, qy]) => Math.hypot(qx - px, qy - dy) < 2 * R + 1).length;
          const dy = SPREAD.reduce((best, d) => (clash(d) < clash(best) ? d : best), SPREAD[0]);
          placed.push([px, dy]);
          const over = p.v > cfg.max ? " ›" : "";
          s += `<g class="hit" data-tip="${esc(`${p.tip}${over}`)}"${cfg.onPick ? ` data-pick="${esc(p.id)}"` : ""} tabindex="0" aria-label="${esc(p.tip.replace(/<[^>]+>/g, " "))}">`
            + `<circle cx="${px}" cy="${cy + dy}" r="${R + 5}" fill="transparent"/>`
            + `<circle cx="${px}" cy="${cy + dy}" r="${R}" fill="${row.color}" fill-opacity="0.9" stroke="var(--bg)" stroke-width="1.5"/></g>`;
        }
      });
      return `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(cfg.label || "")}">${s}</svg>`;
    });
    if (cfg.onPick) {
      el.addEventListener("click", (e) => { const g = e.target.closest("[data-pick]"); if (g) cfg.onPick(g.dataset.pick); });
      el.addEventListener("keydown", (e) => {
        const g = e.target.closest("[data-pick]");
        if (g && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); cfg.onPick(g.dataset.pick); }
      });
    }
  }

  const median = (xs) => {
    const s = [...xs].sort((a, b) => a - b), m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  };

  window.Charts = { columns, strip, median, esc, showTip, moveTip, hideTip };
})();
