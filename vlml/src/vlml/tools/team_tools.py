"""Team analysis tools."""
from typing import Dict, Any, Optional
from vlml.client.grid_client import GRIDClient
from vlml.client.queries import (
    GET_TEAM_BY_NAME,
    GET_TEAM_STATS,
    GET_TEAM_MAP_STATS,
)
from vlml.models.team import (
    TeamInfo,
    TeamPlayer,
    TeamStatistics,
    TeamMapPerformance,
    MapStats,
)


async def get_team_info(
    client: GRIDClient, team_name: str, team_id: Optional[str] = None
) -> Dict[str, Any]:
    """Get team roster and basic information.

    Args:
        client: GRID GraphQL client
        team_name: Team name
        team_id: Optional GRID team ID for exact matching

    Returns:
        Dictionary containing team information
    """
    try:
        result = await client.execute(GET_TEAM_BY_NAME, variables={"name": team_name})

        teams = result.get("teams", {}).get("edges", [])

        if not teams:
            return {"error": f"Team '{team_name}' not found"}

        # Get first matching team
        team_node = teams[0]["node"]

        # Parse players
        players_data = team_node.get("players", [])
        players = [
            TeamPlayer(
                player_id=p.get("id", ""),
                nickname=p.get("nickname", ""),
                role=p.get("role"),
            )
            for p in players_data
        ]

        team_info = TeamInfo(
            team_id=team_node["id"],
            name=team_node["name"],
            short_name=team_node.get("shortName"),
            region=team_node.get("region"),
            players=players,
        )

        return team_info.model_dump()

    except Exception as e:
        return {"error": f"Failed to get team info: {str(e)}"}


async def get_team_stats(
    client: GRIDClient, team_name: str, time_range: str = "recent"
) -> Dict[str, Any]:
    """Get comprehensive team performance statistics.

    Args:
        client: GRID GraphQL client
        team_name: Team name
        time_range: Time window (currently not used)

    Returns:
        Dictionary containing team statistics
    """
    try:
        # First find the team
        result = await client.execute(GET_TEAM_BY_NAME, variables={"name": team_name})

        teams = result.get("teams", {}).get("edges", [])

        if not teams:
            return {"error": f"Team '{team_name}' not found"}

        team_node = teams[0]["node"]
        team_id = team_node["id"]

        # Get team stats
        stats_result = await client.execute(GET_TEAM_STATS, variables={"teamId": team_id})

        team_data = stats_result.get("team", {})
        stats = team_data.get("statistics", {})

        team_stats = TeamStatistics(
            team_id=team_id,
            name=team_node["name"],
            matches_played=stats.get("matchesPlayed", 0),
            matches_won=stats.get("matchesWon", 0),
            matches_lost=stats.get("matchesLost", 0),
            win_rate=stats.get("winRate", 0.0),
            rounds_played=stats.get("roundsPlayed", 0),
            rounds_won=stats.get("roundsWon", 0),
            rounds_lost=stats.get("roundsLost", 0),
            attack_round_win_rate=stats.get("attackRoundWinRate", 0.0),
            defense_round_win_rate=stats.get("defenseRoundWinRate", 0.0),
        )

        return team_stats.model_dump()

    except Exception as e:
        return {"error": f"Failed to get team stats: {str(e)}"}


async def get_team_map_performance(
    client: GRIDClient, team_name: str, map_name: Optional[str] = None
) -> Dict[str, Any]:
    """Get team's map-specific performance statistics.

    Args:
        client: GRID GraphQL client
        team_name: Team name
        map_name: Specific map (optional, None = all maps)

    Returns:
        Dictionary containing map performance data
    """
    try:
        # First find the team
        result = await client.execute(GET_TEAM_BY_NAME, variables={"name": team_name})

        teams = result.get("teams", {}).get("edges", [])

        if not teams:
            return {"error": f"Team '{team_name}' not found"}

        team_node = teams[0]["node"]
        team_id = team_node["id"]

        # Get map stats
        map_result = await client.execute(GET_TEAM_MAP_STATS, variables={"teamId": team_id})

        team_data = map_result.get("team", {})
        map_stats_data = team_data.get("mapStatistics", [])

        maps = []
        for map_stat in map_stats_data:
            map_obj = map_stat.get("map", {})
            map_stat_obj = MapStats(
                map_name=map_obj.get("name", "Unknown"),
                played=map_stat.get("played", 0),
                won=map_stat.get("won", 0),
                lost=map_stat.get("lost", 0),
                win_rate=map_stat.get("winRate", 0.0),
                attack_round_win_rate=map_stat.get("attackRoundWinRate", 0.0),
                defense_round_win_rate=map_stat.get("defenseRoundWinRate", 0.0),
                pick_rate=map_stat.get("pickRate"),
                ban_rate=map_stat.get("banRate"),
            )

            # Filter by map if specified
            if map_name is None or map_stat_obj.map_name.lower() == map_name.lower():
                maps.append(map_stat_obj)

        performance = TeamMapPerformance(
            team_id=team_id, name=team_node["name"], maps=maps
        )

        return performance.model_dump()

    except Exception as e:
        return {"error": f"Failed to get map performance: {str(e)}"}


async def compare_teams(
    client: GRIDClient, team_a: str, team_b: str
) -> Dict[str, Any]:
    """Compare two teams head-to-head.

    Args:
        client: GRID GraphQL client
        team_a: First team name
        team_b: Second team name

    Returns:
        Dictionary containing team comparison
    """
    try:
        team_a_stats = await get_team_stats(client, team_a)
        team_b_stats = await get_team_stats(client, team_b)

        if "error" in team_a_stats:
            return team_a_stats
        if "error" in team_b_stats:
            return team_b_stats

        return {"team_a": team_a_stats, "team_b": team_b_stats}

    except Exception as e:
        return {"error": f"Failed to compare teams: {str(e)}"}
