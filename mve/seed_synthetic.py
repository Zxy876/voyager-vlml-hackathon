#!/usr/bin/env python3
"""Synthetic data generator · builds a small fake dataset for MVE whose fields match VLML's real tables.

Design principles:
- No "answer numbers" are hard-coded. All metrics are computed from base_events by VLML's own transformations SQL.
- Patterns are planted only at the event layer (who gets first blood, who wins after first blood), so the numbers the agg tables compute carry a story of their own.
- This way every number the MCP tools returns is backed by data, in line with VLML's "never guess, only query" principle.

Planted patterns (mirroring the structure of the real Cloud9 vs NRG series):
- Cloud9's first-blood conversion rate collapses on Corrode → a crushing loss
- OXY's first-death count runs high
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parents[1] / "vlml" / "data" / "vlml_events.duckdb"

SERIES_ID = "2843069"
TOURNAMENT_ID = "vct-americas-2025-stage-2"
TOURNAMENT_NAME = "VCT Americas 2025 Stage 2"
TOURNAMENT_YEAR = 2025
REGION = "americas"
C9, NRG = "Cloud9", "NRG"

ROSTERS = {
    C9: [("OXY", "Neon"), ("neT", "Viper"), ("mitch", "Skye"), ("Xeppaa", "Fade"), ("v1c", "Killjoy")],
    NRG: [("mada", "Raze"), ("brawk", "Omen"), ("skuba", "Cypher"), ("Ethan", "Sova"), ("s0m", "Breach")],
}

# First-death weights: OXY highest (planting the "the entry fragger is too exposed" pattern)
FD_WEIGHTS = {C9: [6.0, 2.0, 2.0, 1.5, 1.0], NRG: [1.0, 1.5, 1.5, 2.0, 1.5]}
# First-blood weights (mada/Ethan are NRG's entry fraggers)
FB_WEIGHTS = {C9: [3.0, 1.5, 1.0, 2.0, 1.0], NRG: [3.0, 2.0, 1.0, 2.5, 1.0]}

WEAPONS = [("Vandal", "rifle"), ("Phantom", "rifle"), ("Sheriff", "pistol"),
           ("Operator", "sniper"), ("Spectre", "smg"), ("Classic", "pistol")]

# Per map: Cloud9's and NRG's probability of "winning the round after getting first blood"
# The three maps' story: Haven evenly matched → Corrode collapse → Lotus narrow loss
GAMES = [
    {"game_id": "g-haven", "map": "Haven", "conv": {C9: 0.76, NRG: 0.60}, "fb_p_c9": 0.52},
    {"game_id": "g-corrode", "map": "Corrode", "conv": {C9: 0.28, NRG: 0.88}, "fb_p_c9": 0.32},
    {"game_id": "g-lotus", "map": "Lotus", "conv": {C9: 0.55, NRG: 0.80}, "fb_p_c9": 0.42},
]

MAX_ROUNDS = 30


def pick(roster: list[tuple[str, str]], weights: list[float]) -> tuple[str, str]:
    return random.choices(roster, weights=weights, k=1)[0]


def other(team: str) -> str:
    return NRG if team == C9 else C9


def main() -> None:
    random.seed(20261005)
    con = duckdb.connect(str(DB_PATH))

    # Clear old synthetic data so runs are reproducible
    for t in ["base_events", "rounds", "games", "series",
              "agg_player_round_stats", "agg_player_game_stats", "agg_player_series_stats",
              "agg_team_round_stats", "agg_team_game_stats", "agg_player_daily_stats",
              "agg_tournament_stats", "agg_first_blood_stats", "agg_post_plant_stats",
              "agg_team_round_summary", "agg_team_map_stats", "agg_team_series_stats",
              "agg_player_win_shares"]:
        con.execute(f"DELETE FROM {t}")
    print("old data cleared")

    base_time = datetime(2025, 8, 29, 18, 0, 0)
    events: list[tuple] = []
    round_rows: list[tuple] = []
    game_rows: list[tuple] = []
    event_seq = 0
    clock = base_time

    total_wins = {C9: 0, NRG: 0}

    for gi, g in enumerate(GAMES, start=1):
        gid = g["game_id"]
        score = {C9: 0, NRG: 0}
        rnd = 0

        while max(score.values()) < 13 and rnd < MAX_ROUNDS:
            rnd += 1
            rid = f"{gid}-r{rnd:02d}"
            clock = clock + timedelta(seconds=random.randint(45, 110))

            # Who gets first blood
            fb_team = C9 if random.random() < g["fb_p_c9"] else NRG
            fd_team = other(fb_team)

            # The team that got first blood wins the round per that map's conversion rate
            won = random.random() < g["conv"][fb_team]
            winner = fb_team if won else fd_team
            loser = other(winner)
            score[winner] += 1

            # Attack/defense sides (simplified: alternating)
            atk = C9 if rnd % 2 == 1 else NRG
            def_ = other(atk)

            end_reason = random.choices(
                ["eliminated", "defused", "detonated", "time"],
                weights=[0.62, 0.18, 0.16, 0.04],
            )[0]

            round_rows.append((
                rid, SERIES_ID, gid, rnd, g["map"],
                clock, clock + timedelta(seconds=random.randint(60, 100)),
                random.randint(60, 100),
                winner, loser, end_reason,
                TOURNAMENT_NAME, TOURNAMENT_YEAR, datetime.now(),
            ))

            def mk_event(*, etype, actor, actor_team, target=None, target_team=None,
                         is_kill=False, is_fb=False, is_plant=False, is_defuse=False,
                         damage=None, ts=None):
                nonlocal event_seq
                event_seq += 1
                an, aa = actor
                tn, ta = target if target else (None, None)
                weapon, wtype = random.choice(WEAPONS)
                events.append((
                    f"e{event_seq:07d}", ts or clock, SERIES_ID, gid, rid, etype,
                    f"p_{an}", an, actor_team, aa,
                    f"p_{tn}" if tn else None, tn, target_team, ta,
                    "defense" if target_team == def_ else "attack" if target_team else None,
                    "attack" if actor_team == atk else "defense",
                    round(random.uniform(-5000, 5000), 1), round(random.uniform(-5000, 5000), 1),
                    round(random.uniform(-5000, 5000), 1) if tn else None,
                    round(random.uniform(-5000, 5000), 1) if tn else None,
                    "kill" if is_kill else ("plant" if is_plant else "defuse"),
                    None, None, None,
                    TOURNAMENT_NAME, TOURNAMENT_YEAR, g["map"],
                    is_kill, bool(tn) and is_kill, False, is_fb,
                    is_plant, is_defuse, False, False, False, False, False, False,
                    damage, random.randint(2000, 4000), random.randint(1500, 3500),
                    random.randint(12000, 24000), random.randint(9000, 21000),
                    weapon, wtype, random.random() < 0.35, random.random() < 0.05,
                    random.choice(["head", "body", "legs"]),
                    json.dumps({"synthetic": True}),
                ))

            # First-blood event
            fb_actor = pick(ROSTERS[fb_team], FB_WEIGHTS[fb_team])
            fd_target = pick(ROSTERS[fd_team], FD_WEIGHTS[fd_team])
            mk_event(etype="kill", actor=fb_actor, actor_team=fb_team,
                     target=fd_target, target_team=fd_team,
                     is_kill=True, is_fb=True, damage=150.0)

            # Follow-up kills
            for _ in range(random.randint(2, 5)):
                if random.random() < 0.5:
                    a_team, t_team = winner, loser
                else:
                    a_team, t_team = loser, winner
                mk_event(etype="kill", actor=pick(ROSTERS[a_team], FB_WEIGHTS[a_team]),
                         actor_team=a_team, target=pick(ROSTERS[t_team], FD_WEIGHTS[t_team]),
                         target_team=t_team, is_kill=True, damage=float(random.randint(80, 160)))

            # Plant / defuse
            if end_reason in ("defused", "detonated", "time") or random.random() < 0.35:
                mk_event(etype="plant", actor=pick(ROSTERS[atk], FB_WEIGHTS[atk]),
                         actor_team=atk, is_plant=True)
                if end_reason == "defused":
                    mk_event(etype="defuse", actor=pick(ROSTERS[def_], FB_WEIGHTS[def_]),
                             actor_team=def_, is_defuse=True)

        winner_map = C9 if score[C9] > score[NRG] else NRG
        game_rows.append((
            gid, SERIES_ID, gi, g["map"], C9, NRG, winner_map,
            rnd * 95, rnd,
        ))
        total_wins[C9] += score[C9]
        total_wins[NRG] += score[NRG]
        print(f"  {g['map']:8s}  Cloud9 {score[C9]:2d} - {score[NRG]:2d} NRG   ({rnd} rounds)")

    series_winner = C9 if total_wins[C9] > total_wins[NRG] else NRG
    con.execute(
        "INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?)",
        [SERIES_ID, TOURNAMENT_ID, TOURNAMENT_NAME, TOURNAMENT_YEAR, REGION,
         C9, NRG, series_winner, base_time, datetime.now()],
    )
    con.executemany("INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?)", game_rows)
    con.executemany("INSERT INTO rounds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", round_rows)
    placeholders = ",".join(["?"] * len(events[0]))
    con.executemany(f"INSERT INTO base_events VALUES ({placeholders})", events)
    con.commit()

    print()
    print(f"series      : {len(game_rows)} maps written (total score C9 {total_wins[C9]} - {total_wins[NRG]} NRG, winner {series_winner})")
    print(f"rounds      : {len(round_rows)} rows")
    print(f"base_events : {len(events)} rows")
    print()
    print("next step: run transformations to generate the agg tables")
    print("  python database/scripts/orchestration/run_transformations.py")


if __name__ == "__main__":
    main()
