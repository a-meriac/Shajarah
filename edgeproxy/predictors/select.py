"""Choose which of a page's links a predictor is shown, when it can only take a few.

Every rule uses plain HTML structure, so it works on any website:
- "page": the first N links in page order.
- "content_first": links in the page's content before links in nav/header/footer/aside.
- "repeats_first": like content_first, but links the page repeats come first (a site linking
  to something several times usually considers it important).
"""

from __future__ import annotations

from edgeproxy.predictors.base import Link

ORDERS = ("page", "content_first", "repeats_first")


def choose_links(links: list[Link], n: int, order: str = "page") -> list[Link]:
    if order == "page":
        ranked = links
    elif order == "content_first":
        ranked = sorted(links, key=lambda link: link.boilerplate)
    elif order == "repeats_first":
        ranked = sorted(links, key=lambda link: (link.boilerplate, -link.occurrences))
    else:
        raise ValueError(f"unknown link order {order!r}, expected one of {ORDERS}")
    return ranked[:n]  # sorted() is stable, so ties keep page order
