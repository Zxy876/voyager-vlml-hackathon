"""VLML MCP Server - Valorant esports analytics."""
from mcp.server.fastmcp import FastMCP
from vlml.tools import insights_tools, db_query_tools

# Initialize FastMCP server
mcp = FastMCP("vlml-valorant-analytics")

# ===== Insights Tools =====

@mcp.tool()
async def match_analysis_report(series_id: str, team_name: str | None = None, map_name: str | None = None) -> dict:
    """Generate a match analysis report for a single series."""
    return await insights_tools.match_analysis_report(series_id, team_name, map_name)

@mcp.tool()
async def match_summary_report(series_id: str, team_name: str | None = None, map_name: str | None = None) -> dict:
    """Generate a lightweight match summary (metadata, team comparison, key metrics, benchmarks). Call this first for overview."""
    return await insights_tools.match_summary_report(series_id, team_name, map_name)

@mcp.tool()
async def match_players_report(series_id: str, team_name: str | None = None, map_name: str | None = None) -> dict:
    """Generate a player-focused report (performance stats, KAST impact, highlight rounds). Use for player analysis."""
    return await insights_tools.match_players_report(series_id, team_name, map_name)

@mcp.tool()
async def match_rounds_report(
    series_id: str,
    team_name: str | None = None,
    map_name: str | None = None,
    round_start: int | None = None,
    round_end: int | None = None,
) -> dict:
    """Generate a round-by-round report (timeline, situations, half breakdown). Heavy - use pagination for large datasets."""
    return await insights_tools.match_rounds_report(series_id, team_name, map_name, round_start, round_end)

@mcp.tool()
async def match_economy_report(series_id: str, team_name: str | None = None, map_name: str | None = None) -> dict:
    """Generate an economy/tactical report (economy context, attack patterns). Use for economy cascade or predictability analysis."""
    return await insights_tools.match_economy_report(series_id, team_name, map_name)

@mcp.tool()
async def player_profile_report(
    player_name: str,
    series_ids: list[str] | None = None,
    last_n_series: int = 5,
    map_name: str | None = None,
    agent_name: str | None = None,
) -> dict:
    """Generate a player profile report over multiple series."""
    return await insights_tools.player_profile_report(
        player_name=player_name,
        series_ids=series_ids,
        last_n_series=last_n_series,
        map_name=map_name,
        agent_name=agent_name,
    )

@mcp.tool()
async def scouting_report(
    team_name: str,
    series_ids: list[str] | None = None,
    last_n_series: int = 5,
    map_name: str | None = None,
) -> dict:
    """Generate a scouting report for an opponent team."""
    return await insights_tools.scouting_report(
        team_name=team_name,
        series_ids=series_ids,
        last_n_series=last_n_series,
        map_name=map_name,
    )

@mcp.tool()
async def pattern_detection_report(
    team_name: str | None = None,
    player_name: str | None = None,
    tournament_name: str | None = None,
    series_ids: list[str] | None = None,
    min_rounds: int = 200,
) -> dict:
    """Detect recurring patterns across a large dataset."""
    return await insights_tools.pattern_detection_report(
        team_name=team_name,
        player_name=player_name,
        tournament_name=tournament_name,
        series_ids=series_ids,
        min_rounds=min_rounds,
    )

# ===== Database Tools =====

@mcp.tool()
async def query_sql(sql_query: str) -> dict:
    """Execute a custom SQL query on the event database (SELECT only)."""
    return await db_query_tools.execute_custom_sql(sql_query)

@mcp.tool()
async def get_database_info() -> dict:
    """Get information about the event database schema and stats."""
    return await db_query_tools.get_database_info()

def main():
    """Run the MCP server."""
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
