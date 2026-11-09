"""Basketball player props: assists, rebounds, steals and turnovers.

Two kinds of market are collected:
  * Over/Under ("Kevin Durant - Assists, O/U 3.5"), both sides
  * "N+" milestones ("To record 4+ assists", Yes only). A Yes on N+ wins
    exactly when Over N-0.5 does, so it's stored as an Over at N-0.5.

Books post props on game day (usually morning ET), so outside that window
every book legitimately returns nothing.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import requests

from .books import VIA_ODDS_API, betrivers, fanduel, oddsapi, thescore
from .books.browser import BrowserSession
from .books.http import HEADERS
from .logs import redact
from .models import american_from_str, canonical_team

log = logging.getLogger("lines_agent")

STATS = ("assists", "rebounds", "steals", "turnovers")
LEAGUES = ("NBA", "NCAAB", "WNCAAB")


@dataclass
class Prop:
    book: str
    league: str
    event_id: str
    start_time: str
    away_team: str
    home_team: str
    player: str
    stat: str          # assists | rebounds | steals | turnovers
    side: str          # over | under
    line: float        # e.g. 3.5 (an "N+" Yes is stored as Over N-0.5)
    odds: int
    milestone: bool = False  # came from an "N+" Yes market
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def __post_init__(self):
        self.away_team = canonical_team(self.away_team)
        self.home_team = canonical_team(self.home_team)
        self.player = " ".join(self.player.split())

    def to_dict(self) -> dict:
        return asdict(self)


def player_key(name: str) -> str:
    """'Luka Dončić' / 'Luka Doncic' / 'Jaren Jackson Jr.' -> comparable key."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’,-]", " ", s)
    words = [w for w in s.split() if w not in ("jr", "sr", "ii", "iii", "iv")]
    return " ".join(words)


def _future(iso: str) -> bool:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except ValueError:
        return False


# ---------------------------------------------------------------- FanDuel
FD_OU = re.compile(r"^PLAYER_[A-Z]+_TOTAL_(ASSISTS|REBOUNDS|STEALS|TURNOVERS)$")
FD_MILESTONE = re.compile(r"^TO_RECORD_(\d+)\+_(ASSISTS|REBOUNDS|STEALS|TURNOVERS)$")
FD_TABS = ("Player Assists", "Player Rebounds", "Player Defense", "Player Turnovers")


def fanduel_props(league: str, state: str = "nj") -> list[Prop]:
    if league not in fanduel.PAGE_IDS:
        return []
    base = fanduel.URL.format(state=state).replace("content-managed-page", "event-page")
    sess = requests.Session()
    sess.headers.update(HEADERS)
    att = sess.get(fanduel.URL.format(state=state),
                   params={"page": "CUSTOM", "customPageId": fanduel.PAGE_IDS[league], "_ak": "FhMFpcPWXMeyZxOx"},
                   timeout=30).json().get("attachments", {})
    comps = att.get("competitions", {})
    events = [e for e in att.get("events", {}).values()
              if " @ " in e.get("name", "") and _future(e.get("openDate", ""))
              and fanduel._wanted_competition(league, comps.get(str(e.get("competitionId")), {}).get("name", ""))]

    out: list[Prop] = []
    for ev in events:
        away, home = (re.sub(r"\s*\(.*\)$", "", s).strip() for s in ev["name"].split(" @ ", 1))
        first = sess.get(base, params={"eventId": ev["eventId"], "_ak": "FhMFpcPWXMeyZxOx"}, timeout=30).json()
        titles = {t.get("title") for t in (first.get("layout", {}).get("tabs") or {}).values()}
        for title in (t for t in FD_TABS if t in titles):
            tab = title.lower().replace(" ", "-")
            data = sess.get(base, params={"eventId": ev["eventId"], "tab": tab, "_ak": "FhMFpcPWXMeyZxOx"},
                            timeout=30).json()
            for m in data.get("attachments", {}).get("markets", {}).values():
                if m.get("marketStatus") != "OPEN":
                    continue
                mt = m.get("marketType", "")
                common = dict(book=fanduel.NAME, league=league, event_id=str(ev["eventId"]),
                              start_time=ev["openDate"], away_team=away, home_team=home)
                if (ou := FD_OU.match(mt)):
                    player = m.get("marketName", "").split(" - ")[0]
                    for r in m.get("runners", []):
                        side = "over" if r.get("runnerName", "").endswith(" Over") else \
                               "under" if r.get("runnerName", "").endswith(" Under") else None
                        odds = (r.get("winRunnerOdds", {}).get("americanDisplayOdds") or {}).get("americanOddsInt")
                        if side and odds is not None and r.get("runnerStatus") == "ACTIVE" and r.get("handicap") is not None:
                            out.append(Prop(**common, player=player, stat=ou.group(1).lower(), side=side,
                                            line=float(r["handicap"]), odds=int(odds)))
                elif (ms := FD_MILESTONE.match(mt)):
                    for r in m.get("runners", []):
                        odds = (r.get("winRunnerOdds", {}).get("americanDisplayOdds") or {}).get("americanOddsInt")
                        if odds is not None and r.get("runnerStatus") == "ACTIVE":
                            out.append(Prop(**common, player=r["runnerName"], stat=ms.group(2).lower(), side="over",
                                            line=int(ms.group(1)) - 0.5, odds=int(odds), milestone=True))
    return out


# ---------------------------------------------------------------- BetRivers (Kambi)
KAMBI_OU = re.compile(r"^(Assists|Rebounds|Steals|Turnovers) by the player", re.I)
KAMBI_MILESTONE = re.compile(r"^(\d+)\+ (Assists|Rebounds|Steals|Turnovers) By The Player", re.I)


def betrivers_props(league: str, state: str = "nj") -> list[Prop]:
    if league not in betrivers.PATHS:
        return []
    params = {"lang": "en_US", "market": f"US-{state.upper()}", "client_id": 2, "channel_id": 1, "useCombined": "true"}
    lv = requests.get(betrivers.URL.format(state=state, path=betrivers.PATHS[league]),
                      params=params, headers=HEADERS, timeout=30).json()
    events = [i["event"] for i in lv.get("events", [])
              if "MATCH" in i["event"].get("tags", []) and i["event"].get("state") != "STARTED"]
    offer_url = f"https://eu.offering-api.kambicdn.com/offering/v2018/rsius{state}/betoffer/event/{{}}.json"

    def one(ev) -> list[Prop]:
        home, _, away = ev.get("englishName", "").partition(" - ")
        d = requests.get(offer_url.format(ev["id"]), params=params, headers=HEADERS, timeout=30).json()
        got = []
        common = dict(book=betrivers.NAME, league=league, event_id=str(ev["id"]), start_time=ev["start"],
                      away_team=away or ev.get("awayName", ""), home_team=home or ev.get("homeName", ""))
        for bo in d.get("betOffers", []):
            label = bo.get("criterion", {}).get("englishLabel", "")
            ou, ms = KAMBI_OU.match(label), KAMBI_MILESTONE.match(label)
            for o in bo.get("outcomes", []):
                odds = american_from_str(o.get("oddsAmerican"))
                if odds is None or o.get("status", "OPEN") != "OPEN" or not o.get("participant"):
                    continue
                if ou and o.get("type") in ("OT_OVER", "OT_UNDER") and o.get("line") is not None:
                    got.append(Prop(**common, player=_kambi_name(o["participant"]), stat=ou.group(1).lower(),
                                    side="over" if o["type"] == "OT_OVER" else "under",
                                    line=o["line"] / 1000, odds=odds))
                elif ms and o.get("type") == "OT_YES":
                    got.append(Prop(**common, player=_kambi_name(o["participant"]), stat=ms.group(2).lower(),
                                    side="over", line=int(ms.group(1)) - 0.5, odds=odds, milestone=True))
        return got

    out: list[Prop] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for got in pool.map(one, events):
            out += got
    return out


def _kambi_name(name: str) -> str:
    # Kambi sometimes writes "Last, First"
    last, sep, first = name.partition(", ")
    return f"{first} {last}" if sep else name


# ---------------------------------------------------------------- theScore Bet
SCORE_SECTIONS = ("player-assists", "player-rebounds", "player-defense", "player-turnovers")
SCORE_MARKET = re.compile(r"^(.+?) Total (Assists|Rebounds|Steals|Turnovers)$")


def _walk(obj, typename: str, out: list):
    if isinstance(obj, dict):
        if obj.get("__typename") == typename:
            out.append(obj)
        for v in obj.values():
            _walk(v, typename, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, typename, out)


def thescore_props(league: str, **_) -> list[Prop]:
    """Opens each game's page, then only the prop sections theScore says have content.
    Over/Unders are TOTAL markets; "N+" ladders are LIST markets. The player is
    in the market name, not on the selections."""
    if league not in thescore.PAGES:
        return []
    out: list[Prop] = []
    with BrowserSession() as br:
        cards: list = []
        for p in br.capture(thescore.PAGES[league], "/graphql/", wait_ms=6000, scrolls=3):
            thescore._cards(p, cards)
        games = {}
        for c in cards:
            ev = c.get("fallbackEvent") or (c.get("event") or {}).get("fallbackEvent") or {}
            link = (c.get("deepLink") or {}).get("webUrl")
            if ev and link and ev.get("status") != "IN_PLAY" and _future(ev.get("startTime", "")):
                games[link] = ev
        for link, ev in games.items():
            sections: list = []
            for p in br.capture("https://sportsbook.thescore.bet" + link, "/graphql/", wait_ms=5000):
                _walk(p, "Section", sections)
            live = [s["slug"] for s in sections if s.get("slug") in SCORE_SECTIONS and s.get("hasContent")]
            common = dict(book=thescore.NAME, league=league, event_id=ev["id"].split(":")[-1],
                          start_time=ev.get("startTime", ""),
                          away_team=(ev.get("awayParticipant") or {}).get("fullName", ""),
                          home_team=(ev.get("homeParticipant") or {}).get("fullName", ""))
            for slug in dict.fromkeys(live):
                markets: list = []
                for p in br.capture(f"https://sportsbook.thescore.bet{link}/section/{slug}", "/graphql/",
                                    wait_ms=5000, scrolls=3):
                    _walk(p, "Market", markets)
                seen = set()
                for m in markets:
                    # "<Player> Total Rebounds": full game only, so "1st Quarter ..."
                    # and combos like "Total Steals And Blocks" don't match.
                    nm = SCORE_MARKET.match(m.get("name") or "")
                    if m.get("status") != "OPEN" or not nm or m.get("id") in seen:
                        continue
                    seen.add(m.get("id"))
                    player, stat = nm.group(1), nm.group(2).lower()
                    for s in m.get("selections") or []:
                        odds = american_from_str((s.get("odds") or {}).get("formattedOdds"))
                        if odds is None or s.get("status") != "OPEN":
                            continue
                        if m.get("type") == "TOTAL" and s.get("type") in ("OVER", "UNDER"):
                            pts = (s.get("points") or {}).get("decimalPoints")
                            if pts is not None:
                                out.append(Prop(**common, player=player, stat=stat, side=s["type"].lower(),
                                                line=float(pts), odds=odds))
                        elif m.get("type") == "LIST":
                            # Milestone ladder: selections named "4+", "6+", ...
                            n = re.fullmatch(r"(\d+)\+", (s.get("name") or {}).get("fullName") or "")
                            if n:
                                out.append(Prop(**common, player=player, stat=stat, side="over",
                                                line=int(n.group(1)) - 0.5, odds=odds, milestone=True))
    return out


# ---------------------------------------------------------------- Odds API books (DraftKings, BetMGM, Caesars, Fanatics)
ODDS_MARKETS = {"player_assists": "assists", "player_rebounds": "rebounds",
                "player_steals": "steals", "player_turnovers": "turnovers"}


def oddsapi_props(league: str, books: list[str], stats=STATS, **_) -> list[Prop]:
    """Opt-in (ODDS_API_PROPS=1). Costs up to a credit per stat market returned
    (4 with every stat) per game per run, covering all the Odds API books at once."""
    markets = [k for k, st in ODDS_MARKETS.items() if st in stats]
    key = os.environ.get("ODDS_API_KEY")
    sport = oddsapi.SPORTS.get(league)
    if not key or not sport or not oddsapi._in_season(sport, key):
        return []
    base = f"https://api.the-odds-api.com/v4/sports/{sport}/events"
    events = requests.get(base, params={"apiKey": key}, timeout=30)  # listing events is free
    if not events.ok:
        raise RuntimeError(f"The Odds API returned HTTP {events.status_code}")
    out: list[Prop] = []
    for ev in events.json():
        if not _future(ev["commence_time"]):
            continue
        r = requests.get(f"{base}/{ev['id']}/odds", timeout=30, params={
            "apiKey": key, "markets": ",".join(markets), "oddsFormat": "american",
            "bookmakers": ",".join(oddsapi.BOOK_KEYS[b] for b in books)})
        if not r.ok:
            raise RuntimeError(f"The Odds API returned HTTP {r.status_code}: {r.text[:120]}")
        oddsapi.log_quota(r)
        for bm in r.json().get("bookmakers", []):
            for m in bm.get("markets", []):
                stat = ODDS_MARKETS.get(m["key"])
                for o in m.get("outcomes", []):
                    side = o.get("name", "").lower()
                    if stat and side in ("over", "under") and o.get("description") and o.get("point") is not None:
                        out.append(Prop(book=oddsapi.KEY_TO_NAME.get(bm["key"], bm["title"]), league=league,
                                        event_id=ev["id"], start_time=ev["commence_time"],
                                        away_team=ev["away_team"], home_team=ev["home_team"],
                                        player=o["description"], stat=stat, side=side,
                                        line=float(o["point"]), odds=int(o["price"])))
    return out


# ---------------------------------------------------------------- runner
def scrape_props(league: str, state: str = "nj", books=None, stats=STATS) -> tuple[list[Prop], dict[str, str]]:
    """books: which books to pull (default all); stats: which stats to keep."""
    books = books or [fanduel.NAME, betrivers.NAME, thescore.NAME] + VIA_ODDS_API
    jobs = {name: fn for name, fn in {
        fanduel.NAME: lambda: fanduel_props(league, state),
        betrivers.NAME: lambda: betrivers_props(league, state),
        thescore.NAME: lambda: thescore_props(league),
    }.items() if name in books}
    via_api = [b for b in VIA_ODDS_API if b in books]
    if via_api and os.environ.get("ODDS_API_PROPS") == "1":
        jobs["Odds API books"] = lambda: oddsapi_props(league, via_api, stats)
    if not jobs or not stats:
        return [], {}
    props: list[Prop] = []
    errors: dict[str, str] = {}

    def run(item):
        name, fn = item
        try:
            return name, fn(), None
        except Exception as e:  # one book failing must not sink the others
            return name, [], redact(f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        for name, got, err in pool.map(run, jobs.items()):
            if err:
                errors[f"{name} props"] = err
                log.warning("%s %s props failed: %s", name, league, err)
            else:
                got = [p for p in got if p.stat in stats]
                log.info("%s %s props: %d", name, league, len(got))
                props += got
    return props, errors
