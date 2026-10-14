"""Pattern detection report generation.

This module generates pattern detection reports across large datasets.
It aggregates key metrics over many series to identify recurring tendencies
that may not be visible in single-match analysis.

Report Sections:
    - subject: Team or player being analyzed
    - scope: Series count, round count, confidence level
    - summary: High-level overview of dataset size
    - key_metrics: Aggregated metrics across all series

Use Cases:
    - Identify long-term tendencies (e.g., consistently weak pistol rounds)
    - Compare performance across tournaments
    - Build larger sample sizes for statistical significance

SQL Dependencies:
    Uses assemble_team_metrics() from match_analysis and
    player_key_metrics() from player_profile for metric calculation.

Usage:
    >>> from vlml.tools.reports import pattern_detection_report
    >>> report = await pattern_detection_report(team_name="LOUD")
    >>> report = await pattern_detection_report(player_name="aspas", min_rounds=500)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from vlml.db.manager import EventDatabase

from ..common import (
    confidence_label,
    fetch_series_ids_for_player,
    fetch_series_ids_for_team,
)
from .match_analysis import assemble_team_metrics
from .player_profile import player_key_metrics


async def pattern_detection_report(
    team_name: Optional[str] = None,
    player_name: Optional[str] = None,
    tournament_name: Optional[str] = None,
    series_ids: Optional[List[str]] = None,
    min_rounds: int = 200,
) -> Dict[str, Any]:
    """Detect recurring patterns across a large dataset.

    Aggregates key metrics over multiple series to identify statistically
    significant patterns. Requires either team_name or player_name.

    Args:
        team_name: Team to analyze (mutually exclusive with player_name).
        player_name: Player to analyze (mutually exclusive with team_name).
        tournament_name: Optional tournament filter (not yet implemented).
        series_ids: Optional explicit series list. If not provided,
            fetches the most recent 20 series.
        min_rounds: Minimum rounds for statistical significance (default 200).

    Returns:
        Dict containing:
            - report_type: "pattern_detection"
            - subject: team_name and player_name being analyzed
            - scope: Series count, rounds, confidence level
            - summary: High-level dataset overview
            - key_metrics: Full metrics structure (same as match_analysis)

    Example:
        >>> report = await pattern_detection_report(team_name="LOUD")
        >>> if report["scope"]["confidence"] == "high":
        ...     print("Statistically significant sample")
    """
    if not team_name and not player_name:
        return {"error": "Provide team_name or player_name for pattern detection"}

    with EventDatabase(read_only=True) as db:
        if not series_ids:
            if team_name:
                series_ids = fetch_series_ids_for_team(db, team_name, last_n_series=20)
            else:
                series_ids = fetch_series_ids_for_player(db, player_name, last_n_series=20)

        if not series_ids:
            return {"error": "No series found for pattern detection"}

        subject = team_name or player_name or "Unknown"
        if team_name:
            key_metrics = assemble_team_metrics(db, series_ids, team_name)
            rounds = key_metrics["opening_duels"]["fb"]["denom"]
        else:
            player_metrics = player_key_metrics(db, series_ids, player_name)
            key_metrics = player_metrics["key_metrics"]
            rounds = player_metrics["rounds_played"]

        return {
            "report_type": "pattern_detection",
            "subject": {"team_name": team_name, "player_name": player_name},
            "scope": {
                "series": len(series_ids),
                "rounds": rounds,
                "confidence": confidence_label(rounds),
            },
            "summary": {
                "subject": subject,
                "series": len(series_ids),
                "rounds": rounds,
                "confidence": confidence_label(rounds),
            },
            "key_metrics": key_metrics,
        }
