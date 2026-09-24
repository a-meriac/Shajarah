"""Next-click prediction with Jev (TypeSafe's decision model), called through OpenRouter.

One Choice question per page: "which of these links will the reader click next?". Each candidate
link becomes an option. Responses are cached on disk, so re-running an experiment makes no API
calls and doesn't depend on the service staying up. Notes on the API: docs/jev_notes.md.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from edgeproxy.common.env import get_secret
from edgeproxy.predictors.base import PageState
from edgeproxy.predictors.select import choose_links

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"
INSTRUCTIONS = "Which link on this page will the reader most likely click next?"
# Jev rounds probabilities to 2 decimals, so with hundreds of options most tie at 0. Which N links
# it sees is decided by `link_order` (see predictors/select.py).
DEFAULT_MAX_OPTIONS = 40
DEFAULT_LINK_ORDER = "content_first"


class JevError(RuntimeError):
    pass


@dataclass
class CallInfo:
    """Details of the last predict() call, for the event log."""

    cached: bool
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    model_version: str = ""


class JevPredictor:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = MODEL,
        cache_dir: Path | None = None,
        max_options: int = DEFAULT_MAX_OPTIONS,
        link_order: str = DEFAULT_LINK_ORDER,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or get_secret("OPENROUTER_API_KEY")
        self.model = model
        self.cache_dir = cache_dir
        self.max_options = max_options
        self.link_order = link_order
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self.last_call: CallInfo | None = None

    def build_request(self, state: PageState) -> tuple[dict, dict[str, str]]:
        """Returns the request body and a map from option key back to link URL."""
        links = choose_links(state.candidates, self.max_options, self.link_order)
        keys = {f"link_{i}": link.url for i, link in enumerate(links)}
        criteria = {
            key: f"Link text: '{link.anchor or link.target}', goes to '{link.target}', "
            f"link #{link.position + 1} on the page"
            for key, link in zip(keys, links)
        }
        body = {
            "model": self.model,
            "state": {"current_page": state.title, "previous_pages": state.history[-5:]},
            "questions": {
                "next_click": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": criteria}
            },
        }
        return body, keys

    async def predict(self, state: PageState) -> dict[str, float]:
        """Returns {link url: probability of being clicked next}."""
        if not state.candidates:
            return {}
        body, keys = self.build_request(state)
        cache_file = self._cache_file(body)
        if cache_file is not None and cache_file.exists():
            response = json.loads(cache_file.read_text())
            self.last_call = CallInfo(cached=True, model_version=response.get("model", ""))
        else:
            start = time.perf_counter()
            r = await self._client.post(
                ENDPOINT, headers={"Authorization": f"Bearer {self.api_key}"}, json=body
            )
            latency_ms = (time.perf_counter() - start) * 1000
            if r.status_code != 200:
                raise JevError(f"Jev call failed ({r.status_code}): {_error_message(r)}")
            response = r.json()
            usage = response.get("usage", {})
            self.last_call = CallInfo(
                cached=False,
                latency_ms=latency_ms,
                cost_usd=usage.get("cost", 0.0),
                input_tokens=usage.get("input_tokens", 0),
                model_version=response.get("model", ""),
            )
            if cache_file is not None:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(response))
        probs = response["answers"]["next_click"]["probabilities"]
        return {url: float(probs.get(key, 0.0)) for key, url in keys.items()}

    def _cache_file(self, body: dict) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        return self.cache_dir / f"{digest}.json"

    async def aclose(self) -> None:
        await self._client.aclose()


def _error_message(r: httpx.Response) -> str:
    try:
        return r.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return r.text[:200]
