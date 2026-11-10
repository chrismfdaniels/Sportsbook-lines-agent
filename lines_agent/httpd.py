"""The web-serving code shared by the private server (web.py) and the public
site (public.py): compressed responses, one log line per request, and an answer
even when a handler fails."""
from __future__ import annotations

import gzip
import json
import logging
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import logs

log = logging.getLogger("lines_agent")
# Pages and data shrink to roughly a tenth gzipped, which matters on cellular.
# Images are already compressed, and tiny bodies aren't worth it.
COMPRESS_MIN = 1024
# A read slower than this is logged as a warning (it's what makes the phone give
# up with "Load failed"). Refreshes and scans legitimately take longer.
SLOW_MS = 3000


def compressible(ctype: str) -> bool:
    return ctype.startswith("text/") or any(t in ctype for t in ("json", "javascript", "manifest"))


def size_text(n: int) -> str:
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB" if n < 1024 * 1024 else f"{n / 1048576:.1f}MB"


class BaseHandler(BaseHTTPRequestHandler):
    """Subclasses define _get and _post."""
    expose_errors = True   # put the failure's reason in a 500 (private server only)
    who = ""               # added to the request's log line
    extra_headers: dict[str, str] = {}

    def _send(self, code: int, body: bytes, ctype: str):
        self._status, self._size, self._raw = code, len(body), None
        zipped = (len(body) >= COMPRESS_MIN and compressible(ctype)
                  and "gzip" in self.headers.get("Accept-Encoding", "").lower())
        if zipped:
            self._raw = len(body)
            body = gzip.compress(body, compresslevel=5)
            self._size = len(body)
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if compressible(ctype):
                self.send_header("Vary", "Accept-Encoding")
            if zipped:
                self.send_header("Content-Encoding", "gzip")
            for k, v in self.extra_headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self._status = f"{code} (client gone)"  # the page was closed or reloaded mid-response

    def _dispatch(self, handler):
        """Run a request so that any error still answers (a 500) instead of
        dropping the connection, which the phone shows as "Load failed", and log
        one line for it."""
        started = time.monotonic()
        self._status, self._size, self._raw = None, 0, None
        try:
            handler()
        except Exception as e:
            log.exception("%s %s failed", self.command, self.path[:200])
            if self._status is None:
                why = f"server error: {type(e).__name__}: {e}"[:300] if self.expose_errors else "server error"
                self._send(500, json.dumps({"error": why}).encode(), "application/json")
        ms = (time.monotonic() - started) * 1000
        slow = ms > SLOW_MS and "refresh=1" not in self.path and self.path not in ("/api/scan",)
        status = self._status if self._status is not None else "no response"
        level = logging.WARNING if slow or (isinstance(status, int) and status >= 500) else logging.INFO
        size = f"{size_text(self._raw)}->{size_text(self._size)} gz" if self._raw else size_text(self._size)
        log.log(level, "%s %s %s %s %.0fms %s%s%s", self.command, self.path[:200], status, size, ms,
                logs.device(self.headers.get("User-Agent", "")), f" [{self.who}]" if self.who else "",
                " SLOW" if slow else "")

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def log_request(self, code="-", size="-"):
        pass  # _dispatch logs each request, with its timing

    def log_message(self, fmt, *args):
        # What http.server still reports itself: malformed requests, unsupported methods
        log.warning("http: %s", fmt % args)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Errors outside a handler (reading the request): a phone dropping off
        # mid-request is routine; anything else gets its traceback in the log.
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            log.info("Client hung up mid-request (%s)", type(exc).__name__)
        else:
            log.exception("Request from %s failed outside its handler", client_address[0])
