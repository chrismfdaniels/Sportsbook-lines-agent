"""Cross-book arbitrage finder for moneylines and totals.

A two-way arbitrage exists when the implied probabilities of the two sides,
each taken at a different book, add up to less than 1. Stakes split in
proportion to those implied probabilities lock in the same payout either way:

    profit = 1 / (1/d_a + 1/d_b) - 1        (d = decimal odds)

Totals also qualify when the lines differ, as long as the over line is at or
below the under line (e.g. Over 44.5 / Under 45.5). Then no final score loses
both bets, and a score landing in between wins both (a "middle").
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from itertools import product

from .models import Line

PAIRS = {"moneyline": ("away", "home"), "total": ("over", "under")}


def decimal(american: int) -> float:
    return 1 + american / 100 if american > 0 else 1 + 100 / abs(american)


def _game_key(l: Line) -> tuple:
    return (l.league, l.away_team.lower(), l.home_team.lower(), l.start_time[:10])


def _started(iso: str) -> bool:
    """Whether the game has begun. Cached lines can be from before tip-off and
    still say live=False, but those prices are gone once it starts."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    except ValueError:
        return False


def find_arbs(lines: list[Line], min_profit: float = 0.01, include_live: bool = False,
              bankroll: float = 100.0) -> list[dict]:
    """Every cross-book pairing whose guaranteed return is at least min_profit (0.01 = 1%)."""
    sides: dict[tuple, list[Line]] = defaultdict(list)
    for l in lines:
        if l.market in PAIRS and (include_live or not (l.live or _started(l.start_time))):
            sides[_game_key(l) + (l.market, l.side)].append(l)

    games = {k[:4] for k in sides}
    arbs = []
    for g in games:
        for market, (side_a, side_b) in PAIRS.items():
            for a, b in product(sides.get(g + (market, side_a), []), sides.get(g + (market, side_b), [])):
                if a.book == b.book:
                    continue
                if market == "total" and not (a.line is not None and b.line is not None and a.line <= b.line):
                    continue
                inv_a, inv_b = 1 / decimal(a.odds), 1 / decimal(b.odds)
                total_inv = inv_a + inv_b
                profit = 1 / total_inv - 1
                if profit < min_profit:
                    continue
                legs = []
                for l, inv in ((a, inv_a), (b, inv_b)):
                    stake = bankroll * inv / total_inv
                    legs.append({"book": l.book, "side": l.side, "line": l.line, "odds": l.odds,
                                 "stake": round(stake, 2), "payout": round(stake * decimal(l.odds), 2)})
                arbs.append({
                    "league": a.league, "away_team": a.away_team, "home_team": a.home_team,
                    "start_time": min(a.start_time, b.start_time), "market": market,
                    "profit_pct": round(profit * 100, 2),
                    "middle": market == "total" and a.line < b.line,
                    "live": a.live or b.live,
                    "bankroll": bankroll, "legs": legs,
                })
    arbs.sort(key=lambda x: -x["profit_pct"])
    return arbs


def find_prop_arbs(props: list, min_profit: float = 0.01, bankroll: float = 100.0) -> list[dict]:
    """Over at one book + Under at another on the same player and stat, with the
    over line at or below the under line (same rule as game totals). A player
    plays once a day, so player + stat + Eastern-time date identifies the bet."""
    from .props import player_key  # local import: props imports the book adapters
    from .schedule import ET

    def day(iso: str) -> str:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).date().isoformat()
        except ValueError:
            return iso[:10]

    sides: dict[tuple, list] = defaultdict(list)
    for p in props:
        if _started(p.start_time):
            continue
        sides[(p.league, player_key(p.player), p.stat, day(p.start_time), p.side)].append(p)

    arbs = []
    for key, overs in sides.items():
        if key[-1] != "over":
            continue
        for o, u in product(overs, sides.get(key[:-1] + ("under",), [])):
            if o.book == u.book or o.line > u.line:
                continue
            inv_o, inv_u = 1 / decimal(o.odds), 1 / decimal(u.odds)
            total_inv = inv_o + inv_u
            profit = 1 / total_inv - 1
            if profit < min_profit:
                continue
            legs = []
            for p, inv in ((o, inv_o), (u, inv_u)):
                stake = bankroll * inv / total_inv
                legs.append({"book": p.book, "side": p.side, "line": p.line, "odds": p.odds,
                             "milestone": p.milestone, "stake": round(stake, 2),
                             "payout": round(stake * decimal(p.odds), 2)})
            arbs.append({
                "league": o.league, "away_team": o.away_team, "home_team": o.home_team,
                "start_time": min(o.start_time, u.start_time), "market": "prop",
                "player": o.player, "stat": o.stat,
                "profit_pct": round(profit * 100, 2), "middle": o.line < u.line, "live": False,
                "bankroll": bankroll, "legs": legs,
            })
    arbs.sort(key=lambda x: -x["profit_pct"])
    return arbs


def print_arbs(arbs: list[dict], min_profit: float):
    print(f"\nArbitrage (>= {min_profit * 100:g}% guaranteed): {len(arbs) or 'none found'}")
    for a in arbs:
        mid = "  [middle possible]" if a["middle"] else ""
        print(f"  {a['league']} {a['away_team']} @ {a['home_team']}  {a['market']}  +{a['profit_pct']}%{mid}")
        for leg in a["legs"]:
            line = "" if leg["line"] is None else f" {leg['line']:g}"
            print(f"      {leg['side']:<5}{line:<7} {leg['odds']:>+5} @ {leg['book']:<13} stake ${leg['stake']:.2f}")
