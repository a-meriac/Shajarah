"""HTTPS pass-through: the browser's encrypted bytes cross the tunnel unchanged (CONNECT)."""

import asyncio
import ssl

import httpx
import pytest

from edgeproxy.client.cache import Cache
from edgeproxy.client.proxy import ClientProxy
from edgeproxy.common.protocol import Frame, MsgType
from edgeproxy.server.proxy import ServerProxy
from edgeproxy.tunnel.certs import generate_self_signed
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

PAGE = b"<html><title>secret</title>only the browser and the site can read this</html>"


@pytest.fixture(scope="module")
def site_certs(tmp_path_factory):
    d = tmp_path_factory.mktemp("site")
    cert, key = d / "cert.pem", d / "key.pem"
    generate_self_signed(cert, key, hostname="localhost")
    return cert, key


async def _https_site(site_certs):
    """A minimal HTTPS site on localhost that answers every request with PAGE."""
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(*map(str, site_certs))

    async def on_conn(reader, writer):
        while await reader.readline() not in (b"\r\n", b""):  # skip the request
            pass
        head = f"HTTP/1.1 200 OK\r\nContent-Length: {len(PAGE)}\r\nConnection: close\r\n\r\n"
        writer.write(head.encode() + PAGE)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(on_conn, "127.0.0.1", 0, ssl=ctx)


async def _echo_site():
    """Plain TCP echo, standing in for any site: pass-through doesn't care what the bytes are."""

    async def on_conn(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.close()

    return await asyncio.start_server(on_conn, "127.0.0.1", 0)


async def _quic(tunnel_certs, server_proxy):
    cert, key = tunnel_certs
    server = QuicTunnelServer(
        "127.0.0.1",
        0,
        server_configuration(cert, key),
        server_proxy.handle,
        on_connect=server_proxy.handle_connect,
    )
    await server.start()
    client = QuicClientTransport(
        ("127.0.0.1", server.port), client_configuration(cert), local_addr=("127.0.0.1", 0)
    )
    await client.connect()
    return server, client


async def test_https_page_through_both_proxies(tunnel_certs, site_certs):
    site = await _https_site(site_certs)
    port = site.sockets[0].getsockname()[1]
    server, transport = await _quic(tunnel_certs, ServerProxy())
    proxy = ClientProxy(transport, Cache(1_000_000))
    await proxy.start("127.0.0.1", 0)
    try:
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{proxy.port}",
            verify=ssl.create_default_context(cafile=str(site_certs[0])),
            trust_env=False,
            timeout=5,
        ) as browser:
            r = await browser.get(f"https://localhost:{port}/")
        assert r.status_code == 200 and r.content == PAGE
        assert len(proxy.cache) == 0  # encrypted: nothing the proxy could cache
    finally:
        await proxy.close()
        server.close()
        site.close()


async def test_pass_through_stream_survives_migration(tunnel_certs):
    site = await _echo_site()
    port = site.sockets[0].getsockname()[1]
    server, transport = await _quic(tunnel_certs, ServerProxy())
    try:
        stream = await transport.open_stream(
            Frame(MsgType.CONNECT, {"host": "127.0.0.1", "port": port})
        )
        stream.write(b"before ")
        assert await asyncio.wait_for(stream.read(), 2) == b"before "
        await transport.migrate(("127.0.0.1", 0))
        stream.write(b"after")
        assert await asyncio.wait_for(stream.read(), 2) == b"after"
        stream.write_eof()
        assert await asyncio.wait_for(stream.read(), 2) == b""  # the site closed its side too
        (session,) = server.sessions  # same connection, from two addresses
        assert len(session.peer_addrs) == 2
    finally:
        await transport.close()
        server.close()
        site.close()


async def test_large_transfer_both_ways(tunnel_certs):
    site = await _echo_site()
    port = site.sockets[0].getsockname()[1]
    server, transport = await _quic(tunnel_certs, ServerProxy())
    data = bytes(range(256)) * 20_000  # ~5 MB, more than the stream window
    try:
        stream = await transport.open_stream(
            Frame(MsgType.CONNECT, {"host": "127.0.0.1", "port": port})
        )

        async def send():
            for i in range(0, len(data), 65536):
                stream.write(data[i : i + 65536])
                await stream.drain()
            stream.write_eof()

        async def receive():
            got = bytearray()
            while chunk := await stream.read():
                got += chunk
            return bytes(got)

        _, echoed = await asyncio.wait_for(asyncio.gather(send(), receive()), 30)
        assert echoed == data
    finally:
        await transport.close()
        server.close()
        site.close()


async def test_only_allowed_ports(tunnel_certs, caplog):
    site = await _echo_site()
    port = site.sockets[0].getsockname()[1]
    server, transport = await _quic(tunnel_certs, ServerProxy(connect_ports=[443]))
    try:
        with pytest.raises(ConnectionError, match="403"):
            await transport.open_stream(Frame(MsgType.CONNECT, {"host": "127.0.0.1", "port": port}))
        # The tunnel itself is fine afterwards, and the refused stream's end caused no errors.
        await asyncio.sleep(0.1)
        reply = await transport.request(Frame(MsgType.PING))
        assert reply.headers["status"] == 204
        assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")]
    finally:
        await transport.close()
        server.close()
        site.close()


async def test_unreachable_site_is_502(tunnel_certs):
    server, transport = await _quic(tunnel_certs, ServerProxy(connect_timeout=1))
    try:
        with pytest.raises(ConnectionError, match="502"):
            await transport.open_stream(Frame(MsgType.CONNECT, {"host": "127.0.0.1", "port": 1}))
    finally:
        await transport.close()
        server.close()


async def test_tcp_tunnel_answers_connect_with_502(tunnel_certs):
    cert, key = tunnel_certs
    server_proxy = ServerProxy()
    server = TcpTunnelServer("127.0.0.1", 0, server_ssl_context(cert, key), server_proxy.handle)
    await server.start()
    transport = TcpClientTransport(("127.0.0.1", server.port), client_ssl_context(cert))
    await transport.connect()
    proxy = ClientProxy(transport, Cache(1_000_000))
    await proxy.start("127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n")
        await writer.drain()
        assert (await reader.readline()).startswith(b"HTTP/1.1 502")
        writer.close()
    finally:
        await proxy.close()
        server.close()
