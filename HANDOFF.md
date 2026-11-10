# Sportsbook Lines: handoff

Written 2026-11-08 from the code and the running Mac, not from memory. Items I could not verify are marked **Unverified** and collected in [section 8](#8-what-i-could-not-verify).

## 1. Overview

Sportsbook Lines pulls moneylines, spreads, totals and basketball player props from eight US sportsbooks (Bally Bet added Nov 9, 2026), then finds +EV lines against Pinnacle and cross-book arbitrage. It tracks the bets you place, with profit and loss, a bankroll that drives stake sizing, and live scores with trend arrows. It runs entirely on a Mac mini, with a private full-featured app for the owner and a smaller login-protected website where each invited person tracks their own bets.

## 2. Local vs. online versions

There is no cloud copy of the app. Both versions run on the same Mac and share one database. Cloudflare only handles the login and carries traffic to the Mac.

| | Private ("local") | Public ("online") |
|---|---|---|
| **Program** | `python -m lines_agent.web` | `python -m lines_agent.public` |
| **Listens on** | `127.0.0.1:8765` | `127.0.0.1:8766` |
| **Reached through** | The Mac itself, the Mac app, Tailscale Serve at `mac-mini.tail839a81.ts.net` (tailnet only) | `https://bets.us.com` via Cloudflare Tunnel and Access |
| **Keeps running via** | launchd agent `com.sportsbook-lines-agent` (starts at user login) | launchd agent `com.sportsbook-lines-public` (starts at user login), plus system daemon `com.cloudflare.cloudflared` (starts at boot) |
| **Who can use it** | Anything that can reach it on the Mac or tailnet. There is no login, but other websites are refused ([section 3](#3-membership-and-login)) | The owner and invited members, each signed in through Cloudflare |
| **Whose bets** | The `owner` account only | The signed-in person's own account; the owner's email maps to `owner` |
| **Bets and members** | `data/app.db` (SQLite) | Same file |
| **Other data** | `data/web_cache.json`, `data/lines_history.csv`, `data/settings.json`, `data/push_subscriptions.json`, `data/vapid_private.pem` | None. Live scores come straight from ESPN |
| **Config** | `.env`: `ODDS_API_KEY`, optional `ODDS_API_PROPS`, `PUSH_CONTACT`, `PRIVATE_HOSTS`, and `CF_API_TOKEN`, `CF_ACCOUNT_ID`, `CF_ACCESS_GROUP_ID` | `data/public_site.json`: `team_domain`, `aud`, `owner_email`. Never loads `.env` |
| **Logs** (`~/Library/Logs/SportsbookLines/`) | `app.log`, `errors.log` | `public.log`, `public-errors.log` |
| **Runtime libraries** | `requests`, `playwright` plus Chromium, `pywebpush`, `py-vapid`, `cryptography` | `requests`, `PyJWT[crypto]` (same `.venv`) |
| **Spends Odds API credits** | Yes | No. A test confirms it loads no scraping or scheduling code |

| Feature | Private | Public |
|---|---|---|
| Lines grid, player props, arbitrage | Yes | No |
| +EV Finder, Track bet / Track arb | Yes | No |
| Scan controls, schedule editor, scheduled runs | Yes | No |
| Stats page | Yes (hidden on touch devices) | No |
| Push alerts, home-screen install, offline copies | Yes | No |
| Mac app and menu bar | Yes | No |
| People with access (invite / remove) | Yes | No |
| History and Live | Yes | Yes |
| Settle, cash out, edit, delete, set bankroll | Yes | Yes, own bets only |
| +EV and Arbitrage result tiles, bet-type filter | Yes | No |
| **Add a bet** by hand against ESPN's schedule | No | Yes |
| Sign out | No | Yes |

**How to run each** (first-time setup is in `README.md`):

```bash
# Private
.venv/bin/python -m lines_agent.web --port 8765
launchctl kickstart -k gui/$(id -u)/com.sportsbook-lines-agent   # restart the service

# Public
.venv/bin/python -m lines_agent.public --port 8766
launchctl kickstart -k gui/$(id -u)/com.sportsbook-lines-public  # restart the service

# Tests
.venv/bin/python -m unittest tests.test_public
```

## 3. Membership and login

- **Method:** Cloudflare Access in front of the site, using an emailed one-time PIN. The app has no passwords or login form of its own, and no password hashing, because it stores no passwords.
- **Session:** Access sets its own cookie and adds a signed RS256 token to each request in the `Cf-Access-Jwt-Assertion` header.
- **Token check** (`lines_agent/public.py`, `AccessVerifier`): on every request it checks the signature against the team's published keys, plus issuer, audience, expiry and the fixed RS256 algorithm. The header is used, not the cookie. Keys are cached for an hour and re-fetched when an unknown key appears.
- **Where users live:** the owner is `owner_email` in `data/public_site.json`. Members are in the `members` table of `data/app.db`. Each person's bets and bankroll are in `bets` and `users`, keyed by `owner` or by email.
- **Two lists must agree:** the Cloudflare group decides who can get a code, and `members` decides who the app accepts.

| Flow | How it works |
|---|---|
| **Signup** | None. Invite only: the owner enters an email in **History → People with access** on the private app. `invites.py` adds it to the Cloudflare group first, then to `members`. It sends no email, so the owner must share the address |
| **Login** | Visit the site, enter an email, enter the code Cloudflare sends |
| **Logout** | **Sign out** links to `/cdn-cgi/access/logout` |
| **Password reset** | Not applicable. Each login uses a new code |
| **Removal** | `members` is updated first, so the next request gets a 403 even with an unexpired login, then Cloudflare is updated. Bets are kept for a possible re-invite |
| **Expired login** | `public.js` shows "Your sign-in has expired" with a Sign in again button |

**Roles:** `owner` and `member`. They differ only in which account `owner` maps to. Administration exists only on the private app. There are no tiers.

**Route protection on the public program.** Every request needs a valid token and an owner or member email first; otherwise it gets 401, 403 or 503, before any routing.

| Route | Access |
|---|---|
| `/`, `/bets`, `/history`, `/live`, `theme.css`, `report.js`, `public.js`, favicon and icons | Signed in |
| `GET /api/bets`, `/api/live`, `/api/games`, `/api/whoami` | Signed in, own data |
| `POST /api/bets`, `/api/bets/update`, `/api/bets/delete`, `/api/bets/bankroll` | Signed in, own data, matching `Origin` required, JSON required |
| `POST /api/log` | Signed in, 30 reports a minute across the site |
| Everything else | 404 |

Responses also carry a Content-Security-Policy, HSTS, `nosniff`, frame denial and no-referrer. Failures return a generic message.

**The private program has no login.** It relies on binding to localhost and on Tailscale, plus `_refuse_foreign` in `web.py`: every request must be addressed to a known hostname (`127.0.0.1`, `localhost`, the Tailscale name, or `PRIVATE_HOSTS`), which stops DNS rebinding, and every POST or `/api/` request is refused if the browser marks it as coming from another site (`Origin` or `Sec-Fetch-Site`). The Mac app and command-line tools send neither header and are unaffected.

**Secrets (names only):**

| Name | Where | Used by |
|---|---|---|
| `ODDS_API_KEY` | `.env` | Private |
| `CF_API_TOKEN` (Access: Groups, Read and Edit) | `.env` | Private |
| VAPID private key | `data/vapid_private.pem` | Private (push alerts) |
| Cloudflare tunnel token | `/Library/Application Support/com.cloudflare.cloudflared/token` | Connector |
| GitHub deploy key | `~/.ssh/sportsbook_lines_deploy` | Git push |

`CF_ACCOUNT_ID`, `CF_ACCESS_GROUP_ID`, `team_domain` and `aud` are identifiers, not secrets. `.env`, `*.pem` and `data/` are ignored by Git.

## 4. Architecture

| Path | Purpose |
|---|---|
| `lines_agent/web.py` | Private server: all private routes, scan orchestration, line cache, nightly housekeeping |
| `lines_agent/public.py` | Public server: token check, allowlist, hand-entered bets |
| `lines_agent/httpd.py` | Shared HTTP handling: gzip, per-request logging, errors always answered |
| `lines_agent/tracker.py` | Bets, bankrolls and members in SQLite, one user at a time; daily backup; one-time import of `bets.json` |
| `lines_agent/invites.py` | Adds and removes people in the Cloudflare group |
| `lines_agent/scores.py` | ESPN scores, grading, trend and pace, possession, TV networks, upcoming games |
| `lines_agent/ev.py` | De-vig (multiplicative, Shin, power), +EV, Kelly |
| `lines_agent/arbitrage.py` | Cross-book arbitrage for lines and props |
| `lines_agent/agent.py` | Runs scrapers, saves history, trims history to 365 days |
| `lines_agent/props.py` | Basketball player props |
| `lines_agent/matching.py`, `models.py` | Matching games across books; the shared line record |
| `lines_agent/books/` | One adapter per source: FanDuel, BetRivers, theScore (via headless browser), The Odds API (DraftKings, BetMGM, Caesars, Fanatics, Bally Bet, plus Pinnacle for pricing) |
| `lines_agent/schedule.py` | Scheduled runs (Mountain time) and notifications |
| `lines_agent/settings.py` | Scan controls and run schedule, in `data/settings.json` |
| `lines_agent/push.py` | Web Push to phones |
| `lines_agent/logs.py` | Daily log files, API key redaction, phone error reports |
| `lines_agent/stats.py` | Numbers for the Stats page |
| `lines_agent/__main__.py` | Command-line scraper; `.env` loader |
| `lines_agent/static/index.html` | Lines, props, arbitrage, scan controls |
| `lines_agent/static/ev.html`, `bets.html`, `live.html`, `stats.html` | +EV, History, Live, Stats |
| `lines_agent/static/theme.css` | Shared colors; hides private links on the public site |
| `lines_agent/static/sw.js` | Service worker, private app only |
| `lines_agent/static/report.js`, `public.js` | Browser error reporting; public sign-in handling |
| `macapp/` | Swift wrapper and menu bar app; `build.sh` installs it |
| `launchd/` | Service definitions for both programs |
| `tests/test_public.py` | 37 tests of login checks, allowlist, account separation, storage, invites, and refusal of other websites on the private server |
| `data/` (ignored by Git) | Database, caches, history, settings, keys, `backups/` |

## 5. Deployment

**Where it's hosted:** on the Mac mini at home, an Apple Silicon machine. Cloudflare provides the domain, login and tunnel. Code is at `github.com/chrismfdaniels/Sportsbook-lines-agent` (private).

**There is no deploy pipeline.** Both programs run directly from this working folder, so to deploy a change:

1. Edit the code and run the tests.
2. Restart the affected program. HTML, CSS and JS are read per request and need no restart.
3. If a change must reach the phone's offline copy, bump `CACHE` in `sw.js`.
4. For Mac app changes, run `macapp/build.sh`.

**Required settings** are listed in sections 2 and 3. The Cloudflare side has a tunnel route from `bets.us.com` to `http://127.0.0.1:8766`, an Access application for that hostname, and a policy that includes the allowed-people group.

**Never connect the tunnel to port 8765.** The private app has no login.

**Off switch:**

```bash
launchctl bootout gui/$(id -u)/com.sportsbook-lines-public
```

**On again:**

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sportsbook-lines-public.plist
```

## 6. Decisions and why

| Decision | Why | Rejected |
|---|---|---|
| Cloudflare Tunnel and Access in front of the Mac | About $10–15 a year, no open home ports, login security handled by Cloudflare | Tailscale Funnel with own login (exposes a basic server to bots); copy pushed to the cloud (more build, read-only); everything in the cloud (roughly $36–39 a month for 4 GB per third-party listings, and sportsbooks may block data-center addresses) |
| Public site is a separate program | Never holds the API key, can't scan, can't slow scrapes, simple off switch | A second door inside the private server, which was the first plan |
| Private server refuses other websites by header, without adding a login | Blocks browser-based attacks while keeping the Mac app and Tailscale working unchanged | Requiring JSON on every POST (the Mac app's Scan Now sends none); adding a login to the private app |
| Default-deny allowlist | A new private route is safe by default | Blocking specific routes |
| Lines and +EV left off the public site | Those routes can start a scrape on a cache miss; The Odds API's terms bar redistributing data as a data product | Serving cached copies |
| Per-person accounts in SQLite | Two programs now write bets, and SQLite locks across processes | Per-user JSON files; one shared view; guest view with details hidden |
| Hand-entered bets against ESPN's schedule | Free, and enough detail for Live to grade | Sharing the +EV page with members |
| Invite box updating a Cloudflare group | Nobody edits Cloudflare, and strangers never get a code or use a seat | Open door with invite codes (Cloudflare flags open rules as a misconfiguration; strangers use up seats); own usernames and passwords |
| `Access: Groups` token | Narrowest permission that works; tested for read and write | The broader Organizations, Identity Providers, and Groups permission |
| No offline saving on the public site | Service workers can break Access re-login, and it would leave bets on the device | Sharing the private service worker |
| ESPN for scores | Free | The Odds API scores endpoint (costs credits) |
| One ESPN date per request | ESPN returned 400 for date ranges | Date ranges |
| Over/under arrows follow pace, and projected money follows the arrow | Owner's request: a line that's only "winning" for now shouldn't show as a win | Grading on the current score only |
| +EV alerts need all three de-vig methods at 5%+ | Owner's request | Alerting on any one method |
| Logs kept indefinitely, on the Mac | Owner chose this once phone errors could be stored on the Mac | 48-hour deletion, which was the first request |
| Line history trimmed to 365 days nightly | Owner's request; about 15 MB a day | Unlimited; storing daily files |
| Gzip on text responses | Lines page went from about 1 MB to about 58 KB | Uncompressed |
| Deploy key for GitHub | Limited to one repository, no account-wide token | GitHub CLI login |
| All times in Mountain; 1970s orange-and-royal theme; "My bets" renamed History with `/bets` kept | Owner's requests; the old address keeps existing links working | |

## 7. Known issues, TODOs and next steps

**Most important**

1. **The private server still has no login.** Attacks from other websites were closed on Nov 8 (section 3). Any device on the tailnet, or any program running on the Mac, can still use it fully. A very old browser that sends no `Sec-Fetch-Site` could still trigger a `GET` with `refresh=1`, which spends credits but can't read the result; iPhones on iOS 16.4 or later send the header.
2. **A power cut needs someone at the Mac.** FileVault is on and `autoLoginUser` isn't set, so after an unplanned restart the Mac waits for a password before anything starts. `autorestart` alone won't bring the site back, which differs from what the owner was told earlier. The 2 AM restart on Nov 8 recovered without intervention, which is consistent with a macOS update's authenticated restart, but that is an inference. Options: accept it, or turn off FileVault and enable auto-login, which weakens protection if the Mac is stolen.
3. **The live site runs uncommitted code.** Branch `public-site` has no commits of its own, so GitHub's `main` lacks the public site, database, invites and tests. Commit, push and merge.
4. **Bets have no off-site backup.** Daily copies in `data/backups/` are on the same drive. `data/` isn't in Git.

**Worth doing**

5. A real hand-entered bet hasn't gone through Cloudflare yet; the logs show only reads and error reports. The same path passed local tests, and `/api/log` passed the `Origin` check through Cloudflare. Try one with a real member.
6. No rate limiting on bet writes and no cap on bets per person. Acceptable with trusted members, not for open signup.
7. Signed-in members who are refused see raw JSON such as `{"error":"not allowed"}`, as does any unknown page.
8. Removing someone doesn't free their Cloudflare seat (50 free). Remove them from Cloudflare's user list if that ever matters.
9. The Stats page doesn't include the public program's logs, or per-member numbers.
10. Tests cover only the public site, accounts, storage and invites. Scrapers, +EV math and Live grading have no automated tests, and there is no CI.
11. Paths are hard-coded in the launchd files, the Mac app targets arm64 only, and `push.py` defaults its contact to the Tailscale address.
12. Dependencies are unpinned minimums. `cloudflared` has its own auto-update behavior.
13. ESPN's scoreboard and the sportsbook endpoints are undocumented and can change without notice.
14. Live's arrows and networks need ESPN data; player props get a score but no grade.
15. Leftovers that can be deleted once you're comfortable: `data/bets.pre-database.json`, `data/backups/bets-before-database.json`, `data/backups/bets-2026-11-08.json`, the old `data/lines_*.json` snapshots, and `~/Downloads/cloudflared.pkg`.

**Suggested next steps**

1. Commit, push and merge the `public-site` branch.
2. Set up off-site backups of `data/app.db`.
3. Decide on the FileVault trade-off.
4. Before charging anyone or showing Lines to members, check The Odds API's terms, the sportsbooks' terms and gambling rules with a lawyer.

## 8. What I could not verify

- **Cloudflare dashboard settings.** The API token can only see groups, so the session length, the Access application's login methods and the policy contents are as reported during setup. The behavior was tested: every page redirected to login, and a real sign-in worked.
- **Cloudflare pricing.** Access is free to 50 users per Cloudflare's page. I never confirmed Tunnel's price, and the domain's cost and registrar aren't recorded here.
- **The Odds API plan.** Plan size and reset date aren't in the code. About 17,972 credits remained on Nov 7, using roughly 150–330 a day in November.
- **`cloudflared` updates.** The service doesn't disable auto-update, but I didn't confirm that it updates when installed from the package.
- **iPhone storage and battery figures.** Calculated from what the app stores, not measured on the phone.
- **Hosting cost of the rejected all-cloud option.** From third-party comparisons, not Hetzner's own page.
- **Why the Mac restarted on Nov 5 and Nov 8.** Only the restart times are in the system log.
