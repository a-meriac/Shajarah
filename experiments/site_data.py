"""The numbers behind the website's results and the replay page's summary charts.

Writes site/replays/summary.json from the recorded runs:
- per run of the outage test (batch tunnel20): recovery time (the first answered tunnel ping sent
  after the link came back, minus the moment it came back), total waiting, clicks during the
  outage and how many opened from the phone, and data: page bytes as the origin sent them and as
  they crossed the tunnel (compressed), plus prefetched bytes and the part never opened;
- page load times away from any drop, per setup and link (Wi-Fi from the walk in batch main, 5G
  from the walk after the switch and from the outage test before and after the drop);
- compression over the whole Wikipedia snapshot.

A page's size on the wire is its snapshot HTML deflated as the tunnel does it; the few pages
served as stand-ins (outside the snapshot) count at their full size. Pushes log their wire size.

  python -m experiments.site_data
"""

from __future__ import annotations

import json
import statistics
import zlib
from pathlib import Path

from edgeproxy.common.protocol import COMPRESS_LEVEL
from experiments.analyze import NETEM, _events, run_metrics
from experiments.harness import ROOT

OUT = ROOT / "site" / "replays" / "summary.json"
SNAPSHOT = ROOT / "data" / "snapshot" / "wiki"
RESULTS = ROOT / "results"
OUTAGE = ("tunnel20", "car_tunnel_45s", ("1", "2", "3", "5"))
WALK = ("main", "wifi_to_5g_walk")
WALK_WIFI_DIES_S = 28.5
CLEAR_OF_DROP_S = (36.0, 95.0)  # outage test: page loads overlapping this are left out
_wire: dict[str, int | None] = {}


def wire_bytes(url: str) -> int | None:
    title = url.split("/wiki/", 1)[-1]
    if title not in _wire:
        path = SNAPSHOT / f"{title}.html"
        _wire[title] = (
            len(zlib.compress(path.read_bytes(), COMPRESS_LEVEL)) if path.exists() else None
        )
    return _wire[title]


def link_back(client: list[dict]) -> float | None:
    """When the dead link came back, from the netem log."""
    changes = [
        (float(m[1]), m[3])
        for e in client
        if e["event"] == "netem" and (m := NETEM.match(e["msg"]))
    ]
    deaths = [t for t, p in changes if p == "dead"]
    back = [t for t, p in changes if deaths and p != "dead" and t > deaths[0]]
    return back[-1] if back else None


def outage_run(run_dir: Path) -> dict | None:
    metrics = run_metrics(run_dir)
    if metrics is None:
        return None
    client, server = _events(run_dir / "client.jsonl"), _events(run_dir / "server.jsonl")
    back = link_back(client)
    answered = [e["t"] + e["rtt_ms"] / 1000 for e in client
                if e["event"] == "ping" and e["rtt_ms"] is not None and back is not None and e["t"] >= back]  # fmt: skip
    views = [
        e for e in client if e["event"] == "view" and e["source"] == "origin" and e["status"] == 200
    ]
    raw = sum(e["bytes"] for e in views)
    sent = sum(w if (w := wire_bytes(e["url"])) is not None else e["bytes"] for e in views)
    pushes = {e["url"]: e for e in server if e["event"] == "push"}
    config, session = run_dir.parts[-3], int(run_dir.name[1:])
    return {
        "config": config,
        "session": session,
        "file": f"tunnel20/car_tunnel_45s/s{session}/{config}.json",
        "recovery_s": round(min(answered) - back, 2) if answered else None,
        "wait_s": metrics["wait_s"],
        "outage_pages": metrics["outage_pages"],
        "outage_cached": metrics["outage_cached"],
        "pages_kb": round(raw / 1000, 1),
        "pages_sent_kb": round(sent / 1000, 1),
        "pushed_kb": round(sum(e["bytes"] for e in pushes.values()) / 1000, 1),
        "pushed_sent_kb": metrics["pushed_kb"],
        "wasted_sent_kb": metrics["wasted_kb"],
    }


def page_loads() -> dict:
    """Page load times (ms) away from any drop, per setup ("1", "2") and link."""
    out = {c: {"wifi": [], "5g": []} for c in ("1", "2")}
    for config in out:
        for f in (RESULTS / WALK[0] / config / WALK[1]).glob("s*/client.jsonl"):
            for e in _events(f):
                if e["event"] == "view" and e["source"] == "origin" and e["status"] == 200:
                    if e["t"] + e["wait_s"] < WALK_WIFI_DIES_S - 0.5:
                        out[config]["wifi"].append(round(e["wait_s"] * 1000))
                    elif e["t"] > WALK_WIFI_DIES_S + 3.5:
                        out[config]["5g"].append(round(e["wait_s"] * 1000))
        for f in (RESULTS / OUTAGE[0] / config / OUTAGE[1]).glob("s*/client.jsonl"):
            for e in _events(f):
                if e["event"] == "view" and e["source"] == "origin" and e["status"] == 200:
                    a, b = e["t"], e["t"] + e["wait_s"]
                    if b < CLEAR_OF_DROP_S[0] or a > CLEAR_OF_DROP_S[1]:
                        out[config]["5g"].append(round(e["wait_s"] * 1000))
    return out


def compression() -> dict:
    ratios, raw, sent = [], 0, 0
    for path in SNAPSHOT.glob("*.html"):
        body = path.read_bytes()
        packed = len(zlib.compress(body, COMPRESS_LEVEL))
        ratios.append(len(body) / packed)
        raw, sent = raw + len(body), sent + packed
    ratios.sort()
    q = lambda p: round(ratios[round(p * (len(ratios) - 1))], 2)
    return {
        "pages": len(ratios),
        "median": round(statistics.median(ratios), 2),
        "q1": q(0.25),
        "q3": q(0.75),
        "raw_mb": round(raw / 1e6),
        "sent_mb": round(sent / 1e6),
    }


def main() -> None:
    batch, scenario, configs = OUTAGE
    runs = []
    for config in configs:
        for run_dir in sorted((RESULTS / batch / config / scenario).glob("s*")):
            if (r := outage_run(run_dir)) is not None:
                runs.append(r)
    runs.sort(key=lambda r: (r["session"], r["config"]))
    summary = {"runs": runs, "page_loads": page_loads(), "compression": compression()}
    OUT.write_text(json.dumps(summary, separators=(",", ":")) + "\n")
    for config in configs:
        mine = [r for r in runs if r["config"] == config]
        rec = [r["recovery_s"] for r in mine if r["recovery_s"] is not None]
        print(f"setup {config}: {len(mine)} runs, recovery median {statistics.median(rec):.2f} s, "
              f"sent {sum(r['pages_sent_kb'] + r['pushed_sent_kb'] for r in mine) / 1000:.1f} MB "
              f"of {sum(r['pages_kb'] + r['pushed_kb'] for r in mine) / 1000:.1f} MB")  # fmt: skip
    print(f"-> {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1000:.0f} KB)")


if __name__ == "__main__":
    main()
