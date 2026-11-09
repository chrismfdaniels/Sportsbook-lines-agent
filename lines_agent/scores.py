"""Live scores for games you have open bets on, from ESPN's public scoreboard
(free, no key, so it spends no Odds API credits).

Each open bet whose game has started is matched to an ESPN game by team names
and start time, then graded as if the game ended right now:

  moneyline  your team's score vs the other team's
  spread     your team's score + the line vs the other team's
  total      the combined score vs the line (over wants more, under less)

"up" means the bet would win now, "down" lose, "flat" push (or a tied
moneyline). Player props get the score but no grade (no live stats).

Totals are the exception: their arrow follows the pace (the final total the
game is on track for) rather than the current score, because an under is
always winning and an over always losing until late. An under on a game
pacing past the line points down even while it's still under; an over pacing
past it points up before it gets there. Once the line is passed the bet is
decided (over won, under lost) and the arrow says so. In the first 10% of a
game there's no pace yet, so the arrow uses the current score.

The projected money follows the arrow: a bet trending up counts its payout,
down its stake lost, flat nothing. So an under pacing over is projected as a
loss, not a win.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from . import tracker
from .arbitrage import decimal
from .books.http import HEADERS
from .matching import normalize, teams_match
from .schedule import ET

log = logging.getLogger("lines_agent")

BASE = "https://site.api.espn.com/apis/site/v2/sports/"
# league -> (scoreboard path, extra query)
FEEDS = {
    "NFL": ("football/nfl", ""),
    "NCAAF": ("football/college-football", "&groups=80"),   # FBS, plus FCS teams playing them
    "NBA": ("basketball/nba", ""),
    "NCAAB": ("basketball/mens-college-basketball", "&groups=50"),  # all of Division I
    "WNCAAB": ("basketball/womens-college-basketball", "&groups=50"),
    "NHL": ("hockey/nhl", ""),
    "MLB": ("baseball/mlb", ""),
    "NCAABASE": ("baseball/college-baseball", ""),
}
# Regulation: (periods, seconds per period); baseball counts innings instead
REGULATION = {
    "NFL": (4, 900), "NCAAF": (4, 900), "NBA": (4, 720), "NCAAB": (2, 1200),
    "WNCAAB": (4, 600), "NHL": (3, 1200), "MLB": (9, None), "NCAABASE": (9, None),
}
FOOTBALL = ("NFL", "NCAAF")  # leagues whose cards show who has the ball
TTL = 20          # seconds a scoreboard stays fresh
# ESPN marks these "post" with whatever score stood (often 0-0); books usually void them
CALLED_OFF = ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_SUSPENDED", "STATUS_FORFEIT")
LOOKBACK_H = 36   # open bets on games older than this are left to History
_cache: dict[tuple, tuple[float, list]] = {}
_cache_lock = threading.Lock()


def _ts(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _scoreboard(league: str, day: str) -> list[dict]:
    """ESPN games for one league on one US Eastern date (YYYYMMDD), cached briefly."""
    key = (league, day)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
    path, extra = FEEDS[league]
    url = f"{BASE}{path}/scoreboard?dates={day}&limit=500{extra}"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    events = resp.json().get("events", [])
    games = [g for g in (_game(e) for e in events) if g]
    now = time.time()
    with _cache_lock:
        _cache[key] = (now, games)
        # Drop old days so the cache doesn't grow for as long as the server runs
        for k in [k for k, (at, _) in _cache.items() if now - at > 3600]:
            del _cache[k]
    return games


def _game(e: dict) -> dict | None:
    try:
        comp = e["competitions"][0]
        st = e["status"]
        teams, ids = {}, {}
        for c in comp["competitors"]:
            t = c["team"]
            ids[str(c.get("id") or t.get("id"))] = c["homeAway"]
            teams[c["homeAway"]] = {
                "name": t.get("displayName") or t.get("name", ""),
                "abbr": t.get("abbreviation", ""),
                "score": int(float(c.get("score") or 0)),
                "names": [n for n in (t.get("displayName"), t.get("shortDisplayName"), t.get("location")) if n],
            }
        return {
            "id": e["id"], "start": e.get("date", ""),
            "state": st["type"]["state"],            # pre | in | post
            "detail": st["type"].get("shortDetail") or st["type"].get("detail", ""),
            "status_name": st["type"].get("name", ""),
            "period": int(st.get("period") or 0), "clock": float(st.get("clock") or 0),
            "away": teams["away"], "home": teams["home"],
            "tv": _networks(comp, {side: t["abbr"] for side, t in teams.items()}),
            **_situation(comp.get("situation") or {}, ids),
        }
    except (KeyError, TypeError, ValueError):
        return None


MAX_NETWORKS = 3
_MARKET_ORDER = {"national": 0, "home": 1, "away": 2}


def _networks(comp: dict, abbr: dict) -> list[str]:
    """Where the game is on, national first ("NBC", "ESPN+"), then each team's
    local channel with its abbreviation ("NESN (BOS)"); TV before streaming
    within each. Empty when ESPN lists nothing."""
    found = []  # (market, is_streaming, name)
    for g in comp.get("geoBroadcasts") or []:
        name = (g.get("media") or {}).get("shortName")
        market = str((g.get("market") or {}).get("type", "")).lower()
        if name:
            found.append((market, (g.get("type") or {}).get("shortName") != "TV", name))
    if not found:  # older feeds only have the plain list
        for b in comp.get("broadcasts") or []:
            found += [(str(b.get("market", "")).lower(), False, n) for n in b.get("names") or [] if n]
    out = []
    for market, _, name in sorted(found, key=lambda f: (_MARKET_ORDER.get(f[0], 3), f[1])):
        label = f"{name} ({abbr[market]})" if market in ("home", "away") and abbr.get(market) else name
        if not any(o == label or o.startswith(name + " (") or o == name for o in out):
            out.append(label)
    return out[:MAX_NETWORKS]


def _situation(sit: dict, ids: dict) -> dict:
    """Football only: which side has the ball ("away"/"home") and the down and
    distance. ESPN leaves these out between plays it can't place (kickoffs,
    timeouts, reviews, breaks), so either can be None."""
    side = ids.get(str(sit.get("possession")))
    return {"possession": side,
            "down": sit.get("downDistanceText") if side else None,
            "red_zone": bool(sit.get("isRedZone")) and side is not None}


def _same_team(name: str, team: dict) -> bool:
    n = normalize(name)
    return any(teams_match(n, normalize(x)) for x in team["names"])


def _find(bet: dict, games: list[dict]) -> tuple[dict, bool] | None:
    """The ESPN game for a bet, and whether ESPN lists the teams the other way round."""
    start = _ts(bet.get("start_time", ""))
    best = None
    for g in games:
        gs = _ts(g["start"])
        if start and gs and abs((gs - start).total_seconds()) > 12 * 3600:
            continue
        if _same_team(bet["away_team"], g["away"]) and _same_team(bet["home_team"], g["home"]):
            swapped = False
        elif _same_team(bet["away_team"], g["home"]) and _same_team(bet["home_team"], g["away"]):
            swapped = True   # neutral site, listed the other way
        else:
            continue
        gap = abs((gs - start).total_seconds()) if start and gs else 0
        if best is None or gap < best[2]:
            best = (g, swapped, gap)
    return (best[0], best[1]) if best else None


def _elapsed(league: str, g: dict) -> float | None:
    """Share of regulation played, 0..1 (None if unknown)."""
    periods, length = REGULATION.get(league, (None, None))
    if not periods or g["state"] not in ("in", "post"):
        return None
    if g["state"] == "post":
        return 1.0
    p = g["period"]
    if p <= 0:
        return 0.0
    if length is None:  # baseball: "Top 5th", "Mid 5th", "Bot 5th", "End 5th"
        d = g["detail"].lower()
        part = 0.25 if d.startswith("top") else 0.5 if d.startswith("mid") else 0.75 if d.startswith("bot") else 1.0
        return min((p - 1 + part) / periods, 1.0)
    done = (p - 1) * length + max(length - g["clock"], 0)
    return min(done / (periods * length), 1.0)


def grade(bet: dict, g: dict, swapped: bool) -> dict:
    """How the bet stands if the game ended now."""
    away, home = (g["home"], g["away"]) if swapped else (g["away"], g["home"])
    total = away["score"] + home["score"]
    market, side, line = bet.get("market"), bet.get("side"), bet.get("line")
    # trend: the arrow, which the projected money follows. now: the result if the
    # game ended this second (what a finished game is graded on).
    out = {"trend": None, "now": None, "margin": None, "text": "", "now_text": None, "pace": None}
    if g["status_name"] in CALLED_OFF:
        # Not a result: suggest a push (the usual void) rather than grading the score
        out["text"] = "Called off; usually voided"
        if g["state"] == "post":
            out["result"] = "push"
        return out
    if bet.get("player") or market not in ("moneyline", "spread", "total"):
        out["text"] = "Score only; no live player stats"
        return out
    if market == "total":
        if line is None or side not in ("over", "under"):
            return out
        diff = total - line if side == "over" else line - total
        share = _elapsed(bet.get("league", ""), g)
        if share and share >= 0.1 and g["state"] == "in":
            out["pace"] = round(total / share, 1)
        if diff > 0:
            out["text"] = f"{'Over' if side == 'over' else 'Under'} by {_num(diff)}"
        elif diff == 0:
            out["text"] = "Right on the number"
        else:
            out["text"] = (f"Needs {_num(-diff)} more" if side == "over"
                           else f"Over the line by {_num(-diff)}")
    else:
        if side not in ("away", "home"):
            return out
        mine, theirs = (away, home) if side == "away" else (home, away)
        diff = mine["score"] - theirs["score"] + ((line or 0) if market == "spread" else 0)
        lead = mine["score"] - theirs["score"]
        if market == "moneyline":
            out["text"] = ("Tied" if lead == 0 else
                           f"Leading by {_num(lead)}" if lead > 0 else f"Trailing by {_num(-lead)}")
        else:
            out["text"] = ("Pushing" if diff == 0 else
                           f"Covering by {_num(diff)}" if diff > 0 else f"Short by {_num(-diff)}")
    out["margin"] = diff
    out["now"] = out["trend"] = _sign(diff)
    if g["state"] == "post":
        out["result"] = {"up": "won", "down": "lost", "flat": "push"}[out["now"]]
    elif market == "total" and out["pace"] is not None and total <= line:
        # Not decided yet: point the arrow where the pace says it ends up
        pace = out["pace"]
        out["trend"] = _sign(pace - line if side == "over" else line - pace)
        out["now_text"], out["text"] = out["text"], (
            "On pace for the over" if pace > line else
            "On pace for the under" if pace < line else "On pace to land on the line")
    return out


def _sign(x: float) -> str:
    return "up" if x > 0 else "down" if x < 0 else "flat"


def _num(x: float) -> str:
    return f"{x:g}"


def _projected(bet: dict, trend: str | None) -> float | None:
    """Profit if the bet finishes the way it's trending."""
    if trend == "up":
        return round(bet["stake"] * (decimal(bet["odds"]) - 1), 2)
    if trend == "down":
        return -bet["stake"]
    if trend == "flat":
        return 0.0
    return None


def live() -> dict:
    """Open bets on games that have started, with the score and how each stands."""
    now = datetime.now(timezone.utc)
    bets = [b for b in tracker.summary()["bets"] if b["status"] == "open" and b.get("league") in FEEDS]
    started = []
    for b in bets:
        st = _ts(b.get("start_time", ""))
        if st and now - timedelta(hours=LOOKBACK_H) <= st <= now + timedelta(minutes=10):
            started.append((b, st))

    # One scoreboard per league per Eastern date (ESPN's day), fetched in parallel
    keys = sorted({(b["league"], st.astimezone(ET).strftime("%Y%m%d")) for b, st in started})
    boards, errors = {}, []

    def fetch(k):
        try:
            return k, _scoreboard(*k)
        except Exception as e:  # one feed down shouldn't hide the rest
            log.warning("live scores %s %s: %s", *k, e)
            return k, None

    if keys:
        with ThreadPoolExecutor(max_workers=min(8, len(keys))) as pool:
            for k, games in pool.map(fetch, keys):
                boards[k] = games
                if games is None:
                    errors.append(k[0])

    games: dict[str, dict] = {}
    unmatched = []
    for b, st in started:
        day = st.astimezone(ET).strftime("%Y%m%d")
        board = boards.get((b["league"], day))
        # A late tip can sit on the next ESPN day; look there too
        found = _find(b, board or [])
        if not found:
            nxt = (st.astimezone(ET) + timedelta(days=1)).strftime("%Y%m%d")
            if (b["league"], nxt) in boards:
                found = _find(b, boards[(b["league"], nxt)] or [])
        if not found:
            if board is not None and st <= now:
                unmatched.append(_bet_view(b, {"trend": None, "text": "No score found", "margin": None, "pace": None}))
            continue
        g, swapped = found
        if g["state"] == "pre":
            continue
        gr = grade(b, g, swapped)
        gid = f"{b['league']}:{g['id']}"
        card = games.setdefault(gid, {
            "id": gid, "league": b["league"], "state": g["state"], "detail": g["detail"],
            "halftime": g["status_name"] == "STATUS_HALFTIME",
            "away": {k: g["away"][k] for k in ("name", "abbr", "score")},
            "home": {k: g["home"][k] for k in ("name", "abbr", "score")},
            "tv": g["tv"],
            "start": g["start"], "bets": [],
            # Who has the ball, on ESPN's away/home, which is how the card lists the teams
            **({k: g[k] for k in ("possession", "down", "red_zone")}
               if b["league"] in FOOTBALL and g["state"] == "in" else {}),
        })
        view = _bet_view(b, gr)
        away, home = (g["home"], g["away"]) if swapped else (g["away"], g["home"])
        view["team"] = {"away": away, "home": home}.get(b.get("side"), {}).get("name")
        card["bets"].append(view)

    cards = sorted(games.values(), key=lambda c: (c["state"] != "in", c["start"]))
    live_cards = [c for c in cards if c["state"] == "in"]
    final_cards = [c for c in cards if c["state"] == "post"]

    def total(cs, field):
        return round(sum(x[field] or 0 for c in cs for x in c["bets"]), 2)

    graded = [x for c in live_cards for x in c["bets"]]
    upcoming = sorted(((st, b) for b in bets if (st := _ts(b.get("start_time", ""))) and st > now), key=lambda x: x[0])
    nxt = None
    if upcoming:
        first = upcoming[0][0]
        nxt = {"start": first.isoformat(), "count": sum(st == first for st, _ in upcoming),
               "pick": upcoming[0][1].get("pick"), "later": len(upcoming)}
    return {
        "at": now.isoformat(timespec="seconds"),
        "next": nxt,
        "live": live_cards,
        "final": final_cards,
        "unmatched": unmatched,
        "errors": sorted(set(errors)),
        "summary": {
            "games": len(live_cards),
            "bets": len(graded),
            "stake": total(live_cards, "stake"),
            "projected": total(live_cards, "projected"),
            "up": sum(x["trend"] == "up" for x in graded),
            "down": sum(x["trend"] == "down" for x in graded),
            "flat": sum(x["trend"] == "flat" for x in graded),
            "final_projected": total(final_cards, "projected"),
        },
    }


def _bet_view(b: dict, gr: dict) -> dict:
    return {
        "id": b["id"], "group": b.get("group"), "kind": b.get("kind"), "book": b.get("book"),
        "pick": b.get("pick"), "odds": b["odds"], "stake": b["stake"],
        "to_win": round(b["stake"] * (decimal(b["odds"]) - 1), 2),
        "market": b.get("market"), "side": b.get("side"), "line": b.get("line"),
        "away_team": b.get("away_team"), "home_team": b.get("home_team"), "notes": b.get("notes") or "",
        **gr, "projected": _projected(b, gr.get("trend")),
    }
