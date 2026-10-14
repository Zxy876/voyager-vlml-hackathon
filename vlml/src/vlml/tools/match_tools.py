"""Match history and details tools using DuckDB aggregates."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from vlml.db.manager import EventDatabase


def _series_list_for_team(db: EventDatabase, team_name: str, limit: int) -> List[str]:
    sql = """
        SELECT series_id, MAX(game_started_at) AS last_game
        FROM agg_team_game_stats
        WHERE team_name ILIKE ?
        GROUP BY series_id
        ORDER BY last_game DESC NULLS LAST
        LIMIT ?
    """
    rows = db.query(sql, [f"%{team_name}%", limit])
    return [row[0] for row in rows]


async def get_match_history(
    team_name: Optional[str] = None,
    limit: int = 10,
    tournament_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """Get recent match history for a team or overall (DB-backed)."""
    with EventDatabase(read_only=True) as db:
        params: List[Any] = []
        where_clause = ""
        if team_name:
            series_ids = _series_list_for_team(db, team_name, limit)
            if not series_ids:
                return {"error": f"Team '{team_name}' not found in database"}
            series_clause = ", ".join(["?"] * len(series_ids))
            where_clause = f"WHERE s.series_id IN ({series_clause})"
            params.extend(series_ids)
        if tournament_filter:
            where_clause += " AND s.tournament_name ILIKE ?" if where_clause else "WHERE s.tournament_name ILIKE ?"
            params.append(f"%{tournament_filter}%")

        sql = f"""
            SELECT
                s.series_id,
                s.start_time,
                s.tournament_name,
                s.team1_name,
                s.team2_name,
                s.winning_team_name
            FROM series s
            {where_clause}
            ORDER BY s.start_time DESC NULLS LAST
            LIMIT ?
        """
        params.append(limit)
        rows = db.query(sql, params)

        matches = []
        for row in rows:
            series_id, start_time, tournament_name, team1_name, team2_name, winning_team = row
            matches.append({
                "series_id": series_id,
                "start_time": str(start_time) if start_time else None,
                "tournament": tournament_name,
                "teams": [
                    {"team_name": team1_name, "is_winner": team1_name == winning_team},
                    {"team_name": team2_name, "is_winner": team2_name == winning_team},
                ],
            })

        return {"matches": matches}


async def get_match_details(series_id: str) -> Dict[str, Any]:
    """Get match details for a series (maps, teams, scores)."""
    with EventDatabase(read_only=True) as db:
        series_sql = """
            SELECT series_id, tournament_name, team1_name, team2_name, winning_team_name, start_time
            FROM series
            WHERE series_id = ?
        """
        series_rows = db.query(series_sql, [series_id])
        if not series_rows:
            return {"error": f"Series '{series_id}' not found"}
        series_row = series_rows[0]

        games_sql = """
            SELECT game_id, map_name, team1_name, team2_name, winning_team_name
            FROM games
            WHERE series_id = ?
            ORDER BY game_number
        """
        games_rows = db.query(games_sql, [series_id])

        return {
            "series_id": series_row[0],
            "tournament": series_row[1],
            "start_time": str(series_row[5]) if series_row[5] else None,
            "teams": [
                {"team_name": series_row[2], "is_winner": series_row[2] == series_row[4]},
                {"team_name": series_row[3], "is_winner": series_row[3] == series_row[4]},
            ],
            "maps": [
                {
                    "game_id": g[0],
                    "map_name": g[1],
                    "teams": [
                        {"team_name": g[2], "is_winner": g[2] == g[4]},
                        {"team_name": g[3], "is_winner": g[3] == g[4]},
                    ],
                }
                for g in games_rows
            ],
        }


async def get_head_to_head(
    team_a: str,
    team_b: str,
    limit: int = 5,
) -> Dict[str, Any]:
    """Get head-to-head matchup history between two teams (DB-backed)."""
    with EventDatabase(read_only=True) as db:
        sql = """
            SELECT series_id, start_time, team1_name, team2_name, winning_team_name
            FROM series
            WHERE ((team1_name ILIKE ? AND team2_name ILIKE ?) OR (team1_name ILIKE ? AND team2_name ILIKE ?))
            ORDER BY start_time DESC NULLS LAST
            LIMIT ?
        """
        params = [f"%{team_a}%", f"%{team_b}%", f"%{team_b}%", f"%{team_a}%", limit]
        rows = db.query(sql, params)

        if not rows:
            return {"message": f"No recent matches found between {team_a} and {team_b}"}

        team_a_wins = 0
        team_b_wins = 0
        matches = []
        for row in rows:
            series_id, start_time, team1, team2, winner = row
            if winner:
                if team_a.lower() in winner.lower():
                    team_a_wins += 1
                elif team_b.lower() in winner.lower():
                    team_b_wins += 1
            matches.append({
                "series_id": series_id,
                "start_time": str(start_time) if start_time else None,
                "teams": [
                    {"team_name": team1, "is_winner": team1 == winner},
                    {"team_name": team2, "is_winner": team2 == winner},
                ],
            })

        return {
            "team_a": team_a,
            "team_b": team_b,
            "team_a_wins": team_a_wins,
            "team_b_wins": team_b_wins,
            "total_matches": len(matches),
            "matches": matches,
        }
