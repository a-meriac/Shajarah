"""Extract prefetch candidates from an HTML page.

Filters out links that must never be prefetched: anything that could change state on the origin
(logout, add-to-cart, delete, ...) and non-navigational schemes. Also drops links that aren't a
next page to read: media files on any site, and Wikipedia's non-article pages.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin, urlsplit

from selectolax.parser import HTMLParser

from edgeproxy.common.http import canonical_url
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
# Nearest enclosing element whose text is the link's context.
BLOCK_TAGS = {"p", "li", "td", "th", "dd", "dt", "figcaption", "caption", "blockquote",
              "h1", "h2", "h3", "h4", "h5", "h6"}  # fmt: skip
CONTEXT_CHARS = 160
SUMMARY_CHARS = 500
# Links straight to a media file open a viewer, not a page to read next.
MEDIA_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".tif", ".tiff", ".bmp", ".ico",
    ".mp3", ".ogg", ".oga", ".wav", ".flac", ".mp4", ".webm", ".ogv", ".mov",
)  # fmt: skip
# Wikipedia non-article namespaces (Special:, File:, Talk:, ...) — not in the clickstream as
# article targets and mostly not what people click to read next.
WIKI_NON_ARTICLE = re.compile(
    r"^(Special|File|Help|Talk|User|User_talk|Wikipedia|Template|Template_talk|Category|Portal|"
    r"Draft|Module|MediaWiki|Book|TimedText|Media)(_talk)?:"
)


def is_safe_to_prefetch(url: str) -> bool:
    return not UNSAFE_PATTERN.search(url)


def is_reading_link(url: str) -> bool:
    """False for links that aren't a next page to read (media files, wiki meta pages)."""
    path = unquote(urlsplit(url).path)
    if path.lower().endswith(MEDIA_SUFFIXES):
        return False
    return not ("/wiki/" in path and WIKI_NON_ARTICLE.match(path.split("/wiki/", 1)[1]))


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


def _shorten(text: str, around: str, limit: int) -> str:
    """Collapse whitespace and cut `text` to `limit` chars, keeping `around` in view."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    at = max(0, text.find(around)) if around else 0
    start = max(0, min(at - limit // 3, len(text) - limit))
    return ("…" if start else "") + text[start : start + limit].strip() + "…"


def _context(node, anchor: str) -> str:
    block = node.parent
    while block is not None and block.tag not in BLOCK_TAGS and block.tag != "body":
        block = block.parent
    if block is None or block.tag == "body":
        return ""
    return _shorten(block.text(deep=True, separator=" "), anchor, CONTEXT_CHARS)


def page_summary(html: str | bytes) -> str:
    """The page's meta description, or else its first substantial paragraph."""
    tree = HTMLParser(html)
    meta = tree.css_first('meta[name="description"]')
    if meta is not None and (meta.attributes.get("content") or "").strip():
        return _shorten(meta.attributes["content"], "", SUMMARY_CHARS)
    for p in tree.css("p"):
        text = " ".join(p.text(deep=True, separator=" ").split())
        if len(text) >= 80:
            return _shorten(text, "", SUMMARY_CHARS)
    return ""


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
        url = canonical_url(absolute)
        if url == canonical_url(page_url):
            continue
        if url in index:
            seen_before = raw[index[url]]
            seen_before["occurrences"] += 1
            seen_before["boilerplate"] = seen_before["boilerplate"] and _in_boilerplate(node)
            continue
        same_origin = parts.netloc == page.netloc
        if same_origin_only and not same_origin:
            continue
        if not is_safe_to_prefetch(url) or not is_reading_link(url):
            continue
        index[url] = len(raw)
        anchor = " ".join(node.text(deep=True, separator=" ").split())
        raw.append(
            {
                "url": url,
                "anchor": anchor,
                "context": _context(node, anchor),
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
