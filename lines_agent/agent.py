"""Runs every adapter for every league, then saves and summarizes the lines."""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import arbitrage, books
from .books import oddsapi
from .logs import redact
from .matching import unify_games
from .models import LEAGUES, Line

log = logging.getLogger("lines_agent")

FIELDS = list(Line.__dataclass_fields__)
_save_lock = threading.Lock()  # the web server scrapes leagues in parallel
HISTORY_DAYS = 365  # line history older than this is deleted (prune_history)


def _run(label: str, fn) -> tuple[str, list[Line], str | None]:
    try:
        return label, fn(), None
    except Exception as e:  # one broken book must not sink the whole run
        return label, [], redact(f"{type(e).__name__}: {e}")


def scrape(leagues=LEAGUES, book_names=None, state="nj", include_live=False,
           api_key=None, use_odds_api=True, markets=None) -> tuple[list[Line], dict[str, str]]:
    """markets: moneyline/spread/total to keep (default all). Fewer markets
    means fewer Odds API credits; the direct books return all three anyway."""
    book_names = book_names or books.ALL_BOOKS
    jobs = []
    for league in leagues:
        for name, mod in books.DIRECT.items():
            if name in book_names and league in mod.SUPPORTED:
                jobs.append((f"{name} {league}", lambda m=mod, lg=league: m.fetch(lg, state=state)))
        via_api = [b for b in books.VIA_ODDS_API + books.SHARP if b in book_names] if use_odds_api else []
        if via_api and league in oddsapi.SUPPORTED:
            jobs.append((f"OddsAPI[{','.join(via_api)}] {league}",
                         lambda lg=league, bs=via_api: oddsapi.fetch(lg, bs, api_key=api_key, markets=markets)))

    lines: list[Line] = []
    errors: dict[str, str] = {}
    # Browser-backed adapters are heavy, so keep the pool small.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for label, got, err in pool.map(lambda j: _run(*j), jobs):
            if err:
                errors[label] = err
                log.warning("%s failed: %s", label, err)
            else:
                if markets:
                    got = [l for l in got if l.market in markets]
                log.info("%s: %d lines", label, len(got))
                lines.extend(got)

    lines = unify_games(lines)
    if not include_live:
        lines = [l for l in lines if not l.live]

    # A book that answered but had nothing is easy to miss in the totals.
    found = {(l.league, l.book) for l in lines}
    for league in leagues:
        if not any(lg == league for lg, _ in found):
            if not any(label.endswith(" " + league) for label in errors):
                errors[league] = "no games listed right now (likely out of season)"
            continue
        for b in book_names:
            mod = books.DIRECT.get(b)
            supported = league in (mod.SUPPORTED if mod else oddsapi.SUPPORTED)
            if not supported or (not mod and not use_odds_api) or (league, b) in found:
                continue
            if not any(b in label and label.endswith(" " + league) for label in errors):
                errors[f"{b} {league}"] = "returned no lines"
    return lines, errors


def save(lines: list[Line], out_dir: Path, snapshot: bool = True) -> tuple[Path | None, Path]:
    """Append to lines_history.csv, and with snapshot also write lines_<time>.json.
    The web server skips the snapshot: every row is already in the CSV."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = None
    with _save_lock:
        if snapshot:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            json_path = out_dir / f"lines_{stamp}.json"
            json_path.write_text(json.dumps([l.to_dict() for l in lines], indent=1))

        # One CSV that grows every run, handy for tracking line movement.
        csv_path = out_dir / "lines_history.csv"
        new = not csv_path.exists()
        with csv_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerows(l.to_dict() for l in lines)
    return json_path, csv_path


def prune_history(out_dir: Path, days: int = HISTORY_DAYS) -> dict:
    """Delete line history older than `days`: rows of lines_history.csv (by their
    scraped_at) and lines_<time>.json snapshots. The web server runs this daily.

    Rows are appended in time order (within seconds), so instead of reading the
    whole file this binary-searches for where the kept rows begin, checks the rows
    around that point one by one, and copies the rest as is. When nothing is old
    enough it only reads the first row."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = {"rows_removed": 0, "bytes_before": 0, "bytes_after": 0, "snapshots_removed": 0}

    for snap in out_dir.glob("lines_*.json"):
        m = re.fullmatch(r"lines_(\d{8}T\d{6}Z)\.json", snap.name)
        if m and datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) < cutoff:
            snap.unlink()
            out["snapshots_removed"] += 1

    path = out_dir / "lines_history.csv"
    if not path.exists():
        return out
    with _save_lock:  # no appends while the file is rewritten
        size = out["bytes_before"] = out["bytes_after"] = path.stat().st_size
        with path.open("rb") as f:
            header = f.readline()
            col = header.decode().strip().split(",").index("scraped_at")
            body = f.tell()

            def stamp(raw: bytes) -> datetime | None:
                try:
                    return datetime.fromisoformat(next(csv.reader([raw.decode()]))[col])
                except (ValueError, IndexError, StopIteration, UnicodeDecodeError):
                    return None  # unreadable: keep it

            def row_at(pos: int) -> tuple[int, datetime | None, bytes]:
                f.seek(pos)
                if pos > body:
                    f.readline()  # finish the row pos landed in
                start = f.tell()
                raw = f.readline()
                return start, stamp(raw) if raw else None, raw

            first = stamp(f.readline())
            if first is None or first >= cutoff:
                return out  # nothing old enough
            # Find a point safely before the cutoff, then check rows one by one
            # until well past it (covers the few seconds rows can be out of order).
            margin = timedelta(hours=1)
            lo, hi = body, size
            while hi - lo > 1 << 16:
                mid = (lo + hi) // 2
                _, t, raw = row_at(mid)
                if not raw or t is None or t >= cutoff - margin:  # unreadable: lean toward keeping
                    hi = mid
                else:
                    lo = mid
            start, _, _ = row_at(lo)
            f.seek(start)
            kept = []
            while raw := f.readline():
                t = stamp(raw)
                if t is not None and t < cutoff:
                    out["rows_removed"] += 1
                    continue
                kept.append(raw)
                if t is not None and t >= cutoff + margin:
                    break
            out["rows_removed"] += _count_rows(path, body, start)
            if not out["rows_removed"]:
                return out
            if shutil.disk_usage(out_dir).free < size + (1 << 30):
                log.warning("Not trimming line history: not enough free disk to rewrite %s", path.name)
                return out
            tmp = path.with_suffix(".tmp")
            with tmp.open("wb") as w:
                w.write(header)
                w.writelines(kept)
                shutil.copyfileobj(f, w, 1 << 24)
            os.replace(tmp, path)
        out["bytes_after"] = path.stat().st_size
    return out


def _count_rows(path: Path, start: int, end: int) -> int:
    """Rows between two byte offsets (the ones skipped without being read row by row)."""
    n = 0
    with path.open("rb") as f:
        f.seek(start)
        left = end - start
        while left > 0:
            chunk = f.read(min(left, 1 << 24))
            if not chunk:
                break
            n += chunk.count(b"\n")
            left -= len(chunk)
    return n


def _game_key(l: Line) -> tuple:
    # Match the same game across books by league, teams and start date.
    return (l.league, l.away_team.lower(), l.home_team.lower(), l.start_time[:10])


def best_lines(lines: list[Line]) -> list[dict]:
    """Best available price per game/market/side across all books."""
    best: dict[tuple, Line] = {}
    for l in lines:
        k = _game_key(l) + (l.market, l.side)
        cur = best.get(k)
        # Prefer the better number first (more points for the bettor), then the better price.
        if cur is None or _better(l, cur):
            best[k] = l
    return [
        {"league": l.league, "game": f"{l.away_team} @ {l.home_team}", "start": l.start_time,
         "market": l.market, "side": l.side, "line": l.line, "odds": l.odds, "book": l.book}
        for l in sorted(best.values(), key=lambda x: (x.league, x.start_time, x.away_team, x.market, x.side))
    ]


def _better(a: Line, b: Line) -> bool:
    if a.market != "moneyline" and a.line is not None and b.line is not None and a.line != b.line:
        if a.market == "spread" or a.side == "under":
            return a.line > b.line
        return a.line < b.line  # over: lower total is better
    return a.odds > b.odds


def print_summary(lines: list[Line], errors: dict[str, str]):
    by_book: dict[str, int] = {}
    for l in lines:
        by_book[l.book] = by_book.get(l.book, 0) + 1
    print(f"\n{len(lines)} lines scraped")
    for b, n in sorted(by_book.items()):
        print(f"  {b:<14} {n}")
    for label, err in errors.items():
        print(f"  ! {label}: {err}")

    rows = best_lines(lines)
    if rows:
        print("\nBest available prices:")
        print(f"  {'LG':<4}{'GAME':<46}{'MARKET':<10}{'SIDE':<6}{'LINE':>7}{'ODDS':>7}  BOOK")
        for r in rows:
            line = "" if r["line"] is None else f"{r['line']:+g}" if r["market"] == "spread" else f"{r['line']:g}"
            print(f"  {r['league']:<4}{r['game'][:45]:<46}{r['market']:<10}{r['side']:<6}"
                  f"{line:>7}{r['odds']:>+7}  {r['book']}")


def watch(interval_s: int, out_dir: Path, odds_api_interval_s: int = 3600, min_arb: float = 0.01, **kw):
    """Direct books refresh every interval_s; The Odds API books only every
    odds_api_interval_s, since each pull spends ~3 credits per league."""
    last_api = 0.0
    while True:
        use_api = time.monotonic() - last_api >= odds_api_interval_s
        lines, errors = scrape(use_odds_api=use_api, **kw)
        if use_api:
            last_api = time.monotonic()
        json_path, _ = save(lines, out_dir)
        print_summary(lines, errors)
        arbitrage.print_arbs(arbitrage.find_arbs(lines, min_arb, include_live=kw.get("include_live", False)), min_arb)
        print(f"\nSaved {json_path}. Next run in {interval_s}s (Ctrl+C to stop).")
        time.sleep(interval_s)
