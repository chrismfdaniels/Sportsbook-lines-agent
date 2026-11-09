"""Normalized line record shared by every sportsbook adapter."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone

LEAGUE_NAMES = {
    "NBA": "NBA",
    "NFL": "NFL",
    "NHL": "NHL",
    "MLB": "MLB",
    "NCAAF": "College Football",
    "NCAAB": "Men's College Basketball",
    "WNCAAB": "Women's College Basketball",
    "NCAABASE": "College Baseball",
}
LEAGUES = tuple(LEAGUE_NAMES)
MARKETS = ("moneyline", "spread", "total")

# Books spell a few teams differently; map those to one name so games line up.
TEAM_ALIASES = {
    "LA Clippers": "Los Angeles Clippers",
    "LA Lakers": "Los Angeles Lakers",
    "LA Chargers": "Los Angeles Chargers",
    "LA Rams": "Los Angeles Rams",
    "LA Dodgers": "Los Angeles Dodgers",
    "LA Angels": "Los Angeles Angels",
    "Oakland Athletics": "Athletics",
    "Sacramento Athletics": "Athletics",
    "Washington Football Team": "Washington Commanders",
}


def canonical_team(name: str) -> str:
    name = " ".join((name or "").split())
    name = re.sub(r"^\(\d+\)\s*", "", name)  # BetRivers prefixes AP rankings: "(3) Ohio State"
    return TEAM_ALIASES.get(name, name)


@dataclass
class Line:
    book: str          # e.g. "FanDuel"
    league: str        # a key of LEAGUE_NAMES
    event_id: str      # book-specific event id
    start_time: str    # ISO-8601 UTC
    away_team: str
    home_team: str
    market: str        # moneyline | spread | total
    side: str          # away | home | over | under
    line: float | None  # spread/total number; None for moneyline
    odds: int          # American odds
    live: bool = False
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def __post_init__(self):
        self.away_team = canonical_team(self.away_team)
        self.home_team = canonical_team(self.home_team)

    def to_dict(self) -> dict:
        return asdict(self)


def american_from_str(value) -> int | None:
    """'+150' / '-110' / 'EVEN' / 150 -> int American odds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().upper().replace("−", "-")
    if s in ("EVEN", "EV"):
        return 100
    try:
        return int(s.replace("+", ""))
    except ValueError:
        return None
