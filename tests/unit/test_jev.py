"""Jev predictor against a fake HTTP server (no real API calls, no credits spent)."""

import json

import httpx
import pytest

from edgeproxy.common.env import get_secret
from edgeproxy.predictors.base import Link, PageState
from edgeproxy.predictors.jev import JevError, JevPredictor


def page(n=3):
    links = [Link(f"http://w/wiki/T{i}", f"anchor {i}", i, i / n, target=f"T{i}") for i in range(n)]
    return PageState("http://w/wiki/P", "P", links, history=["A", "B"])


def fake_jev(calls, status=200):
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "Insufficient credits."}})
        keys = list(body["questions"]["next_click"]["criteria"])
        probs = {k: (0.7 if i == 1 else 0.1) for i, k in enumerate(keys)}
        return httpx.Response(
            200,
            json={
                "model": "typesafe/jev-1.13-20260917",
                "answers": {"next_click": {"type": "choice", "probabilities": probs}},
                "usage": {"input_tokens": 400, "cost": 2e-5},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_maps_probabilities_back_to_urls():
    calls = []
    jev = JevPredictor(api_key="k", client=fake_jev(calls))
    probs = await jev.predict(page())
    assert probs == {"http://w/wiki/T0": 0.1, "http://w/wiki/T1": 0.7, "http://w/wiki/T2": 0.1}
    q = calls[0]["questions"]["next_click"]
    assert calls[0]["model"] == "typesafe/jev-1.13"
    assert calls[0]["state"] == {"current_page": "P", "previous_pages": ["A", "B"]}
    assert q["type"] == "choice" and len(q["criteria"]) == 3
    assert "anchor 1" in q["criteria"]["link_1"]
    assert jev.last_call.model_version == "typesafe/jev-1.13-20260917"
    assert not jev.last_call.cached


async def test_caps_options():
    calls = []
    probs = await JevPredictor(api_key="k", client=fake_jev(calls), max_options=2).predict(page(5))
    assert len(calls[0]["questions"]["next_click"]["criteria"]) == 2
    assert len(probs) == 2


@pytest.mark.parametrize("n", [1, 39, 40, 41])
async def test_pages_with_few_or_many_links(n):
    calls = []
    probs = await JevPredictor(api_key="k", client=fake_jev(calls)).predict(page(n))
    assert len(calls[0]["questions"]["next_click"]["criteria"]) == min(n, 40)
    assert len(probs) == min(n, 40)


async def test_cache_avoids_second_call(tmp_path):
    calls = []
    jev = JevPredictor(api_key="k", client=fake_jev(calls), cache_dir=tmp_path)
    first = await jev.predict(page())
    second = await jev.predict(page())
    assert first == second and len(calls) == 1 and jev.last_call.cached


async def test_api_error_is_readable():
    with pytest.raises(JevError, match="402.*Insufficient credits"):
        await JevPredictor(api_key="k", client=fake_jev([], status=402)).predict(page())


async def test_no_links_no_call():
    calls = []
    assert await JevPredictor(api_key="k", client=fake_jev(calls)).predict(page(0)) == {}
    assert calls == []


def test_secret_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    f = tmp_path / ".env"
    f.write_text("# comment\nOPENROUTER_API_KEY='sk-or-test'\n")
    assert get_secret("OPENROUTER_API_KEY", f) == "sk-or-test"
    with pytest.raises(RuntimeError, match="not set"):
        get_secret("OPENROUTER_API_KEY", tmp_path / "missing")


async def test_context_is_sent_only_when_enabled():
    links = [Link("u/A", "A", 0, 0.0, target="A", context="Roach styles A for events.")]
    state = PageState("u/P", "P", links, summary="P is a stylist.")
    plain, _ = JevPredictor(api_key="k").build_request(state)
    rich, _ = JevPredictor(api_key="k", include_context=True).build_request(state)
    assert "page_summary" not in plain["state"]
    assert "in: " not in plain["questions"]["next_click"]["criteria"]["link_0"]
    assert rich["state"]["page_summary"] == "P is a stylist."
    assert "Roach styles A" in rich["questions"]["next_click"]["criteria"]["link_0"]
