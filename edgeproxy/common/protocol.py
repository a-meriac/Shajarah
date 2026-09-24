"""Tunnel wire format shared by the QUIC and TCP transports.

Every message is one frame: 4-byte big-endian header length, a JSON header, then the raw body.
A request/response pair travels on one bidirectional stream; server pushes use their own streams.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from enum import Enum

_LEN = struct.Struct("!I")


class MsgType(str, Enum):
    REQUEST = "request"  # client -> server: fetch a URL (may carry validators)
    RESPONSE = "response"  # server -> client: full response
    NOT_MODIFIED = "not_modified"  # server -> client: origin said 304, keep your cached copy
    PUSH = "push"  # server -> client: predicted page/asset, unsolicited
    HANDOVER_HINT = "handover_hint"  # client -> server: outage predicted in eta_s seconds
    BUDGET = "budget"  # client -> server: current prefetch byte budget / threshold


@dataclass
class Frame:
    type: MsgType
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def encode(self) -> bytes:
        header = json.dumps({"type": self.type.value, **self.headers}, separators=(",", ":"))
        header_bytes = header.encode()
        return _LEN.pack(len(header_bytes)) + header_bytes + self.body


def decode(data: bytes) -> Frame:
    if len(data) < _LEN.size:
        raise ValueError("frame too short")
    (hlen,) = _LEN.unpack_from(data)
    end = _LEN.size + hlen
    if len(data) < end:
        raise ValueError("truncated frame header")
    headers = json.loads(data[_LEN.size : end])
    msg_type = MsgType(headers.pop("type"))
    return Frame(msg_type, headers, data[end:])
