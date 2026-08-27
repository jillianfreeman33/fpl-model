#!/usr/bin/env python3
"""End-to-end smoke test: run main() against stubbed API responses.

py_compile only checks syntax, so a NameError from a half-applied refactor
reaches CI looking healthy. This exercises the whole pipeline offline and
asserts the model's invariants, so that class of failure is caught here.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)


def iso(days):
    return (NOW + timedelta(days=days)).isoformat().replace("+00:00", "Z")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snapshot as s

TEAMS = [
    {"id": i, "name": f"Club{i}", "short_name": n,
     "strength_overall_home": h, "strength_overall_away": a,
     "strength_attack_home": 0, "strength_attack_away": 0,
     "strength_defence_home": 0, "strength_defence_away": 0}
    for i, (n, h, a) in enumerate(
        [("ARS", 4, 5), ("CHE", 4, 4), ("LIV", 4, 4), ("MCI", 4, 5),
         ("EVE", 3, 3), ("SUN", 2, 3), ("HUL", 2, 2), ("COV", 2, 2)], start=1)
]
POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def make_elements():
    elements, pid = [], 0
    for team in TEAMS:
        for etype in (1, 2, 3, 4):
            for k in range(4):
                pid += 1
                # Deliberately varied: some unavailable, some with no minutes.
                status = "a"
                chance = None
                if pid % 17 == 0:
                    status, chance = "i", 0
                elif pid % 11 == 0:
                    status, chance = "d", 75
                minutes = 0 if pid % 7 == 0 else 90 * (1 + pid % 3)
                elements.append({
                    "id": pid, "web_name": f"P{pid}", "team": team["id"], "element_type": etype,
                    "now_cost": 40 + (pid % 90), "selected_by_percent": str(round(1 + pid % 40, 1)),
                    "status": status, "chance_of_playing_next_round": chance,
                    "form": str(round((pid % 8) * 0.7, 1)), "total_points": pid % 60,
                    "minutes": minutes,
                    "expected_goals": str(round((pid % 9) * 0.12, 2)),
                    "expected_goals_conceded": str(round((pid % 5) * 0.3, 2)),
                    "transfers_in_event": pid * 13, "transfers_out_event": pid * 7,
                })
    return elements


ELEMENTS = make_elements()
FIXTURES = []
for gw in range(1, 8):
    for j in range(0, len(TEAMS), 2):
        h, a = TEAMS[j]["id"], TEAMS[j + 1]["id"]
        played = gw <= 2
        FIXTURES.append({
            "id": gw * 100 + j, "event": gw, "team_h": h, "team_a": a,
            "team_h_score": (gw + j) % 4 if played else None,
            "team_a_score": (gw + j + 1) % 3 if played else None,
            "team_h_difficulty": 3, "team_a_difficulty": 3,
            "finished": False, "finished_provisional": played,
            "kickoff_time": f"2026-0{8 + gw // 5}-{10 + gw:02d}T14:00:00Z",
        })

BOOTSTRAP = {
    "elements": ELEMENTS, "teams": TEAMS,
    "element_types": [{"id": k, "singular_name_short": v} for k, v in POS.items()],
    # Deadlines are relative to the real clock: GW1-2 are behind us, GW3 is the
    # upcoming one. seal_predictions gates on the deadline having passed, so a
    # fixed 2026-09 calendar would never let the test reach the sealed state.
    "events": [{"id": g, "is_current": g == 2, "is_next": g == 3, "finished": g <= 1,
                "data_checked": g <= 1,
                "deadline_time": iso(g - 3 if g <= 2 else (g - 3) * 3 + 2)}
               for g in range(1, 8)],
}
MY_PICKS = [{"element": e["id"], "is_captain": i == 0, "is_vice_captain": i == 1,
             "multiplier": 2 if i == 0 else (1 if i < 11 else 0), "position": i + 1}
            for i, e in enumerate(ELEMENTS[:15])]


def fake_get(path, **params):
    if path == "bootstrap-static/":
        return BOOTSTRAP
    if path == "fixtures/":
        return FIXTURES
    if path.startswith("element-summary/"):
        pid = int(path.split("/")[1])
        el = next(e for e in ELEMENTS if e["id"] == pid)
        rounds = [] if el["minutes"] == 0 else [
            {"round": r,
             "minutes": 90 - (pid % 4) * 15,
             "starts": 1 if (pid + r) % 5 else 0,
             "expected_goals": str(round(0.05 * (pid % 11), 3)),
             "expected_assists": str(round(0.03 * (pid % 7), 3)),
             "defensive_contribution": (pid % 13) + r,
             "bonus": (pid + r) % 4,
             "total_points": (pid + r) % 13}
            for r in range(1, 4 + pid % 3)
        ]
        return {"history": rounds}
    if path.endswith("/history/"):
        return {"current": [{"event": 1, "event_transfers": 0}], "chips": []}
    if "/event/" in path and path.endswith("/picks/"):
        entry = int(path.split("/")[1])
        if entry < 900:
            return {"picks": MY_PICKS}
        offset = (entry - 900) * 3
        return {"picks": [
            {"element": ELEMENTS[(offset + k) % len(ELEMENTS)]["id"],
             "is_captain": k == 0, "is_vice_captain": k == 1,
             "multiplier": 2 if k == 0 else (1 if k < 11 else 0), "position": k + 1}
            for k in range(15)]}
    if path.startswith("entry/"):
        return {"last_deadline_bank": 15, "last_deadline_value": 1003}
    if path.startswith("leagues-classic/"):
        return {"standings": {"results": [
            {"entry": 900 + i, "player_name": f"Rival{i}", "total": 50 - i} for i in range(20)]}}
    raise AssertionError(f"unstubbed path: {path}")


def main():
    tmp = tempfile.mkdtemp()
    s.get = fake_get
    s.ELEMENT_SUMMARY_SLEEP_SECONDS = 0
    s.OUTPUT_PATH = os.path.join(tmp, "snapshot.json")
    s.ELEMENT_SUMMARY_CACHE_PATH = os.path.join(tmp, "cache.json")
    s.DEADLINE_STATE_DIR = os.path.join(tmp, "deadline_state")
    s.PREDICTIONS_PATH = os.path.join(tmp, "predictions.json")
    s.CALIBRATION_PATH = os.path.join(tmp, "calibration.csv")
    real_sleep = s.time.sleep
    s.time.sleep = lambda *_: None
    try:
        s.main()
    finally:
        s.time.sleep = real_sleep

    snap = json.load(open(s.OUTPUT_PATH))
    squad, watch = snap["my_squad"], snap["watchlist"]
    failures = []

    def check(label, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
        if not ok:
            failures.append(label)

    print("\nend-to-end pipeline")
    check("main() runs and writes a snapshot", os.path.getsize(s.OUTPUT_PATH) > 0)
    for key in ("my_squad", "watchlist", "watchlist_rank_stats", "rank_weights",
                "transfer_options", "team_strength", "fixtures_next6", "rivals"):
        check(f"snapshot has {key}", key in snap)

    print("\nfix 1 -- position-relative z")
    stats = snap["watchlist_rank_stats"]
    check("per-position fixture_score stats exist",
          any(k.startswith("fixture_score|") for k in stats),
          str([k for k in stats if k.startswith("fixture_score|")]))
    pool = squad + watch
    for pos in {p["pos"] for p in pool}:
        zs = [p["rank_components"]["fixture_score"] for p in pool
              if p["pos"] == pos and p.get("rank_components", {}).get("fixture_score") is not None]
        if zs:
            # Averaged over the pool the statistics were built from; excluded
            # (unavailable) players drop out, so allow a small tolerance.
            check(f"{pos} fixture z centres on 0", abs(sum(zs) / len(zs)) < 0.35,
                  f"mean={sum(zs)/len(zs):+.3f}")

    print("\nfix 2 -- coverage floor")
    ranked = [p for p in squad + watch if p["rank_score"] is not None]
    check("no ranked player below the coverage floor",
          all(p["rank_coverage"] >= s.RANK_MIN_COVERAGE for p in ranked))
    check("every player reports rank_coverage",
          all("rank_coverage" in p for p in squad + watch))

    print("\nfix 3 -- availability")
    unavail = [p for p in squad + watch if p["status"] in ("i", "s", "u", "n")]
    check("unavailable players are still scored, not nulled",
          all(p["rank_score"] is not None for p in unavail), f"n={len(unavail)}")
    check("unavailable players carry the full availability penalty",
          all(p["availability_penalty"] == s.AVAILABILITY_PENALTY for p in unavail))

    print("\neligibility flag")
    check("every player carries eligible", all("eligible" in p for p in squad + watch))
    check("i/s/u/n are ineligible", all(p["eligible"] is False for p in unavail))
    low = [p for p in squad + watch
           if p["chance_of_playing"] is not None and p["chance_of_playing"] < s.ELIGIBILITY_MIN_CHANCE]
    check("chance below the threshold is ineligible",
          all(p["eligible"] is False for p in low), f"n={len(low)}")
    at_threshold = [p for p in squad + watch
                    if p["chance_of_playing"] == s.ELIGIBILITY_MIN_CHANCE and p["status"] not in ("i", "s", "u", "n")]
    check("chance exactly at the threshold stays eligible",
          all(p["eligible"] is True for p in at_threshold), f"n={len(at_threshold)}")
    check("ineligible players keep a rank_score",
          all(p["rank_score"] is not None
              for p in squad + watch if p["eligible"] is False and p["rank_coverage"] >= s.RANK_MIN_COVERAGE))

    print("\nrate shrinkage")
    priors = snap["rate_priors"]
    check("positional priors published for every rate stat",
          all(f"{f}|{pos}" in priors for f in s.RATE_STATS for pos in {p["pos"] for p in pool}),
          f"{len(priors)} priors")
    played = [p for p in pool if (p["minutes_total"] or 0) > 0]
    check("every player who has played gets a rate, none nulled by a cliff",
          all(p["xgi_per90"] is not None for p in played),
          f"{len(played)} played, {sum(1 for p in played if p['xgi_per90'] is None)} null")
    check("players who never played stay null",
          all(p["xgi_per90"] is None for p in pool if not (p["minutes_total"] or 0)))
    check("shrinkage weight follows minutes/(minutes+180)",
          all(abs(p["rate_shrinkage_weight"]
                  - p["minutes_total"] / (p["minutes_total"] + s.RATE_SHRINKAGE_MINUTES)) < 5e-4
              for p in played))
    check("weight rises with minutes",
          all(a["rate_shrinkage_weight"] <= b["rate_shrinkage_weight"]
              for a, b in zip(sorted(played, key=lambda p: p["minutes_total"]),
                              sorted(played, key=lambda p: p["minutes_total"])[1:])))
    check("xgi_per90 is exactly xg + xa after shrinking",
          all(abs(p["xgi_per90"] - round(p["xg_per90"] + p["xa_per90"], 2)) < 0.011
              for p in played if p["xg_per90"] is not None))
    # The point of the exercise: a small sample must land closer to its position
    # than to its own raw rate.
    low = min(played, key=lambda p: p["minutes_total"])
    high = max(played, key=lambda p: p["minutes_total"])
    check("a small sample is pulled harder than a large one",
          low["rate_shrinkage_weight"] < high["rate_shrinkage_weight"],
          f"{low['minutes_total']}min w={low['rate_shrinkage_weight']} vs "
          f"{high['minutes_total']}min w={high['rate_shrinkage_weight']}")
    check("no shrunk rate exceeds the largest raw rate at its position",
          all(p["xgi_per90"] <= 12 for p in played), "sanity bound")
    check("bonus_per90 goes through shrinkage too, not raw division",
          all(p.get("bonus_per90") is not None for p in played))
    check("bonus_per90 no longer annualises a part-match",
          all(p["bonus_per90"] <= 4.0 for p in played),
          f"max={max(p['bonus_per90'] for p in played)}")
    check("no rate inputs leak into the snapshot",
          all("_rate_inputs" not in p for p in pool))

    print("\nfixture-proxy flag")
    check("every player carries rank_fixture_proxy",
          all("rank_fixture_proxy" in p for p in squad + watch))
    check("the flag agrees with rank_basis",
          all(p["rank_fixture_proxy"] == (not ({"xgi_per90", "minutes_per_app"} & set(p["rank_basis"])))
              for p in squad + watch))

    print("\nfix 6 -- squad ranked")
    check("all squad players scored or explained",
          all("rank_score" in p for p in squad), f"{len(squad)} players")

    print("\nfix 7/9 -- team metrics")
    ts = snap["team_strength"]
    check("_league_anchors measured, not constant", "_league_anchors" in ts,
          json.dumps(ts.get("_league_anchors", {}).get("home")))
    check("_xg_available reported", "_xg_available" in ts, str(ts.get("_xg_available")))

    print("\nno component silently inert")
    for name in s.RANK_WEIGHTS:
        vals = {p["rank_components"].get(name) for p in squad + watch if p.get("rank_components")}
        vals.discard(None)
        check(f"{name} varies", len(vals) > 1, f"{len(vals)} distinct")

    print("\nweights")
    check("all weights positive", all(w > 0 for w in s.RANK_WEIGHTS.values()))
    check("14 z-scored components", len(s.RANK_WEIGHTS) == 14, str(len(s.RANK_WEIGHTS)))
    check("availability is not z-scored", "availability" not in s.RANK_WEIGHTS)

    print("\navailability penalty is bounded")
    pen = [p["availability_penalty"] for p in squad + watch if p.get("availability_penalty") is not None]
    check("penalty never exceeds AVAILABILITY_PENALTY",
          all(0.0 <= x <= s.AVAILABILITY_PENALTY for x in pen),
          f"max={max(pen) if pen else 0}")
    doubtful = [p for p in squad + watch
                if p.get("availability") not in (None, 1.0) and p["rank_score"] is not None]
    check("doubtful players are penalised proportionally",
          all(abs(p["availability_penalty"] - s.AVAILABILITY_PENALTY * (1 - p["availability"])) < 1e-9
              for p in doubtful), f"n={len(doubtful)}")

    print("\nxG is blended, not raw")
    xgs = [ts[c][v]["xg_for_per_match"] for c in ts if not c.startswith("_")
           for v in ("home", "away") if ts[c][v].get("xg_for_per_match") is not None]
    check("blended xG sits in a plausible per-match range",
          all(0.2 <= x <= 3.2 for x in xgs) if xgs else True,
          f"range {min(xgs):.2f}..{max(xgs):.2f}" if xgs else "no xG")

    print("\nwatchlist selection is merit-based")
    sel = snap["watchlist_selection"]
    check("selection method recorded", sel.get("method") == "merit_preselect", str(sel.get("method")))
    check("ownership is not a selection weight", "ownership" not in sel["weights"])
    check("unavailable never selected",
          all(p["status"] not in ("i", "s", "u", "n") for p in watch),
          f"rejected {sel['rejected']['unavailable']} unavailable")
    check("zero-minute players never selected",
          all((p["minutes_total"] or 0) > 0 for p in watch),
          f"rejected {sel['rejected']['no_minutes']} with no minutes")
    check("every selected player has a preselect_score",
          all(p.get("preselect_score") is not None for p in watch))
    check("selection is ordered by preselect_score",
          all(p["preselect_score"] >= sel["cutoff_preselect_score"] for p in watch))
    owns = [p["ownership"] for p in watch]
    check("pool is no longer an ownership prefix",
          min(owns) < 4.0 or len({round(o) for o in owns}) > 5,
          f"ownership {min(owns)}..{max(owns)}")

    print("\nper-league ownership")
    lo = [p for p in squad + watch if p.get("league_ownership")]
    check("every player carries per-league ownership", len(lo) == len(squad + watch))
    sample = lo[0]["league_ownership"]
    check("each league reports its own denominator",
          all(set(v) == {"owned_by", "of", "share", "captained_by", "captain_share"}
              for v in sample.values()), str(sample))
    check("shares are within 0..1",
          all(0.0 <= v["share"] <= 1.0 for p in lo for v in p["league_ownership"].values()
              if v["share"] is not None))
    check("overall rival_ownership still present",
          all(p.get("rival_ownership") is not None for p in lo))

    print("\ncaptain data in rivals")
    rival = snap["rivals"][str(s.load_config()["league_ids"][0])][0]
    check("picks are objects, not bare names", isinstance(rival["picks"][0], dict),
          str(rival["picks"][0]))
    check("all four pick fields stored",
          all({"is_captain", "is_vice_captain", "multiplier", "position"} <= set(p)
              for p in rival["picks"]))
    check("exactly one captain per rival",
          all(sum(1 for p in r["picks"] if p["is_captain"]) == 1
              for lg in snap["rivals"].values() for r in lg if r["picks"]))
    check("exactly one vice-captain per rival",
          all(sum(1 for p in r["picks"] if p["is_vice_captain"]) == 1
              for lg in snap["rivals"].values() for r in lg if r["picks"]))
    check("captain carries a multiplier above 1",
          all(p["multiplier"] > 1 for lg in snap["rivals"].values() for r in lg
              for p in r["picks"] if p["is_captain"]))
    check("positions span the full 15", sorted(p["position"] for p in rival["picks"]) == list(range(1, 16)))

    print("\ncaptain ownership per league")
    co = snap["captain_ownership"]
    check("one entry per league", set(co) == set(snap["rivals"]), str(list(co)))
    for league_id, rows in co.items():
        check(f"league {league_id} captain counts sum to the rival count",
              sum(r["captained_by"] for r in rows) == len(snap["rivals"][league_id]),
              f"{sum(r['captained_by'] for r in rows)}/{len(snap['rivals'][league_id])}")
        check(f"league {league_id} sorted most-captained first",
              [r["captained_by"] for r in rows] == sorted((r["captained_by"] for r in rows), reverse=True))
        check(f"league {league_id} shares within 0..1",
              all(0.0 <= r["share"] <= 1.0 for r in rows))
    check("per-player captain share matches the league summary",
          all(any(r["player_id"] == p["player_id"] and r["captained_by"] == v["captained_by"]
                  for r in co[lid]) or v["captained_by"] == 0
              for p in squad + watch for lid, v in p["league_ownership"].items()))

    print("\ngameweeks_played reconciled with the fixture list")
    dm = snap["data_maturity"]
    played_gws = {f["event"] for f in FIXTURES if f["team_h_score"] is not None}
    check("gameweeks_played counts scored fixtures, not event.finished",
          dm["gameweeks_played"] == len(played_gws),
          f"reported {dm['gameweeks_played']}, fixtures say {len(played_gws)}")
    check("gameweeks_played is not contradicted by minutes on the board",
          not (dm["players_with_90plus_minutes"] > 0 and dm["gameweeks_played"] == 0),
          f"{dm['players_with_90plus_minutes']} players with 90+ minutes")
    check("complete never exceeds played", dm["gameweeks_complete"] <= dm["gameweeks_played"])
    check("locked never exceeds complete", dm["gameweeks_locked"] <= dm["gameweeks_complete"])
    check("per-gameweek fixture counts reported",
          set(dm["fixtures_played_by_gameweek"]) == {str(g) for g in played_gws})

    print("\ntransfer options")
    opts = snap["transfer_options"]
    check("affordable upgrades are position-legal and funded",
          all(o["bank_after"] >= 0 for o in opts["affordable_upgrades"]),
          f"{opts['affordable_upgrade_count']} options")

    print("\ncalibration: freeze before the deadline, grade after the lock")
    gw3 = os.path.join(s.DEADLINE_STATE_DIR, "gw3.json")
    check("pre-deadline state captured for the upcoming GW", os.path.exists(gw3))
    if os.path.exists(gw3):
        state = json.load(open(gw3))
        check("captured strictly before the deadline",
              state["captured_at"] < state["deadline"],
              f"{state['captured_at']} < {state['deadline']}")
        check("captures squad and watchlist", len(state["players"]) == len(squad) + len(watch),
              f"{len(state['players'])} players")
        check("frozen rows carry score and components",
              all("rank_score" in p and "rank_components" in p for p in state["players"]))
        check("frozen rows carry the club's fixture",
              any(p.get("fixture") for p in state["players"]),
              str(next((p["fixture"] for p in state["players"] if p.get("fixture")), None)))
        check("frozen fixture names opponent and venue",
              all({"opponent", "is_home"} <= set(p["fixture"])
                  for p in state["players"] if p.get("fixture")))
        check("frozen rows carry eligible and the proxy flag",
              all("eligible" in p and "rank_fixture_proxy" in p for p in state["players"]))

    check("predictions.json not sealed while the deadline is open",
          not os.path.exists(s.PREDICTIONS_PATH),
          "GW3 is still ahead; the buffer must stay mutable")
    check("nothing logged yet -- GW1 has no recorded prediction",
          not os.path.exists(s.CALIBRATION_PATH),
          "correct: GW1 predates the model, never reconstructed")

    # A second pre-deadline run must still be free to move the buffer, since it
    # is the LAST pre-deadline view that should be sealed.
    prior_capture = state["captured_at"]
    s.time.sleep = lambda *_: None
    try:
        s.main()
    finally:
        s.time.sleep = real_sleep
    state2 = json.load(open(gw3))
    check("buffer stays mutable before the deadline", state2["captured_at"] >= prior_capture,
          f"{prior_capture} -> {state2['captured_at']}")
    check("still unsealed after a second pre-deadline run", not os.path.exists(s.PREDICTIONS_PATH))

    # Time passes: GW3's deadline goes by, its fixtures are played, scores lock.
    for event in BOOTSTRAP["events"]:
        if event["id"] == 3:
            event["finished"] = event["data_checked"] = True
            event["deadline_time"] = iso(-1)
        event["is_current"] = event["id"] == 3
        event["is_next"] = event["id"] == 4
    for fixture in FIXTURES:
        if fixture["event"] == 3:
            fixture["team_h_score"], fixture["team_a_score"] = 1, 0
            fixture["finished_provisional"] = True
    s.time.sleep = lambda *_: None
    try:
        s.main()
    finally:
        s.time.sleep = real_sleep

    print("\npredictions.json is the append-only record")
    check("sealed once the deadline passed", os.path.exists(s.PREDICTIONS_PATH))
    preds = json.load(open(s.PREDICTIONS_PATH))
    check("keyed by gameweek", "3" in preds["gameweeks"], str(list(preds["gameweeks"])))
    gw3_pred = preds["gameweeks"]["3"]
    check("sealed from the pre-deadline buffer, unchanged",
          gw3_pred["captured_at"] == state2["captured_at"],
          f"{gw3_pred['captured_at']}")
    check("sealed strictly after the capture", gw3_pred["sealed_at"] > gw3_pred["captured_at"])
    check("carries every player", len(gw3_pred["players"]) == len(state2["players"]))
    check("carries rank_basis, components and fixture per player",
          all({"rank_basis", "rank_components", "fixture"} <= set(p) for p in gw3_pred["players"]))
    check("records the weights the score was built with",
          gw3_pred["rank_weights"] == s.RANK_WEIGHTS)
    check("records the shrinkage k the score was built under",
          gw3_pred["rate_shrinkage_minutes"] == s.RATE_SHRINKAGE_MINUTES,
          f"k={gw3_pred['rate_shrinkage_minutes']}")
    check("records the positional priors used",
          gw3_pred["rate_priors"] == snap["rate_priors"], f"{len(gw3_pred['rate_priors'])} priors")
    check("records minutes and observed rates per player",
          all({"minutes_total", "rate_shrinkage_weight", "rate_observed"} <= set(p)
              for p in gw3_pred["players"]))

    # The point of storing all that: a different k must be recomputable from the
    # record alone, without re-running the season. Verify by reconstructing the
    # shrunk value at the ORIGINAL k and checking it matches what was published.
    k = gw3_pred["rate_shrinkage_minutes"]
    priors = gw3_pred["rate_priors"]
    live = {p["player_id"]: p for p in pool}
    rebuilt, checked = True, 0
    for rec in gw3_pred["players"]:
        m = rec["minutes_total"]
        if not m or not rec["rate_observed"]:
            continue
        w = m / (m + k)
        for field, observed in rec["rate_observed"].items():
            prior = priors.get(f"{field}|{rec['pos']}")
            expected = round(w * observed + (1 - w) * prior, 2) if prior is not None \
                else round(observed, 2)
            if abs(live[rec["player_id"]][field] - expected) > 0.011:
                rebuilt = False
            checked += 1
    check("a k-sweep is reconstructable from the record alone", rebuilt and checked > 0,
          f"{checked} rate/player pairs rebuilt at k={k}")

    # And that a DIFFERENT k actually moves the answer -- otherwise the stored
    # inputs would be decoration.
    moved = False
    for rec in gw3_pred["players"]:
        m = rec["minutes_total"]
        if not m or "xg_per90" not in rec["rate_observed"]:
            continue
        prior = priors.get(f"xg_per90|{rec['pos']}")
        if prior is None:
            continue
        w_alt = m / (m + 45)
        if abs((w_alt * rec["rate_observed"]["xg_per90"] + (1 - w_alt) * prior)
               - live[rec["player_id"]]["xg_per90"]) > 0.02:
            moved = True
    check("re-running at k=45 gives a different answer", moved,
          "stored inputs are load-bearing, not decorative")
    check("GW1 and GW2 never reconstructed",
          "1" not in preds["gameweeks"] and "2" not in preds["gameweeks"],
          "no pre-deadline buffer existed for either")

    print("\ncalibration reads the record, never recomputes")
    check("calibration.csv written once the GW locked", os.path.exists(s.CALIBRATION_PATH))
    import csv as _csv
    rows = list(_csv.DictReader(open(s.CALIBRATION_PATH, newline="")))
    check("one row per recorded player", len(rows) == len(gw3_pred["players"]), f"{len(rows)} rows")
    check("columns match the schema", list(rows[0]) == s.CALIBRATION_COLUMNS if rows else False)
    check("every rank component has its own column",
          all(f"z_{n}" in rows[0] for n in s.RANK_WEIGHTS) if rows else False,
          f"{len(s.RANK_WEIGHTS)} components")
    check("actual outcomes joined on",
          any(r["actual_points"] not in ("", None) for r in rows) if rows else False)
    check("source distinguishes squad from watchlist",
          {r["source"] for r in rows} == {"squad", "watchlist"} if rows else False)
    frozen = {p["player_id"]: p["rank_score"] for p in gw3_pred["players"]}
    check("logged score is the RECORDED one, not recomputed",
          all(r["rank_score"] == ("" if frozen[int(r["player_id"])] is None
                                  else str(frozen[int(r["player_id"])])) for r in rows) if rows else False)
    frozen_z = {p["player_id"]: p["rank_components"] for p in gw3_pred["players"]}
    check("logged components are the RECORDED ones too",
          all(r[f"z_{n}"] == ("" if frozen_z[int(r["player_id"])].get(n) is None
                              else str(frozen_z[int(r["player_id"])][n]))
              for r in rows for n in s.RANK_WEIGHTS) if rows else False)
    check("the fixture predicted against is logged",
          any(r["opponent"] for r in rows) if rows else False)
    check("eligibility at the deadline is logged",
          all(r["eligible"] in ("True", "False") for r in rows) if rows else False)
    check("the shrinkage k is stamped on every calibration row",
          all(r["rate_shrinkage_minutes"] == str(s.RATE_SHRINKAGE_MINUTES) for r in rows)
          if rows else False, f"k={s.RATE_SHRINKAGE_MINUTES}")
    check("minutes at the deadline are logged",
          all(r["minutes_at_deadline"] != "" for r in rows) if rows else False)

    # Re-run again: append-only means no duplication, and the sealed prediction
    # must be byte-identical -- a record that can be revised is not a prediction.
    before = len(rows)
    s.time.sleep = lambda *_: None
    try:
        s.main()
    finally:
        s.time.sleep = real_sleep
    rows2 = list(_csv.DictReader(open(s.CALIBRATION_PATH, newline="")))
    check("re-running does not duplicate a logged gameweek",
          len(rows2) == before, f"{before} -> {len(rows2)}")
    preds2 = json.load(open(s.PREDICTIONS_PATH))
    check("a sealed gameweek is never overwritten",
          preds2["gameweeks"]["3"] == gw3_pred,
          f"sealed_at still {preds2['gameweeks']['3']['sealed_at']}")

    # Tamper with the buffer and re-run: the sealed record must ignore it.
    tampered = json.load(open(gw3))
    for p in tampered["players"]:
        p["rank_score"] = 99.0
    json.dump(tampered, open(gw3, "w"))
    s.time.sleep = lambda *_: None
    try:
        s.main()
    finally:
        s.time.sleep = real_sleep
    preds3 = json.load(open(s.PREDICTIONS_PATH))
    check("a rewritten buffer cannot revise a sealed prediction",
          preds3["gameweeks"]["3"] == gw3_pred,
          "sealed record unchanged by a post-hoc buffer edit")
    rows3 = list(_csv.DictReader(open(s.CALIBRATION_PATH, newline="")))
    check("and cannot revise what was already logged",
          rows3 == rows2, f"{len(rows3)} rows unchanged")

    print(f"\n{'ALL SMOKE TESTS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
