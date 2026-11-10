"""Local web UI: python -m lines_agent.web [--port 8765]

Serves the odds page at http://127.0.0.1:<port>/ and a JSON endpoint:
  GET /api/lines?league=NBA            cached lines (scrapes on first request)
  GET /api/lines?league=NBA&refresh=1  force a fresh scrape
  GET /api/arbs?min=1[&bankroll=100]   arbitrage across all leagues (>= min %); stakes split
                                       the History bankroll unless bankroll is given
  GET /api/props?league=NBA[&refresh=1] basketball player props (cached; refresh scrapes props only)
  GET /api/status                      scheduled-run times and last result
  GET /api/push/key                    public key for Web Push subscriptions
  POST /api/push/subscribe|unsubscribe|test
  GET/POST /api/settings               scan controls and scheduled runs (settings.py)
  POST /api/estimate                   Odds API credits a scan profile would spend
  POST /api/scan                       scan now with the saved controls, in the background
  GET /ev                              the +EV page
  GET /api/ev?min=5[&method=shin][&refresh=1]  your books' lines with EV >= min % vs the sharp consensus;
                                       method: multiplicative (default), shin, power or all (ev.py)
  GET /bets, /history                  History, the bet tracker page
  GET /api/bets                        tracked bets with P&L totals (tracker.py)
  POST /api/bets                       track a placed bet: {"kind": "ev"|"arb", "legs": [...], "notes"}
  POST /api/bets/update|delete         {"id", ...changes} to settle or correct a bet, or remove it
  POST /api/bets/bankroll              {"starting": 300}
  GET /stats, /api/stats               app health: disk, memory, credits, traffic, scrapes, results (stats.py)
  GET /live, /api/live                 open bets on games under way, with live scores (scores.py)
  GET /api/members, POST /api/members/invite|remove   who can use the public site (invites.py)
  POST /api/log                        error reports from the phone app, written to the Mac's log (logs.py)

Every request is logged with its status, size and time taken (logs.py).

Scans and refreshes take an optional profile in the query string:
  leagues=NBA,MLB&books=FanDuel,DraftKings&markets=moneyline,total&stats=assists
(each list optional; an empty one means none), or use=manual for the saved controls.

It also re-scrapes every league at the --schedule times (see schedule.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

from . import agent, ev, invites, logs, props, push, schedule, scores, settings, stats, tracker
from .__main__ import _load_env
from .arbitrage import find_arbs, find_prop_arbs
from .books import ALL_BOOKS, SHARP, VIA_ODDS_API
from .httpd import BaseHandler, Server
from .matching import unify_games
from .models import LEAGUES, Line

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
# Home-screen app files (an explicit list, so nothing else in static/ is served)
STATIC_FILES = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "text/javascript; charset=utf-8",
    "theme.css": "text/css; charset=utf-8",
    "report.js": "text/javascript; charset=utf-8",
    "favicon.ico": "image/x-icon",       # browsers ask for this on their own
    "favicon-32.png": "image/png",       # tabs and bookmarks
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
}
log = logging.getLogger("lines_agent")

_locks = {lg: threading.Lock() for lg in LEAGUES}
# Player props live in the same cache under "props:<league>"
PROPS_KEY = "props:"
_locks.update({PROPS_KEY + lg: threading.Lock() for lg in props.LEAGUES})
# Kept on disk so a restart (login, crash, code change) doesn't spend Odds API
# credits re-pulling lines the page already had.
CACHE_FILE = ROOT / "data" / "web_cache.json"
_save_lock = threading.Lock()


def _load_cache() -> dict[str, dict]:
    try:
        data = json.loads(CACHE_FILE.read_text())
        return {k: v for k, v in data.items() if k in _locks}
    except (OSError, ValueError):
        return {}


def _save_cache():
    with _save_lock:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(_cache)))
        tmp.replace(CACHE_FILE)


_cache: dict[str, dict] = _load_cache()
STARTED = time.time()  # for the Stats page's uptime
MIN_ARB = 0.01  # notification threshold, from --min-arb
MIN_EV = 0.05   # +EV notification threshold, from --min-ev
EV_METHOD = "all"  # de-vig method for +EV alerts, from --ev-method


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _keep_errors(errors: dict, books: list[str], league: str) -> dict:
    """Errors from earlier scrapes of the books this scrape didn't touch."""
    return {k: v for k, v in errors.items()
            if k != league and not k.startswith("OddsAPI[") and not any(b in k for b in books)}


def get_league(league: str, refresh: bool, books=None, markets=None) -> dict:
    """Cached lines for a league. A refresh limited to some books or markets
    replaces just those lines and keeps the rest of the cache."""
    # Reading the cache never waits: a scheduled run can hold the lock for a
    # minute, long enough for the phone to give up ("Load failed").
    if not refresh and league in _cache:
        return _cache[league]
    # One scrape per league at a time; concurrent refreshes wait and share the result.
    with _locks[league]:
        if refresh or league not in _cache:
            started = time.time()
            books = books or list(ALL_BOOKS)
            markets = markets or list(settings.MARKETS)
            # The sharp books ride along in the Odds API request for free (books.SHARP).
            if any(b in VIA_ODDS_API for b in books):
                books = books + [b for b in SHARP if b not in books]
            lines, errors = agent.scrape(leagues=[league], book_names=books, include_live=True, markets=markets)
            agent.save(lines, ROOT / "data", snapshot=False)
            old = _cache.get(league) or {}
            partial = not set(ALL_BOOKS) <= set(books) or set(markets) != set(settings.MARKETS)
            if partial and old:
                # Kept lines keep their own scraped_at, so the page can tell old from new.
                kept = [Line(**d) for d in old.get("lines", [])
                        if not (d["book"] in books and d["market"] in markets)]
                lines = unify_games(kept + lines)
                errors = {**_keep_errors(old.get("errors", {}), books, league), **errors}
            now = _now()
            _cache[league] = {
                "league": league,
                "scraped_at": now,
                "seconds": round(time.time() - started, 1),
                "lines": [l.to_dict() for l in lines],
                "errors": errors,
                "book_scraped_at": {**(old.get("book_scraped_at", {}) if partial else {}),
                                    **{b: now for b in books}},
            }
            _save_cache()
        return _cache[league]


def get_props(league: str, refresh: bool, books=None, stats=None) -> dict | None:
    """Cached props. Only a refresh (scheduled run or Scan button) scrapes, since
    props mean visiting every game page and take a few minutes."""
    key = PROPS_KEY + league
    if not refresh:
        return _cache.get(key)  # don't wait out a props scrape (minutes)
    with _locks[key]:
        if refresh:
            started = time.time()
            books = books or list(ALL_BOOKS)
            stats = stats or list(props.STATS)
            got, errors = props.scrape_props(league, books=books, stats=stats)
            old = _cache.get(key) or {}
            if old and (set(books) != set(ALL_BOOKS) or set(stats) != set(props.STATS)):
                kept = [d for d in old.get("props", []) if not (d["book"] in books and d["stat"] in stats)]
                got_d = kept + [p.to_dict() for p in got]
                errors = {**{k: v for k, v in old.get("errors", {}).items()
                             if not any(b in k for b in books) and not k.startswith("Odds API")}, **errors}
            else:
                got_d = [p.to_dict() for p in got]
            _cache[key] = {
                "league": league,
                "scraped_at": _now(),
                "seconds": round(time.time() - started, 1),
                "props": got_d,
                "errors": errors,
            }
            _save_cache()
        return _cache.get(key)


def _props_of(data: dict) -> list:
    return [props.Prop(**{k: v for k, v in d.items() if k != "scraped_at"}) for d in data["props"]]


def scan_arbs(min_profit: float, include_live: bool, refresh: bool, bankroll: float, profile=None) -> dict:
    """Arbitrage for the profile's leagues, pairing only the profile's books,
    markets and prop stats (default: everything). With refresh, re-pulls just
    those first; everything else in the cache is left alone."""
    p = settings.clean_profile(profile)
    leagues, books, markets, stats = p["leagues"], p["books"], p["markets"], p["stats"]
    arbs, scanned, errors = [], {}, {}

    def one(league):
        try:
            # A scan without game-line markets still reads cached lines (for the page)
            # but doesn't re-pull them.
            return league, get_league(league, refresh and bool(markets), books, markets), None
        except Exception as e:
            return league, None, str(e)

    # Each league scrape already runs its books in parallel (one with a browser),
    # so only a few leagues at a time.
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(one, leagues if markets else []))
    for league, data, err in results:
        if err:
            errors[league] = err
            continue
        scanned[league] = data["scraped_at"]
        lines = [Line(**{k: v for k, v in d.items() if k != "scraped_at"}) for d in data["lines"]
                 if d["book"] in books and d["market"] in markets]
        arbs += find_arbs(lines, min_profit, include_live=include_live, bankroll=bankroll)

    # Props one league at a time: each one drives a browser through every game page.
    props_scanned = {}
    for league in [lg for lg in props.LEAGUES if lg in leagues] if stats else []:
        try:
            data = get_props(league, refresh, books, stats)
        except Exception as e:
            errors[f"{league} props"] = str(e)
            continue
        if data:
            props_scanned[league] = data["scraped_at"]
            got = [pr for pr in _props_of(data) if pr.book in books and pr.stat in stats]
            arbs += find_prop_arbs(got, min_profit, bankroll=bankroll)
    arbs.sort(key=lambda a: -a["profit_pct"])
    return {"arbs": arbs, "scanned_at": scanned, "props_scanned_at": props_scanned,
            "errors": errors, "min_pct": min_profit * 100, "profile": p, "bankroll": bankroll}


def scan_ev(min_ev: float, refresh: bool, leagues=ev.LEAGUES, include_live: bool = False,
            method: str = "multiplicative") -> dict:
    """+EV lines across the EV leagues. A refresh re-pulls every book in those
    leagues (Pinnacle rides along with the Odds API books); otherwise it reads
    the cache, which every scan and scheduled run keeps current."""
    bets, scanned, sharp, errors = [], {}, {}, {}

    def one(league):
        try:
            return league, get_league(league, refresh), None
        except Exception as e:
            return league, None, str(e)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(one, leagues))
    for league, data, err in results:
        if err:
            errors[league] = err
            continue
        lines = [Line(**d) for d in data["lines"]]
        scanned[league] = data["scraped_at"]
        sharp[league] = {b: sum(1 for l in lines if l.book == b) for b in ev.SHARP_WEIGHTS}
        errors.update({k: v for k, v in data.get("errors", {}).items() if any(b in k for b in ev.SHARP_WEIGHTS)})
        bets += ev.find_ev(lines, min_ev, include_live=include_live, method=method)
    bets.sort(key=lambda b: -b["ev_pct"])
    return {"bets": bets, "scanned_at": scanned, "sharp_lines": sharp, "errors": errors,
            "min_pct": min_ev * 100, "method": method, "weights": ev.SHARP_WEIGHTS, "leagues": list(leagues),
            "bankroll": tracker.bankroll()}


def _ev_check(profile) -> tuple[list[dict], str]:
    """After a scheduled run: +EV bets in the leagues it just re-pulled (no new
    pull), and the de-vig method they were judged by."""
    leagues = [lg for lg in ev.LEAGUES if lg in settings.clean_profile(profile)["leagues"]]
    bets = scan_ev(MIN_EV, refresh=False, leagues=leagues, method=EV_METHOD)["bets"] if leagues else []
    return bets, EV_METHOD


def _profile_from_query(q: dict) -> dict | None:
    """?leagues=NBA,MLB&books=...&markets=...&stats=... (each optional; "" means none)."""
    if q.get("use", [""])[0] == "manual":
        return settings.load()["manual"]
    keys = ("leagues", "books", "markets", "stats")
    if not any(k in q for k in keys):
        return None
    return {k: [v for v in q[k][0].split(",") if v] for k in keys if k in q}


def _housekeeping():
    """Daily at 3:30 MT (and once shortly after start): back up the bets database and
    delete line history older than agent.HISTORY_DAYS."""
    time.sleep(120)  # let startup and any catch-up run go first
    while True:
        try:
            log.info("Bets backed up to %s", tracker.backup())
        except Exception:
            log.exception("Backing up bets failed")
        try:
            r = agent.prune_history(ROOT / "data")
            if r["rows_removed"] or r["snapshots_removed"]:
                log.info("Line history trimmed to %d days: %d rows removed (%.0f MB -> %.0f MB), %d snapshots removed",
                         agent.HISTORY_DAYS, r["rows_removed"], r["bytes_before"] / 1e6, r["bytes_after"] / 1e6,
                         r["snapshots_removed"])
            else:
                log.info("Line history: nothing older than %d days (%.0f MB)", agent.HISTORY_DAYS, r["bytes_before"] / 1e6)
        except Exception:
            log.exception("Trimming line history failed")
        now = datetime.now(schedule.MT)
        nxt = now.replace(hour=3, minute=30, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        time.sleep(max((nxt - now).total_seconds(), 60))


def _scan_now(min_profit: float, notify: bool = True):
    """Scan with the page's current controls in the background: the Mac app's
    Scan Now, and the page's Scan button (quietly; the page shows the results)."""
    profile = settings.load()["manual"]
    schedule.state["running"] = True  # so a status poll right after this sees it
    threading.Thread(target=schedule.run_once, name="manual-scan", daemon=True,
                     args=(scan_arbs, min_profit, profile, "Manual scan"),
                     kwargs={"notify": notify}).start()


# Names this server may be addressed by; PRIVATE_HOSTS in .env adds more (comma-separated).
# Anything else is a website pointing its own domain at this Mac (DNS rebinding).
PRIVATE_HOSTS = ("127.0.0.1", "localhost", "mac-mini.tail839a81.ts.net")


class Handler(BaseHandler):
    def _refuse_foreign(self) -> bool:
        """This server has no login, so it must not act for other websites open in
        a browser. Answers 403 and returns True for a request from one. Browsers
        always label cross-site requests; the Mac app and command-line tools send no
        such labels and are unaffected."""
        host = self.headers.get("Host", "")
        extra = [h.strip() for h in os.environ.get("PRIVATE_HOSTS", "").split(",") if h.strip()]
        if urlsplit("//" + host).hostname not in (*PRIVATE_HOSTS, *extra):
            log.warning("Refused a request addressed to an unknown host")
            self._send(403, b'{"error":"unknown host"}', "application/json")
            return True
        if self.command == "POST" or urlparse(self.path).path.startswith("/api/"):
            origin = self.headers.get("Origin")
            if (origin is not None and urlparse(origin).netloc != host) \
                    or self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
                log.warning("Refused a cross-site %s %s", self.command, urlparse(self.path).path)
                self._send(403, b'{"error":"cross-site request refused"}', "application/json")
                return True
        return False

    def _get(self):
        if self._refuse_foreign():
            return
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path in ("/ev", "/ev.html"):
            return self._send(200, (STATIC / "ev.html").read_bytes(), "text/html; charset=utf-8")
        if url.path in ("/bets", "/bets.html", "/history"):
            return self._send(200, (STATIC / "bets.html").read_bytes(), "text/html; charset=utf-8")
        if url.path in ("/live", "/live.html"):
            return self._send(200, (STATIC / "live.html").read_bytes(), "text/html; charset=utf-8")
        if url.path in ("/stats", "/stats.html"):
            return self._send(200, (STATIC / "stats.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/stats":
            return self._send(200, json.dumps(stats.collect(_cache, STARTED, schedule.state)).encode(),
                              "application/json")
        if url.path == "/api/bets":
            return self._send(200, json.dumps(tracker.summary()).encode(), "application/json")
        if url.path == "/api/live":
            return self._send(200, json.dumps(scores.live()).encode(), "application/json")
        if url.path == "/api/members":
            out = {"members": tracker.members(), "problem": invites.check()}
            return self._send(200, json.dumps(out).encode(), "application/json")
        if url.path == "/api/ev":
            q = parse_qs(url.query)
            try:
                min_pct = _finite(q.get("min", [str(MIN_EV * 100)])[0])
            except ValueError:
                return self._send(400, b'{"error":"bad number"}', "application/json")
            method = q.get("method", ["multiplicative"])[0]
            if method not in ev.METHODS + ("all",):
                return self._send(400, b'{"error":"unknown method"}', "application/json")
            data = scan_ev(min_pct / 100, refresh=q.get("refresh", ["0"])[0] == "1",
                           include_live=q.get("live", ["0"])[0] == "1", method=method)
            return self._send(200, json.dumps(data).encode(), "application/json")
        if url.path == "/apple-touch-icon-precomposed.png":  # iPhone asks for this name too
            url = url._replace(path="/apple-touch-icon.png")
        if url.path.lstrip("/") in STATIC_FILES:
            name = url.path.lstrip("/")
            return self._send(200, (STATIC / name).read_bytes(), STATIC_FILES[name])
        if url.path == "/api/lines":
            q = parse_qs(url.query)
            league = q.get("league", ["NBA"])[0].upper()
            if league not in LEAGUES:
                return self._send(400, b'{"error":"unknown league"}', "application/json")
            prof = settings.clean_profile(_profile_from_query(q))
            try:
                data = get_league(league, refresh=q.get("refresh", ["0"])[0] == "1",
                                  books=prof["books"], markets=prof["markets"] or None)
            except Exception as e:
                log.exception("scrape failed")
                return self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return self._send(200, json.dumps(data).encode(), "application/json")
        if url.path == "/api/props":
            q = parse_qs(url.query)
            league = q.get("league", ["NBA"])[0].upper()
            if league not in props.LEAGUES:
                return self._send(400, b'{"error":"no props for that league"}', "application/json")
            # refresh=1 re-scrapes props alone (free unless ODDS_API_PROPS is on)
            prof = settings.clean_profile(_profile_from_query(q))
            data = get_props(league, refresh=q.get("refresh", ["0"])[0] == "1",
                             books=prof["books"], stats=prof["stats"] or None) \
                or {"league": league, "scraped_at": None, "props": [], "errors": {}}
            return self._send(200, json.dumps(data).encode(), "application/json")
        if url.path == "/api/push/key":
            return self._send(200, json.dumps({"key": push.public_key(), "devices": push.count()}).encode(),
                              "application/json")
        if url.path == "/api/settings":
            out = {**settings.load(), "options": {
                **settings.full_profile(), "api_books": VIA_ODDS_API, "prop_leagues": list(props.LEAGUES),
                "min_arb_pct": MIN_ARB * 100}}
            return self._send(200, json.dumps(out).encode(), "application/json")
        if url.path == "/api/status":
            return self._send(200, json.dumps(schedule.state).encode(), "application/json")
        if url.path == "/api/arbs":
            q = parse_qs(url.query)
            try:
                min_pct = _finite(q.get("min", ["1"])[0])
                bankroll = _finite(q["bankroll"][0]) if "bankroll" in q else max(tracker.bankroll(), 0) or 100.0
            except ValueError:
                return self._send(400, b'{"error":"bad number"}', "application/json")
            data = scan_arbs(min_pct / 100, include_live=q.get("live", ["0"])[0] == "1",
                             refresh=q.get("refresh", ["0"])[0] == "1", bankroll=bankroll,
                             profile=_profile_from_query(q))
            return self._send(200, json.dumps(data).encode(), "application/json")
        self._send(404, b"not found", "text/plain")

    def _post(self):
        if self._refuse_foreign():
            return
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 8192:
                return self._send(413, b'{"error":"too large"}', "application/json")
            if length < 0:  # rfile.read(-1) would wait for the client to hang up
                return self._send(400, b'{"error":"bad length"}', "application/json")
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        if not isinstance(body, dict):  # e.g. [] or 5: every endpoint expects {...}
            return self._send(400, b'{"error":"expected a JSON object"}', "application/json")
        try:
            if url.path == "/api/push/subscribe":
                out = {"devices": push.subscribe(body)}
            elif url.path == "/api/push/unsubscribe":
                out = {"devices": push.unsubscribe(str(body.get("endpoint", "")))}
            elif url.path == "/api/settings":
                out = settings.save(body)
                schedule.reload()
            elif url.path == "/api/estimate":
                out = settings.estimate(body, _cache)
            elif url.path == "/api/scan":
                if schedule.state["running"]:
                    return self._send(409, b'{"error":"a scan is already running"}', "application/json")
                _scan_now(MIN_ARB, notify=not body.get("quiet"))
                out = {"started": True}
            elif url.path == "/api/bets":
                out = {"added": tracker.add(body.get("kind", "ev"), body.get("legs") or [], body.get("notes", ""))}
            elif url.path == "/api/bets/update":
                out = {"bet": tracker.update(str(body.get("id", "")), body)}
            elif url.path == "/api/bets/delete":
                tracker.delete(str(body.get("id", "")))
                out = {"deleted": body.get("id")}
            elif url.path == "/api/bets/bankroll":
                out = {"starting_bankroll": tracker.set_bankroll(body.get("starting", 0))}
            elif url.path == "/api/members/invite":
                out = {"invited": invites.invite(body.get("email"))}
            elif url.path == "/api/members/remove":
                out = {"removed": invites.remove(body.get("email"))}
            elif url.path == "/api/push/test":
                out = push.send_all("Sportsbook Lines", "Test alert: notifications are working.", tag="test")
            elif url.path == "/api/log":
                out = {"logged": logs.phone_reports(body, self.headers.get("User-Agent", ""))}
            else:
                return self._send(404, b"not found", "text/plain")
        except (ValueError, TypeError) as e:
            return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
        self._send(200, json.dumps(out).encode(), "application/json")


def _finite(v: str) -> float:
    """A query-string number; "nan" and "inf" parse as floats but would break the
    math and put NaN into the JSON, which browsers can't read."""
    x = float(v)
    if not math.isfinite(x):
        raise ValueError(f"bad number {v!r}")
    return x


def main():
    _load_env(ROOT / ".env")
    p = argparse.ArgumentParser(prog="lines_agent.web")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--schedule", default=",".join(schedule.DEFAULT_TIMES),
                   help="comma-separated HH:MM times (US Mountain) for the first settings file; "
                        "after that, edit runs on the page (data/settings.json)")
    p.add_argument("--min-arb", type=float, default=1.0, metavar="PCT",
                   help="notify when a scheduled run finds arbitrage of at least this %% (default 1)")
    p.add_argument("--min-ev", type=float, default=5.0, metavar="PCT",
                   help="notify when a scheduled run finds a +EV line of at least this %% (default 5)")
    p.add_argument("--ev-method", choices=ev.METHODS + ("all",), default="all",
                   help="de-vig method for +EV alerts (default all: every method must clear --min-ev)")
    a = p.parse_args()
    logs.setup()
    global MIN_ARB, MIN_EV, EV_METHOD
    MIN_ARB, MIN_EV, EV_METHOD = a.min_arb / 100, a.min_ev / 100, a.ev_method
    schedule.ev_check = _ev_check
    times = [t.strip() for t in a.schedule.split(",") if t.strip()]
    # The runs saved from the page win; --schedule only seeds the first settings file.
    if not settings.FILE.exists():
        settings.save(settings.load(default_times=times))
    elif '"zone": "MT"' not in settings.FILE.read_text():
        settings.save({})  # rewrite Eastern run times as Mountain (settings.load)
    schedule.start(scan_arbs, min_profit=MIN_ARB)
    threading.Thread(target=_housekeeping, name="housekeeping", daemon=True).start()
    # Bind to localhost only: this is a personal tool, not a public site.
    server = Server(("127.0.0.1", a.port), Handler)
    log.info("Server started on http://127.0.0.1:%d/ (logs in %s)", a.port, logs.LOG_DIR)
    print(f"Serving on http://127.0.0.1:{a.port}/  (logs in {logs.LOG_DIR})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
