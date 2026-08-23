#!/usr/bin/env python3
"""Throwaway probe round 2 -- settle the crux questions.

Round 1: FPL strength_attack_*/strength_defence_* are 0 for all 20 clubs, but
strength_overall_home/away ARE populated (1-5). FPL fixtures reported
finished=0 despite player histories containing GW1 minutes and xG, so check
whether SCORES are populated regardless of the finished flag. understat
returned an 18KB page with no teamsData; football-data.co.uk 300'd on the URL;
fbref is Cloudflare-blocked.
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
        print(f"  body[:160]: {r.text[:160]}")
        return None
    return r


# --- CRUX 1: are fixture scores populated even though finished is False? ---
r = get("FPL fixtures -- scores vs finished flags", f"{FPL}/fixtures/")
if r:
    fx = r.json()
    scored = [f for f in fx if f.get("team_h_score") is not None]
    print(f"  total={len(fx)}  finished=={sum(1 for f in fx if f.get('finished'))}"
          f"  finished_provisional=={sum(1 for f in fx if f.get('finished_provisional'))}"
          f"  WITH SCORES={len(scored)}")
    if scored:
        s = scored[0]
        print(f"  sample scored fixture: event={s.get('event')} "
              f"{s.get('team_h')} {s.get('team_h_score')}-{s.get('team_a_score')} {s.get('team_a')} "
              f"finished={s.get('finished')} provisional={s.get('finished_provisional')} "
              f"kickoff={s.get('kickoff_time')}")
        print(f"  all fixture keys: {sorted(s.keys())}")
        ids = sorted({st.get('identifier') for f in scored for st in (f.get('stats') or [])})
        print(f"  stats identifiers present: {ids}")
        by_ev = {}
        for f in scored:
            by_ev[f.get('event')] = by_ev.get(f.get('event'), 0) + 1
        print(f"  scored fixtures per gameweek: {dict(sorted(by_ev.items()))}")
    else:
        print("  NO fixture has a score yet -> no team-level match data exists in FPL at all.")

# --- CRUX 2: full strength_overall spread across all 20 clubs ---
r = get("FPL teams -- strength_overall spread (candidate prior)", f"{FPL}/bootstrap-static/")
if r:
    teams = r.json()["teams"]
    print(f"  {'club':<6}{'overall_home':>14}{'overall_away':>14}")
    for t in sorted(teams, key=lambda x: -(x['strength_overall_home'] or 0)):
        print(f"  {t['short_name']:<6}{t['strength_overall_home']:>14}{t['strength_overall_away']:>14}")
    hs = [t['strength_overall_home'] for t in teams]
    as_ = [t['strength_overall_away'] for t in teams]
    print(f"  home range {min(hs)}-{max(hs)}, away range {min(as_)}-{max(as_)}, "
          f"distinct home values={sorted(set(hs))}")

# --- CRUX 3: any reachable team-level xG? ---
for label, url in [
    ("understat EPL 2026", "https://understat.com/league/EPL/2026"),
    ("understat EPL 2025 (prior season, proves parser + reachability)",
     "https://understat.com/league/EPL/2025"),
]:
    r = get(label, url)
    if r:
        m = re.search(r"teamsData\s*=\s*JSON\.parse\('([^']+)'", r.text)
        print(f"  teamsData found: {bool(m)}")
        if m:
            d = json.loads(m.group(1).encode().decode("unicode_escape"))
            f = next(iter(d.values()))
            h = (f.get("history") or [{}])[0]
            print(f"  teams={len(d)} sample={f.get('title')} per-match keys={sorted(h.keys())}")
            print(f"  sample match: h_a={h.get('h_a')} xG={h.get('xG')} xGA={h.get('xGA')}")

for label, url in [
    ("football-data.co.uk E0 26/27", "https://www.football-data.co.uk/mmz4281/2627/E0.csv"),
    ("football-data.co.uk fixtures.csv", "https://www.football-data.co.uk/fixtures.csv"),
]:
    r = get(label, url, allow_redirects=True)
    if r:
        rows = list(csv.DictReader(io.StringIO(r.text)))
        print(f"  rows={len(rows)}")
        if rows:
            print(f"  columns[:26]: {list(rows[0].keys())[:26]}")
            e0 = [x for x in rows if x.get('Div') == 'E0']
            print(f"  E0 rows={len(e0)}")
            if e0:
                x = e0[0]
                print(f"  sample: {x.get('Date')} {x.get('HomeTeam')} {x.get('FTHG')}-{x.get('FTAG')} "
                      f"{x.get('AwayTeam')} HS/AS={x.get('HS')}/{x.get('AS')}")

print("\nPROBE COMPLETE")
