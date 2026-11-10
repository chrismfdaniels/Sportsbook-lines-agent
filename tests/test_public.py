"""Security and account-separation tests for the public site. Run: .venv/bin/python -m unittest tests.test_public -v

A throwaway signing key plays Cloudflare's part, so these exercise the real
token checks and the real allowlist on a real local server."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from lines_agent import invites, public, tracker, web
from lines_agent.httpd import Server

ROOT = Path(__file__).resolve().parent.parent
TEAM, AUD, EMAIL = "test-team.cloudflareaccess.com", "a" * 64, "owner@example.com"
FRIEND = "friend@example.com"
GAME = {"league": "NFL", "away_team": "Denver Broncos", "home_team": "Las Vegas Raiders",
        "start_time": "2099-11-08T21:05Z", "market": "spread", "side": "away", "line": -3.5,
        "book": "FanDuel", "odds": -110, "stake": 20}
ISS = f"https://{TEAM}"


def new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY, ATTACKER = new_key(), new_key()


def jwks():
    k = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    return {"keys": [{**k, "kid": "good", "alg": "RS256"}]}


def token(key=KEY, kid="good", alg="RS256", **over):
    now = int(time.time())
    claims = {"iss": ISS, "aud": [AUD], "email": EMAIL, "iat": now, "exp": now + 600, **over}
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


class PublicSite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Never the real bets: every test here runs against a throwaway database
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = tracker.DB, tracker.FILE
        tracker.DB, tracker.FILE = Path(cls.tmp.name) / "app.db", Path(cls.tmp.name) / "bets.json"
        v = public.AccessVerifier(TEAM, AUD, fetch_keys=jwks)
        for m in (FRIEND, "a@example.com", "b@example.com"):
            tracker.add_member(m)
        cls.server = Server(("127.0.0.1", 0), public.make_handler(v, EMAIL))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        tracker.DB, tracker.FILE = cls.saved
        cls.tmp.cleanup()

    def req(self, path, tok=None, method="GET", body=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if tok:
            h["Cf-Access-Jwt-Assertion"] = tok
        r = urllib.request.Request(self.base + path, method=method, headers=h,
                                   data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def write(self, path, tok, body, **h):
        return self.req(path, tok, "POST", body, {"Origin": self.base, **h})

    def bets(self, tok):
        return json.loads(self.req("/api/bets", tok)[1])["bets"]

    # ---- who gets in
    def test_no_token_is_refused_everywhere(self):
        for p in ("/", "/bets", "/live", "/api/bets", "/api/live", "/theme.css", "/nothing"):
            self.assertEqual(self.req(p)[0], 401, p)

    def test_bad_tokens_are_refused(self):
        forged = jwt.encode({"iss": ISS, "aud": [AUD], "email": EMAIL}, "", algorithm="none", headers={"kid": "good"})
        pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        import base64, hashlib as hl, hmac
        def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=")
        now = int(time.time())
        head = b64(json.dumps({"alg": "HS256", "kid": "good"}).encode())
        pay = b64(json.dumps({"iss": ISS, "aud": [AUD], "email": EMAIL, "iat": now, "exp": now + 600}).encode())
        confused = b".".join([head, pay, b64(hmac.new(pem, head + b"." + pay, hl.sha256).digest())]).decode()
        cases = {
            "garbage": "not.a.token",
            "unsigned": forged,
            "public key used as a shared secret": confused,
            "attacker's key, right key id": token(key=ATTACKER),
            "unknown key id": token(key=ATTACKER, kid="other"),
            "expired": token(exp=int(time.time()) - 3600),
            "other application": token(aud=["b" * 64]),
            "other Cloudflare team": token(iss="https://evil.cloudflareaccess.com"),
            "no expiry": token(exp=None),
        }
        for name, t in cases.items():
            self.assertEqual(self.req("/api/bets", t)[0], 401, name)

    def test_other_account_is_refused(self):
        self.assertEqual(self.req("/api/bets", token(email="stranger@example.com"))[0], 403)

    def test_token_in_a_cookie_is_not_enough(self):
        self.assertEqual(self.req("/api/bets", headers={"Cookie": f"CF_Authorization={token()}"})[0], 401)

    def test_removal_takes_effect_at_once_and_keeps_their_bets(self):
        t = token(email="temp@example.com")
        self.assertEqual(self.req("/api/bets", t)[0], 403)  # not invited yet, even with a valid login
        tracker.add_member("temp@example.com")
        self.assertEqual(self.write("/api/bets", t, GAME)[0], 200)
        tracker.remove_member("temp@example.com")
        self.assertEqual(self.req("/api/bets", t)[0], 403)  # same token, still unexpired
        tracker.add_member("temp@example.com")
        self.assertEqual(len(self.bets(t)), 1)

    # ---- what's allowed
    def test_owner_sees_history_and_live(self):
        t = token()
        for p in ("/", "/bets", "/history", "/live", "/theme.css", "/report.js", "/public.js", "/favicon.ico"):
            self.assertEqual(self.req(p, t)[0], 200, p)
        code, body, _ = self.req("/api/bets", t)
        self.assertEqual(code, 200)
        self.assertIn("bets", json.loads(body))
        self.assertEqual(json.loads(self.req("/api/whoami", t)[1])["email"], EMAIL)

    def test_pages_are_marked_read_only(self):
        for p in ("/", "/live"):
            html = self.req(p, token())[1].decode()
            self.assertIn('data-public="1"', html)
            self.assertIn('/public.js', html)
            self.assertNotIn('rel="manifest"', html)

    def test_security_headers(self):
        h = self.req("/", token())[2]
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertIn("max-age", h["Strict-Transport-Security"])

    # ---- what's refused, even when signed in
    def test_private_paths_do_not_exist(self):
        t = token()
        for p in ("/ev", "/stats", "/index.html", "/bets.html", "/sw.js", "/manifest.webmanifest", "/api/stats",
                  "/api/lines?league=NFL", "/api/lines?league=NFL&refresh=1", "/api/ev?refresh=1", "/api/arbs?refresh=1",
                  "/api/props?league=NBA&refresh=1", "/api/settings", "/api/status", "/api/push/key", "/api/members",
                  "/.env", "/data/bets.json", "/../.env", "/%2e%2e/.env", "/static/../../.env", "//etc/passwd"):
            self.assertEqual(self.req(p, t)[0], 404, p)

    def test_private_actions_are_refused(self):
        t = token()
        before = self.bets(t)
        for p, b in [("/api/scan", {"quiet": True}), ("/api/settings", {"runs": []}), ("/api/estimate", {}),
                     ("/api/push/test", {}), ("/api/push/subscribe", {}), ("/api/push/unsubscribe", {}),
                     ("/api/members/invite", {"email": "x@example.com"}), ("/api/members/remove", {"email": FRIEND})]:
            self.assertEqual(self.write(p, t, b)[0], 404, p)
        self.assertEqual(self.bets(t), before)

    # ---- each person's bets are their own
    def test_accounts_are_separate(self):
        me, them = token(email="a@example.com"), token(email="b@example.com")
        mine = json.loads(self.write("/api/bets", me, {**GAME, "notes": "owner note"})[1])["added"][0]
        theirs = json.loads(self.write("/api/bets", them, {**GAME, "stake": 5})[1])["added"][0]
        self.assertEqual(json.loads(self.write("/api/bets/bankroll", them, {"starting": 50})[1])["starting_bankroll"], 50)
        self.assertNotIn("owner note", self.req("/api/bets", them)[1].decode())
        self.assertEqual({b["stake"] for b in self.bets(them)}, {5})
        # The same id exists in both accounts; settling mine must leave theirs open
        self.assertEqual(mine["id"], theirs["id"])
        self.assertEqual(self.write("/api/bets/update", me, {"id": mine["id"], "status": "won"})[0], 200)
        self.assertEqual([b["status"] for b in self.bets(them)], ["open"])
        self.write("/api/bets/delete", them, {"id": theirs["id"]})
        self.assertEqual(self.bets(them), [])
        self.assertTrue(any(b["id"] == mine["id"] for b in self.bets(me)))

    def test_a_request_cannot_name_another_user(self):
        t = token(email=FRIEND)
        self.write("/api/bets", t, {**GAME, "user": "owner", "email": EMAIL, "kind": "arb"})
        self.write("/api/bets/bankroll", t, {"starting": 999, "user": "owner"})
        self.assertNotEqual(json.loads(self.req("/api/bets", token())[1])["starting_bankroll"], 999)
        self.assertEqual({b["kind"] for b in self.bets(t)}, {"manual"})

    def test_changes_need_this_sites_origin(self):
        t = token()
        n = len(self.bets(t))
        self.assertEqual(self.req("/api/bets", t, "POST", GAME)[0], 403)  # no Origin
        self.assertEqual(self.req("/api/bets", t, "POST", GAME, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.write("/api/bets", t, GAME, **{"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.write("/api/bets", None, GAME)[0], 401)
        self.assertEqual(len(self.bets(t)), n)

    def test_bad_bets_are_rejected(self):
        t = token(email="friend@example.com")
        n = len(self.bets(t))
        for bad in ({"league": "XFL"}, {"market": "parlay"}, {"side": "over"}, {"line": None}, {"line": "abc"},
                    {"line": float("nan")}, {"away_team": "x" * 200}, {"book": ""}, {"odds": 50}, {"odds": None},
                    {"stake": 0}, {"stake": -5}, {"start_time": "soon"}):
            self.assertEqual(self.write("/api/bets", t, {**GAME, **bad})[0], 400, bad)
        self.assertEqual(self.write("/api/bets", t, {**GAME, "market": "total", "side": "over", "line": 47.5})[0], 200)
        self.assertEqual(len(self.bets(t)), n + 1)

    def test_game_list(self):
        real = public.scores.upcoming
        public.scores.upcoming = lambda lg: [{"away": "A", "home": "B", "start": "2099-01-01T00:00Z", "state": "pre"}]
        try:
            self.assertEqual(json.loads(self.req("/api/games?league=NFL", token())[1])["games"][0]["away"], "A")
            self.assertEqual(self.req("/api/games?league=XFL", token())[0], 400)
            self.assertEqual(self.req("/api/games?league=NFL")[0], 401)
        finally:
            public.scores.upcoming = real

    def test_error_reports_are_limited(self):
        t = token()
        self.assertEqual(self.req("/api/log", None, "POST", {"events": []})[0], 401)
        self.assertEqual(self.req("/api/log", t, "POST", [1])[0], 400)
        self.assertEqual(self.req("/api/log", t, "POST", {"events": []},
                                  {"Origin": "https://evil.example"})[0], 403)

    def test_errors_reveal_nothing(self):
        real = public.tracker.summary
        public.tracker.summary = lambda *a: (_ for _ in ()).throw(RuntimeError("secret path /Users/x"))
        try:
            code, body, _ = self.req("/api/bets", token())
        finally:
            public.tracker.summary = real
        self.assertEqual(code, 500)
        self.assertNotIn(b"secret", body)


class Config(unittest.TestCase):
    def test_bad_config_refuses_to_start(self):
        good = {"team_domain": TEAM, "aud": AUD, "owner_email": EMAIL}
        bad = [None, {}, {**good, "team_domain": "evil.com"}, {**good, "aud": ""}, {**good, "owner_email": "nope"}]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            for c in bad:
                if c is None:
                    p.write_text("{not json")
                else:
                    p.write_text(json.dumps(c))
                with self.assertRaises(public.ConfigError):
                    public.load_config(p)
            p.write_text(json.dumps(good))
            self.assertEqual(public.load_config(p)["owner_email"], EMAIL)
            with self.assertRaises(public.ConfigError):
                public.load_config(Path(d) / "missing.json")


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = tracker.DB, tracker.FILE
        d = Path(self.tmp.name)
        tracker.DB, tracker.FILE = d / "app.db", d / "bets.json"

    def tearDown(self):
        tracker.DB, tracker.FILE = self.saved
        self.tmp.cleanup()

    def old_file(self):
        bets = [{"id": f"B{i}", "group": None, "kind": "ev", "placed_at": f"2026-11-0{i}T00:00:00+00:00", "book": "FanDuel",
                 "pick": "X ML", "odds": -110, "stake": 10.0, "status": "won" if i % 2 else "open", "cashout": None,
                 "settled_at": "2026-11-09T00:00:00+00:00" if i % 2 else None, "notes": "", "ev_pct": 2.5} for i in range(1, 6)]
        tracker.FILE.write_text(json.dumps({"starting_bankroll": 935.0, "bets": bets}))
        return bets

    def test_existing_bets_become_the_owners_once(self):
        bets = self.old_file()
        s = tracker.summary()
        self.assertEqual(s["starting_bankroll"], 935.0)
        self.assertEqual(sorted(b["id"] for b in s["bets"]), sorted(b["id"] for b in bets))
        self.assertFalse(tracker.FILE.exists())
        self.assertTrue(tracker.FILE.with_name("bets.pre-database.json").exists())
        self.assertEqual(tracker.summary("someone")["bets"], [])

    def test_damaged_old_file_stops_instead_of_starting_empty(self):
        tracker.FILE.write_text("{broken")
        with self.assertRaises(ValueError):
            tracker.summary()
        self.assertTrue(tracker.FILE.exists())

    def test_a_failed_change_saves_nothing(self):
        self.old_file()
        before = tracker.summary()["bets"]
        with self.assertRaises(ValueError):
            tracker.update("B1", {"stake": 5, "odds": 1})  # the stake is fine, the odds aren't
        self.assertEqual(tracker.summary()["bets"], before)

    def test_backup_is_a_working_copy(self):
        self.old_file()
        tracker.summary()
        dest = tracker.backup()
        rows = sqlite3.connect(dest).execute("SELECT count(*) FROM bets").fetchone()[0]
        self.assertEqual(rows, 5)

    def test_two_programs_writing_at_once_lose_nothing(self):
        code = ("import sys, pathlib; from lines_agent import tracker; tracker.DB = pathlib.Path(sys.argv[1]);"
                "tracker.FILE = tracker.DB.with_name('none.json');"
                "[tracker.add('manual', [dict(book='A', odds=-110, stake=1, pick='x')], '', 'shared') for _ in range(40)]")
        tracker.summary("shared")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(tracker.DB)], cwd=ROOT) for _ in range(3)]
        self.assertEqual([p.wait(timeout=60) for p in procs], [0, 0, 0])
        ids = [b["id"] for b in tracker.summary("shared")["bets"]]
        self.assertEqual(len(ids), 120)
        self.assertEqual(len(set(ids)), 120)


class Invites(unittest.TestCase):
    """The Cloudflare side is simulated: a group held in memory."""
    SECRET = "super-secret-token"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.saved = tracker.DB, tracker.FILE, invites.CONFIG, invites.requests.request
        tracker.DB, tracker.FILE, invites.CONFIG = d / "app.db", d / "bets.json", d / "site.json"
        invites.CONFIG.write_text(json.dumps({"owner_email": EMAIL}))
        self.env = mock.patch.dict(os.environ, {"CF_API_TOKEN": self.SECRET, "CF_ACCOUNT_ID": "acct", "CF_ACCESS_GROUP_ID": "grp"})
        self.env.start()
        self.group = {"name": "Allowed", "include": [{"email": {"email": EMAIL}}, {"ip": {"ip": "1.2.3.4/32"}}],
                      "exclude": [], "require": [], "id": "grp"}
        self.fail = None
        self.calls = []
        invites.requests.request = self.fake

    def tearDown(self):
        self.env.stop()
        tracker.DB, tracker.FILE, invites.CONFIG, invites.requests.request = self.saved
        self.tmp.cleanup()

    def fake(self, method, url, json=None, timeout=None, headers=None):
        self.calls.append((method, url, headers))
        if self.fail == "network":
            raise invites.requests.ConnectionError("boom")
        r = mock.Mock(status_code=200)
        if self.fail == "refused" or (self.fail == "put" and method == "PUT"):
            r.status_code = 403
            r.json.return_value = {"success": False, "errors": [{"message": "Authentication error"}]}
            return r
        if method == "PUT":
            self.group = {**self.group, **json}
        r.json.return_value = {"success": True, "result": dict(self.group)}
        return r

    def emails(self):
        return [r["email"]["email"] for r in self.group["include"] if "email" in r]

    def test_invite_updates_cloudflare_and_the_site(self):
        self.assertEqual(invites.invite("  Pal@Example.com "), "pal@example.com")
        self.assertIn("pal@example.com", self.emails())
        self.assertTrue(tracker.is_member("pal@example.com"))
        self.assertEqual(self.calls[0][1], "https://api.cloudflare.com/client/v4/accounts/acct/access/groups/grp")
        self.assertEqual(self.calls[0][2]["Authorization"], f"Bearer {self.SECRET}")

    def test_other_rules_are_left_alone(self):
        invites.invite("pal@example.com")
        invites.remove("pal@example.com")
        self.assertEqual(self.group["include"], [{"email": {"email": EMAIL}}, {"ip": {"ip": "1.2.3.4/32"}}])

    def test_inviting_twice_adds_once(self):
        invites.invite("pal@example.com")
        invites.invite("pal@example.com")
        self.assertEqual(self.emails().count("pal@example.com"), 1)

    def test_failed_invite_leaves_nobody_half_invited(self):
        for mode in ("network", "refused", "put"):
            self.fail = mode
            with self.assertRaises(invites.InviteError) as e:
                invites.invite("pal@example.com")
            self.assertFalse(tracker.is_member("pal@example.com"), mode)
            self.assertNotIn(self.SECRET, str(e.exception))

    def test_removal_still_blocks_if_cloudflare_is_down(self):
        invites.invite("pal@example.com")
        self.fail = "network"
        with self.assertRaises(invites.InviteError) as e:
            invites.remove("pal@example.com")
        self.assertFalse(tracker.is_member("pal@example.com"))
        self.assertIn("wasn't updated", str(e.exception))

    def test_bad_requests(self):
        for bad in ("", "nope", "a@b", "a b@c.com", None, "x" * 300 + "@a.com"):
            with self.assertRaises(invites.InviteError):
                invites.invite(bad)
        with self.assertRaises(invites.InviteError):
            invites.invite(EMAIL)
        with self.assertRaises(invites.InviteError):
            invites.remove(EMAIL)
        self.assertEqual(tracker.members(), [])
        self.assertEqual(self.calls, [])

    def test_not_connected(self):
        self.env.stop()
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                for k in invites.ENV:
                    os.environ.pop(k, None)
                self.assertIn("Not connected", invites.check())
                with self.assertRaises(invites.InviteError):
                    invites.invite("pal@example.com")
                self.assertFalse(tracker.is_member("pal@example.com"))
        finally:
            self.env.start()

    def test_member_list_counts_bets(self):
        invites.invite("pal@example.com")
        tracker.add("manual", [dict(book="A", odds=-110, stake=1, pick="x")], "", "pal@example.com")
        self.assertEqual([(m["email"], m["bets"]) for m in tracker.members()], [("pal@example.com", 1)])


class PrivateServer(unittest.TestCase):
    """The private app has no login, so other websites in a browser must be refused."""
    EVIL = "https://evil.example"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = tracker.DB, tracker.FILE
        d = Path(cls.tmp.name)
        tracker.DB, tracker.FILE = d / "app.db", d / "bets.json"
        tracker.add("manual", [dict(book="A", odds=-110, stake=1, pick="x")])
        cls.server = Server(("127.0.0.1", 0), web.Handler)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        tracker.DB, tracker.FILE = cls.saved
        cls.tmp.cleanup()

    def req(self, path, method="GET", body=None, **headers):
        h = {k.replace("_", "-"): v for k, v in headers.items()}
        r = urllib.request.Request(self.base + path, method=method, headers=h,
                                   data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def status(self):
        return tracker.summary()["bets"][0]["status"]

    SETTLE = {"id": "B1", "status": "won"}

    def test_other_sites_cannot_change_anything(self):
        for headers in ({"Origin": self.EVIL, "Content_Type": "application/json"},
                        {"Origin": self.EVIL, "Content_Type": "text/plain"},
                        {"Origin": "null", "Content_Type": "text/plain"},
                        {"Sec_Fetch_Site": "cross-site", "Content_Type": "text/plain"},
                        {"Origin": f"http://127.0.0.1:{self.port + 1}"}):
            self.assertEqual(self.req("/api/bets/update", "POST", self.SETTLE, **headers)[0], 403, headers)
        self.assertEqual(self.status(), "open")

    def test_other_sites_cannot_invite(self):
        code, _ = self.req("/api/members/invite", "POST", {"email": "evil@example.com"}, Origin=self.EVIL)
        self.assertEqual(code, 403)
        self.assertEqual(tracker.members(), [])

    def test_rebound_domain_is_refused(self):
        evil = {"Host": f"evil.example:{self.port}"}
        self.assertEqual(self.req("/api/bets", **evil)[0], 403)
        self.assertEqual(self.req("/", **evil)[0], 403)
        same = {**evil, "Origin": f"http://evil.example:{self.port}"}
        self.assertEqual(self.req("/api/bets/update", "POST", self.SETTLE, **same)[0], 403)
        self.assertEqual(self.status(), "open")

    def test_cross_site_refresh_spends_nothing(self):
        calls = []
        with mock.patch.object(web, "get_league", lambda *a, **k: calls.append(a) or {"lines": []}):
            self.assertEqual(self.req("/api/lines?league=NFL&refresh=1", Sec_Fetch_Site="cross-site")[0], 403)
            self.assertEqual(calls, [])
            self.assertEqual(self.req("/api/lines?league=NFL&refresh=1", Sec_Fetch_Site="same-origin")[0], 200)
            self.assertEqual(len(calls), 1)

    def test_legitimate_callers_still_work(self):
        same = {"Origin": self.base, "Content_Type": "application/json", "Sec_Fetch_Site": "same-origin"}
        self.assertEqual(self.req("/api/bets/update", "POST", {"id": "NOPE"}, **same)[0], 400)      # reached the app
        self.assertEqual(self.req("/api/bets/update", "POST", {"id": "NOPE"})[0], 400)              # Mac app: no browser headers
        tail = "mac-mini.tail839a81.ts.net"
        self.assertEqual(self.req("/api/bets", Host=tail, Sec_Fetch_Site="same-origin")[0], 200)
        self.assertEqual(self.req("/api/bets/update", "POST", {"id": "NOPE"}, Host=tail, Origin=f"https://{tail}")[0], 400)
        self.assertEqual(self.req("/bets", Sec_Fetch_Site="cross-site")[0], 200)                    # following a link to a page
        self.assertEqual(self.req("/api/status", Host=f"localhost:{self.port}")[0], 200)


class Isolation(unittest.TestCase):
    def test_public_program_loads_no_scraping_or_key(self):
        code = ("import os, sys; os.environ.pop('ODDS_API_KEY', None); import lines_agent.public as p;"
                "bad=[m for m in ('web','agent','schedule','settings','push','props','ev','stats','books','invites') if 'lines_agent.'+m in sys.modules];"
                "print(bad, 'ODDS_API_KEY' in os.environ)")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "[] False", out.stderr)


if __name__ == "__main__":
    unittest.main()
