"""Tests for bugs found in the Nov 2026 code review. Run: .venv/bin/python -m unittest tests.test_review -v

Nothing here touches the real bets, settings, caches or The Odds API: storage
goes to a throwaway folder and every scrape is replaced by a fake."""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from lines_agent import schedule, settings, tracker, web
from lines_agent.httpd import Server
from lines_agent.models import Line


def fake_lines(league):
    return [Line(book="FanDuel", league=league, event_id="1", start_time="2099-01-01T00:00:00Z",
                 away_team="A", home_team="B", market="moneyline", side="away", line=None, odds=120)]


class BadNumbers(unittest.TestCase):
    """Odds or a stake of Infinity used to crash the request (a 500) instead of saying what's wrong."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = tracker.DB, tracker.FILE
        tracker.DB, tracker.FILE = Path(self.tmp.name) / "app.db", Path(self.tmp.name) / "bets.json"

    def tearDown(self):
        tracker.DB, tracker.FILE = self.saved
        self.tmp.cleanup()

    def test_refused_with_a_message(self):
        for odds, stake in ((float("inf"), 10), (-110, float("inf")), (None, 10), (-110, True), ("abc", 10), (-110, [5])):
            with self.subTest(odds=odds, stake=stake), self.assertRaises(ValueError):
                tracker.add("manual", [{"book": "A", "odds": odds, "stake": stake, "pick": "x"}])
        self.assertEqual(tracker.summary()["bets"], [])

    def test_update_refuses_bad_numbers(self):
        tracker.add("manual", [{"book": "A", "odds": -110, "stake": 10, "pick": "x"}])
        for change in ({"odds": float("inf")}, {"stake": False}, {"status": "cashout", "cashout": float("nan")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                tracker.update("B1", change)
        b = tracker.summary()["bets"][0]
        self.assertEqual((b["odds"], b["stake"], b["status"]), (-110, 10, "open"))

    def test_good_numbers_still_work(self):
        tracker.add("manual", [{"book": "A", "odds": "+150", "stake": "12.50", "pick": "x"}])
        b = tracker.summary()["bets"][0]
        self.assertEqual((b["odds"], b["stake"]), (150, 12.5))


class SettingsShape(unittest.TestCase):
    """A settings request in the wrong shape used to crash with a 500."""

    def test_wrong_shapes_are_refused(self):
        for bad in ({"leagues": "NBA"}, 5, ["NBA"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                settings.clean_profile(bad)
        with self.assertRaises(ValueError):
            settings.clean_run(5)

    def test_save_refuses_without_writing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(settings, "FILE", Path(d) / "settings.json"):
            for bad in ({"runs": [5]}, {"runs": "07:00"}, {"manual": 5}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    settings.save(bad)
            self.assertFalse(settings.FILE.exists())


class SharedRefresh(unittest.TestCase):
    """Two refreshes of one league at once used to scrape twice, spending Odds API credits twice."""

    def setUp(self):
        self.calls = 0
        self.lock = threading.Lock()
        patches = [mock.patch.object(web, "_cache", {}), mock.patch.object(web, "_scraped", {}),
                   mock.patch.object(web, "_save_cache"), mock.patch.object(web.agent, "save"),
                   mock.patch.object(web.agent, "scrape", side_effect=self.scrape)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def scrape(self, leagues, **_):
        with self.lock:
            self.calls += 1
        time.sleep(0.5)
        return fake_lines(leagues[0]), {}

    def test_waiting_refresh_uses_the_one_that_just_finished(self):
        threads = [threading.Thread(target=web.get_league, args=("NBA", True)) for _ in range(2)]
        for t in threads:
            t.start()
            time.sleep(0.1)  # the second asks while the first is scraping
        for t in threads:
            t.join()
        self.assertEqual(self.calls, 1)

    def test_a_later_refresh_still_scrapes(self):
        web.get_league("NBA", True)
        web.get_league("NBA", True)
        self.assertEqual(self.calls, 2)

    def test_a_wider_refresh_is_not_skipped(self):
        # The first pulls one book; the second, asked meanwhile, wants every book
        t = threading.Thread(target=web.get_league, args=("NBA", True, ["FanDuel"]))
        t.start()
        time.sleep(0.1)
        web.get_league("NBA", True)
        t.join()
        self.assertEqual(self.calls, 2)

    def test_scan_with_no_books_pulls_nothing(self):
        web._cache["NBA"] = {"league": "NBA", "scraped_at": "2099-01-01T00:00:00Z", "lines": [], "errors": {}}
        web.scan_arbs(0.01, False, True, 100, profile={"leagues": ["NBA"], "books": [], "markets": ["moneyline"], "stats": []})
        self.assertEqual(self.calls, 0)


class EmptyRefresh(unittest.TestCase):
    """Refresh with every book (or market) switched off used to pull all of them, and
    arbitrage paired props with every stat switched off ("stats=" was read as all)."""

    @classmethod
    def setUpClass(cls):
        cls.server = Server(("127.0.0.1", 0), web.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_nothing_switched_on_pulls_nothing(self):
        with mock.patch.object(web.agent, "scrape", side_effect=AssertionError("scraped")) as scrape:
            for q in ("books=&markets=moneyline", "books=FanDuel&markets="):
                with self.subTest(q=q):
                    code, body = self.get(f"/api/lines?league=NBA&refresh=1&{q}")
                    self.assertEqual(code, 400)
                    self.assertIn("Scan controls", body["error"])
            scrape.assert_not_called()

    def test_switched_off_stats_are_not_paired(self):
        with mock.patch.object(web, "get_props", return_value=None) as props, \
                mock.patch.object(web, "get_league", return_value={"scraped_at": "x", "lines": []}):
            code, body = self.get("/api/arbs?leagues=NBA&books=FanDuel,BetMGM&markets=moneyline&stats=")
        self.assertEqual(code, 200)
        self.assertEqual(body["profile"]["stats"], [])
        props.assert_not_called()


class OneRunAtATime(unittest.TestCase):
    """A scheduled run that came due during Scan Now used to run alongside it."""

    def test_runs_do_not_overlap(self):
        busy, most = [0], [0]
        guard = threading.Lock()

        def scan(*_, **__):
            with guard:
                busy[0] += 1
                most[0] = max(most[0], busy[0])
            time.sleep(0.3)
            with guard:
                busy[0] -= 1
            return {"arbs": [], "errors": {}}

        with mock.patch.object(schedule, "ev_check", None), mock.patch.object(schedule.tracker, "bankroll", return_value=100):
            threads = [threading.Thread(target=schedule.run_once, args=(scan, 0.01), kwargs={"notify": False}) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(most[0], 1)
        self.assertFalse(schedule.state["running"])


if __name__ == "__main__":
    unittest.main()
