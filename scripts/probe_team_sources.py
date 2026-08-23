#!/usr/bin/env python3
"""Throwaway probe: find a TEAM-LEVEL source for team strength.

Current team_strength is aggregated from a 75-player sample, which biases it by
how many of a club's players happen to be on the watchlist. Team metrics must
come from team-level data instead. Checking what FPL itself already gives us at
team level, then external team-xG sources. Delete once a source is chosen.
"""
import csv
import io
import json
import re

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fpl-snapshot-bot/1.0)"}
FPL = "https://fantasy.premierleague.com/api"


def get(label, url, **kwargs):
    print(f"\n{'=' * 72}\n{label}\n{url}\n{'-' * 72}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=25, **kwargs)
    except Exception as exc:
        print(f"  UNREACHABLE: {type(exc).__name__}: {exc}")
        return None
    print(f"  HTTP {r.status_code}  bytes={len(r.content)}")
    if r.status_code != 200:
        print(f"  body[:200]: {r.text[:200]}")
        return None
    return r


# --- 1. FPL bootstrap teams[]: team-level strength ratings, already fetched ---
r = get("FPL bootstrap-static -> teams[] (team-level strength ratings)", f"{FPL}/bootstrap-static/")
if r:
    teams = r.json()["teams"]
    keys = [k for k in teams[0] if "strength" in k or k in ("name", "short_name", "id")]
    print(f"  team-level keys: {keys}")
    nonzero = 0
    for t in teams[:6]:
        vals = {k: t[k] for k in keys if k not in ("id", "name", "short_name")}
        print(f"    {t['short_name']}: {vals}")
    for t in teams:
        if any(t.get(k) for k in keys if k.startswith("strength")):
            nonzero += 1
    print(f"  clubs with ANY non-zero strength rating: {nonzero}/{len(teams)}")

# --- 2. FPL fixtures[]: team-level scores for finished matches ---
r = get("FPL fixtures -> finished matches (team-level goals, no sampling)", f"{FPL}/fixtures/")
if r:
    fx = r.json()
    fin = [f for f in fx if f.get("finished")]
    print(f"  fixtures total={len(fx)} finished={len(fin)}")
    if fin:
        s = fin[0]
        print(f"  sample finished fixture keys: {sorted(k for k in s if not k.startswith('stats'))}")
        print(f"  sample: event={s.get('event')} team_h={s.get('team_h')} {s.get('team_h_score')}"
              f" - {s.get('team_a_score')} team_a={s.get('team_a')}")
        stat_ids = [st.get("identifier") for st in (s.get("stats") or [])]
        print(f"  per-fixture 'stats' identifiers: {stat_ids}")
        print("  NOTE: goals/clean sheets are exactly derivable per club per venue from these.")

# --- 3. External team-level xG candidates ---
r = get("understat EPL 2026 (team xG, embedded JSON)", "https://understat.com/league/EPL/2026")
if r:
    m = re.search(r"teamsData\s*=\s*JSON\.parse\('([^']+)'", r.text)
    print(f"  teamsData block found: {bool(m)}")
    if m:
        data = json.loads(m.group(1).encode().decode("unicode_escape"))
        first = next(iter(data.values()))
        print(f"  teams: {len(data)}  sample title={first.get('title')}")
        h = (first.get("history") or [{}])[0]
        print(f"  per-match keys: {sorted(h.keys())}")
        print(f"  sample match: h_a={h.get('h_a')} xG={h.get('xG')} xGA={h.get('xGA')} "
              f"date={h.get('date')} scored={h.get('scored')} missed={h.get('missed')}")

r = get("football-data.co.uk E0 2026/27 (team-level match CSV)",
        "https://www.football-data.co.uk/mmz4281/2627/E0.csv")
if r:
    rows = list(csv.DictReader(io.StringIO(r.text)))
    print(f"  rows: {len(rows)}")
    if rows:
        cols = list(rows[0].keys())
        print(f"  columns[:24]: {cols[:24]}")
        print(f"  sample: {rows[0].get('Date')} {rows[0].get('HomeTeam')} "
              f"{rows[0].get('FTHG')}-{rows[0].get('FTAG')} {rows[0].get('AwayTeam')} "
              f"shots H/A={rows[0].get('HS')}/{rows[0].get('AS')} "
              f"SoT H/A={rows[0].get('HST')}/{rows[0].get('AST')}")

get("fbref Premier League 2026-2027 squad stats", "https://fbref.com/en/comps/9/Premier-League-Stats")

print("\nPROBE COMPLETE")
