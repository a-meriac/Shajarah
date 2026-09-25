"""Browser -> client proxy -> tunnel (QUIC or TCP) -> server proxy -> local origin, on loopback."""

import asyncio
import threading

import httpx
import pytest

from data.origin_server import OriginServer
from edgeproxy.client.cache import Cache
from edgeproxy.client.proxy import SOURCE_HEADER, ClientProxy
from edgeproxy.common.protocol import Frame, MsgType
from edgeproxy.server.proxy import ServerProxy
from edgeproxy.tunnel.quic_tunnel import (
    QuicClientTransport,
    QuicTunnelServer,
    client_configuration,
    server_configuration,
)
from edgeproxy.tunnel.tcp_transport import (
    TcpClientTransport,
    TcpTunnelServer,
    client_ssl_context,
    server_ssl_context,
)

PAGE_A = b"<html><head><title>A</title></head><body><a href='/wiki/B'>B</a> <a href='/wiki/C'>C</a></body></html>"
PAGE_B = b"<html><body>B</body></html>"
PAGE_C = b"<html><body>C</body></html>"


@pytest.fixture
def origin(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "A.html").write_bytes(PAGE_A)
    (tmp_path / "wiki" / "B.html").write_bytes(PAGE_B)
    (tmp_path / "wiki" / "C.html").write_bytes(PAGE_C)
    server = OriginServer(tmp_path, ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _url(origin, path):
    return f"http://127.0.0.1:{origin.server_address[1]}{path}"


class Stack:
    """Everything between the browser and the origin, with a switch to stall the server side."""

    def __init__(self, kind, tunnel_certs, server_proxy=None, **proxy_kwargs):
        self.kind = kind
        self.certs = tunnel_certs
        self.proxy_kwargs = proxy_kwargs
        self.server_proxy = server_proxy or ServerProxy()
        self.link_up = asyncio.Event()
        self.link_up.set()

    async def _handler(self, frame, session):
        await self.link_up.wait()  # cleared = outage: requests hang, like 100% loss
        return await self.server_proxy.handle(frame, session)

    async def __aenter__(self):
        cert, key = self.certs
        if self.kind == "quic":
            self.server = QuicTunnelServer(
                "127.0.0.1", 0, server_configuration(cert, key), self._handler
            )
            await self.server.start()
            self.transport = QuicClientTransport(
                ("127.0.0.1", self.server.port),
                client_configuration(cert),
                local_addr=("127.0.0.1", 0),
            )
        else:
            self.server = TcpTunnelServer(
                "127.0.0.1", 0, server_ssl_context(cert, key), self._handler
            )
            await self.server.start()
            self.transport = TcpClientTransport(
                ("127.0.0.1", self.server.port),
                client_ssl_context(cert),
                local_addr=("127.0.0.1", 0),
            )
        await self.transport.connect()
        self.cache = Cache(10_000_000)
        self.proxy = ClientProxy(self.transport, self.cache, **self.proxy_kwargs)
        await self.proxy.start("127.0.0.1", 0)
        self.browser = httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{self.proxy.port}",
            trust_env=False,
            timeout=10,
            headers={"Accept": "text/html"},  # page visits, as a browser navigating
        )
        return self

    async def __aexit__(self, *exc):
        self.link_up.set()
        await self.browser.aclose()
        await self.proxy.close()
        await self.server_proxy.fetcher.close()
        self.server.close()


@pytest.fixture(params=["quic", "tcp"])
def kind(request):
    return request.param


async def test_page_then_revalidation(kind, tunnel_certs, origin):
    async with Stack(kind, tunnel_certs) as s:
        r = await s.browser.get(_url(origin, "/wiki/A"))
        assert r.status_code == 200 and r.content == PAGE_A
        assert r.headers[SOURCE_HEADER] == "origin"
        assert r.headers["content-type"].startswith("text/html")

        r = await s.browser.get(_url(origin, "/wiki/A"))
        assert r.status_code == 200 and r.content == PAGE_A
        assert r.headers[SOURCE_HEADER] == "revalidated"
        assert origin.log == [("/wiki/A", 200), ("/wiki/A", 304)]


async def test_changed_page_is_refetched(kind, tunnel_certs, origin):
    async with Stack(kind, tunnel_certs) as s:
        await s.browser.get(_url(origin, "/wiki/B"))
        (origin.root / "wiki" / "B.html").write_bytes(b"<html>B, edited</html>")
        r = await s.browser.get(_url(origin, "/wiki/B"))
        assert r.content == b"<html>B, edited</html>"
        assert r.headers[SOURCE_HEADER] == "origin"


async def test_not_found_passes_through(kind, tunnel_certs, origin):
    async with Stack(kind, tunnel_certs) as s:
        r = await s.browser.get(_url(origin, "/wiki/Nope"))
        assert r.status_code == 404
        assert not s.cache.peek(_url(origin, "/wiki/Nope"))


async def test_pushed_page_served_without_tunnel(kind, tunnel_certs, origin):
    async with Stack(kind, tunnel_certs) as s:
        await s.browser.get(_url(origin, "/wiki/A"))  # opens the session on the server
        url_b = _url(origin, "/wiki/B")
        push = Frame(
            MsgType.PUSH,
            {"url": url_b, "prob": 0.8, "headers": [["Content-Type", "text/html"]]},
            PAGE_B,
        )
        await s.server.sessions[-1].push(push)
        for _ in range(50):
            if s.cache.peek(url_b):
                break
            await asyncio.sleep(0.01)

        s.link_up.clear()  # the push must be usable even with the link down
        r = await s.browser.get(url_b)
        assert r.content == PAGE_B and r.headers[SOURCE_HEADER] == "push"
        assert s.cache.stats.push_hits == 1
        assert ("/wiki/B", 200) not in origin.log


async def test_outage_serves_stale_or_504(kind, tunnel_certs, origin):
    async with Stack(kind, tunnel_certs, request_timeout=0.5, revalidate_timeout=0.3) as s:
        await s.browser.get(_url(origin, "/wiki/A"))
        s.link_up.clear()
        r = await s.browser.get(_url(origin, "/wiki/A"))
        assert r.content == PAGE_A and r.headers[SOURCE_HEADER] == "stale"
        r = await s.browser.get(_url(origin, "/wiki/B"))
        assert r.status_code == 504


async def test_tcp_request_survives_reconnect(tunnel_certs, origin):
    """Config 1 has no migration: an interface change kills the connection and the client
    proxy retries on the new one."""
    async with Stack("tcp", tunnel_certs) as s:
        await s.browser.get(_url(origin, "/wiki/A"))
        s.link_up.clear()
        pending = asyncio.ensure_future(s.proxy.fetch("GET", _url(origin, "/wiki/B")))
        await asyncio.sleep(0.1)
        await s.transport.migrate(("127.0.0.1", 0))
        s.link_up.set()
        reply = await asyncio.wait_for(pending, 5)
        assert reply.status == 200 and reply.body == PAGE_B
        assert s.transport.connects == 2
        assert len({sess.peer_addr for sess in s.server.sessions}) == 2


class FakePredictor:
    """Fixed probabilities by link target, standing in for Jev (no API calls)."""

    def __init__(self, by_target, fail=False):
        self.by_target = by_target
        self.fail = fail
        self.states = []

    async def predict(self, state):
        self.states.append(state)
        if self.fail:
            raise RuntimeError("model down")
        return {link.url: self.by_target.get(link.target, 0.0) for link in state.candidates}


async def _wait_for(cond, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_predicted_page_is_pushed_and_served_from_cache(kind, tunnel_certs, origin):
    predictor = FakePredictor({"B": 0.8, "C": 0.1})
    async with Stack(kind, tunnel_certs, ServerProxy(predictor=predictor)) as s:
        r = await s.browser.get(_url(origin, "/wiki/A"))
        assert r.content == PAGE_A
        url_b, url_c = _url(origin, "/wiki/B"), _url(origin, "/wiki/C")
        assert await _wait_for(lambda: s.cache.peek(url_b) is not None)
        assert s.cache.peek(url_c) is None  # 0.1 is below the normal threshold
        assert predictor.states[0].title == "A"

        s.link_up.clear()  # served from the push, the tunnel isn't needed
        r = await s.browser.get(url_b)
        assert r.content == PAGE_B and r.headers[SOURCE_HEADER] == "push"


async def test_handover_hint_pushes_more(kind, tunnel_certs, origin):
    predictor = FakePredictor({"B": 0.8, "C": 0.1})
    async with Stack(kind, tunnel_certs, ServerProxy(predictor=predictor)) as s:
        hint = Frame(MsgType.HANDOVER_HINT, {"active": True, "eta_s": 4.0, "outage_s": 45.0})
        await s.transport.send(hint)
        await asyncio.sleep(0.1)
        await s.browser.get(_url(origin, "/wiki/A"))
        assert await _wait_for(lambda: s.cache.peek(_url(origin, "/wiki/C")) is not None)
        assert s.cache.peek(_url(origin, "/wiki/B")) is not None


async def test_failing_predictor_does_not_break_browsing(kind, tunnel_certs, origin):
    predictor = FakePredictor({}, fail=True)
    async with Stack(kind, tunnel_certs, ServerProxy(predictor=predictor)) as s:
        r = await s.browser.get(_url(origin, "/wiki/A"))
        assert r.status_code == 200 and r.content == PAGE_A
        assert await _wait_for(lambda: predictor.states)
        await asyncio.sleep(0.1)
        assert len(s.cache) == 1  # only the page itself


async def test_reading_a_pushed_page_keeps_the_server_predicting(kind, tunnel_certs, origin):
    """Opening a pushed page is reported to the server, which then predicts from that page."""
    (origin.root / "wiki" / "B.html").write_bytes(
        b"<html><head><title>B</title></head><body><a href='/wiki/C'>C</a></body></html>"
    )
    predictor = FakePredictor({"B": 0.9, "C": 0.9})
    async with Stack(kind, tunnel_certs, ServerProxy(predictor=predictor)) as s:
        await s.browser.get(_url(origin, "/wiki/A"))
        url_b = _url(origin, "/wiki/B")
        assert await _wait_for(lambda: s.cache.peek(url_b) is not None)
        r = await s.browser.get(url_b)
        assert r.headers[SOURCE_HEADER] == "push"
        assert await _wait_for(lambda: len(predictor.states) == 2)
        assert predictor.states[1].title == "B"
        assert predictor.states[1].history == ["A"]


async def test_revalidated_page_counts_as_viewed(kind, tunnel_certs, origin):
    predictor = FakePredictor({})
    async with Stack(kind, tunnel_certs, ServerProxy(predictor=predictor)) as s:
        await s.browser.get(_url(origin, "/wiki/A"))
        r = await s.browser.get(_url(origin, "/wiki/A"))
        assert r.headers[SOURCE_HEADER] == "revalidated"
        assert await _wait_for(lambda: len(predictor.states) == 2)
        assert predictor.states[1].history == ["A"]


async def test_oracle_prefetch_is_served_instantly_later(kind, tunnel_certs, origin):
    from edgeproxy.client.proxy import PREFETCH_HEADER

    async with Stack(kind, tunnel_certs) as s:
        url_b = _url(origin, "/wiki/B")
        await s.browser.get(url_b, headers={PREFETCH_HEADER: "1"})
        s.link_up.clear()
        r = await s.browser.get(url_b)
        assert r.content == PAGE_B and r.headers[SOURCE_HEADER] == "push"


async def test_hint_replans_pushes_for_the_current_page(kind, tunnel_certs, origin):
    predictor = FakePredictor({"B": 0.8, "C": 0.1})
    async with Stack(kind, tunnel_certs, ServerProxy(predictor=predictor)) as s:
        await s.browser.get(_url(origin, "/wiki/A"))
        url_b, url_c = _url(origin, "/wiki/B"), _url(origin, "/wiki/C")
        assert await _wait_for(lambda: s.cache.peek(url_b) is not None)
        assert s.cache.peek(url_c) is None
        # No new page: the warning alone makes the server push C for the page the reader is on.
        await s.transport.send(Frame(MsgType.HANDOVER_HINT, {"active": True, "outage_s": 45.0}))
        assert await _wait_for(lambda: s.cache.peek(url_c) is not None)


async def test_history_comes_from_the_client_in_visit_order(kind, tunnel_certs, origin):
    for page in ("B", "C"):
        (origin.root / "wiki" / f"{page}.html").write_bytes(
            f"<html><head><title>{page}</title></head><body><a href='/wiki/A'>A</a></body>".encode()
        )
    predictor = FakePredictor({})
    async with Stack(kind, tunnel_certs, ServerProxy(predictor=predictor)) as s:
        for page in ("A", "B", "C"):
            await s.browser.get(_url(origin, f"/wiki/{page}"))
            await asyncio.sleep(0.05)
        assert await _wait_for(lambda: len(predictor.states) == 3)
        assert [st.history for st in predictor.states] == [[], ["A"], ["A", "B"]]


def _link_page(title, *targets):
    links = " ".join(f"<a href='/wiki/{t}'>{t}</a>" for t in targets)
    return f"<html><head><title>{title}</title></head><body>{links}</body></html>".encode()


@pytest.mark.parametrize("outage_s", [45.0, 5.0])
async def test_long_outage_pushes_two_clicks_deep(kind, tunnel_certs, origin, outage_s):
    """A -> {B, C}; B -> D; C -> E. During a long outage the reader can't ask for more, so the
    server also pushes the likely links of B and C. A short outage stays one click deep."""
    wiki = origin.root / "wiki"
    wiki.joinpath("B.html").write_bytes(_link_page("B", "D"))
    wiki.joinpath("C.html").write_bytes(_link_page("C", "E"))
    wiki.joinpath("D.html").write_bytes(_link_page("D"))
    wiki.joinpath("E.html").write_bytes(_link_page("E"))
    predictor = FakePredictor({"B": 0.8, "C": 0.1, "D": 0.9, "E": 0.05})
    async with Stack(kind, tunnel_certs, ServerProxy(predictor=predictor)) as s:
        hint = Frame(MsgType.HANDOVER_HINT, {"active": True, "outage_s": outage_s})
        await s.transport.send(hint)
        await asyncio.sleep(0.1)
        await s.browser.get(_url(origin, "/wiki/A"))
        url = {t: _url(origin, f"/wiki/{t}") for t in "BCDE"}
        assert await _wait_for(lambda: s.cache.peek(url["C"]) is not None)
        if outage_s == 45.0:
            assert await _wait_for(
                lambda: s.cache.peek(url["D"]) is not None and s.cache.peek(url["E"]) is not None
            )
            deep = {st.title: st.history for st in predictor.states[1:]}
            assert deep == {"B": ["A"], "C": ["A"]}  # asked as if the reader had opened them
        else:
            await asyncio.sleep(0.3)
            assert s.cache.peek(url["D"]) is None and len(predictor.states) == 1


async def test_no_pushes_once_the_warned_dropout_is_due(kind, tunnel_certs, origin):
    """Pushes sent into a dead link jam the connection after it returns, so they stop in time."""
    predictor = FakePredictor({"B": 0.8, "C": 0.1})
    async with Stack(kind, tunnel_certs, ServerProxy(predictor=predictor)) as s:
        # The link is about to drop right now (eta 0): nothing may be pushed any more.
        hint = {"active": True, "eta_s": 0.0, "outage_s": 45.0}
        await s.transport.send(Frame(MsgType.HANDOVER_HINT, hint))
        await asyncio.sleep(0.1)
        await s.browser.get(_url(origin, "/wiki/A"))
        await asyncio.sleep(0.3)
        assert len(s.cache) == 1  # only the page itself

        # Warning cleared (link back): pushing resumes, with the normal threshold again.
        await s.transport.send(Frame(MsgType.HANDOVER_HINT, {"active": False}))
        await asyncio.sleep(0.1)
        await s.browser.get(_url(origin, "/wiki/A"))
        assert await _wait_for(lambda: s.cache.peek(_url(origin, "/wiki/B")) is not None)
        assert s.cache.peek(_url(origin, "/wiki/C")) is None
