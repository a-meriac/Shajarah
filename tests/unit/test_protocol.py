import pytest

from edgeproxy.common.protocol import Frame, MsgType, decode


def test_roundtrip():
    f = Frame(MsgType.PUSH, {"url": "http://a/b", "prob": 0.42}, b"\x00\xffbody")
    g = decode(f.encode())
    assert g == f


def test_truncated():
    data = Frame(MsgType.REQUEST, {"url": "x"}).encode()
    with pytest.raises(ValueError):
        decode(data[:6])
