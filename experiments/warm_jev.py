"""Ask Jev ahead of time about every page view in the sessions (the emulation has no internet).

For page i of a session, the server proxy's question is built from that page and the titles of
the pages viewed before it, which is exactly what this script builds (same function), so the
server finds every answer in the cache during runs.

Two clicks deep: during a long predicted outage the server also asks about every page it would
push from the current one (prefetch.depth2_top_k). Any page view can be the one on screen when
the warning comes, so those questions are asked for every view, about the page the origin would
serve for each link (a stand-in for articles outside the snapshot, as in the runs). Stand-ins
depend on the whole snapshot, so run this on the machine that runs the emulation.

  python -m experiments.warm_jev
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

from data import build_snapshot
from data.build_snapshot import page_path
from data.origin_server import served_file, standin_files
from edgeproxy.common.protocol import HISTORY_LEN
from edgeproxy.common.settings import load_settings
from edgeproxy.server.prefetch_policy import LinkOutlook, PrefetchPolicy
from edgeproxy.server.proxy import build_page_state, child_state, depth2_parents, make_predictor
from experiments.harness import load_sessions, page_url


async def warm(settings, concurrency: int) -> None:
    jev = make_predictor("jev", settings, offline=False)
    sem = asyncio.Semaphore(concurrency)
    calls, cost = 0, 0.0

    async def ask(state):
        nonlocal calls, cost
        async with sem:
            probs = await jev.predict(state)
            if not jev.last_call.cached:
                calls += 1
                cost += jev.last_call.cost_usd
            return probs

    states = []
    for session in load_sessions():
        history: list[str] = []
        for page in session["pages"]:
            html = page_path(page["title"]).read_bytes()
            state = build_page_state(
                page_url(page["title"]),
                html,
                history,
                settings.links.max_candidates,
                settings.links.same_origin_only,
            )
            if state.candidates:
                states.append(state)
            history = [*history, state.title][-HISTORY_LEN:]
    answers = await asyncio.gather(*(ask(s) for s in states))
    print(f"{len(states)} page views: {calls} new Jev calls, ${cost:.4f}", file=sys.stderr)

    children = depth2_states(settings, states, answers)
    if children:
        calls, cost = 0, 0.0
        await asyncio.gather(*(ask(s) for s in children))
        print(
            f"{len(children)} two-clicks-deep questions: {calls} new Jev calls, ${cost:.4f}",
            file=sys.stderr,
        )
    await jev.aclose()


def depth2_states(settings, states, answers) -> list:
    """The server's two-clicks-deep questions, if a long outage warning came on each page view."""
    policy = PrefetchPolicy(settings.prefetch)
    outlook = LinkOutlook(
        handover_imminent=True, predicted_outage_s=settings.paths.expected_outage_s
    )
    if not policy.depth2_k(outlook):
        return []
    root = build_snapshot.SNAPSHOT_DIR
    standins = standin_files(root)
    children, seen = [], set()
    for state, probs in zip(states, answers, strict=True):
        for url in depth2_parents(policy, probs, outlook):
            path = served_file(root, urlsplit(url).path, standins)
            if path is None:
                continue
            child = child_state(
                url,
                path.read_bytes(),
                state,
                settings.links.max_candidates,
                settings.links.same_origin_only,
            )
            key = (child.url, tuple(child.history))
            if child.candidates and key not in seen:
                seen.add(key)
                children.append(child)
    return children


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--settings", type=Path, default=None, help="default: settings.yaml")
    args = ap.parse_args()
    asyncio.run(warm(load_settings(args.settings), args.concurrency))


if __name__ == "__main__":
    main()
