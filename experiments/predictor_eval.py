"""Offline link-predictor evaluation on the Wikipedia snapshot, scored against real clicks.

For each seed page, a predictor ranks the page's links. Scores:
- hit@k: share of the page's real link clicks (answer-key month) that went to its top k links.
  Clicks on links the predictor never saw count as misses, so limiting what Jev sees costs here.
- calibration: for each page's top 10 links, predicted probability vs the link's real share of
  clicks, in bins, plus the expected calibration error (ECE). Only the top links matter, since
  those are the ones a byte budget would prefetch; the long tail of near-zero links would make
  any predictor look calibrated.
Everything is split by popularity bucket.

Predictors: position (any website, no data), history (past clicks, needs site logs), jev.
Jev answers are cached in data/cache/jev, so a rerun costs nothing.

  python -m experiments.predictor_eval --predictors position history
  python -m experiments.predictor_eval --predictors jev --limit 50      # spends OpenRouter credit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import Counter, defaultdict
from itertools import pairwise
from pathlib import Path
from urllib.parse import quote

from edgeproxy.common.settings import load_settings
from edgeproxy.predictors.base import PageState, Predictor
from edgeproxy.predictors.baselines import HistoryPredictor, PositionPredictor
from edgeproxy.server.links import extract_links

ROOT = Path(__file__).parents[1]
SNAPSHOT = ROOT / "data" / "snapshot"
MANIFEST = ROOT / "data" / "snapshot_manifest.json"
JEV_CACHE = ROOT / "data" / "cache" / "jev"
OUT = ROOT / "results" / "predictor_eval"
ORIGIN = "http://origin.local"  # page URLs only need to be consistent; nothing is fetched
KS = (1, 3, 5, 10)
CALIBRATION_TOP = 10
BINS = (0.0, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0001)


def read_clicks(month: str) -> dict[str, Counter[str]]:
    clicks: dict[str, Counter[str]] = defaultdict(Counter)
    with (SNAPSHOT / f"clickstream-{month}.tsv").open(encoding="utf-8") as fh:
        for line in fh:
            prev, curr, n = line.rstrip("\n").split("\t")
            clicks[prev][curr] += int(n)
    return clicks


def page_state(title: str, max_candidates: int) -> PageState:
    url = f"{ORIGIN}/wiki/{quote(title)}"
    html = (SNAPSHOT / "wiki" / f"{title}.html").read_text(encoding="utf-8")
    links = extract_links(html, url, max_candidates=max_candidates)
    return PageState(url, title.replace("_", " "), links)


def score_page(state: PageState, probs: dict[str, float], truth: Counter[str]) -> dict:
    total = sum(truth.values())
    target = {link.url: link.target for link in state.candidates}
    position = {link.url: link.position for link in state.candidates}
    ranked = sorted(probs, key=lambda url: (-probs[url], position[url]))
    hits = {f"hit@{k}": sum(truth[target[url]] for url in ranked[:k]) / total for k in KS}
    pairs = [(probs[url], truth[target[url]] / total) for url in ranked[:CALIBRATION_TOP]]
    return {**hits, "offered": len(probs), "links": len(state.candidates), "pairs": pairs}


def calibration(pairs: list[tuple[float, float]]) -> tuple[list[dict], float]:
    bins = []
    ece = 0.0
    for lo, hi in pairwise(BINS):
        inside = [(p, y) for p, y in pairs if lo <= p < hi]
        if not inside:
            continue
        pred = statistics.fmean(p for p, _ in inside)
        real = statistics.fmean(y for _, y in inside)
        bins.append(
            {"lo": lo, "hi": min(hi, 1.0), "n": len(inside), "predicted": pred, "real": real}
        )
        ece += len(inside) / len(pairs) * abs(pred - real)
    return bins, ece


def make_predictor(name: str, settings) -> Predictor:
    if name == "position":
        return PositionPredictor(settings.jev.link_order)
    if name == "history":
        return HistoryPredictor(read_clicks(settings.snapshot.history_month))
    if name == "jev":
        from edgeproxy.predictors.jev import JevPredictor

        return JevPredictor(
            model=settings.jev.model,
            max_options=settings.jev.max_options,
            link_order=settings.jev.link_order,
            timeout_s=settings.jev.timeout_s,
            cache_dir=JEV_CACHE,
        )
    raise ValueError(f"unknown predictor {name!r}")


async def evaluate(name: str, seeds: list[dict], truth, settings, concurrency: int) -> dict:
    predictor = make_predictor(name, settings)
    sem = asyncio.Semaphore(concurrency)
    cost = 0.0
    calls = 0

    async def one(page: dict) -> dict | None:
        nonlocal cost, calls
        clicks = truth.get(page["title"])
        if not clicks:
            return None
        state = page_state(page["title"], settings.links.max_candidates)
        async with sem:
            probs = await predictor.predict(state)
            info = getattr(predictor, "last_call", None)  # read before yielding: it's per call
            if info is not None and not info.cached:
                cost += info.cost_usd
                calls += 1
        return {
            "title": page["title"],
            "bucket": page["bucket"],
            **score_page(state, probs, clicks),
        }

    rows = [r for r in await asyncio.gather(*(one(p) for p in seeds)) if r is not None]
    if hasattr(predictor, "aclose"):
        await predictor.aclose()

    summary = {}
    for bucket in ("all", *settings.snapshot.buckets):
        subset = [r for r in rows if bucket in ("all", r["bucket"])]
        if not subset:
            continue
        bins, ece = calibration([pair for r in subset for pair in r["pairs"]])
        summary[bucket] = {
            "pages": len(subset),
            **{f"hit@{k}": statistics.fmean(r[f"hit@{k}"] for r in subset) for k in KS},
            "ece": ece,
            "calibration": bins,
        }
    return {
        "predictor": name,
        "settings": {"jev": vars(settings.jev), "links": vars(settings.links)},
        "new_api_calls": calls,
        "cost_usd": cost,
        "summary": summary,
        "pages": [{k: v for k, v in r.items() if k != "pairs"} for r in rows],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictors", nargs="+", default=["position", "history"])
    ap.add_argument("--limit", type=int, default=None, help="evaluate only the first N seeds")
    ap.add_argument("--concurrency", type=int, default=4, help="parallel Jev calls")
    ap.add_argument("--settings", type=Path, default=None, help="default: settings.yaml")
    args = ap.parse_args()
    settings = load_settings(args.settings)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    seeds = [p for p in manifest["pages"] if p["role"] == "seed" and "error" not in p]
    if args.limit:
        # Take the same share from every bucket, so a small paid run is still stratified.
        per_bucket = max(1, args.limit // len(settings.snapshot.buckets))
        by_bucket = defaultdict(list)
        for p in seeds:
            by_bucket[p["bucket"]].append(p)
        seeds = [p for pages in by_bucket.values() for p in pages[:per_bucket]]
    truth = read_clicks(settings.snapshot.key_month)

    OUT.mkdir(parents=True, exist_ok=True)
    header = f"{'predictor':10} {'bucket':6} {'pages':>5} " + " ".join(
        f"{'hit@' + str(k):>7}" for k in KS
    )
    print(header + f" {'ECE':>6}")
    for name in args.predictors:
        result = asyncio.run(evaluate(name, seeds, truth, settings, args.concurrency))
        suffix = f"-limit{args.limit}" if args.limit else ""
        (OUT / f"{name}{suffix}.json").write_text(json.dumps(result, indent=1))
        for bucket, s in result["summary"].items():
            hits = " ".join(f"{s[f'hit@{k}']:7.1%}" for k in KS)
            print(f"{name:10} {bucket:6} {s['pages']:5} {hits} {s['ece']:6.3f}")
        if result["new_api_calls"]:
            print(
                f"  {result['new_api_calls']} new Jev calls, ${result['cost_usd']:.4f}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
