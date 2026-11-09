"""Scan profiles: which leagues, books, game-line markets and prop stats a scan
pulls. Saved to data/settings.json so the page, the Mac app and the scheduler
share them.

  {"manual": <profile>,                      # the controls on the page
   "runs": [{"time": "07:07", "enabled": true, ...<profile>}, ...],  # schedule, US Mountain
   "zone": "MT"}

Files saved before "zone" existed hold US Eastern times; they're read two hours
earlier (Eastern and Mountain change clocks on the same dates).

A profile is {"leagues": [...], "books": [...], "markets": [...], "stats": [...]}.
An empty "markets" skips game lines; an empty "stats" skips player props.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import props
from .books import ALL_BOOKS, VIA_ODDS_API, oddsapi
from .models import LEAGUES

FILE = Path(__file__).resolve().parent.parent / "data" / "settings.json"
MARKETS = ("moneyline", "spread", "total")
_lock = threading.Lock()


def full_profile() -> dict:
    return {"leagues": list(LEAGUES), "books": list(ALL_BOOKS), "markets": list(MARKETS), "stats": list(props.STATS)}


def clean_profile(p: dict | None) -> dict:
    """Keep only known values, in canonical order. Missing keys mean "all"."""
    p = p or {}
    full = full_profile()
    return {k: [v for v in full[k] if v in p.get(k, full[k])] for k in full}


def clean_run(r: dict) -> dict:
    t = str(r.get("time", ""))
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
        raise ValueError(f"bad time {t!r}; use 24-hour HH:MM")
    return {"time": t, "enabled": bool(r.get("enabled", True)), **clean_profile(r)}


def _default(times) -> dict:
    return {"manual": full_profile(), "runs": [{"time": t, "enabled": True, **full_profile()} for t in times]}


def _from_eastern(r: dict) -> dict:
    t = str(r.get("time", ""))
    if not re.fullmatch(r"\d\d:\d\d", t):
        return r  # clean_run reports it
    h, m = map(int, t.split(":"))
    return {**r, "time": f"{(h - 2) % 24:02d}:{m:02d}"}


def load(default_times=()) -> dict:
    """Settings from disk; the first time, every scheduled run scans everything."""
    with _lock:
        try:
            data = json.loads(FILE.read_text())
            runs = data.get("runs", [])
            if data.get("zone") != "MT":
                runs = [_from_eastern(r) for r in runs]
            return {"manual": clean_profile(data.get("manual")),
                    "runs": sorted((clean_run(r) for r in runs), key=lambda r: r["time"])}
        except (OSError, ValueError):
            return _default(default_times)


def save(data: dict) -> dict:
    """Validate and store {"manual": ..., "runs": [...]} (either key may be left out)."""
    cur = load()
    if "manual" in data:
        cur["manual"] = clean_profile(data["manual"])
    if "runs" in data:
        if len(data["runs"]) > 24:
            raise ValueError("at most 24 scheduled runs")
        cur["runs"] = sorted((clean_run(r) for r in data["runs"]), key=lambda r: r["time"])
    with _lock:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({**cur, "zone": "MT"}, indent=1))
        tmp.replace(FILE)
    return cur


def estimate(profile: dict, cache: dict) -> dict:
    """Odds API credits a scan with this profile would spend. Game lines cost one
    credit per market per in-season league; props cost up to one credit per stat
    per upcoming game the feed lists (counted from the cached lines). The direct books are free."""
    p = clean_profile(profile)
    api_books = [b for b in p["books"] if b in VIA_ODDS_API]
    out = {"lines": 0, "props": 0, "games": {}, "off_season": [], "api_books": api_books,
           "remaining": oddsapi.quota["remaining"], "quota_at": oddsapi.quota["at"]}
    if not api_books:
        return {**out, "total": 0}
    now = datetime.now(timezone.utc)
    for lg in p["leagues"]:
        try:
            active = oddsapi.in_season(lg)
        except Exception:
            active = None  # can't tell; count it
        if active is False:
            out["off_season"].append(lg)
            continue
        out["lines"] += len(p["markets"])
        if lg in props.LEAGUES and p["stats"]:
            # Odds API event ids of games that haven't started, as the props pull sees them
            games = {l["event_id"] for l in (cache.get(lg) or {}).get("lines", [])
                     if l["book"] in VIA_ODDS_API and _ts(l["start_time"]) > now}
            out["games"][lg] = len(games)
            out["props"] += len(games) * len(p["stats"])
    return {**out, "total": out["lines"] + out["props"]}


def _ts(iso: str) -> datetime:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
