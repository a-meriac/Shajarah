"""Client proxy: a local plain-HTTP forward proxy that sends every request through the tunnel.

What it answers from:
- "push": a prefetched copy that hasn't been read yet is served straight from the cache.
- "revalidated": a cached copy the origin confirmed with 304 (only headers crossed the tunnel).
- "stale": a cached copy served because the tunnel didn't answer within `revalidate_timeout`.
  This is what keeps pages readable during an outage.
- "origin": a full response through the tunnel.

HTTPS (CONNECT) isn't handled here. The live browser demo puts mitmproxy in front for that.

  python -m edgeproxy.client.proxy --transport quic --server 10.9.0.2 --local-ip 10.1.0.2
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from edgeproxy.client.cache import Cache, Entry
from edgeproxy.common.eventlog import NULL_LOG, EventLog
from edgeproxy.common.http import (
    REQUEST_DROP,
    RESPONSE_DROP,
    Headers,
    canonical_url,
    filter_headers,
    get_header,
    reason,
)
from edgeproxy.common.protocol import Frame, MsgType, set_compression
from edgeproxy.tunnel.transport import ClientTransport

SOURCE_HEADER = "X-Edgeproxy-Source"


@dataclass
class Reply:
    status: int
    headers: Headers
    body: bytes
    source: str  # origin | push | revalidated | stale | error


def _cacheable(method: str, status: int, headers: Headers) -> bool:
    cache_control = (get_header(headers, "cache-control") or "").lower()
    return (
        method == "GET"
        and status == 200
        and "no-store" not in cache_control
        and get_header(headers, "set-cookie") is None
    )


def _entry_headers(entry: Entry) -> Headers:
    return list(entry.headers.items())


class ClientProxy:
    def __init__(
        self,
        transport: ClientTransport,
        cache: Cache,
        log: EventLog = NULL_LOG,
        request_timeout: float = 60.0,
        revalidate_timeout: float = 2.0,
        retry_delay: float = 0.2,
    ) -> None:
        self.transport = transport
        self.cache = cache
        self.log = log
        self.request_timeout = request_timeout
        self.revalidate_timeout = revalidate_timeout
        self.retry_delay = retry_delay
        self.port: int | None = None
        self._server: asyncio.Server | None = None
        transport.on_push = self._on_push

    # ------------------------------------------------------------------------------- tunnel side

    async def _on_push(self, frame: Frame) -> None:
        if frame.type is not MsgType.PUSH:
            return
        headers = frame.headers.get("headers", [])
        url = frame.headers["url"]
        self.cache.put(
            Entry(
                url=url,
                body=frame.body,
                headers={k.lower(): v for k, v in headers},
                etag=get_header(headers, "etag"),
                last_modified=get_header(headers, "last-modified"),
                pushed=True,
                prob=frame.headers.get("prob"),
            )
        )
        self.log.emit("push_recv", url=url, bytes=len(frame.body), prob=frame.headers.get("prob"))

    async def _notify_viewed(self, url: str) -> None:
        try:
            await asyncio.wait_for(
                self.transport.send(Frame(MsgType.VIEWED, {"url": url})), self.request_timeout
            )
        except (OSError, TimeoutError):
            self.log.emit("viewed_not_sent", url=url)

    async def _roundtrip(self, frame: Frame, timeout: float) -> Frame:
        """Send one request, retrying on a broken connection (TCP reconnect) until the deadline."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            try:
                return await asyncio.wait_for(self.transport.request(frame), remaining)
            except OSError:  # includes ConnectionError
                if deadline - loop.time() <= self.retry_delay:
                    raise
                await asyncio.sleep(self.retry_delay)

    async def fetch(self, method: str, url: str, headers: Headers = (), body: bytes = b"") -> Reply:
        start = time.monotonic()
        self.log.emit("req_start", url=url, method=method)
        reply = await self._fetch(method, url, filter_headers(headers, REQUEST_DROP), body)
        self.log.emit(
            "req_done",
            url=url,
            status=reply.status,
            bytes=len(reply.body),
            source=reply.source,
            from_cache=reply.source in ("push", "revalidated", "stale"),
            dur_s=time.monotonic() - start,
        )
        return reply

    async def _fetch(self, method: str, url: str, headers: Headers, body: bytes) -> Reply:
        entry = self.cache.peek(url) if method in ("GET", "HEAD") else None
        if entry is not None and entry.pushed and entry.hits == 0:
            self.cache.get(url)
            # The server never saw this request; tell it, so it predicts from here (and in the
            # background, since the link may be down).
            asyncio.ensure_future(self._notify_viewed(url))
            return Reply(200, _entry_headers(entry), entry.body, "push")

        timeout = self.request_timeout
        if entry is not None:
            timeout = self.revalidate_timeout
            if entry.etag and get_header(headers, "if-none-match") is None:
                headers = [*headers, ("If-None-Match", entry.etag)]
            if entry.last_modified and get_header(headers, "if-modified-since") is None:
                headers = [*headers, ("If-Modified-Since", entry.last_modified)]

        frame = Frame(MsgType.REQUEST, {"method": method, "url": url, "headers": headers}, body)
        try:
            answer = await self._roundtrip(frame, timeout)
        except (OSError, TimeoutError):
            if entry is not None:
                self.cache.get(url)
                return Reply(200, _entry_headers(entry), entry.body, "stale")
            return Reply(504, [("Content-Type", "text/plain")], b"tunnel unavailable\n", "error")

        status = answer.headers.get("status", 502)
        resp_headers = [tuple(h) for h in answer.headers.get("headers", [])]
        if answer.type is MsgType.NOT_MODIFIED:
            if entry is not None:
                self.cache.touch(url)
                return Reply(200, _entry_headers(entry), entry.body, "revalidated")
            return Reply(304, resp_headers, b"", "origin")  # the browser sent its own validators
        if _cacheable(method, status, resp_headers):
            self.cache.put(
                Entry(
                    url=url,
                    body=answer.body,
                    headers={k.lower(): v for k, v in resp_headers},
                    etag=get_header(resp_headers, "etag"),
                    last_modified=get_header(resp_headers, "last-modified"),
                )
            )
        return Reply(status, resp_headers, answer.body, "origin")

    # ------------------------------------------------------------------------------ browser side

    async def start(self, host: str = "127.0.0.1", port: int = 8118) -> None:
        self._server = await asyncio.start_server(self._on_connection, host, port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while await self._serve_one(reader, writer):
                pass
        except (asyncio.IncompleteReadError, OSError, ValueError):
            pass
        finally:
            writer.close()

    async def _serve_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """Serve one HTTP/1.1 request. Returns whether to keep the connection open."""
        line = await reader.readline()
        if not line:
            return False
        method, target, version = line.decode("latin-1").split()
        headers: Headers = []
        while (raw := await reader.readline()) not in (b"\r\n", b"\n", b""):
            name, _, value = raw.decode("latin-1").partition(":")
            headers.append((name.strip(), value.strip()))
        length = int(get_header(headers, "content-length") or 0)
        body = await reader.readexactly(length) if length else b""

        if not target.startswith("http://"):
            await self._write(writer, method, Reply(501, [], b"only plain HTTP\n", "error"), False)
            return False
        reply = await self.fetch(method, canonical_url(target), headers, body)
        keep = (
            version == "HTTP/1.1" and (get_header(headers, "connection") or "").lower() != "close"
        )
        await self._write(writer, method, reply, keep)
        return keep

    async def _write(
        self, writer: asyncio.StreamWriter, method: str, reply: Reply, keep: bool
    ) -> None:
        lines = [f"HTTP/1.1 {reply.status} {reason(reply.status)}"]
        lines += [f"{k}: {v}" for k, v in filter_headers(reply.headers, RESPONSE_DROP)]
        lines += [
            f"Content-Length: {len(reply.body)}",
            f"{SOURCE_HEADER}: {reply.source}",
            f"Connection: {'keep-alive' if keep else 'close'}",
        ]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        if method != "HEAD" and reply.status != 304:
            writer.write(reply.body)
        await writer.drain()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        await self.transport.close()


async def _run(args) -> None:
    from edgeproxy.common.settings import load_settings

    s = load_settings(args.settings)
    set_compression(s.tunnel.compress)
    cafile = args.certdir / "cert.pem"
    local = (args.local_ip, 0)
    if args.transport == "quic":
        from edgeproxy.tunnel.quic_tunnel import QuicClientTransport, client_configuration

        config = client_configuration(cafile, idle_timeout=s.tunnel.idle_timeout_s)
        transport = QuicClientTransport((args.server, args.port), config, local_addr=local)
    else:
        from edgeproxy.tunnel.tcp_transport import TcpClientTransport, client_ssl_context

        transport = TcpClientTransport(
            (args.server, args.port),
            client_ssl_context(cafile),
            local_addr=local,
            connect_timeout=s.tunnel.tcp_connect_timeout_s,
        )
    await transport.connect()
    proxy = ClientProxy(
        transport,
        Cache(s.client.cache_mb * 1_000_000),
        EventLog(args.log, "client", args.run_id),
        request_timeout=s.client.request_timeout_s,
        revalidate_timeout=s.client.revalidate_timeout_s,
        retry_delay=s.client.retry_delay_s,
    )
    await proxy.start(args.listen, args.listen_port)
    print(f"client proxy on {args.listen}:{proxy.port} -> {args.transport} tunnel", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=["quic", "tcp"], default="quic")
    ap.add_argument("--server", default="10.9.0.2")
    ap.add_argument("--port", type=int, default=4433)
    ap.add_argument("--local-ip", default="0.0.0.0", help="source address (picks the interface)")
    ap.add_argument("--certdir", type=Path, default=Path("/tmp/ep"))
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=8118)
    ap.add_argument("--settings", type=Path, default=None, help="default: settings.yaml")
    ap.add_argument("--log", type=Path, default=None, help="JSONL event log")
    ap.add_argument("--run-id", default="")
    asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    main()
