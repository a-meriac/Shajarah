"""Paper figures from the result files (PDF for the paper, PNG to preview), into figures/.

- predictors: hit@k for Jev and the two reference baselines, and hit@3 by page popularity
  (results/predictor_eval/{jev,position,history}.json)
- calibration: Jev's probability vs the real share of clicks, per bin (same files)
- cutoff: next page already pushed vs data per dropout warning (results/cutoff_sweep.json)
- warning: outages warned 2+ s ahead vs false alarms per hour, per warning horizon
  (results/warning_sweep.json)
- waiting, data: per-reader waiting and pushed/wasted data per config for one scenario of a
  system batch (results/<batch>/runs.csv)

  python -m experiments.figures --batch tunnel20
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, PercentFormatter

from experiments.harness import CONFIGS, ROOT

RESULTS = ROOT / "results"
OUT = ROOT / "figures"

# Reference categorical slots 1-3 (validated all-pairs, light mode); aqua is below 3:1 on white,
# so every aqua series also carries a direct text label.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK_2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
PREDICTORS = [("jev", "Jev", BLUE), ("position", "Position (no data)", ORANGE),
              ("history", "Past clicks (site logs)", AQUA)]  # fmt: skip
BUCKETS = [("head", "Popular"), ("torso", "Medium"), ("tail", "Obscure")]
SINGLE, WIDE = (3.4, 2.5), (7.0, 2.6)  # inches: one column, full width


def style() -> None:
    plt.rcParams.update({
        "font.size": 8, "axes.titlesize": 8.5, "axes.labelsize": 8, "xtick.labelsize": 7.5,
        "ytick.labelsize": 7.5, "legend.fontsize": 7.5, "axes.edgecolor": MUTED,
        "axes.labelcolor": INK_2, "xtick.color": INK_2, "ytick.color": INK_2, "text.color": INK,
        "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
        "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
        "lines.linewidth": 1.5, "lines.markersize": 5, "legend.frameon": False,
        "savefig.bbox": "tight", "savefig.dpi": 200, "pdf.fonttype": 42,
    })  # fmt: skip


def save(fig, name: str) -> None:
    OUT.mkdir(exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(OUT / f"{name}.{ext}")
    plt.close(fig)
    print(f"figures/{name}.pdf")


def _eval(name: str) -> dict | None:
    path = RESULTS / "predictor_eval" / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def fig_predictors() -> None:
    evals = [(label, color, e) for key, label, color in PREDICTORS if (e := _eval(key))]
    if not evals:
        return
    ks = [1, 3, 5, 10]
    fig, (left, right) = plt.subplots(1, 2, figsize=WIDE, gridspec_kw={"wspace": 0.35})
    for label, color, e in evals:
        ys = [e["summary"]["all"][f"hit@{k}"] for k in ks]
        left.plot(ks, ys, color=color, marker="o", label=label)
        left.annotate(f"{ys[-1]:.0%}", (ks[-1], ys[-1]), xytext=(4, 0),
                      textcoords="offset points", va="center", color=INK_2, fontsize=7)  # fmt: skip
    left.set_xticks(ks)
    left.set_xlabel("links pushed (top k)")
    left.set_ylabel("real clicks on those links")
    left.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    left.set_ylim(0, None)
    left.set_title("Share of real clicks caught, all 1,002 pages", loc="left")
    left.legend(loc="upper left")

    width = 0.8 / len(evals)
    for i, (label, color, e) in enumerate(evals):
        xs = [b + (i - (len(evals) - 1) / 2) * width for b in range(len(BUCKETS))]
        ys = [e["summary"][key]["hit@3"] for key, _ in BUCKETS]
        right.bar(xs, ys, width * 0.92, color=color, label=label, edgecolor="white", lw=0.8)
        for x, y in zip(xs, ys, strict=True):
            right.annotate(f"{y:.0%}", (x, y), xytext=(0, 2), textcoords="offset points",
                           ha="center", va="bottom", color=INK_2, fontsize=6.5)  # fmt: skip
    right.set_xticks(range(len(BUCKETS)), [name for _, name in BUCKETS])
    right.set_xlabel("page popularity")
    right.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    right.set_title("Top 3 links, by page popularity", loc="left")
    right.grid(axis="x", visible=False)
    save(fig, "predictors")


def fig_calibration() -> None:
    evals = [(label, color, e) for key, label, color in PREDICTORS if (e := _eval(key))]
    if not evals:
        return
    fig, ax = plt.subplots(figsize=SINGLE)
    ax.plot([0, 1], [0, 1], color=MUTED, lw=1, ls=(0, (3, 3)))
    ax.annotate("perfectly calibrated", (0.74, 0.74), xytext=(5, -5), textcoords="offset points",
                rotation=45, color=MUTED, fontsize=6.5, ha="center", va="top",
                rotation_mode="anchor",
                transform_rotates_text=True)  # fmt: skip
    top = 0
    for label, color, e in evals:
        bins = [b for b in e["summary"]["all"]["calibration"] if b["n"] >= 20]
        xs, ys = [b["predicted"] for b in bins], [b["real"] for b in bins]
        ax.plot(xs, ys, color=color, marker="o", label=label)
        top = max(top, *xs, *ys)
    lim = min(1.0, top * 1.1)
    ax.set_xlim(0, lim)
    ax.set_ylim(0, lim)
    ax.set_aspect("equal")
    ax.set_xlabel("probability the predictor gave")
    ax.set_ylabel("real share of clicks")
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax.set_title("Calibration of each page's top 10 links", loc="left")
    ax.legend(loc="upper left")
    save(fig, "calibration")


def fig_cutoff() -> None:
    path = RESULTS / "cutoff_sweep.json"
    if not path.exists():
        return
    sweep = json.loads(path.read_text())["results"]
    by_p = [r for r in sweep if r["rule"].startswith("p >=")]
    by_top = [r for r in sweep if r["rule"].startswith("top")]
    fig, ax = plt.subplots(figsize=SINGLE)
    for rows, color, label in ((by_p, BLUE, "Jev probability ≥ cutoff"),
                               (by_top, ORANGE, "Jev's top k links")):  # fmt: skip
        rows = sorted(rows, key=lambda r: r["mb"])
        xs, ys = [r["mb"] for r in rows], [r["next_pushed"] for r in rows]
        lo = [r["next_pushed"] - r["ci95"][0] for r in rows]
        hi = [r["ci95"][1] - r["next_pushed"] for r in rows]
        ax.errorbar(xs, ys, yerr=[lo, hi], color=color, marker="o", capsize=0, elinewidth=0.8,
                    label=label)  # fmt: skip
    for r in by_p:
        cutoff = r["rule"].split(">=")[1].strip()
        if cutoff in ("0.25", "0.03", "0.01", "0"):
            ax.annotate(f"p ≥ {cutoff}", (r["mb"], r["next_pushed"]), xytext=(5, -8),
                        textcoords="offset points", color=INK_2, fontsize=6.5)  # fmt: skip
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.set_xlabel("data pushed per dropout warning (MB, log scale)")
    ax.set_ylabel("next page already pushed")
    ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax.set_ylim(0, 1)
    ax.set_title("What a dropout warning buys", loc="left")
    ax.legend(loc="upper left")
    save(fig, "cutoff")


def fig_warning() -> None:
    path = RESULTS / "warning_sweep.json"
    if not path.exists():
        return
    sweep = json.loads(path.read_text())
    current = sweep["current_horizon_s"]
    fig, ax = plt.subplots(figsize=SINGLE)
    for mobility, color in (("walking", BLUE), ("driving", ORANGE)):
        rows = [r for r in sweep["rows"] if r["mobility"] == mobility]
        xs = [r["false_alarms_per_hour"] for r in rows]
        ys = [r["warned_2s_pct"] / 100 for r in rows]
        ax.plot(xs, ys, color=color, marker="o", label=mobility.capitalize())
        for r, x, y in zip(rows, xs, ys, strict=True):
            if r["horizon_s"] == current:
                ax.plot(x, y, marker="o", markersize=9, mfc="none", color=INK, lw=0)
            if r["horizon_s"] in (2, current, 15):
                ax.annotate(f"{r['horizon_s']:g} s", (x, y), xytext=(4, -9),
                            textcoords="offset points", color=INK_2, fontsize=6.5)  # fmt: skip
    ax.set_xlabel("false alarms per hour")
    ax.set_ylabel("outages warned 2+ s ahead")
    ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax.set_ylim(0, 1)
    ax.set_xlim(left=0)
    ax.set_title(f"Warning horizon (circled: {current:g} s, in use)", loc="left")
    ax.legend(loc="lower right")
    save(fig, "warning")


def _runs(batch: str) -> list[dict]:
    path = RESULTS / batch / "runs.csv"
    return list(csv.DictReader(path.open())) if path.exists() else []


def fig_waiting(batch: str, scenario: str) -> None:
    rows = [r for r in _runs(batch) if r["scenario"] == scenario]
    if not rows:
        return
    by_config = defaultdict(list)
    for r in rows:
        by_config[r["config"]].append(float(r["wait_s"]))
    names = [n for n in CONFIGS if n in by_config]
    fig, ax = plt.subplots(figsize=(SINGLE[0], 0.5 + 0.38 * len(names)))
    for y, name in enumerate(names):
        waits = by_config[name]
        jitter = [((i % 5) - 2) * 0.05 for i in range(len(waits))]
        ax.scatter(waits, [y + j for j in jitter], s=12, color=BLUE, alpha=0.55, lw=0)
        mean = statistics.fmean(waits)
        ax.plot([mean, mean], [y - 0.3, y + 0.3], color=INK, lw=1.5)
        ax.annotate(f"mean {mean:.1f} s", (mean, y + 0.3), xytext=(3, 0),
                    textcoords="offset points", va="center", color=INK_2, fontsize=6.5)  # fmt: skip
    ax.set_yticks(range(len(names)), [f"{n}  {CONFIGS[n].about}" for n in names])
    ax.invert_yaxis()
    ax.set_xlim(0, None)
    ax.set_xlabel("total time the reader waited for pages (s)")
    ax.grid(axis="y", visible=False)
    n = len(by_config[names[0]])
    ax.set_title(f"{scenario.replace('_', ' ')}: waiting per reader (dots, n={n})", loc="left")
    save(fig, f"waiting_{scenario}")


def fig_data(batch: str, scenario: str) -> None:
    rows = [r for r in _runs(batch) if r["scenario"] == scenario]
    by_config = defaultdict(list)
    for r in rows:
        by_config[r["config"]].append((float(r["pushed_kb"]), float(r["wasted_kb"])))
    names = [n for n in CONFIGS if n in by_config and any(p for p, _ in by_config[n])]
    if not names:
        return
    fig, ax = plt.subplots(figsize=(SINGLE[0], 0.5 + 0.38 * len(names)))
    for y, name in enumerate(names):
        pushed = statistics.fmean(p for p, _ in by_config[name]) / 1000
        wasted = statistics.fmean(w for _, w in by_config[name]) / 1000
        read = pushed - wasted
        ax.barh(y, read, 0.6, color=BLUE, label="read" if y == 0 else None)
        ax.barh(y, wasted, 0.6, left=read, color=ORANGE, edgecolor="white", lw=1,
                label="never read" if y == 0 else None)  # fmt: skip
        ax.annotate(f"{pushed:.2f} MB, {read / pushed:.0%} read", (pushed, y), xytext=(3, 0),
                    textcoords="offset points", va="center", color=INK_2, fontsize=6.5)  # fmt: skip
    ax.set_yticks(range(len(names)), [f"{n}  {CONFIGS[n].about}" for n in names])
    ax.invert_yaxis()
    ax.set_xlabel("data pushed per run (MB, mean)")
    ax.grid(axis="y", visible=False)
    ax.set_title(f"{scenario.replace('_', ' ')}: pushed data", loc="left")
    ax.set_xlim(0, ax.get_xlim()[1] * 1.3)  # room for the labels
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.3), ncol=2)
    save(fig, f"data_{scenario}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default="main", help="system batch in results/")
    ap.add_argument("--scenario", default="car_tunnel_45s")
    args = ap.parse_args()
    style()
    fig_predictors()
    fig_calibration()
    fig_cutoff()
    fig_warning()
    fig_waiting(args.batch, args.scenario)
    fig_data(args.batch, args.scenario)


if __name__ == "__main__":
    main()
