"""Export finished runs for the replay viewer (site/replay.html) on GitHub Pages.

Each run becomes one compact JSON file with everything on the scenario's clock (seconds from
t=0): the network zones the reader moves through, each interface's signal, which interface the
tunnel used, dropout warnings, ping latency, every page the reader opened (and where it came
from), and every page the server pushed. site/replays/index.json lists the exported runs with
their summary metrics (the same ones experiments/analyze.py reports).

Server events are put on the client's clock through the shared monotonic clock (client and
server run on one machine, in different network namespaces).

  python -m experiments.export_replay main                        # every finished run
  python -m experiments.export_replay main --scenarios car_tunnel_45s --sessions 0 1 2
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

from edgeproxy.client.signal_monitor import TraceSignal
from edgeproxy.common.settings import load_settings
from experiments.analyze import _events, outage_window, run_metrics
from experiments.harness import CONFIGS, INTERFACE_IPS, ROOT, SCENARIOS

OUT = ROOT / "site" / "replays"
NO_SIGNAL_DBM = -140.0
SIGNAL_STEP_S = 0.5  # resolution when rebuilding signal from the scenario (older runs)


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def page_title(url: str) -> str:
    path = urlsplit(url).path
    return unquote(path.split("/wiki/", 1)[-1]).replace("_", " ") if "/wiki/" in path else url


def zones(scenario: dict, end: float) -> dict[str, list]:
    """Per interface: [start, end, profile] segments of the scenario's link conditions."""
    state = {i: scenario["initial"].get(i, "dead") for i in INTERFACE_IPS}
    since = dict.fromkeys(state, 0.0)
    out: dict[str, list] = {i: [] for i in state}
    for ev in sorted(scenario.get("events", []), key=lambda e: e["t"]):
        i = ev["iface"]
        out[i].append([since[i], ev["t"], state[i]])
        state[i], since[i] = ev["profile"], ev["t"]
    for i, profile in state.items():
        out[i].append([since[i], end, profile])
    return out


def clock_zero(client: list[dict]) -> float:
    """The monotonic time of scenario t=0."""
    for e in client:
        if e["event"] == "clock":
            return e["mono"]
    # Runs from before the clock event: a click is logged right when it happens.
    offsets = [e["mono"] - e["t"] for e in client if e["event"] == "click"]
    if offsets:
        return statistics.median(offsets)
    return next(e["mono"] for e in client if e["event"] == "run_start")


def thresholds(meta: dict) -> dict[str, float]:
    settings = load_settings(None)
    wifi, cellular = settings.paths.wifi_threshold_dbm, settings.handover.threshold_dbm
    try:  # the settings the run actually used, if recorded
        recorded = yaml.safe_load(meta.get("settings") or "") or {}
        wifi = recorded.get("paths", {}).get("wifi_threshold_dbm", wifi)
        cellular = recorded.get("handover", {}).get("threshold_dbm", cellular)
    except yaml.YAMLError:
        pass
    return {i: (wifi if i.startswith("wifi") else cellular) for i in INTERFACE_IPS}


def export_run(run_dir: Path) -> dict | None:
    metrics = run_metrics(run_dir)
    if metrics is None:
        return None
    meta = json.loads((run_dir / "meta.json").read_text())
    client, server = _events(run_dir / "client.jsonl"), _events(run_dir / "server.jsonl")
    scenario = yaml.safe_load((SCENARIOS / f"{meta['scenario']}.yaml").read_text())
    end = next(e["t"] for e in client if e["event"] == "run_end")
    t0 = clock_zero(client)

    logged = [e for e in client if e["event"] == "signal"]
    if logged:
        signal = {
            "t": [_r(e["t"]) for e in logged],
            "dbm": {i: [e["dbm"].get(i) for e in logged] for i in INTERFACE_IPS},
        }
    else:  # older runs: rebuild from the scenario's trace, which is what the client read
        traces = scenario.get("signal", {})
        ts = [k * SIGNAL_STEP_S for k in range(int(end / SIGNAL_STEP_S) + 1)]
        sig = {
            i: TraceSignal(traces.get(i, [{"t": 0, "dbm": NO_SIGNAL_DBM}])) for i in INTERFACE_IPS
        }
        signal = {"t": ts, "dbm": {i: [_r(sig[i].sample(t), 1) for t in ts] for i in sig}}

    start_iface = next(e["active"] for e in client if e["event"] == "run_start")
    switches = [
        {"t": _r(e["t"]), "from": e["frm"], "to": e["to"], "reason": e["reason"]}
        for e in client
        if e["event"] == "path_switch"
    ]

    views = {e["i"]: e for e in client if e["event"] == "view"}
    pages = []
    for click in (e for e in client if e["event"] == "click"):
        view = views.get(click["i"])
        pages.append(
            {
                "t": _r(click["t"]),
                "title": page_title(click["url"]),
                "wait": _r(view["wait_s"] if view else end - click["t"], 3),
                "source": view["source"] if view else "pending",
                "kb": _r(view["bytes"] / 1000, 1) if view else None,
            }
        )
    opened = {views[i]["url"] for i in views if views[i]["source"] == "push"}

    pushes = [
        {
            "t": _r(e["mono"] - t0),
            "title": page_title(e["url"]),
            "kb": _r(e["wire_bytes"] / 1000, 1),
            "depth": e.get("depth", 1),
            "prob": e.get("prob"),
            "opened": e["url"] in opened,
        }
        for e in server
        if e["event"] == "push"
    ]
    window = outage_window(client, end)
    return {
        "config": meta["config"],
        "about": meta.get("about") or CONFIGS[meta["config"]].about,
        "scenario": meta["scenario"],
        "session": meta["session"],
        "duration": _r(end),
        "code": meta.get("code"),
        "interfaces": list(INTERFACE_IPS),
        "thresholds": thresholds(meta),
        "zones": zones(scenario, end),
        "outage": [_r(window[0]), _r(min(window[1], end))] if window else None,
        "signal": signal,
        "start_iface": start_iface,
        "switches": switches,
        "warnings": [
            {"t": _r(e["t"]), "iface": e["iface"], "on": e["on"]}
            for e in client
            if e["event"] == "warning"
        ],
        "hints": [
            {"t": _r(e["t"]), "on": e["active"]} for e in client if e["event"] == "hint_sent"
        ],
        "pings": [
            [_r(e["t"]), _r(e["rtt_ms"], 1), e["iface"]] for e in client if e["event"] == "ping"
        ],
        "pages": pages,
        "pushes": pushes,
        "metrics": metrics,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("batch", help="results/<batch>/")
    ap.add_argument("--configs", nargs="*", default=None)
    ap.add_argument("--scenarios", nargs="*", default=None)
    ap.add_argument("--sessions", nargs="*", type=int, default=None)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    batch_dir = ROOT / "results" / args.batch
    runs = []
    for run_dir in sorted(batch_dir.glob("*/*/s*")):
        config, scenario, session = run_dir.parts[-3], run_dir.parts[-2], int(run_dir.name[1:])
        if (
            (args.configs and config not in args.configs)
            or (args.scenarios and scenario not in args.scenarios)
            or (args.sessions is not None and session not in args.sessions)
        ):
            continue
        replay = export_run(run_dir)
        if replay is None:
            continue
        rel = Path(args.batch) / scenario / f"s{session}" / f"{config}.json"
        path = args.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(replay, separators=(",", ":")))
        runs.append(
            {
                "batch": args.batch,
                "config": config,
                "about": replay["about"],
                "scenario": scenario,
                "session": session,
                "file": rel.as_posix(),
                "metrics": replay["metrics"],
            }
        )
    if not runs:
        sys.exit(f"no finished runs matched in {batch_dir}")
    index_file = args.out / "index.json"
    index = json.loads(index_file.read_text()) if index_file.exists() else {"runs": []}
    fresh = {r["file"] for r in runs}
    index["runs"] = [r for r in index["runs"] if r["file"] not in fresh] + runs
    index["runs"].sort(key=lambda r: (r["batch"], r["scenario"], r["session"], r["config"]))
    index_file.write_text(json.dumps(index, indent=1) + "\n")
    size = sum((args.out / r["file"]).stat().st_size for r in runs)
    print(f"{len(runs)} runs exported to {args.out} ({size / 1e6:.1f} MB)", file=sys.stderr)
    export_voip(args.out)


def export_voip(out: Path) -> None:
    """Voice-call probe results (experiments/voip_probe.py), for the demo's live-call panel."""
    runs = [json.loads(p.read_text()) for p in sorted((ROOT / "results" / "voip").glob("*.json"))]
    if not runs:
        return
    keep = ("scenario", "mode", "lost_pct", "longest_silence_s", "rtt_ms_median", "switches")
    (out / "voip.json").write_text(
        json.dumps({"runs": [{k: r[k] for k in keep} for r in runs]}, separators=(",", ":"))
    )
    print(f"{len(runs)} voice-call runs exported to {out / 'voip.json'}", file=sys.stderr)


if __name__ == "__main__":
    main()
