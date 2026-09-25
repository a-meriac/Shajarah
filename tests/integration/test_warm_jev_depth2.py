"""The warm-up asks the same two-clicks-deep questions the server would, stand-ins included."""

from data import build_snapshot
from edgeproxy.common.settings import load_settings
from edgeproxy.server.proxy import build_page_state
from experiments.harness import page_url
from experiments.warm_jev import depth2_states


def _page(title, *targets):
    links = " ".join(f"<a href='/wiki/{t}'>{t}</a>" for t in targets)
    return f"<html><head><title>{title}</title></head><body>{links}</body></html>"


def test_depth2_questions_follow_the_server(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    for title, targets in {"A": ("B", "Missing", "C"), "B": ("A",), "C": ("B",)}.items():
        (wiki / f"{title}.html").write_text(_page(title, *targets))
    monkeypatch.setattr(build_snapshot, "SNAPSHOT_DIR", tmp_path)
    settings = load_settings(None)
    settings.prefetch.handover_threshold = 0.03  # independent of whatever settings.yaml says

    state = build_page_state(page_url("A"), (wiki / "A.html").read_text(), ["Z"], 2000, True)
    by_target = {"B": 0.5, "Missing": 0.2, "C": 0.01}  # C is below the outage threshold
    probs = {link.url: by_target[link.target] for link in state.candidates}
    children = depth2_states(settings, [state], [probs])

    assert [c.url for c in children] == [page_url("B"), page_url("Missing")]
    assert all(c.history == ["Z", "A"] for c in children)
    # The missing article is asked about as the stand-in page the origin would serve for it.
    assert children[1].title in {"A", "B", "C"}


def test_no_depth2_questions_when_switched_off(tmp_path, monkeypatch):
    monkeypatch.setattr(build_snapshot, "SNAPSHOT_DIR", tmp_path)
    settings = load_settings(None)
    settings.prefetch.depth2_top_k = 0
    assert depth2_states(settings, [object()], [{"u": 1.0}]) == []
