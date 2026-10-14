#!/usr/bin/env python3
"""Run incremental transformation models from YAML configuration."""
import sys
from pathlib import Path
import yaml
import duckdb
from datetime import datetime


def load_config(config_path: Path) -> dict:
    """Load YAML configuration file."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def execute_sql_file(conn: duckdb.DuckDBPyConnection, sql_file: Path) -> int:
    """Execute a SQL file with multiple statements."""
    with open(sql_file, 'r') as f:
        sql = f.read()

    # Execute all statements in the file
    try:
        # DuckDB can execute multiple statements in one call
        # Remove comments and split by semicolons
        statements = []
        for line in sql.split('\n'):
            # Skip comment lines
            if line.strip().startswith('--'):
                continue
            statements.append(line)

        # Join back and split by semicolons
        sql_clean = '\n'.join(statements)

        # Execute each statement
        for statement in sql_clean.split(';'):
            statement = statement.strip()
            if statement:  # Skip empty statements
                conn.execute(statement)

        return 0
    except Exception as e:
        print(f"Error executing SQL: {e}")
        raise


def check_dependencies(conn: duckdb.DuckDBPyConnection, dependencies: list) -> bool:
    """Check if all dependency tables exist."""
    for table in dependencies:
        result = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table]
        ).fetchone()

        if result[0] == 0:
            return False
    return True


def run_transformations(
    config_path: str = None,
    db_path: str = None,
    models: list = None,
    full_refresh: bool = False,
):
    """Run transformation models from configuration.

    Args:
        config_path: Path to transformations.yaml (defaults to database/config/transformations.yaml)
        db_path: Path to DuckDB file (overrides config if provided)
        models: List of specific model names to run (runs all if None)
    """
    # Default paths
    if config_path is None:
        project_root = Path(__file__).resolve().parents[3]
        config_path = project_root / "database" / "config" / "transformations.yaml"
    else:
        config_path = Path(config_path)
        project_root = config_path.parent.parent.parent

    # Load configuration
    config = load_config(config_path)

    # Get database path
    if db_path is None:
        db_path = project_root / config['database']['path']
    else:
        db_path = Path(db_path)

    print("=" * 70)
    print("  Run VLML Transformation Models")
    print("=" * 70)
    print(f"Database: {db_path}")
    print(f"Config:   {config_path}")
    print()

    # Connect to database
    conn = duckdb.connect(str(db_path))

    # Filter models if specified
    all_models = config['models']
    if models:
        all_models = [m for m in all_models if m['name'] in models]

    print(f"📋 Running {len(all_models)} transformation model(s)\n")

    # Execute transformation models
    for i, model in enumerate(all_models, 1):
        print(f"[{i}/{len(all_models)}] {model['name']}")
        print(f"   {model['description']}")

        # Check dependencies
        if 'depends_on' in model:
            print(f"   📦 Dependencies: {', '.join(model['depends_on'])}")
            if not check_dependencies(conn, model['depends_on']):
                print(f"   ❌ Missing dependencies - skipping")
                print()
                continue

        # Get SQL file
        sql_file = project_root / model['file']

        if not sql_file.exists():
            print(f"   ❌ File not found: {sql_file}")
            print()
            continue

        # Full refresh: clear target table before running
        if full_refresh and 'target_table' in model:
            conn.execute(f"DELETE FROM {model['target_table']}")

        # Execute transformation
        try:
            start_time = datetime.now()
            rows_affected = execute_sql_file(conn, sql_file)
            duration = (datetime.now() - start_time).total_seconds()

            print(f"   ✅ Completed in {duration:.2f}s")

            # Show row count in target table
            if 'target_table' in model:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {model['target_table']}"
                ).fetchone()[0]
                print(f"   📊 {model['target_table']}: {count:,} rows")

        except Exception as e:
            print(f"   ❌ Error: {str(e)[:100]}")
            raise

        print()

    # Run validation queries
    if 'validation' in config and 'queries' in config['validation']:
        print("=" * 70)
        print("  Validation")
        print("=" * 70)

        for validation in config['validation']['queries']:
            print(f"\n{validation['name']}:")
            result = conn.execute(validation['query']).fetchall()

            # Pretty print results
            for row in result:
                if len(row) == 2:
                    print(f"  {row[0]}: {row[1]:,}")
                else:
                    print(f"  {row}")

    print()
    print("=" * 70)
    print("  ✅ Transformations complete!")
    print("=" * 70)

    conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run transformation models from YAML config")
    parser.add_argument(
        "--config",
        help="Path to transformations.yaml (default: database/config/transformations.yaml)"
    )
    parser.add_argument(
        "--db",
        help="Path to DuckDB file (default: from config)"
    )
    parser.add_argument(
        "--models",
        nargs="+",
        help="Specific models to run (e.g., --models agg_player_round_stats agg_player_game_stats)"
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Rebuild target tables from scratch (delete all rows before each model).",
    )

    args = parser.parse_args()

    try:
        run_transformations(
            config_path=args.config,
            db_path=args.db,
            models=args.models,
            full_refresh=args.full_refresh,
        )
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        sys.exit(1)
