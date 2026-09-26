"""Day-1 spike, kept as a regression test: does the tunnel survive a client address change?

On loopback we can only change the source *port* (NAT-rebinding style). The server still sees a
new peer address and must validate the path, which is the same code path as an interface change.
The netns version (real interface switch) lives in tests/emulation/.
"""

import asyncio

from edgeproxy.common.protocol import Frame, MsgType
from edgeproxy.tunnel.quic_tunnel import (
    QuicClientTransport,
    QuicTunnelServer,
    client_configuration,
    server_configuration,
)


async def _start(tunnel_certs, handler, on_datagram=None):
    cert, key = tunnel_certs
    server = QuicTunnelServer(
        "127.0.0.1", 0, server_configuration(cert, key), handler, on_datagram=on_datagram
    )
    await server.start()
    client = QuicClientTransport(
        ("127.0.0.1", server.port), client_configuration(cert), local_addr=("127.0.0.1", 0)
    )
    return server, client


async def test_inflight_request_survives_migration(tunnel_certs):
    release = asyncio.Event()

    async def handler(frame, session):
        await release.wait()  # hold the response until after the client has moved
        size = frame.headers["size"]
        return Frame(MsgType.RESPONSE, {"status": 200}, b"x" * size)

    server, client = await _start(tunnel_certs, handler)
    await client.connect()
    try:
        pending = asyncio.ensure_future(client.request(Frame(MsgType.REQUEST, {"size": 4_000_000})))
        await asyncio.sleep(0.05)
        await client.migrate(("127.0.0.1", 0))
        release.set()
        reply = await asyncio.wait_for(pending, 10)
        assert reply.type is MsgType.RESPONSE
        assert len(reply.body) == 4_000_000

        (session,) = server.sessions  # one connection, no second handshake
        assert len({addr[1] for addr in session.peer_addrs}) == 2
        assert client.migrations == 1
    finally:
        await client.close()
        server.close()


async def test_push_and_datagrams_after_migration(tunnel_certs):
    got_datagrams: list[bytes] = []
    echoed: list[bytes] = []
    pushes: list[Frame] = []

    async def handler(frame, session):
        await session.push(Frame(MsgType.PUSH, {"url": "http://o/next"}, b"page"))
        return Frame(MsgType.RESPONSE, {"status": 200}, b"ok")

    def on_server_datagram(data):
        got_datagrams.append(data)
        server.sessions[0].send_datagram(data)  # echo, like a voice call peer

    server, client = await _start(tunnel_certs, handler, on_datagram=on_server_datagram)

    async def on_push(frame):
        pushes.append(frame)

    client.on_push = on_push
    client.on_datagram = echoed.append
    await client.connect()
    try:
        await client.migrate(("127.0.0.1", 0))
        await client.request(Frame(MsgType.REQUEST, {}))
        for i in range(5):
            client.send_datagram(i.to_bytes(4, "big"))
        await asyncio.sleep(0.2)
        assert [f.headers["url"] for f in pushes] == ["http://o/next"]
        assert len(got_datagrams) == 5 and len(echoed) == 5
    finally:
        await client.close()
        server.close()


async def test_probe_backoff_resets_when_the_peer_is_heard_again(tunnel_certs):
    """After an outage the probe timeout has backed off to tens of seconds; the first packet
    from the peer afterwards must reset it on both ends."""

    async def handler(frame, session):
        return Frame(MsgType.RESPONSE, {"status": 200}, b"ok")

    server, client = await _start(tunnel_certs, handler)
    await client.connect()
    try:
        await client.request(Frame(MsgType.REQUEST, {}))
        (session,) = server.sessions
        for proto in (client._protocol, session):
            proto._quic._loss._pto_count = 9  # as after ~45 s of unanswered probes
            proto._last_heard -= 5  # and silence
        await client.request(Frame(MsgType.REQUEST, {}))
        assert session._quic._loss._pto_count == 0
        assert client._protocol._quic._loss._pto_count == 0
    finally:
        await client.close()
        server.close()
