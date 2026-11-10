"""Bets you actually placed, for tracking profit and loss. Stored per person in
data/app.db (SQLite), shared by the private server and the public site.

  users: user, starting_bankroll
  bets:  user, id, data = {"id": "B12", "group": "ARB-3" | null,
                           "kind": "ev" | "arb" | "manual", ...}

"owner" is the Mac's owner, the only account the private server uses; on the
public site each signed-in email is its own account. Every function works on one
user's bets, so one person's actions can't reach another's. SQLite's locks keep
the two programs from overwriting each other. On first use, an existing
data/bets.json becomes the owner's account and is renamed bets.pre-database.json.

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
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .arbitrage import decimal

DATA = Path(__file__).resolve().parent.parent / "data"
DB = DATA / "app.db"
FILE = DATA / "bets.json"  # only read once, to bring existing bets into the database
OWNER = "owner"
KINDS = ("ev", "arb", "manual")
STATUSES = ("open", "won", "lost", "push", "cashout")
# Copied from the card that was tracked, so the tracker can show what the bet looked like
CONTEXT = ("league", "away_team", "home_team", "start_time", "market", "side", "line",
           "player", "stat", "fair_odds", "ev_pct", "method", "methods", "kelly_pct", "profit_pct")
BACKUP_DAYS = 90
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (user TEXT PRIMARY KEY, starting_bankroll REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bets (user TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (user, id));
CREATE TABLE IF NOT EXISTS members (email TEXT PRIMARY KEY, added_at TEXT NOT NULL);
"""
_prepared: set = set()


def _connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=15, isolation_level=None)  # transactions are explicit
    if DB not in _prepared:
        with _lock:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            _import_json(conn)
            _prepared.add(DB)
    return conn


def _import_json(conn: sqlite3.Connection):
    """Bring the old bets.json into the database as the owner's account, once."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        done = conn.execute("SELECT 1 FROM users WHERE user = ?", (OWNER,)).fetchone()
        if done or not FILE.exists():
            conn.execute("COMMIT")
            return
        old = json.loads(FILE.read_text())  # a damaged file stops here rather than start empty
        _write(conn, OWNER, {"starting_bankroll": float(old.get("starting_bankroll") or 0),
                             "bets": list(old.get("bets", []))})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    FILE.replace(FILE.with_name("bets.pre-database.json"))


def _read(conn: sqlite3.Connection, user: str) -> dict:
    row = conn.execute("SELECT starting_bankroll FROM users WHERE user = ?", (user,)).fetchone()
    bets = [json.loads(r[0]) for r in conn.execute("SELECT data FROM bets WHERE user = ? ORDER BY rowid", (user,))]
    return {"starting_bankroll": row[0] if row else 0.0, "bets": bets}


def _write(conn: sqlite3.Connection, user: str, data: dict):
    conn.execute("INSERT INTO users (user, starting_bankroll) VALUES (?, ?) "
                 "ON CONFLICT (user) DO UPDATE SET starting_bankroll = excluded.starting_bankroll",
                 (user, data["starting_bankroll"]))
    conn.execute("DELETE FROM bets WHERE user = ?", (user,))
    conn.executemany("INSERT INTO bets (user, id, data) VALUES (?, ?, ?)",
                     [(user, b["id"], json.dumps(b)) for b in data["bets"]])


def _load(user: str = OWNER) -> dict:
    conn = _connect()
    try:
        return _read(conn, user)
    finally:
        conn.close()


@contextmanager
def _tx(user: str = OWNER):
    """One user's data, saved when the block ends. Nothing else can write until
    then (in this program or the other), and an error saves nothing."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            data = _read(conn, user)
            yield data
            _write(conn, user, data)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _number(v, what: str) -> float:
    """A number from a request. true/false, text and Infinity are refused with a
    message instead of crashing the request (int(Infinity) raises OverflowError)."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"{what} must be a number")
    try:
        x = float(v)
    except ValueError:
        raise ValueError(f"{what} must be a number")
    if not math.isfinite(x):
        raise ValueError(f"{what} must be a number")
    return x


def _odds(v) -> int:
    o = int(_number(v, "odds"))
    if -100 < o < 100:
        raise ValueError(f"bad American odds {v!r}")
    return o


def _money(v, what: str) -> float:
    x = round(_number(v, what), 2)
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


def add(kind: str, legs: list[dict], notes: str = "", user: str = OWNER) -> list[dict]:
    """Track a placed bet: kind "ev" or "manual" with one leg, or "arb" with two. Each leg is
    {"book", "odds", "stake", "pick"} plus the card's context fields (CONTEXT)."""
    if kind not in KINDS or len(legs) != (2 if kind == "arb" else 1):
        raise ValueError("a single bet has one leg, an arbitrage two")
    with _tx(user) as data:
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
    return added


def update(bet_id: str, changes: dict, user: str = OWNER) -> dict:
    """Settle or correct a bet: status, cashout, stake, odds, notes."""
    with _tx(user) as data:
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
        return bet


def delete(bet_id: str, user: str = OWNER):
    with _tx(user) as data:
        data["bets"] = [b for b in data["bets"] if b["id"] != bet_id]


def set_bankroll(amount, user: str = OWNER) -> float:
    with _tx(user) as data:
        data["starting_bankroll"] = _money(amount, "bankroll")
        return data["starting_bankroll"]


def backup(keep_days: int = BACKUP_DAYS) -> Path:
    """Copy the database to data/backups/bets-<date>.db (one per day, the latest
    wins) and delete copies older than keep_days. It's on the same drive, so it
    guards against a bad edit or a damaged file, not against losing the disk."""
    folder = DB.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    today = datetime.now().date()
    dest = folder / f"bets-{today.isoformat()}.db"
    tmp = dest.with_suffix(".tmp")
    src, out = _connect(), sqlite3.connect(tmp)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    tmp.replace(dest)
    for old in folder.glob("bets-*"):
        m = re.fullmatch(r"bets-(\d{4}-\d{2}-\d{2})\.(db|json)", old.name)
        if m and datetime.strptime(m.group(1), "%Y-%m-%d").date() < today - timedelta(days=keep_days):
            old.unlink()
    return dest


def members() -> list[dict]:
    """Everyone invited to the public site, with how many bets they've saved."""
    conn = _connect()
    try:
        return [{"email": e, "added_at": at, "bets": n} for e, at, n in conn.execute(
            "SELECT m.email, m.added_at, COUNT(b.id) FROM members m LEFT JOIN bets b ON b.user = m.email "
            "GROUP BY m.email ORDER BY m.email")]
    finally:
        conn.close()


def is_member(email: str) -> bool:
    conn = _connect()
    try:
        return conn.execute("SELECT 1 FROM members WHERE email = ?", (email,)).fetchone() is not None
    finally:
        conn.close()


def add_member(email: str):
    conn = _connect()
    try:
        conn.execute("INSERT OR IGNORE INTO members (email, added_at) VALUES (?, ?)", (email, _now()))
    finally:
        conn.close()


def remove_member(email: str):
    """Ends their access; their bets stay, in case they're invited back."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM members WHERE email = ?", (email,))
    finally:
        conn.close()


def bankroll(user: str = OWNER) -> float:
    """Starting bankroll plus settled P&L: what stakes are sized from."""
    data = _load(user)
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


def summary(user: str = OWNER) -> dict:
    data = _load(user)
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
