"""Match analysis report generation.

This module generates detailed match analysis reports for single Valorant series.
It provides team comparisons, round-by-round timelines, and impact metrics.

Report Sections:
    - metadata: Series info (tournament, date, teams)
    - games: Per-map results and scores
    - scope: Data coverage and confidence level
    - team_comparison: Side-by-side team stats
    - key_metrics: Opening duels, conversions, impact, consistency, economy
    - player_performance: Per-player stats (K/D, ADR, clutches, multikills)
    - kast_impact_analysis: KAST correlation with round outcomes
    - opening_death_impact: First death correlation with losses
    - round_timeline: Chronological round-by-round breakdown
    - highlight_rounds: Aces, clutches, and multikill rounds
    - half_breakdown: First vs second half performance

SQL Dependencies:
    See src/vlml/tools/sql/README.md for the complete file mapping.
    Key files: team_round_metrics.sql, team_impact_metrics.sql, team_comparison.sql,
    player_performance.sql, round_timeline_enhanced.sql, highlight_rounds.sql

Usage:
    >>> from vlml.tools.reports import match_analysis_report
    >>> report = await match_analysis_report(series_id="abc123")
    >>> report = await match_analysis_report(series_id="abc123", team_name="Cloud9", map_name="Ascent")
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from vlml.db.manager import EventDatabase

from ..common import (
    confidence_label,
    in_clause,
    kast_impact,
    load_sql,
    opening_death_impact,
    series_games,
    series_metadata,
    situation_benchmarks,
)


def _team_round_metrics(
    db: EventDatabase,
    series_ids: List[str],
    team_name: str,
    map_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch round-level metrics for a team (opening duels, conversions).

    Args:
        db: Database connection.
        series_ids: List of series IDs to include.
        team_name: Team name to filter (uses ILIKE matching).
        map_name: Optional map name filter.

    Returns:
        Dict with rounds_played, rounds_won, first_bloods, first_deaths,
        fb_converted_total, fb_attempts_total, fd_salvage_total, fd_attempts_total.
    """
    series_clause = in_clause(series_ids)
    params: List[Any] = list(series_ids) + [f"%{team_name}%"]
    map_filter = ""
    if map_name:
        map_filter = " AND trs.map_name = ?"
        params.append(map_name)

    sql = load_sql("team_round_metrics.sql").format(
        series_clause=series_clause,
        map_filter=map_filter,
    )
    row = db.query(sql, params)[0]

    return {
        "rounds_played": row[0] or 0,
        "rounds_won": row[1] or 0,
        "first_bloods": row[2] or 0,
        "first_deaths": row[3] or 0,
        "fb_converted_total": row[4] or 0,
        "fb_attempts_total": row[5] or 0,
        "fd_salvage_total": row[6] or 0,
        "fd_attempts_total": row[7] or 0,
    }


def _team_impact_metrics(
    db: EventDatabase,
    series_ids: List[str],
    team_name: str,
    map_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch impact metrics for a team (multikills, clutches).

    Args:
        db: Database connection.
        series_ids: List of series IDs to include.
        team_name: Team name to filter.
        map_name: Optional map name filter.

    Returns:
        Dict with double_kills, triple_kills, quad_kills, aces,
        clutch_attempts, clutch_wins, clutch_avg_opponents.
    """
    series_clause = in_clause(series_ids)
    params: List[Any] = list(series_ids) + [f"%{team_name}%"]
    map_filter = ""
    if map_name:
        map_filter = " AND prs.map_name = ?"
        params.append(map_name)

    sql = load_sql("team_impact_metrics.sql").format(
        series_clause=series_clause,
        map_filter=map_filter,
    )
    row = db.query(sql, params)[0]

    return {
        "double_kills": row[0] or 0,
        "triple_kills": row[1] or 0,
        "quad_kills": row[2] or 0,
        "aces": row[3] or 0,
        "clutch_attempts": row[4] or 0,
        "clutch_wins": row[5] or 0,
        "clutch_avg_opponents": float(row[6]) if row[6] is not None else None,
    }


def _team_consistency_metrics(
    db: EventDatabase,
    series_ids: List[str],
    team_name: str,
    map_name: Optional[str] = None,
) -> Dict[str, Any]:
    series_clause = in_clause(series_ids)
    params: List[Any] = list(series_ids) + [f"%{team_name}%"]
    map_filter = ""
    if map_name:
        map_filter = " AND prs.map_name = ?"
        params.append(map_name)

    sql = load_sql("team_consistency_metrics.sql").format(
        series_clause=series_clause,
        map_filter=map_filter,
    )
    row = db.query(sql, params)[0]
    kills = row[4] or 0
    deaths = row[5] or 0

    return {
        "kast_num": row[0] or 0,
        "kast_denom": row[1] or 0,
        "adr_num": float(row[2]) if row[2] is not None else 0.0,
        "adr_denom": row[3] or 0,
        "kd_ratio": round(kills / deaths, 2) if deaths > 0 else float(kills),
    }


def _team_economy_metrics(
    db: EventDatabase,
    series_ids: List[str],
    team_name: str,
    map_name: Optional[str] = None,
) -> Dict[str, Any]:
    series_clause = in_clause(series_ids)
    params: List[Any] = list(series_ids) + [f"%{team_name}%"]
    map_filter = ""
    if map_name:
        map_filter = " AND tgs.map_name = ?"
        params.append(map_name)

    pistol_sql = load_sql("team_economy_pistol.sql").format(
        series_clause=series_clause,
        map_filter=map_filter,
    )
    pistol_row = db.query(pistol_sql, params)[0]

    eco_sql = load_sql("team_economy_eco.sql").format(
        series_clause=series_clause,
        map_filter=map_filter,
    )
    eco_row = db.query(eco_sql, params)[0]

    return {
        "pistol_wins": pistol_row[0] or 0,
        "pistol_rounds": pistol_row[1] or 0,
        "eco_rounds": eco_row[0] or 0,
        "eco_wins": eco_row[1] or 0,
        "thrifty_wins": eco_row[2] or 0,
    }


def _build_key_metrics(team_metrics: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "opening_duels": {
            "fb": {"num": team_metrics["first_bloods"], "denom": team_metrics["rounds_played"]},
            "fd": {"num": team_metrics["first_deaths"], "denom": team_metrics["rounds_played"]},
            "net_fb": team_metrics["first_bloods"] - team_metrics["first_deaths"],
        },
        "conversion": {
            "fb_conv": {"num": team_metrics["fb_converted_total"], "denom": team_metrics["fb_attempts_total"]},
            "fd_salvage": {"num": team_metrics["fd_salvage_total"], "denom": team_metrics["fd_attempts_total"]},
        },
        "impact": {
            "multikills": {
                "2k": team_metrics["double_kills"],
                "3k": team_metrics["triple_kills"],
                "4k": team_metrics["quad_kills"],
                "ace": team_metrics["aces"],
            },
            "clutches": {
                "attempts": team_metrics["clutch_attempts"],
                "wins": team_metrics["clutch_wins"],
                "rate": {"num": team_metrics["clutch_wins"], "denom": team_metrics["clutch_attempts"]},
                "avg_difficulty": team_metrics["clutch_avg_opponents"],
            },
        },
        "consistency": {
            "kast": {"num": team_metrics["kast_num"], "denom": team_metrics["kast_denom"]},
            "adr": {"num": team_metrics["adr_num"], "denom": team_metrics["adr_denom"]},
            "kd": team_metrics["kd_ratio"],
        },
        "economy": {
            "pistol": {"num": team_metrics["pistol_wins"], "denom": team_metrics["pistol_rounds"]},
            "eco": {"num": team_metrics["eco_wins"], "denom": team_metrics["eco_rounds"]},
            "thrifty": {"count": team_metrics["thrifty_wins"]},
        },
    }


def assemble_team_metrics(
    db: EventDatabase,
    series_ids: List[str],
    team_name: str,
    map_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble all team metrics into key metrics structure."""
    round_metrics = _team_round_metrics(db, series_ids, team_name, map_name)
    impact_metrics = _team_impact_metrics(db, series_ids, team_name, map_name)
    consistency_metrics = _team_consistency_metrics(db, series_ids, team_name, map_name)
    economy_metrics = _team_economy_metrics(db, series_ids, team_name, map_name)

    metrics = {
        **round_metrics,
        **impact_metrics,
        **consistency_metrics,
        **economy_metrics,
    }
    return _build_key_metrics(metrics)


def _round_timeline(
    db: EventDatabase,
    series_ids: List[str],
    team_name: str,
    map_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    series_clause = in_clause(series_ids)
    params: List[Any] = list(series_ids) + [f"%{team_name}%"]
    map_filter = ""
    if map_name:
        map_filter = " AND trs.map_name = ?"
        params.append(map_name)

    rounds_sql = load_sql("round_timeline_rounds.sql").format(
        series_clause=series_clause,
        map_filter=map_filter,
    )
    rounds = db.query(rounds_sql, params)
    round_ids = [row[0] for row in rounds]
    if not round_ids:
        return []

    opener_sql = load_sql("round_timeline_opener.sql").format(
        round_ids_clause=in_clause(round_ids),
    )
    opener_rows = db.query(opener_sql, round_ids)
    opener_map = {row[0]: row[1:] for row in opener_rows}

    timeline = []
    for round_id, round_number, map_name_val, round_won in rounds:
        opener_row = opener_map.get(round_id, (None, None, None, None, 0, 0, 0))
        opener, opener_team, victim, victim_team, max_multi, ace_flag, clutch_win = opener_row
        highlight = "Standard"
        if ace_flag:
            highlight = "ACE"
        elif max_multi and max_multi >= 3:
            highlight = "Multi-kill"
        elif clutch_win:
            highlight = "Clutch"

        flag = "STANDARD"
        if opener_team and opener_team.lower() in team_name.lower() and not round_won:
            flag = "REVIEW"
        elif opener_team and opener_team.lower() not in team_name.lower() and round_won:
            flag = "REPLICATE"

        timeline.append({
            "round": int(round_number) if round_number is not None else None,
            "map": map_name_val,
            "opener": opener,
            "opener_victim": victim,
            "winner": team_name if round_won else None,
            "highlight": highlight,
            "flag": flag,
        })

    return timeline


def _issues_from_metrics(team_name: str, key_metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []

    fb = key_metrics["opening_duels"]["fb"]
    fd = key_metrics["opening_duels"]["fd"]
    fb_conv = key_metrics["conversion"]["fb_conv"]
    fd_salvage = key_metrics["conversion"]["fd_salvage"]
    clutches = key_metrics["impact"]["clutches"]
    clutch_attempts = clutches.get("attempts", 0)
    clutch_wins = clutches.get("wins", 0)

    if fd["denom"] > 0 and fd["num"] / fd["denom"] > 0.25:
        issues.append({
            "id": len(issues) + 1,
            "title": "High First Death Rate",
            "description": f"{team_name} is dying first too often, putting rounds at an early disadvantage.",
            "evidence": f"First deaths {fd['num']}/{fd['denom']}",
            "frequency": f"{fd['num']} of {fd['denom']} rounds",
            "impact": "Early man disadvantage",
            "root_cause": "Entry timing or insufficient trade setup",
        })

    if fb_conv["denom"] > 0 and fb_conv["num"] / fb_conv["denom"] < 0.55:
        issues.append({
            "id": len(issues) + 1,
            "title": "Low First Blood Conversion",
            "description": f"{team_name} fails to close rounds despite getting first blood.",
            "evidence": f"FB conversion {fb_conv['num']}/{fb_conv['denom']}",
            "frequency": f"{fb_conv['num']} of {fb_conv['denom']} rounds",
            "impact": "Wasted entry advantages",
            "root_cause": "Poor mid-round trading or post-entry spacing",
        })

    if clutch_attempts > 0 and clutch_wins / clutch_attempts < 0.3:
        issues.append({
            "id": len(issues) + 1,
            "title": "Clutch Conversion Below Baseline",
            "description": f"{team_name} is struggling to close clutch situations.",
            "evidence": f"Clutch wins {clutch_wins}/{clutch_attempts}",
            "frequency": f"{clutch_wins} of {clutch_attempts} clutches",
            "impact": "Lost swing rounds",
            "root_cause": "Post-plant setup or isolated fights",
        })

    return issues


def _action_plan_from_issues(issues: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    immediate = []
    short_term = []
    long_term = []

    for issue in issues:
        title = issue["title"].lower()
        if "first death" in title:
            immediate.append("Rehearse entry timing with coordinated utility (flash/recon before first peek).")
        if "conversion" in title:
            short_term.append("Review post-entry protocols to secure trades and avoid solo pushes.")
        if "clutch" in title:
            long_term.append("Build structured post-plant setups to reduce isolated clutch attempts.")

    return {
        "immediate": immediate or ["Review opening duel protocols for consistent trade spacing."],
        "short_term": short_term or ["Standardize mid-round comms for trade-ready spacing."],
        "long_term": long_term or ["Refine default setups to reduce early deaths and improve conversions."],
    }


def _team_comparison(
    db: EventDatabase,
    series_id: str,
    map_name: Optional[str] = None,
) -> Dict[str, Any]:
    params: List[Any] = [series_id]
    map_filter = ""
    if map_name:
        map_filter = " AND trs.map_name = ?"
        params.append(map_name)

    sql = load_sql("team_comparison.sql").format(map_filter=map_filter)
    rows = db.query(sql, params)

    impact_sql = load_sql("team_comparison_impact.sql").format(map_filter=map_filter)
    impact_rows = db.query(impact_sql, params)
    impact_map = {row[0]: row[1:] for row in impact_rows}

    pistol_sql = load_sql("team_comparison_pistol.sql").format(map_filter=map_filter)
    pistol_rows = db.query(pistol_sql, params)
    pistol_map = {row[0]: row[1:] for row in pistol_rows}

    comparison = {}
    for row in rows:
        team = row[0]
        impact = impact_map.get(team, (0, 0, 0, 0, 0, 0, 0, 0))
        pistol = pistol_map.get(team, (0, 0, 0, 0))
        rounds = row[1] or 0
        comparison[team] = {
            "name": team,
            "rounds_won": row[2] or 0,
            "rounds_played": rounds,
            "assists": row[11] or 0,
            "opening_duels": {
                "fb": {"num": row[3] or 0, "denom": rounds},
                "fd": {"num": row[4] or 0, "denom": rounds},
                "net_fb": (row[3] or 0) - (row[4] or 0),
            },
            "conversion": {
                "fb_converted": {"num": row[5] or 0, "denom": row[6] or 0},
                "fd_salvaged": {"num": row[7] or 0, "denom": row[8] or 0},
            },
            "impact": {
                "multikills": {
                    "2k": impact[0] or 0,
                    "3k": impact[1] or 0,
                    "4k": impact[2] or 0,
                    "ace": impact[3] or 0,
                },
                "clutches": {
                    "attempts": impact[4] or 0,
                    "wins": impact[5] or 0,
                    "rate": {"num": impact[5] or 0, "denom": impact[4] or 0},
                },
            },
            "consistency": {
                "kast": {"num": impact[6] or 0, "denom": impact[7] or 0},
                "adr": {"num": float(row[12]) if row[12] is not None else 0.0, "denom": rounds},
                "kd_ratio": round((row[9] or 0) / (row[10] or 1), 2),
            },
            "timing": {
                "avg_time_to_fk_s": float(row[15]) if row[15] is not None else None,
                "avg_time_to_plant_s": float(row[16]) if row[16] is not None else None,
                "avg_post_plant_duration_s": float(row[17]) if row[17] is not None else None,
            },
            "trades": {
                "deaths_traded": row[13] or 0,
                "deaths_untraded": row[14] or 0,
                "trade_rate": {"num": row[13] or 0, "denom": (row[13] or 0) + (row[14] or 0)},
            },
            "post_plant": {
                "kills": row[18] or 0,
                "deaths": row[19] or 0,
            },
            "economy": {
                "pistol": {"wins": pistol[0] or 0, "played": pistol[1] or 0},
                "post_pistol": {"wins": pistol[2] or 0, "played": pistol[3] or 0},
            },
        }
    return comparison


def _player_performance(
    db: EventDatabase,
    series_id: str,
    map_name: Optional[str] = None,
    team_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    params: List[Any] = [series_id]
    map_filter = ""
    team_filter = ""
    if map_name:
        map_filter = " AND prs.map_name = ?"
        params.append(map_name)
    if team_name:
        team_filter = " AND prs.team_name ILIKE ?"
        params.append(f"%{team_name}%")

    sql = load_sql("player_performance.sql").format(map_filter=map_filter, team_filter=team_filter)
    rows = db.query(sql, params)
    results = []
    for row in rows:
        rounds = row[3] or 0
        deaths = row[5] or 0
        headshot_hits = row[12] or 0
        hits_total = row[13] or 0
        results.append({
            "player_name": row[0],
            "team_name": row[1],
            "agent_name": row[2],
            "rounds": rounds,
            "kills": row[4] or 0,
            "deaths": deaths,
            "assists": row[6] or 0,
            "kd_ratio": round((row[4] or 0) / deaths, 2) if deaths > 0 else float(row[4] or 0),
            "fb": row[7] or 0,
            "fd": row[8] or 0,
            "opening_kills": row[9] or 0,
            "opening_deaths": row[10] or 0,
            "adr": round((row[11] or 0) / rounds, 1) if rounds > 0 else 0.0,
            "headshot_hits": headshot_hits,
            "hits_total": hits_total,
            "hs_pct": round(headshot_hits / hits_total * 100, 2) if hits_total > 0 else 0.0,
            "kast_pct": round((row[14] or 0) / (row[15] or 1) * 100, 1),
            "clutch_attempts": row[16] or 0,
            "clutches_won": row[17] or 0,
            "multikills": {
                "2k": row[18] or 0,
                "3k": row[19] or 0,
                "4k": row[20] or 0,
                "ace": row[21] or 0,
            },
        })
    return results


def _round_timeline_enhanced(
    db: EventDatabase,
    series_id: str,
    map_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    params: List[Any] = [series_id]
    map_filter = ""
    if map_name:
        map_filter = " AND r.map_name = ?"
        params.append(map_name)

    sql = load_sql("round_timeline_enhanced.sql").format(map_filter=map_filter)
    rows = db.query(sql, params)
    timeline = []
    for row in rows:
        round_number = row[2]
        round_type = "gun"
        if round_number in (1, 13):
            round_type = "pistol"
        elif round_number in (2, 14):
            round_type = "post_pistol"

        timeline.append({
            "game_number": row[0],
            "map_name": row[1],
            "round_number": round_number,
            "round_type": round_type,
            "fb_player": row[5],
            "fb_team": row[6],
            "fd_player": row[7],
            "fd_team": row[8],
            "winner": row[3],
            "end_reason": row[4],
            "time_to_fk_s": float(row[9]) if row[9] is not None else None,
            "time_to_plant_s": float(row[10]) if row[10] is not None else None,
            "post_plant_duration_s": float(row[11]) if row[11] is not None else None,
        })
    return timeline


def _highlight_rounds(
    db: EventDatabase,
    series_id: str,
    map_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    params: List[Any] = [series_id]
    map_filter = ""
    if map_name:
        map_filter = " AND r.map_name = ?"
        params.append(map_name)

    sql = load_sql("highlight_rounds.sql").format(map_filter=map_filter)
    rows = db.query(sql, params)
    results = []
    for row in rows:
        results.append({
            "game_number": row[0],
            "map_name": row[1],
            "round_number": row[2],
            "player": row[3],
            "team": row[4],
            "kills": row[5],
            "type": row[6],
            "clutch_won": bool(row[7]) if row[7] is not None else None,
            "clutch_opponents": row[8],
        })
    return results


def _half_breakdown(
    db: EventDatabase,
    series_id: str,
    map_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    params: List[Any] = [series_id]
    map_filter = ""
    if map_name:
        map_filter = " AND r.map_name = ?"
        params.append(map_name)

    sql = load_sql("half_breakdown.sql").format(map_filter=map_filter)
    rows = db.query(sql, params)
    results = []
    for row in rows:
        results.append({
            "map_name": row[0],
            "team_name": row[1],
            "first_half": {"rounds": row[2] or 0, "wins": row[3] or 0},
            "second_half": {"rounds": row[4] or 0, "wins": row[5] or 0},
        })
    return results


def _economy_context(
    db: EventDatabase,
    series_ids: List[str],
    team_name: str,
    map_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch round-by-round economy progression for cascade pattern analysis.

    Args:
        db: Database connection.
        series_ids: List of series IDs to include.
        team_name: Team name to filter (uses ILIKE matching).
        map_name: Optional map name filter.

    Returns:
        List of dicts with per-round economy data including:
            - round_number, buy_type, loadout_value, round_won
            - prev_round context (LAG data)
            - streak and momentum indicators
            - opponent economy comparison
    """
    series_clause = in_clause(series_ids)
    # SQL uses series_clause twice: once in team_loadouts CTE, once in main query
    params: List[Any] = list(series_ids) + list(series_ids) + [f"%{team_name}%"]
    map_filter = ""
    if map_name:
        map_filter = " AND g.map_name = ?"
        params.append(map_name)

    sql = load_sql("economy_round_context.sql").format(
        series_clause=series_clause,
        map_filter=map_filter,
    )
    rows = db.query(sql, params)

    results = []
    for row in rows:
        results.append({
            "round_id": row[0],
            "team_name": row[1],
            "game_id": row[2],
            "game_number": row[3],
            "map_name": row[4],
            "round_number": row[5],
            "side": row[6],
            "round_won": bool(row[7]) if row[7] is not None else None,
            "buy_type": row[8],
            "loadout_value": row[9],
            "net_worth": row[10],
            "prev_round": {
                "round_number": row[11],
                "round_won": bool(row[12]) if row[12] is not None else None,
                "loadout_value": row[13],
                "buy_type": row[14],
            } if row[11] is not None else None,
            "streak": row[15],
            "recent_momentum": row[16],
            "opponent": {
                "loadout_value": row[17],
                "buy_type": row[18],
            },
            "loadout_diff": row[19],
            "end_reason": row[20],
        })
    return results


def _round_situations(
    db: EventDatabase,
    series_ids: List[str],
    team_name: str,
    map_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch rich per-round situation data for LLM reasoning.

    Args:
        db: Database connection.
        series_ids: List of series IDs to include.
        team_name: Team name to filter (uses ILIKE matching).
        map_name: Optional map name filter.

    Returns:
        List of dicts with per-round situation data including:
            - Score context, match point, overtime flags
            - Player counts, loadout values, weapon distribution
            - Utility usage comparison
            - Opening duel results
            - Post-plant context (retakes, kills/deaths)
            - Next round economy projection
    """
    series_clause = in_clause(series_ids)
    map_filter = ""
    map_params: List[Any] = []
    if map_name:
        map_filter = " AND g.map_name = ?"
        map_params = [map_name]

    # Need to pass series_ids and team filter multiple times for CTEs
    # CTEs: team_loadouts, round_state (+ team), player_weapons (+ team), next_round_economy (+ team)
    full_params: List[Any] = (
        list(series_ids)  # team_loadouts CTE
        + list(series_ids) + [f"%{team_name}%"] + map_params  # round_state CTE
        + list(series_ids) + [f"%{team_name}%"] + map_params  # player_weapons CTE
        + list(series_ids) + [f"%{team_name}%"] + map_params  # next_round_economy CTE
    )

    sql = load_sql("round_situation_context.sql").format(
        series_clause=series_clause,
        map_filter=map_filter,
    )
    rows = db.query(sql, full_params)

    results = []
    for row in rows:
        results.append({
            "round_id": row[0],
            "game_id": row[1],
            "game_number": row[2],
            "map_name": row[3],
            "team_name": row[4],
            "round_number": row[5],
            "side": row[6],
            "round_won": bool(row[7]) if row[7] is not None else None,
            "score": {
                "at_start": row[8],
                "team": row[9],
                "opponent": row[10],
            },
            "is_match_point": bool(row[11]) if row[11] is not None else False,
            "is_overtime": bool(row[12]) if row[12] is not None else False,
            "situation": {
                "team_alive_at_end": row[13],
                "opp_alive_at_end": row[14],
                "team_loadout": row[15],
                "opp_loadout": row[16],
                "team_buy_type": row[17],
                "opp_buy_type": row[18],
            },
            "weapons": {
                "rifles": row[19] or 0,
                "snipers": row[20] or 0,
                "smgs": row[21] or 0,
            },
            "utility": {
                "team_used": row[22],
                "opp_used": row[23],
                "flashes": row[24],
                "smokes": row[25],
            },
            "opening_duel": {
                "got_entry_kill": bool(row[26]) if row[26] is not None else False,
                "got_entry_death": bool(row[27]) if row[27] is not None else False,
                "first_bloods": row[28] or 0,
                "first_deaths": row[29] or 0,
            },
            "post_plant": {
                "had_plant": (row[30] or 0) > 0,
                "retake_attempted": row[31] or 0,
                "retake_kills": row[32] or 0,
                "post_plant_kills": row[33] or 0,
                "post_plant_deaths": row[34] or 0,
            },
            "timing": {
                "time_to_first_kill_s": float(row[35]) if row[35] else None,
                "time_to_plant_s": float(row[36]) if row[36] else None,
                "post_plant_duration_s": float(row[37]) if row[37] else None,
            },
            "outcome": {
                "end_reason": row[38],
                "winner": row[39],
            },
            "trades": {
                "deaths_traded": row[40] or 0,
                "deaths_untraded": row[41] or 0,
            },
            "streak": row[42],
            "combat": {
                "team_kills": row[43] or 0,
                "team_deaths": row[44] or 0,
                "opp_kills": row[45] or 0,
                "opp_deaths": row[46] or 0,
            },
            "next_round": {
                "projected_loadout": row[47],
                "projected_buy_type": row[48],
            } if row[47] is not None else None,
        })
    return results


def _attack_patterns(
    db: EventDatabase,
    series_ids: List[str],
    team_name: str,
    map_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch attack pattern context for execute timing and site selection analysis.

    Args:
        db: Database connection.
        series_ids: List of series IDs to include.
        team_name: Team name to filter (uses ILIKE matching).
        map_name: Optional map name filter.

    Returns:
        List of dicts with per-round attack pattern data including:
            - Execute timing (early/mid/late)
            - First contact time and first blood info
            - Site hit determination
            - Plant timing classification
    """
    series_clause = in_clause(series_ids)
    params: List[Any] = list(series_ids) + list(series_ids) + list(series_ids) + [f"%{team_name}%"]
    map_filter = ""
    if map_name:
        map_filter = " AND g.map_name = ?"
        params.append(map_name)

    sql = load_sql("attack_pattern_context.sql").format(
        series_clause=series_clause,
        map_filter=map_filter,
    )
    rows = db.query(sql, params)

    results = []
    for row in rows:
        results.append({
            "round_id": row[0],
            "game_id": row[1],
            "game_number": row[2],
            "map_name": row[3],
            "team_name": row[4],
            "round_number": row[5],
            "round_won": bool(row[6]) if row[6] is not None else None,
            "loadout_value": row[7],
            "side": row[8],
            "first_contact": {
                "timing": row[9],
                "time_s": float(row[10]) if row[10] else None,
                "first_blood_for": row[11],
                "killer": row[12],
                "victim": row[13],
            },
            "site_hit": row[14],
            "had_plant": (row[15] or 0) > 0,
            "plant_time_s": float(row[16]) if row[16] else None,
            "plant_speed": row[17],
            "outcome": {
                "end_reason": row[18],
                "winner": row[19],
            },
            "late_execute": bool(row[20]) if row[20] is not None else False,
        })
    return results


async def match_analysis_report(
    series_id: str,
    team_name: Optional[str] = None,
    map_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a comprehensive match analysis report for a single series.

    This is the main entry point for match analysis. It assembles data from
    multiple queries to provide a complete picture of a series including
    team comparisons, player performance, round timelines, and coaching insights.

    Args:
        series_id: The unique identifier for the series to analyze.
        team_name: Optional focus team (defaults to first team found).
        map_name: Optional map filter to analyze a specific game.

    Returns:
        Dict containing:
            - report_type: "match_analysis"
            - version: Report format version
            - series_id: The analyzed series ID
            - team_name: Focus team name
            - metadata: Tournament, date, teams involved
            - games: List of maps with scores
            - scope: Round count and confidence level
            - team_comparison: Side-by-side team metrics
            - key_metrics: Focus team's detailed metrics
            - player_performance: Per-player stats
            - kast_impact_analysis: KAST correlation data
            - opening_death_impact: First death impact data
            - round_timeline: Round-by-round breakdown
            - highlight_rounds: Notable rounds (aces, clutches)
            - half_breakdown: First/second half splits
            - economy_context: Round-by-round economy progression for cascade analysis
            - round_situations: Rich per-round situation data for LLM reasoning
            - attack_patterns: Execute timing and site selection patterns
            - benchmarks: Historical baseline rates for LLM reference

    Example:
        >>> report = await match_analysis_report("abc123")
        >>> print(report["team_comparison"]["Team A"]["rounds_won"])
    """
    with EventDatabase(read_only=True) as db:
        metadata = series_metadata(db, series_id)
        teams_sql = load_sql("match_analysis_teams.sql")
        team_rows = db.query(teams_sql, [series_id])
        teams = [row[0] for row in team_rows]
        if not teams:
            return {"error": f"No data found for series {series_id}"}

        focus_team = team_name or teams[0]
        opponent_team = next((t for t in teams if t.lower() != focus_team.lower()), None)

        series_scope_sql = load_sql("match_analysis_scope_rounds.sql")
        scope_rounds = db.query(series_scope_sql, [series_id, f"%{focus_team}%"])[0][0]

        focus_metrics = assemble_team_metrics(db, [series_id], focus_team, map_name)
        opponent_metrics = assemble_team_metrics(db, [series_id], opponent_team, map_name) if opponent_team else {}

        return {
            "report_type": "match_analysis",
            "version": "3.0",
            "series_id": series_id,
            "team_name": focus_team,
            "metadata": metadata,
            "games": series_games(db, series_id),
            "scope": {
                "maps": [map_name] if map_name else list({row[0] for row in db.query(
                    load_sql("match_analysis_scope_maps.sql"),
                    [series_id],
                )}),
                "rounds": scope_rounds or 0,
                "confidence": confidence_label(scope_rounds or 0),
            },
            "team_comparison": _team_comparison(db, series_id, map_name),
            "key_metrics": {
                "team": focus_metrics,
                "opponent": opponent_metrics,
            },
            "player_performance": _player_performance(db, series_id, map_name),
            "kast_impact_analysis": kast_impact(db, series_id),
            "opening_death_impact": opening_death_impact(db, series_id),
            "round_timeline": _round_timeline_enhanced(db, series_id, map_name),
            "highlight_rounds": _highlight_rounds(db, series_id, map_name),
            "half_breakdown": _half_breakdown(db, series_id, map_name),
            # Enhanced coaching context (v3.0)
            "economy_context": _economy_context(db, [series_id], focus_team, map_name),
            "round_situations": _round_situations(db, [series_id], focus_team, map_name),
            "attack_patterns": _attack_patterns(db, [series_id], focus_team, map_name),
            "benchmarks": situation_benchmarks(db, [series_id]),
        }


async def match_summary_report(
    series_id: str,
    team_name: Optional[str] = None,
    map_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a lightweight match summary report for quick overview.

    This is the recommended first call for match analysis. Returns metadata
    and high-level metrics without the heavy round-by-round data. Use this
    to get an overview and decide what to drill into with the other reports.

    Args:
        series_id: The unique identifier for the series to analyze.
        team_name: Optional focus team (defaults to first team found).
        map_name: Optional map filter to analyze a specific game.

    Returns:
        Dict containing:
            - report_type: "match_summary"
            - version: Report format version
            - series_id: The analyzed series ID
            - team_name: Focus team name
            - metadata: Tournament, date, teams involved
            - games: List of maps with scores
            - scope: Round count and confidence level
            - team_comparison: Side-by-side team metrics (condensed)
            - key_metrics: Focus team's detailed metrics (condensed)
            - benchmarks: Historical baseline rates for LLM reference

    Example:
        >>> report = await match_summary_report("abc123")
        >>> print(report["team_comparison"]["Team A"]["rounds_won"])
    """
    with EventDatabase(read_only=True) as db:
        metadata = series_metadata(db, series_id)
        teams_sql = load_sql("match_analysis_teams.sql")
        team_rows = db.query(teams_sql, [series_id])
        teams = [row[0] for row in team_rows]
        if not teams:
            return {"error": f"No data found for series {series_id}"}

        focus_team = team_name or teams[0]
        opponent_team = next((t for t in teams if t.lower() != focus_team.lower()), None)

        series_scope_sql = load_sql("match_analysis_scope_rounds.sql")
        scope_rounds = db.query(series_scope_sql, [series_id, f"%{focus_team}%"])[0][0]

        focus_metrics = assemble_team_metrics(db, [series_id], focus_team, map_name)
        opponent_metrics = assemble_team_metrics(db, [series_id], opponent_team, map_name) if opponent_team else {}

        return {
            "report_type": "match_summary",
            "version": "1.0",
            "series_id": series_id,
            "team_name": focus_team,
            "metadata": metadata,
            "games": series_games(db, series_id),
            "scope": {
                "maps": [map_name] if map_name else list({row[0] for row in db.query(
                    load_sql("match_analysis_scope_maps.sql"),
                    [series_id],
                )}),
                "rounds": scope_rounds or 0,
                "confidence": confidence_label(scope_rounds or 0),
            },
            "team_comparison": _team_comparison(db, series_id, map_name),
            "key_metrics": {
                "team": focus_metrics,
                "opponent": opponent_metrics,
            },
            "benchmarks": situation_benchmarks(db, [series_id]),
        }


async def match_players_report(
    series_id: str,
    team_name: Optional[str] = None,
    map_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a player-focused match report.

    Returns player performance data, KAST impact analysis, opening death
    impact, and highlight rounds. Use this for player performance analysis
    and VOD priority identification.

    Args:
        series_id: The unique identifier for the series to analyze.
        team_name: Optional focus team (defaults to first team found).
        map_name: Optional map filter to analyze a specific game.

    Returns:
        Dict containing:
            - report_type: "match_players"
            - version: Report format version
            - series_id: The analyzed series ID
            - team_name: Focus team name
            - player_performance: Per-player stats (K/D, ADR, clutches, multikills)
            - kast_impact_analysis: KAST correlation with round outcomes
            - opening_death_impact: First death correlation with losses
            - highlight_rounds: Aces, clutches, and multikill rounds

    Example:
        >>> report = await match_players_report("abc123")
        >>> for player in report["player_performance"]:
        ...     print(f"{player['player_name']}: {player['kd_ratio']} K/D")
    """
    with EventDatabase(read_only=True) as db:
        teams_sql = load_sql("match_analysis_teams.sql")
        team_rows = db.query(teams_sql, [series_id])
        teams = [row[0] for row in team_rows]
        if not teams:
            return {"error": f"No data found for series {series_id}"}

        focus_team = team_name or teams[0]

        return {
            "report_type": "match_players",
            "version": "1.0",
            "series_id": series_id,
            "team_name": focus_team,
            "player_performance": _player_performance(db, series_id, map_name, focus_team),
            "kast_impact_analysis": kast_impact(db, series_id, player_name=None),
            "opening_death_impact": opening_death_impact(db, series_id, player_name=None),
            "highlight_rounds": _highlight_rounds(db, series_id, map_name),
        }


async def match_rounds_report(
    series_id: str,
    team_name: Optional[str] = None,
    map_name: Optional[str] = None,
    round_start: Optional[int] = None,
    round_end: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate a round-by-round match report.

    Returns detailed round timeline, rich situation context, and half breakdown.
    This is a heavy report - call only when needed for deep-dive analysis.
    Use round_start/round_end to paginate large datasets.

    Args:
        series_id: The unique identifier for the series to analyze.
        team_name: Optional focus team (defaults to first team found).
        map_name: Optional map filter to analyze a specific game.
        round_start: Optional starting round number (inclusive) for pagination.
        round_end: Optional ending round number (inclusive) for pagination.

    Returns:
        Dict containing:
            - report_type: "match_rounds"
            - version: Report format version
            - series_id: The analyzed series ID
            - team_name: Focus team name
            - pagination: Round range if filtered
            - round_timeline: Chronological round-by-round breakdown
            - round_situations: Rich per-round situation data for LLM reasoning
            - half_breakdown: First vs second half performance

    Example:
        >>> # Get all rounds
        >>> report = await match_rounds_report("abc123")
        >>> # Get rounds 10-15 only
        >>> report = await match_rounds_report("abc123", round_start=10, round_end=15)
    """
    with EventDatabase(read_only=True) as db:
        teams_sql = load_sql("match_analysis_teams.sql")
        team_rows = db.query(teams_sql, [series_id])
        teams = [row[0] for row in team_rows]
        if not teams:
            return {"error": f"No data found for series {series_id}"}

        focus_team = team_name or teams[0]

        # Get round data
        round_timeline = _round_timeline_enhanced(db, series_id, map_name)
        round_situations = _round_situations(db, [series_id], focus_team, map_name)

        # Apply pagination if specified
        pagination = None
        if round_start is not None or round_end is not None:
            start = round_start or 1
            end = round_end or 999

            round_timeline = [
                r for r in round_timeline
                if start <= r["round_number"] <= end
            ]
            round_situations = [
                r for r in round_situations
                if start <= r["round_number"] <= end
            ]
            pagination = {"round_start": start, "round_end": end}

        return {
            "report_type": "match_rounds",
            "version": "1.0",
            "series_id": series_id,
            "team_name": focus_team,
            "pagination": pagination,
            "round_timeline": round_timeline,
            "round_situations": round_situations,
            "half_breakdown": _half_breakdown(db, series_id, map_name),
        }


async def match_economy_report(
    series_id: str,
    team_name: Optional[str] = None,
    map_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate an economy and tactical patterns report.

    Returns economy context and attack pattern data. This is a heavy report -
    call only when needed for economy cascade analysis or attack predictability
    detection.

    Args:
        series_id: The unique identifier for the series to analyze.
        team_name: Optional focus team (defaults to first team found).
        map_name: Optional map filter to analyze a specific game.

    Returns:
        Dict containing:
            - report_type: "match_economy"
            - version: Report format version
            - series_id: The analyzed series ID
            - team_name: Focus team name
            - economy_context: Round-by-round economy progression for cascade analysis
            - attack_patterns: Execute timing and site selection patterns

    Example:
        >>> report = await match_economy_report("abc123", team_name="Cloud9")
        >>> for round in report["economy_context"]:
        ...     print(f"R{round['round_number']}: {round['buy_type']} ({round['loadout_value']})")
    """
    with EventDatabase(read_only=True) as db:
        teams_sql = load_sql("match_analysis_teams.sql")
        team_rows = db.query(teams_sql, [series_id])
        teams = [row[0] for row in team_rows]
        if not teams:
            return {"error": f"No data found for series {series_id}"}

        focus_team = team_name or teams[0]

        return {
            "report_type": "match_economy",
            "version": "1.0",
            "series_id": series_id,
            "team_name": focus_team,
            "economy_context": _economy_context(db, [series_id], focus_team, map_name),
            "attack_patterns": _attack_patterns(db, [series_id], focus_team, map_name),
        }
