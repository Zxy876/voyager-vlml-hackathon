"""Player profile report generation.

This module generates career-focused player profile reports across multiple series.
It provides career stats, agent/map splits, recent form, and clutch performance.

Report Sections:
    - metadata: Player name, team, date range, series/games/rounds count
    - career_stats: Aggregate K/D/A, ADR, KAST%, FB/FD rates, win rate
    - agent_performance: Stats broken down by agent played
    - map_performance: Stats broken down by map
    - recent_form: Last N series results with key stats
    - kast_impact: Correlation between KAST and round outcomes
    - opening_death_impact: Correlation between first deaths and losses
    - clutch_performance: 1v1 through 1v5 attempt/win breakdown
    - round_type_performance: Pistol/eco/gun round splits
    - multikills: 2k/3k/4k/ace counts

SQL Dependencies:
    See src/vlml/tools/sql/README.md for the complete file mapping.
    Key files: player_profile_*.sql, player_key_metrics.sql

Usage:
    >>> from vlml.tools.reports import player_profile_report
    >>> report = await player_profile_report(player_name="aspas")
    >>> report = await player_profile_report(player_name="aspas", last_n_series=10, map_name="Ascent")
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from vlml.db.manager import EventDatabase

from ..common import (
    fetch_series_ids_for_player,
    in_clause,
    kast_impact,
    load_sql,
    opening_death_impact,
)


def player_key_metrics(
    db: EventDatabase,
    series_ids: List[str],
    player_name: str,
    map_name: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Calculate key metrics for a player across multiple series.

    Assembles the standard key_metrics structure used across reports,
    including opening duels, impact (multikills, clutches), and consistency.

    Args:
        db: Database connection.
        series_ids: List of series IDs to include.
        player_name: Player name to filter (uses ILIKE matching).
        map_name: Optional map filter.
        agent_name: Optional agent filter.

    Returns:
        Dict with rounds_played and key_metrics containing opening_duels,
        conversion, impact (multikills, clutches), consistency, and economy sections.
    """
    series_clause = in_clause(series_ids)
    params: List[Any] = list(series_ids) + [f"%{player_name}%"]
    map_filter = ""
    agent_filter = ""
    if map_name:
        map_filter = " AND prs.map_name = ?"
        params.append(map_name)
    if agent_name:
        agent_filter = " AND prs.agent_name ILIKE ?"
        params.append(f"%{agent_name}%")

    rounds_sql = load_sql("player_key_metrics.sql").format(
        series_clause=series_clause,
        map_filter=map_filter,
        agent_filter=agent_filter,
    )
    row = db.query(rounds_sql, params)[0]
    rounds_played = row[0] or 0

    return {
        "rounds_played": rounds_played,
        "key_metrics": {
            "opening_duels": {
                "fb": {"num": row[2] or 0, "denom": rounds_played},
                "fd": {"num": row[3] or 0, "denom": rounds_played},
                "net_fb": (row[2] or 0) - (row[3] or 0),
            },
            "conversion": {
                "fb_conv": {"num": 0, "denom": 0},
                "fd_salvage": {"num": 0, "denom": 0},
            },
            "impact": {
                "multikills": {
                    "2k": row[6] or 0,
                    "3k": row[7] or 0,
                    "4k": row[8] or 0,
                    "ace": row[9] or 0,
                },
                "clutches": {
                    "attempts": row[4] or 0,
                    "wins": row[5] or 0,
                    "rate": {"num": row[5] or 0, "denom": row[4] or 0},
                    "avg_difficulty": None,
                },
            },
            "consistency": {
                "kast": {"num": row[12] or 0, "denom": row[13] or 0},
                "adr": {"num": float(row[14]) if row[14] is not None else 0.0, "denom": row[15] or 0},
                "kd": round((row[10] or 0) / (row[11] or 1), 2),
            },
            "economy": {"pistol": {"num": 0, "denom": 0}, "eco": {"num": 0, "denom": 0}, "thrifty": {"count": 0}},
        },
    }


async def player_profile_report(
    player_name: str,
    series_ids: Optional[List[str]] = None,
    last_n_series: int = 5,
    map_name: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a comprehensive player profile report over multiple series.

    This is the main entry point for player analysis. It aggregates performance
    data across multiple series to provide career stats, agent/map splits,
    recent form, and detailed clutch/impact metrics.

    Args:
        player_name: The player name to search for (uses ILIKE matching).
        series_ids: Optional explicit list of series IDs. If not provided,
            fetches the most recent series for the player.
        last_n_series: Number of recent series to include (default 5).
            Ignored if series_ids is provided.
        map_name: Optional map filter to analyze specific map performance.
        agent_name: Optional agent filter to analyze specific agent performance.

    Returns:
        Dict containing:
            - report_type: "player_profile"
            - version: Report format version
            - player_name: The searched player name
            - metadata: Player info, team, date range, sample sizes
            - career_stats: Aggregate K/D/A, ADR, KAST%, FB/FD, win rate
            - agent_performance: List of agent-specific stats
            - map_performance: List of map-specific stats
            - recent_form: Recent series with results and key stats
            - kast_impact: KAST correlation with round outcomes
            - opening_death_impact: First death correlation with losses
            - clutch_performance: 1v1-1v5 attempt/win breakdown
            - round_type_performance: Pistol/eco/gun round splits
            - multikills: 2k/3k/4k/ace counts

    Example:
        >>> report = await player_profile_report("aspas", last_n_series=10)
        >>> print(report["career_stats"]["adr"])
    """
    with EventDatabase(read_only=True) as db:
        if not series_ids:
            series_ids = fetch_series_ids_for_player(db, player_name, last_n_series)
        if not series_ids:
            return {"error": f"No series found for player {player_name}"}

        series_clause = in_clause(series_ids)
        params: List[Any] = list(series_ids) + [f"%{player_name}%"]
        map_filter = ""
        agent_filter = ""
        if map_name:
            map_filter = " AND prs.map_name = ?"
            params.append(map_name)
        if agent_name:
            agent_filter = " AND prs.agent_name ILIKE ?"
            params.append(f"%{agent_name}%")

        metadata_sql = load_sql("player_profile_metadata.sql").format(
            series_clause=series_clause,
            map_filter=map_filter,
            agent_filter=agent_filter,
        )
        meta_row = db.query(metadata_sql, params)[0]

        career_sql = load_sql("player_profile_career.sql").format(
            series_clause=series_clause,
            map_filter=map_filter,
            agent_filter=agent_filter,
        )
        career_row = db.query(career_sql, params)[0]

        agent_sql = load_sql("player_profile_agent.sql").format(
            series_clause=series_clause,
            map_filter=map_filter,
            agent_filter=agent_filter,
        )
        agent_rows = db.query(agent_sql, params)

        map_sql = load_sql("player_profile_map.sql").format(
            series_clause=series_clause,
            map_filter=map_filter,
            agent_filter=agent_filter,
        )
        map_rows = db.query(map_sql, params)

        recent_sql = load_sql("player_profile_recent.sql").format(series_clause=series_clause)
        recent_rows = db.query(recent_sql, list(series_ids) + [f"%{player_name}%"])

        kast_impact_data = kast_impact(
            db,
            series_ids=series_ids,
            player_name=player_name,
            min_deaths_no_kast=0,
        )
        od_impact = opening_death_impact(
            db,
            series_ids=series_ids,
            player_name=player_name,
            min_opening_deaths=0,
        )

        clutch_sql = load_sql("player_profile_clutch.sql").format(series_clause=series_clause)
        clutch_row = db.query(clutch_sql, params)[0]

        round_type_sql = load_sql("player_profile_round_types.sql").format(series_clause=series_clause)
        round_type_rows = db.query(round_type_sql, list(series_ids) + [f"%{player_name}%"])

        multikill_sql = load_sql("player_profile_multikill.sql").format(series_clause=series_clause)
        multikill_row = db.query(multikill_sql, list(series_ids) + [f"%{player_name}%"])[0]

        rounds_played = career_row[9] or 0
        kills = career_row[0] or 0
        deaths = career_row[1] or 0
        fb = career_row[6] or 0
        fd = career_row[7] or 0
        adr = (career_row[3] or 0) / rounds_played if rounds_played > 0 else 0.0
        kast_pct = (career_row[4] or 0) / (career_row[5] or 1) * 100
        win_rate = (career_row[8] or 0) / rounds_played * 100 if rounds_played > 0 else 0.0

        return {
            "report_type": "player_profile",
            "version": "2.0",
            "player_name": player_name,
            "metadata": {
                "player_name": meta_row[0],
                "team_name": meta_row[1],
                "first_game": str(meta_row[2])[:10] if meta_row[2] else None,
                "last_game": str(meta_row[3])[:10] if meta_row[3] else None,
                "total_series": meta_row[4] or 0,
                "total_games": meta_row[5] or 0,
                "total_rounds": meta_row[6] or 0,
            },
            "career_stats": {
                "kills": kills,
                "deaths": deaths,
                "assists": career_row[2] or 0,
                "kd_ratio": round(kills / deaths, 2) if deaths > 0 else float(kills),
                "adr": round(adr, 1),
                "kast_pct": round(kast_pct, 1),
                "fb": fb,
                "fd": fd,
                "fb_pct": round(fb / rounds_played * 100, 1) if rounds_played > 0 else 0.0,
                "fd_pct": round(fd / rounds_played * 100, 1) if rounds_played > 0 else 0.0,
                "rounds_won": career_row[8] or 0,
                "win_rate": round(win_rate, 1),
            },
            "agent_performance": [
                {
                    "agent_name": row[0],
                    "rounds": row[1] or 0,
                    "kills": row[2] or 0,
                    "deaths": row[3] or 0,
                    "kd_ratio": round((row[2] or 0) / (row[3] or 1), 2),
                    "adr": round((row[4] or 0) / (row[1] or 1), 1),
                    "fb": row[5] or 0,
                    "fd": row[6] or 0,
                    "kast_pct": round((row[7] or 0) / (row[8] or 1) * 100, 1),
                    "rounds_won": row[9] or 0,
                    "win_rate": round((row[9] or 0) / (row[1] or 1) * 100, 1),
                }
                for row in agent_rows if row[0]
            ],
            "map_performance": [
                {
                    "map_name": row[0],
                    "rounds": row[1] or 0,
                    "kd_ratio": round((row[2] or 0) / (row[3] or 1), 2),
                    "adr": round((row[4] or 0) / (row[1] or 1), 1),
                    "fb": row[5] or 0,
                    "fd": row[6] or 0,
                    "kast_pct": round((row[7] or 0) / (row[8] or 1) * 100, 1),
                    "win_rate": round((row[9] or 0) / (row[1] or 1) * 100, 1),
                }
                for row in map_rows if row[0]
            ],
            "recent_form": [
                {
                    "series_id": row[0],
                    "date": str(row[1])[:10] if row[1] else None,
                    "opponent": row[2],
                    "result": row[3],
                    "kills": row[4] or 0,
                    "deaths": row[5] or 0,
                    "adr": round((row[6] or 0) / (row[9] or 1), 1),
                    "fb": row[7] or 0,
                    "fd": row[8] or 0,
                }
                for row in recent_rows
            ],
            "kast_impact": (kast_impact_data[0] if kast_impact_data else {
                "deaths_without_kast": 0,
                "rounds_lost_when_no_kast": 0,
                "loss_rate_when_no_kast": 0.0,
            }),
            "opening_death_impact": (od_impact[0] if od_impact else {
                "opening_deaths": 0,
                "rounds_won_after_od": 0,
                "rounds_lost_after_od": 0,
                "loss_rate_when_od": 0.0,
            }),
            "clutch_performance": {
                "total_attempts": clutch_row[0] or 0,
                "wins": clutch_row[1] or 0,
                "rate": round((clutch_row[1] or 0) / (clutch_row[0] or 1) * 100, 1),
                "1v1": {"attempts": clutch_row[2] or 0, "wins": clutch_row[3] or 0},
                "1v2": {"attempts": clutch_row[4] or 0, "wins": clutch_row[5] or 0},
                "1v3": {"attempts": clutch_row[6] or 0, "wins": clutch_row[7] or 0},
                "1v4": {"attempts": clutch_row[8] or 0, "wins": clutch_row[9] or 0},
                "1v5": {"attempts": clutch_row[10] or 0, "wins": clutch_row[11] or 0},
            },
            "round_type_performance": {
                row[0]: {
                    "rounds": row[1] or 0,
                    "kills": row[2] or 0,
                    "deaths": row[3] or 0,
                    "kd_ratio": round((row[2] or 0) / (row[3] or 1), 2),
                    "adr": round((row[4] or 0) / (row[1] or 1), 1),
                    "fb": row[5] or 0,
                    "win_rate": round((row[6] or 0) / (row[1] or 1) * 100, 1),
                }
                for row in round_type_rows
            },
            "multikills": {
                "double_kills": multikill_row[0] or 0,
                "triple_kills": multikill_row[1] or 0,
                "quad_kills": multikill_row[2] or 0,
                "aces": multikill_row[3] or 0,
            },
        }
