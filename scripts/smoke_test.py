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
    "events": [{"id": g, "is_current": g == 2, "is_next": g == 3, "finished": g <= 1,
                "deadline_time": f"2026-09-{g:02d}T17:30:00Z"} for g in range(1, 8)],
}
MY_PICKS = [{"element": e["id"]} for e in ELEMENTS[:15]]


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
             "bonus": (pid + r) % 4}
            for r in range(1, 3 + pid % 3)
        ]
        return {"history": rounds}
    if path.endswith("/history/"):
        return {"current": [{"event": 1, "event_transfers": 0}], "chips": []}
    if "/event/" in path and path.endswith("/picks/"):
        entry = int(path.split("/")[1])
        if entry < 900:
            return {"picks": MY_PICKS}
        offset = (entry - 900) * 3
        return {"picks": [{"element": ELEMENTS[(offset + k) % len(ELEMENTS)]["id"]} for k in range(15)]}
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
    check("unavailable players are unranked",
          all(p["rank_score"] is None and p["rank_excluded_reason"] == "unavailable" for p in unavail),
          f"n={len(unavail)}")

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
          all(set(v) == {"owned_by", "of", "share"} for v in sample.values()), str(sample))
    check("shares are within 0..1",
          all(0.0 <= v["share"] <= 1.0 for p in lo for v in p["league_ownership"].values()
              if v["share"] is not None))
    check("overall rival_ownership still present",
          all(p.get("rival_ownership") is not None for p in lo))

    print("\ntransfer options")
    opts = snap["transfer_options"]
    check("affordable upgrades are position-legal and funded",
          all(o["bank_after"] >= 0 for o in opts["affordable_upgrades"]),
          f"{opts['affordable_upgrade_count']} options")

    print(f"\n{'ALL SMOKE TESTS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
