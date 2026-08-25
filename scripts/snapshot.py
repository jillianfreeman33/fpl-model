#!/usr/bin/env python3
"""Fetch a daily FPL data snapshot and write it to snapshot.json."""
import csv
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
DEADLINE_STATE_DIR = os.path.join(ROOT, "data", "deadline_state")
CALIBRATION_PATH = os.path.join(ROOT, "calibration.csv")

CHIPS_THAT_SKIP_TRANSFER_DEDUCTION = {"wildcard", "freehit"}
ELEMENT_SUMMARY_SLEEP_SECONDS = 0.2

# DefCon 2pt threshold: 10 CBIT for defenders/keepers, 12 CBIRT for mids/forwards.
DEFCON_THRESHOLD_BY_POS = {"GKP": 10, "DEF": 10, "MID": 12, "FWD": 12}

# rank_score weights, applied to component z-scores. Every quantity the snapshot
# derives feeds the model through exactly one of these, so nothing is counted
# twice: xg_per90 + xa_per90 are exactly xgi_per90; price enters via
# points_per_million; apps enters via starts_rate and bonus_per90. selling_price
# is a verbatim copy of price and carries no independent signal.
RANK_WEIGHTS = {
    "xgi_per90": 3.0,          # attacking output (xG + xA per 90)
    "minutes_per_app": 2.0,    # how long they last when they play
    "starts_rate": 1.5,        # started vs came off the bench
    "fixture_score": 2.0,      # opponent difficulty, next 6
    "fdr_next3": 1.0,          # opponent difficulty, next 3 (transfer horizon)
    "fdr_next1": 0.5,          # opponent difficulty, the very next game
    "points_per_million": 1.0, # value for money
    "form": 1.5,               # FPL's own recent-points form
    "defcon_per90": 1.0,       # defensive contribution volume
    "defcon_hit_rate": 0.5,    # share of appearances hitting the DefCon threshold
    "bonus_per90": 1.0,        # bonus point accrual
    "net_transfers": 0.5,      # market momentum / price-change pressure
    "rival_ownership": 0.5,    # how many of your league rivals already own them
    "ownership": 0.5,          # inverted below: tilt toward differentials
}

# Availability is deliberately NOT a z-scored component. Almost every player sits
# at 1.0, so the standard deviation is tiny and z-scoring turned a 50% doubt into
# -7 standard deviations -- enough to make one flag outrank every other signal
# combined. It is applied instead as a bounded, interpretable penalty in score
# space: a player half likely to feature loses half this many z-units. Players at
# 0.0 (injured, suspended, unavailable, not in squad) are excluded outright.
AVAILABILITY_PENALTY = 1.0

# Components whose scale differs by position for reasons unrelated to quality,
# so they are z-scored WITHIN position. fixture_score/fdr_next3 read opposite
# sides of the opponent (attack for GKP/DEF, defence for MID/FWD) and land in
# disjoint ranges; DefCon thresholds differ by position by rule.
POSITION_RELATIVE_COMPONENTS = {"fixture_score", "fdr_next3", "fdr_next1",
                                "defcon_per90", "defcon_hit_rate"}

# Components where a lower raw value is better, so their z is negated. Kept
# separate from the weights so every weight stays positive -- a negative weight
# would corrupt both the denominator and the coverage fraction.
LOWER_IS_BETTER = {"fixture_score", "fdr_next3", "fdr_next1", "ownership"}

# Refuse to rank a player carrying less than this share of the total weight in
# actual data -- below it the score says more about what is missing than about
# the player.
RANK_MIN_COVERAGE = 0.45

# Which of the ~700 players get considered at all. Everything here comes from
# bootstrap-static plus the fixture list, so the whole pool can be scored before
# a single element-summary call is made.
#
# Ownership is deliberately absent. It is a scoring component with an inverted
# sign (differential tilt), so selecting the most-owned players and then
# rewarding low ownership inside that pool rewards the least-owned of the
# most-owned -- incoherent, and the cause of the template bias. Ownership still
# feeds rank_score; it just no longer decides who is eligible.
PRESELECT_WEIGHTS = {
    "xgi_per90": 2.0,               # attacking output, from bootstrap season totals
    "form": 1.5,                    # FPL's own recent-points form
    "points_per_million": 1.5,      # value
    "minutes": 1.5,                 # is this player actually playing
    "fixture_ease_next3": 1.5,      # transfer horizon
    "fixture_ease_next1": 0.5,      # the very next game
}
# Fixture ease differs in scale by position (GKP/DEF read the opponent's attack,
# MID/FWD their defence), so it is z-scored within position, as in rank_score.
PRESELECT_POSITION_RELATIVE = {"fixture_ease_next3", "fixture_ease_next1"}
# Never consider a player who cannot play, or who has not played at all -- 17 of
# 60 slots were going to such players under the old ownership sort.
PRESELECT_REQUIRE_MINUTES = True

# A European tie this many calendar days or fewer before a league kickoff is flagged.
EUROPEAN_RECOVERY_THRESHOLD_DAYS = 4

# Beyond this gap a preceding European tie has no bearing on recovery, so report
# null rather than a technically-true but meaningless figure like 73 days.
EUROPEAN_MAX_LOOKBACK_DAYS = 14

# Rolling team-strength window, and the prior it blends with early in the season.
TEAM_STRENGTH_WINDOW = 6
# League anchors are MEASURED from this season's played fixtures, never guessed.
# They are computed league-wide (every club, both venues), so they depend on real
# results only -- not on which players are being tracked. With few matches played
# they are noisy but honest; measured_from_matches is reported in the snapshot.

# Bumping this forces a full cache refetch when the cached schema is missing fields
# a newer version of this script needs (e.g. the team-strength inputs added later).
CACHE_SCHEMA_VERSION = 4

# Fields kept per gameweek in the cache. Per-player stats only -- team metrics
# come from the fixture list, not from aggregating these.
HISTORY_FIELDS = (
    "round", "minutes", "starts", "total_points",
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
        # Market momentum, previously computed for the squad only (price_watch).
        "transfers_in_event": el.get("transfers_in_event", 0),
        "transfers_out_event": el.get("transfers_out_event", 0),
        "net_transfers": el.get("transfers_in_event", 0) - el.get("transfers_out_event", 0),
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


def measure_league_anchors(matches_by_club):
    """League-average goals and clean sheet rate per venue, MEASURED from every
    played match this season rather than asserted as constants. Returns None
    per venue when nothing has been played there yet -- pre-season we genuinely
    do not know, and inventing a number is what this replaces."""
    anchors = {}
    total = 0
    for venue, want_home in (("home", True), ("away", False)):
        rows = [m for rows_ in matches_by_club.values() for m in rows_ if m["was_home"] == want_home]
        total += len(rows)
        anchors[venue] = None if not rows else {
            "goals_per_match": round(statistics.fmean(m["goals_for"] for m in rows), 4),
            "clean_sheet_rate": round(statistics.fmean(1 if m["goals_against"] == 0 else 0 for m in rows), 4),
            "matches": len(rows),
        }
    anchors["measured_from_matches"] = total
    return anchors


def build_team_xg(elements, teams_by_id, matches_by_club):
    """Real team xG for and against, per club per match.

    FPL publishes no team-level xG endpoint, so this sums bootstrap-static's
    season xG totals over EVERY player at the club -- the complete population,
    not a sample, which makes the sum arithmetically the team total. That is the
    distinction from the version this replaces, which summed a 75-player
    watchlist sample and therefore measured squad coverage as much as team
    quality.

    xG against is published per player as the xG their team faced while they
    were on the pitch, so summing it across a squad counts each team-minute
    about eleven times over; dividing by the squad's own 90s recovers the team
    rate exactly, without assuming eleven outfield players.

    Returns None per club when the club has no xG fields or no played matches,
    so callers fall back to goals rather than to a fabricated figure."""
    totals = defaultdict(lambda: {"xg": 0.0, "xgc": 0.0, "minutes": 0, "has_fields": False})
    for el in elements.values():
        club = teams_by_id[el["team"]]["short_name"]
        row = totals[club]
        if el.get("expected_goals") is not None:
            row["has_fields"] = True
            row["xg"] += float(el.get("expected_goals") or 0)
        if el.get("expected_goals_conceded") is not None:
            row["xgc"] += float(el.get("expected_goals_conceded") or 0)
        row["minutes"] += el.get("minutes") or 0

    team_xg = {}
    for club, row in totals.items():
        played = len(matches_by_club.get(club, []))
        nineties = row["minutes"] / 90
        if not row["has_fields"] or played == 0 or nineties == 0:
            team_xg[club] = None
            continue
        team_xg[club] = {
            "xg_for_per_match": round(row["xg"] / played, 3),
            "xg_against_per_match": round(row["xgc"] / nineties, 3),
        }
    return team_xg


def relative_strength(team_rating, league_mean_rating):
    """team_rating / league_mean_rating, defined as 1.0 (average) when the
    league mean is 0, so a season with unpublished ratings degrades to
    "everyone average" instead of dividing by zero."""
    return team_rating / league_mean_rating if league_mean_rating else 1.0


def build_team_strength(fixtures, teams_by_id, elements):
    """Team-level strength: rolling last-6 home/away form blended with a prior
    from FPL's own team strength ratings.

    Takes no player *sample*. Rolling form comes from the fixture list; xG comes
    from summing every player at a club (the full population -- see
    build_team_xg), so no club's rating depends on who is on the watchlist.

    Attacking and defensive quality are reported as both xG and goals. xG is
    the better predictor and is used by fixture_score when available; goals are
    kept alongside so the two can be compared, and are the fallback when FPL
    publishes no xG."""
    all_clubs = list(teams_by_id.values())
    matches_by_club = build_team_match_log(fixtures, teams_by_id)
    anchors = measure_league_anchors(matches_by_club)
    team_xg = build_team_xg(elements, teams_by_id, matches_by_club)

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
    # season's payload; strength_overall_home/away are the populated ones. They
    # conflate attack and defence, so the same rating scales both sides of the
    # prior -- coarse, but real team-level signal rather than none.
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
            anchor = anchors[venue]

            if anchor is None:
                # Nothing played at this venue league-wide: no measured scale exists.
                club_result[venue] = {
                    "goals_for_per_match": None, "goals_against_per_match": None,
                    "clean_sheet_rate": None, "xg_for_per_match": None,
                    "xg_against_per_match": None, "big_chances_conceded_per_match": None,
                    "matches_in_window": roll["matches_in_window"], "prior_weight": prior_weight,
                }
                continue

            prior_goals_for = anchor["goals_per_match"] * strength
            prior_goals_against = anchor["goals_per_match"] / strength if strength else anchor["goals_per_match"]
            prior_clean_sheet = min(anchor["clean_sheet_rate"] * strength, 1.0)

            def blend(rolling_value, prior_value):
                observed = rolling_value if rolling_value is not None else prior_value
                return round(rolling_weight * observed + prior_weight * prior_value, 3)

            xg = team_xg.get(short)
            # xG goes through the SAME prior blend as goals. Left raw it bypassed
            # the shrinkage entirely: one match of data produced a 0.21-3.91 league
            # spread, and because FPL's season xG is not split by venue, a club
            # with no home matches yet showed its away xG under the home key while
            # goals correctly showed the prior. Blending fixes both -- the season
            # figure is the observation, shrunk toward each venue's own prior.
            club_result[venue] = {
                "goals_for_per_match": blend(roll["goals_for_per_match"], prior_goals_for),
                "goals_against_per_match": blend(roll["goals_against_per_match"], prior_goals_against),
                "clean_sheet_rate": blend(roll["clean_sheet_rate"], prior_clean_sheet),
                "xg_for_per_match": blend(xg["xg_for_per_match"], prior_goals_for) if xg else None,
                "xg_against_per_match": blend(xg["xg_against_per_match"], prior_goals_against) if xg else None,
                "big_chances_conceded_per_match": None,
                "matches_in_window": roll["matches_in_window"],
                "prior_weight": prior_weight,
            }
        team_strength[short] = club_result
    team_strength["_league_anchors"] = anchors
    team_strength["_xg_available"] = any(v is not None for v in team_xg.values())
    return team_strength


# Per position group, which opponent fields drive fixture difficulty and in which
# direction. xG variants are preferred when FPL publishes them and fall back to
# goals otherwise -- fixture_difficulty tries each pair in order.
FIXTURE_DIFFICULTY_COMPONENTS = {
    "MID": ((("xg_against_per_match", "goals_against_per_match"), -1.0),
            (("clean_sheet_rate", None), 3.0)),
    "FWD": ((("xg_against_per_match", "goals_against_per_match"), -1.0),
            (("clean_sheet_rate", None), 3.0)),
    "GKP": ((("xg_for_per_match", "goals_for_per_match"), 1.0),
            (("big_chances_conceded_per_match", None), 1.0)),
    "DEF": ((("xg_for_per_match", "goals_for_per_match"), 1.0),
            (("big_chances_conceded_per_match", None), 1.0)),
}


def fixture_difficulty(pos, opponent_stats):
    """Opponent difficulty for one fixture, from that opponent's strength at the
    venue they will be playing at.

    The absolute scale differs by position group -- GKP/DEF read the opponent's
    attack, MID/FWD read their defence -- so these numbers are only ever
    comparable within a position. compute_rank z-scores them per position for
    exactly that reason."""
    if not opponent_stats:
        return None
    terms = []
    for names, weight in FIXTURE_DIFFICULTY_COMPONENTS[pos]:
        for name in (n for n in names if n):
            value = opponent_stats.get(name)
            if value is not None:
                terms.append(weight * value)
                break
    return round(sum(terms), 3) if terms else None


def compute_fixture_score(club_fixtures, count, pos, team_strength):
    """Mean opponent difficulty over a club's next `count` fixtures.

    A mean rather than a sum: with a blank gameweek a club has fewer upcoming
    fixtures, and summing would score it as though its remaining games were
    easier. Averaging compares like with like. (Where every club has the full
    count the two are a monotonic transform of each other, so the z-scores are
    unchanged.)"""
    total, counted = 0.0, 0
    for f in club_fixtures[:count]:
        # Face the opponent at the venue they will actually be playing:
        # if we are home, they are away, and vice versa.
        opponent_venue = "away" if f["is_home"] else "home"
        difficulty = fixture_difficulty(pos, team_strength.get(f["opponent"], {}).get(opponent_venue))
        if difficulty is not None:
            total += difficulty
            counted += 1
    return round(total / counted, 3) if counted else None


def attach_fixture_scores(records, fixtures_next6, team_strength):
    for record in records:
        club_fixtures = fixtures_next6.get(record["club"], [])
        for field, horizon in (("fixture_score", 6), ("fdr_next3", 3), ("fdr_next1", 1)):
            record[field] = compute_fixture_score(club_fixtures, horizon, record["pos"], team_strength)


def bootstrap_xgi_per90(el):
    """xGI per 90 straight from bootstrap season totals.

    Available for every player in the league without an element-summary call,
    which is what makes merit-based preselection possible at all. Guarded at 90
    minutes for the same reason the per-player stats are: below that it is a
    rate extrapolated from almost nothing."""
    minutes = el.get("minutes") or 0
    if minutes < 90:
        return None
    xg = el.get("expected_goals")
    xa = el.get("expected_assists")
    if xg is None and xa is None:
        return None
    total = float(xg or 0) + float(xa or 0)
    return round(total / (minutes / 90), 3)


def build_watchlist(elements, teams_by_id, types_by_id, my_ids, empty_stats,
                    fixtures_next6, team_strength, watchlist_size):
    """Choose the candidate pool on merit rather than popularity.

    The previous version sorted every unowned player by ownership descending and
    kept the top 60, with form as a tiebreak that only bit when two players
    shared an ownership figure. That made the 14-component model a ranker of a
    pool chosen by popularity alone: it selected 25 defenders to 8 forwards by
    accident, 11 players from one club, three who were unavailable and fourteen
    with zero minutes -- and it capped visibility at 4.3% ownership, so an
    equally good player one decimal lower was never seen.

    Selection now scores the whole league on bootstrap-derived merit plus
    fixture ease, after dropping players who cannot play or have not played."""
    eligible, rejected = [], {"unavailable": 0, "no_minutes": 0, "owned": 0}
    for pid, el in elements.items():
        if pid in my_ids:
            rejected["owned"] += 1
            continue
        record = player_record(el, teams_by_id, types_by_id, empty_stats)
        if availability(record) == 0.0:
            rejected["unavailable"] += 1
            continue
        if PRESELECT_REQUIRE_MINUTES and not (el.get("minutes") or 0):
            rejected["no_minutes"] += 1
            continue
        club_fixtures = fixtures_next6.get(record["club"], [])
        eligible.append((record, {
            "xgi_per90": bootstrap_xgi_per90(el),
            "form": record["form"],
            "points_per_million": record["points_per_million"],
            "minutes": el.get("minutes") or 0,
            # Negated: a low difficulty number is an easy fixture, and every
            # preselect component is "higher is better".
            "fixture_ease_next3": _negate(compute_fixture_score(club_fixtures, 3, record["pos"], team_strength)),
            "fixture_ease_next1": _negate(compute_fixture_score(club_fixtures, 1, record["pos"], team_strength)),
            "pos": record["pos"],
        }))

    inputs = [values for _, values in eligible]
    stats = compute_pool_stats(inputs, PRESELECT_WEIGHTS.keys(), PRESELECT_POSITION_RELATIVE)
    total_weight = sum(PRESELECT_WEIGHTS.values())
    for record, values in eligible:
        contributions = {}
        for name, weight in PRESELECT_WEIGHTS.items():
            z = zscore(values[name], lookup_stats(stats, name, values["pos"], PRESELECT_POSITION_RELATIVE))
            contributions[name] = None if z is None else round(z, 4)
        # Missing components count as pool-average, the same shrinkage rank_score uses.
        record["preselect_score"] = round(
            sum(PRESELECT_WEIGHTS[n] * (contributions[n] or 0.0) for n in PRESELECT_WEIGHTS) / total_weight, 4)
        record["preselect_components"] = contributions

    eligible.sort(key=lambda pair: -pair[0]["preselect_score"])
    watchlist = [record for record, _ in eligible[:watchlist_size]]
    selection = {
        "method": "merit_preselect",
        "weights": PRESELECT_WEIGHTS,
        "requires_minutes": PRESELECT_REQUIRE_MINUTES,
        "considered": len(eligible),
        "selected": len(watchlist),
        "rejected": rejected,
        "cutoff_preselect_score": watchlist[-1]["preselect_score"] if watchlist else None,
    }
    return watchlist, selection


def _negate(value):
    return None if value is None else -value


def compute_pool_stats(pool, component_names, position_relative):
    """Mean/population-stdev per component across the ranking pool.

    Components in position_relative get their own stats per position group.
    Those are quantities whose scale differs by position for reasons that have
    nothing to do with player quality -- fixture_score reads the opponent's
    attack for GKP/DEF and their defence for MID/FWD, producing disjoint ranges,
    and DefCon thresholds differ by position. Pooling them made the z-score
    encode position rather than merit. Components measuring genuinely comparable
    things (attacking output, minutes, value) stay pooled, so a forward's real
    scoring advantage over a defender still counts."""
    stats = {}
    for name in component_names:
        if name in position_relative:
            for pos in {p["pos"] for p in pool}:
                subset = [p[name] for p in pool if p["pos"] == pos and p.get(name) is not None]
                stats[(name, pos)] = _mean_std(subset)
        else:
            stats[name] = _mean_std([p[name] for p in pool if p.get(name) is not None])
    return stats


def _mean_std(values):
    return {
        "mean": round(statistics.fmean(values), 4) if values else 0.0,
        "std": round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0,
        "n": len(values),
    }


def lookup_stats(pool_stats, name, pos, position_relative):
    key = (name, pos) if name in position_relative else name
    return pool_stats.get(key) or {"mean": 0.0, "std": 0.0, "n": 0}


def zscore(value, stats):
    if value is None:
        return None
    if stats["std"] == 0:
        return 0.0
    return (value - stats["mean"]) / stats["std"]


def availability(player):
    """0.0 unavailable, 1.0 fully fit, fractional for doubtful.

    FPL status codes: a available, d doubtful, i injured, s suspended,
    u unavailable, n not in squad. chance_of_playing_next_round refines it."""
    status = player.get("status")
    chance = player.get("chance_of_playing")
    if status in ("i", "s", "u", "n"):
        return 0.0
    if chance is not None:
        return max(0.0, min(chance / 100.0, 1.0))
    return 0.5 if status == "d" else 1.0


def derive_rank_inputs(player):
    """The raw quantities rank_score is built from, one per component.

    Several are derived rather than raw so nothing is double-counted:
    xg_per90 and xa_per90 sum exactly to xgi_per90 and so feed the model
    through it; price feeds through points_per_million; apps feeds through
    starts_rate and bonus_per90. selling_price is a verbatim copy of price and
    carries no independent signal."""
    apps = player.get("apps") or 0
    minutes_total = player.get("minutes_total") or 0
    nineties = minutes_total / 90 if minutes_total else 0
    net = player.get("net_transfers")
    return {
        "xgi_per90": player.get("xgi_per90"),
        "minutes_per_app": player.get("minutes_per_app"),
        "starts_rate": round(player["starts"] / apps, 3) if apps and player.get("starts") is not None else None,
        "fixture_score": player.get("fixture_score"),
        "fdr_next3": player.get("fdr_next3"),
        "fdr_next1": player.get("fdr_next1"),
        "points_per_million": player.get("points_per_million"),
        "form": player.get("form"),
        "defcon_per90": player.get("defcon_per90"),
        "defcon_hit_rate": player.get("defcon_hit_rate"),
        "bonus_per90": round(player["bonus_total"] / nineties, 3)
        if nineties and player.get("bonus_total") is not None else None,
        "net_transfers": net,
        "rival_ownership": player.get("rival_ownership"),
        "ownership": player.get("ownership"),
        "availability": availability(player),
    }


def compute_rank(values, pos, pool_stats):
    """Weighted mean of component z-scores, shrunk by how much of the model a
    player actually has data for.

    Takes pre-derived values rather than the raw player so that the same derived
    quantities feed both the pool statistics and the scoring -- derived fields
    like starts_rate and availability do not exist on the player record, and
    reading them from there silently produced a zero-variance pool.

    Missing components are treated as pool-average (z 0) and the divisor is the
    FULL weight total, not just the weights present. The previous version
    divided by the weights used, which turned "we know almost nothing about this
    player" into "average of the two things we do know" -- a player with only
    fixtures and price ranked seventh on no evidence. Dividing by the full total
    shrinks the unknown toward the middle instead, and RANK_MIN_COVERAGE refuses
    to rank anyone below a floor of evidence at all.

    Players who cannot play are excluded outright rather than scored, since no
    amount of underlying quality helps a suspended player this gameweek."""
    if values["availability"] == 0.0:
        return {"rank_score": None, "rank_score_before_availability": None,
                "availability_penalty": None, "rank_basis": [], "rank_components": {},
                "weights_used": {}, "rank_coverage": 0.0,
                "rank_excluded_reason": "unavailable"}

    z, components = {}, {}
    for name in RANK_WEIGHTS:
        stats = lookup_stats(pool_stats, name, pos, POSITION_RELATIVE_COMPONENTS)
        raw = zscore(values[name], stats)
        if raw is not None and name in LOWER_IS_BETTER:
            raw = -raw
        z[name] = raw
        components[name] = None if raw is None else round(raw, 4)

    rank_basis = [n for n in RANK_WEIGHTS if z[n] is not None]
    weights_used = {n: RANK_WEIGHTS[n] for n in rank_basis}
    total_weight = sum(RANK_WEIGHTS.values())
    coverage = round(sum(weights_used.values()) / total_weight, 3)

    if coverage < RANK_MIN_COVERAGE:
        return {"rank_score": None, "rank_score_before_availability": None,
                "availability_penalty": None, "rank_basis": rank_basis, "rank_components": components,
                "weights_used": weights_used, "rank_coverage": coverage,
                "rank_excluded_reason": f"insufficient data (coverage {coverage} < {RANK_MIN_COVERAGE})"}

    score = sum(RANK_WEIGHTS[n] * z[n] for n in rank_basis) / total_weight
    penalty = round(AVAILABILITY_PENALTY * (1.0 - values["availability"]), 4)
    return {"rank_score": round(score - penalty, 4), "rank_score_before_availability": round(score, 4),
            "availability_penalty": penalty, "rank_basis": rank_basis, "rank_components": components,
            "weights_used": weights_used, "rank_coverage": coverage, "rank_excluded_reason": None}


def rank_players(pool):
    """Score every player in the pool -- squad and watchlist alike -- against the
    same distribution, so a player you own can be compared directly with a
    transfer target.

    Derived inputs are computed once up front and reused for both the pool
    statistics and each player's score, so the two can never disagree."""
    inputs = []
    for player in pool:
        values = derive_rank_inputs(player)
        values["pos"] = player["pos"]
        inputs.append(values)
        # Surface the derived quantities so the score stays auditable.
        player.update({k: values[k] for k in ("starts_rate", "bonus_per90", "availability")})

    pool_stats = compute_pool_stats(inputs, RANK_WEIGHTS.keys(), POSITION_RELATIVE_COMPONENTS)
    for player, values in zip(pool, inputs):
        player.update(compute_rank(values, player["pos"], pool_stats))
    return {(f"{k[0]}|{k[1]}" if isinstance(k, tuple) else k): v for k, v in pool_stats.items()}


def sort_by_rank(players):
    players.sort(key=lambda p: (p["rank_score"] is None, -(p["rank_score"] or 0)))


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
    """Top 20 per league, with each rival's picks.

    Also returns picks per league as element IDs. IDs rather than web_names:
    names are not unique in this league (there are two Palmers), and the old
    name-matching counter credited every match, overstating ownership."""
    rivals, picks_by_league = {}, {}
    for league_id in league_ids:
        standings = get(f"leagues-classic/{league_id}/standings/")
        results = standings["standings"]["results"][:20]
        league_rivals, league_picks = [], []
        for r in results:
            rival_entry_id = r["entry"]
            try:
                resp = get(f"entry/{rival_entry_id}/event/{picks_gw}/picks/")
                pick_ids = [p["element"] for p in resp["picks"]]
            except requests.RequestException:
                pick_ids = []
            league_picks.append({"entry_id": rival_entry_id, "ids": set(pick_ids)})
            league_rivals.append({
                "entry_id": rival_entry_id,
                "name": r["player_name"],
                "total": r["total"],
                "picks": [elements[pid]["web_name"] for pid in pick_ids if pid in elements],
            })
            time.sleep(0.1)
        rivals[str(league_id)] = league_rivals
        picks_by_league[str(league_id)] = league_picks
    return rivals, picks_by_league


def compute_league_ownership(picks_by_league):
    """Ownership share per league, plus a deduplicated overall share.

    Per-league matters for transfer decisions in a way the aggregate hides: a
    player owned by half of one mini-league and nobody in the other is a very
    different proposition depending on which title is in play. Each entry keeps
    its own denominator, since the leagues differ in size and only the top 20
    of each is fetched."""
    per_league, overall = {}, defaultdict(int)
    for league_id, squads in picks_by_league.items():
        counts = defaultdict(int)
        for squad in squads:
            for pid in squad["ids"]:
                counts[pid] += 1
        per_league[league_id] = {"counts": counts, "of": len(squads)}

    # A rival appearing in both leagues is one manager, counted once overall.
    seen = set()
    for squads in picks_by_league.values():
        for squad in squads:
            if squad["entry_id"] in seen:
                continue
            seen.add(squad["entry_id"])
            for pid in squad["ids"]:
                overall[pid] += 1
    return per_league, overall, len(seen)


def attach_league_ownership(records, per_league, overall, overall_total):
    for record in records:
        pid = record["player_id"]
        record["league_ownership"] = {
            league_id: {
                "owned_by": data["counts"].get(pid, 0),
                "of": data["of"],
                "share": round(data["counts"].get(pid, 0) / data["of"], 3) if data["of"] else None,
            }
            for league_id, data in per_league.items()
        }
        record["rival_ownership"] = round(overall.get(pid, 0) / overall_total, 3) if overall_total else None


def deadline_state_path(gameweek):
    return os.path.join(DEADLINE_STATE_DIR, f"gw{gameweek}.json")


def capture_deadline_state(gameweek, deadline, pool, now):
    """Freeze what the model believed BEFORE the deadline.

    Calibration has to grade the score the model actually stood behind at the
    time, not one recomputed later against finished results -- that would be
    marking its own homework with the answers in hand. Every run before the
    deadline overwrites this file, so it converges on the last pre-deadline
    view; once the deadline passes, gw_next advances and this gameweek's file
    is never touched again.

    Nothing is written at or after the deadline, so a post-deadline run cannot
    contaminate the frozen state."""
    if not gameweek or not deadline:
        return None
    deadline_dt = parse_iso(deadline)
    if deadline_dt is None or now >= deadline_dt:
        return None

    os.makedirs(DEADLINE_STATE_DIR, exist_ok=True)
    state = {
        "gameweek": gameweek,
        "deadline": deadline,
        "captured_at": now.isoformat(),
        "rank_weights": RANK_WEIGHTS,
        "players": [
            {
                "player_id": p["player_id"], "name": p["name"], "club": p["club"],
                "pos": p["pos"], "price": p["price"], "source": p["source"],
                "rank_score": p["rank_score"], "rank_coverage": p.get("rank_coverage"),
                "rank_basis": p.get("rank_basis") or [],
                "rank_components": p.get("rank_components") or {},
                "availability_penalty": p.get("availability_penalty"),
            }
            for p in pool
        ],
    }
    with open(deadline_state_path(gameweek), "w") as f:
        json.dump(state, f, separators=(",", ":"))
    return state


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


CALIBRATION_COLUMNS = (
    ["gameweek", "captured_at", "source", "player_id", "name", "club", "pos", "price",
     "rank_score", "rank_coverage", "availability_penalty"]
    + [f"z_{name}" for name in RANK_WEIGHTS]
    + ["rank_basis", "minutes_played", "actual_points"]
)


def logged_gameweeks():
    """Gameweeks already in calibration.csv, so a rerun appends nothing twice."""
    if not os.path.exists(CALIBRATION_PATH):
        return set()
    with open(CALIBRATION_PATH, newline="") as f:
        return {row["gameweek"] for row in csv.DictReader(f) if row.get("gameweek")}


def scores_locked(event):
    """FPL flips finished first and data_checked once bonus is applied and the
    numbers are final, so data_checked is the one to wait for."""
    return bool(event.get("data_checked", event.get("finished")))


def append_calibration_rows(events, elements, cache, latest_finished_gw):
    """Append one row per player per locked gameweek, joining the frozen
    pre-deadline score to what actually happened.

    Append-only: a gameweek already present is skipped rather than rewritten,
    so history cannot be quietly revised."""
    already = logged_gameweeks()
    appended = {}
    for event in events:
        gameweek = event["id"]
        if not scores_locked(event) or str(gameweek) in already:
            continue
        path = deadline_state_path(gameweek)
        if not os.path.exists(path):
            continue  # no pre-deadline capture exists; never reconstruct one
        with open(path) as f:
            state = json.load(f)

        rows = []
        for player in state["players"]:
            # Players who have since left the watchlist are still graded, so the
            # log is not silently restricted to those the model still likes.
            history = fetch_player_history(player["player_id"], cache, latest_finished_gw)
            played = [h for h in history if h.get("round") == gameweek]
            row = {
                "gameweek": gameweek,
                "captured_at": state["captured_at"],
                "source": player["source"],
                "player_id": player["player_id"],
                "name": player["name"],
                "club": player["club"],
                "pos": player["pos"],
                "price": player["price"],
                "rank_score": player["rank_score"],
                "rank_coverage": player["rank_coverage"],
                "availability_penalty": player["availability_penalty"],
                "rank_basis": "|".join(player["rank_basis"]),
                "minutes_played": sum(h.get("minutes") or 0 for h in played) if played else None,
                "actual_points": sum(h.get("total_points") or 0 for h in played) if played else None,
            }
            for name in RANK_WEIGHTS:
                row[f"z_{name}"] = player["rank_components"].get(name)
            rows.append(row)

        write_header = not os.path.exists(CALIBRATION_PATH)
        with open(CALIBRATION_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CALIBRATION_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)
        appended[gameweek] = len(rows)
    return appended


def build_transfer_options(my_squad, watchlist, bank, free_transfers):
    """Which upgrades you can actually afford right now.

    bank, squad_value and free_transfers are entry-level constants -- the same
    number for every player -- so they cannot differentiate anyone inside a
    per-player z-score. They belong here instead, gating the ranking by what is
    actually executable: an upgrade you cannot fund is not a recommendation."""
    ranked_squad = [p for p in my_squad if p.get("rank_score") is not None]
    options = []
    for target in (p for p in watchlist if p.get("rank_score") is not None):
        for held in ranked_squad:
            if held["pos"] != target["pos"]:
                continue  # FPL transfers are position-for-position
            budget = round(held["selling_price"] + bank, 1)
            if target["price"] > budget:
                continue
            gain = round(target["rank_score"] - held["rank_score"], 4)
            if gain <= 0:
                continue
            options.append({
                "out": held["name"], "out_pos": held["pos"], "out_rank_score": held["rank_score"],
                "in": target["name"], "in_pos": target["pos"], "in_rank_score": target["rank_score"],
                "rank_gain": gain,
                "cost_change": round(target["price"] - held["selling_price"], 1),
                "bank_after": round(budget - target["price"], 1),
            })
    options.sort(key=lambda o: -o["rank_gain"])
    return {
        "free_transfers": free_transfers,
        "bank": bank,
        "affordable_upgrades": options[:25],
        "affordable_upgrade_count": len(options),
    }


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
        "fixture_score": None, "fdr_next3": None, "fdr_next1": None,
        "rival_ownership": None, "league_ownership": None,
    }
    my_squad = [
        player_record(elements[pid], teams_by_id, types_by_id, empty_stats)
        for pid in (p["element"] for p in my_picks)
    ]

    # Neither depends on which players we track, so both can precede selection
    # -- which is what lets the candidate pool be chosen on fixture-aware merit.
    team_strength = build_team_strength(fixtures, teams_by_id, elements)
    fixtures_next6 = build_fixtures_next6(fixtures, teams_by_id, list(teams_by_id))

    watchlist, watchlist_selection = build_watchlist(
        elements, teams_by_id, types_by_id, my_ids, empty_stats,
        fixtures_next6, team_strength, watchlist_size)

    cache = load_element_summary_cache()
    attach_player_stats(my_squad, elements, cache, latest_finished_gw)
    attach_player_stats(watchlist, elements, cache, latest_finished_gw)
    save_element_summary_cache(cache)

    attach_fixture_scores(my_squad, fixtures_next6, team_strength)
    attach_fixture_scores(watchlist, fixtures_next6, team_strength)

    # Rivals feed rank_ownership, so they must be fetched before ranking.
    rivals, picks_by_league = build_rivals(league_ids, picks_gw, elements)
    per_league, overall_counts, overall_total = compute_league_ownership(picks_by_league)
    attach_league_ownership(my_squad, per_league, overall_counts, overall_total)
    attach_league_ownership(watchlist, per_league, overall_counts, overall_total)

    # Squad and watchlist are scored against the same distribution, so a player
    # you own is directly comparable with a transfer target.
    for player in my_squad:
        player["source"] = "squad"
    for player in watchlist:
        player["source"] = "watchlist"
    pool_stats = rank_players(my_squad + watchlist)
    sort_by_rank(watchlist)

    # Informational only, and applied after every score above is already final.
    european_clubs = config.get("european_clubs", {})
    european_by_club = load_european_fixtures(config)
    annotate_european_context(fixtures_next6, european_by_club)
    warn_european_calendar_stale(european_clubs, european_by_club, fixtures_next6)

    price_watch = [
        {"name": p["name"], "transfers_in": p["transfers_in_event"],
         "transfers_out": p["transfers_out_event"], "net": p["net_transfers"]}
        for p in my_squad
    ]

    bank = entry_info.get("last_deadline_bank", 0) / 10
    squad_value = entry_info.get("last_deadline_value", 0) / 10
    free_transfers = compute_free_transfers(entry_history, picks_gw)
    transfer_options = build_transfer_options(my_squad, watchlist, bank, free_transfers)

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
        "watchlist_selection": watchlist_selection,
        "rank_weights": RANK_WEIGHTS,
        "transfer_options": transfer_options,
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

    # Freeze the pre-deadline view, then grade any gameweek whose scores have
    # locked. Capture happens after scoring so the frozen state is the real one,
    # and only ever before the deadline itself.
    now = datetime.now(timezone.utc)
    captured = capture_deadline_state(gw_next, next_deadline, my_squad + watchlist, now)
    appended = append_calibration_rows(events, elements, cache, latest_finished_gw)
    save_element_summary_cache(cache)

    with open(OUTPUT_PATH, "w") as f:
        json.dump(snapshot, f, separators=(",", ":"))

    size_bytes = os.path.getsize(OUTPUT_PATH)
    print(f"snapshot.json written: {size_bytes} bytes ({size_bytes / 1024:.2f} KB)")
    if captured:
        print(f"deadline state captured for GW{captured['gameweek']} "
              f"({len(captured['players'])} players, deadline {captured['deadline']})")
    else:
        print(f"no deadline state captured (GW{gw_next} deadline {next_deadline} has passed or is unknown)")
    for gameweek, count in sorted(appended.items()):
        print(f"calibration.csv: appended {count} rows for GW{gameweek}")
    if not appended:
        print("calibration.csv: nothing new to append")
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
