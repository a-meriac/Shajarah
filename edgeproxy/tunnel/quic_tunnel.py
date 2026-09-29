"""QUIC tunnel between client proxy and server proxy (aioquic).

Each request/response uses one bidirectional stream. Server pushes and client control frames use
unidirectional streams. Real-time traffic (e.g. the VoIP probe) uses QUIC DATAGRAM frames (RFC 9221).
HTTPS pass-through uses one bidirectional stream per connection: a CONNECT header frame, the
server's header-only RESPONSE frame back, then the browser's encrypted bytes both ways. Since
these are ordinary QUIC streams, they survive migration like everything else.

Migration: the client protocol object outlives its UDP socket. `migrate()` binds a new socket
(on the new interface's address), hands it to the same protocol, rotates the connection ID and
closes the old socket. The server validates the new path itself (PATH_CHALLENGE/RESPONSE), so no
new handshake happens. Unlike aioquic's `connect()`, which binds one socket for the life of the
connection, this lets the connection move between interfaces.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
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
    StreamReset,
)

from edgeproxy.common.protocol import Frame, MsgType, decode, split_head
from edgeproxy.tunnel.transport import (
    ByteStream,
    ClientTransport,
    ConnectHandler,
    DatagramHandler,
    PushHandler,
    RequestHandler,
    ServerSession,
)

log = logging.getLogger(__name__)

ALPN = "edgeproxy/1"
SERVER_NAME = "edgeproxy"
# Must outlast the longest outage we emulate (45 s, satellite gaps), or the connection dies
# during the gap and "migration" silently turns into a reconnect.
DEFAULT_IDLE_TIMEOUT = 120.0
MAX_DATAGRAM = 65536
# Silence after which a packet from the peer means "reachable again" (see _ReachabilityMixin).
REACHABLE_AGAIN_S = 1.0
# A pass-through stream's writer waits once this much of its data is unacknowledged.
STREAM_WINDOW = 1_000_000


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

    def pending(self, stream_id: int) -> bytes:
        return bytes(self._buffers.get(stream_id, b""))

    def drop(self, stream_id: int) -> None:
        self._buffers.pop(stream_id, None)


class _QuicStream(ByteStream):
    """A pass-through stream on one QUIC connection (either end)."""

    def __init__(self, protocol: QuicConnectionProtocol, stream_id: int) -> None:
        self._protocol, self.stream_id = protocol, stream_id
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._head = b""  # read before the queue: bytes that arrived with the header
        self._eof = False
        self._closed = False  # we reset it, or the connection ended

    def feed(self, data: bytes, end: bool = False) -> None:
        if data:
            self._chunks.put_nowait(data)
        if end:
            self._chunks.put_nowait(b"")

    def unread(self, data: bytes) -> None:
        self._head = data + self._head

    async def read(self) -> bytes:
        if self._head:
            data, self._head = self._head, b""
            return data
        if self._eof:
            return b""
        data = await self._chunks.get()
        self._eof = not data
        return data

    def _send(self, data: bytes, end: bool = False) -> None:
        if self._closed:
            raise ConnectionError("stream closed")
        try:
            self._protocol._quic.send_stream_data(self.stream_id, data, end_stream=end)
        except (ValueError, AssertionError) as e:  # aioquic: a finished or reset stream
            raise ConnectionError(f"stream closed: {e}") from e
        self._protocol.transmit()

    def write(self, data: bytes) -> None:
        self._send(data)

    def write_eof(self) -> None:
        self._send(b"", end=True)

    def _unacked(self) -> int:
        # aioquic has no public API for this; checked against 1.3 (as backlog_bytes).
        stream = self._protocol._quic._streams.get(self.stream_id)
        return len(stream.sender._buffer) if stream is not None else 0

    async def drain(self) -> None:
        while not self._closed and self._unacked() > STREAM_WINDOW:
            await asyncio.sleep(0.01)

    def fail(self) -> None:
        """The peer reset the stream or the connection ended: reads see the end."""
        self._closed = True
        self.feed(b"", end=True)

    def abort(self) -> None:
        if not self._closed:
            with contextlib.suppress(ValueError, AssertionError):  # already finished or reset
                self._protocol._quic.reset_stream(self.stream_id, 0)
                self._protocol.transmit()
        self.fail()


# ---------------------------------------------------------------------------------------- client


class _ReachabilityMixin:
    """Probe at once when the peer is heard from again after an outage.

    With data unacknowledged, QUIC waits one probe timeout before probing, doubling the wait after
    every unanswered probe (RFC 9002 6.2.1). After a 45 s outage that wait has grown to ~30 s,
    and nothing new may be sent until a probe is acknowledged, so the connection stays silent
    long after the link is back. The RFC resets the doubling on an acknowledgement; we also reset
    it when any packet arrives after a second of silence, since the peer is evidently reachable.
    aioquic has no API for this: _loss._pto_count is internal (checked against 1.3).
    """

    _last_heard = 0.0

    def datagram_received(self, data, addr) -> None:
        now = time.monotonic()
        loss = self._quic._loss
        if now - self._last_heard > REACHABLE_AGAIN_S and loss._pto_count:
            log.info("peer reachable again: probe timeout backoff %d reset", loss._pto_count)
            loss._pto_count = 0  # the timer is re-armed by transmit() after receiving
        self._last_heard = now
        super().datagram_received(data, addr)


class _ClientProtocol(_ReachabilityMixin, QuicConnectionProtocol):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._assembler = _StreamAssembler()
        self._pending: dict[int, asyncio.Future[Frame]] = {}
        self._streams: dict[int, _QuicStream] = {}
        self.on_push: PushHandler | None = None
        self.on_datagram: DatagramHandler | None = None
        self.terminated = asyncio.Event()

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, StreamDataReceived) and event.stream_id in self._streams:
            stream = self._streams[event.stream_id]
            if event.end_stream:  # nothing more will arrive on it
                del self._streams[event.stream_id]
            stream.feed(event.data, event.end_stream)
        elif isinstance(event, StreamReset) and event.stream_id in self._streams:
            self._streams.pop(event.stream_id).fail()
        elif isinstance(event, StreamDataReceived):
            payload = self._assembler.feed(event)
            if payload is None:
                return
            try:
                frame = decode(payload)
            except ValueError:
                log.warning("dropped a malformed frame on stream %d", event.stream_id)
                return
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
            for stream in self._streams.values():
                stream.fail()
            self._streams.clear()

    def open_request(self, data: bytes) -> asyncio.Future[Frame]:
        stream_id = self._quic.get_next_available_stream_id()
        waiter = asyncio.get_running_loop().create_future()
        self._pending[stream_id] = waiter
        self._quic.send_stream_data(stream_id, data, end_stream=True)
        self.transmit()
        return waiter

    async def open_stream(self, frame: Frame) -> _QuicStream:
        stream_id = self._quic.get_next_available_stream_id()
        stream = self._streams[stream_id] = _QuicStream(self, stream_id)
        stream.write(frame.encode())
        buf = b""
        while (head := split_head(buf)) is None:
            chunk = await stream.read()
            if not chunk:
                raise ConnectionError("tunnel stream closed before the server answered")
            buf += chunk
        reply, rest = head
        if reply.headers.get("status") != 200:
            stream.abort()  # stays routed until the server's end of stream arrives
            raise ConnectionError(f"server could not connect: {reply.headers.get('status')}")
        stream.unread(rest)
        return stream

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
        if self._protocol is not None and self._protocol._transport is not None:
            self._protocol._transport.close()  # reconnecting: drop the dead connection's socket
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

    async def open_stream(self, frame: Frame) -> ByteStream:
        return await self._require().open_stream(frame)

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


class _ServerProtocol(_ReachabilityMixin, QuicConnectionProtocol, ServerSession):
    def __init__(
        self,
        *args,
        handler: RequestHandler,
        on_datagram: DatagramHandler | None,
        on_connect: ConnectHandler | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._assembler = _StreamAssembler()
        self._handler = handler
        self._on_datagram = on_datagram
        self._on_connect = on_connect
        self._streams: dict[int, _QuicStream] = {}
        self._framed: set[int] = set()  # streams known to carry one whole frame, not CONNECT
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
            sid = event.stream_id
            if sid in self._streams:
                stream = self._streams[sid]
                if event.end_stream:  # nothing more will arrive on it
                    del self._streams[sid]
                stream.feed(event.data, event.end_stream)
                return
            payload = self._assembler.feed(event)
            if payload is None:
                self._maybe_pass_through(sid)
                return
            self._framed.discard(sid)
            try:
                frame = decode(payload)
            except ValueError:
                log.warning("dropped a malformed frame on stream %d", sid)
                return
            if frame.type is MsgType.CONNECT:  # header and end of stream in one go
                self._open_pass_through(sid, frame, b"", end=True)
            else:
                asyncio.ensure_future(self._dispatch(sid, frame))
        elif isinstance(event, StreamReset) and event.stream_id in self._streams:
            self._streams.pop(event.stream_id).fail()
        elif isinstance(event, ConnectionTerminated):
            for stream in self._streams.values():
                stream.fail()
            self._streams.clear()
        elif isinstance(event, DatagramFrameReceived) and self._on_datagram is not None:
            self._on_datagram(event.data)

    def _maybe_pass_through(self, stream_id: int) -> None:
        """Once a client stream's header has arrived, switch CONNECT streams to relaying."""
        if stream_id in self._framed or not _is_bidirectional(stream_id):
            return
        head = split_head(self._assembler.pending(stream_id))
        if head is None:
            return
        frame, rest = head
        if frame.type is MsgType.CONNECT:
            self._assembler.drop(stream_id)
            self._open_pass_through(stream_id, frame, rest)
        else:
            self._framed.add(stream_id)

    def _open_pass_through(self, stream_id: int, frame: Frame, rest: bytes, end=False) -> None:
        stream = _QuicStream(self, stream_id)
        if not end:
            self._streams[stream_id] = stream
        stream.feed(rest, end)
        if self._on_connect is None:
            stream.write(Frame(MsgType.RESPONSE, {"status": 501}).encode())
            stream.write_eof()
            return
        asyncio.ensure_future(self._on_connect(frame, stream))

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
        on_connect: ConnectHandler | None = None,
    ) -> None:
        self.host, self.port = host, port
        self.configuration = configuration
        self.handler = handler
        self.on_datagram = on_datagram
        self.on_connect = on_connect
        self._server: QuicServer | None = None
        self.sessions: list[_ServerProtocol] = []

    def _create_protocol(self, *args, **kwargs) -> _ServerProtocol:
        protocol = _ServerProtocol(
            *args,
            handler=self.handler,
            on_datagram=self.on_datagram,
            on_connect=self.on_connect,
            **kwargs,
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
