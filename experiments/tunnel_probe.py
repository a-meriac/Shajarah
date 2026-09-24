"""Day-4 gate: does the QUIC tunnel survive a real interface switch inside the emulation?

  server:  ip netns exec ep-srv python -m experiments.tunnel_probe server --certdir /tmp/ep
  client:  ip netns exec ep-cli python -m experiments.tunnel_probe client --certdir /tmp/ep \
               --from 10.1.0.2 --to 10.2.0.2 --kill wifi0 --at 3 --duration 8

The client sends a small request every 50 ms. At --at seconds it kills --kill (100% loss via
netem) and migrates to --to. It prints the longest gap between completed requests: that is the
user-visible stall of a handover for this mechanism. Prints one JSON line.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from itertools import pairwise
from pathlib import Path

from edgeproxy.common.protocol import Frame, MsgType
from edgeproxy.tunnel.certs import generate_self_signed
from edgeproxy.tunnel.quic_tunnel import (
    QuicClientTransport,
    QuicTunnelServer,
    client_configuration,
    server_configuration,
)
from emulation.shaper import apply, load_profiles

PORT = 4433


async def serve(certdir: Path) -> None:
    certdir.mkdir(parents=True, exist_ok=True)
    cert, key = certdir / "cert.pem", certdir / "key.pem"
    if not cert.exists():
        generate_self_signed(cert, key)

    async def handler(frame, session):
        return Frame(MsgType.RESPONSE, {"status": 200}, b"x" * frame.headers.get("size", 1000))

    server = QuicTunnelServer("0.0.0.0", PORT, server_configuration(cert, key), handler)
    await server.start()
    print(f"tunnel probe server on :{PORT}", flush=True)
    await asyncio.Event().wait()


async def client(args) -> dict:
    cfg = client_configuration(args.certdir / "cert.pem")
    t = QuicClientTransport((args.server, PORT), cfg, local_addr=(args.from_ip, 0))
    await t.connect()
    start = time.monotonic()
    done: list[float] = []
    migrated_at = None

    async def switch():
        nonlocal migrated_at
        await asyncio.sleep(args.at)
        apply(args.kill, load_profiles()["dead"])
        if args.mode == "migrate":
            await t.migrate((args.to_ip, 0))
        migrated_at = time.monotonic() - start

    switcher = asyncio.ensure_future(switch())
    while time.monotonic() - start < args.duration:
        try:
            await asyncio.wait_for(t.request(Frame(MsgType.REQUEST, {"size": 1000})), 2.0)
            done.append(time.monotonic() - start)
        except (TimeoutError, ConnectionError):
            pass
        await asyncio.sleep(0.05)
    await switcher
    gaps = [b - a for a, b in pairwise(done)]
    return {
        "mode": args.mode,
        "requests_ok": len(done),
        "max_gap_s": max(gaps) if gaps else None,
        "migrated_at_s": migrated_at,
        "completed_after_switch": sum(1 for x in done if migrated_at and x > migrated_at),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("server")
    s.add_argument("--certdir", type=Path, default=Path("/tmp/ep"))
    c = sub.add_parser("client")
    c.add_argument("--certdir", type=Path, default=Path("/tmp/ep"))
    c.add_argument("--server", default="10.9.0.2")
    c.add_argument("--from", dest="from_ip", default="10.1.0.2")
    c.add_argument("--to", dest="to_ip", default="10.2.0.2")
    c.add_argument("--kill", default="wifi0")
    c.add_argument("--at", type=float, default=3.0)
    c.add_argument("--duration", type=float, default=8.0)
    c.add_argument("--mode", choices=["migrate", "none"], default="migrate")
    args = ap.parse_args()
    if args.cmd == "server":
        asyncio.run(serve(args.certdir))
    else:
        print(json.dumps(asyncio.run(client(args))))


if __name__ == "__main__":
    main()
