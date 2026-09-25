"""Server proxy: fetches requests from the origin and pushes the pages the reader will likely want.

After an HTML page is answered, a background task extracts its links, asks the predictor how
likely each one is to be clicked next, and lets the prefetch policy choose which to fetch and
push within the byte budget. The page response is never held up by this, and a failing predictor
only means nothing is pushed.

Every page view carries the client's own list of recently visited pages, which becomes the
predictor's history (frames can arrive out of order after an outage, so the server doesn't
keep its own). Pages the client opens from its cache never reach the server as requests, so
the client reports them with VIEWED, and the server predicts from there too.

Per client connection the server keeps the URLs the client already has, the page it's on, and
its connectivity outlook. A HANDOVER_HINT ("dropout in eta_s seconds, lasting about outage_s")
switches the policy to its lower threshold and bigger budget and immediately re-plans pushes
for the current page; clearing the hint switches back.

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
from edgeproxy.common.protocol import HISTORY_LEN, Frame, MsgType, set_compression
from edgeproxy.predictors.base import PageState, Predictor
from edgeproxy.server.fetcher import Fetcher
from edgeproxy.server.links import extract_links, page_summary
from edgeproxy.server.prefetch_policy import LinkOutlook, PrefetchPolicy
from edgeproxy.tunnel.transport import ServerSession


@dataclass
class _Client:
    current: tuple[str, list[str]] | None = None  # (page url, history urls) the reader is on
    sent: set[str] = field(default_factory=set)  # URLs the client already has a copy of
    outlook: LinkOutlook = field(default_factory=LinkOutlook)
    prefetch: asyncio.Task | None = None


def _title(html: bytes, url: str) -> str:
    node = HTMLParser(html).css_first("title")
    text = node.text(strip=True) if node is not None else ""
    return text or url


def build_page_state(
    url: str,
    html: bytes | str,
    history: list[str],
    max_candidates: int = 2000,
    same_origin_only: bool = True,
) -> PageState:
    """What the predictor is asked about for a page. The Jev warm-up builds states with this
    same function, so its cached answers match the server's questions exactly."""
    links = extract_links(
        html, url, same_origin_only=same_origin_only, max_candidates=max_candidates
    )
    return PageState(
        url, _title(html, url), links, history=list(history), summary=page_summary(html)
    )


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
        self._titles: dict[str, str] = {}  # url -> page title, for history

    def _client(self, session: ServerSession) -> _Client:
        return self._clients.setdefault(session, _Client())

    async def handle(self, frame: Frame, session: ServerSession) -> Frame | None:
        if frame.type is MsgType.PING:
            return Frame(MsgType.RESPONSE, {"status": 204})
        if frame.type is MsgType.HANDOVER_HINT:
            self._on_hint(frame, session)
            return None
        if frame.type is MsgType.VIEWED:
            self._page_viewed(frame.headers["url"], session, None, frame.headers.get("history", []))
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
        self._client(session).sent.add(url)
        is_html = (get_header(r.headers, "content-type") or "").startswith("text/html")
        if method == "GET" and is_html and r.status in (200, 304):
            html = r.body if r.status == 200 else None
            self._page_viewed(url, session, html, frame.headers.get("history", []))
        if r.status == 304:
            return Frame(MsgType.NOT_MODIFIED, {"status": 304, "headers": r.headers})
        return Frame(MsgType.RESPONSE, {"status": r.status, "headers": r.headers}, r.body)

    def _page_viewed(
        self, url: str, session: ServerSession, html: bytes | None, history: list[str]
    ) -> None:
        """The reader is now on `url`: start predicting and pushing its likely next pages."""
        if self.predictor is None:
            return
        client = self._client(session)
        client.current = (url, list(history)[-HISTORY_LEN:])
        if client.prefetch is not None:
            client.prefetch.cancel()  # the reader moved on; predictions for the old page are stale
        client.prefetch = asyncio.ensure_future(
            self._prefetch(url, html, client.current[1], session, client)
        )

    def _on_hint(self, frame: Frame, session: ServerSession) -> None:
        h = frame.headers
        outlook = self._client(session).outlook
        outlook.handover_imminent = bool(h.get("active", True))
        outlook.predicted_outage_s = float(h.get("outage_s", 0.0))
        outlook.metered = bool(h.get("metered", outlook.metered))
        self.log.emit("handover_hint_recv", **{k: v for k, v in h.items() if k != "type"})
        client = self._client(session)
        if outlook.handover_imminent and client.current is not None:
            # Re-plan for the page the reader is on now, with the outage budget.
            url, history = client.current
            self._page_viewed(url, session, None, history)

    async def _title_of(self, url: str) -> str:
        if url not in self._titles:
            try:
                r = await self.fetcher.fetch(url)
                self._titles[url] = _title(r.body, url) if r.status == 200 else url
            except httpx.HTTPError:
                return url
        return self._titles[url]

    async def _prefetch(
        self,
        url: str,
        html: bytes | None,
        history: list[str],
        session: ServerSession,
        client: _Client,
    ) -> None:
        if html is None:  # viewed from the client's cache, or revalidated: get the page ourselves
            try:
                r = await self.fetcher.fetch(url)
            except httpx.HTTPError as e:
                self.log.emit("origin_error", url=url, error=type(e).__name__)
                return
            if r.status != 200:
                return
            html = r.body
        titles = [await self._title_of(u) for u in history]
        state = build_page_state(url, html, titles, self.max_candidates, self.same_origin_only)
        self._titles[url] = state.title
        probs = await self._predict(state, depth=1)
        if probs is None:
            return
        decision = self.policy.decide(probs, client.outlook, already_cached=client.sent)
        self.log.emit(
            "decide",
            url=url,
            chosen=len(decision.urls),
            threshold=decision.threshold,
            budget_bytes=decision.budget_bytes,
            handover=client.outlook.handover_imminent,
        )
        budget = _Budget(decision.budget_bytes)
        bodies = await self._push(decision.urls, url, 1, budget, session, client)

        k = self.policy.depth2_k(client.outlook)
        if not k:
            return
        # Expand every page the reader is likely to open next, including ones it already has.
        # Ask about them all at once: the warning comes only seconds before the link dies.
        children = []
        for parent in depth2_parents(self.policy, probs, client.outlook):
            body = bodies.get(parent)
            if body is None:
                try:
                    r = await self.fetcher.fetch(parent)
                except httpx.HTTPError as e:
                    self.log.emit("origin_error", url=parent, error=type(e).__name__)
                    continue
                if r.status != 200:
                    continue
                body = r.body
            child = child_state(parent, body, state, self.max_candidates, self.same_origin_only)
            self._titles[parent] = child.title
            children.append(child)
        answers = await asyncio.gather(*(self._predict(c, depth=2) for c in children))
        for child, child_probs in zip(children, answers, strict=True):  # most likely parent first
            if budget.spent >= budget.limit:
                break
            if not child_probs:
                continue
            ranked = sorted(child_probs.items(), key=lambda kv: kv[1], reverse=True)
            picks = [(u, p) for u, p in ranked if u not in client.sent][:k]
            await self._push(picks, child.url, 2, budget, session, client)

    async def _predict(self, state: PageState, depth: int) -> dict[str, float] | None:
        if not state.candidates:
            return None
        start = time.monotonic()
        try:
            probs = await self.predictor.predict(state)
        except Exception as e:  # noqa: BLE001 -- a failing predictor must never break browsing
            self.log.emit(
                "predict_error", url=state.url, depth=depth, error=f"{type(e).__name__}: {e}"
            )
            return None
        self.log.emit(
            "predict",
            url=state.url,
            depth=depth,
            links=len(state.candidates),
            scored=len(probs),
            dur_s=time.monotonic() - start,
        )
        return probs

    async def _push(
        self,
        targets: list[tuple[str, float]],
        source_url: str,
        depth: int,
        budget: _Budget,
        session: ServerSession,
        client: _Client,
    ) -> dict[str, bytes]:
        """Push targets in order until the budget runs out. Returns the pushed pages' bodies."""
        pushed: dict[str, bytes] = {}
        for target, prob in targets:  # most likely first
            if budget.spent >= budget.limit:
                break
            try:
                r = await self.fetcher.fetch(target)
            except httpx.HTTPError as e:
                self.log.emit("push_error", url=target, error=type(e).__name__)
                continue
            if r.status != 200:
                continue
            headers = {"url": target, "prob": prob, "status": 200, "headers": r.headers}
            frame = Frame(MsgType.PUSH, headers, r.body)
            wire = len(frame.encode())  # the budget is about bytes on the network
            if budget.spent + wire > budget.limit:
                continue
            budget.spent += wire
            client.sent.add(target)
            pushed[target] = r.body
            await session.push(frame)
            self.log.emit(
                "push",
                url=target,
                prob=prob,
                bytes=len(r.body),
                wire_bytes=wire,
                source_url=source_url,
                depth=depth,
            )
        return pushed


@dataclass
class _Budget:
    limit: int
    spent: int = 0


def depth2_parents(policy: PrefetchPolicy, probs: dict[str, float], outlook: LinkOutlook):
    """Pages whose links get pushed two clicks deep: everything likely enough to push at depth 1,
    whether or not the client already has it. Shared with the Jev warm-up."""
    return [url for url, _ in policy.decide(probs, outlook).urls]


def child_state(
    url: str, html: bytes, parent: PageState, max_candidates: int, same_origin_only: bool
) -> PageState:
    """The predictor question for a page one click beyond `parent`, as if the reader opened it.
    Shared with the Jev warm-up, so the emulation finds these answers cached."""
    history = [*parent.history, parent.title][-HISTORY_LEN:]
    return build_page_state(url, html, history, max_candidates, same_origin_only)


def make_predictor(name: str, settings, offline: bool = False) -> Predictor | None:
    """offline: Jev answers only from the cache, replaying the recorded delay (emulation runs)."""
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
            include_context=settings.jev.include_context,
            timeout_s=settings.jev.timeout_s,
            cache_dir=Path(__file__).parents[2] / "data" / "cache" / "jev",
            offline=offline,
            replay_latency=offline,
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
    set_compression(s.tunnel.compress)
    proxy = ServerProxy(
        Fetcher(timeout=s.server.origin_timeout_s),
        EventLog(args.log, "server", args.run_id),
        predictor=make_predictor(args.predictor, s, offline=args.offline),
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
        "--offline", action="store_true", help="Jev from the warmed cache only (no internet)"
    )
    ap.add_argument(
        "--fixed-policy", action="store_true", help="ignore handover hints (config 3, ablation 5a)"
    )
    ap.add_argument("--log", type=Path, default=None, help="JSONL event log")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--settings", type=Path, default=None, help="default: settings.yaml")
    asyncio.run(_serve(ap.parse_args()))


if __name__ == "__main__":
    main()
