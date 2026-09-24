"""First-order Markov predictor trained on Wikipedia clickstream (prev -> curr counts).

Train on month M and evaluate on month M+1: the clickstream is aggregated transition counts, so
evaluating on the training month would score the model against its own data.
"""

from __future__ import annotations

import csv
import gzip
from collections import defaultdict
from pathlib import Path

from edgeproxy.predictors.base import LinkPredictor, PageState


class MarkovPredictor(LinkPredictor):
    name = "markov"

    def __init__(self, alpha: float = 0.1) -> None:
        self.alpha = alpha  # additive smoothing so unseen links keep a little mass
        self.counts: dict[str, dict[str, int]] = defaultdict(dict)
        self.out_total: dict[str, int] = defaultdict(int)

    def add(self, prev: str, curr: str, n: int) -> None:
        row = self.counts[prev]
        row[curr] = row.get(curr, 0) + n
        self.out_total[prev] += n

    def load_clickstream(self, path: Path, keep: set[str] | None = None) -> None:
        """Wikimedia clickstream TSV (optionally gzipped): prev, curr, type, n.
        Only 'link' rows are internal article-to-article clicks. `keep` limits `prev` pages to
        save memory (the full English file is ~30M rows)."""
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", newline="") as fh:
            for prev, curr, typ, n in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
                if typ != "link" or (keep is not None and prev not in keep):
                    continue
                self.add(prev, curr, int(n))

    def known(self, key: str) -> bool:
        return key in self.counts

    def predict(self, state: PageState) -> dict[str, float]:
        row = self.counts.get(state.key, {})
        weights = {l.url: row.get(l.target, 0) + self.alpha for l in state.candidates}
        # Normalise by all clicks out of the page, not just by these candidates, so the
        # probabilities stay calibrated when the candidate list was pre-filtered.
        total = max(
            self.out_total.get(state.key, 0), sum(row.get(l.target, 0) for l in state.candidates)
        )
        denom = total + self.alpha * len(state.candidates)
        return {u: w / denom for u, w in weights.items()} if denom else {}
