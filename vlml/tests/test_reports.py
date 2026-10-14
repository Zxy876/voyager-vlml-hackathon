"""Integration tests for report modules.

Note: These tests verify that report functions can be imported and called correctly.
Full integration tests with mock data are complex due to the many SQL queries involved.
The smoke tests in test_insights_tools.py provide better coverage using real data.
"""
from __future__ import annotations

import pytest

from vlml.tools.reports import (
    match_analysis_report,
    pattern_detection_report,
    player_profile_report,
    scouting_report,
)


class TestReportImports:
    """Test that report functions can be imported correctly."""

    def test_match_analysis_report_callable(self):
        """match_analysis_report is callable."""
        assert callable(match_analysis_report)

    def test_player_profile_report_callable(self):
        """player_profile_report is callable."""
        assert callable(player_profile_report)

    def test_scouting_report_callable(self):
        """scouting_report is callable."""
        assert callable(scouting_report)

    def test_pattern_detection_report_callable(self):
        """pattern_detection_report is callable."""
        assert callable(pattern_detection_report)


class TestReportFunctionSignatures:
    """Test that report functions have expected signatures."""

    def test_match_analysis_has_series_id_param(self):
        """match_analysis_report accepts series_id parameter."""
        import inspect
        sig = inspect.signature(match_analysis_report)
        assert "series_id" in sig.parameters

    def test_player_profile_has_player_name_param(self):
        """player_profile_report accepts player_name parameter."""
        import inspect
        sig = inspect.signature(player_profile_report)
        assert "player_name" in sig.parameters

    def test_scouting_has_team_name_param(self):
        """scouting_report accepts team_name parameter."""
        import inspect
        sig = inspect.signature(scouting_report)
        assert "team_name" in sig.parameters

    def test_pattern_detection_has_team_name_param(self):
        """pattern_detection_report accepts team_name parameter."""
        import inspect
        sig = inspect.signature(pattern_detection_report)
        assert "team_name" in sig.parameters


class TestBackwardCompatibility:
    """Test that insights_tools re-exports work correctly."""

    def test_import_from_insights_tools(self):
        """Functions can be imported from insights_tools module."""
        from vlml.tools import insights_tools
        assert hasattr(insights_tools, "match_analysis_report")
        assert hasattr(insights_tools, "player_profile_report")
        assert hasattr(insights_tools, "scouting_report")
        assert hasattr(insights_tools, "pattern_detection_report")

    def test_insights_tools_all_attribute(self):
        """insights_tools has correct __all__ attribute."""
        from vlml.tools import insights_tools
        assert "match_analysis_report" in insights_tools.__all__
        assert "player_profile_report" in insights_tools.__all__
        assert "scouting_report" in insights_tools.__all__
        assert "pattern_detection_report" in insights_tools.__all__

    def test_same_function_references(self):
        """insights_tools exports same functions as reports module."""
        from vlml.tools import insights_tools
        from vlml.tools.reports import (
            match_analysis_report as mar,
            player_profile_report as ppr,
            scouting_report as sr,
            pattern_detection_report as pdr,
        )
        assert insights_tools.match_analysis_report is mar
        assert insights_tools.player_profile_report is ppr
        assert insights_tools.scouting_report is sr
        assert insights_tools.pattern_detection_report is pdr
