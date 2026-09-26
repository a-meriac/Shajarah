"""Tunnel wire format shared by the QUIC and TCP transports.

Every message is one frame: 4-byte big-endian header length, a JSON header, then the body.
A request/response pair travels on one bidirectional stream; server pushes use their own streams.

Bodies are deflate-compressed on the wire when that makes them meaningfully smaller (HTML shrinks
~5x; images don't, so they're sent as is). The header then carries "enc": "deflate", and decode()
restores the original body, so nothing above the tunnel sees compression.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field
from enum import Enum

_LEN = struct.Struct("!I")
HISTORY_LEN = 5  # pages of browsing history the client sends with each page view
COMPRESS_MIN_BYTES = 1024  # smaller bodies aren't worth it
COMPRESS_MIN_SAVING = 0.1  # send compressed only if it saves at least 10%
COMPRESS_LEVEL = 6
_compress = True


def set_compression(enabled: bool) -> None:
    """Turn body compression on or off for this process (off = ablation without compression)."""
    global _compress
    _compress = enabled


class MsgType(str, Enum):
    REQUEST = "request"  # client -> server: fetch a URL (may carry validators)
    RESPONSE = "response"  # server -> client: full response
    NOT_MODIFIED = "not_modified"  # server -> client: origin said 304, keep your cached copy
    PUSH = "push"  # server -> client: predicted page/asset, unsolicited
    HANDOVER_HINT = "handover_hint"  # client -> server: outage predicted in eta_s seconds
    PING = "ping"  # client -> server: liveness check, answered with an empty RESPONSE
    VIEWED = "viewed"  # client -> server: the reader opened this page from the cache
    NETWORK = "network"  # client -> server: the tunnel now runs over this kind of network


@dataclass
class Frame:
    type: MsgType
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    _wire: bytes | None = field(default=None, repr=False, compare=False)

    def encode(self) -> bytes:
        """Wire bytes, computed once (len(frame.encode()) is what the frame costs to send)."""
        if self._wire is None:
            headers, body = {"type": self.type.value, **self.headers}, self.body
            if _compress and len(body) >= COMPRESS_MIN_BYTES:
                packed = zlib.compress(body, COMPRESS_LEVEL)
                if len(packed) <= len(body) * (1 - COMPRESS_MIN_SAVING):
                    headers["enc"], body = "deflate", packed
            header_bytes = json.dumps(headers, separators=(",", ":")).encode()
            self._wire = _LEN.pack(len(header_bytes)) + header_bytes + body
        return self._wire


def decode(data: bytes) -> Frame:
    if len(data) < _LEN.size:
        raise ValueError("frame too short")
    (hlen,) = _LEN.unpack_from(data)
    end = _LEN.size + hlen
    if len(data) < end:
        raise ValueError("truncated frame header")
    headers = json.loads(data[_LEN.size : end])
    msg_type = MsgType(headers.pop("type"))
    body = data[end:]
    if headers.pop("enc", None) == "deflate":
        body = zlib.decompress(body)
    return Frame(msg_type, headers, body)
