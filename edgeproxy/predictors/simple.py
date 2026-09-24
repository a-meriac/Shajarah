"""Trivial baselines: uniform over candidates, and position-weighted (earlier links more likely)."""

from __future__ import annotations

from edgeproxy.predictors.base import LinkPredictor, PageState


class UniformPredictor(LinkPredictor):
    name = "uniform"

    def predict(self, state: PageState) -> dict[str, float]:
        n = len(state.candidates)
        return {link.url: 1.0 / n for link in state.candidates} if n else {}


class PositionPredictor(LinkPredictor):
    """p ∝ 1 / (position + 1): Zipf-like decay down the page, a strong clickstream prior."""

    name = "position"

    def __init__(self, exponent: float = 1.0) -> None:
        self.exponent = exponent

    def predict(self, state: PageState) -> dict[str, float]:
        weights = {l.url: 1.0 / (l.position + 1) ** self.exponent for l in state.candidates}
        total = sum(weights.values())
        return {u: w / total for u, w in weights.items()} if total else {}
