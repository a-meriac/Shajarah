"""TCP+TLS tunnel: the config-1 baseline, using the same frames as the QUIC tunnel.

Frames from many requests share one TCP connection, so each message carries a stream id:
8-byte stream id, 4-byte payload length, then the encoded Frame. Client requests use ids >= 1 and
the server replies with the same id. Id 0 is one-way: control frames from the client and pushes
from the server.

There is no migration. `migrate()` closes the connection and opens a new one from the new
address, so requests in flight fail with ConnectionError and the client proxy retries them. That
reconnect stall is exactly what config 1 measures.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import struct
from pathlib import Path

from edgeproxy.common.protocol import Frame, decode
from edgeproxy.tunnel.transport import ClientTransport, RequestHandler, ServerSession

log = logging.getLogger(__name__)

SERVER_NAME = "edgeproxy"
ONEWAY = 0
_HDR = struct.Struct("!QI")  # stream id, payload length


def _pack(stream_id: int, payload: bytes) -> bytes:
    return _HDR.pack(stream_id, len(payload)) + payload


async def _read_msg(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    stream_id, n = _HDR.unpack(await reader.readexactly(_HDR.size))
    return stream_id, await reader.readexactly(n)


def client_ssl_context(cafile: Path) -> ssl.SSLContext:
    return ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(cafile))


def server_ssl_context(certfile: Path, keyfile: Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(str(certfile), str(keyfile))
    return ctx


# ---------------------------------------------------------------------------------------- client


class _Connection:
    """One TCP connection and the requests waiting on it."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.pending: dict[int, asyncio.Future[Frame]] = {}
        self.next_id = 1
        self.closed = False

    def fail(self, err: Exception) -> None:
        self.closed = True
        for waiter in self.pending.values():
            if not waiter.done():
                waiter.set_exception(err)
        self.pending.clear()
        self.writer.close()


class TcpClientTransport(ClientTransport):
    def __init__(
        self,
        server_addr: tuple[str, int],
        ssl_context: ssl.SSLContext | None,
        local_addr: tuple[str, int] = ("0.0.0.0", 0),
        connect_timeout: float = 5.0,
    ) -> None:
        self.server_addr = server_addr
        self.ssl_context = ssl_context
        self.local_addr = local_addr
        self.connect_timeout = connect_timeout
        self._conn: _Connection | None = None
        self._reader_task: asyncio.Task | None = None
        self.connects = 0

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    async def connect(self) -> None:
        host, port = self.server_addr
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                ssl=self.ssl_context,
                server_hostname=SERVER_NAME if self.ssl_context else None,
                local_addr=self.local_addr,
            ),
            self.connect_timeout,
        )
        self._conn = _Connection(reader, writer)
        self._reader_task = asyncio.ensure_future(self._read_loop(self._conn))
        self.connects += 1

    async def _read_loop(self, conn: _Connection) -> None:
        try:
            while True:
                stream_id, payload = await _read_msg(conn.reader)
                frame = decode(payload)
                waiter = conn.pending.pop(stream_id, None)
                if waiter is not None:
                    if not waiter.done():
                        waiter.set_result(frame)
                elif stream_id == ONEWAY and self.on_push is not None:
                    asyncio.ensure_future(self.on_push(frame))
        except (asyncio.IncompleteReadError, OSError, ValueError):
            pass
        finally:
            conn.fail(ConnectionError("tunnel closed"))

    async def _ensure(self) -> _Connection:
        if not self.connected:
            await self.connect()
        assert self._conn is not None
        return self._conn

    async def request(self, frame: Frame) -> Frame:
        conn = await self._ensure()
        stream_id = conn.next_id
        conn.next_id += 1
        waiter = asyncio.get_running_loop().create_future()
        conn.pending[stream_id] = waiter
        conn.writer.write(_pack(stream_id, frame.encode()))
        return await waiter

    async def send(self, frame: Frame) -> None:
        conn = await self._ensure()
        conn.writer.write(_pack(ONEWAY, frame.encode()))

    async def migrate(self, local_addr: tuple[str, int]) -> None:
        """No migration in TCP: drop the connection and reconnect from the new address."""
        if self._conn is not None:
            self._conn.fail(ConnectionError("tunnel moved to a new interface"))
        self.local_addr = local_addr
        await self.connect()
        log.info("reconnected tunnel from %s", local_addr)

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.fail(ConnectionError("tunnel closed"))
            self._conn = None
        if self._reader_task is not None:
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None


# ---------------------------------------------------------------------------------------- server


class _TcpServerSession(ServerSession):
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.peer_addr = writer.get_extra_info("peername")
        self.closed = False

    def write(self, stream_id: int, payload: bytes) -> None:
        if not self.closed:
            self.writer.write(_pack(stream_id, payload))

    async def push(self, frame: Frame) -> None:
        self.write(ONEWAY, frame.encode())

    def backlog_bytes(self) -> int:
        return self.writer.transport.get_write_buffer_size()


class TcpTunnelServer:
    def __init__(
        self,
        host: str,
        port: int,
        ssl_context: ssl.SSLContext | None,
        handler: RequestHandler,
    ) -> None:
        self.host, self.port = host, port
        self.ssl_context = ssl_context
        self.handler = handler
        self._server: asyncio.Server | None = None
        self.sessions: list[_TcpServerSession] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connection, self.host, self.port, ssl=self.ssl_context
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        session = _TcpServerSession(writer)
        self.sessions.append(session)
        try:
            while True:
                stream_id, payload = await _read_msg(reader)
                asyncio.ensure_future(self._dispatch(session, stream_id, decode(payload)))
        except (asyncio.IncompleteReadError, OSError, ValueError):
            pass
        finally:
            session.closed = True
            writer.close()

    async def _dispatch(self, session: _TcpServerSession, stream_id: int, frame: Frame) -> None:
        try:
            reply = await self.handler(frame, session)
        except Exception:
            log.exception("handler failed for %s", frame.type)
            reply = None
        if stream_id != ONEWAY:
            session.write(stream_id, reply.encode() if reply is not None else b"")

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for session in self.sessions:
            session.closed = True
            session.writer.close()
