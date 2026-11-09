"""Logs for the web server and the phone app, kept on the Mac only.

  ~/Library/Logs/SportsbookLines/app.log      everything: every request (status, size,
                                              time taken, which device), scrapes,
                                              scheduled runs, pushes, errors
  ~/Library/Logs/SportsbookLines/errors.log   warnings and errors only, from the server
                                              and from phones (those lines say PHONE)

Each file starts fresh at midnight; the day before is renamed with its date
(app.log.2026-11-07). Nothing is deleted. Open them in Console.app or any text
editor; errors.log is the one to read first.

The phone app reports its own failures (a page that couldn't reach the Mac, a
server error, a script error) with POST /api/log; see static/report.js. A phone
keeps a report only until the Mac has it.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_DIR = Path.home() / "Library" / "Logs" / "SportsbookLines"
FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_API_KEY = re.compile(r"(apiKey=)[^&\s'\"]+", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
log = logging.getLogger("lines_agent")
phone_log = logging.getLogger("lines_agent.phone")
MT = ZoneInfo("America/Denver")


def redact(text) -> str:
    """Hide The Odds API key, which rides in the query string of its URLs and so
    turns up in connection errors."""
    return _API_KEY.sub(r"\1[hidden]", str(text))


class _Formatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def setup(level: int = logging.INFO):
    """Send every logger to the two daily files (and nothing to the console)."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = _Formatter(FORMAT)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        for name, lvl in (("app.log", logging.DEBUG), ("errors.log", logging.WARNING)):
            h = logging.handlers.TimedRotatingFileHandler(LOG_DIR / name, when="midnight", backupCount=0,
                                                          encoding="utf-8")
            h.setLevel(lvl)
            h.setFormatter(fmt)
            root.addHandler(h)
    except OSError as e:  # can't write the folder: fall back to the launchd log
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        root.addHandler(h)
        log.error("Can't write logs to %s (%s); logging to stderr", LOG_DIR, e)
    for noisy in ("urllib3", "asyncio", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # A crash in a background thread (scheduler, a scan) would otherwise only
    # reach stderr, if anywhere.
    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.error("Uncaught error in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_hook
    sys.excepthook = lambda t, v, tb: log.critical("Uncaught error", exc_info=(t, v, tb))


def device(user_agent: str) -> str:
    """A short label for who made a request: iPhone, iPad, Android, Mac app, Mac."""
    ua = user_agent or ""
    if "SportsbookLinesMac" in ua:
        return "Mac app"
    for name in ("iPhone", "iPad", "Android"):
        if name in ua:
            return name
    if "Macintosh" in ua:
        return "Mac"
    return ua[:24] or "unknown"


def _when(iso: str) -> str:
    """The phone's timestamp (UTC) as Mountain time, like the rest of the log."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MT).strftime("%b %d %H:%M:%S MT")
    except ValueError:
        return iso


def _clean(v, n: int) -> str:
    return _CONTROL.sub(" ", str(v if v is not None else ""))[:n].strip()


_seen: deque = deque(maxlen=1000)  # report ids already written (a phone may resend)
_seen_lock = threading.Lock()
MAX_EVENTS = 10


def phone_reports(body: dict, user_agent: str) -> int:
    """Write a phone's error reports to the log, one line each. Returns how many
    were new. A report the phone sent before (same id) is skipped."""
    events = body.get("events")
    if not isinstance(events, list):
        raise ValueError("expected {\"events\": [...]}")
    who = device(user_agent) + (" home-screen app" if body.get("app") else "")
    written = 0
    for e in events[:MAX_EVENTS]:
        if not isinstance(e, dict):
            continue
        rid = _clean(e.get("id"), 24)
        with _seen_lock:
            if rid and rid in _seen:
                continue
            if rid:
                _seen.append(rid)
        kind = re.sub(r"[^a-z]", "", str(e.get("kind", "")).lower())[:12] or "error"
        extra = []
        if isinstance(e.get("ms"), (int, float)):
            extra.append(f"{int(e['ms'])} ms")
        if isinstance(e.get("n"), int) and e["n"] > 1:
            extra.append(f"x{e['n']}")
        if e.get("online") is False:
            extra.append("phone offline")
        phone_log.warning("PHONE %s %s | %s%s | happened %s | %s", kind, _clean(e.get("page"), 40),
                          _clean(e.get("msg"), 300), f" ({', '.join(extra)})" if extra else "",
                          _when(_clean(e.get("t"), 30)), who)
        written += 1
    return written
