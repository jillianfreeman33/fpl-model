#!/usr/bin/env python3
"""Fetch a daily FPL data snapshot and write it to snapshot.json."""
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

API_BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fpl-snapshot-bot/1.0)"}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
OUTPUT_PATH = os.path.join(ROOT, "snapshot.json")

CHIPS_THAT_SKIP_TRANSFER_DEDUCTION = {"wildcard", "freehit"}


def get(path, **params):
    url = f"{API_BASE}/{path}"
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=20)
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
        time.sleep(2 * (attempt + 1))
    if last_exc:
        raise last_exc
    resp.raise_for_status()


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def player_record(el, teams_by_id, types_by_id):
    return {
        "name": el["web_name"],
        "club": teams_by_id[el["team"]]["short_name"],
        "pos": types_by_id[el["element_type"]],
        "price": el["now_cost"] / 10,
        # Public API has no auth'd sell price; approximate with current price.
        "selling_price": el["now_cost"] / 10,
        "ownership": float(el["selected_by_percent"]),
        "status": el["status"],
        "chance_of_playing": el.get("chance_of_playing_next_round"),
        "form": float(el["form"]),
    }


def compute_free_transfers(history, upcoming_event_id):
    """Best-effort estimate of banked free transfers (max 5), since FPL's
    public API doesn't expose this directly."""
    if not upcoming_event_id:
        return None
    played = sorted(history.get("current", []), key=lambda c: c["event"])
    chips = {c["event"]: c["name"] for c in history.get("chips", [])}
    ft = 1
    for gw in played:
        event = gw["event"]
        if event >= upcoming_event_id:
            break
        if chips.get(event) in CHIPS_THAT_SKIP_TRANSFER_DEDUCTION:
            ft = min(ft + 1, 5)
            continue
        ft = max(ft - gw.get("event_transfers", 0), 0)
        ft = min(ft + 1, 5)
    return ft


def build_fixtures_next6(fixtures, teams_by_id, my_club_ids):
    upcoming = [f for f in fixtures if not f["finished"] and f.get("event")]
    upcoming.sort(key=lambda f: (f["event"], f.get("kickoff_time") or ""))

    fixtures_next6 = {}
    for club_id in my_club_ids:
        club_short = teams_by_id[club_id]["short_name"]
        club_fixtures = []
        for f in upcoming:
            if f["team_h"] == club_id:
                opp, is_home, difficulty = teams_by_id[f["team_a"]]["short_name"], True, f["team_h_difficulty"]
            elif f["team_a"] == club_id:
                opp, is_home, difficulty = teams_by_id[f["team_h"]]["short_name"], False, f["team_a_difficulty"]
            else:
                continue
            club_fixtures.append(
                {"gw": f["event"], "opponent": opp, "is_home": is_home, "difficulty": difficulty}
            )
            if len(club_fixtures) == 6:
                break
        fixtures_next6[club_short] = club_fixtures
    return fixtures_next6


def build_rivals(league_ids, picks_gw, elements):
    rivals = {}
    for league_id in league_ids:
        standings = get(f"leagues-classic/{league_id}/standings/")
        results = standings["standings"]["results"][:20]
        league_rivals = []
        for r in results:
            rival_entry_id = r["entry"]
            try:
                rival_picks_resp = get(f"entry/{rival_entry_id}/event/{picks_gw}/picks/")
                rival_picks = [elements[p["element"]]["web_name"] for p in rival_picks_resp["picks"]]
            except requests.RequestException:
                rival_picks = []
            league_rivals.append(
                {
                    "entry_id": rival_entry_id,
                    "name": r["player_name"],
                    "total": r["total"],
                    "picks": rival_picks,
                }
            )
            time.sleep(0.1)
        rivals[str(league_id)] = league_rivals
    return rivals


def main():
    config = load_config()
    entry_id = config["entry_id"]
    league_ids = config["league_ids"]
    watchlist_size = config.get("watchlist_size", 60)

    bootstrap = get("bootstrap-static/")
    elements = {el["id"]: el for el in bootstrap["elements"]}
    teams_by_id = {t["id"]: t for t in bootstrap["teams"]}
    types_by_id = {t["id"]: t["singular_name_short"] for t in bootstrap["element_types"]}
    events = bootstrap["events"]

    current_event = next((e for e in events if e["is_current"]), None)
    next_event = next((e for e in events if e["is_next"]), None)
    gw_current = current_event["id"] if current_event else None
    gw_next = next_event["id"] if next_event else None
    next_deadline = next_event["deadline_time"] if next_event else None
    picks_gw = gw_current or gw_next

    fixtures = get("fixtures/")
    entry_info = get(f"entry/{entry_id}/")
    entry_history = get(f"entry/{entry_id}/history/")
    my_picks_resp = get(f"entry/{entry_id}/event/{picks_gw}/picks/")

    my_picks = my_picks_resp["picks"]
    my_ids = {p["element"] for p in my_picks}

    my_squad = [player_record(elements[pid], teams_by_id, types_by_id) for pid in (p["element"] for p in my_picks)]

    candidates = [
        player_record(el, teams_by_id, types_by_id) for pid, el in elements.items() if pid not in my_ids
    ]
    candidates.sort(key=lambda p: (p["ownership"], p["form"]), reverse=True)
    watchlist = candidates[:watchlist_size]

    my_club_ids = {elements[pid]["team"] for pid in my_ids}
    fixtures_next6 = build_fixtures_next6(fixtures, teams_by_id, my_club_ids)

    rivals = build_rivals(league_ids, picks_gw, elements)

    price_watch = []
    for pid in my_ids:
        el = elements[pid]
        transfers_in = el.get("transfers_in_event", 0)
        transfers_out = el.get("transfers_out_event", 0)
        price_watch.append(
            {
                "name": el["web_name"],
                "transfers_in": transfers_in,
                "transfers_out": transfers_out,
                "net": transfers_in - transfers_out,
            }
        )

    bank = entry_info.get("last_deadline_bank", 0) / 10
    squad_value = entry_info.get("last_deadline_value", 0) / 10
    free_transfers = compute_free_transfers(entry_history, picks_gw)

    snapshot = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "api",
        "gw": {"current": gw_current, "next": gw_next, "next_deadline": next_deadline},
        "my_squad": my_squad,
        "watchlist": watchlist,
        "fixtures_next6": fixtures_next6,
        "rivals": rivals,
        "price_watch": price_watch,
        "bank": bank,
        "squad_value": squad_value,
        "free_transfers": free_transfers,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(snapshot, f, separators=(",", ":"))

    size_bytes = os.path.getsize(OUTPUT_PATH)
    print(f"snapshot.json written: {size_bytes} bytes ({size_bytes / 1024:.2f} KB)")
    if size_bytes > 200 * 1024:
        print("WARNING: snapshot exceeds 200KB target", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
