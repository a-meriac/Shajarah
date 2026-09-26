"""Turn a batch's event logs into per-run metrics and a config × scenario summary.

Per run (results/<batch>/<config>/<scenario>/s<session>/):
- wait_s: total time the reader spent waiting for pages; a page still loading when the run
  ended counts until the end. max_wait_s: the longest single wait.
- outage_wait_s: waiting for pages clicked during the outage window (first link death to 10 s
  after the last recovery), which is where the configs differ.
- pages: pages the reader got to see; outage_pages / outage_cached: pages opened in the outage
  window, and how many of those came from the cache (push or stale copy).
- pushed_kb / wasted_kb: bytes pushed over the tunnel (compressed, as sent), and the part never
  opened by the reader.

  python -m experiments.analyze main          # writes results/main/runs.csv, prints the table
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

from experiments.harness import CONFIGS, ROOT

AFTER_RECOVERY_S = 10.0
SUSPEND_SLACK_S = 5.0  # wall clock running this far ahead of the monotonic clock = machine slept
NETEM = re.compile(r"t=([\d.]+) (\w+) -> (\w+)")


def _events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def outage_window(client: list[dict], end: float) -> tuple[float, float] | None:
    changes = [NETEM.match(e["msg"]) for e in client if e["event"] == "netem"]
    changes = [(float(m[1]), m[3]) for m in changes if m]
    deaths = [t for t, profile in changes if profile == "dead"]
    if not deaths:
        return None
    recoveries = [t for t, profile in changes if profile != "dead" and t > deaths[0]]
    return deaths[0], (max(recoveries) if recoveries else end) + AFTER_RECOVERY_S


def slept(events: list[dict]) -> bool:
    """True if the machine was suspended during the run: the monotonic clock stops while
    suspended and the wall clock doesn't, so the two drift apart. Such a run is meaningless."""
    if len(events) < 2:
        return False
    wall = events[-1]["ts"] - events[0]["ts"]
    mono = events[-1]["mono"] - events[0]["mono"]
    return wall - mono > SUSPEND_SLACK_S


def run_metrics(run_dir: Path) -> dict | None:
    meta_file = run_dir / "meta.json"
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text())
    client, server = _events(run_dir / "client.jsonl"), _events(run_dir / "server.jsonl")
    if not meta.get("done") or not client:
        return None
    end = next((e["t"] for e in client if e["event"] == "run_end"), None)
    if end is None:
        return None
    window = outage_window(client, end)

    views = {e["i"]: e for e in client if e["event"] == "view"}
    waits = []  # (click time, wait, source)
    for click in (e for e in client if e["event"] == "click"):
        view = views.get(click["i"])
        if view is not None:
            waits.append((click["t"], view["wait_s"], view["source"]))
        else:  # still waiting when the run ended
            waits.append((click["t"], end - click["t"], "pending"))
    in_outage = [w for w in waits if window and window[0] <= w[0] < window[1]]

    pushes = {e["url"]: e["wire_bytes"] for e in server if e["event"] == "push"}
    opened = {views[i]["url"] for i in views if views[i]["source"] == "push"}
    return {
        "config": meta["config"],
        "scenario": meta["scenario"],
        "session": meta["session"],
        "wait_s": round(sum(w for _, w, _ in waits), 3),
        "max_wait_s": round(max((w for _, w, _ in waits), default=0.0), 3),
        "outage_wait_s": round(sum(w for _, w, _ in in_outage), 3),
        "pages": sum(1 for _, _, s in waits if s not in ("pending", "error")),
        "outage_pages": len(in_outage),
        "outage_cached": sum(1 for _, _, s in in_outage if s in ("push", "stale")),
        "switches": sum(1 for e in client if e["event"] == "path_switch"),
        "pushed_kb": round(sum(pushes.values()) / 1000, 1),
        "wasted_kb": round(sum(b for u, b in pushes.items() if u not in opened) / 1000, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("batch", help="results/<batch>/")
    args = ap.parse_args()
    batch = ROOT / "results" / args.batch
    runs = sorted(batch.glob("*/*/s*"))
    asleep = [d for d in runs if slept(_events(d / "client.jsonl"))]
    for d in asleep:
        print(f"skipped {d.relative_to(batch)}: the machine slept during this run; delete it "
              "and run the batch again to redo it")  # fmt: skip
    rows = [m for d in runs if d not in asleep and (m := run_metrics(d))]
    if not rows:
        raise SystemExit(f"no finished runs in {batch}")
    with (batch / "runs.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["scenario"], r["config"])].append(r)
    cols = ["wait_s", "outage_wait_s", "max_wait_s", "pages", "outage_cached", "pushed_kb",
            "wasted_kb"]  # fmt: skip
    print(f"{len(rows)} runs; medians per config (n = sessions)\n")
    for scenario in sorted({s for s, _ in groups}):
        print(scenario)
        print(f"  {'config':28} {'n':>2} " + " ".join(f"{c:>13}" for c in cols))
        for name in CONFIGS:
            group = groups.get((scenario, name))
            if not group:
                continue
            label = f"{name} {CONFIGS[name].about}"[:28]
            vals = " ".join(f"{statistics.median(r[c] for r in group):13.1f}" for c in cols)
            print(f"  {label:28} {len(group):2} {vals}")
        print()
    print(f"per-run rows: {batch / 'runs.csv'}")


if __name__ == "__main__":
    main()
