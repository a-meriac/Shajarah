"""Ask Jev ahead of time about every page view in the sessions (the emulation has no internet).

For page i of a session, the server proxy's question is built from that page and the titles of
the pages viewed before it, which is exactly what this script builds (same function), so the
server finds every answer in the cache during runs.

  python -m experiments.warm_jev
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from data.build_snapshot import page_path
from edgeproxy.common.protocol import HISTORY_LEN
from edgeproxy.common.settings import load_settings
from edgeproxy.server.proxy import build_page_state, make_predictor
from experiments.harness import load_sessions, page_url


async def warm(settings, concurrency: int) -> None:
    jev = make_predictor("jev", settings)
    sem = asyncio.Semaphore(concurrency)
    calls, cost = 0, 0.0

    async def ask(state):
        nonlocal calls, cost
        async with sem:
            await jev.predict(state)
            if not jev.last_call.cached:
                calls += 1
                cost += jev.last_call.cost_usd

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
    await asyncio.gather(*(ask(s) for s in states))
    await jev.aclose()
    print(f"{len(states)} page views, {calls} new Jev calls, ${cost:.4f}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--settings", type=Path, default=None, help="default: settings.yaml")
    args = ap.parse_args()
    asyncio.run(warm(load_settings(args.settings), args.concurrency))


if __name__ == "__main__":
    main()
