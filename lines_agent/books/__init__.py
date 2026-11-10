from . import betrivers, fanduel, oddsapi, thescore

# Books scraped straight from the sportsbook's own public endpoints.
DIRECT = {
    fanduel.NAME: fanduel,
    betrivers.NAME: betrivers,
    thescore.NAME: thescore,
}

# Books that block automated access to their sites; served by The Odds API instead.
# Caesars and Fanatics need a paid Odds API plan. Bally Bet comes from here too:
# it rides in the same request at no extra cost, so it has no scraper of its own.
VIA_ODDS_API = ["DraftKings", "BetMGM", "Caesars", "Fanatics", "Bally Bet"]

ALL_BOOKS = list(DIRECT) + VIA_ODDS_API

# Sharp books, pulled only to price the +EV finder (ev.py); they aren't among the
# books you bet at, so the lines grid and arbitrage leave them out. They come
# from The Odds API in the same request as VIA_ODDS_API, which costs nothing
# extra while the request names 10 bookmakers or fewer (it names 6).
SHARP = ["Pinnacle"]
