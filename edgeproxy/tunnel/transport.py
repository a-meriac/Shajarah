"""Transport interface so the proxies don't care whether they run over QUIC or TCP."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from edgeproxy.common.protocol import Frame

PushHandler = Callable[[Frame], Awaitable[None]]
DatagramHandler = Callable[[bytes], None]


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
