"""Baselines to compare Jev against.

- PositionPredictor works on any website with no data: earlier links (skipping page chrome) are
  more likely to be clicked. This is what everyday browsing gets without a model.
- HistoryPredictor counts past clicks: how often readers of this page clicked each link before.
  It needs the site's own click logs (Wikipedia publishes them as the clickstream; any site could
  keep aggregate counts), so it shows what a site with good logs could do without a model.
"""

from __future__ import annotations

from collections import Counter

from edgeproxy.predictors.base import PageState
from edgeproxy.predictors.select import choose_links
from edgeproxy.server.links import wiki_title


def page_key(url: str) -> str:
    """Same key as Link.target: the article title on Wikipedia, the URL anywhere else."""
    return wiki_title(url) or url


class PositionPredictor:
    """Probability falls off as 1 / rank (Zipf), in the given link order."""

    def __init__(self, order: str = "content_first") -> None:
        self.order = order

    async def predict(self, state: PageState) -> dict[str, float]:
        ranked = choose_links(state.candidates, len(state.candidates), self.order)
        weights = {link.url: 1 / (rank + 1) for rank, link in enumerate(ranked)}
        total = sum(weights.values())
        return {url: w / total for url, w in weights.items()}


class HistoryPredictor:
    """Share of last period's clicks from this page that went to each link.

    Links nobody clicked before, and pages with no history at all, fall back to the position
    baseline; `prior_weight` of the probability always comes from it.
    """

    def __init__(
        self,
        clicks: dict[str, Counter[str]],
        prior: PositionPredictor | None = None,
        prior_weight: float = 0.05,
    ) -> None:
        self.clicks = clicks  # page key -> Counter(link target -> clicks)
        self.prior = prior or PositionPredictor()
        self.prior_weight = prior_weight

    async def predict(self, state: PageState) -> dict[str, float]:
        prior = await self.prior.predict(state)
        past = self.clicks.get(page_key(state.url), Counter())
        counts = {link.url: past[link.target] for link in state.candidates}
        total = sum(counts.values())
        if total == 0:
            return prior
        w = self.prior_weight
        return {url: (1 - w) * counts[url] / total + w * prior[url] for url in counts}
