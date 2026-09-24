"""Server proxy: fetches requests from the origin and pushes the pages the reader will likely want.

After an HTML page is answered, a background task extracts its links, asks the predictor how
likely each one is to be clicked next, and lets the prefetch policy choose which to fetch and
push within the byte budget. The page response is never held up by this, and a failing predictor
only means nothing is pushed.

Each client connection has its own state: pages it browsed (the predictor's history), URLs it
already has, and its connectivity outlook. A HANDOVER_HINT frame from the client ("dropout in
eta_s seconds, lasting about outage_s") switches the policy to its lower threshold and bigger
budget until the client clears it.

  python -m edgeproxy.server.proxy --transport quic --port 4433 --predictor jev
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

from edgeproxy.common.eventlog import NULL_LOG, EventLog
from edgeproxy.common.http import get_header
from edgeproxy.common.protocol import Frame, MsgType
from edgeproxy.predictors.base import PageState, Predictor
from edgeproxy.server.fetcher import Fetcher
from edgeproxy.server.links import extract_links
from edgeproxy.server.prefetch_policy import LinkOutlook, PrefetchPolicy
from edgeproxy.tunnel.transport import ServerSession

HISTORY_LEN = 5


@dataclass
class _Client:
    history: list[str] = field(default_factory=list)  # page titles, oldest first
    sent: set[str] = field(default_factory=set)  # URLs the client already has a copy of
    outlook: LinkOutlook = field(default_factory=LinkOutlook)
    prefetch: asyncio.Task | None = None


def _title(html: bytes, url: str) -> str:
    node = HTMLParser(html).css_first("title")
    text = node.text(strip=True) if node is not None else ""
    return text or url


class ServerProxy:
    def __init__(
        self,
        fetcher: Fetcher | None = None,
        log: EventLog = NULL_LOG,
        predictor: Predictor | None = None,
        policy: PrefetchPolicy | None = None,
        max_candidates: int = 2000,
        same_origin_only: bool = True,
    ) -> None:
        self.fetcher = fetcher or Fetcher()
        self.log = log
        self.predictor = predictor  # None: plain proxy, no prefetching (configs 1 and 2)
        self.policy = policy or PrefetchPolicy()
        self.max_candidates = max_candidates
        self.same_origin_only = same_origin_only
        self._clients: dict[ServerSession, _Client] = {}

    def _client(self, session: ServerSession) -> _Client:
        return self._clients.setdefault(session, _Client())

    async def handle(self, frame: Frame, session: ServerSession) -> Frame | None:
        if frame.type is MsgType.HANDOVER_HINT:
            self._on_hint(frame, session)
            return None
        if frame.type is not MsgType.REQUEST:
            return None
        url = frame.headers["url"]
        method = frame.headers.get("method", "GET")
        start = time.monotonic()
        try:
            r = await self.fetcher.fetch(url, method, frame.headers.get("headers", []), frame.body)
        except httpx.HTTPError as e:
            self.log.emit("origin_error", url=url, error=type(e).__name__)
            msg = f"origin unreachable: {type(e).__name__}".encode()
            return Frame(MsgType.RESPONSE, {"status": 502, "headers": []}, msg)
        self.log.emit(
            "origin_fetch",
            url=url,
            status=r.status,
            bytes=len(r.body),
            dur_s=time.monotonic() - start,
        )
        client = self._client(session)
        client.sent.add(url)
        if r.status == 304:
            return Frame(MsgType.NOT_MODIFIED, {"status": 304, "headers": r.headers})
        is_html = (get_header(r.headers, "content-type") or "").startswith("text/html")
        if self.predictor is not None and method == "GET" and r.status == 200 and is_html:
            if client.prefetch is not None:
                client.prefetch.cancel()  # the reader moved on; predictions for the old page are stale
            client.prefetch = asyncio.ensure_future(self._prefetch(url, r.body, session, client))
        return Frame(MsgType.RESPONSE, {"status": r.status, "headers": r.headers}, r.body)

    def _on_hint(self, frame: Frame, session: ServerSession) -> None:
        h = frame.headers
        outlook = self._client(session).outlook
        outlook.handover_imminent = bool(h.get("active", True))
        outlook.predicted_outage_s = float(h.get("outage_s", 0.0))
        outlook.metered = bool(h.get("metered", outlook.metered))
        self.log.emit("handover_hint_recv", **{k: v for k, v in h.items() if k != "type"})

    async def _prefetch(self, url: str, html: bytes, session: ServerSession, client: _Client):
        title = _title(html, url)
        links = extract_links(
            html, url, same_origin_only=self.same_origin_only, max_candidates=self.max_candidates
        )
        state = PageState(url, title, links, history=list(client.history))
        client.history = [*client.history, title][-HISTORY_LEN:]
        if not links:
            return
        start = time.monotonic()
        try:
            probs = await self.predictor.predict(state)
        except Exception as e:  # noqa: BLE001 -- a failing predictor must never break browsing
            self.log.emit("predict_error", url=url, error=f"{type(e).__name__}: {e}")
            return
        decision = self.policy.decide(probs, client.outlook, already_cached=client.sent)
        self.log.emit(
            "predict",
            url=url,
            links=len(links),
            scored=len(probs),
            chosen=len(decision.urls),
            threshold=decision.threshold,
            budget_bytes=decision.budget_bytes,
            handover=client.outlook.handover_imminent,
            dur_s=time.monotonic() - start,
        )
        spent = 0
        for target, prob in decision.urls:  # most likely first
            if spent >= decision.budget_bytes:
                break
            try:
                r = await self.fetcher.fetch(target)
            except httpx.HTTPError as e:
                self.log.emit("push_error", url=target, error=type(e).__name__)
                continue
            if r.status != 200 or spent + len(r.body) > decision.budget_bytes:
                continue
            spent += len(r.body)
            client.sent.add(target)
            headers = {"url": target, "prob": prob, "status": 200, "headers": r.headers}
            await session.push(Frame(MsgType.PUSH, headers, r.body))
            self.log.emit("push", url=target, prob=prob, bytes=len(r.body), source_url=url)


def make_predictor(name: str, settings) -> Predictor | None:
    if name == "none":
        return None
    if name == "position":
        from edgeproxy.predictors.baselines import PositionPredictor

        return PositionPredictor(settings.jev.link_order)
    if name == "jev":
        from edgeproxy.predictors.jev import JevPredictor

        return JevPredictor(
            model=settings.jev.model,
            max_options=settings.jev.max_options,
            link_order=settings.jev.link_order,
            timeout_s=settings.jev.timeout_s,
            cache_dir=Path(__file__).parents[2] / "data" / "cache" / "jev",
        )
    raise ValueError(f"unknown predictor {name!r}")


async def _serve(args) -> None:
    from edgeproxy.common.settings import load_settings
    from edgeproxy.tunnel.certs import generate_self_signed

    args.certdir.mkdir(parents=True, exist_ok=True)
    cert, key = args.certdir / "cert.pem", args.certdir / "key.pem"
    if not cert.exists():
        generate_self_signed(cert, key)
    s = load_settings(args.settings)
    proxy = ServerProxy(
        Fetcher(timeout=s.server.origin_timeout_s),
        EventLog(args.log, "server", args.run_id),
        predictor=make_predictor(args.predictor, s),
        policy=PrefetchPolicy(s.prefetch, adaptive=not args.fixed_policy),
        max_candidates=s.links.max_candidates,
        same_origin_only=s.links.same_origin_only,
    )
    if args.transport == "quic":
        from edgeproxy.tunnel.quic_tunnel import QuicTunnelServer, server_configuration

        config = server_configuration(cert, key, idle_timeout=s.tunnel.idle_timeout_s)
        server = QuicTunnelServer(args.host, args.port, config, proxy.handle)
    else:
        from edgeproxy.tunnel.tcp_transport import TcpTunnelServer, server_ssl_context

        server = TcpTunnelServer(args.host, args.port, server_ssl_context(cert, key), proxy.handle)
    await server.start()
    print(
        f"server proxy ({args.transport}, predictor={args.predictor}) on {args.host}:{server.port}",
        flush=True,
    )
    await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=["quic", "tcp"], default="quic")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=4433)
    ap.add_argument("--certdir", type=Path, default=Path("/tmp/ep"))
    ap.add_argument("--predictor", choices=["none", "position", "jev"], default="none")
    ap.add_argument(
        "--fixed-policy", action="store_true", help="ignore handover hints (config 3, ablation 5a)"
    )
    ap.add_argument("--log", type=Path, default=None, help="JSONL event log")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--settings", type=Path, default=None, help="default: settings.yaml")
    asyncio.run(_serve(ap.parse_args()))


if __name__ == "__main__":
    main()
