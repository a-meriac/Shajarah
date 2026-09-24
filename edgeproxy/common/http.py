"""HTTP header handling shared by both proxies.

Headers travel in tunnel frames as a list of [name, value] pairs (a dict would lose repeated
headers). Hop-by-hop headers never cross the tunnel.
"""

from __future__ import annotations

from http import HTTPStatus

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
