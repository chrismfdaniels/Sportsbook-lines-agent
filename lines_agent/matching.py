"""Line up the same game across books when team names differ.

Pro teams are spelled almost the same everywhere, but college names vary:
FanDuel says "Florida @ Georgia", The Odds API "Florida Gators @ Georgia
Bulldogs", and others write "Miami (OH)" or "Sam Houston St". Two teams match
when one normalized name is a word-prefix of the other. Two events match when
both teams match and the start times are close. Every line in a matched game
then gets the longest (most complete) team names and the earliest start time,
so the rest of the agent can group by exact name.
"""
from __future__ import annotations

import re
from datetime import datetime

from .models import Line

# Whole-name aliases, compared after normalization
ALIASES = {
    "uconn": "connecticut",
    "ole miss": "mississippi",
    "umass": "massachusetts",
    "pitt": "pittsburgh",
    "lsu": "louisiana state",
    "ucf": "central florida",
    "smu": "southern methodist",
    "tcu": "texas christian",
    "byu": "brigham young",
    "unlv": "nevada las vegas",
    "utep": "texas el paso",
    "utsa": "texas san antonio",
    "fiu": "florida international",
    "fau": "florida atlantic",
    "app state": "appalachian state",
    "southern miss": "southern mississippi",
    "nc state": "north carolina state",
    "ul monroe": "louisiana monroe",
    "ulm": "louisiana monroe",
    "ul lafayette": "louisiana",
    "louisiana lafayette": "louisiana",
    "middle tennessee state": "middle tennessee",
    "uab": "alabama birmingham",
    "usc": "southern california",
}
ALIAS_PREFIXES = sorted(ALIASES, key=len, reverse=True)


def normalize(name: str) -> list[str]:
    s = name.lower().replace("&", " and ").replace("é", "e")
    s = re.sub(r"[().'’,]", " ", s)
    s = re.sub(r"[-/]", " ", s)
    words = s.split()
    out = []
    for i, w in enumerate(words):
        if w == "st":
            w = "saint" if i == 0 else "state"
        elif w == "univ":
            w = "university"
        elif w == "oh" and i > 0:
            w = "ohio"
        elif w == "fl" and i > 0:
            w = "florida"
        elif w == "fightin":
            w = "fighting"
        out.append(w)
    joined = " ".join(out)
    for a in ALIAS_PREFIXES:
        if joined == a or joined.startswith(a + " "):
            joined = ALIASES[a] + joined[len(a):]
            break
    return joined.split()


def teams_match(a: list[str], b: list[str]) -> bool:
    """'Florida' ~ 'Florida Gators' (prefix), and 'Delaware Blue Hens' ~
    'Delaware Fighting Blue Hens' (same school word and same mascot word).
    The opponent has to match too, which keeps loose matches honest."""
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if long_[: len(short)] == short:
        return True
    return len(short) >= 2 and a[0] == b[0] and a[-1] == b[-1]


def _max_gap_h(league: str) -> float:
    # Baseball teams play each other on back-to-back days, so stay tight there.
    # Elsewhere a team plays once a day at most, and some books post a
    # placeholder start time (BetRivers uses midnight ET for "time TBD").
    return 6 if league in ("MLB", "NCAABASE") else 20


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def unify_games(lines: list[Line]) -> list[Line]:
    events: dict[tuple, list[Line]] = {}
    for l in lines:
        events.setdefault((l.book, l.league, l.event_id, l.away_team, l.home_team), []).append(l)

    # Longest names first, so each cluster is named after its most complete spelling.
    order = sorted(events, key=lambda k: -(len(k[3]) + len(k[4])))
    clusters: list[dict] = []
    for key in order:
        _, league, _, away, home = key
        group = events[key]
        start = min(l.start_time for l in group)
        na, nh = normalize(away), normalize(home)
        target, swapped = None, False
        for c in clusters:
            if c["league"] != league or abs(_ts(c["start"]) - _ts(start)) > _max_gap_h(league) * 3600:
                continue
            if teams_match(na, c["na"]) and teams_match(nh, c["nh"]):
                target = c
                break
            # Neutral-site games: books sometimes disagree on who is "home".
            if teams_match(na, c["nh"]) and teams_match(nh, c["na"]):
                target, swapped = c, True
                break
        if target is None:
            target = {"league": league, "away": away, "home": home, "start": start, "na": na, "nh": nh}
            clusters.append(target)
        target["start"] = min(target["start"], start)
        target.setdefault("members", []).append((group, swapped))

    for c in clusters:
        for group, swapped in c["members"]:
            for l in group:
                l.away_team, l.home_team, l.start_time = c["away"], c["home"], c["start"]
                if swapped and l.side in ("away", "home"):
                    l.side = "home" if l.side == "away" else "away"
    return lines
