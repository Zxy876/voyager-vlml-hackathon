#!/usr/bin/env python3
"""Reset database schema by dropping tables."""
import argparse
import duckdb


def list_tables(conn: duckdb.DuckDBPyConnection) -> list:
    """Return all user tables in main schema."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    ]


def reset_schema(db_path: str, tables: list = None, all_tables: bool = False):
    """Drop selected tables or all tables in the database."""
    conn = duckdb.connect(db_path)

    if all_tables:
        tables = list_tables(conn)
    elif not tables:
        raise ValueError("No tables specified. Use --all or provide table names.")

    for table in tables:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        print(f"✅ Dropped {table}")

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Reset DuckDB schema by dropping tables",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Drop all tables
  python database/scripts/maintenance/reset_schema.py --db data/vlml_events.duckdb --all

  # Drop specific tables
  python database/scripts/maintenance/reset_schema.py --db data/vlml_events.duckdb series games rounds base_events
"""
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to DuckDB file (e.g., data/vlml_events.duckdb)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Drop all tables in the main schema"
    )
    parser.add_argument(
        "tables",
        nargs="*",
        help="Specific tables to drop"
    )

    args = parser.parse_args()
    reset_schema(db_path=args.db, tables=args.tables, all_tables=args.all)


if __name__ == "__main__":
    main()
