"""Daily scrape schedule for the web server (US Mountain time).

The runs live in data/settings.json (edited on the page): each has a time and
its own scan profile (leagues, books, markets, prop stats). At each time, that
profile is re-scraped into the server's cache (so the page shows the new lines)
and checked for +EV lines; any found are announced as a Mac banner and a push to
subscribed phones. Arbitrage is on demand only: the Mac app's Scan Now checks
and announces it, scheduled runs don't. If the Mac is asleep at a scheduled time, the missed run
happens once, as soon as it wakes. Edits take effect within a few seconds.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import push, settings, tracker

ET = ZoneInfo("America/New_York")  # game-day dates for props (arbitrage.py)
MT = ZoneInfo("America/Denver")    # run times and everything shown
DEFAULT_TIMES = ("07:07", "09:37", "13:57", "17:27", "19:07")
log = logging.getLogger("lines_agent")

state = {"times": list(DEFAULT_TIMES), "next_run": None, "last_run": None, "last_result": None,
         "running": False, "running_label": None}
_changed = threading.Event()
# Set by the web server: profile -> (+EV bets in the leagues a run just pulled, de-vig method)
ev_check = None


def reload():
    """Settings changed: the scheduler re-reads its runs right away."""
    _changed.set()


def next_after(now: datetime, times) -> datetime:
    for day in range(2):
        for t in sorted(times):
            h, m = map(int, t.split(":"))
            cand = (now + timedelta(days=day)).replace(hour=h, minute=m, second=0, microsecond=0)
            if cand > now:
                return cand
    raise ValueError("no schedule times")


def _notify(title: str, message: str):
    # macOS banner; harmless no-op elsewhere.
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    script = f"display notification {q(message)} with title {q(title)}"
    try:
        subprocess.run(["osascript", "-e", script], timeout=10, check=False, capture_output=True)
    except Exception:
        pass


def run_once(scan, min_profit: float, profile=None, label: str = "Scheduled scrape", arbs: bool = True,
             notify: bool = True):
    """Pull the profile's lines, then announce +EV lines, and arbitrage too if arbs.
    Without notify, only the counts are recorded (for the page's own Scan button)."""
    state["running"], state["running_label"] = True, label
    started = datetime.now(MT)
    log.info("%s starting: %s", label, profile or "everything")
    try:
        result = scan(min_profit, include_live=False, refresh=True, bankroll=max(tracker.bankroll(), 0) or 100.0, profile=profile)
        found = result["arbs"] if arbs else []
        state["last_result"] = {"arbs": len(found) if arbs else None, "errors": result["errors"], "label": label}
        if arbs:
            log.info("%s done: %d arbitrage opportunities >= %.1f%%", label, len(found), min_profit * 100)
        if found and notify:
            best = found[0]
            title = f"Arbitrage: {len(found)} at {min_profit * 100:g}%+"
            if best["market"] == "prop":
                what = f"{best['player']} {best['stat']} ({best['away_team']} @ {best['home_team']})"
            else:
                what = f"{best['away_team']} @ {best['home_team']} ({best['market']})"
            body = f"Best +{best['profit_pct']}%: {what}"
            _notify(title, body)
            try:
                push.send_all(title, body)
            except Exception:
                log.exception("Push to phones failed")
        if ev_check:
            _announce_ev(*ev_check(profile), label, notify)
    except Exception as e:
        log.exception("%s failed", label)
        state["last_result"] = {"error": str(e), "label": label}
    finally:
        state["last_run"] = started.isoformat(timespec="seconds")
        state["running"], state["running_label"] = False, None


def _announce_ev(bets: list[dict], method: str, label: str, notify: bool = True):
    state["last_result"]["ev"] = len(bets)
    log.info("%s: %d +EV lines", label, len(bets))
    if not bets or not notify:
        return
    best = bets[0]
    name = "all methods" if method == "all" else method.capitalize()
    title = f"+EV ({name}): {len(bets)} line{'s' if len(bets) != 1 else ''}"
    body = (f"Best +{best['ev_pct']}%: {best['book']} {best['odds']:+d} "
            f"({best['away_team']} @ {best['home_team']}, {best['market']} {best['side']})")
    _notify(title, body)
    try:
        push.send_all(title, body, url="/ev", tag="ev")
    except Exception:
        log.exception("Push to phones failed")


def _runs() -> list[dict]:
    return [r for r in settings.load()["runs"] if r["enabled"]]


def start(scan, min_profit: float = 0.01):
    def plan(now):
        runs = _runs()
        state["times"] = [r["time"] for r in runs]
        if not runs:
            state["next_run"] = None
            return None, []
        nxt = next_after(now, state["times"])
        state["next_run"] = nxt.isoformat(timespec="seconds")
        return nxt, [r for r in runs if r["time"] == nxt.strftime("%H:%M")]

    first, _ = plan(datetime.now(MT))

    def loop():
        nxt, due = plan(datetime.now(MT))
        while True:
            # One bad pass (a settings file mid-write, say) must not end the
            # scheduler thread, which would silently stop every future run.
            try:
                # Poll rather than sleep until the target, so a laptop waking from
                # sleep notices a missed run within a few seconds.
                if _changed.wait(20):
                    _changed.clear()
                    nxt, due = plan(datetime.now(MT))
                now = datetime.now(MT)
                if nxt and now >= nxt:
                    for r in due:
                        profile = {k: r[k] for k in ("leagues", "books", "markets", "stats")}
                        run_once(scan, min_profit, profile, f"{r['time']} MT run", arbs=False)
                    nxt, due = plan(datetime.now(MT))
            except Exception:
                log.exception("Scheduler pass failed; retrying")
                time.sleep(20)
                nxt, due = None, []
                try:
                    nxt, due = plan(datetime.now(MT))
                except Exception:
                    pass

    threading.Thread(target=loop, name="scheduler", daemon=True).start()
    log.info("Scheduled scrapes at %s MT; next at %s", ", ".join(state["times"]) or "(none)", first)
