#!/usr/bin/env python3
"""
VLML Database Pipeline - Master Script

Runs the complete data pipeline:
1. Initialize schema (if needed)
2. Load raw JSONL data into atomic tables
3. Run transformation models
4. Validate data integrity
"""
import argparse
import sys
from pathlib import Path

scripts_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(scripts_root))

from orchestration.init_schema import init_schema
from ingestion.load_data import load_raw_data
from orchestration.run_transformations import run_transformations
from manager import EventDatabase


def run_pipeline(
    db_path: str = None,
    year: int = None,
    skip_schema: bool = False,
    skip_load: bool = False,
    skip_transforms: bool = False,
    skip_validate: bool = False,
):
    """Run the complete VLML database pipeline.

    Args:
        db_path: Path to DuckDB file
        year: Year to process (e.g., 2025)
        skip_schema: Skip schema initialization
        skip_load: Skip data loading
        skip_transforms: Skip transformations
        skip_validate: Skip validation
    """
    print("=" * 70)
    print("  VLML DATABASE PIPELINE")
    print("=" * 70)
    print()

    # Step 1: Initialize Schema
    if not skip_schema:
        print("STEP 1: Initialize Schema")
        print("-" * 70)
        try:
            init_schema(db_path=db_path)
        except RuntimeError as e:
            if "already exist" in str(e):
                print("  ℹ️  Schema already initialized")
            else:
                raise
        print()

    # Step 2: Load Raw Data
    if not skip_load:
        print("STEP 2: Load Raw Data")
        print("-" * 70)
        load_raw_data(year=year, db_path=db_path)
        print()

    # Step 3: Run Transformations
    if not skip_transforms:
        print("STEP 3: Run Transformations")
        print("-" * 70)
        run_transformations(db_path=db_path)
        print()

    # Step 4: Validate Data
    if not skip_validate:
        print("STEP 4: Validate Data")
        print("-" * 70)

        with EventDatabase(db_path=db_path, read_only=True) as db:
            # Check for duplicates in each table
            tables_to_check = [
                ("series", "series_id"),
                ("games", "game_id"),
                ("rounds", "round_id"),
                ("base_events", "event_id"),
                ("agg_player_round_stats", "(round_id, player_id)"),
                ("agg_player_game_stats", "(game_id, player_id)"),
            ]

            print("Checking for duplicate primary keys...\n")
            all_pass = True

            for table, pk in tables_to_check:
                try:
                    if "(" in pk:  # Composite key
                        query = f"SELECT COUNT(*) - COUNT(DISTINCT {pk}) FROM {table}"
                    else:
                        query = f"SELECT COUNT(*) - COUNT(DISTINCT {pk}) FROM {table}"

                    duplicates = db.query(query)[0][0]

                    if duplicates == 0:
                        print(f"  ✅ {table}: No duplicates")
                    else:
                        print(f"  ❌ {table}: {duplicates} duplicate(s)")
                        all_pass = False
                except Exception as e:
                    print(f"  ⚠️  {table}: {str(e)[:50]}")

            print()
            if all_pass:
                print("  ✅ All validation checks passed!")
            else:
                print("  ⚠️  Some validation checks failed")

        print()

    # Final Summary
    print("=" * 70)
    print("  PIPELINE COMPLETE")
    print("=" * 70)

    with EventDatabase(db_path=db_path, read_only=True) as db:
        stats = db.get_database_stats()
        print()
        print("Database Statistics:")
        print(f"  Series:      {stats['series_count']:,}")
        print(f"  Games:       {stats['games_count']:,}")
        print(f"  Rounds:      {stats['rounds_count']:,}")
        print(f"  Base Events: {stats['base_events_count']:,}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the complete VLML database pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run full pipeline for 2025 data
  python run_pipeline.py --year 2025

  # Run only transformations (data already loaded)
  python run_pipeline.py --skip-schema --skip-load

  # Initialize and load, skip transforms
  python run_pipeline.py --year 2025 --skip-transforms
        """
    )

    parser.add_argument(
        "--db",
        help="Path to DuckDB file (default: data/vlml_events.duckdb)"
    )
    parser.add_argument(
        "--year",
        type=int,
        help="Year to process (e.g., 2025)"
    )
    parser.add_argument(
        "--skip-schema",
        action="store_true",
        help="Skip schema initialization"
    )
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="Skip data loading"
    )
    parser.add_argument(
        "--skip-transforms",
        action="store_true",
        help="Skip transformations"
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="Skip validation"
    )

    args = parser.parse_args()

    try:
        run_pipeline(
            db_path=args.db,
            year=args.year,
            skip_schema=args.skip_schema,
            skip_load=args.skip_load,
            skip_transforms=args.skip_transforms,
            skip_validate=args.skip_validate,
        )
    except Exception as e:
        print(f"\n❌ Pipeline failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
