"""QUIC tunnel between client proxy and server proxy (aioquic).

Each request/response uses one bidirectional stream. Server pushes and client control frames use
unidirectional streams. Real-time traffic (e.g. the VoIP probe) uses QUIC DATAGRAM frames (RFC 9221).

Migration: the client protocol object outlives its UDP socket. `migrate()` binds a new socket
(on the new interface's address), hands it to the same protocol, rotates the connection ID and
closes the old socket. The server validates the new path itself (PATH_CHALLENGE/RESPONSE), so no
new handshake happens. Unlike aioquic's `connect()`, which binds one socket for the life of the
connection, this lets the connection move between interfaces.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.asyncio.server import QuicServer
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import (
    ConnectionTerminated,
    DatagramFrameReceived,
    QuicEvent,
    StreamDataReceived,
)

from edgeproxy.common.protocol import Frame, decode
from edgeproxy.tunnel.transport import (
    ClientTransport,
    DatagramHandler,
    PushHandler,
    RequestHandler,
    ServerSession,
)

log = logging.getLogger(__name__)

ALPN = "edgeproxy/1"
SERVER_NAME = "edgeproxy"
# Must outlast the longest outage we emulate (car tunnel, satellite gap), or the connection dies
# during the gap and "migration" silently turns into a reconnect.
DEFAULT_IDLE_TIMEOUT = 120.0
MAX_DATAGRAM = 65536


def _is_client_initiated(stream_id: int) -> bool:
    return stream_id & 0x1 == 0


def _is_bidirectional(stream_id: int) -> bool:
    return stream_id & 0x2 == 0


class _StreamAssembler:
    """Buffers stream data until end_stream, then yields the whole payload."""

    def __init__(self) -> None:
        self._buffers: dict[int, bytearray] = {}

    def feed(self, event: StreamDataReceived) -> bytes | None:
        buf = self._buffers.setdefault(event.stream_id, bytearray())
        buf += event.data
        if event.end_stream:
            return bytes(self._buffers.pop(event.stream_id))
        return None


# ---------------------------------------------------------------------------------------- client


class _ClientProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._assembler = _StreamAssembler()
        self._pending: dict[int, asyncio.Future[Frame]] = {}
        self.on_push: PushHandler | None = None
        self.on_datagram: DatagramHandler | None = None
        self.terminated = asyncio.Event()

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, StreamDataReceived):
            payload = self._assembler.feed(event)
            if payload is None:
                return
            frame = decode(payload)
            waiter = self._pending.pop(event.stream_id, None)
            if waiter is not None:
                if not waiter.done():
                    waiter.set_result(frame)
            elif self.on_push is not None:
                asyncio.ensure_future(self.on_push(frame))
        elif isinstance(event, DatagramFrameReceived):
            if self.on_datagram is not None:
                self.on_datagram(event.data)
        elif isinstance(event, ConnectionTerminated):
            self.terminated.set()
            err = ConnectionError(f"tunnel closed: {event.reason_phrase or event.error_code}")
            for waiter in self._pending.values():
                if not waiter.done():
                    waiter.set_exception(err)
            self._pending.clear()

    def open_request(self, data: bytes) -> asyncio.Future[Frame]:
        stream_id = self._quic.get_next_available_stream_id()
        waiter = asyncio.get_running_loop().create_future()
        self._pending[stream_id] = waiter
        self._quic.send_stream_data(stream_id, data, end_stream=True)
        self.transmit()
        return waiter

    def send_oneway(self, data: bytes) -> None:
        stream_id = self._quic.get_next_available_stream_id(is_unidirectional=True)
        self._quic.send_stream_data(stream_id, data, end_stream=True)
        self.transmit()

    def send_datagram(self, data: bytes) -> None:
        self._quic.send_datagram_frame(data)
        self.transmit()


def client_configuration(
    cafile: Path, idle_timeout: float = DEFAULT_IDLE_TIMEOUT
) -> QuicConfiguration:
    config = QuicConfiguration(
        is_client=True,
        alpn_protocols=[ALPN],
        server_name=SERVER_NAME,
        idle_timeout=idle_timeout,
        max_datagram_frame_size=MAX_DATAGRAM,
    )
    config.load_verify_locations(cafile=str(cafile))
    return config


class QuicClientTransport(ClientTransport):
    def __init__(
        self,
        server_addr: tuple[str, int],
        configuration: QuicConfiguration,
        local_addr: tuple[str, int] = ("0.0.0.0", 0),
    ) -> None:
        self.server_addr = server_addr
        self.configuration = configuration
        self.local_addr = local_addr
        self._protocol: _ClientProtocol | None = None
        self.migrations = 0

    @property
    def connected(self) -> bool:
        return self._protocol is not None and not self._protocol.terminated.is_set()

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        connection = QuicConnection(configuration=self.configuration)
        _, protocol = await loop.create_datagram_endpoint(
            lambda: _ClientProtocol(connection), local_addr=self.local_addr
        )
        # Look the handlers up per event, so ones set after connect() (the client proxy's) apply.
        protocol.on_push = self._on_push
        protocol.on_datagram = self._on_datagram
        self._protocol = protocol
        protocol.connect(self.server_addr)
        await protocol.wait_connected()

    async def _on_push(self, frame: Frame) -> None:
        if self.on_push is not None:
            await self.on_push(frame)

    def _on_datagram(self, data: bytes) -> None:
        if self.on_datagram is not None:
            self.on_datagram(data)

    def _require(self) -> _ClientProtocol:
        if self._protocol is None:
            raise ConnectionError("tunnel not connected")
        return self._protocol

    async def request(self, frame: Frame) -> Frame:
        return await self._require().open_request(frame.encode())

    async def send(self, frame: Frame) -> None:
        self._require().send_oneway(frame.encode())

    def send_datagram(self, data: bytes) -> None:
        self._require().send_datagram(data)

    async def migrate(self, local_addr: tuple[str, int]) -> None:
        protocol = self._require()
        loop = asyncio.get_running_loop()
        old_transport = protocol._transport
        # create_datagram_endpoint calls protocol.connection_made(new_transport), which replaces
        # the socket the protocol sends on. QuicConnectionProtocol has no connection_lost, so
        # closing the old socket afterwards does not affect the connection.
        await loop.create_datagram_endpoint(lambda: protocol, local_addr=local_addr)
        protocol.change_connection_id()  # new CID so the new path is unlinkable; also transmits
        if old_transport is not None:
            old_transport.close()
        self.local_addr = local_addr
        self.migrations += 1
        log.info("migrated tunnel to %s", local_addr)

    async def close(self) -> None:
        if self._protocol is not None:
            self._protocol.close()
            await self._protocol.wait_closed()
            self._protocol = None


# ---------------------------------------------------------------------------------------- server


class _ServerProtocol(QuicConnectionProtocol, ServerSession):
    def __init__(
        self, *args, handler: RequestHandler, on_datagram: DatagramHandler | None, **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)
        self._assembler = _StreamAssembler()
        self._handler = handler
        self._on_datagram = on_datagram
        self.peer_addrs: list = []  # every source address seen, in order (proves migration)

    def backlog_bytes(self) -> int:
        # Each stream's send buffer holds everything from the first unacknowledged byte on
        # (sent or still queued). aioquic has no public API for this; checked against 1.3.
        return sum(len(stream.sender._buffer) for stream in self._quic._streams.values())

    def datagram_received(self, data, addr) -> None:
        if not self.peer_addrs or self.peer_addrs[-1] != addr:
            self.peer_addrs.append(addr)
        super().datagram_received(data, addr)

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, StreamDataReceived):
            payload = self._assembler.feed(event)
            if payload is not None:
                asyncio.ensure_future(self._dispatch(event.stream_id, decode(payload)))
        elif isinstance(event, DatagramFrameReceived) and self._on_datagram is not None:
            self._on_datagram(event.data)

    async def _dispatch(self, stream_id: int, frame: Frame) -> None:
        try:
            reply = await self._handler(frame, self)
        except Exception:
            log.exception("handler failed for %s", frame.type)
            reply = None
        if _is_client_initiated(stream_id) and _is_bidirectional(stream_id):
            data = reply.encode() if reply is not None else b""
            self._quic.send_stream_data(stream_id, data, end_stream=True)
            self.transmit()

    async def push(self, frame: Frame) -> None:
        stream_id = self._quic.get_next_available_stream_id(is_unidirectional=True)
        self._quic.send_stream_data(stream_id, frame.encode(), end_stream=True)
        self.transmit()

    def send_datagram(self, data: bytes) -> None:
        self._quic.send_datagram_frame(data)
        self.transmit()


def server_configuration(
    certfile: Path, keyfile: Path, idle_timeout: float = DEFAULT_IDLE_TIMEOUT
) -> QuicConfiguration:
    config = QuicConfiguration(
        is_client=False,
        alpn_protocols=[ALPN],
        idle_timeout=idle_timeout,
        max_datagram_frame_size=MAX_DATAGRAM,
    )
    config.load_cert_chain(str(certfile), str(keyfile))
    return config


class QuicTunnelServer:
    def __init__(
        self,
        host: str,
        port: int,
        configuration: QuicConfiguration,
        handler: RequestHandler,
        on_datagram: DatagramHandler | None = None,
    ) -> None:
        self.host, self.port = host, port
        self.configuration = configuration
        self.handler = handler
        self.on_datagram = on_datagram
        self._server: QuicServer | None = None
        self.sessions: list[_ServerProtocol] = []

    def _create_protocol(self, *args, **kwargs) -> _ServerProtocol:
        protocol = _ServerProtocol(
            *args, handler=self.handler, on_datagram=self.on_datagram, **kwargs
        )
        self.sessions.append(protocol)
        return protocol

    async def start(self) -> None:
        self._server = await serve(
            self.host,
            self.port,
            configuration=self.configuration,
            create_protocol=partial(self._create_protocol),
        )
        # Pick up the real port when started with port 0.
        self.port = self._server._transport.get_extra_info("sockname")[1]

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
