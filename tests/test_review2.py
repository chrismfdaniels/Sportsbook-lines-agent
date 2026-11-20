"""Tests for the Nov 19 code review: team matching, bet storage, cache saving.
Run: .venv/bin/python -m unittest tests.test_review2 -v. Nothing here touches real data."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lines_agent import matching, scores, tracker, web
from lines_agent.models import Line


def line(book, away, home, start="2099-11-20T19:00:00Z", side="away", odds=110):
    return Line(book=book, league="NCAAB", event_id=book + away, start_time=start, away_team=away,
                home_team=home, market="moneyline", side=side, line=None, odds=odds)


def games(lines):
    return {(l.away_team, l.home_team) for l in matching.unify_games(lines)}


class TeamMatching(unittest.TestCase):
    """Books spell some college teams differently; each split game dropped those books
    from +EV and arbitrage (8 men's and 2 women's basketball games on Nov 19)."""

    SAME = [("Murray State Racers", "N.C. State Wolfpack", "Murray State Racers", "North Carolina State Wolfpack"),
            ("SE Missouri St Redhawks", "St. Thomas (MN) Tommies", "Southeast Missouri State Redhawks", "St. Thomas (MN) Tommies"),
            ("Western Michigan Broncos", "Mt. St. Mary's Mountaineers", "Western Michigan Broncos", "Mount St. Mary's Mountaineers"),
            ("UT Martin Skyhawks", "Southern Illinois Salukis", "Tenn-Martin Skyhawks", "Southern Illinois Salukis"),
            ("Delaware Blue Hens", "CSU Northridge Matadors", "Delaware Blue Hens", "Cal State Northridge Matadors"),
            ("Charleston Cougars", "Drake Bulldogs", "College of Charleston Cougars", "Drake Bulldogs"),
            ("Little Rock Trojans", "Jacksonville Dolphins", "Arkansas Little Rock Trojans", "Jacksonville Dolphins"),
            ("Radford Highlanders", "Tennessee Volunteers", "Radford Highlanders (W)", "Tennessee Lady Volunteers (W)")]

    def test_spellings_of_one_game_join(self):
        for a1, h1, a2, h2 in self.SAME:
            with self.subTest(game=a1 + " @ " + h1):
                self.assertEqual(len(games([line("Pinnacle", a1, h1), line("BetRivers", a2, h2)])), 1)

    def test_nickname_with_home_and_away_swapped(self):
        got = games([line("Pinnacle", "UAB Blazers", "GW Revolutionaries"),
                     line("BetRivers", "George Washington Revolutionaries", "UAB Blazers")])
        self.assertEqual(len(got), 1)

    def test_different_games_stay_apart(self):
        cases = [
            # same nickname, different opponents
            [line("A", "Western Michigan Broncos", "Toledo Rockets"), line("B", "Boise State Broncos", "Nevada Wolf Pack")],
            # same opponent and nickname, but a day apart
            [line("A", "Kent State Golden Flashes", "Ohio Bobcats", "2099-11-20T00:00:00Z"),
             line("B", "Georgia Southern Golden Flashes", "Ohio Bobcats", "2099-11-22T00:00:00Z")],
            # "State" isn't a nickname
            [line("A", "Jackson State", "Alcorn State"), line("B", "Jackson State", "Alabama State")],
        ]
        for c in cases:
            with self.subTest(c=[(l.away_team, l.home_team) for l in c]):
                self.assertEqual(len(games(c)), 2)

    def test_live_finds_the_espn_game_by_nickname(self):
        team = lambda name, short, loc: {"name": name, "names": [name, short, loc]}
        g = {"start": "2099-11-20T19:00Z", "away": team("Little Rock Trojans", "Little Rock", "Little Rock"),
             "home": team("Jacksonville Dolphins", "Jacksonville", "Jacksonville")}
        bet = {"away_team": "Arkansas-Little Rock Trojans", "home_team": "Jacksonville Dolphins", "start_time": "2099-11-20T19:00Z"}
        self.assertEqual(scores._find(bet, [g]), (g, False))


class BetStorage(unittest.TestCase):
    """Saving used to delete and rewrite every bet; now only changed rows are written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = tracker.DB, tracker.FILE
        tracker.DB, tracker.FILE = Path(self.tmp.name) / "app.db", Path(self.tmp.name) / "bets.json"
        for i in range(3):
            tracker.add("manual", [{"book": "A", "odds": -110, "stake": 10 + i, "pick": f"p{i}"}])

    def tearDown(self):
        tracker.DB, tracker.FILE = self.saved
        self.tmp.cleanup()

    def rows(self):
        with sqlite3.connect(tracker.DB) as c:
            return c.execute("SELECT rowid, id, data FROM bets ORDER BY rowid").fetchall()

    def test_settling_one_bet_rewrites_one_row(self):
        before = self.rows()
        tracker.update("B2", {"status": "won"})
        after = self.rows()
        self.assertEqual([r[:2] for r in before], [r[:2] for r in after])  # same rows, same order
        changed = [b[1] for b, a in zip(before, after) if b[2] != a[2]]
        self.assertEqual(changed, ["B2"])
        self.assertEqual(json.loads(after[1][2])["status"], "won")

    def test_add_and_delete(self):
        tracker.delete("B1")
        tracker.add("manual", [{"book": "A", "odds": 150, "stake": 5, "pick": "new"}])
        self.assertEqual([r[1] for r in self.rows()], ["B2", "B3", "B4"])
        self.assertEqual({b["id"] for b in tracker.summary()["bets"]}, {"B2", "B3", "B4"})
        tracker.update("B3", {"status": "lost"})
        self.assertEqual({b["id"] for b in tracker.open_bets()}, {"B2", "B4"})

    def test_a_failed_change_writes_nothing(self):
        before = self.rows()
        with self.assertRaises(ValueError):
            tracker.update("B1", {"stake": -5})
        self.assertEqual(before, self.rows())


class CacheSaving(unittest.TestCase):
    """A scheduled run finishes six leagues in a row; the cache file is written once."""

    def test_saves_close_together_write_once(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(web, "CACHE_FILE", Path(d) / "c.json"), \
                mock.patch.object(web, "SAVE_DELAY", 0.2), mock.patch.object(web, "_cache", {"NBA": {"x": 1}}), \
                mock.patch.object(web, "_write_cache", wraps=web._write_cache) as write:
            for _ in range(6):
                web._save_cache()
            time.sleep(0.5)
            self.assertEqual(write.call_count, 1)
            self.assertEqual(json.loads((Path(d) / "c.json").read_text()), {"NBA": {"x": 1}})
            web._save_cache()  # a later change is saved too
            time.sleep(0.5)
            self.assertEqual(write.call_count, 2)


class UnreadableSettings(unittest.TestCase):
    """A damaged settings file used to be replaced by defaults on the next save, losing the schedule."""

    def test_a_copy_is_kept(self):
        from lines_agent import settings
        with tempfile.TemporaryDirectory() as d, mock.patch.object(settings, "FILE", Path(d) / "settings.json"):
            settings.FILE.write_text('{"manual": {}, "runs": [{"time": "25:99"}]}')
            self.assertEqual(settings.load(("07:00",))["runs"][0]["time"], "07:00")
            self.assertIn("25:99", (Path(d) / "settings.unreadable.json").read_text())
            settings.FILE.write_text("[]")  # not an object at all: still defaults, no crash
            self.assertEqual(settings.load()["runs"], [])


if __name__ == "__main__":
    unittest.main()
