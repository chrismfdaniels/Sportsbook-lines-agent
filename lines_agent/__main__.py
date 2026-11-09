"""CLI: python -m lines_agent [--league NBA NCAAF] [--book FanDuel ...] [--watch 300]"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from . import agent, arbitrage, books, ev
from .models import LEAGUES


def _load_env(path: Path):
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        key, sep, val = raw.strip().partition("=")
        if sep and not key.startswith("#") and val:
            os.environ.setdefault(key.strip(), val.strip())


def main():
    root = Path(__file__).resolve().parent.parent
    _load_env(root / ".env")

    p = argparse.ArgumentParser(prog="lines_agent", description="Scrape NBA, NFL, NHL, MLB and college game lines from US sportsbooks.")
    p.add_argument("--league", nargs="+", choices=LEAGUES, default=list(LEAGUES), type=str.upper)
    p.add_argument("--book", nargs="+", choices=books.ALL_BOOKS, default=None,
                   help="defaults to every book")
    p.add_argument("--state", default="nj", help="state code for state-specific endpoints (default nj)")
    p.add_argument("--live", action="store_true", help="include in-play games")
    p.add_argument("--watch", type=int, metavar="SECONDS", help="re-scrape every N seconds")
    p.add_argument("--odds-api-every", type=int, metavar="SECONDS", default=3600,
                   help="in watch mode, refresh Odds API books at most this often (default 3600)")
    p.add_argument("--min-arb", type=float, default=1.0, metavar="PCT",
                   help="report moneyline/total arbitrage of at least this %% (default 1)")
    p.add_argument("--ev", action="store_true",
                   help="also pull the sharp books and list +EV lines at your books (see ev.py)")
    p.add_argument("--min-ev", type=float, default=5.0, metavar="PCT",
                   help="with --ev, list lines with at least this %% EV (default 5)")
    p.add_argument("--ev-method", choices=ev.METHODS + ("all",), default="multiplicative",
                   help="with --ev, de-vig method; all = every method must clear --min-ev")
    p.add_argument("--out", type=Path, default=root / "data")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    kw = dict(leagues=a.league, book_names=a.book, state=a.state, include_live=a.live)

    if a.watch:
        agent.watch(a.watch, a.out, odds_api_interval_s=a.odds_api_every, min_arb=a.min_arb / 100, **kw)
    else:
        if a.ev:
            kw["book_names"] = (a.book or books.ALL_BOOKS) + books.SHARP
        lines, errors = agent.scrape(**kw)
        json_path, csv_path = agent.save(lines, a.out)
        mine = [l for l in lines if l.book not in books.SHARP]
        agent.print_summary(mine, errors)
        arbitrage.print_arbs(arbitrage.find_arbs(mine, a.min_arb / 100, include_live=a.live), a.min_arb / 100)
        if a.ev:
            ev.print_ev(ev.find_ev(lines, a.min_ev / 100, include_live=a.live, method=a.ev_method),
                        a.min_ev / 100, a.ev_method)
        print(f"\nSaved {json_path}\nAppended {csv_path}")


if __name__ == "__main__":
    main()
