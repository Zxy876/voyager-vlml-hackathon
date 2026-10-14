"""Shared metric utilities for insights reports."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, List

SQL_DIR = Path(__file__).parent.parent / "sql"


@lru_cache(maxsize=None)
def load_sql(name: str) -> str:
    """Load SQL file from the sql directory."""
    return (SQL_DIR / name).read_text(encoding="utf-8")


def in_clause(values: List[Any]) -> str:
    """Build a parameterized IN clause placeholder."""
    return ", ".join(["?"] * len(values))


def confidence_label(rounds: int) -> str:
    """Return confidence label based on sample size."""
    if rounds >= 100:
        return "strong"
    if rounds >= 50:
        return "moderate"
    if rounds >= 20:
        return "weak"
    return "insufficient"
