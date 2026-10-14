"""Smoke tests for insights tools against local DuckDB data."""
import pytest

from vlml.db.manager import EventDatabase
from vlml.tools import insights_tools


def _get_any_series_id(db: EventDatabase) -> str | None:
    row = db.query("SELECT series_id FROM series ORDER BY start_time DESC LIMIT 1")
    return row[0][0] if row else None


def _get_any_player(db: EventDatabase) -> str | None:
    row = db.query(
        "SELECT player_name FROM agg_player_game_stats WHERE player_name IS NOT NULL LIMIT 1"
    )
    return row[0][0] if row else None


def _get_any_team(db: EventDatabase) -> str | None:
    row = db.query(
        "SELECT team_name FROM agg_team_game_stats WHERE team_name IS NOT NULL LIMIT 1"
    )
    return row[0][0] if row else None


@pytest.mark.asyncio
async def test_match_analysis_report_smoke():
    with EventDatabase(read_only=True) as db:
        series_id = _get_any_series_id(db)
    if not series_id:
        pytest.skip("No series data available for smoke test.")

    result = await insights_tools.match_analysis_report(series_id)
    assert result.get("report_type") == "match_analysis"
    assert "key_metrics" in result


@pytest.mark.asyncio
async def test_player_profile_report_smoke():
    with EventDatabase(read_only=True) as db:
        player_name = _get_any_player(db)
    if not player_name:
        pytest.skip("No player data available for smoke test.")

    result = await insights_tools.player_profile_report(player_name, last_n_series=3)
    assert result.get("report_type") == "player_profile"
    assert "career_stats" in result


@pytest.mark.asyncio
async def test_scouting_report_smoke():
    with EventDatabase(read_only=True) as db:
        team_name = _get_any_team(db)
    if not team_name:
        pytest.skip("No team data available for smoke test.")

    result = await insights_tools.scouting_report(team_name, last_n_series=3)
    assert result.get("report_type") == "scouting_report"
    assert "map_pool" in result


@pytest.mark.asyncio
async def test_pattern_detection_report_smoke():
    with EventDatabase(read_only=True) as db:
        team_name = _get_any_team(db)
    if not team_name:
        pytest.skip("No team data available for smoke test.")

    result = await insights_tools.pattern_detection_report(team_name=team_name, min_rounds=1)
    assert result.get("report_type") == "pattern_detection"
    assert "key_metrics" in result
