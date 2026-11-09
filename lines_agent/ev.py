"""+EV finder: price every side from the sharp books, then find your books' lines
that beat that price.

1. Implied probability of each sharp price:   p = 1 / decimal_odds
2. Remove the vig, three ways (each sharp book on its own):
   multiplicative  fair_a = p_a / (p_a + p_b). Every side gives up the same share
                   of its probability, so the favorite absorbs most of the vig.
   shin            Shin's model of a book guarding against a share z of insiders:
                   fair_i = (sqrt(z^2 + 4(1-z) p_i^2 / S) - z) / (2(1-z)), S = p_a + p_b,
                   with z solved so the fair probabilities sum to 1.
   power           fair_i = p_i ** k, with k solved so they sum to 1. Takes the
                   most vig off the longshot.
   Near even money the three agree; on big underdogs multiplicative rates the
   dog highest and power lowest (the favorite-longshot bias). The books are then
   blended into one consensus fair probability per method, a weighted average
   (SHARP_WEIGHTS) of the books that post that exact market and number.
3. EV at your book:                            EV = fair_p * decimal_odds - 1
   find_ev filters, sorts and sizes Kelly by one method, or by "all" (the lowest
   EV of the three, so a line passes only if every method agrees).

Spreads and totals are compared only at the same number (Pinnacle -3.5 prices
only your -3.5), since moving a point changes the probability by an amount that
depends on the sport and the number.

Kelly fraction of bankroll: (fair_p * d - 1) / (d - 1).
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

from .arbitrage import decimal
from .books import ALL_BOOKS, SHARP
from .models import Line

# Leagues the +EV page scans
LEAGUES = ("NBA", "NFL", "NHL", "NCAAF", "NCAAB", "WNCAAB")
# Blend weights for the consensus fair price. Circa and BetCRIS aren't on The
# Odds API; add them here once an adapter pulls them (see books/__init__.py SHARP).
SHARP_WEIGHTS = {"Pinnacle": 1.0}
PAIRS = {"moneyline": ("away", "home"), "spread": ("away", "home"), "total": ("over", "under")}
# Edges this big on a main line usually mean a stale or mismatched price.
CHECK_ABOVE = 0.20


def implied(american: int) -> float:
    return 1 / decimal(american)


METHODS = ("multiplicative", "shin", "power")


def _solve(f, lo: float, hi: float) -> float:
    """Bisect for x in [lo, hi] where f(x) = 0, f decreasing."""
    for _ in range(100):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def devig(american_a: int, american_b: int, method: str = "multiplicative") -> tuple[float, float]:
    """Fair probabilities of a two-way market with the vig removed."""
    pa, pb = implied(american_a), implied(american_b)
    s = pa + pb
    if method == "power":
        k = _solve(lambda k: pa ** k + pb ** k - 1, 0.1, 10)
        return pa ** k, 1 - pa ** k
    if method == "shin" and s > 1:
        def shin(z, p):
            return (math.sqrt(z * z + 4 * (1 - z) * p * p / s) - z) / (2 * (1 - z))
        z = _solve(lambda z: shin(z, pa) + shin(z, pb) - 1, 0, 0.5)
        fa = shin(z, pa)
        return fa, 1 - fa
    # multiplicative, and Shin on a market with no vig (its model needs some)
    return pa / s, pb / s


def american(p: float) -> int:
    """Fair probability -> American odds."""
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def _game_key(l: Line) -> tuple:
    return (l.league, l.away_team.lower(), l.home_team.lower(), l.start_time[:10])


def _number(l: Line) -> float | None:
    """One number per two-sided market: the home spread (the away side is its
    negative) or the total. None for moneylines."""
    if l.market == "moneyline" or l.line is None:
        return None
    n = -l.line if l.market == "spread" and l.side == "away" else l.line
    return round(n, 2)


def fair_prices(lines: list[Line]) -> dict[tuple, dict]:
    """(game, market, number) -> {"p": {method: {side: prob}}, "books": {book: {...}}}."""
    by_book: dict[tuple, dict[str, Line]] = defaultdict(dict)
    for l in lines:
        if l.book in SHARP_WEIGHTS and l.market in PAIRS and not l.live:
            by_book[_game_key(l) + (l.market, _number(l), l.book)][l.side] = l

    blends: dict[tuple, dict] = {}
    for key, sides in by_book.items():
        a, b = PAIRS[key[4]]
        if a not in sides or b not in sides:
            continue
        book = key[-1]
        g = blends.setdefault(key[:-1], {"books": {}})
        g["books"][book] = {
            "odds": {a: sides[a].odds, b: sides[b].odds},
            "fair": {m: dict(zip((a, b), devig(sides[a].odds, sides[b].odds, m))) for m in METHODS},
            "vig_pct": round((implied(sides[a].odds) + implied(sides[b].odds) - 1) * 100, 2),
            "scraped_at": min(sides[a].scraped_at, sides[b].scraped_at),
        }
    for key, g in blends.items():
        a, b = PAIRS[key[4]]
        w = {bk: SHARP_WEIGHTS[bk] for bk in g["books"]}
        total = sum(w.values())
        g["p"] = {}
        for m in METHODS:
            pa = sum(w[bk] * g["books"][bk]["fair"][m][a] for bk in w) / total
            g["p"][m] = {a: pa, b: 1 - pa}
    return blends


def find_ev(lines: list[Line], min_ev: float = 0.05, include_live: bool = False,
            books=ALL_BOOKS, method: str = "multiplicative") -> list[dict]:
    """Every line at your books whose EV against the consensus fair price is at
    least min_ev (0.05 = 5%) by `method`, or by every method for "all". The
    top-level fair/EV/Kelly fields are that method's (for "all", the lowest EV's);
    "methods" has all three."""
    fair = fair_prices(lines)
    now = datetime.now(timezone.utc)
    out = []
    for l in lines:
        if l.book not in books or l.book in SHARP or l.market not in PAIRS:
            continue
        # Cached lines can be from before the game started.
        started = l.live or _ts(l.start_time) <= now
        if started and not include_live:
            continue
        f = fair.get(_game_key(l) + (l.market, _number(l)))
        if not f:
            continue
        d = decimal(l.odds)
        evs = {m: f["p"][m][l.side] * d - 1 for m in METHODS}
        used = min(evs, key=evs.get) if method == "all" else method
        p, ev = f["p"][used][l.side], evs[used]
        if ev < min_ev:
            continue
        sharp_at = min(b["scraped_at"] for b in f["books"].values())
        out.append({
            "league": l.league, "away_team": l.away_team, "home_team": l.home_team,
            "start_time": l.start_time, "market": l.market, "side": l.side, "line": l.line,
            "book": l.book, "odds": l.odds, "scraped_at": l.scraped_at,
            "fair_prob": round(p, 4), "fair_odds": american(p),
            "ev_pct": round(ev * 100, 2), "kelly_pct": round(ev / (d - 1) * 100, 2), "method": used,
            "methods": {m: {"fair_prob": round(f["p"][m][l.side], 4), "fair_odds": american(f["p"][m][l.side]),
                            "ev_pct": round(evs[m] * 100, 2)} for m in METHODS},
            "sharp": {bk: {"odds": v["odds"][l.side], "other_odds": next(o for s, o in v["odds"].items() if s != l.side),
                           "fair_prob": {m: round(v["fair"][m][l.side], 4) for m in METHODS}, "vig_pct": v["vig_pct"]}
                      for bk, v in f["books"].items()},
            "sharp_scraped_at": sharp_at,
            "check": ev >= CHECK_ABOVE,
            "live": started,
        })
    out.sort(key=lambda x: -x["ev_pct"])
    return out


def _ts(iso: str) -> datetime:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def print_ev(bets: list[dict], min_ev: float, method: str = "multiplicative"):
    print(f"\n+EV vs sharp consensus (>= {min_ev * 100:g}% by {method}): {len(bets) or 'none found'}")
    for b in bets:
        line = "" if b["line"] is None else f" {b['line']:+g}" if b["market"] == "spread" else f" {b['line']:g}"
        sharp = ", ".join(f"{bk} {v['odds']:+d}/{v['other_odds']:+d}" for bk, v in b["sharp"].items())
        flag = "  [check: unusually large]" if b["check"] else ""
        print(f"  {b['league']} {b['away_team']} @ {b['home_team']}  {b['market']} {b['side']}{line}"
              f"  {b['odds']:+d} @ {b['book']}  fair {b['fair_odds']:+d}  EV +{b['ev_pct']}%{flag}")
        print("      " + "  ".join(f"{m} {v['ev_pct']:+.2f}% (fair {v['fair_odds']:+d})" for m, v in b["methods"].items()))
        print(f"      sharp: {sharp}")
