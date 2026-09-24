"""Generate browsing sessions for the system experiments, from the session-month clickstream.

A session starts on a seed page (cycling through the popularity buckets) and then follows real
clicks: at each step the next page is drawn from the links actually on the current page, in
proportion to how often readers clicked them in the session month. Reading time per page is
log-normal. A session ends early if nobody clicked any link on its current page.

Pages a session reaches that aren't in the snapshot yet are fetched into it. Everything is
written to data/sessions.json (committed), so every config replays the same sessions.

  python -m data.sessions
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from data.build_snapshot import fetch_all, page_path
from data.clickstream import dump_path, transitions
from edgeproxy.common.settings import load_settings
from edgeproxy.server.links import extract_links

HERE = Path(__file__).parent
SESSIONS = HERE / "sessions.json"
MANIFEST = HERE / "snapshot_manifest.json"


def linked_targets(title: str) -> set[str]:
    html = page_path(title).read_text(encoding="utf-8")
    links = extract_links(html, f"http://origin.local/wiki/{quote(title)}", max_candidates=10**6)
    return {link.target for link in links}


def dwell(rng: random.Random, cfg) -> float:
    s = rng.lognormvariate(math.log(cfg.dwell_median_s), cfg.dwell_sigma)
    return round(min(max(s, cfg.dwell_min_s), cfg.dwell_max_s), 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", type=Path, default=None, help="default: settings.yaml")
    settings = load_settings(ap.parse_args().settings)
    cfg, month = settings.sessions, settings.snapshot.session_month
    rng = random.Random(cfg.seed)

    seeds = json.loads(MANIFEST.read_text(encoding="utf-8"))["pages"]
    by_bucket: dict[str, list[str]] = {}
    for p in seeds:
        if p["role"] == "seed" and "error" not in p:
            by_bucket.setdefault(p["bucket"], []).append(p["title"])
    buckets = list(by_bucket)
    sessions = []
    for i in range(cfg.count):
        bucket = buckets[i % len(buckets)]
        start = rng.choice(by_bucket[bucket])
        sessions.append({"id": i, "bucket": bucket, "pages": [{"title": start}], "open": True})

    fetched: list[dict] = []
    for step in range(1, cfg.pages):
        open_sessions = [s for s in sessions if s["open"]]
        if not open_sessions:
            break
        frontier = {s["pages"][-1]["title"] for s in open_sessions}
        print(f"step {step}: {len(open_sessions)} sessions, reading {month}", file=sys.stderr)
        clicks = transitions(dump_path(month), frontier)
        new_pages = []
        for s in open_sessions:
            here = s["pages"][-1]["title"]
            on_page = linked_targets(here)
            options = [(t, n) for t, n in clicks[here].items() if t in on_page]
            if not options:
                s["open"] = False
                continue
            targets, weights = zip(*sorted(options))
            nxt = rng.choices(targets, weights)[0]
            s["pages"].append({"title": nxt})
            if not page_path(nxt).exists():
                new_pages.append({"title": nxt, "role": "session"})
        if new_pages:
            unique = list({p["title"]: p for p in new_pages}.values())
            print(f"  fetching {len(unique)} new pages", file=sys.stderr)
            asyncio.run(fetch_all(unique, settings.snapshot.concurrency))
            fetched += unique
            failed = {p["title"] for p in unique if "error" in p}
            for s in sessions:  # a page that couldn't be fetched ends its session there
                if s["pages"][-1]["title"] in failed:
                    s["pages"].pop()
                    s["open"] = False

    for s in sessions:
        del s["open"]
        for page in s["pages"]:
            page["dwell_s"] = dwell(rng, cfg)
    SESSIONS.write_text(
        json.dumps(
            {"settings": asdict(cfg), "month": month, "sessions": sessions, "fetched": fetched},
            indent=1,
            ensure_ascii=False,
        )
        + "\n"
    )
    lengths = sorted(len(s["pages"]) for s in sessions)
    print(
        f"{len(sessions)} sessions, pages per session median {lengths[len(lengths) // 2]}, "
        f"min {lengths[0]}; {len(fetched)} pages fetched",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
