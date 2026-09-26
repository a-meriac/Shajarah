"""Offline: how early the dropout predictor warns, and how often it warns for nothing.

The predictor (client/handover_predictor.py) runs on synthetic cellular signal traces, as the
path manager feeds it (every 0.1 s), once per setting of its warning horizon. Each trace is two
minutes of signal with shadowing and measurement noise, plus one of:

- outage: the signal fades steadily for 3-15 s down to the usable threshold, then the link drops
  (as in the scenario files: entering a car park, an underpass, a lift);
- near miss: the same kind of fade, but it bottoms out 2-12 dB above the threshold and recovers;
- nothing: shadowing and noise only.

Shadowing (buildings and terrain passing by) changes slowly when walking and fast when driving,
so both are run: it decorrelates over ~20 s walking and ~3 s driving (about 40 m of movement).

Ground truth is the noise-free signal: the link is down while it is below the threshold. For
every outage (a crossing after 10+ s above) this reports the warning time, from the start of the
warning that was still on at the crossing (0 = not warned: the system only reacts). A false alarm
is a warning that switched off again without an outage in it. Its cost is small but real: an
early switch to another network (and back once it proves healthy), or ~0.7 MB of extra prefetch.

The traces are synthetic, so the numbers show the trade-off's shape, not field accuracy. Real
drive-test recordings are the next step.

  python -m experiments.warning_sweep           # writes results/warning_sweep.json
  python -m experiments.figures                 # figures/warning.pdf, with the others
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from dataclasses import dataclass, replace
from multiprocessing import Pool

from edgeproxy.client.handover_predictor import HandoverPredictor
from edgeproxy.common.settings import load_settings
from experiments.harness import ROOT

OUT = ROOT / "results" / "warning_sweep.json"
TICK_S = 0.1  # the path manager's tick
TRACE_S = 120.0
HORIZONS = [2, 4, 6, 8, 10, 12, 15]
KINDS = ("outage", "near_miss", "nothing")


@dataclass
class TraceModel:
    base_dbm: tuple[float, float] = (-100.0, -85.0)  # typical level, uniform in this range
    shadow_db: float = 4.0  # slow shadowing: standard deviation...
    shadow_s: float = 3.0  # ...and how long it takes to decorrelate
    noise_db: float = 2.0  # measurement noise on each reading
    report_s: float = 0.5  # the modem reports a new reading this often; held in between
    fade_s: tuple[float, float] = (3.0, 15.0)  # duration of a fade
    miss_margin_db: tuple[float, float] = (2.0, 12.0)  # near miss: lowest point above threshold


MOBILITY = {"walking": TraceModel(shadow_s=20.0), "driving": TraceModel(shadow_s=3.0)}


def make_trace(rng: random.Random, kind: str, m: TraceModel, threshold: float):
    """(clean, measured) signal, one value per tick. `clean` decides when the link is down."""
    n = int(TRACE_S / TICK_S)
    base = rng.uniform(*m.base_dbm)
    a = math.exp(-TICK_S / m.shadow_s)
    shadow, clean = rng.gauss(0, m.shadow_db), []
    start, fade = rng.uniform(30, 90), rng.uniform(*m.fade_s)
    bottom = threshold + rng.uniform(*m.miss_margin_db)  # near miss: lowest point
    for i in range(n):
        t = i * TICK_S
        shadow = a * shadow + math.sqrt(1 - a * a) * rng.gauss(0, m.shadow_db)
        level = base + shadow
        x = (t - start) / fade
        if kind == "outage" and x >= 1:
            level = threshold - 30  # the link drops
        elif kind == "outage" and x >= 0:
            level += x * (threshold - 2 - base)  # steady fade, reaching the threshold at the end
        elif kind == "near_miss" and x >= 0:
            depth = min(0.0, bottom - base)  # never pushes a low base down to the threshold
            level += depth * (x if x <= 1 else max(0.0, 2 - x))  # down, then back up
        clean.append(level)
    measured, held, next_report = [], 0.0, 0.0
    for i, level in enumerate(clean):
        if i * TICK_S >= next_report:
            held = level + rng.gauss(0, m.noise_db)
            next_report += m.report_s
        measured.append(held)
    return clean, measured


def evaluate(clean, measured, config) -> tuple[list[float], int]:
    """Warning times for every outage in the trace, and the number of false alarms."""
    pred = HandoverPredictor(config)
    threshold = config.threshold_dbm
    leads, false_alarms = [], 0
    warn_start: float | None = None
    had_outage = False  # in the current warning
    up_since = 0.0
    down = False
    for i, (c, v) in enumerate(zip(clean, measured, strict=True)):
        t = i * TICK_S
        on = pred.update(t, v) is not None
        if on and warn_start is None:
            warn_start, had_outage = t, False
        elif not on and warn_start is not None:
            false_alarms += not had_outage
            warn_start = None
        if not down and c < threshold:
            down = True
            if t - up_since >= 10:  # a new outage, not a flicker of the last one
                leads.append(t - warn_start if warn_start is not None else 0.0)
            had_outage = True
        elif down and c >= threshold:
            down, up_since = False, t
    return leads, false_alarms


def _run(job):
    mobility, horizon, seed, n, base_config = job
    config = replace(base_config, horizon_s=float(horizon))
    rng = random.Random(seed)  # the same traces for every horizon
    leads, false_alarms = [], 0
    for i in range(n):
        kind = KINDS[i % len(KINDS)]
        clean, measured = make_trace(rng, kind, MOBILITY[mobility], config.threshold_dbm)
        trace_leads, fa = evaluate(clean, measured, config)
        leads += trace_leads
        false_alarms += fa
    return mobility, horizon, leads, false_alarms


def sweep(traces: int, seed: int) -> dict:
    config = load_settings().handover
    jobs = [(m, h, seed, traces, config) for m in MOBILITY for h in HORIZONS]
    with Pool() as pool:
        results = pool.map(_run, jobs)
    hours = traces * TRACE_S / 3600
    rows = []
    for mobility, horizon, leads, false_alarms in results:
        rows.append({
            "mobility": mobility,
            "horizon_s": horizon,
            "outages": len(leads),
            "warned_pct": round(100 * sum(x > 0 for x in leads) / len(leads), 1),
            "warned_2s_pct": round(100 * sum(x >= 2 for x in leads) / len(leads), 1),
            "median_warning_s": round(statistics.median(leads), 2),
            "false_alarms_per_hour": round(false_alarms / hours, 1),
        })  # fmt: skip
    return {
        "traces": traces,
        "hours": round(hours, 2),
        "seed": seed,
        "current_horizon_s": config.horizon_s,
        "models": {m: vars(model) for m, model in MOBILITY.items()},
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", type=int, default=600, help="per setting (a third of each kind)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    result = sweep(args.traces, args.seed)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, indent=1))
    print(f"{result['traces']} traces, {result['hours']} h of signal per setting")
    print(f"{'mobility':8} {'horizon s':>9} {'outages':>7} {'warned':>7} {'2+ s early':>10} "
          f"{'median warning s':>16} {'false alarms/h':>14}")  # fmt: skip
    for r in result["rows"]:
        print(f"{r['mobility']:8} {r['horizon_s']:9} {r['outages']:7} {r['warned_pct']:6.1f}% "
              f"{r['warned_2s_pct']:9.1f}% {r['median_warning_s']:16.2f} "
              f"{r['false_alarms_per_hour']:14.1f}")  # fmt: skip
    print(f"-> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
