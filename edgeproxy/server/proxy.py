"""Server proxy: receives REQUEST frames from the tunnel and fetches them from the origin.

Link prediction and pushing come later (plan step 4). Control frames (HANDOVER_HINT, BUDGET)
are accepted and ignored for now.

  python -m edgeproxy.server.proxy --transport quic --port 4433 --certdir /tmp/ep
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import httpx

from edgeproxy.common.eventlog import NULL_LOG, EventLog
from edgeproxy.common.protocol import Frame, MsgType
from edgeproxy.server.fetcher import Fetcher
from edgeproxy.tunnel.transport import ServerSession


class ServerProxy:
    def __init__(self, fetcher: Fetcher | None = None, log: EventLog = NULL_LOG) -> None:
        self.fetcher = fetcher or Fetcher()
        self.log = log

    async def handle(self, frame: Frame, session: ServerSession) -> Frame | None:
        if frame.type is not MsgType.REQUEST:
            return None
        url = frame.headers["url"]
        method = frame.headers.get("method", "GET")
        start = time.monotonic()
        try:
            r = await self.fetcher.fetch(url, method, frame.headers.get("headers", []), frame.body)
        except httpx.HTTPError as e:
            self.log.emit("origin_error", url=url, error=type(e).__name__)
            msg = f"origin unreachable: {type(e).__name__}".encode()
            return Frame(MsgType.RESPONSE, {"status": 502, "headers": []}, msg)
        self.log.emit(
            "origin_fetch",
            url=url,
            status=r.status,
            bytes=len(r.body),
            dur_s=time.monotonic() - start,
        )
        if r.status == 304:
            return Frame(MsgType.NOT_MODIFIED, {"status": 304, "headers": r.headers})
        return Frame(MsgType.RESPONSE, {"status": r.status, "headers": r.headers}, r.body)


async def _serve(args) -> None:
    from edgeproxy.common.settings import load_settings
    from edgeproxy.tunnel.certs import generate_self_signed

    args.certdir.mkdir(parents=True, exist_ok=True)
    cert, key = args.certdir / "cert.pem", args.certdir / "key.pem"
    if not cert.exists():
        generate_self_signed(cert, key)
    s = load_settings(args.settings)
    proxy = ServerProxy(
        Fetcher(timeout=s.server.origin_timeout_s), EventLog(args.log, "server", args.run_id)
    )
    if args.transport == "quic":
        from edgeproxy.tunnel.quic_tunnel import QuicTunnelServer, server_configuration

        config = server_configuration(cert, key, idle_timeout=s.tunnel.idle_timeout_s)
        server = QuicTunnelServer(args.host, args.port, config, proxy.handle)
    else:
        from edgeproxy.tunnel.tcp_transport import TcpTunnelServer, server_ssl_context

        server = TcpTunnelServer(args.host, args.port, server_ssl_context(cert, key), proxy.handle)
    await server.start()
    print(f"server proxy ({args.transport}) on {args.host}:{server.port}", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=["quic", "tcp"], default="quic")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=4433)
    ap.add_argument("--certdir", type=Path, default=Path("/tmp/ep"))
    ap.add_argument("--log", type=Path, default=None, help="JSONL event log")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--settings", type=Path, default=None, help="default: settings.yaml")
    asyncio.run(_serve(ap.parse_args()))


if __name__ == "__main__":
    main()
