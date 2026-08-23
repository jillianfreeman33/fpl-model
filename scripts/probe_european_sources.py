#!/usr/bin/env python3
"""Throwaway probe round 2.

Round 1 result: TheSportsDB free tier truncates to 5-15 events (July qualifiers
only, no English clubs); football-data.org returns 403 without a paid token;
openfootball root is a README. Now testing ESPN's free unauthenticated API and
openfootball's actual data tree.
"""
import json

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fpl-snapshot-bot/1.0)"}
ENGLISH_HINTS = ("Arsenal", "Manchester", "Aston Villa", "Liverpool", "Bournemouth",
                 "Sunderland", "Crystal Palace", "Brighton")
ESPN_LEAGUES = {"UCL": "uefa.champions", "UEL": "uefa.europa", "UECL": "uefa.europa.conf"}


def fetch(label, url, **kwargs):
    print(f"\n{'=' * 70}\n{label}\n{url}  {kwargs.get('params','')}\n{'-' * 70}")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=25, **kwargs)
    except Exception as exc:
        print(f"  UNREACHABLE: {type(exc).__name__}: {exc}")
        return None
    print(f"  HTTP {resp.status_code}  bytes={len(resp.content)}")
    if resp.status_code != 200:
        print(f"  body[:200]: {resp.text[:200]}")
        return None
    try:
        return resp.json()
    except ValueError:
        print(f"  not JSON. body[:200]: {resp.text[:200]}")
        return resp.text


def summarize_espn(data):
    if not isinstance(data, dict):
        return
    events = data.get("events") or []
    print(f"  events: {len(events)}")
    if not events:
        print(f"  top-level keys: {list(data.keys())[:12]}")
        return
    for e in events[:4]:
        names = [c.get("team", {}).get("displayName") for c in
                 (e.get("competitions") or [{}])[0].get("competitors", [])]
        print(f"    {e.get('date')}  {' v '.join(n for n in names if n)}")
    english = [e for e in events if any(
        h in json.dumps(e.get("competitions", [{}])[0].get("competitors", [])) for h in ENGLISH_HINTS)]
    print(f"  involving English clubs: {len(english)}")
    for e in english[:8]:
        names = [c.get("team", {}).get("displayName") for c in
                 (e.get("competitions") or [{}])[0].get("competitors", [])]
        print(f"    ENG> {e.get('date')}  {' v '.join(n for n in names if n)}")


# 1. ESPN scoreboard over the league-phase window, per competition.
for label, slug in ESPN_LEAGUES.items():
    data = fetch(f"ESPN scoreboard {label} (league-phase window)",
                 f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard",
                 params={"dates": "20260908-20260918"})
    summarize_espn(data)

# 2. ESPN full-season range, to see whether it will serve a wide window at once.
data = fetch("ESPN scoreboard UCL (whole season range)",
             "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard",
             params={"dates": "20260901-20270601"})
summarize_espn(data)

# 3. ESPN per-team schedule, which would sidestep date-window paging entirely.
data = fetch("ESPN teams list UCL (to find club ids)",
             "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/teams")
if isinstance(data, dict):
    try:
        teams = data["sports"][0]["leagues"][0]["teams"]
        print(f"  teams: {len(teams)}")
        for t in teams[:10]:
            print(f"    id={t['team'].get('id'):>6}  {t['team'].get('displayName')}")
    except (KeyError, IndexError) as exc:
        print(f"  unexpected shape: {exc}; keys={list(data.keys())[:10]}")

# 4. openfootball actual data tree for 2026-27.
for path in ("2026-27", "2026-27/cl.txt"):
    fetch(f"openfootball champions-league contents {path}",
          f"https://api.github.com/repos/openfootball/champions-league/contents/{path}")

print("\nPROBE 2 COMPLETE")
