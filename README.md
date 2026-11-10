# Sportsbook Lines Agent

Pulls pregame **moneyline, spread and total** lines for **NBA, NFL, NHL, MLB, college football, men's and women's college basketball, and college baseball** games from 7 US sportsbooks. It normalizes them into one format, saves each run, and prints the best available price for every side.

## Where each book's lines come from

| Book | Source | Needs |
|---|---|---|
| FanDuel | FanDuel's public sportsbook API | nothing |
| BetRivers | Kambi public offering API (powers BetRivers) | nothing |
| theScore Bet | Public competition page in headless Chromium, reading the GraphQL the page loads | Playwright |
| DraftKings | The Odds API | `ODDS_API_KEY` (free plan OK) |
| BetMGM | The Odds API | `ODDS_API_KEY` (free plan OK) |
| Caesars | The Odds API (`williamhill_us`) | `ODDS_API_KEY` on a paid plan |
| Fanatics | The Odds API | `ODDS_API_KEY` on a paid plan |
| Bally Bet | The Odds API (`ballybet`) | `ODDS_API_KEY` (works on the current paid plan; free plan not checked) |

DraftKings, BetMGM and Caesars return HTTP 403 (Akamai / Cloudflare / CloudFront bot protection) to automated clients, even a real headless browser, and Fanatics' website doesn't serve its own lines. Rather than try to get around that protection, the agent gets these four books from [The Odds API](https://the-odds-api.com), a licensed feed. Bally Bet comes from the same feed: it's in the same request as the others (6 bookmakers counting Pinnacle, under the feed's 10-per-credit limit), so it costs no extra credits. Any book that comes back empty is listed at the end of each run.

**Bet365 isn't covered.** The Odds API has no US Bet365 feed on any plan (checked across all 74 bookmakers in every region, Oct 2026), and Bet365's own site sits behind a Cloudflare challenge.

**Quota:** one run uses 9 Odds API credits (3 markets × 3 leagues), so the free 500/month covers about 55 runs. In `--watch` mode, the direct books refresh every interval, and the Odds API books refresh at most once per `--odds-api-every` seconds (default 3600). At that default, the free tier lasts about 2–3 days of nonstop watching; for anything longer, raise the interval or move to a paid plan.

## Setup

```bash
cd sportsbook-lines-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env   # then paste your ODDS_API_KEY
```

## Usage

```bash
.venv/bin/python -m lines_agent                       # all books, all leagues, one run
.venv/bin/python -m lines_agent --league NCAAF        # one league
.venv/bin/python -m lines_agent --book FanDuel BetRivers
.venv/bin/python -m lines_agent --watch 300           # re-scrape every 5 minutes
.venv/bin/python -m lines_agent --live                # include in-play games
.venv/bin/python -m lines_agent --state pa            # state-specific endpoints (FanDuel, BetRivers)
```

### Web page

```bash
.venv/bin/python -m lines_agent.web          # then open http://127.0.0.1:8765/
```

Pick a league to see every game's moneyline, spread and total across all eight books, with the best price in each column highlighted. The page only answers on this machine (127.0.0.1). A league is scraped the first time you pick it and then cached. **Refresh** scrapes it again from the books and markets switched on in Scan controls, which takes about 15 seconds and uses up to 3 Odds API credits.

### Mac app

**Sportsbook Lines** in /Applications opens the same page in its own window, and adds a menu bar item (the chart icon) that shows the last and next scan and has **Scan Now**, which scans with the saved Scan controls without opening the window. Closing the window leaves the menu bar item running; clicking the Dock icon brings the window back. **Open at Login** is in the menu bar item's menu. To keep it in the Dock, right-click its Dock icon while it's open → Options → **Keep in Dock**.

The app is a window onto the server, which the launch agent keeps running (see Automatic runs). If the server isn't up when the app opens, the app starts it. To rebuild after changing `macapp/SportsbookLines.swift`:

```bash
macapp/build.sh
```

### Scan controls

The top of the arbitrage pane has toggles for what a scan pulls:

- **Sports:** the six leagues
- **Books:** the eight books. Books marked **API** come from The Odds API and cost credits; the others are free.
- **Game lines:** moneyline, spread, total. Each one is a separate Odds API credit per league.
- **Player props:** assists, rebounds, steals, turnovers. Each one costs up to a credit per basketball game.

Switched-off items aren't pulled and aren't paired for arbitrage. Below the toggles, the page shows roughly how many Odds API credits a scan will spend, and how many are left. Out-of-season leagues are skipped for free. **Scan selected**, a league's **Refresh**, and the menu bar's **Scan Now** all use these controls. A scan replaces only the lines it pulled and keeps the rest from earlier scans. The controls are saved on the Mac (`data/settings.json`), so the phone, the browser and the app share them.

**Edit schedule** (under the Scan button) lists the daily runs. Each run has its own time (US Mountain; every time in the app is Mountain), on/off switch and set of toggles, so, say, the 7:07 run can check only college basketball from the free books. Changes take effect right away.

### Arbitrage

The right-hand pane on the web page scans every league for **two-way arbitrage** across books, at or above a minimum guaranteed profit (default 1%):

- **Moneyline:** away at one book plus home at another
- **Totals:** over at one book plus under at another, where the over line is at or below the under line. If the lines differ (e.g. O 44.5 / U 45.5), the pick is flagged **MIDDLE**, because a final total between them wins both bets.

Each opportunity shows the stake split for your bankroll (your bankroll from History: starting amount plus settled P&L) so either outcome pays the same. Games with an opportunity get an **ARB** badge in the main list. The CLI prints the same results; set the threshold with `--min-arb 0.5`.

Profit = `1 / (1/dec_a + 1/dec_b) − 1`. Before you bet, check both prices still stand: lines move fast, and books may limit or void bets that look like arbitrage.

### +EV finder

**http://127.0.0.1:8765/ev** (or **+EV Finder** at the top of the main page) lists lines at your eight books that beat a sharp **consensus fair price** by at least 5% (adjustable), for NBA, NFL, NHL, college football and men's and women's college basketball.

1. Each sharp price becomes an implied probability, `p = 1 / decimal odds`.
2. The vig is removed three ways, each sharp book on its own:
   - **Multiplicative:** `fair = p_side / (p_side + p_other)`. Every side gives up the same share of its probability, so the favorite absorbs most of the vig.
   - **Shin:** models the book guarding against a share *z* of insiders and solves for *z*. It takes more vig off the longshot.
   - **Power:** `fair = p^k`, with *k* solved so both sides sum to 1. It takes the most vig off the longshot.

   The books are then blended by a weighted average (`SHARP_WEIGHTS` in `lines_agent/ev.py`) of the books that post that exact market and number. The three methods agree near even money and split widely on big underdogs, where multiplicative rates the dog highest. Each card shows all three; the **Method** picker sets which one the Min EV filter, sorting and Kelly stake use, and **All three must clear** keeps only lines that clear Min EV under every method.
3. EV at your book is `fair × decimal odds − 1`. The page also shows the Kelly fraction, `EV / (decimal − 1)`, and a ¼-Kelly stake of your bankroll from History (starting amount plus settled P&L, so stakes grow and shrink with results).

Spreads and totals are compared only at the same number as the sharp line, and only pregame. An edge of 20% or more is tagged **CHECK**, since on a main line that usually means a stale or mismatched price.

**Sharp books:** Pinnacle comes from The Odds API in the same request as DraftKings/BetMGM/Caesars/Fanatics, so it costs no extra credits, and every refresh or scheduled run updates it. Circa and BetCRIS aren't on The Odds API and have no public API; Circa is available from paid aggregators (e.g. SportsGameOdds Pro). To blend another sharp book, write an adapter for it, add it to `SHARP` in `books/__init__.py` and give it a weight in `SHARP_WEIGHTS`.

Scheduled runs post a Mac notification and a phone push (opening the +EV page) when they find a line at or above `--min-ev` (default 5%) by `--ev-method` (default `all`: multiplicative, Shin and power must each clear it; or one of `multiplicative`, `shin`, `power`). The page's Method picker is separate and defaults to **All three must clear**. **Re-pull lines** on the page re-scrapes all six leagues from every book, up to about 18 Odds API credits. From the CLI: `.venv/bin/python -m lines_agent --ev --league NBA NFL NHL`.

### History (bet tracker)

**History** (http://127.0.0.1:8765/bets, or /history; linked from every page; called My bets before) tracks the bets you actually place, for profit and loss.

- **Track bet** on a +EV card, or **Track arb** on an arbitrage card, opens a window to confirm the odds you got and how much you bet. It starts from the card's price and its ¼-Kelly (or arbitrage) stake; change either to what your bet slip says. An arbitrage is saved as two bets sharing an ID (ARB-1, ARB-2, …).
- On History, settle each bet with **Won**, **Lost** or **Push**, or **Cash out / edit** to enter a cash-out amount, correct the odds or stake, add notes or delete it. **Reopen** undoes a settlement. An open bet whose game is under way shows an orange **Live** tag, judged from its start time and a typical game length for the league.
- The top shows your bankroll (set the starting amount at the top of the page), settled P&L and ROI, money in open bets, your record, and +EV and arbitrage results separately. For +EV bets it also shows the **expected** profit (stake × EV at the odds you took), so you can see whether results are tracking the edge. Below that: a cumulative P&L chart, the bet list (filter by open/settled and type), and a by-sportsbook breakdown.
- A tracked +EV bet keeps its EV under all three de-vig methods, recalculated at the odds you took.

### Live

**Live** (http://127.0.0.1:8765/live, linked from every page) shows the games under way that you have open bets on: the score and clock, and under it each bet with its book, odds, stake and payout. An arrow shows how each bet stands if the game ended right now: up (would win), down (would lose) or flat (push, or a tied moneyline), except over/unders, below.

- Moneyline compares the two scores; a spread adds your line to your team's score. **Over/unders follow the pace** instead: the final total the game is on track for, from the share of regulation played. An under on a game pacing past the line points down even while it's still under, and an over pacing past it points up before it gets there; underneath, the card shows where it stands now. Once the line is passed the bet is decided (over won, under lost). In the first 10% of a game there's no pace yet, so the arrow uses the current score.
- Each card shows where the game is on, from ESPN: national networks first ("NBC", "ESPN+"), then each team's local channel with its abbreviation ("NESN (BOS)"). Nothing shows when ESPN doesn't list one.
- Football cards (NFL and college) show a football next to the team with the ball, with the down and distance underneath ("USU ball · 4th & 2 at FRES 38") and a **Red zone** tag inside the 20. ESPN leaves possession out during kickoffs, timeouts, reviews and breaks, so the ball disappears until the next snap.
- A postponed, canceled or suspended game isn't graded from its score (ESPN often leaves it at 0-0); it shows **Called off; usually voided** and suggests **Push**.
- Each bet shows its **projected** profit, which follows the arrow: the payout if it's trending up, the stake lost if down, nothing if flat. So an under on pace to go over is projected as a loss even while it's still under. The top adds it up, with the money at stake and how many bets are trending up and down.
- Games that have finished but whose bets you haven't settled show below, graded Won / Lost from the final score. Settle them right there with the same **Won**, **Lost**, **Push** and **Cash out / edit** choices as History; the one matching the final score is outlined, and **Undo** (for a few seconds) reopens the bet. Player props show the score but no arrow (there are no live player stats).
- Scores come from ESPN's free public scoreboard (no Odds API credits) and refresh every 30 seconds while the page is open.

Bets are stored on the Mac in `data/app.db`, one account per person, so the phone, browser and app share them. They don't sync to `Project Trackers/bet_tracker.xlsx`.

### Basketball player props

For NBA and men's and women's college basketball, the agent also collects player **assists, rebounds, steals and turnovers** Over/Unders. The arbitrage scan pairs an Over at one book with an Under at another on the same player and stat, using the same rules as game totals. "N+" milestone bets ("4+ assists", Yes only) count as Over N−0.5, so they can pair with another book's Under.

| Book | Source | Notes |
|---|---|---|
| FanDuel | event-page API, Player Assists / Rebounds / Defense tabs | assists, rebounds, steals |
| BetRivers | Kambi event offers | assists, rebounds (O/U and N+), steals (N+ only) |
| theScore Bet | each game's prop sections, in the browser | assists, rebounds (O/U and N+), steals (N+) |
| DraftKings, BetMGM, Caesars, Fanatics, Bally Bet | The Odds API, on via `ODDS_API_PROPS=1` in `.env` | up to 4 credits per game per run (one per stat market returned) |

Turnovers are collected, but no book had posted any when this was written. A milestone bet and an O/U at another book can settle differently if a player doesn't play, so check both books' rules before betting a pair that uses one. Books post props on game day, usually in the morning, so the 7:07 MT run may land before some books have posted. Many states don't allow college player props.

Props are scraped only by scheduled runs and **Scan selected** (a few minutes, since theScore means opening every game page), or on their own with `GET /api/props?league=NBA&refresh=1`, which costs no credits. On the page, each basketball game has a **Player props** section showing the best Over and Under across books.

### Automatic runs

The web server pulls lines daily at **7:07, 9:37, 13:57, 17:27 and 19:07 US Mountain** (change them with **Edit schedule**). The runs are set up for the +EV finder: game lines (no player props) for NBA, NFL, NHL, college football and men's and women's college basketball. Each run refreshes what the page shows (an open page picks it up within a minute) and posts a notification when it finds a +EV line (`--min-ev`, `--ev-method`). If the Mac is asleep at one of those times, the missed run happens once, when it wakes. The phone can't reach a sleeping Mac at all, so for the phone app and the scheduled runs to work reliably, turn on **Prevent automatic sleeping when the display is off** and **Start up automatically after a power failure** in System Settings → Energy. **Scan selected** runs on the Mac in the background; the page shows the results when it finishes, even if you left the app in the meantime. (`--schedule "07:07,10:00"` only sets the runs the first time, before `data/settings.json` exists.)

**Arbitrage is on demand only.** Scheduled runs don't check for it or alert on it; press **Scan selected** on the page, or **Scan Now** in the menu bar (which also sends an alert for arbitrage of at least `--min-arb`, default 1%). Both use the Scan controls, including player props if they're switched on there.

Each scheduled run spends 3 Odds API credits per in-season league: about 18 per run with all six in season, 12 while college basketball is off. Out-of-season leagues cost nothing.

A launch agent keeps the server running: it starts at login and restarts if it stops. The log is at `~/Library/Logs/sportsbook-lines-agent.log`.

```bash
# install / start
cp launchd/com.sportsbook-lines-agent.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sportsbook-lines-agent.plist
# restart after code changes
launchctl kickstart -k gui/$(id -u)/com.sportsbook-lines-agent
# stop and remove
launchctl bootout gui/$(id -u)/com.sportsbook-lines-agent && rm ~/Library/LaunchAgents/com.sportsbook-lines-agent.plist
```

Output goes to `data/`:
- `lines_<timestamp>.json`: a snapshot of every line from that run (command-line runs only; the web server's scrapes go to the CSV alone)
- `lines_history.csv`: one row per line, appended every run and every web-server scrape, for tracking line movement. The web server deletes rows older than 365 days every night at 3:30 MT (and the CLI's `lines_<time>.json` snapshots older than that), so it holds at most a year (roughly 5 GB at current use)

Each row looks like this:
`book, league, event_id, start_time, away_team, home_team, market, side, line, odds, live, scraped_at`

### Phone app (home screen + Tailscale)

The page installs as a home-screen app: it gets its own icon, opens full screen, and has a phone layout with **Lines / Arbitrage** tabs and a **Moneyline / Spread / Total** switch. When the phone can't reach the Mac, it opens with the last lines it loaded.

The server only listens on this Mac. [Tailscale](https://tailscale.com) gives your phone a private, encrypted route to it, over Wi-Fi or cellular, without opening it to the internet.

1. **Mac:** install Tailscale from the Mac App Store (or tailscale.com/download), open it and sign in.
2. **iPhone:** install Tailscale from the App Store and sign in with the **same account**. Leave it connected.
3. **Mac, Terminal:** share the server inside your tailnet over HTTPS:
   ```bash
   /Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg 8765
   ```
   If it says HTTPS isn't enabled, open the link it prints, turn on HTTPS certificates, and run it again. It prints your app's address, e.g. `https://chris-mac.tail1234.ts.net`. The setting survives restarts. To turn it off: `... Tailscale serve --https=443 off`.
4. **iPhone, Safari:** open that address → Share → **Add to Home Screen**. (On Android: Chrome menu → **Install app**.)

The Mac has to be on, awake and running Tailscale for the phone to get new lines.

#### Push alerts

When a scheduled run finds arbitrage of at least 1% (the server's `--min-arb`), every device that turned on alerts gets a push notification. Tap it to open the app on the Arbitrage tab.

To turn them on: open **Lines from its home-screen icon** (on iPhone, alerts only work there, not in a Safari tab), go to the **Arbitrage** tab, tap **Turn on alerts** and allow notifications. **Send test** confirms the whole route works.

- The Mac signs each push with its own key (`data/vapid_private.pem`) and keeps device subscriptions in `data/push_subscriptions.json`. Delete both to reset.
- Pushes go through Apple's (or Google's) push service, end-to-end encrypted. The phone gets them even when Tailscale is disconnected. The Mac still has to be awake to run the scans.
- An alert that can't be delivered within 30 minutes is dropped, since the price will have moved by then.

## Adding a book

Add a module in `lines_agent/books/` with `NAME` and `fetch(league, **kw) -> list[Line]`, then register it in `books/__init__.py`.

## Notes

- These endpoints are undocumented and change without notice. When a book breaks, the run still finishes and reports which book/league failed.
- Check each sportsbook's terms of use before running this at high frequency. Keep `--watch` intervals reasonable (minutes, not seconds).

## Stats (desktop)

**Stats** (http://127.0.0.1:8765/stats, linked on the Mac only: the link is hidden on phones and tablets) shows how the app is running: disk use by part, the server's memory and uptime, Odds API credits left and used per day, requests, errors and response times over the last 24 hours, data per page open on the phone (compressed vs not) and what the phone keeps offline, scrape and scheduled-run health over the last 7 days, push alerts, how fresh each league's cached lines are, results by sportsbook, and the newest entries in errors.log. It reads the files and logs that already exist and refreshes every minute while open.

## Private app security

The private server has no login, so it relies on listening only on this Mac and Tailscale. It also refuses other websites: a request must be addressed to `127.0.0.1`, `localhost` or the Tailscale name (add more with `PRIVATE_HOSTS` in `.env`), and any POST or `/api/` request a browser marks as cross-site is rejected. The Mac app and command-line tools aren't affected. If the Tailscale machine name ever changes, update `PRIVATE_HOSTS`.

## Public site

`lines_agent/public.py` is a separate program that shows **History** and **Live** through Cloudflare, for one or several people. Cloudflare Access handles the login; the program checks Access's signed token on every request, and each person sees and changes only their own bets and bankroll. It can't start a scan or spend Odds API credits, and never loads the API key or the scraping code. The private server, Mac app and Tailscale phone app are unchanged and use the owner's account.

- **Adding a bet:** **Add a bet** on History takes the game from ESPN's free schedule, then the market, pick, line, book, odds and stake. Live grades these like any other bet.
- **Setup:** `data/public_site.json` holds `team_domain`, `aud` (the Access application's audience tag) and `owner_email` (sees the bets the Mac tracks). Without a valid file the program stops rather than start unprotected.
- **Adding a person:** open **People with access** on the private History page and enter their email. The Mac adds them to the Cloudflare Access group the login rule uses, so nobody edits Cloudflare by hand. **Remove** ends their access at once and keeps their bets in case you invite them back. This needs `CF_API_TOKEN` (limited to "Access: Groups", Read and Edit), `CF_ACCOUNT_ID` and `CF_ACCESS_GROUP_ID` in `.env`, which only the private server reads.
- **Run:** `launchd/com.sportsbook-lines-public.plist` keeps it running on `127.0.0.1:8766`. Only Cloudflare's connector on this Mac talks to it.
- **Logs:** `public.log` and `public-errors.log`, next to the others.
- **Off switch:** `launchctl bootout gui/$(id -u)/com.sportsbook-lines-public` stops the public site at once and leaves the private app running. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sportsbook-lines-public.plist` turns it back on.
- **Tests:** `.venv/bin/python -m unittest tests.test_public`

The bets database is copied daily to `data/backups/` (kept 90 days). That's the same drive, so it protects against a bad edit or a damaged file, not against losing the disk.

## Logs

Everything is logged on the Mac, in `~/Library/Logs/SportsbookLines/` (open it in Console.app or any text editor):

- `errors.log`: warnings and errors only. Read this one first. It includes reports from the phone app (lines starting `PHONE`), slow responses (`SLOW`), server errors with their tracebacks, failed scrapes and failed pushes.
- `app.log`: everything, including one line per request with its status, size, time taken and device (`GET /api/live 200 4.5KB 156ms iPhone`), plus scrapes, scheduled runs and pushes.

Each file starts fresh at midnight; the previous day is kept as `app.log.2026-11-07` and so on. Nothing is deleted automatically (about 100 KB a day at current use). The Odds API key is hidden in anything logged.

The phone app reports its own problems to the Mac: a page that couldn't reach it ("Load failed"), a server error, a slow answer, or a script error. It sends nothing while things work. When the Mac is out of reach, the phone holds the reports (at most 10, a few KB) and sends them the next time it connects; once the Mac confirms, they're deleted from the phone. The same problem repeating is counted (x3), not sent again.

The launchd log, `~/Library/Logs/sportsbook-lines-agent.log`, now only records server starts and crashes that happen before logging is set up.

