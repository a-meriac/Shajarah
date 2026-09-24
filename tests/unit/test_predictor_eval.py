from collections import Counter

import pytest

from edgeproxy.predictors.base import Link, PageState
from experiments.predictor_eval import calibration, score_page


def test_score_page_counts_clicks_in_top_k():
    links = [Link(f"u/{t}", t, i, i / 3, target=t) for i, t in enumerate("ABC")]
    state = PageState("u/P", "P", links)
    truth = Counter({"B": 60, "C": 30, "Unlinked": 10})  # 10% of clicks go to links not on the page
    probs = {"u/A": 0.5, "u/B": 0.3, "u/C": 0.2}
    s = score_page(state, probs, truth)
    assert s["hit@1"] == 0
    assert s["hit@3"] == pytest.approx(0.9)
    assert s["pairs"][0] == (0.5, 0.0) and s["pairs"][1] == (0.3, 0.6)


def test_ties_break_by_page_position():
    links = [Link(f"u/{t}", t, i, i / 2, target=t) for i, t in enumerate("AB")]
    s = score_page(PageState("u/P", "P", links), {"u/B": 0.5, "u/A": 0.5}, Counter({"A": 1}))
    assert s["hit@1"] == 1


def test_calibration_of_a_perfect_predictor_is_zero():
    bins, ece = calibration([(0.1, 0.1), (0.4, 0.4), (0.4, 0.4)])
    assert ece == pytest.approx(0)
    assert sum(b["n"] for b in bins) == 3
