"""Unit tests for common/data_fetch.py using mock database."""
import pytest

from vlml.tools.common.data_fetch import (
    fetch_series_ids_for_player,
    fetch_series_ids_for_team,
    kast_impact,
    opening_death_impact,
    series_games,
    series_metadata,
)


class TestFetchSeriesIdsForTeam:
    """Tests for fetch_series_ids_for_team function."""

    def test_returns_series_for_team(self, mock_db):
        """Returns series IDs for a matching team."""
        result = fetch_series_ids_for_team(mock_db, "Team Alpha", last_n_series=10)
        assert isinstance(result, list)
        assert len(result) >= 1
        assert "series-001" in result or "series-002" in result

    def test_returns_empty_for_unknown_team(self, mock_db):
        """Returns empty list for unknown team."""
        result = fetch_series_ids_for_team(mock_db, "Unknown Team", last_n_series=10)
        assert result == []

    def test_respects_limit(self, mock_db):
        """Respects the last_n_series limit."""
        result = fetch_series_ids_for_team(mock_db, "Team Alpha", last_n_series=1)
        assert len(result) <= 1

    def test_partial_match(self, mock_db):
        """Matches teams with partial name (ILIKE)."""
        result = fetch_series_ids_for_team(mock_db, "Alpha", last_n_series=10)
        assert len(result) >= 1


class TestFetchSeriesIdsForPlayer:
    """Tests for fetch_series_ids_for_player function."""

    def test_returns_series_for_player(self, mock_db):
        """Returns series IDs for a matching player."""
        result = fetch_series_ids_for_player(mock_db, "Alice", last_n_series=10)
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_returns_empty_for_unknown_player(self, mock_db):
        """Returns empty list for unknown player."""
        result = fetch_series_ids_for_player(mock_db, "Unknown Player", last_n_series=10)
        assert result == []

    def test_respects_limit(self, mock_db):
        """Respects the last_n_series limit."""
        result = fetch_series_ids_for_player(mock_db, "Alice", last_n_series=1)
        assert len(result) <= 1


class TestSeriesMetadata:
    """Tests for series_metadata function."""

    def test_returns_metadata_dict(self, mock_db):
        """Returns correct metadata for existing series."""
        result = series_metadata(mock_db, "series-001")
        assert isinstance(result, dict)
        assert result["series_id"] == "series-001"
        assert result["tournament_name"] == "VCT Masters"
        assert result["team1"] == "Team Alpha"
        assert result["team2"] == "Team Beta"
        assert result["winner"] == "Team Alpha"

    def test_returns_empty_for_unknown_series(self, mock_db):
        """Returns empty dict for unknown series."""
        result = series_metadata(mock_db, "unknown-series")
        assert result == {}

    def test_date_format(self, mock_db):
        """Date is formatted as YYYY-MM-DD string."""
        result = series_metadata(mock_db, "series-001")
        assert result["date"] is not None
        assert len(result["date"]) == 10  # YYYY-MM-DD format


class TestSeriesGames:
    """Tests for series_games function."""

    def test_returns_games_list(self, mock_db):
        """Returns list of games for series."""
        result = series_games(mock_db, "series-001")
        assert isinstance(result, list)
        assert len(result) == 2  # game-001 and game-002

    def test_game_structure(self, mock_db):
        """Each game has correct structure."""
        result = series_games(mock_db, "series-001")
        game = result[0]
        assert "game_number" in game
        assert "map_name" in game
        assert "winner" in game
        assert "score" in game
        assert "total_rounds" in game

    def test_game_order(self, mock_db):
        """Games are ordered by game_number."""
        result = series_games(mock_db, "series-001")
        assert result[0]["game_number"] == 1
        assert result[1]["game_number"] == 2

    def test_returns_empty_for_unknown_series(self, mock_db):
        """Returns empty list for unknown series."""
        result = series_games(mock_db, "unknown-series")
        assert result == []


class TestKastImpact:
    """Tests for kast_impact function."""

    def test_returns_list(self, mock_db):
        """Returns list of impact analysis."""
        result = kast_impact(mock_db, series_id="series-001", min_deaths_no_kast=0)
        assert isinstance(result, list)

    def test_returns_empty_without_series(self, mock_db):
        """Returns empty list when no series IDs provided."""
        result = kast_impact(mock_db, series_id=None, series_ids=None)
        assert result == []

    def test_accepts_series_ids_list(self, mock_db):
        """Accepts list of series IDs."""
        result = kast_impact(
            mock_db,
            series_ids=["series-001", "series-002"],
            min_deaths_no_kast=0
        )
        assert isinstance(result, list)

    def test_result_structure(self, mock_db):
        """Each result has correct structure."""
        result = kast_impact(mock_db, series_id="series-001", min_deaths_no_kast=0)
        if result:
            item = result[0]
            assert "player_name" in item
            assert "team_name" in item
            assert "deaths_without_kast" in item
            assert "rounds_lost_when_no_kast" in item
            assert "loss_rate_when_no_kast" in item

    def test_player_filter(self, mock_db):
        """Filters results by player name."""
        result = kast_impact(
            mock_db,
            series_id="series-001",
            player_name="Alice",
            min_deaths_no_kast=0
        )
        for item in result:
            assert "alice" in item["player_name"].lower()


class TestOpeningDeathImpact:
    """Tests for opening_death_impact function."""

    def test_returns_list(self, mock_db):
        """Returns list of opening death analysis."""
        result = opening_death_impact(mock_db, series_id="series-001", min_opening_deaths=0)
        assert isinstance(result, list)

    def test_returns_empty_without_series(self, mock_db):
        """Returns empty list when no series IDs provided."""
        result = opening_death_impact(mock_db, series_id=None, series_ids=None)
        assert result == []

    def test_result_structure(self, mock_db):
        """Each result has correct structure."""
        result = opening_death_impact(mock_db, series_id="series-001", min_opening_deaths=0)
        if result:
            item = result[0]
            assert "player_name" in item
            assert "team_name" in item
            assert "opening_deaths" in item
            assert "rounds_lost_when_od" in item
            assert "loss_rate_when_od" in item
            assert "rounds_list" in item

    def test_player_filter(self, mock_db):
        """Filters results by player name."""
        result = opening_death_impact(
            mock_db,
            series_id="series-001",
            player_name="Alice",
            min_opening_deaths=0
        )
        for item in result:
            assert "alice" in item["player_name"].lower()
