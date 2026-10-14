"""DuckDB database manager for VLML event data."""
from pathlib import Path
from typing import Any, Dict, List, Optional
import duckdb


class EventDatabase:
    """Manages DuckDB database for Valorant event data (query-focused)."""

    def __init__(self, db_path: Optional[str] = None, read_only: bool = False):
        """Initialize database connection.

        Args:
            db_path: Path to DuckDB file. If None, uses default location.
            read_only: If True, open database in read-only mode (allows concurrent access)
        """
        if db_path is None:
            # Use project root/data directory
            project_root = Path(__file__).parent.parent.parent.parent
            data_dir = project_root / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "vlml_events.duckdb")

        self.db_path = db_path
        self.read_only = read_only

        # Open in read-only mode to avoid locking conflicts
        self.conn = duckdb.connect(db_path, read_only=read_only)

        # Only initialize schema if not read-only
        if not read_only:
            self._initialize_schema()

    def _initialize_schema(self):
        """Initialize database schema from SQL file (skipped if tables exist)."""
        # Check if base_events table exists (new schema)
        result = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'base_events'"
        ).fetchone()

        if result and result[0] > 0:
            # Tables already exist, skip initialization
            return

        # Tables don't exist - database should be initialized using orchestration script
        print("⚠️  Warning: Database tables not found.")
        print("            Please run: python database/scripts/orchestration/init_schema.py")
        print("            Then load data with: python database/scripts/orchestration/run_pipeline.py")
        print("            Continuing anyway - some operations may fail.")

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    # ===== Series Management =====

    def series_exists(self, series_id: str) -> bool:
        """Check if series is already ingested.

        Args:
            series_id: GRID series ID

        Returns:
            True if series exists in database
        """
        result = self.conn.execute(
            "SELECT COUNT(*) FROM series WHERE series_id = ?", [series_id]
        ).fetchone()
        return result[0] > 0

    # ===== Query Methods =====

    def query(self, sql: str, params: List = None) -> List[tuple]:
        """Execute a SQL query and return results.

        Args:
            sql: SQL query string
            params: Optional query parameters

        Returns:
            List of result tuples
        """
        if params:
            return self.conn.execute(sql, params).fetchall()
        return self.conn.execute(sql).fetchall()

    def query_df(self, sql: str, params: List = None):
        """Execute a SQL query and return results as pandas DataFrame.

        Args:
            sql: SQL query string
            params: Optional query parameters

        Returns:
            pandas DataFrame
        """
        if params:
            return self.conn.execute(sql, params).df()
        return self.conn.execute(sql).df()

    def get_player_stats(self, player_name: str, series_id: str = None) -> List[Dict]:
        """Get player statistics.

        Args:
            player_name: Player name
            series_id: Optional series ID to filter by

        Returns:
            List of stat dictionaries
        """
        if series_id:
            sql = """
                SELECT * FROM player_stats
                WHERE player_name = ? AND series_id = ?
            """
            results = self.conn.execute(sql, [player_name, series_id]).fetchall()
        else:
            sql = """
                SELECT * FROM player_stats
                WHERE player_name = ?
            """
            results = self.conn.execute(sql, [player_name]).fetchall()

        # Convert to list of dicts
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def get_series_list(self) -> List[str]:
        """Get list of all series IDs in database.

        Returns:
            List of series IDs
        """
        results = self.conn.execute("SELECT series_id FROM series ORDER BY ingested_at DESC").fetchall()
        return [row[0] for row in results]

    def get_database_stats(self) -> Dict[str, int]:
        """Get database statistics.

        Returns:
            Dictionary with counts of series, games, events, etc.
        """
        stats = {
            "series_count": self.conn.execute("SELECT COUNT(*) FROM series").fetchone()[0],
            "games_count": self.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0],
            "rounds_count": self.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0],
            "base_events_count": self.conn.execute("SELECT COUNT(*) FROM base_events").fetchone()[0],
        }

        # Add aggregation table stats if they exist
        try:
            stats["player_round_stats_count"] = self.conn.execute("SELECT COUNT(*) FROM agg_player_round_stats").fetchone()[0]
            stats["player_game_stats_count"] = self.conn.execute("SELECT COUNT(*) FROM agg_player_game_stats").fetchone()[0]
            stats["player_series_stats_count"] = self.conn.execute("SELECT COUNT(*) FROM agg_player_series_stats").fetchone()[0]
            stats["unique_players"] = self.conn.execute("SELECT COUNT(DISTINCT player_id) FROM agg_player_game_stats").fetchone()[0]
        except:
            # Aggregation tables don't exist yet
            stats["transformations_status"] = "NOT RUN - run database/scripts/orchestration/run_transformations.py"

        return stats
