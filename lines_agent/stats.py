"""Numbers for the Stats page (/stats, desktop only): disk use, the server's
memory, Odds API credits, traffic and phone data, scrape and scheduled-run
health, and betting results. Read from the files on the Mac and from the logs
(logs.py); nothing extra is recorded to produce them.

Disk sizes take a moment to add up (the scraping browser is thousands of
files), so they're kept for an hour; everything else for a minute.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import logs, tracker
from .books import oddsapi

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATIC = Path(__file__).resolve().parent / "static"
MAC_APP = Path("/Applications/Sportsbook Lines.app")
PLAYWRIGHT = Path.home() / "Library" / "Caches" / "ms-playwright"
OLD_LOG = Path.home() / "Library" / "Logs" / "sportsbook-lines-agent.log"  # launchd's, before logs.py
LEAGUES = "NBA|NFL|NHL|MLB|NCAAF|NCAAB|WNCAAB|NCAABASE"

_lock = threading.Lock()
_memo: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, fn):
    with _lock:
        hit = _memo.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _lock:
        _memo[key] = (time.time(), value)
    return value


def _size(p: Path) -> int:
    """Bytes in a file or folder (without following links); 0 if it's missing."""
    try:
        if p.is_file():
            return p.stat().st_size
        total = 0
        for dirpath, dirnames, files in os.walk(p):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for f in files:
                try:
                    total += os.lstat(os.path.join(dirpath, f)).st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def disk() -> dict:
    code = sum(_size(p) for p in (ROOT / "lines_agent", ROOT / "macapp" / "SportsbookLines.swift",
                                  ROOT / "macapp" / "build.sh", ROOT / "README.md", ROOT / "launchd"))
    parts = [
        ("Scraping browser", _size(PLAYWRIGHT)),
        ("Line history", _size(DATA / "lines_history.csv")),
        ("Python libraries", _size(ROOT / ".venv")),
        ("Old snapshots", sum(p.stat().st_size for p in DATA.glob("lines_*.json"))),
        ("Lines cache", _size(DATA / "web_cache.json")),
        ("Logs", _size(logs.LOG_DIR) + _size(OLD_LOG)),
        ("Bets & settings", sum(_size(DATA / f) for f in ("app.db", "settings.json", "push_subscriptions.json"))),
        ("Code", code),
        ("Mac app", _size(MAC_APP)),
    ]
    usage = shutil.disk_usage("/")
    return {"parts": [{"name": n, "bytes": b} for n, b in parts], "total": sum(b for _, b in parts),
            "free": usage.free, "disk": usage.total}


def server(started: float) -> dict:
    rss = 0
    try:
        rss = int(subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True,
                                 text=True, timeout=5).stdout.strip() or 0) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    t = os.times()
    return {"memory": rss, "cpu_seconds": round(t.user + t.system, 1), "threads": threading.active_count(),
            "uptime_seconds": round(time.time() - started), "started": datetime.fromtimestamp(started).isoformat(timespec="seconds")}


# ---------------------------------------------------------------- logs
_REQ = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \w+\s+(GET|POST) (\S+) (\d+|no response)(?: \(client gone\))? "
                  r"(\S+?)(?:->(\S+) gz)? (\d+)ms (.+?)( SLOW)?$")


def _bytes(s: str) -> float:
    m = re.match(r"([\d.]+)(B|KB|MB)$", s or "")
    return float(m.group(1)) * {"B": 1, "KB": 1024, "MB": 1048576}[m.group(2)] if m else 0.0


def _log_lines(days: int, errors_only: bool = False) -> list[str]:
    """Log lines from the last `days` days, oldest first: the rotated daily files,
    today's file, and (for history before logs.py) the old launchd log."""
    name = "errors.log" if errors_only else "app.log"
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    files = sorted(p for p in logs.LOG_DIR.glob(name + ".*") if p.name[len(name) + 1:] >= cutoff)
    out = []
    if not errors_only and OLD_LOG.exists():
        out += [l for l in OLD_LOG.read_text(errors="ignore").splitlines() if l[:10] >= cutoff]
    for p in files + [logs.LOG_DIR / name]:
        try:
            out += [l for l in p.read_text(errors="ignore").splitlines() if l[:10] >= cutoff]
        except OSError:
            pass
    return out


def traffic() -> dict:
    """The last 24 hours of requests: by device and endpoint, status codes,
    response times, and data per page open (what the phone downloads)."""
    since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [m.groups() for l in _log_lines(2) if (m := _REQ.match(l)) and m.group(1) >= since]
    by_dev = defaultdict(lambda: {"requests": 0, "sent": 0.0, "raw": 0.0})
    by_ep = defaultdict(lambda: {"requests": 0, "sent": 0.0, "ms": 0})
    codes, times = Counter(), []
    sizes = defaultdict(lambda: [[], []])  # path -> [sent sizes, uncompressed sizes], real browsers only
    for _, method, path, status, size, gz, ms, device, slow in rows:
        sent, raw = _bytes(gz) if gz else _bytes(size), _bytes(size)
        dev = device.strip()
        dev = dev if dev in ("iPhone", "iPad", "Android", "Mac app", "Mac") or dev.startswith("iPhone") else "Other"
        d = by_dev[dev]; d["requests"] += 1; d["sent"] += sent; d["raw"] += raw
        p = path.split("?")[0]
        e = by_ep[p]; e["requests"] += 1; e["sent"] += sent; e["ms"] += int(ms)
        codes[status.split()[0]] += 1
        if p.startswith("/api/") and "refresh=1" not in path:
            times.append(int(ms))
        if dev != "Other" and status == "200":
            # "now" counts only responses sent the current way (compressed, or too small to
            # bother), so requests from before compression don't skew it
            if gz or raw < 1024:
                sizes[p][0].append(sent)
            sizes[p][1].append(raw)
    times.sort()
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    pages = {"Lines": ["/", "/theme.css", "/report.js", "/api/lines", "/api/props", "/api/settings", "/api/status",
                       "/api/arbs", "/api/bets", "/api/estimate"],
             "+EV": ["/ev", "/theme.css", "/report.js", "/api/ev", "/api/bets"],
             "History": ["/bets", "/theme.css", "/report.js", "/api/bets"],
             "Live": ["/live", "/theme.css", "/report.js", "/api/live"]}
    per_page = [{"page": name, "now": round(sum(avg(sizes[p][0]) for p in paths)),
                 "uncompressed": round(sum(avg(sizes[p][1]) for p in paths))} for name, paths in pages.items()]
    live = avg(sizes["/api/live"][0])
    return {
        "requests": len(rows), "codes": dict(codes),
        "server_errors": sum(n for c, n in codes.items() if c.isdigit() and int(c) >= 500),
        "slow": sum(1 for r in rows if r[8]),
        "median_ms": times[len(times) // 2] if times else None,
        "p95_ms": times[int(len(times) * 0.95)] if times else None,
        "devices": sorted(({"device": k, **{f: round(v) if f != "requests" else v for f, v in d.items()}}
                           for k, d in by_dev.items()), key=lambda x: -x["sent"]),
        "endpoints": sorted(({"path": k, "requests": e["requests"], "sent": round(e["sent"]),
                              "avg_ms": round(e["ms"] / e["requests"])} for k, e in by_ep.items()),
                            key=lambda x: -x["sent"])[:10],
        "per_page": per_page, "live_refresh": round(live), "live_hour": round(live * 120),
        "since": since,
    }


def health() -> dict:
    """The last 7 days (14 for credits): scrapes by book, scheduled runs, pushes,
    phone reports and recent problems."""
    lines = _log_lines(14)
    week = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    ok, failed = Counter(), Counter()
    runs = run_fail = pushes = push_sent = push_failed = 0
    readings = []
    for l in lines:
        if "requests remaining:" in l:
            m = re.search(r"remaining: (\d+)", l)
            if m:
                readings.append((l[:10], int(m.group(1))))
        if l[:10] < week:
            continue
        if (m := re.search(rf"(?:INFO|WARNING)\s+(.+?) ({LEAGUES})(?: props)?: \d+(?: lines?)?$", l)):
            ok[_book(m.group(1))] += 1
        elif (m := re.search(rf"WARNING\s+(.+?) ({LEAGUES}) (?:props )?failed: ", l)):
            failed[_book(m.group(1))] += 1
        elif "MT run starting" in l:
            runs += 1
        elif re.search(r"MT run failed", l):
            run_fail += 1
        elif (m := re.search(r"Push: (\d+) sent, (\d+) failed", l)):
            pushes += 1; push_sent += int(m.group(1)); push_failed += int(m.group(2))
    used = Counter()
    prev = None
    for day, v in readings:
        if prev is not None and v < prev:
            used[day] += prev - v
        prev = v
    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
    problems = [l for l in _log_lines(7, errors_only=True) if " WARNING " in l or " ERROR " in l or " CRITICAL " in l]
    books = sorted(set(ok) | set(failed))
    return {
        "scrapes": [{"book": b, "ok": ok[b], "failed": failed[b]} for b in books],
        "runs": runs, "run_failures": run_fail,
        "pushes": pushes, "push_sent": push_sent, "push_failed": push_failed,
        "credits_by_day": [{"day": d, "used": used[d]} for d in days],
        "credits_week": sum(used[d] for d in days[-7:]),
        "phone_reports": sum(1 for l in problems if " PHONE " in l),
        "problems": [l[:300] for l in problems[-12:]][::-1],
        "credits_last": readings[-1][1] if readings else None,  # after a restart, until the next pull
    }


def _book(label: str) -> str:
    return "The Odds API" if label.startswith("OddsAPI") or label.startswith("Odds API") else label


def phone_storage(cache: dict) -> dict:
    """What the phone app keeps for offline use: the app's pages and icons, one copy
    of each data feed, and the lines/props of each sport it has opened."""
    shell = sum(_size(STATIC / f) for f in ("index.html", "ev.html", "bets.html", "live.html", "theme.css",
                                            "report.js", "manifest.webmanifest", "icon-192.png", "icon-512.png",
                                            "apple-touch-icon.png", "favicon-32.png", "favicon.ico"))
    shell += _size(STATIC / "bets.html")  # cached again as /history
    feeds = len(json.dumps(tracker.summary())) + 6 * 1024  # bets, plus live/settings/arbs/ev (small)
    lines = {k: len(json.dumps(v)) for k, v in cache.items() if not k.startswith("props:")}
    props = {k: len(json.dumps(v)) for k, v in cache.items() if k.startswith("props:")}
    return {"app": shell, "feeds": feeds, "lines_all": sum(lines.values()), "props_all": sum(props.values()),
            "most": shell + feeds + sum(lines.values()) + sum(props.values())}


def freshness(cache: dict) -> list[dict]:
    out = []
    for key, v in sorted(cache.items()):
        out.append({"name": key.replace("props:", "") + (" props" if key.startswith("props:") else ""),
                    "scraped_at": v.get("scraped_at"), "seconds": v.get("seconds"),
                    "rows": len(v.get("lines", v.get("props", []))), "errors": len(v.get("errors") or {})})
    return out


def bets() -> dict:
    d = tracker.summary()
    t = d["total"]
    return {"bankroll": d["bankroll"], "starting": d["starting_bankroll"], "pnl": t["pnl"], "roi_pct": t["roi_pct"],
            "staked": t["staked"], "won": t["won"], "lost": t["lost"], "push": t["push"],
            "open": t["bets"] - t["settled"], "open_stake": t["open_stake"],
            "ev_expected": d["ev"]["expected"], "ev_pnl": d["ev"]["pnl"],
            "by_book": [{"book": b, "bets": v["bets"], "pnl": v["pnl"], "roi_pct": v["roi_pct"]}
                        for b, v in sorted(d["by_book"].items(), key=lambda kv: -kv[1]["pnl"])]}


def collect(cache: dict, started: float, schedule_state: dict) -> dict:
    return {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "disk": _cached("disk", 3600, disk),
        "server": server(started),
        "credits": {**oddsapi.quota},
        "traffic": _cached("traffic", 60, traffic),
        "health": (h := _cached("health", 60, health)),
        "phone": _cached("phone", 300, lambda: phone_storage(cache)),
        "freshness": freshness(cache),
        "schedule": {k: schedule_state.get(k) for k in ("times", "next_run", "last_run", "running")},
        "bets": bets(),
    } | ({"credits": {**oddsapi.quota, "remaining": h["credits_last"], "from_log": True}}
         if oddsapi.quota.get("remaining") is None else {})
