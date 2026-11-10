"""The public site: python -m lines_agent.public [--port 8766]

A separate program that Cloudflare Tunnel connects to. Cloudflare Access handles
the login; this program then checks Access's signed token on every request and
serves History and Live, each person seeing and changing only their own bets
(tracker.py). Everything else is refused. It can't start a scan or spend Odds API
credits: it never loads the API key or any of the scraping, scanning or settings
code. New bets are entered by hand against ESPN's free schedule.

The private server (web.py) keeps running as before for the Mac app and the
Tailscale phone app.

Setup lives in data/public_site.json:
  {"team_domain": "yourteam.cloudflareaccess.com",
   "aud": "<Application Audience tag from the Access application>",
   "owner_email": "you@example.com"}     # sees the bets the Mac tracks
Everyone else is invited from the private History page (invites.py) and is
checked against the database on every request, so removal takes effect at once.
Without a valid file the program stops rather than start unprotected.

Logs: public.log and public-errors.log, next to the private server's (logs.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
import requests

from . import logs, scores, tracker
from .httpd import BaseHandler, Server

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
CONFIG = ROOT / "data" / "public_site.json"
log = logging.getLogger("lines_agent")

HTML = "text/html; charset=utf-8"
PAGES = {"/": "bets.html", "/bets": "bets.html", "/history": "bets.html", "/live": "live.html"}
FILES = {
    "/theme.css": "text/css; charset=utf-8",
    "/report.js": "text/javascript; charset=utf-8",
    "/public.js": "text/javascript; charset=utf-8",
    "/favicon.ico": "image/x-icon",
    "/favicon-32.png": "image/png",
    "/icon-192.png": "image/png",
    "/apple-touch-icon.png": "image/png",
}
SECURITY_HEADERS = {
    # The pages use inline scripts and styles; everything else must come from this site.
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                               "form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
}
MAX_REPORTS_PER_MINUTE = 30
KEY_TTL = 3600        # how long Cloudflare's signing keys are kept
KEY_RETRY = 60        # an unknown key re-fetches at most this often (Cloudflare rotates them)


class ConfigError(Exception):
    pass


def load_config(path: Path = CONFIG) -> dict:
    try:
        c = json.loads(path.read_text())
    except OSError as e:
        raise ConfigError(f"can't read {path.name}: {e.strerror}")
    except ValueError:
        raise ConfigError(f"{path.name} isn't valid JSON")
    team, aud = c.get("team_domain"), c.get("aud")
    if not isinstance(team, str) or not re.fullmatch(r"[a-z0-9-]+\.cloudflareaccess\.com", team):
        raise ConfigError('team_domain must look like "yourteam.cloudflareaccess.com"')
    if not isinstance(aud, str) or len(aud) < 16:
        raise ConfigError("aud must be the Application Audience tag from the Access application")
    owner = c.get("owner_email")
    if not isinstance(owner, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", owner.strip()):
        raise ConfigError("owner_email must be your email address")
    return {"team_domain": team, "aud": aud, "owner_email": owner.strip().lower()}


class AccessVerifier:
    """Checks the token Cloudflare Access attaches to each request: signed by your
    Cloudflare team's key, issued for this application, and not expired."""

    def __init__(self, team_domain: str, aud: str, fetch_keys=None):
        self.issuer = f"https://{team_domain}"
        self.aud = aud
        self._fetch = fetch_keys or self._download
        self._keys: dict[str, object] = {}
        self._at = 0.0
        self._lock = threading.Lock()

    def _download(self) -> dict:
        r = requests.get(f"{self.issuer}/cdn-cgi/access/certs", timeout=10)
        r.raise_for_status()
        return r.json()

    def _refresh(self):
        jwks = self._fetch()
        self._keys = {k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(k))
                      for k in jwks.get("keys", []) if k.get("kty") == "RSA" and k.get("kid")}
        self._at = time.monotonic()

    def _key(self, kid: str):
        with self._lock:
            age = time.monotonic() - self._at
            if not self._keys or age > KEY_TTL or (kid not in self._keys and age > KEY_RETRY):
                try:
                    self._refresh()
                except Exception:
                    if not self._keys:
                        raise
                    log.warning("Couldn't refresh Cloudflare's signing keys; using the saved ones")
            return self._keys.get(kid)

    def verify(self, token: str) -> dict:
        """The token's claims. Raises jwt.InvalidTokenError if it isn't valid."""
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":  # never let the token choose how it's checked
            raise jwt.InvalidAlgorithmError("unexpected algorithm")
        key = self._key(header.get("kid") or "")
        if key is None:
            raise jwt.InvalidSignatureError("unknown signing key")
        return jwt.decode(token, key, algorithms=["RS256"], audience=self.aud, issuer=self.issuer, leeway=30,
                          options={"require": ["exp", "iat", "aud", "iss"]})


def public_html(raw: bytes) -> bytes:
    """A page in public form: marked so links to private pages are hidden (theme.css),
    with no home-screen install, and with public.js for sign-in handling."""
    s = raw.decode()
    report = '<script src="/report.js"></script>'
    if '<html lang="en">' not in s or report not in s:
        raise ValueError("page layout changed; refusing to serve it without the read-only marker")
    s = s.replace('<html lang="en">', '<html lang="en" data-public="1">', 1)
    s = s.replace(report, report + '\n<script src="/public.js"></script>', 1)
    s = re.sub(r'<link rel="manifest"[^>]*>\n?', "", s)
    return s.encode()


def _text(body: dict, key: str, limit: int, label: str) -> str:
    v = body.get(key)
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise ValueError(f"choose a {label}")
    return v.strip()


def manual_leg(b: dict) -> dict:
    """A hand-entered bet, checked and in the same shape as a tracked one so Live can grade it."""
    league = b.get("league")
    if league not in scores.FEEDS:
        raise ValueError("choose a league")
    away, home = _text(b, "away_team", 80, "game"), _text(b, "home_team", 80, "game")
    start = _text(b, "start_time", 40, "game")
    if scores._ts(start) is None:
        raise ValueError("choose a game")
    market, side = b.get("market"), b.get("side")
    if market not in ("moneyline", "spread", "total") or side not in (("over", "under") if market == "total" else ("away", "home")):
        raise ValueError("choose a market and side")
    line = None
    if market != "moneyline":
        try:
            line = float(b.get("line"))
        except (TypeError, ValueError):
            raise ValueError("enter the line, like -3.5 or 47.5")
        if not math.isfinite(line) or abs(line) > 1000 or (market == "total" and line <= 0):
            raise ValueError("enter the line, like -3.5 or 47.5")
    team = away if side == "away" else home
    pick = f"{side.capitalize()} {line:g}" if market == "total" else f"{team} {line:+g}" if market == "spread" else f"{team} ML"
    return {"book": _text(b, "book", 40, "sportsbook"), "odds": b.get("odds"), "stake": b.get("stake"), "pick": pick,
            "league": league, "away_team": away, "home_team": home, "start_time": start,
            "market": market, "side": side, "line": line}


WRITES = ("/api/bets", "/api/bets/update", "/api/bets/delete", "/api/bets/bankroll")
EDITABLE = ("status", "cashout", "stake", "odds", "notes")


class PublicHandler(BaseHandler):
    verifier: AccessVerifier
    owner: str
    user = ""  # whose bets this request may touch
    expose_errors = False
    extra_headers = SECURITY_HEADERS
    timeout = 30  # seconds before a stalled connection is dropped
    _reports: deque = deque()
    _reports_lock = threading.Lock()

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _signed_in(self) -> bool:
        """Answers with an error and returns False unless the request carries a valid
        Access token for the owner or an invited member. Runs before any routing, so a stranger
        learns nothing about which addresses exist."""
        token = self.headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            self._json(401, {"error": "sign in required"})
            return False
        try:
            claims = self.verifier.verify(token)
        except jwt.InvalidTokenError as e:
            log.warning("Refused a bad Access token: %s", type(e).__name__)
            self._json(401, {"error": "sign in required"})
            return False
        except Exception:
            log.exception("Couldn't check an Access token")
            self._json(503, {"error": "sign-in check unavailable"})
            return False
        email = str(claims.get("email", "")).strip().lower()
        if email != self.owner and not tracker.is_member(email):
            log.warning("Refused a signed-in account that isn't on the allowed list")
            self._json(403, {"error": "not allowed"})
            return False
        self.who = email
        self.user = tracker.OWNER if email == self.owner else email
        return True

    def _get(self):
        if not self._signed_in():
            return
        path = urlparse(self.path).path
        if path in PAGES:
            return self._send(200, public_html((STATIC / PAGES[path]).read_bytes()), HTML)
        if path in FILES:
            return self._send(200, (STATIC / path.lstrip("/")).read_bytes(), FILES[path])
        if path == "/api/bets":
            return self._json(200, tracker.summary(self.user))
        if path == "/api/live":
            return self._json(200, scores.live(self.user))
        if path == "/api/games":
            league = parse_qs(urlparse(self.path).query).get("league", [""])[0]
            if league not in scores.FEEDS:
                return self._json(400, {"error": "unknown league"})
            return self._json(200, {"games": scores.upcoming(league)})
        if path == "/api/whoami":
            return self._json(200, {"email": self.who})
        self._json(404, {"error": "not found"})

    def _post(self):
        if not self._signed_in():
            return
        path = urlparse(self.path).path
        if path != "/api/log" and path not in WRITES:
            return self._json(404, {"error": "not found"})
        # Stops another website from submitting changes using your signed-in browser
        origin = self.headers.get("Origin")
        if (origin and urlparse(origin).netloc != self.headers.get("Host")) or (path in WRITES and not origin):
            return self._json(403, {"error": "wrong origin"})
        if path in WRITES and not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json(415, {"error": "expected JSON"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 8192:
                return self._json(413, {"error": "bad size"})
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "expected a JSON object"})
        try:
            if path == "/api/log":
                return self._report(body)
            return self._json(200, self._change(path, body))
        except (ValueError, TypeError) as e:
            return self._json(400, {"error": str(e)[:200]})

    def _report(self, body: dict):
        now = time.monotonic()
        with self._reports_lock:
            while self._reports and now - self._reports[0] > 60:
                self._reports.popleft()
            if len(self._reports) >= MAX_REPORTS_PER_MINUTE:
                return self._json(429, {"error": "too many reports"})
            self._reports.append(now)
        return self._json(200, {"logged": logs.phone_reports(body, self.headers.get("User-Agent", ""))})

    def _change(self, path: str, body: dict) -> dict:
        """Every change is made as the signed-in user; nothing in the request can name another."""
        bet_id = str(body.get("id", ""))[:40]
        if path == "/api/bets":
            return {"added": tracker.add("manual", [manual_leg(body)], str(body.get("notes", "")), self.user)}
        if path == "/api/bets/update":
            return {"bet": tracker.update(bet_id, {k: body[k] for k in EDITABLE if k in body}, self.user)}
        if path == "/api/bets/delete":
            tracker.delete(bet_id, self.user)
            return {"deleted": bet_id}
        return {"starting_bankroll": tracker.set_bankroll(body.get("starting", 0), self.user)}


def make_handler(verifier: AccessVerifier, owner: str) -> type[PublicHandler]:
    return type("ConfiguredPublicHandler", (PublicHandler,), {"verifier": verifier, "owner": owner})


def main():
    p = argparse.ArgumentParser(prog="lines_agent.public")
    p.add_argument("--port", type=int, default=8766)
    a = p.parse_args()
    logs.setup(app_log="public.log", error_log="public-errors.log")
    os.environ.pop("ODDS_API_KEY", None)
    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("Public site not started: %s", e)
        print(f"Public site not started: {e}", file=sys.stderr, flush=True)
        sys.exit(0)  # a clean exit, so launchd doesn't keep retrying an unconfigured site
    handler = make_handler(AccessVerifier(cfg["team_domain"], cfg["aud"]), cfg["owner_email"])
    # Localhost only: Cloudflare's connector on this Mac is the sole way in.
    server = Server(("127.0.0.1", a.port), handler)
    log.info("Public site started on 127.0.0.1:%d", a.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
