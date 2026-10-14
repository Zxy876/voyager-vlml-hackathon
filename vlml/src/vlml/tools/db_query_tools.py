"""Database query tools for VLML - Fast SQL-based analytics using new schema."""
from typing import Any, Dict
from vlml.db.manager import EventDatabase


async def query_player_events_db(
    player_name: str, series_id: str = None, event_type: str = None
) -> Dict[str, Any]:
    """Query player events from database using SQL (NEW SCHEMA).

    Args:
        player_name: Player name to query
        series_id: Optional series ID to filter by
        event_type: Optional event type filter (e.g., 'player-killed-player')

    Returns:
        Dictionary with player events and statistics
    """
    try:
        with EventDatabase(read_only=True) as db:
            # Build SQL query using new schema (base_events table)
            sql = """
                SELECT
                    event_type,
                    occurred_at,
                    actor_player_name,
                    target_player_name,
                    actor_team_name,
                    target_team_name,
                    weapon_name,
                    is_kill,
                    is_death,
                    game_id,
                    round_id
                FROM base_events
                WHERE actor_player_name ILIKE ?
            """
            params = [f"%{player_name}%"]

            if series_id:
                sql += " AND series_id = ?"
                params.append(series_id)

            if event_type:
                sql += " AND event_type = ?"
                params.append(event_type)

            sql += " ORDER BY occurred_at"

            results = db.query(sql, params)

            # Process results
            events = []
            for row in results:
                events.append({
                    "event_type": row[0],
                    "occurred_at": str(row[1]),
                    "actor_name": row[2],
                    "target_name": row[3],
                    "actor_team": row[4],
                    "target_team": row[5],
                    "weapon": row[6],
                    "is_kill": row[7],
                    "is_death": row[8],
                    "game_id": row[9],
                    "round_id": row[10],
                })

            # Get kill/death counts from events
            kill_count = sum(1 for e in events if e["is_kill"])
            death_count = sum(1 for e in events if e["is_death"])

            return {
                "player_name": player_name,
                "series_id": series_id,
                "total_events": len(events),
                "kills": kill_count,
                "deaths": death_count,
                "kd_ratio": round(kill_count / death_count, 2) if death_count > 0 else kill_count,
                "events": events[:100],  # Limit to first 100 events
            }

    except Exception as e:
        return {"error": f"Database query failed: {str(e)}"}


async def query_player_patterns_db(
    player_name: str, num_series: int = 5
) -> Dict[str, Any]:
    """Analyze player patterns using NEW aggregation tables.

    Args:
        player_name: Player name
        num_series: Number of recent series to analyze

    Returns:
        Dictionary with pattern analysis
    """
    try:
        with EventDatabase(read_only=True) as db:
            # Check if aggregation tables exist
            check_sql = """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_name = 'agg_player_game_stats'
            """
            table_exists = db.query(check_sql)[0][0] > 0

            if not table_exists:
                return {
                    "error": "Aggregation tables not found. Please run: python database/scripts/orchestration/run_transformations.py"
                }

            # Get player's recent game stats using new schema
            # Need to join with games table to get series_id
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
                    pgs.game_started_at
                FROM agg_player_game_stats pgs
                LEFT JOIN games g ON pgs.game_id = g.game_id
                WHERE pgs.player_name ILIKE ?
                ORDER BY pgs.game_started_at DESC
                LIMIT ?
            """
            games = db.query(sql, [f"%{player_name}%", num_series * 3])  # Get enough games

            if not games:
                return {
                    "error": f"No data found for player '{player_name}' in database"
                }

            # Calculate aggregated stats
            total_kills = sum(g[3] for g in games)
            total_deaths = sum(g[4] for g in games)
            total_assists = sum(g[5] for g in games)
            total_rounds = sum(g[2] for g in games)
            total_first_bloods = sum(g[10] for g in games)
            total_first_deaths = sum(g[11] for g in games)
            games_played = len(games)

            avg_kills = round(total_kills / games_played, 1)
            avg_deaths = round(total_deaths / games_played, 1)
            avg_assists = round(total_assists / games_played, 1)
            avg_kast = round(sum(g[9] or 0 for g in games) / games_played * 100, 1) if games_played > 0 else 0
            avg_adr = round(sum(g[7] or 0 for g in games) / games_played, 1)
            kd_ratio = round(total_kills / total_deaths, 2) if total_deaths > 0 else total_kills

            # Identify patterns
            weak_spots = []
            strong_spots = []
            recommendations = []

            # K/D Analysis
            if kd_ratio < 0.9:
                weak_spots.append(f"Below-average K/D ratio: {kd_ratio}")
                recommendations.append("Focus on staying alive - you're dying more than killing")
            elif kd_ratio >= 1.2:
                strong_spots.append(f"Strong K/D ratio: {kd_ratio}")

            # Frag output
            if avg_kills < 15.0:
                weak_spots.append(f"Low frag output: {avg_kills:.1f} avg kills per game")
                recommendations.append("Work on mechanical aim and positioning for more kills")
            elif avg_kills >= 18.0:
                strong_spots.append(f"High frag output: {avg_kills:.1f} kills/game")

            # ADR analysis
            if avg_adr < 130:
                weak_spots.append(f"Low ADR: {avg_adr:.1f}")
                recommendations.append("Increase damage output - play more aggressive or improve utility usage")
            elif avg_adr >= 160:
                strong_spots.append(f"Strong ADR: {avg_adr:.1f}")

            # KAST analysis
            if avg_kast < 60:
                weak_spots.append(f"Low KAST: {avg_kast:.1f}%")
                recommendations.append("Improve round impact - get more kills/assists or survive longer")
            elif avg_kast >= 70:
                strong_spots.append(f"Excellent KAST: {avg_kast:.1f}%")

            # First blood analysis
            fk_fd_diff = total_first_bloods - total_first_deaths
            if fk_fd_diff < -5:
                weak_spots.append(f"Negative FK/FD differential: {fk_fd_diff} ({total_first_bloods}FK/{total_first_deaths}FD)")
                recommendations.append("Dying first too often - improve entry dueling or play less aggressive")
            elif fk_fd_diff > 5:
                strong_spots.append(f"Positive FK/FD: +{fk_fd_diff} ({total_first_bloods}FK/{total_first_deaths}FD)")

            return {
                "player_name": player_name,
                "games_analyzed": games_played,
                "aggregated_stats": {
                    "kd_ratio": kd_ratio,
                    "avg_kills_per_game": avg_kills,
                    "avg_deaths_per_game": avg_deaths,
                    "avg_assists_per_game": avg_assists,
                    "avg_adr": avg_adr,
                    "avg_kast_percentage": avg_kast,
                    "total_rounds": total_rounds,
                    "fk_fd_differential": fk_fd_diff,
                },
                "weak_spots": weak_spots,
                "strong_spots": strong_spots,
                "recommendations": recommendations,
                "recent_games": [
                    {
                        "game_id": g[0],
                        "team": g[12],
                        "opponent": g[13],
                        "kills": g[3],
                        "deaths": g[4],
                        "assists": g[5],
                        "kd": round(g[6], 2) if g[6] else 0,
                        "adr": round(g[7], 1) if g[7] else 0,
                        "kast": round(g[9] * 100, 1) if g[9] else 0,
                    }
                    for g in games[:10]
                ],
            }

    except Exception as e:
        return {"error": f"Database query failed: {str(e)}"}


async def execute_custom_sql(sql_query: str) -> Dict[str, Any]:
    """Execute a custom SQL query on the event database.

    Args:
        sql_query: SQL query to execute (SELECT only)

    Returns:
        Query results as a dictionary
    """
    try:
        # Security: Only allow SELECT queries
        if not sql_query.strip().upper().startswith("SELECT"):
            return {"error": "Only SELECT queries are allowed"}

        with EventDatabase(read_only=True) as db:
            # Execute query and get column names
            result_obj = db.conn.execute(sql_query)
            columns = [desc[0] for desc in result_obj.description]
            rows = result_obj.fetchall()

            # Convert to dict
            result = {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
            }

            return result

    except Exception as e:
        return {"error": f"Query execution failed: {str(e)}"}


async def get_database_info() -> Dict[str, Any]:
    """Get information about the event database.

    Returns:
        Database statistics and schema information
    """
    try:
        with EventDatabase(read_only=True) as db:
            stats = db.get_database_stats()
            series_list = db.get_series_list()

            # Get sample data
            sample_games = db.query("""
                SELECT
                    player_name,
                    team_name,
                    kills,
                    deaths,
                    kd_ratio,
                    adr,
                    kast_percentage
                FROM agg_player_game_stats
                ORDER BY game_started_at DESC
                LIMIT 5
            """)

            return {
                "database_path": db.db_path,
                "statistics": stats,
                "recent_series": series_list[:10],
                "available_tables": [
                    "series - Series/match metadata",
                    "games - Game/map metadata",
                    "rounds - Round metadata",
                    "base_events - Raw event data (500K+ events)",
                    "agg_player_round_stats - Player stats per round (55K+ rows)",
                    "agg_player_game_stats - Player stats per game (2.6K+ rows)",
                    "agg_player_series_stats - Player stats per series (1K+ rows)",
                    "agent_roles - Agent role reference table",
                    "weapon_types - Weapon type reference table",
                ],
                "sample_recent_stats": [
                    {
                        "player": row[0],
                        "team": row[1],
                        "K": row[2],
                        "D": row[3],
                        "K/D": round(row[4], 2) if row[4] else 0,
                        "ADR": round(row[5], 1) if row[5] else 0,
                        "KAST%": round(row[6] * 100, 1) if row[6] else 0,
                    }
                    for row in sample_games
                ],
                "usage_tips": [
                    "Use agg_player_game_stats for fast player performance queries",
                    "Use base_events for detailed event-level analysis",
                    "All tables have team_name populated (91% coverage)",
                    "Metrics include: K/D, ADR, KAST%, first bloods, multi-kills",
                ]
            }

    except Exception as e:
        return {"error": f"Failed to get database info: {str(e)}"}
