// Page behaviour shared by the English and Arabic home pages: mark the section being read in the
// contents list, and draw the results charts (their words come from the page).

(() => {
  function markSections() {
    const links = [...document.querySelectorAll('nav.toc a[href^="#"]')];
    const byId = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
    const heads = [...byId.keys()].map((id) => document.getElementById(id)).filter(Boolean);
    if (!heads.length) return;
    const mark = () => {
      let current = heads[0];
      for (const h of heads) if (h.getBoundingClientRect().top < innerHeight * 0.35) current = h;
      // The last section is too short to scroll its top that far: at the page bottom, it's current.
      if (innerHeight + scrollY >= document.documentElement.scrollHeight - 4) current = heads[heads.length - 1];
      const active = current ? byId.get(current.id) : null;
      for (const a of links) a.classList.toggle("active", a === active);
      if (active) {
        const bar = active.parentElement;
        if (bar.scrollWidth > bar.clientWidth) {
          const left = active.offsetLeft - bar.clientWidth / 2 + active.clientWidth / 2;
          bar.scrollTo({ left, behavior: "smooth" });
        }
      }
    };
    addEventListener("scroll", mark, { passive: true });
    addEventListener("resize", mark);
    mark();
  }

  function home(cfg) {
    markSections();
    Charts.columns(document.getElementById("chart-recovery"), { series: cfg.series, ...cfg.recovery });
    Charts.columns(document.getElementById("chart-loads"), { series: cfg.series, ...cfg.loads });
  }

  window.Site = { home, markSections };
})();
