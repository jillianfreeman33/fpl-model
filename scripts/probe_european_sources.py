#!/usr/bin/env python3
"""Throwaway probe: which free source can give us per-club European fixtures?

Run from the GitHub Actions runner (the dev sandbox has no egress to these
hosts). Prints reachability and payload shape for each candidate so we can
pick one before wiring it into snapshot.py. Delete once a source is chosen.
"""
import json

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fpl-snapshot-bot/1.0)"}
SEASON = "2026-2027"
ENGLISH_HINTS = ("Arsenal", "Manchester", "Aston Villa", "Liverpool", "Bournemouth",
                 "Sunderland", "Crystal Palace", "Brighton")


def show(label, url, **kwargs):
    print(f"\n{'=' * 70}\n{label}\n{url}\n{'-' * 70}")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=25, **kwargs)
    except Exception as exc:
        print(f"  UNREACHABLE: {type(exc).__name__}: {exc}")
        return None
    print(f"  HTTP {resp.status_code}  content-type={resp.headers.get('content-type','?')}  bytes={len(resp.content)}")
    if resp.status_code != 200:
        print(f"  body[:300]: {resp.text[:300]}")
        return None
    try:
        return resp.json()
    except ValueError:
        print(f"  NOT JSON. body[:300]: {resp.text[:300]}")
        return None


def summarize_sportsdb(data, league):
    events = (data or {}).get("events")
    if not events:
        print(f"  no 'events' key or empty. keys={list((data or {}).keys())}")
        return
    print(f"  events returned: {len(events)}")
    sample = events[0]
    print(f"  sample keys: {sorted(sample.keys())[:18]}")
    print(f"  sample: {sample.get('dateEvent')} {sample.get('strTime')} | "
          f"{sample.get('strHomeTeam')} v {sample.get('strAwayTeam')} | round={sample.get('intRound')}")
    english = [e for e in events
               if any(h in f"{e.get('strHomeTeam','')} {e.get('strAwayTeam','')}" for h in ENGLISH_HINTS)]
    print(f"  fixtures involving English clubs: {len(english)}")
    for e in english[:6]:
        print(f"    {e.get('dateEvent')} {e.get('strTime') or '':>8}  "
              f"{e.get('strHomeTeam')} v {e.get('strAwayTeam')}  (round {e.get('intRound')})")


for key in ("3", "123"):
    for league_id, league in (("4480", "UCL"), ("4481", "UEL"), ("5071", "UECL")):
        data = show(
            f"TheSportsDB v1 key={key} eventsseason {league}",
            f"https://www.thesportsdb.com/api/v1/json/{key}/eventsseason.php",
            params={"id": league_id, "s": SEASON},
        )
        if data is not None:
            summarize_sportsdb(data, league)
    # One key is usually enough; only try the second if the first gave nothing.

show("football-data.org v4 CL matches (no token -> expect 401/403, tests reachability)",
     "https://api.football-data.org/v4/competitions/CL/matches", params={"season": "2026"})

show("openfootball champions-league raw (no key)",
     "https://raw.githubusercontent.com/openfootball/champions-league/master/README.md")

print("\nPROBE COMPLETE")
