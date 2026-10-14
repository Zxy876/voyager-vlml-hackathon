#!/usr/bin/env python3
"""Initialize DuckDB schema from YAML configuration."""
import sys
from pathlib import Path
import yaml
import duckdb


def load_config(config_path: Path) -> dict:
    """Load YAML configuration file."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def execute_sql_file(conn: duckdb.DuckDBPyConnection, sql_file: Path):
    """Execute a SQL file.

    Args:
        conn: DuckDB connection
        sql_file: Path to SQL file
    """
    with open(sql_file, 'r') as f:
        sql = f.read()
    conn.execute(sql)


def init_schema(config_path: str = None, db_path: str = None):
    """Initialize database schema from configuration.

    Args:
        config_path: Path to init_schema.yaml (defaults to database/config/init_schema.yaml)
        db_path: Path to DuckDB file (overrides config if provided)
    """
    # Default paths
    if config_path is None:
        project_root = Path(__file__).resolve().parents[3]
        config_path = project_root / "database" / "config" / "init_schema.yaml"
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
    print("  Initialize VLML OLAP Database Schema")
    print("=" * 70)
    print(f"Database: {db_path}")
    print(f"Config:   {config_path}")
    print()

    # Connect to database
    conn = duckdb.connect(str(db_path))

    # Execute initialization steps
    for step in config['steps']:
        print(f"📋 {step['name']}")
        print(f"   {step['description']}")

        # Check if this is a seed/reference table step
        is_seed_step = 'reference' in step['name'].lower() or 'seed' in step.get('description', '').lower()

        for sql_file_rel in step['files']:
            sql_file = project_root / sql_file_rel

            if not sql_file.exists():
                print(f"   ❌ File not found: {sql_file}")
                continue

            try:
                # Check if table already has data (for seeds only)
                if is_seed_step:
                    table_name = sql_file.stem
                    try:
                        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                        if count > 0:
                            print(f"   ⏭️  {sql_file.name} (already loaded, {count} rows)")
                            continue
                    except:
                        pass  # Table doesn't exist yet

                # Execute the SQL file
                execute_sql_file(conn, sql_file)
                print(f"   ✅ {sql_file.name}")
            except Exception as e:
                print(f"   ❌ {sql_file.name}: {str(e)}")
                raise
        print()

    # Run validation
    if 'validation' in config:
        print("=" * 70)
        print("  Validation")
        print("=" * 70)

        result = conn.execute(config['validation']['query']).fetchall()
        for row in result:
            print(f"  {row[0]}: {row[1]}")

        # Check table count
        if 'expected_table_count' in config['validation']:
            table_count = result[0][1]
            expected = config['validation']['expected_table_count']

            if table_count == expected:
                print(f"\n  ✅ All {table_count} tables created successfully!")
            else:
                print(f"\n  ⚠️  Expected {expected} tables, found {table_count}")

    print()
    print("=" * 70)
    print("  ✅ Schema initialization complete!")
    print("=" * 70)

    conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Initialize DuckDB schema from YAML config")
    parser.add_argument(
        "--config",
        help="Path to init_schema.yaml (default: database/config/init_schema.yaml)"
    )
    parser.add_argument(
        "--db",
        help="Path to DuckDB file (default: from config)"
    )

    args = parser.parse_args()

    try:
        init_schema(config_path=args.config, db_path=args.db)
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        sys.exit(1)
