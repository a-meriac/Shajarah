"""HTTP header handling shared by both proxies.

Headers travel in tunnel frames as a list of [name, value] pairs (a dict would lose repeated
headers). Hop-by-hop headers never cross the tunnel.
"""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import quote, unquote, urlsplit, urlunsplit

Headers = list[tuple[str, str]]

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
# The server proxy's HTTP client picks its own encoding and hands us a decoded body, and each
# proxy recomputes Content-Length for the body it actually sends.
REQUEST_DROP = HOP_BY_HOP | {"host", "accept-encoding", "content-length"}
RESPONSE_DROP = HOP_BY_HOP | {"content-encoding", "content-length"}


def filter_headers(headers, drop: set[str]) -> Headers:
    return [(k, v) for k, v in headers if k.lower() not in drop]


def get_header(headers: Headers, name: str) -> str | None:
    name = name.lower()
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


def reason(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Unknown"


_PATH_SAFE = "/:@!$&'()*+,;=~"
_QUERY_SAFE = "/:@!$&'()*+,;=~?%"


def canonical_url(url: str) -> str:
    """One spelling per URL, so a pushed page and the browser's later request share a cache key.

    Pages link to 'Sandra_Hüller' while browsers send 'Sandra_H%C3%BCller': percent-encode the
    path uniformly, encode non-ASCII in the query (keeping its existing escapes), lowercase the
    scheme and host, and drop the fragment.
    """
    p = urlsplit(url)
    path = quote(unquote(p.path), safe=_PATH_SAFE)
    query = quote(p.query, safe=_QUERY_SAFE)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, query, ""))
