#!/usr/bin/env python3
"""Load raw JSONL event files into DuckDB using bulk operations.

This script loads downloaded GRID event data from JSONL files into the
atomic tables (series, games, rounds, base_events).

Usage:
    # Load all years
    python load_data.py

    # Load specific year
    python load_data.py --year 2025

    # Custom database path
    python load_data.py --year 2025 --db /path/to/db.duckdb

Processing:
    1. Scans data/raw_events/{year}/ for *.jsonl files
    2. Parses each file using parsers.py
    3. Bulk inserts into atomic tables using db_loader.py
    4. Skips already-loaded series (idempotent)

Output Tables:
    - series: Tournament metadata (series_id, tournament, date)
    - games: Map-level info (game_id, map_name, winner)
    - rounds: Round-level info (round_id, round_number, winner)
    - base_events: Individual events (kills, deaths, plants, abilities)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

scripts_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(scripts_root))

from ingestion.db_loader import process_series_bulk
from ingestion.parsers import extract_metadata_from_path


def load_raw_data(year: int | None = None, db_path: str | None = None) -> None:
    """Load raw JSONL files into database using bulk operations."""
    project_root = Path(__file__).resolve().parents[3]
    raw_events_dir = project_root / "data" / "raw_events"

    if not raw_events_dir.exists():
        print(f"❌ Raw events directory not found: {raw_events_dir}")
        return

    if db_path is None:
        db_path = str(project_root / "data" / "vlml_events.duckdb")

    print("=" * 70)
    print("  Load Raw JSONL Data into Database (Bulk Mode)")
    print("=" * 70)
    print()

    if year:
        year_dir = raw_events_dir / str(year)
        if not year_dir.exists():
            print(f"❌ Year directory not found: {year_dir}")
            return
        jsonl_files = sorted(year_dir.rglob("*.jsonl"))
    else:
        jsonl_files = sorted(raw_events_dir.rglob("*.jsonl"))

    print(f"📁 Found {len(jsonl_files)} JSONL file(s)")
    print(f"📊 Database: {db_path}")
    print()

    conn = duckdb.connect(db_path)

    successful = 0
    skipped = 0
    failed = 0
    total_events = 0

    for i, file_path in enumerate(jsonl_files, 1):
        metadata = extract_metadata_from_path(file_path)
        tournament = metadata['tournament'] or "Unknown"

        print(f"[{i}/{len(jsonl_files)}] {metadata['series_id']}: {tournament}")

        result, event_count = process_series_bulk(conn, file_path)

        if result:
            successful += 1
            total_events += event_count
        elif event_count == 0:
            skipped += 1
        else:
            failed += 1

        print()

    conn.close()

    print("=" * 70)
    print(f"  ✅ Loaded: {successful} series")
    print(f"  ⏭️  Skipped: {skipped} series")
    print(f"  ❌ Failed: {failed} series")
    print(f"  📊 Total Events: {total_events:,}")
    print("=" * 70)
    print()

    conn = duckdb.connect(db_path, read_only=True)

    series_count = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
    games_count = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    rounds_count = conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
    events_count = conn.execute("SELECT COUNT(*) FROM base_events").fetchone()[0]

    print("Database Stats:")
    print(f"  Series: {series_count:,}")
    print(f"  Games: {games_count:,}")
    print(f"  Rounds: {rounds_count:,}")
    print(f"  Events: {events_count:,}")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load raw JSONL files into DuckDB")
    parser.add_argument(
        "--year",
        type=int,
        help="Filter by year (e.g., 2025)",
    )
    parser.add_argument(
        "--db",
        help="Path to DuckDB file (default: data/vlml_events.duckdb)",
    )
    args = parser.parse_args()

    try:
        load_raw_data(year=args.year, db_path=args.db)
    except Exception as exc:
        print(f"\n❌ Error: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)
