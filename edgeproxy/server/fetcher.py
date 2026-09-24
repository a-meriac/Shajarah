"""Fetch from the origin on behalf of the client.

Conditional headers (If-None-Match, If-Modified-Since) come from the client proxy's cache and
are passed through unchanged, so a 304 from the origin reaches the client as NOT_MODIFIED and the
page body never crosses the tunnel.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from edgeproxy.common.http import REQUEST_DROP, RESPONSE_DROP, Headers, filter_headers


@dataclass
class OriginResponse:
    status: int
    headers: Headers
    body: bytes


class Fetcher:
    def __init__(self, timeout: float = 15.0, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False
        )

    async def fetch(
        self, url: str, method: str = "GET", headers: Headers = (), body: bytes = b""
    ) -> OriginResponse:
        r = await self._client.request(
            method, url, headers=filter_headers(headers, REQUEST_DROP), content=body or None
        )
        return OriginResponse(
            r.status_code, filter_headers(r.headers.multi_items(), RESPONSE_DROP), r.content
        )

    async def close(self) -> None:
        await self._client.aclose()
