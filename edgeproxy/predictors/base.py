"""What the link predictor sees: the current page and the links the user could click next."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Link:
    url: str
    anchor: str
    position: int  # 0-based order of first appearance in the document
    rel_position: float  # position / number of candidates, in [0, 1)
    same_origin: bool = True
    target: str = ""  # canonical key, e.g. Wikipedia article title (joins with the clickstream)
    occurrences: int = 1  # how many times the page links to this URL
    boilerplate: bool = False  # every occurrence is inside nav/header/footer/aside


@dataclass
class PageState:
    url: str
    title: str
    candidates: list[Link]
    history: list[str] = field(default_factory=list)  # previous page titles, oldest first


class Predictor(Protocol):
    async def predict(self, state: PageState) -> dict[str, float]:
        """Returns {link url: probability that it's the next link clicked}."""
        ...
