"""Link predictor interface: given the page the user is on, how likely is each outgoing click?"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Link:
    url: str
    anchor: str
    position: int  # 0-based order of first appearance in the document
    rel_position: float  # position / number of candidates, in [0, 1)
    same_origin: bool = True
    target: str = ""  # canonical key, e.g. Wikipedia article title (used to join clickstream)


@dataclass
class PageState:
    url: str
    title: str
    candidates: list[Link]
    history: list[str] = field(default_factory=list)  # previous page keys, oldest first
    key: str = ""  # canonical key of the current page (article title)


class LinkPredictor(ABC):
    name: str = "base"

    @abstractmethod
    def predict(self, state: PageState) -> dict[str, float]:
        """Return {link.url: probability}. Probabilities over the candidates sum to <= 1
        (the rest is the chance the user clicks nothing / goes elsewhere)."""

    def top_k(self, state: PageState, k: int) -> list[tuple[str, float]]:
        probs = self.predict(state)
        return sorted(probs.items(), key=lambda kv: kv[1], reverse=True)[:k]
