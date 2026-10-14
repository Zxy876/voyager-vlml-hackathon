"""Player statistics tools using DuckDB backend."""
from typing import Dict, Any, Optional, List
from vlml.db.manager import EventDatabase
from vlml.client.grid_client import GRIDClient

# We keep GRIDClient for basic info lookup if needed, but prioritize DB for stats

async def get_player_info(
    client: GRIDClient, player_name: str
) -> Dict[str, Any]:
    """Get basic player information.
    
    Tries to find player in local DB first, falls back to API.
    """
    # Try DB first
    try:
        with EventDatabase(read_only=True) as db:
            result = db.query(
                "SELECT DISTINCT player_name, team_name FROM agg_player_game_stats WHERE player_name ILIKE ? LIMIT 1",
                [f"%{player_name}%"]
            )
            if result:
                return {
                    "nickname": result[0][0],
                    "team_name": result[0][1],
                    "source": "database"
                }
    except Exception:
        pass

    # Fallback to API (simplified for now, or just return error if we want to force DB)
    return {"error": f"Player '{player_name}' not found in database. Please ensure ETL pipeline has run."}


async def get_player_stats(
    player_name: str, num_matches: int = 5
) -> Dict[str, Any]:
    """Get comprehensive player statistics from the database.
    
    Args:
        player_name: Player's in-game name
        num_matches: Number of recent matches to analyze
    """
    try:
        with EventDatabase(read_only=True) as db:
            # Get player's recent game stats
            sql = """
                SELECT
                    pgs.game_id,
                    g.series_id,
                    pgs.rounds_played,
                    pgs.kills,
                    pgs.deaths,
                    pgs.assists,
                    pgs.kd_ratio,
                    pgs.adr,
                    pgs.kpr,
                    pgs.kast_percentage,
                    pgs.first_bloods,
                    pgs.first_deaths,
                    pgs.team_name,
                    pgs.opponent_team_name,
                    pgs.game_started_at,
                    pgs.agent_name
                FROM agg_player_game_stats pgs
                LEFT JOIN games g ON pgs.game_id = g.game_id
                WHERE pgs.player_name ILIKE ?
                ORDER BY pgs.game_started_at DESC
                LIMIT ?
            """
            games = db.query(sql, [f"%{player_name}%", num_matches])

            if not games:
                return {"error": f"No data found for player '{player_name}'"}

            # Calculate aggregated stats
            total_kills = sum(g[3] for g in games)
            total_deaths = sum(g[4] for g in games)
            total_assists = sum(g[5] for g in games)
            total_rounds = sum(g[2] for g in games)
            games_played = len(games)

            avg_kills = round(total_kills / games_played, 1)
            avg_deaths = round(total_deaths / games_played, 1)
            avg_assists = round(total_assists / games_played, 1)
            avg_adr = round(sum(g[7] or 0 for g in games) / games_played, 1)
            kd_ratio = round(total_kills / total_deaths, 2) if total_deaths > 0 else total_kills

            # Agent usage
            agents = {}
            for g in games:
                agent = g[15] or "Unknown"
                agents[agent] = agents.get(agent, 0) + 1

            return {
                "player_name": games[0][12], # Use name from DB
                "team_name": games[0][12],
                "matches_analyzed": games_played,
                "stats": {
                    "kd_ratio": kd_ratio,
                    "avg_kills": avg_kills,
                    "avg_deaths": avg_deaths,
                    "avg_assists": avg_assists,
                    "avg_adr": avg_adr,
                },
                "agent_usage": agents,
                "recent_matches": [
                    {
                        "date": str(g[14])[:10],
                        "opponent": g[13],
                        "agent": g[15],
                        "kda": f"{g[3]}/{g[4]}/{g[5]}",
                        "adr": round(g[7], 1) if g[7] else 0
                    }
                    for g in games
                ]
            }

    except Exception as e:
        return {"error": f"Database query failed: {str(e)}"}



