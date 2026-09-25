"""Choose the outage push cutoff offline: how often would the reader's next page already be pushed?

Replays the browsing sessions (data/sessions.json) against the cached Jev answers, with no
emulation and no API calls. Every page view counts as "the page on screen when the dropout
warning arrives", so ~300 views stand in for the ~5 outage clicks one emulation batch produces.
For each push rule (a probability cutoff, or Jev's top k) it reports:

- next page pushed: share of views where the page the reader clicked next was pushed
  (with a 95% bootstrap interval over sessions; views within a session aren't independent)
- pages / MB pushed per warning, in bytes on the wire (compressed, as the tunnel sends them)
- useful data: share of pushed bytes that the reader then opened
- 2nd click too: the next page AND the one after it were both pushed, if the server also pushed
  the likely links of every page it pushed ("depth 2"). Today the server pushes one click ahead
  only: pages opened from the cache during an outage can't reach it, so a second click inside
  the outage always misses. The depth-2 page count is an estimate (pages x pages).

The ceiling row is the share of next pages among the links Jev was offered at all.

  python -m experiments.cutoff_sweep            # needs data/snapshot and data/cache/jev
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from data.build_snapshot import page_path
from edgeproxy.common.protocol import HISTORY_LEN, Frame, MsgType
from edgeproxy.common.settings import load_settings
from edgeproxy.server.links import wiki_title
from edgeproxy.server.proxy import build_page_state, make_predictor
from experiments.harness import ROOT, load_sessions, page_url

THRESHOLDS = [0.5, 0.3, 0.25, 0.2, 0.1, 0.05, 0.03, 0.02, 0.01, 0.0]
TOP_K = [1, 2, 3, 5, 10, 20, 40]
OUT = ROOT / "results" / "cutoff_sweep.json"


@dataclass
class View:
    session: int
    probs: dict[str, float]  # Jev's answer for the page on screen (offered links only)
    next_url: str  # the page the reader opened next
    next_probs: dict[str, float] | None = None  # Jev's answer for that next page
    after_next_url: str | None = None  # and the page after it


@dataclass(frozen=True)
class Rule:
    kind: str  # "threshold" | "top"
    value: float

    @property
    def name(self) -> str:
        return f"p >= {self.value:g}" if self.kind == "threshold" else f"top {self.value:g}"

    def pushed(self, probs: dict[str, float]) -> list[str]:
        ranked = sorted(probs, key=lambda u: probs[u], reverse=True)
        if self.kind == "top":
            return ranked[: int(self.value)]
        return [u for u in ranked if probs[u] >= self.value]


def evaluate(views: list[View], rule: Rule, size_of, reps: int = 1000, seed: int = 0) -> dict:
    rows = []
    for v in views:
        pushed = rule.pushed(v.probs)
        hit = v.next_url in pushed
        nbytes = sum(size_of(u) for u in pushed)
        second = None
        if v.after_next_url is not None and v.next_probs is not None:
            second = hit and v.after_next_url in rule.pushed(v.next_probs)
        rows.append(
            (v.session, hit, len(pushed), nbytes, size_of(v.next_url) if hit else 0, second)
        )

    def share(sample, i):
        vals = [r[i] for r in sample if r[i] is not None]
        return sum(vals) / len(vals) if vals else 0.0

    # Bootstrap over sessions, not views: a reader's views share topics and Jev's habits.
    by_session: dict[int, list] = {}
    for r in rows:
        by_session.setdefault(r[0], []).append(r)
    ids = sorted(by_session)
    rng = random.Random(seed)
    boots = sorted(
        share([r for s in rng.choices(ids, k=len(ids)) for r in by_session[s]], 1)
        for _ in range(reps)
    )
    pages = statistics.mean(r[2] for r in rows)
    total_bytes = sum(r[3] for r in rows)
    return {
        "rule": rule.name,
        "next_pushed": share(rows, 1),
        "ci95": [boots[int(0.025 * reps)], boots[int(0.975 * reps) - 1]],
        "pages": pages,
        "mb": total_bytes / len(rows) / 1e6,
        "useful_data": sum(r[4] for r in rows) / total_bytes if total_bytes else 0.0,
        "second_click_depth2": share(rows, 5),
        "depth2_pages_est": pages * pages,
    }


def ceiling(views: list[View]) -> float:
    return sum(v.next_url in v.probs for v in views) / len(views)


def to_markdown(results: list[dict], views: list[View], current: dict[str, str]) -> str:
    lines = [
        (
            f"{len(views)} page views from {len({v.session for v in views})} sessions. "
            f"Next page among the links Jev saw (ceiling): {ceiling(views):.0%}."
        ),
        "",
        (
            "| push rule | next page pushed (95% CI) | pages | MB | useful data "
            "| 2nd click too (depth 2) | depth-2 pages (est.) |"
        ),
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        mark = f" ← {current[r['rule']]}" if r["rule"] in current else ""
        lo, hi = r["ci95"]
        lines.append(
            f"| {r['rule']}{mark} | {r['next_pushed']:.0%} ({lo:.0%}–{hi:.0%}) | {r['pages']:.1f} "
            f"| {r['mb']:.2f} | {r['useful_data']:.0%} | {r['second_click_depth2']:.0%} "
            f"| {r['depth2_pages_est']:.0f} |"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ loading (needs the snapshot)


class PageSizes:
    """Bytes on the wire for pushing a page, exactly as the server frames it."""

    def __init__(self) -> None:
        self._sizes: dict[str, int] = {}
        self._fallback: int | None = None

    def __call__(self, url: str) -> int:
        if url not in self._sizes:
            title = wiki_title(url)
            path = page_path(title) if title else None
            if path is not None and path.exists():
                frame = Frame(MsgType.PUSH, {"url": url, "status": 200}, path.read_bytes())
                self._sizes[url] = len(frame.encode())
            else:
                # The origin serves stand-ins with real pages' sizes for articles outside the
                # snapshot; use the typical size seen so far.
                return self._fallback or 60_000
            self._fallback = int(statistics.median(self._sizes.values()))
        return self._sizes[url]


async def load_views(settings) -> list[View]:
    jev = make_predictor("jev", settings, offline=True)
    jev.replay_latency = False  # no need to wait out the recorded delay here
    views: list[View] = []
    missing = 0
    for session in load_sessions():
        history: list[str] = []
        answers: list[tuple[dict[str, float] | None, dict[str, str]]] = []
        for page in session["pages"]:
            html = page_path(page["title"]).read_bytes()
            state = build_page_state(
                page_url(page["title"]),
                html,
                history,
                settings.links.max_candidates,
                settings.links.same_origin_only,
            )
            try:
                probs = await jev.predict(state) if state.candidates else {}
            except Exception:  # noqa: BLE001 -- not cached: skip the view, report the count
                probs, missing = None, missing + 1
            urls = {link.target: link.url for link in state.candidates}
            answers.append((probs, urls))
            history = [*history, state.title][-HISTORY_LEN:]
        titles = [p["title"] for p in session["pages"]]
        for i in range(len(titles) - 1):
            probs, urls = answers[i]
            if probs is None:
                continue
            view = View(session["id"], probs, urls.get(titles[i + 1], page_url(titles[i + 1])))
            if i + 2 < len(titles) and answers[i + 1][0] is not None:
                next_probs, next_urls = answers[i + 1]
                view.next_probs = next_probs
                view.after_next_url = next_urls.get(titles[i + 2], page_url(titles[i + 2]))
            views.append(view)
    await jev.aclose()
    if missing:
        print(
            f"warning: {missing} page views had no cached Jev answer (run warm_jev)",
            file=sys.stderr,
        )
    return views


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", type=Path, default=None, help="default: settings.yaml")
    settings = load_settings(ap.parse_args().settings)
    missing = [
        title
        for session in load_sessions()
        for title in (p["title"] for p in session["pages"])
        if not page_path(title).exists()
    ]
    if missing:
        sys.exit(
            f"{len(missing)} session pages aren't in data/snapshot (e.g. {missing[0]}). "
            "Run this where the snapshot was built (the Linux PC), or rebuild it here: "
            "see 'Wikipedia snapshot' in NEXT_STEPS.md."
        )
    views = asyncio.run(load_views(settings))
    if not views:
        sys.exit("no page views with cached Jev answers: run `python -m experiments.warm_jev`")
    rules = [Rule("threshold", t) for t in THRESHOLDS] + [Rule("top", k) for k in TOP_K]
    sizes = PageSizes()
    results = [evaluate(views, rule, sizes) for rule in rules]
    p = settings.prefetch
    current = {
        Rule("threshold", p.handover_threshold).name: "outage now",
        Rule("threshold", p.threshold).name: "normal now",
    }
    print(to_markdown(results, views, current))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "views": len(views),
                "ceiling": ceiling(views),
                "settings": asdict(p),
                "results": results,
            },
            indent=1,
        )
        + "\n"
    )
    print(f"\nwritten to {OUT.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
