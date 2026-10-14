"""Shared data fetching functions for insights reports."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from vlml.db.manager import EventDatabase

from .metrics import in_clause, load_sql


def fetch_series_ids_for_team(
    db: EventDatabase,
    team_name: str,
    last_n_series: int,
) -> List[str]:
    """Fetch recent series IDs for a team."""
    sql = load_sql("fetch_series_ids_for_team.sql")
    rows = db.query(sql, [f"%{team_name}%", last_n_series])
    return [row[0] for row in rows]


def fetch_series_ids_for_player(
    db: EventDatabase,
    player_name: str,
    last_n_series: int,
) -> List[str]:
    """Fetch recent series IDs for a player."""
    sql = load_sql("fetch_series_ids_for_player.sql")
    rows = db.query(sql, [f"%{player_name}%", last_n_series])
    return [row[0] for row in rows]


def series_metadata(db: EventDatabase, series_id: str) -> Dict[str, Any]:
    """Get metadata for a series."""
    sql = load_sql("series_metadata.sql")
    rows = db.query(sql, [series_id])
    if not rows:
        return {}
    row = rows[0]
    return {
        "series_id": row[0],
        "tournament_name": row[1],
        "date": str(row[2])[:10] if row[2] else None,
        "team1": row[3],
        "team2": row[4],
        "winner": row[5],
    }


def series_games(db: EventDatabase, series_id: str) -> List[Dict[str, Any]]:
    """Get games for a series."""
    sql = load_sql("series_games.sql")
    games = db.query(sql, [series_id])

    score_sql = load_sql("series_games_scores.sql")
    score_rows = db.query(score_sql, [series_id])
    scores: Dict[str, Dict[str, int]] = {}
    for game_id, team_name, rounds_won in score_rows:
        scores.setdefault(game_id, {})[team_name] = rounds_won

    results = []
    for game_id, game_number, map_name, winner, total_rounds in games:
        score_map = scores.get(game_id, {})
        if len(score_map) == 2:
            team_names = list(score_map.keys())
            score = f"{score_map[team_names[0]]}-{score_map[team_names[1]]}"
        else:
            score = None
        results.append({
            "game_number": game_number,
            "map_name": map_name,
            "winner": winner,
            "score": score,
            "total_rounds": total_rounds,
        })
    return results


def kast_impact(
    db: EventDatabase,
    series_id: Optional[str] = None,
    series_ids: Optional[List[str]] = None,
    player_name: Optional[str] = None,
    min_deaths_no_kast: int = 5,
) -> List[Dict[str, Any]]:
    """Calculate KAST impact analysis."""
    if not series_ids and series_id:
        series_ids = [series_id]
    if not series_ids:
        return []
    series_clause = in_clause(series_ids)
    params: List[Any] = list(series_ids)
    player_filter = ""
    if player_name:
        player_filter = " AND prs.player_name ILIKE ?"
        params.append(f"%{player_name}%")
    sql = load_sql("kast_impact.sql").format(
        series_clause=series_clause,
        player_filter=player_filter,
    )
    params.append(min_deaths_no_kast)
    rows = db.query(sql, params)
    results = []
    for row in rows:
        denom = row[2] or 0
        num = row[3] or 0
        results.append({
            "player_name": row[0],
            "team_name": row[1],
            "deaths_without_kast": denom,
            "rounds_lost_when_no_kast": num,
            "loss_rate_when_no_kast": round(num / denom * 100, 1) if denom > 0 else 0.0,
        })
    return results


def opening_death_impact(
    db: EventDatabase,
    series_id: Optional[str] = None,
    series_ids: Optional[List[str]] = None,
    player_name: Optional[str] = None,
    min_opening_deaths: int = 3,
) -> List[Dict[str, Any]]:
    """Calculate opening death impact analysis."""
    if not series_ids and series_id:
        series_ids = [series_id]
    if not series_ids:
        return []
    series_clause = in_clause(series_ids)
    params: List[Any] = list(series_ids)
    player_filter = ""
    if player_name:
        player_filter = " AND prs.player_name ILIKE ?"
        params.append(f"%{player_name}%")
    sql = load_sql("opening_death_impact.sql").format(
        series_clause=series_clause,
        player_filter=player_filter,
    )
    params.append(min_opening_deaths)
    rows = db.query(sql, params)
    results = []
    for row in rows:
        denom = row[2] or 0
        lost = row[4] or 0
        results.append({
            "player_name": row[0],
            "team_name": row[1],
            "opening_deaths": denom,
            "rounds_lost_when_od": lost,
            "loss_rate_when_od": round(lost / denom * 100, 1) if denom > 0 else 0.0,
            "rounds_list": row[5] or [],
        })
    return results


def situation_benchmarks(
    db: EventDatabase,
    series_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Get historical benchmark rates for clutches, economy, and conversions.

    Computes baseline probabilities from historical data for LLM reference.
    If series_ids is provided, computes benchmarks from those series only.
    Otherwise, uses entire database.

    Args:
        db: Database connection.
        series_ids: Optional list of series IDs to compute benchmarks from.

    Returns:
        Dict with:
            - clutch_rates: Win rates by 1vX situation
            - economy_matchups: Win rates by buy type vs opponent buy type
            - first_blood_conversion: FB conversion rates by side
            - first_death_salvage: FD salvage rates by side
            - retake_rates: Retake success by defenders alive
            - sample_size: Total rounds/games/series in dataset
    """
    series_filter = ""
    params: List[Any] = []
    if series_ids:
        series_filter = f"AND g.series_id IN ({in_clause(series_ids)})"
        # SQL has 6 CTEs that each use series_filter, so repeat params 6 times
        params = list(series_ids) * 6

    sql = load_sql("situation_benchmarks.sql").format(series_filter=series_filter)
    rows = db.query(sql, params)

    # Parse results into structured dict
    benchmarks: Dict[str, Any] = {
        "clutch_rates": {},
        "economy_matchups": {},
        "first_blood_conversion": {},
        "first_death_salvage": {},
        "retake_rates": {},
        "sample_size": {},
    }

    for row in rows:
        stat_type, situation_key, secondary_key, denom, num, rate = row

        if stat_type is None:
            continue

        if stat_type == "clutch_stats" and situation_key is not None:
            key = f"1v{int(situation_key)}"
            benchmarks["clutch_rates"][key] = {
                "attempts": denom or 0,
                "wins": num or 0,
                "rate": float(rate) if rate is not None else 0.0,
            }
        elif stat_type == "economy_matchup":
            matchup_key = f"{situation_key}_vs_{secondary_key}"
            benchmarks["economy_matchups"][matchup_key] = {
                "rounds": denom or 0,
                "wins": num or 0,
                "rate": float(rate) if rate is not None else 0.0,
            }
        elif stat_type.startswith("fb_conversion_"):
            side = stat_type.replace("fb_conversion_", "")
            benchmarks["first_blood_conversion"][side] = {
                "total_fbs": denom or 0,
                "fb_wins": num or 0,
                "rate": float(rate) if rate is not None else 0.0,
            }
        elif stat_type.startswith("fd_salvage_"):
            side = stat_type.replace("fd_salvage_", "")
            benchmarks["first_death_salvage"][side] = {
                "total_fds": denom or 0,
                "fd_salvages": num or 0,
                "rate": float(rate) if rate is not None else 0.0,
            }
        elif stat_type == "retake":
            benchmarks["retake_rates"][situation_key] = {
                "attempts": denom or 0,
                "wins": num or 0,
                "rate": float(rate) if rate is not None else 0.0,
            }
        elif stat_type == "sample_size":
            benchmarks["sample_size"] = {
                "total_rounds": denom or 0,
                "total_games": num or 0,
                "total_series": int(rate) if rate is not None else 0,
            }

    return benchmarks
