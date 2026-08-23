#!/usr/bin/env python3
"""Fetch a daily FPL data snapshot and write it to snapshot.json."""
import json
import os
import statistics
import sys
import time
from collections import defaultdict
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

# rank_score weights, applied to z-scores (fixture_score's z is negated: lower FDR-proxy is better).
RANK_WEIGHTS = {"xgi_per90": 3, "minutes_per_app": 2, "fixture_score": 2, "points_per_million": 1}

# A European tie this many calendar days or fewer before a league kickoff is flagged.
EUROPEAN_RECOVERY_THRESHOLD_DAYS = 4

# Beyond this gap a preceding European tie has no bearing on recovery, so report
# null rather than a technically-true but meaningless figure like 73 days.
EUROPEAN_MAX_LOOKBACK_DAYS = 14

# Rolling team-strength window, and the prior it blends with early in the season.
TEAM_STRENGTH_WINDOW = 6
# League-average anchors, in goals per team per match, used to scale FPL's
# (unitless) team strength ratings into the same units as the rolling figures.
# Fixed constants on purpose: deriving them from whatever data has arrived so
# far made every club's prior move whenever the observed sample changed.
LEAGUE_GOALS_PER_MATCH = {"home": 1.6, "away": 1.3}
LEAGUE_CLEAN_SHEET_RATE = {"home": 0.32, "away": 0.24}

# Bumping this forces a full cache refetch when the cached schema is missing fields
# a newer version of this script needs (e.g. the team-strength inputs added later).
CACHE_SCHEMA_VERSION = 3

# Fields kept per gameweek in the cache. Per-player stats only -- team metrics
# come from the fixture list, not from aggregating these.
HISTORY_FIELDS = (
    "round", "minutes", "starts",
    "expected_goals", "expected_assists", "defensive_contribution", "bonus",
)


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
        cache = json.load(f)
    if cache.get("_schema_version") != CACHE_SCHEMA_VERSION:
        return {}
    return cache


def save_element_summary_cache(cache):
    cache["_schema_version"] = CACHE_SCHEMA_VERSION
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
            # difficulty is FPL's own FDR, kept alongside our team-strength-based score
            # purely for comparison -- fixture_score below no longer derives from it.
            club_fixtures.append(
                {
                    "gw": f["event"],
                    "opponent": opp,
                    "is_home": is_home,
                    "difficulty": difficulty,
                    "kickoff_time": f.get("kickoff_time"),
                }
            )
            if len(club_fixtures) == 6:
                break
        fixtures_next6[club_short] = club_fixtures
    return fixtures_next6


def summarize_match_window(matches):
    """Average a club's last TEAM_STRENGTH_WINDOW matches at one venue into
    per-match figures. Each match is one team-level row from the fixture list,
    so this does not depend on which players we happen to be tracking."""
    subset = sorted(matches, key=lambda m: m["sort_key"], reverse=True)[:TEAM_STRENGTH_WINDOW]
    if not subset:
        return {"goals_for_per_match": None, "goals_against_per_match": None,
                "clean_sheet_rate": None, "matches_in_window": 0}
    return {
        "goals_for_per_match": round(statistics.fmean(m["goals_for"] for m in subset), 3),
        "goals_against_per_match": round(statistics.fmean(m["goals_against"] for m in subset), 3),
        "clean_sheet_rate": round(statistics.fmean(1 if m["goals_against"] == 0 else 0 for m in subset), 3),
        "matches_in_window": len(subset),
    }


def build_team_match_log(fixtures, teams_by_id):
    """One row per club per played match, straight from the fixture list.

    Uses team_h_score rather than the finished flag: FPL leaves finished False
    for a while after a match ends (it flips finished_provisional first), so
    keying off finished would discard real results. Nothing here touches player
    data, so a club's record is unaffected by who is on the watchlist."""
    matches_by_club = defaultdict(list)
    for f in fixtures:
        home_goals, away_goals = f.get("team_h_score"), f.get("team_a_score")
        if home_goals is None or away_goals is None:
            continue
        sort_key = (f.get("event") or 0, f.get("kickoff_time") or "")
        for club_id, is_home, scored, conceded in (
            (f["team_h"], True, home_goals, away_goals),
            (f["team_a"], False, away_goals, home_goals),
        ):
            club = teams_by_id[club_id]["short_name"]
            matches_by_club[club].append(
                {"sort_key": sort_key, "was_home": is_home,
                 "goals_for": scored, "goals_against": conceded}
            )
    return matches_by_club


def relative_strength(team_rating, league_mean_rating):
    """team_rating / league_mean_rating, defined as 1.0 (average) when the
    league mean is 0, so a season with unpublished ratings degrades to
    "everyone average" instead of dividing by zero."""
    return team_rating / league_mean_rating if league_mean_rating else 1.0


def build_team_strength(fixtures, teams_by_id):
    """Team-level strength: rolling last-6 home/away form from actual match
    results, blended with a prior from FPL's own team strength ratings.

    Deliberately takes no player data. An earlier version summed
    element-summary xG across the squad+watchlist sample, which made a club's
    rating depend on how many of its players were being tracked -- swapping one
    watchlist player moved every club's numbers, including clubs with no
    tracked players at all.

    The league anchors are fixed constants rather than averages of the
    observed sample, so the prior cannot drift with whatever data happens to
    have arrived. Goals, not xG: no free team-level xG source was reachable
    (understat serves a stub, fbref is Cloudflare-blocked, football-data.co.uk
    has no 26/27 CSV), and the fields are named for what they actually hold."""
    all_clubs = list(teams_by_id.values())
    matches_by_club = build_team_match_log(fixtures, teams_by_id)

    rolling_by_club = {
        team["short_name"]: {
            "home": summarize_match_window(
                [m for m in matches_by_club.get(team["short_name"], []) if m["was_home"]]),
            "away": summarize_match_window(
                [m for m in matches_by_club.get(team["short_name"], []) if not m["was_home"]]),
        }
        for team in all_clubs
    }

    # strength_attack_* and strength_defence_* are 0 for every club in this
    # season's payload; strength_overall_home/away are the populated ones.
    # They conflate attack and defence, so the same rating scales both sides of
    # the prior -- coarse, but real team-level signal rather than none.
    fpl_means = {
        venue: statistics.fmean(t[f"strength_overall_{venue}"] or 0 for t in all_clubs)
        for venue in ("home", "away")
    }

    team_strength = {}
    for team in all_clubs:
        short = team["short_name"]
        club_result = {}
        for venue in ("home", "away"):
            roll = rolling_by_club[short][venue]
            rolling_weight = min(roll["matches_in_window"] / TEAM_STRENGTH_WINDOW, 1.0)
            prior_weight = round(1 - rolling_weight, 3)

            strength = relative_strength(team[f"strength_overall_{venue}"] or 0, fpl_means[venue])
            anchor_goals = LEAGUE_GOALS_PER_MATCH[venue]
            anchor_clean_sheet = LEAGUE_CLEAN_SHEET_RATE[venue]
            prior_goals_for = anchor_goals * strength
            # A stronger side concedes fewer and keeps more clean sheets.
            prior_goals_against = anchor_goals / strength if strength else anchor_goals
            prior_clean_sheet_rate = min(anchor_clean_sheet * strength, 1.0)

            def blend(rolling_value, prior_value):
                observed = rolling_value if rolling_value is not None else prior_value
                return round(rolling_weight * observed + prior_weight * prior_value, 3)

            club_result[venue] = {
                "goals_for_per_match": blend(roll["goals_for_per_match"], prior_goals_for),
                "goals_against_per_match": blend(roll["goals_against_per_match"], prior_goals_against),
                "clean_sheet_rate": blend(roll["clean_sheet_rate"], prior_clean_sheet_rate),
                # No free team-level source exposes big chances; null, not invented.
                "big_chances_conceded_per_match": None,
                "matches_in_window": roll["matches_in_window"],
                "prior_weight": prior_weight,
            }
        team_strength[short] = club_result
    return team_strength


# Per position group, which opponent team_strength fields drive fixture difficulty,
# and in which direction. Null components (always true for big_chances_conceded_per_match,
# since the API doesn't expose it) are skipped rather than zeroed.
FIXTURE_DIFFICULTY_COMPONENTS = {
    "MID": (("goals_against_per_match", -1.0), ("clean_sheet_rate", 3.0)),
    "FWD": (("goals_against_per_match", -1.0), ("clean_sheet_rate", 3.0)),
    "GKP": (("goals_for_per_match", 1.0), ("big_chances_conceded_per_match", 1.0)),
    "DEF": (("goals_for_per_match", 1.0), ("big_chances_conceded_per_match", 1.0)),
}


def fixture_difficulty(pos, opponent_stats):
    if not opponent_stats:
        return None
    components = FIXTURE_DIFFICULTY_COMPONENTS[pos]
    terms = [weight * opponent_stats[name] for name, weight in components if opponent_stats.get(name) is not None]
    return round(sum(terms), 3) if terms else None


def compute_fixture_score(club_fixtures, count, pos, team_strength):
    subset = club_fixtures[:count]
    total = 0.0
    counted = 0
    for f in subset:
        # Facing the opponent at the venue where they'll actually be playing:
        # if we're home, they're away, and vice versa.
        opponent_venue = "away" if f["is_home"] else "home"
        opponent_stats = team_strength.get(f["opponent"], {}).get(opponent_venue)
        difficulty = fixture_difficulty(pos, opponent_stats)
        if difficulty is not None:
            total += difficulty
            counted += 1
    return round(total, 3) if counted else None


def attach_fixture_scores(records, fixtures_next6, team_strength):
    for record in records:
        club_fixtures = fixtures_next6.get(record["club"], [])
        record["fixture_score"] = compute_fixture_score(club_fixtures, 6, record["pos"], team_strength)
        record["fdr_next3"] = compute_fixture_score(club_fixtures, 3, record["pos"], team_strength)


def compute_pool_stats(pool, component_names):
    """Mean/population-stdev per rank component across the full ranking pool
    (squad + watchlist), ignoring nulls. std 0 -> every z-score for that
    component is defined as 0 (compute_rank handles the division)."""
    stats = {}
    for name in component_names:
        values = [p[name] for p in pool if p.get(name) is not None]
        mean_ = round(statistics.fmean(values), 4) if values else 0.0
        std_ = round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0
        stats[name] = {"mean": mean_, "std": std_, "n": len(values)}
    return stats


def zscore(value, stats):
    if value is None:
        return None
    if stats["std"] == 0:
        return 0.0
    return (value - stats["mean"]) / stats["std"]


def compute_rank(player, pool_stats):
    """z-scored, null-safe weighted composite. Each present component is
    converted to a z-score against the squad+watchlist pool before weighting,
    so components on wildly different scales (fixture_score's raw xG units vs.
    points_per_million) contribute comparably. Missing components are excluded
    from both the numerator and the weight total, rather than zeroed, so a
    player scored on 3 of 4 components isn't penalized against one scored on
    all 4 -- rank_score is always effectively a weighted *average* of the
    z-scores actually available."""
    z = {
        "xgi_per90": zscore(player.get("xgi_per90"), pool_stats["xgi_per90"]),
        "minutes_per_app": zscore(player.get("minutes_per_app"), pool_stats["minutes_per_app"]),
        "points_per_million": zscore(player.get("points_per_million"), pool_stats["points_per_million"]),
    }
    fixture_z = zscore(player.get("fixture_score"), pool_stats["fixture_score"])
    z["fixture_score"] = -fixture_z if fixture_z is not None else None  # lower fixture_score is better

    rank_basis = [name for name in RANK_WEIGHTS if z[name] is not None]
    rank_components = {name: (round(z[name], 4) if z[name] is not None else None) for name in RANK_WEIGHTS}
    weights_used = {name: RANK_WEIGHTS[name] for name in rank_basis}

    if not rank_basis:
        return None, [], rank_components, {}

    weight_total = sum(weights_used.values())
    rank_score = round(sum(RANK_WEIGHTS[name] * z[name] for name in rank_basis) / weight_total, 4)
    return rank_score, rank_basis, rank_components, weights_used


def rank_and_sort_watchlist(watchlist, pool):
    pool_stats = compute_pool_stats(pool, RANK_WEIGHTS.keys())
    for player in watchlist:
        rank_score, rank_basis, rank_components, weights_used = compute_rank(player, pool_stats)
        player["rank_score"] = rank_score
        player["rank_basis"] = rank_basis
        player["rank_components"] = rank_components
        player["weights_used"] = weights_used
    watchlist.sort(key=lambda p: (p["rank_score"] is None, -(p["rank_score"] or 0)))
    return pool_stats


def parse_kickoff_date(value):
    """Calendar date from a date string or an ISO timestamp; None if unusable
    (FPL leaves kickoff_time null for fixtures it hasn't scheduled yet)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def load_european_fixtures(config):
    """club -> European dates as (date, competition, basis), ascending.

    The FPL API is Premier League only, so European dates come from config. Two
    sources, in priority order per club:

    1. european_fixtures -- exact kickoffs, once a draw has assigned them.
    2. european_matchdays -- the published league-phase matchday windows, which
       are fixed long before the draws. A club plays exactly one date per
       window, but which one is unknown until the draw, so every date in the
       window is treated as a candidate. Nearest-preceding selection then lands
       on the window's last date, making recovery_days the worst case (the least
       rest the club could have had) rather than an invented exact figure.
    """
    european_clubs = config.get("european_clubs", {})
    matchdays = config.get("european_matchdays", {})
    exits = {club: parse_kickoff_date(value) for club, value in config.get("european_exit", {}).items()}

    explicit = defaultdict(list)
    for entry in config.get("european_fixtures", []):
        club = entry.get("club")
        kickoff_date = parse_kickoff_date(entry.get("kickoff"))
        if not club or kickoff_date is None:
            continue
        competition = entry.get("competition") or european_clubs.get(club)
        explicit[club].append((kickoff_date, competition, "exact_kickoff"))

    by_club = {}
    for club, competition in european_clubs.items():
        if explicit.get(club):
            by_club[club] = explicit[club]
            continue
        by_club[club] = [
            (date, competition, "matchday_window")
            for window in matchdays.get(competition, [])
            for date in (parse_kickoff_date(d) for d in window)
            if date is not None
        ]
    # A club given exact fixtures but missing from european_clubs still counts.
    for club, dates in explicit.items():
        by_club.setdefault(club, dates)

    # Once a club is out, later windows are not theirs to be flagged against.
    for club, exit_date in exits.items():
        if exit_date and club in by_club:
            by_club[club] = [item for item in by_club[club] if item[0] <= exit_date]

    for club_dates in by_club.values():
        club_dates.sort(key=lambda item: item[0])
    return by_club


def annotate_european_context(fixtures_next6, european_by_club):
    """Tag each Premier League fixture with the club's own preceding European
    commitment. Informational only -- deliberately applied after scoring and
    ranking, and read by nothing downstream.

    recovery_days counts calendar days from the European date to this league
    kickoff (a Thursday UEL tie before a Sunday league game is 3, before a
    Saturday one is 2), which is why this is computed from dates rather than
    from a "played midweek" day-of-week flag.

    recovery_days_basis says how precise the figure is: exact_kickoff means it
    came from a known kickoff, matchday_window means the draw has not assigned
    the club a date yet and this is the worst case within the window."""
    for club, club_fixtures in fixtures_next6.items():
        european_dates = european_by_club.get(club, [])
        for fixture in club_fixtures:
            pl_date = parse_kickoff_date(fixture.get("kickoff_time"))
            recovery_days = None
            competition = None
            basis = None
            if pl_date is not None:
                preceding = [item for item in european_dates if item[0] <= pl_date]
                if preceding:
                    european_date, european_competition, candidate_basis = preceding[-1]
                    gap = (pl_date - european_date).days
                    if gap <= EUROPEAN_MAX_LOOKBACK_DAYS:
                        recovery_days, basis = gap, candidate_basis
                        if gap <= EUROPEAN_RECOVERY_THRESHOLD_DAYS:
                            competition = european_competition
            fixture["european_fixture_within_4_days"] = (
                recovery_days is not None and recovery_days <= EUROPEAN_RECOVERY_THRESHOLD_DAYS
            )
            fixture["competition"] = competition
            fixture["recovery_days"] = recovery_days
            fixture["recovery_days_basis"] = basis


def warn_european_calendar_stale(european_clubs, european_by_club, fixtures_next6):
    """The European calendar is hand-maintained, so it can silently fall behind
    the season. Complain on stderr when a club has no dates at all, or when its
    calendar ends before league fixtures we are already reporting on -- that is
    the state in which recovery_days quietly goes null for midweek-laden weeks."""
    undated = sorted(club for club in european_clubs if not european_by_club.get(club))
    if undated:
        print(
            "NOTE: no European dates for " + ", ".join(undated)
            + " -- add their competition to european_matchdays, or the clubs to european_fixtures.",
            file=sys.stderr,
        )

    exhausted = []
    for club in sorted(european_clubs):
        dates = european_by_club.get(club)
        if not dates:
            continue
        last_european = dates[-1][0]
        pl_dates = [parse_kickoff_date(f.get("kickoff_time")) for f in fixtures_next6.get(club, [])]
        pl_dates = [d for d in pl_dates if d is not None]
        if pl_dates and max(pl_dates) > last_european:
            exhausted.append(f"{club} (calendar ends {last_european}, fixtures run to {max(pl_dates)})")
    if exhausted:
        print(
            "NOTE: European calendar is exhausted for " + "; ".join(exhausted)
            + " -- recovery_days will read null for those fixtures until it is extended.",
            file=sys.stderr,
        )


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
        "fixture_score": None, "fdr_next3": None,
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

    team_strength = build_team_strength(fixtures, teams_by_id)

    attach_fixture_scores(my_squad, fixtures_next6, team_strength)
    attach_fixture_scores(watchlist, fixtures_next6, team_strength)
    pool_stats = rank_and_sort_watchlist(watchlist, my_squad + watchlist)

    # Informational only, and applied after every score above is already final.
    european_clubs = config.get("european_clubs", {})
    european_by_club = load_european_fixtures(config)
    annotate_european_context(fixtures_next6, european_by_club)
    warn_european_calendar_stale(european_clubs, european_by_club, fixtures_next6)

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
        "watchlist_rank_stats": pool_stats,
        "fixtures_next6": fixtures_next6,
        "european_clubs": european_clubs,
        "team_strength": team_strength,
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

    print("\nz-score mean/std used (across squad + watchlist):")
    print(json.dumps(pool_stats, indent=2))
    print("\nTop 10 watchlist players by rank_score:")
    for p in watchlist[:10]:
        print(json.dumps(
            {k: p[k] for k in (
                "name", "club", "pos", "rank_score", "rank_basis", "weights_used",
                "rank_components", "xgi_per90", "minutes_per_app", "fixture_score", "points_per_million",
            )},
            indent=2,
        ))


if __name__ == "__main__":
    main()
