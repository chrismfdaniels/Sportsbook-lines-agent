"""The Odds API (the-odds-api.com): a licensed odds feed.

DraftKings, BetMGM and Caesars block automated requests to their sites and
Fanatics' site doesn't serve its own lines, so their lines come from this feed
(Caesars and Fanatics need a paid plan). Bally Bet comes from here too, since it
costs nothing extra in the same request. Each league pull costs a credit per market (3 for all of them) for up to
10 books.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests

from ..models import Line

URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
SPORTS = {
    "NBA": "basketball_nba", "NFL": "americanfootball_nfl", "NHL": "icehockey_nhl", "MLB": "baseball_mlb",
    "NCAAF": "americanfootball_ncaaf", "NCAAB": "basketball_ncaab",
    "WNCAAB": "basketball_wncaab", "NCAABASE": "baseball_ncaa",
}
SUPPORTED = set(SPORTS)
SPORTS_URL = "https://api.the-odds-api.com/v4/sports"
_active: dict = {"at": 0.0, "keys": set()}
_active_lock = threading.Lock()
MARKETS = {"h2h": "moneyline", "spreads": "spread", "totals": "total"}

# Display name -> The Odds API bookmaker key
BOOK_KEYS = {
    "DraftKings": "draftkings",
    "BetMGM": "betmgm",
    "Caesars": "williamhill_us",  # paid plans only
    "Fanatics": "fanatics",       # paid plans only
    "Bally Bet": "ballybet",      # in the feed's "us2" bookmaker list; named directly, so no region is needed
    "Pinnacle": "pinnacle",       # sharp reference for the +EV finder
    # Also available here if you'd rather not run the direct scrapers:
    "FanDuel": "fanduel",
    "BetRivers": "betrivers",
    "theScore Bet": "espnbet",
}
KEY_TO_NAME = {v: k for k, v in BOOK_KEYS.items()}


# Latest credit balance seen in a response, shown on the page next to scan estimates
quota: dict = {"remaining": None, "used": None, "at": None}


class MissingApiKey(RuntimeError):
    pass


def log_quota(resp):
    remaining = resp.headers.get("x-requests-remaining")
    if remaining is not None:
        quota.update(remaining=int(float(remaining)), used=int(float(resp.headers.get("x-requests-used") or 0)),
                     at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        logging.getLogger("lines_agent").info("The Odds API requests remaining: %s", remaining)


def _in_season(sport: str, api_key: str) -> bool:
    """The /sports listing is free (no credits), so check it before spending 3
    credits on an odds pull for an out-of-season league. Cached for an hour."""
    with _active_lock:
        if time.time() - _active["at"] > 3600:
            resp = requests.get(SPORTS_URL, params={"apiKey": api_key}, timeout=30)
            if not resp.ok:
                return True  # can't tell, so try the pull anyway
            log_quota(resp)
            _active["keys"] = {s["key"] for s in resp.json() if s.get("active")}
            _active["at"] = time.time()
        return sport in _active["keys"]


def in_season(league: str) -> bool | None:
    """Whether the feed lists the league as active; None without a key."""
    key = os.environ.get("ODDS_API_KEY")
    return _in_season(SPORTS[league], key) if key and league in SPORTS else None


def fetch(league: str, books: list[str], api_key: str | None = None, markets=None, **_) -> list[Line]:
    """markets: a subset of moneyline/spread/total; each one costs a credit."""
    wanted = [k for k, m in MARKETS.items() if not markets or m in markets]
    api_key = api_key or os.environ.get("ODDS_API_KEY")
    if not api_key:
        raise MissingApiKey("set ODDS_API_KEY (free at https://the-odds-api.com) to pull " + ", ".join(books))
    if not _in_season(SPORTS[league], api_key):
        return []

    resp = requests.get(
        URL.format(sport=SPORTS[league]),
        params={"apiKey": api_key, "markets": ",".join(wanted), "oddsFormat": "american",
                "bookmakers": ",".join(BOOK_KEYS[b] for b in books)},
        timeout=30,
    )
    if not resp.ok:
        # Don't let the API key (it's in the query string) leak into logs.
        raise RuntimeError(f"The Odds API returned HTTP {resp.status_code}: {resp.text[:200]}")
    log_quota(resp)
    now = datetime.now(timezone.utc)

    lines: list[Line] = []
    for ev in resp.json():
        home, away = ev["home_team"], ev["away_team"]
        live = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")) <= now
        for bm in ev.get("bookmakers", []):
            book = KEY_TO_NAME.get(bm["key"], bm.get("title", bm["key"]))
            for m in bm.get("markets", []):
                market = MARKETS.get(m["key"])
                for o in m.get("outcomes", []):
                    name = o["name"]
                    if market == "total":
                        side = name.lower()
                    else:
                        side = "home" if name == home else "away" if name == away else None
                    if not market or side not in ("home", "away", "over", "under"):
                        continue
                    lines.append(Line(
                        book=book, league=league, event_id=ev["id"], start_time=ev["commence_time"],
                        away_team=away, home_team=home, market=market, side=side,
                        line=o.get("point") if market != "moneyline" else None,
                        odds=int(o["price"]), live=live,
                    ))
    return lines
