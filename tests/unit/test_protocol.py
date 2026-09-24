import os

import pytest

from edgeproxy.common.protocol import Frame, MsgType, decode, set_compression


def test_roundtrip():
    f = Frame(MsgType.PUSH, {"url": "http://a/b", "prob": 0.42}, b"\x00\xffbody")
    g = decode(f.encode())
    assert g == f


def test_truncated():
    data = Frame(MsgType.REQUEST, {"url": "x"}).encode()
    with pytest.raises(ValueError):
        decode(data[:6])


def test_html_is_compressed_on_the_wire():
    html = b"<p>Albert Einstein was a physicist.</p>" * 200
    f = Frame(MsgType.PUSH, {"url": "http://a/b"}, html)
    wire = f.encode()
    assert len(wire) < len(html) / 5
    assert decode(wire) == f


def test_incompressible_and_small_bodies_are_sent_as_is():
    noise = os.urandom(5000)
    for body in (noise, b"tiny"):
        wire = Frame(MsgType.RESPONSE, {}, body).encode()
        assert b'"enc"' not in wire and decode(wire).body == body


def test_compression_can_be_turned_off():
    html = b"<p>text</p>" * 500
    try:
        set_compression(False)
        wire = Frame(MsgType.PUSH, {}, html).encode()
    finally:
        set_compression(True)
    assert b'"enc"' not in wire and len(wire) > len(html)
