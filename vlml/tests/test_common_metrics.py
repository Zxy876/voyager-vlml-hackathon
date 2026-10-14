"""Unit tests for common/metrics.py utility functions."""
import pytest

from vlml.tools.common.metrics import confidence_label, in_clause, load_sql


class TestInClause:
    """Tests for the in_clause function."""

    def test_empty_list(self):
        """Empty list returns empty string."""
        assert in_clause([]) == ""

    def test_single_value(self):
        """Single value returns single placeholder."""
        assert in_clause(["a"]) == "?"

    def test_multiple_values(self):
        """Multiple values return comma-separated placeholders."""
        assert in_clause(["a", "b", "c"]) == "?, ?, ?"

    def test_five_values(self):
        """Five values return five placeholders."""
        assert in_clause([1, 2, 3, 4, 5]) == "?, ?, ?, ?, ?"


class TestConfidenceLabel:
    """Tests for the confidence_label function."""

    def test_strong_at_100(self):
        """100 rounds returns 'strong'."""
        assert confidence_label(100) == "strong"

    def test_strong_above_100(self):
        """Above 100 rounds returns 'strong'."""
        assert confidence_label(150) == "strong"
        assert confidence_label(1000) == "strong"

    def test_moderate_at_50(self):
        """50 rounds returns 'moderate'."""
        assert confidence_label(50) == "moderate"

    def test_moderate_at_99(self):
        """99 rounds returns 'moderate'."""
        assert confidence_label(99) == "moderate"

    def test_weak_at_20(self):
        """20 rounds returns 'weak'."""
        assert confidence_label(20) == "weak"

    def test_weak_at_49(self):
        """49 rounds returns 'weak'."""
        assert confidence_label(49) == "weak"

    def test_insufficient_at_19(self):
        """19 rounds returns 'insufficient'."""
        assert confidence_label(19) == "insufficient"

    def test_insufficient_at_0(self):
        """0 rounds returns 'insufficient'."""
        assert confidence_label(0) == "insufficient"

    def test_insufficient_negative(self):
        """Negative rounds returns 'insufficient'."""
        assert confidence_label(-5) == "insufficient"


class TestLoadSql:
    """Tests for the load_sql function."""

    def test_load_existing_file(self):
        """Loading an existing SQL file works."""
        sql = load_sql("series_metadata.sql")
        assert "SELECT" in sql
        assert "series_id" in sql

    def test_load_another_file(self):
        """Loading another SQL file works."""
        sql = load_sql("fetch_series_ids_for_team.sql")
        assert "SELECT" in sql
        assert "team_name" in sql

    def test_load_nonexistent_file(self):
        """Loading non-existent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_sql("nonexistent_file.sql")

    def test_load_sql_cached(self):
        """SQL files are cached after first load."""
        # Load twice - should use cache on second call
        sql1 = load_sql("series_metadata.sql")
        sql2 = load_sql("series_metadata.sql")
        assert sql1 == sql2
