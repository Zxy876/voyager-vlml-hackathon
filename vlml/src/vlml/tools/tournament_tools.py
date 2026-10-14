"""Tournament and live data tools."""
from typing import Dict, Any, Optional
from vlml.client.grid_client import GRIDClient
from vlml.client.queries import (
    GET_TOURNAMENTS,
    GET_TOURNAMENT_STANDINGS,
    GET_RECENT_SERIES,
)
from vlml.models.tournament import Tournament, TournamentStandings, TeamStanding


async def list_tournaments(
    client: GRIDClient,
    region: Optional[str] = None,
    status: str = "ongoing",
    year: Optional[int] = None,
) -> Dict[str, Any]:
    """List Valorant tournaments (VCT, Challengers, etc.).

    Args:
        client: GRID GraphQL client
        region: Filter by region (Americas, EMEA, Pacific, China)
        status: Tournament status - "ongoing", "upcoming", "completed"
        year: Filter by year (not implemented in query yet)

    Returns:
        Dictionary containing tournament list
    """
    try:
        result = await client.execute(
            GET_TOURNAMENTS, variables={"status": status.upper(), "region": region}
        )

        tournament_data = result.get("tournaments", {}).get("edges", [])

        tournaments = []
        for tournament_edge in tournament_data:
            tournament_node = tournament_edge["node"]

            tournament = Tournament(
                tournament_id=tournament_node["id"],
                name=tournament_node["name"],
                region=tournament_node.get("region"),
                start_time=tournament_node.get("startTime"),
                end_time=tournament_node.get("endTime"),
                status=tournament_node.get("status"),
                prize_pool=tournament_node.get("prizePool"),
            )
            tournaments.append(tournament)

        return {"tournaments": [t.model_dump() for t in tournaments]}

    except Exception as e:
        return {"error": f"Failed to list tournaments: {str(e)}"}


async def get_tournament_standings(
    client: GRIDClient, tournament_name: str, tournament_id: Optional[str] = None
) -> Dict[str, Any]:
    """Get current standings/brackets for a tournament.

    Args:
        client: GRID GraphQL client
        tournament_name: Tournament name
        tournament_id: Optional GRID tournament ID

    Returns:
        Dictionary containing tournament standings
    """
    try:
        # If tournament_id not provided, search for tournament by name
        if not tournament_id:
            # Search for tournament by name (simplified - may need more complex logic)
            tournaments = await list_tournaments(client)
            if "error" in tournaments:
                return tournaments

            # Find matching tournament
            for tournament in tournaments.get("tournaments", []):
                if tournament_name.lower() in tournament["name"].lower():
                    tournament_id = tournament["tournament_id"]
                    break

            if not tournament_id:
                return {"error": f"Tournament '{tournament_name}' not found"}

        result = await client.execute(
            GET_TOURNAMENT_STANDINGS, variables={"tournamentId": tournament_id}
        )

        tournament_data = result.get("tournament")

        if not tournament_data:
            return {"error": f"Tournament standings not found"}

        standings_data = tournament_data.get("standings", [])

        standings = []
        for standing_item in standings_data:
            standing = TeamStanding(
                rank=standing_item.get("rank", 0),
                team_id=standing_item.get("team", {}).get("id", ""),
                team_name=standing_item.get("team", {}).get("name", ""),
                matches_played=standing_item.get("matchesPlayed", 0),
                matches_won=standing_item.get("matchesWon", 0),
                matches_lost=standing_item.get("matchesLost", 0),
                points=standing_item.get("points", 0),
            )
            standings.append(standing)

        tournament_standings = TournamentStandings(
            tournament_id=tournament_id,
            tournament_name=tournament_data.get("name", ""),
            standings=standings,
        )

        return tournament_standings.model_dump()

    except Exception as e:
        return {"error": f"Failed to get tournament standings: {str(e)}"}


async def get_recent_series(
    client: GRIDClient, limit: int = 5, region: Optional[str] = None
) -> Dict[str, Any]:
    """Get most recent completed series across all tournaments.

    Args:
        client: GRID GraphQL client
        limit: Number of series to return
        region: Filter by region (optional)

    Returns:
        Dictionary containing recent series
    """
    try:
        result = await client.execute(
            GET_RECENT_SERIES, variables={"limit": limit, "region": region}
        )

        series_data = result.get("series", {}).get("edges", [])

        series_list = []
        for series_edge in series_data:
            series_node = series_edge["node"]

            series_list.append(
                {
                    "series_id": series_node["id"],
                    "start_time": series_node.get("startTime"),
                    "end_time": series_node.get("endTime"),
                    "tournament": series_node.get("tournament", {}).get("name"),
                    "teams": [
                        {
                            "name": team.get("team", {}).get("name", ""),
                            "score": team.get("score", 0),
                            "winner": team.get("isWinner", False),
                        }
                        for team in series_node.get("teams", [])
                    ],
                }
            )

        return {"series": series_list}

    except Exception as e:
        return {"error": f"Failed to get recent series: {str(e)}"}


async def get_upcoming_matches(
    client: GRIDClient,
    team_name: Optional[str] = None,
    tournament_name: Optional[str] = None,
    days_ahead: int = 7,
) -> Dict[str, Any]:
    """Get scheduled upcoming matches.

    Args:
        client: GRID GraphQL client
        team_name: Filter by team (optional, not fully implemented)
        tournament_name: Filter by tournament (optional, not fully implemented)
        days_ahead: How many days to look ahead

    Returns:
        Dictionary containing upcoming matches

    Note: This is a placeholder implementation. The actual query needs
    to filter by scheduled state and time range, which requires
    the exact GRID API schema.
    """
    try:
        # This is a simplified implementation
        # The actual implementation would need proper filtering by scheduled state
        # and time range based on the GRID schema

        return {
            "message": "Upcoming matches feature requires GRID schema exploration",
            "note": "Please check the GraphQL playground to implement proper filtering for scheduled matches",
        }

    except Exception as e:
        return {"error": f"Failed to get upcoming matches: {str(e)}"}
