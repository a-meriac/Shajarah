"""Transport interface so the proxies don't care whether they run over QUIC or TCP."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from edgeproxy.common.protocol import Frame

PushHandler = Callable[[Frame], Awaitable[None]]
DatagramHandler = Callable[[bytes], None]
PIPE_CHUNK = 65536


class ByteStream(ABC):
    """A two-way byte stream through the tunnel, for HTTPS pass-through. The bytes are the
    browser's own TLS records: neither proxy can read them."""

    @abstractmethod
    async def read(self) -> bytes:
        """The next chunk, or b"" once the other end has finished sending (or the stream died)."""

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None:
        """Wait while too much written data is still unacknowledged."""

    @abstractmethod
    def write_eof(self) -> None: ...

    @abstractmethod
    def abort(self) -> None: ...


async def pipe(stream: ByteStream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Copy bytes both ways between a tunnel stream and a TCP connection until both are done."""

    async def to_tunnel() -> None:
        while data := await reader.read(PIPE_CHUNK):
            stream.write(data)
            await stream.drain()
        stream.write_eof()

    async def from_tunnel() -> None:
        while data := await stream.read():
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()

    try:
        await asyncio.gather(to_tunnel(), from_tunnel())
    except (OSError, ConnectionError):
        stream.abort()
    finally:
        writer.close()


# Server side: called for each CONNECT with the stream; answers with a header-only RESPONSE frame
# (status 200 = connected) written to the stream, then relays.
ConnectHandler = Callable[[Frame, ByteStream], Awaitable[None]]


class ClientTransport(ABC):
    """Client end of the tunnel."""

    on_push: PushHandler | None = None
    on_datagram: DatagramHandler | None = None

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def request(self, frame: Frame) -> Frame:
        """Send one frame and wait for the single reply frame."""

    @abstractmethod
    async def send(self, frame: Frame) -> None:
        """Fire-and-forget control frame (HANDOVER_HINT, VIEWED)."""

    def send_datagram(self, data: bytes) -> None:
        raise NotImplementedError("this transport has no unreliable datagrams")

    async def migrate(self, local_addr: tuple[str, int]) -> None:
        """Move the tunnel to a new local address (new interface) without a new handshake."""
        raise NotImplementedError

    async def open_stream(self, frame: Frame) -> ByteStream:
        """Send a CONNECT frame and return the stream once the server has connected (status 200);
        raises ConnectionError otherwise."""
        raise NotImplementedError("this transport has no pass-through streams")

    @abstractmethod
    async def close(self) -> None: ...


# Server side: called with each request frame, returns the reply frame. `push` lets the handler
# send unsolicited frames back to the same client.
RequestHandler = Callable[[Frame, "ServerSession"], Awaitable[Frame | None]]


class ServerSession(ABC):
    """One connected client, as seen by the server proxy."""

    @abstractmethod
    async def push(self, frame: Frame) -> None: ...

    def send_datagram(self, data: bytes) -> None:
        raise NotImplementedError

    def backlog_bytes(self) -> int:
        """Bytes handed to the tunnel for this client and not yet acknowledged by it."""
        return 0
