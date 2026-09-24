"""Decide which predicted links to push, and how much, given the client's connectivity outlook.

Normal operation: push only confident predictions within a small byte budget.
Handover/outage predicted: lower the threshold and raise the budget so the cache can carry the
user through the gap. The budget scales with the predicted outage length.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PolicyConfig:
    threshold: float = 0.25
    budget_bytes: int = 2_000_000
    handover_threshold: float = 0.03
    handover_budget_bytes: int = 20_000_000
    # Extra budget per second of predicted outage (assumes one click every ~15-30 s of reading).
    handover_budget_per_outage_s: int = 500_000
    max_budget_bytes: int = 60_000_000
    metered_factor: float = 0.25  # guideline: prefetch aggressively only on unmetered links
    default_size: int = 60_000  # wire-size guess for an unfetched page (~250 KB HTML, compressed)


@dataclass
class LinkOutlook:
    handover_imminent: bool = False
    predicted_outage_s: float = 0.0
    metered: bool = False


@dataclass
class Decision:
    urls: list[tuple[str, float]]  # (url, prob) in push order
    threshold: float
    budget_bytes: int
    planned_bytes: int


class PrefetchPolicy:
    def __init__(self, config: PolicyConfig | None = None, adaptive: bool = True) -> None:
        self.config = config or PolicyConfig()
        self.adaptive = adaptive  # False = fixed policy (ablation / Silk-style config)

    def limits(self, outlook: LinkOutlook) -> tuple[float, int]:
        c = self.config
        if self.adaptive and outlook.handover_imminent:
            threshold = c.handover_threshold
            budget = c.handover_budget_bytes + int(
                c.handover_budget_per_outage_s * max(0.0, outlook.predicted_outage_s)
            )
        else:
            threshold, budget = c.threshold, c.budget_bytes
        if outlook.metered:
            budget = int(budget * c.metered_factor)
        return threshold, min(budget, c.max_budget_bytes)

    def decide(
        self,
        probs: dict[str, float],
        outlook: LinkOutlook,
        sizes: dict[str, int] | None = None,
        already_cached: set[str] | None = None,
    ) -> Decision:
        threshold, budget = self.limits(outlook)
        sizes = sizes or {}
        already_cached = already_cached or set()
        chosen: list[tuple[str, float]] = []
        planned = 0
        for url, p in sorted(probs.items(), key=lambda kv: kv[1], reverse=True):
            if p < threshold:
                break
            if url in already_cached:
                continue
            size = sizes.get(url, self.config.default_size)
            if planned + size > budget:
                continue  # a smaller, less likely page may still fit
            chosen.append((url, p))
            planned += size
        return Decision(chosen, threshold, budget, planned)
