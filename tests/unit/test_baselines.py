from collections import Counter

import pytest

from edgeproxy.predictors.base import Link, PageState
from edgeproxy.predictors.baselines import HistoryPredictor, PositionPredictor


def state(url="https://s/wiki/Page"):
    links = [
        Link("https://s/wiki/Menu", "menu", 0, 0.0, target="Menu", boilerplate=True),
        Link("https://s/wiki/A", "a", 1, 0.25, target="A"),
        Link("https://s/wiki/B", "b", 2, 0.5, target="B"),
        Link("https://s/wiki/C", "c", 3, 0.75, target="C"),
    ]
    return PageState(url, "Page", links)


async def test_position_prefers_early_content_links():
    probs = await PositionPredictor().predict(state())
    assert sum(probs.values()) == pytest.approx(1)
    order = sorted(probs, key=probs.get, reverse=True)
    assert order == [
        "https://s/wiki/A",
        "https://s/wiki/B",
        "https://s/wiki/C",
        "https://s/wiki/Menu",
    ]


async def test_history_follows_past_clicks():
    clicks = {"Page": Counter({"C": 90, "A": 10, "Elsewhere": 500})}
    probs = await HistoryPredictor(clicks).predict(state())
    assert sum(probs.values()) == pytest.approx(1)
    assert max(probs, key=probs.get) == "https://s/wiki/C"
    assert probs["https://s/wiki/B"] > 0  # never clicked before, still gets some prior


async def test_history_without_data_falls_back_to_position():
    history = await HistoryPredictor({}).predict(state("https://s/wiki/New_page"))
    assert history == await PositionPredictor().predict(state("https://s/wiki/New_page"))
