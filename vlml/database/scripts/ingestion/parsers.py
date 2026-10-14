"""Parsing helpers for raw event ingestion."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List


def parse_jsonl_file(file_path: Path) -> List[Dict]:
    """Parse a raw JSONL file and extract events."""
    events: List[Dict] = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                wrapper = json.loads(line)
                wrapper_events = wrapper.get("events", [])
                wrapper_timestamp = wrapper.get("occurredAt")
                for event in wrapper_events:
                    if "occurredAt" not in event and wrapper_timestamp:
                        event["occurredAt"] = wrapper_timestamp
                    events.append(event)
            except json.JSONDecodeError as exc:
                print(f"  ⚠️  Skipping malformed line: {str(exc)[:50]}")
                continue
    return events


def extract_metadata_from_path(file_path: Path) -> Dict[str, str]:
    """Extract year, tournament, and series_id from file path."""
    parts = file_path.parts
    series_id = file_path.stem

    try:
        raw_idx = parts.index('raw_events')
        year = parts[raw_idx + 1] if raw_idx + 1 < len(parts) else None
        tournament = parts[raw_idx + 2] if raw_idx + 2 < len(parts) else None
    except (ValueError, IndexError):
        year = None
        tournament = None

    return {
        'series_id': series_id,
        'year': int(year) if year and year.isdigit() else None,
        'tournament': tournament,
    }


def parse_iso_duration(duration: str) -> float | None:
    """Parse ISO 8601 duration strings like PT13M36.139S into seconds."""
    if not duration or not isinstance(duration, str) or not duration.startswith("PT"):
        return None
    duration = duration[2:]
    hours = minutes = seconds = 0.0
    number = ""
    for ch in duration:
        if ch.isdigit() or ch == ".":
            number += ch
            continue
        if ch == "H":
            hours = float(number or 0)
        elif ch == "M":
            minutes = float(number or 0)
        elif ch == "S":
            seconds = float(number or 0)
        number = ""
    return hours * 3600 + minutes * 60 + seconds


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse ISO 8601 timestamp with optional Z suffix."""
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None
