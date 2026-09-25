from experiments.cutoff_sweep import Rule, View, ceiling, evaluate, to_markdown


def size(url):
    return 100


VIEWS = [
    # session 0: next page is Jev's favourite; the one after is Jev's 2nd pick on that page
    View(0, {"a": 0.6, "b": 0.3, "c": 0.02}, "a", {"x": 0.5, "y": 0.4}, "y"),
    # session 1: next page is a long shot
    View(1, {"a": 0.7, "b": 0.2, "c": 0.02}, "c"),
    # session 2: next page wasn't among the offered links
    View(2, {"a": 0.9, "b": 0.05}, "zzz"),
]


def test_rules_rank_and_cut():
    probs = {"a": 0.2, "b": 0.5, "c": 0.03}
    assert Rule("threshold", 0.03).pushed(probs) == ["b", "a", "c"]
    assert Rule("threshold", 0.25).pushed(probs) == ["b"]
    assert Rule("top", 2).pushed(probs) == ["b", "a"]
    assert Rule("threshold", 0.0).name == "p >= 0" and Rule("top", 5).name == "top 5"


def test_evaluate_counts_hits_bytes_and_depth2():
    r = evaluate(VIEWS, Rule("threshold", 0.25), size, reps=200)
    assert r["next_pushed"] == 1 / 3  # only session 0
    assert r["pages"] == 4 / 3  # a,b | a | a
    assert abs(r["mb"] - 400 / 3 / 1e6) < 1e-12
    assert r["useful_data"] == 100 / 400
    assert r["second_click_depth2"] == 1.0  # y (0.4) passes 0.25 on the next page
    lo, hi = r["ci95"]
    assert 0 <= lo <= r["next_pushed"] <= hi <= 1


def test_lower_cutoff_catches_long_shot_but_costs_more():
    low = evaluate(VIEWS, Rule("threshold", 0.02), size, reps=200)
    high = evaluate(VIEWS, Rule("threshold", 0.25), size, reps=200)
    assert low["next_pushed"] == 2 / 3 and low["mb"] > high["mb"]
    assert evaluate(VIEWS, Rule("top", 1), size, reps=200)["second_click_depth2"] == 0.0


def test_ceiling_and_markdown():
    assert ceiling(VIEWS) == 2 / 3
    rows = [evaluate(VIEWS, Rule("threshold", 0.25), size, reps=50)]
    md = to_markdown(rows, VIEWS, {"p >= 0.25": "normal now"})
    assert "ceiling): 67%" in md and "p >= 0.25 ← normal now" in md
