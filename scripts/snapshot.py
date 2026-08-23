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

# Rolling team-strength window, and where the pre-season/early-season prior comes from.
TEAM_STRENGTH_WINDOW = 6
# Fallback league-average anchors (roughly PL historical norms) used only when there's
# no rolling data at all yet (e.g. before gameweek 1 has finished) to convert FPL's
# strength ratings into pseudo-xG units.
FALLBACK_XG_ANCHOR = 1.3
FALLBACK_CLEAN_SHEET_ANCHOR = 0.28

# Bumping this forces a full cache refetch when the cached schema is missing fields
# a newer version of this script needs (e.g. the team-strength inputs added later).
CACHE_SCHEMA_VERSION = 2

# Fields kept per gameweek in the cache -- just enough to derive player stats and,
# aggregated across watched players' clubs, team-strength inputs.
HISTORY_FIELDS = (
    "round", "fixture", "was_home", "minutes", "starts",
    "expected_goals", "expected_assists", "expected_goals_conceded",
    "clean_sheets", "defensive_contribution", "bonus",
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
    """Average the last TEAM_STRENGTH_WINDOW matches (already filtered to one
    venue) into per-match rolling figures. None fields, not zero, when empty."""
    subset = sorted(matches, key=lambda m: m["round"], reverse=True)[:TEAM_STRENGTH_WINDOW]
    n = len(subset)
    xg_for_vals = [m["xg_for"] for m in subset]
    xg_against_vals = [m["xg_against"] for m in subset if m["xg_against"] is not None]
    cs_vals = [m["clean_sheet"] for m in subset if m["clean_sheet"] is not None]
    return {
        "xg_for_per_match": round(statistics.fmean(xg_for_vals), 3) if xg_for_vals else None,
        "xg_against_per_match": round(statistics.fmean(xg_against_vals), 3) if xg_against_vals else None,
        "clean_sheet_rate": round(statistics.fmean(cs_vals), 3) if cs_vals else None,
        "matches_in_window": n,
    }


def build_rolling_team_matches(elements, teams_by_id, histories):
    """Reconstruct one row per (club, fixture) from the watched players' own
    element-summary histories. expected_goals is summed across whichever of
    the club's players we have data for (so it under-counts for clubs with
    fewer watchlist/squad players -- an inherent limit of not fetching every
    player on every club); expected_goals_conceded and clean_sheets are
    team-level values reported identically by every player from that side in
    that match, so any single sample for a given (club, fixture) is enough."""
    match_log = {}
    for player_id, history in histories.items():
        team_short = teams_by_id[elements[player_id]["team"]]["short_name"]
        for gw in history:
            if not gw.get("minutes") or gw.get("fixture") is None:
                continue
            key = (team_short, gw["fixture"])
            entry = match_log.setdefault(
                key, {"round": gw["round"], "was_home": bool(gw.get("was_home")), "xg_for": 0.0,
                      "xg_against_samples": [], "clean_sheet_samples": []},
            )
            entry["xg_for"] += float(gw.get("expected_goals") or 0)
            if gw.get("expected_goals_conceded") is not None:
                entry["xg_against_samples"].append(float(gw["expected_goals_conceded"]))
            if gw.get("clean_sheets") is not None:
                entry["clean_sheet_samples"].append(gw["clean_sheets"])

    matches_by_club = defaultdict(list)
    for (team_short, _fixture_id), entry in match_log.items():
        matches_by_club[team_short].append(
            {
                "round": entry["round"],
                "was_home": entry["was_home"],
                "xg_for": round(entry["xg_for"], 3),
                "xg_against": round(statistics.fmean(entry["xg_against_samples"]), 3)
                if entry["xg_against_samples"] else None,
                "clean_sheet": (1 if any(entry["clean_sheet_samples"]) else 0)
                if entry["clean_sheet_samples"] else None,
            }
        )
    return matches_by_club


def compute_league_anchors(rolling_by_club):
    """Pseudo-xG scale to convert FPL's own (unitless) strength ratings into
    numbers comparable with our rolling per-match figures. Anchored to
    whatever rolling data actually exists across the league so far; falls
    back to fixed historical-average constants pre-season when there's none."""
    xg_for_pool, xg_against_pool, cs_pool = [], [], []
    for splits in rolling_by_club.values():
        for split in splits.values():
            if split["xg_for_per_match"] is not None:
                xg_for_pool.append(split["xg_for_per_match"])
            if split["xg_against_per_match"] is not None:
                xg_against_pool.append(split["xg_against_per_match"])
            if split["clean_sheet_rate"] is not None:
                cs_pool.append(split["clean_sheet_rate"])
    return {
        "xg_for_per_match": round(statistics.fmean(xg_for_pool), 3) if xg_for_pool else FALLBACK_XG_ANCHOR,
        "xg_against_per_match": round(statistics.fmean(xg_against_pool), 3) if xg_against_pool else FALLBACK_XG_ANCHOR,
        "clean_sheet_rate": round(statistics.fmean(cs_pool), 3) if cs_pool else FALLBACK_CLEAN_SHEET_ANCHOR,
    }


def relative_strength(team_rating, league_mean_rating):
    """team_rating / league_mean_rating, defined as 1.0 (average) if the
    league mean is 0 -- FPL occasionally reports every club's strength
    rating as 0 (e.g. before they're published for a new season)."""
    return team_rating / league_mean_rating if league_mean_rating else 1.0


def build_team_strength(elements, teams_by_id, histories):
    """Rolling last-6 (home/away split) xG-based team strength, blended with
    FPL's own strength ratings before there's enough rolling data (i.e.
    before gameweek 7). matches_in_window/6 is the rolling weight; whatever
    remains -- 1 full season prior to kickoff -- goes to the FPL-rating prior,
    so a club with zero observed matches falls back to the prior entirely."""
    all_clubs = list(teams_by_id.values())

    rolling_by_club = {}
    matches_by_club = build_rolling_team_matches(elements, teams_by_id, histories)
    for team in all_clubs:
        matches = matches_by_club.get(team["short_name"], [])
        rolling_by_club[team["short_name"]] = {
            "home": summarize_match_window([m for m in matches if m["was_home"]]),
            "away": summarize_match_window([m for m in matches if not m["was_home"]]),
        }

    anchors = compute_league_anchors(rolling_by_club)
    fpl_means = {
        "attack_home": statistics.fmean(t["strength_attack_home"] for t in all_clubs),
        "attack_away": statistics.fmean(t["strength_attack_away"] for t in all_clubs),
        "defence_home": statistics.fmean(t["strength_defence_home"] for t in all_clubs),
        "defence_away": statistics.fmean(t["strength_defence_away"] for t in all_clubs),
    }

    team_strength = {}
    for team in all_clubs:
        short = team["short_name"]
        club_result = {}
        for venue, attack_key, defence_key in (
            ("home", "strength_attack_home", "strength_defence_home"),
            ("away", "strength_attack_away", "strength_defence_away"),
        ):
            roll = rolling_by_club[short][venue]
            rolling_weight = min(roll["matches_in_window"] / TEAM_STRENGTH_WINDOW, 1.0)
            prior_weight = round(1 - rolling_weight, 3)

            # FPL sometimes reports these ratings as 0 for every club (e.g. before
            # they're published for a new season) -- fall back to "average" (1.0)
            # rather than dividing by zero when the league mean itself is 0.
            relative_attack = relative_strength(team[attack_key], fpl_means[f"attack_{venue}"])
            relative_defence = relative_strength(team[defence_key], fpl_means[f"defence_{venue}"])
            prior_xg_for = anchors["xg_for_per_match"] * relative_attack
            # Stronger defence (higher rating) -> concedes less -> divide, not multiply.
            prior_xg_against = anchors["xg_against_per_match"] / relative_defence if relative_defence else anchors["xg_against_per_match"]
            prior_clean_sheet_rate = min(anchors["clean_sheet_rate"] * relative_defence, 1.0)

            def blend(rolling_value, prior_value):
                observed = rolling_value if rolling_value is not None else prior_value
                return round(rolling_weight * observed + prior_weight * prior_value, 3)

            club_result[venue] = {
                "xg_for_per_match": blend(roll["xg_for_per_match"], prior_xg_for),
                "xg_against_per_match": blend(roll["xg_against_per_match"], prior_xg_against),
                "clean_sheet_rate": blend(roll["clean_sheet_rate"], prior_clean_sheet_rate),
                # Not exposed anywhere in the public FPL API (bootstrap-static and
                # element-summary have no "big chances" stat) -- always null rather
                # than fabricated, on both the rolling and the prior side.
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
    "MID": (("xg_against_per_match", -1.0), ("clean_sheet_rate", 3.0)),
    "FWD": (("xg_against_per_match", -1.0), ("clean_sheet_rate", 3.0)),
    "GKP": (("xg_for_per_match", 1.0), ("big_chances_conceded_per_match", 1.0)),
    "DEF": (("xg_for_per_match", 1.0), ("big_chances_conceded_per_match", 1.0)),
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


def attach_player_stats(records, elements, cache, latest_finished_gw, histories_out):
    for record in records:
        el = elements[record["player_id"]]
        history = fetch_player_history(record["player_id"], cache, latest_finished_gw)
        histories_out[record["player_id"]] = history
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
    histories = {}
    attach_player_stats(my_squad, elements, cache, latest_finished_gw, histories)
    attach_player_stats(watchlist, elements, cache, latest_finished_gw, histories)
    save_element_summary_cache(cache)

    watched_club_ids = {elements[p["player_id"]]["team"] for p in my_squad + watchlist}
    fixtures_next6 = build_fixtures_next6(fixtures, teams_by_id, watched_club_ids)

    team_strength = build_team_strength(elements, teams_by_id, histories)

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
