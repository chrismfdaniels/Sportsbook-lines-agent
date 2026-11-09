"""Bets you actually placed, for tracking profit and loss. Stored in
data/bets.json so the page, the phone and the Mac app share them.

  {"starting_bankroll": 300.0,
   "bets": [{"id": "B12", "group": "ARB-3" | null, "kind": "ev" | "arb", ...}]}

A bet is one wager at one book. An arbitrage is two bets with the same
"group". Settling a bet sets its status:

  open     not settled yet
  won      profit = stake * (decimal - 1)
  lost     profit = -stake
  push     stake returned, profit 0 (also for a voided bet)
  cashout  profit = cashout - stake
"""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path

from .arbitrage import decimal

FILE = Path(__file__).resolve().parent.parent / "data" / "bets.json"
STATUSES = ("open", "won", "lost", "push", "cashout")
# Copied from the card that was tracked, so the tracker can show what the bet looked like
CONTEXT = ("league", "away_team", "home_team", "start_time", "market", "side", "line",
           "player", "stat", "fair_odds", "ev_pct", "method", "methods", "kelly_pct", "profit_pct")
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    try:
        data = json.loads(FILE.read_text())
        return {"starting_bankroll": float(data.get("starting_bankroll") or 0), "bets": list(data.get("bets", []))}
    except (OSError, ValueError):
        return {"starting_bankroll": 0.0, "bets": []}


def _store(data: dict):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(FILE)


def _odds(v) -> int:
    o = int(v)
    if -100 < o < 100:
        raise ValueError(f"bad American odds {v!r}")
    return o


def _money(v, what: str) -> float:
    x = round(float(v), 2)
    if not math.isfinite(x):
        raise ValueError(f"{what} must be a number")
    if x < 0:
        raise ValueError(f"{what} can't be negative")
    return x


def profit(b: dict) -> float | None:
    """Settled profit or loss of one bet; None while it's open."""
    s = b["status"]
    if s == "won":
        return round(b["stake"] * (decimal(b["odds"]) - 1), 2)
    if s == "lost":
        return -b["stake"]
    if s == "push":
        return 0.0
    if s == "cashout":
        return round((b.get("cashout") or 0) - b["stake"], 2)
    return None


def _next_id(bets: list[dict], prefix: str, key: str) -> str:
    nums = [int(b[key][len(prefix):]) for b in bets if (b.get(key) or "").startswith(prefix) and b[key][len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1}"


def add(kind: str, legs: list[dict], notes: str = "") -> list[dict]:
    """Track a placed bet: kind "ev" with one leg, or "arb" with two. Each leg is
    {"book", "odds", "stake", "pick"} plus the card's context fields (CONTEXT)."""
    if kind not in ("ev", "arb") or len(legs) != (1 if kind == "ev" else 2):
        raise ValueError("an EV bet has one leg, an arbitrage two")
    with _lock:
        data = _load()
        group = _next_id(data["bets"], "ARB-", "group") if kind == "arb" else None
        added = []
        for leg in legs:
            stake = _money(leg.get("stake", 0), "stake")
            if stake <= 0:
                raise ValueError("enter a stake above $0")
            bet = {
                "id": _next_id(data["bets"] + added, "B", "id"), "group": group, "kind": kind,
                "placed_at": _now(), "book": str(leg.get("book", ""))[:40],
                "pick": str(leg.get("pick", ""))[:120], "odds": _odds(leg.get("odds")), "stake": stake,
                "status": "open", "cashout": None, "settled_at": None, "notes": str(notes)[:300],
                **{k: leg[k] for k in CONTEXT if k in leg},
            }
            added.append(bet)
        data["bets"] += added
        _store(data)
    return added


def update(bet_id: str, changes: dict) -> dict:
    """Settle or correct a bet: status, cashout, stake, odds, notes."""
    with _lock:
        data = _load()
        bet = next((b for b in data["bets"] if b["id"] == bet_id), None)
        if not bet:
            raise ValueError(f"no bet {bet_id}")
        if "stake" in changes:
            bet["stake"] = _money(changes["stake"], "stake")
            if bet["stake"] <= 0:
                raise ValueError("enter a stake above $0")
        if "odds" in changes:
            bet["odds"] = _odds(changes["odds"])
        if "notes" in changes:
            bet["notes"] = str(changes["notes"])[:300]
        if "status" in changes:
            status = changes["status"]
            if status not in STATUSES:
                raise ValueError(f"bad status {status!r}")
            if status == "cashout":
                bet["cashout"] = _money(changes.get("cashout", bet.get("cashout") or 0), "cash-out amount")
            if status == "open":
                bet["settled_at"] = None
            elif status != bet["status"] or not bet.get("settled_at"):
                bet["settled_at"] = _now()
            bet["status"] = status
            if status != "cashout":
                bet["cashout"] = None
        _store(data)
        return bet


def delete(bet_id: str):
    with _lock:
        data = _load()
        data["bets"] = [b for b in data["bets"] if b["id"] != bet_id]
        _store(data)


def set_bankroll(amount) -> float:
    with _lock:
        data = _load()
        data["starting_bankroll"] = _money(amount, "bankroll")
        _store(data)
        return data["starting_bankroll"]


def bankroll() -> float:
    """Starting bankroll plus settled P&L: what stakes are sized from."""
    data = _load()
    return round(data["starting_bankroll"] + sum(profit(b) or 0 for b in data["bets"]), 2)


def _totals(bets: list[dict]) -> dict:
    settled = [b for b in bets if b["status"] != "open"]
    pnl = sum(profit(b) for b in settled)
    staked = sum(b["stake"] for b in settled)
    return {
        "bets": len(bets), "settled": len(settled),
        "won": sum(b["status"] == "won" for b in settled), "lost": sum(b["status"] == "lost" for b in settled),
        "push": sum(b["status"] == "push" for b in settled),
        "pnl": round(pnl, 2), "staked": round(staked, 2),
        "roi_pct": round(pnl / staked * 100, 2) if staked else None,
        # EV bets only: what the edge said these bets should have made, for comparison with pnl
        "expected": round(sum(b["stake"] * b["ev_pct"] / 100 for b in settled if b.get("ev_pct") is not None), 2),
        "open_stake": round(sum(b["stake"] for b in bets if b["status"] == "open"), 2),
    }


def summary() -> dict:
    data = _load()
    bets = data["bets"]
    for b in bets:
        b["profit"] = profit(b)
    tot = _totals(bets)
    by_book = {}
    for book in sorted({b["book"] for b in bets}):
        by_book[book] = _totals([b for b in bets if b["book"] == book])
    # Cumulative P&L by settle time, for the chart
    running, curve = 0.0, []
    for b in sorted((b for b in bets if b["status"] != "open"), key=lambda b: b["settled_at"] or ""):
        running += b["profit"]
        curve.append({"at": b["settled_at"], "pnl": round(running, 2), "id": b["id"]})
    return {
        "starting_bankroll": data["starting_bankroll"],
        "bankroll": round(data["starting_bankroll"] + tot["pnl"], 2),
        "total": tot,
        "ev": _totals([b for b in bets if b["kind"] == "ev"]),
        "arb": _totals([b for b in bets if b["kind"] == "arb"]),
        "by_book": by_book,
        "curve": curve,
        "bets": sorted(bets, key=lambda b: b["placed_at"], reverse=True),
    }
