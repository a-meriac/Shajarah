"""Local origin: serves the frozen Wikipedia snapshot over plain HTTP, with validators.

/wiki/Albert_Einstein is served from <root>/wiki/Albert_Einstein.html. Every response carries an
ETag (content hash) and Last-Modified (file mtime), and conditional requests get 304, so the
revalidation path can be measured.

With --standins, an article outside the snapshot (/wiki/<anything> with no file) is answered with
a stand-in: the HTML of a real snapshot page, picked deterministically from the URL, marked with
X-Stand-In: 1. Readers never open those pages (sessions only visit snapshot pages), but the
server proxy does prefetch them, and a push must cost what a real page would, or prefetching
looks cheaper than it is. Stand-ins have real pages' sizes and compressibility.

  python -m data.origin_server --root data/snapshot --port 8080
"""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import sys
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


class OriginServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root: Path, addr: tuple[str, int], standins: bool = False) -> None:
        super().__init__(addr, _Handler)
        self.root = root.resolve()
        self.log: list[tuple[str, int]] = []  # (path, status), for tests
        self.standins = standin_files(self.root) if standins else []

    def handle_error(self, request, client_address) -> None:
        # A client hanging up mid-response (e.g. a cancelled prefetch) is normal, not an error.
        if isinstance(sys.exception(), ConnectionError):
            return
        super().handle_error(request, client_address)

    def resolve(self, url_path: str) -> Path | None:
        return resolve_file(self.root, url_path)

    def standin(self, url_path: str) -> Path | None:
        return standin_file(self.standins, url_path)


def resolve_file(root: Path, url_path: str) -> Path | None:
    """The snapshot file for a URL path, if there is one."""
    root = root.resolve()
    rel = unquote(url_path).lstrip("/")
    for candidate in (rel, rel + ".html", f"{rel.rstrip('/')}/index.html".lstrip("/")):
        path = (root / candidate).resolve()
        if path.is_relative_to(root) and path.is_file():
            return path
    return None


def standin_files(root: Path) -> list[Path]:
    return sorted((root.resolve() / "wiki").rglob("*.html"))


def standin_file(standins: list[Path], url_path: str) -> Path | None:
    """The page served in place of a missing article: picked from the URL, so always the same."""
    if not standins or not url_path.startswith("/wiki/"):
        return None
    digest = hashlib.sha256(unquote(url_path).encode()).digest()
    return standins[int.from_bytes(digest[:8], "big") % len(standins)]


def served_file(root: Path, url_path: str, standins: list[Path]) -> Path | None:
    """What the origin (run with --standins) answers for a URL path."""
    return resolve_file(root, url_path) or standin_file(standins, url_path)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: OriginServer

    def do_HEAD(self) -> None:
        self._respond(send_body=False)

    def do_GET(self) -> None:
        self._respond(send_body=True)

    def _respond(self, send_body: bool) -> None:
        url_path = urlsplit(self.path).path
        path = self.server.resolve(url_path)
        standin = False
        if path is None:
            path, standin = self.server.standin(url_path), True
        if path is None:
            self._send(404, [("Content-Type", "text/plain")], b"not found\n", send_body)
            return
        body = path.read_bytes()
        mtime = int(path.stat().st_mtime)
        etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        headers = [
            ("Content-Type", ctype),
            ("ETag", etag),
            ("Last-Modified", formatdate(mtime, usegmt=True)),
            ("Cache-Control", "no-cache"),  # always revalidate, like Wikipedia's HTML
        ]
        if standin:
            headers.append(("X-Stand-In", "1"))
        if self._not_modified(etag, mtime):
            self._send(304, headers, b"", send_body=False)
        else:
            self._send(200, headers, body, send_body)

    def _not_modified(self, etag: str, mtime: int) -> bool:
        inm = self.headers.get("If-None-Match")
        if inm is not None:  # takes precedence over If-Modified-Since (RFC 9110 13.2.2)
            tags = [t.strip().removeprefix("W/") for t in inm.split(",")]
            return "*" in tags or etag in tags
        ims = self.headers.get("If-Modified-Since")
        if ims is not None:
            try:
                return mtime <= int(parsedate_to_datetime(ims).timestamp())
            except (TypeError, ValueError):
                return False
        return False

    def _send(self, status: int, headers, body: bytes, send_body: bool) -> None:
        self.server.log.append((urlsplit(self.path).path, status))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        if status != 304:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # quiet
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("data/snapshot"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--standins", action="store_true", help="serve stand-ins for missing articles")
    args = ap.parse_args()
    server = OriginServer(args.root, (args.host, args.port), standins=args.standins)
    print(f"origin serving {args.root} on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
