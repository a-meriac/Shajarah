"""Extract prefetch candidates from an HTML page.

Filters out links that must never be prefetched: anything that could change state on the origin
(logout, add-to-cart, delete, ...) and non-navigational schemes.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from edgeproxy.predictors.base import Link

# Deliberately broad: a missed prefetch costs little, a prefetched "logout" breaks the session.
UNSAFE_PATTERN = re.compile(
    r"log[-_]?out|sign[-_]?out|logoff|delete|remove|unsubscribe|add[-_]?to[-_]?cart|"
    r"checkout|purchase|buy[-_]?now|/cart\b|[?&]action=|/vote|/like\b|confirm|token=",
    re.IGNORECASE,
)
SKIP_SCHEMES = ("mailto:", "javascript:", "tel:", "data:")
# Page chrome rather than content, in standard HTML any site can use (Wikipedia's navboxes carry
# role="navigation" too).
BOILERPLATE_TAGS = {"nav", "header", "footer", "aside"}
BOILERPLATE_ROLES = {"navigation", "banner", "contentinfo", "complementary"}
# Wikipedia non-article namespaces (Special:, File:, Talk:, ...) — not in the clickstream as
# article targets and mostly not what people click to read next.
WIKI_NON_ARTICLE = re.compile(
    r"^(Special|File|Help|Talk|User|User_talk|Wikipedia|Template|Template_talk|Category|Portal|"
    r"Draft|Module|MediaWiki|Book|TimedText|Media)(_talk)?:"
)


def is_safe_to_prefetch(url: str) -> bool:
    return not UNSAFE_PATTERN.search(url)


def wiki_title(url: str) -> str | None:
    """Article title as used by the clickstream ('Albert_Einstein'), or None for non-articles."""
    path = urlsplit(url).path
    marker = "/wiki/"
    if marker not in path:
        return None
    title = unquote(path.split(marker, 1)[1])
    if not title or WIKI_NON_ARTICLE.match(title):
        return None
    return title


def _in_boilerplate(node) -> bool:
    node = node.parent
    while node is not None:
        if node.tag in BOILERPLATE_TAGS:
            return True
        if (node.attributes.get("role") or "") in BOILERPLATE_ROLES:
            return True
        node = node.parent
    return False


def extract_links(
    html: str | bytes,
    page_url: str,
    same_origin_only: bool = True,
    max_candidates: int = 2000,
    content_selector: str | None = None,
) -> list[Link]:
    tree = HTMLParser(html)
    root = tree.css_first(content_selector) if content_selector else None
    root = root or tree.body or tree.root
    page = urlsplit(page_url)
    index: dict[str, int] = {}  # url -> position in raw
    raw: list[dict] = []
    for node in root.css("a[href]"):
        href = node.attributes.get("href") or ""
        if not href or href.startswith("#") or href.lower().startswith(SKIP_SCHEMES):
            continue
        absolute = urljoin(page_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        url = urlunsplit(parts._replace(fragment=""))
        if url == urlunsplit(page._replace(fragment="")):
            continue
        if url in index:
            seen_before = raw[index[url]]
            seen_before["occurrences"] += 1
            seen_before["boilerplate"] = seen_before["boilerplate"] and _in_boilerplate(node)
            continue
        same_origin = parts.netloc == page.netloc
        if same_origin_only and not same_origin:
            continue
        if not is_safe_to_prefetch(url):
            continue
        index[url] = len(raw)
        raw.append(
            {
                "url": url,
                "anchor": " ".join(node.text(deep=True, separator=" ").split()),
                "same_origin": same_origin,
                "target": wiki_title(url) or url,
                "occurrences": 1,
                "boilerplate": _in_boilerplate(node),
            }
        )
    # Keep the earliest links when over the cap.
    raw = raw[:max_candidates]
    n = len(raw)
    return [Link(position=i, rel_position=i / n, **r) for i, r in enumerate(raw)]
