"""The sweep's loader on a tiny fake snapshot + Jev cache (the real ones live on the Linux PC)."""

import json

import data.build_snapshot as snapshot
import experiments.cutoff_sweep as sweep
from edgeproxy.common.settings import load_settings
from edgeproxy.server.proxy import make_predictor
from experiments import harness

PAGES = {
    "A": ["B", "C"],
    "B": ["C", "A"],
    "C": ["A", "B"],
}


def page_html(title):
    links = "".join(f'<a href="/wiki/{t}">{t}</a> ' for t in PAGES[title])
    return f"<html><head><title>{title}</title></head><body><p>{links}</p></body></html>"


async def test_load_views_matches_warmup_questions(tmp_path, monkeypatch):
    (tmp_path / "snap" / "wiki").mkdir(parents=True)
    for title in PAGES:
        (tmp_path / "snap" / "wiki" / f"{title}.html").write_text(page_html(title))
    sessions = {"sessions": [{"id": 0, "pages": [{"title": t} for t in ["A", "B", "C"]]}]}
    (tmp_path / "sessions.json").write_text(json.dumps(sessions))
    monkeypatch.setattr(snapshot, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(harness, "SESSIONS", tmp_path / "sessions.json")

    settings = load_settings(None)
    real_make = make_predictor

    def make(name, s, offline=False):
        p = real_make(name, s, offline=offline)
        p.cache_dir = tmp_path / "cache"
        return p

    monkeypatch.setattr(sweep, "make_predictor", make)

    # Fake Jev answers, stored exactly where the predictor will look: favourite = first link.
    jev = make("jev", settings, offline=True)
    history = []
    for title in ["A", "B", "C"]:
        state = sweep.build_page_state(
            harness.page_url(title), page_html(title), history, 2000, True
        )
        body, keys = jev.build_request(state)
        probs = {k: (0.8 if i == 0 else 0.1) for i, k in enumerate(keys)}
        path = jev._cache_file(body)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"answers": {"next_click": {"probabilities": probs}}}))
        history = [*history, state.title]

    views = await sweep.load_views(settings)
    assert len(views) == 2  # A->B and B->C
    assert views[0].next_url == harness.page_url("B")
    assert views[0].probs[views[0].next_url] == 0.8  # B is A's first link
    assert views[0].after_next_url == harness.page_url("C")
    assert views[1].after_next_url is None
    r = sweep.evaluate(views, sweep.Rule("threshold", 0.5), sweep.PageSizes(), reps=50)
    assert r["next_pushed"] == 1.0 and r["second_click_depth2"] == 1.0 and r["mb"] > 0
