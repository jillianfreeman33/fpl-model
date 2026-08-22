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
ELEMENT_SUMMARY_CACHE_PATH = os.path.join(ROOT, "data", "element_summary_cache.json")

CHIPS_THAT_SKIP_TRANSFER_DEDUCTION = {"wildcard", "freehit"}
ELEMENT_SUMMARY_SLEEP_SECONDS = 0.2

# DefCon 2pt threshold: 10 CBIT for defenders/keepers, 12 CBIRT for mids/forwards.
DEFCON_THRESHOLD_BY_POS = {"GKP": 10, "DEF": 10, "MID": 12, "FWD": 12}

# Fields kept per gameweek in the cache -- just enough to derive the stats below.
HISTORY_FIELDS = ("round", "minutes", "starts", "expected_goals", "expected_assists", "defensive_contribution", "bonus")


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


def load_element_summary_cache():
    if not os.path.exists(ELEMENT_SUMMARY_CACHE_PATH):
        return {}
    with open(ELEMENT_SUMMARY_CACHE_PATH) as f:
        return json.load(f)


def save_element_summary_cache(cache):
    os.makedirs(os.path.dirname(ELEMENT_SUMMARY_CACHE_PATH), exist_ok=True)
    with open(ELEMENT_SUMMARY_CACHE_PATH, "w") as f:
        json.dump(cache, f, separators=(",", ":"))


def fetch_player_history(player_id, cache, latest_finished_gw):
    """Per-gameweek history for one player, cached by player_id so repeat
    runs only hit the API again once a new gameweek has finished."""
    key = str(player_id)
    cached = cache.get(key)
    if cached and cached.get("last_synced_gw", -1) >= latest_finished_gw:
        return cached["history"]

    resp = get(f"element-summary/{player_id}/")
    history = [{field: gw.get(field) for field in HISTORY_FIELDS} for gw in resp.get("history", [])]
    cache[key] = {"last_synced_gw": latest_finished_gw, "history": history}
    time.sleep(ELEMENT_SUMMARY_SLEEP_SECONDS)
    return history


def compute_player_stats(history, price, total_points, pos):
    played = [gw for gw in history if (gw.get("minutes") or 0) > 0]
    apps = len(played)
    minutes_total = sum(gw.get("minutes") or 0 for gw in history)
    starts = sum(1 for gw in played if gw.get("starts"))
    bonus_total = sum(gw.get("bonus") or 0 for gw in history)
    points_per_million = round(total_points / price, 2) if price else None

    # Guard every per-90/rate stat: under 90 minutes is too small a sample to
    # extrapolate a rate from, so emit null rather than a wild number.
    if minutes_total < 90:
        xg_per90 = xa_per90 = xgi_per90 = defcon_per90 = defcon_hit_rate = None
    else:
        nineties = minutes_total / 90
        xg_total = sum(float(gw.get("expected_goals") or 0) for gw in history)
        xa_total = sum(float(gw.get("expected_assists") or 0) for gw in history)
        defcon_total = sum(gw.get("defensive_contribution") or 0 for gw in history)
        xg_per90 = round(xg_total / nineties, 2)
        xa_per90 = round(xa_total / nineties, 2)
        xgi_per90 = round(xg_per90 + xa_per90, 2)
        defcon_per90 = round(defcon_total / nineties, 2)
        threshold = DEFCON_THRESHOLD_BY_POS.get(pos, 12)
        hits = sum(1 for gw in played if (gw.get("defensive_contribution") or 0) >= threshold)
        defcon_hit_rate = round(hits / apps, 2)

    return {
        "minutes_total": minutes_total,
        "minutes_per_app": round(minutes_total / apps, 1) if apps else None,
        "starts": starts,
        "apps": apps,
        "xg_per90": xg_per90,
        "xa_per90": xa_per90,
        "xgi_per90": xgi_per90,
        "defcon_per90": defcon_per90,
        "defcon_hit_rate": defcon_hit_rate,
        "points_per_million": points_per_million,
        "bonus_total": bonus_total,
    }


def player_record(el, teams_by_id, types_by_id, player_stats):
    record = {
        "player_id": el["id"],
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
    record.update(player_stats)
    return record


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


def build_fixtures_next6(fixtures, teams_by_id, club_ids):
    upcoming = [f for f in fixtures if not f["finished"] and f.get("event")]
    upcoming.sort(key=lambda f: (f["event"], f.get("kickoff_time") or ""))

    fixtures_next6 = {}
    for club_id in club_ids:
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


def attach_player_stats(records, elements, cache, latest_finished_gw):
    for record in records:
        el = elements[record["player_id"]]
        history = fetch_player_history(record["player_id"], cache, latest_finished_gw)
        record.update(compute_player_stats(history, el["now_cost"] / 10, el["total_points"], record["pos"]))


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
    finished_events = [e for e in events if e["finished"]]
    latest_finished_gw = max((e["id"] for e in finished_events), default=0)

    fixtures = get("fixtures/")
    entry_info = get(f"entry/{entry_id}/")
    entry_history = get(f"entry/{entry_id}/history/")
    my_picks_resp = get(f"entry/{entry_id}/event/{picks_gw}/picks/")

    my_picks = my_picks_resp["picks"]
    my_ids = {p["element"] for p in my_picks}

    empty_stats = {
        "minutes_total": None, "minutes_per_app": None, "starts": None, "apps": None,
        "xg_per90": None, "xa_per90": None, "xgi_per90": None, "defcon_per90": None,
        "defcon_hit_rate": None, "points_per_million": None, "bonus_total": None,
    }
    my_squad = [
        player_record(elements[pid], teams_by_id, types_by_id, empty_stats)
        for pid in (p["element"] for p in my_picks)
    ]

    candidates = [
        player_record(el, teams_by_id, types_by_id, empty_stats) for pid, el in elements.items() if pid not in my_ids
    ]
    candidates.sort(key=lambda p: (p["ownership"], p["form"]), reverse=True)
    watchlist = candidates[:watchlist_size]

    cache = load_element_summary_cache()
    attach_player_stats(my_squad, elements, cache, latest_finished_gw)
    attach_player_stats(watchlist, elements, cache, latest_finished_gw)
    save_element_summary_cache(cache)

    watched_club_ids = {elements[p["player_id"]]["team"] for p in my_squad + watchlist}
    fixtures_next6 = build_fixtures_next6(fixtures, teams_by_id, watched_club_ids)

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

    all_watched_players = my_squad + watchlist
    data_maturity = {
        "gameweeks_played": len(finished_events),
        "players_with_90plus_minutes": sum(
            1 for p in all_watched_players if p["minutes_total"] is not None and p["minutes_total"] >= 90
        ),
    }

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
        "data_maturity": data_maturity,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(snapshot, f, separators=(",", ":"))

    size_bytes = os.path.getsize(OUTPUT_PATH)
    print(f"snapshot.json written: {size_bytes} bytes ({size_bytes / 1024:.2f} KB)")
    if size_bytes > 500 * 1024:
        print("WARNING: snapshot exceeds 500KB target", file=sys.stderr)
        sys.exit(1)

    sample_squad = my_squad[0] if my_squad else None
    sample_watchlist = next(
        (p for p in watchlist if p["minutes_total"] is not None and p["minutes_total"] >= 90),
        watchlist[0] if watchlist else None,
    )
    sample_under90 = next(
        (p for p in all_watched_players if p["minutes_total"] is not None and p["minutes_total"] < 90),
        None,
    )
    print("\nSample squad player:")
    print(json.dumps(sample_squad, indent=2))
    print("\nSample watchlist player:")
    print(json.dumps(sample_watchlist, indent=2))
    print("\nSample under-90-minutes player:")
    print(json.dumps(sample_under90, indent=2))


if __name__ == "__main__":
    main()
