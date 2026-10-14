#!/usr/bin/env python3
"""DuckDB database manager for VLML OLAP database (updated for new schema)."""
from pathlib import Path
from typing import Any, Dict, List, Optional
import duckdb
from datetime import datetime


class EventDatabase:
    """Manages DuckDB database for Valorant event data."""

    def __init__(self, db_path: Optional[str] = None, read_only: bool = False):
        """Initialize database connection.

        Args:
            db_path: Path to DuckDB file. If None, uses default location.
            read_only: If True, open database in read-only mode (allows concurrent access)
        """
        if db_path is None:
            # Use project root/data directory
            project_root = Path(__file__).parent.parent.parent
            data_dir = project_root / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "vlml_events.duckdb")

        self.db_path = db_path
        self.read_only = read_only
        self.conn = duckdb.connect(db_path, read_only=read_only)

        # Verify schema exists
        if not read_only:
            self._verify_schema()

    def _verify_schema(self):
        """Verify database schema exists."""
        result = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'base_events'"
        ).fetchone()

        if result and result[0] == 0:
            print("⚠️  Warning: Database tables not found.")
            print("    Please run: python database/scripts/orchestration/init_schema.py")
            raise RuntimeError("Database not initialized")

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

    def insert_series(
        self,
        series_id: str,
        tournament_name: str = None,
        tournament_year: int = None,
        tournament_region: str = None,
        team1_name: str = None,
        team2_name: str = None,
        start_time: str = None,
        winning_team_name: str = None,
    ):
        """Insert series metadata.

        Args:
            series_id: GRID series ID
            tournament_name: Tournament name
            tournament_year: Tournament year
            tournament_region: Tournament region
            team1_name: First team name
            team2_name: Second team name
            start_time: Series start time (ISO format)
            winning_team_name: Winning team name
        """
        self.conn.execute(
            """
            INSERT OR REPLACE INTO series (
                series_id, tournament_name, tournament_year, tournament_region,
                team1_name, team2_name, start_time, winning_team_name, ingested_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [
                series_id,
                tournament_name,
                tournament_year,
                tournament_region,
                team1_name,
                team2_name,
                start_time,
                winning_team_name,
            ],
        )

    def insert_game(
        self,
        game_id: str,
        series_id: str,
        game_number: int,
        map_name: str = None,
        team1_name: str = None,
        team2_name: str = None,
        winning_team_name: str = None,
        total_rounds: int = None,
        game_duration_seconds: int = None,
    ):
        """Insert game metadata.

        Args:
            game_id: Game ID
            series_id: Parent series ID
            game_number: Game number in series
            map_name: Map name
            team1_name: First team
            team2_name: Second team
            winning_team_name: Winner
            total_rounds: Total rounds played
            game_duration_seconds: Game duration
        """
        self.conn.execute(
            """
            INSERT OR REPLACE INTO games (
                game_id, series_id, game_number, map_name,
                team1_name, team2_name, winning_team_name,
                total_rounds, game_duration_seconds
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                game_id,
                series_id,
                game_number,
                map_name,
                team1_name,
                team2_name,
                winning_team_name,
                total_rounds,
                game_duration_seconds,
            ],
        )

    def insert_round(
        self,
        round_id: str,
        series_id: str,
        game_id: str,
        round_number: int,
        map_name: str = None,
        started_at: str = None,
        ended_at: str = None,
        winning_team_name: str = None,
        losing_team_name: str = None,
        end_reason: str = None,
        duration_seconds: float = None,
        tournament_name: str = None,
        tournament_year: int = None,
    ):
        """Insert round metadata.

        Args:
            round_id: Round ID
            series_id: Parent series ID
            game_id: Parent game ID
            round_number: Round number
            map_name: Map name
            started_at: Round start timestamp
            ended_at: Round end timestamp
            winning_team_name: Winner
            losing_team_name: Loser
            end_reason: How round ended
            duration_seconds: Round duration
            tournament_name: Tournament name
            tournament_year: Tournament year
        """
        self.conn.execute(
            """
            INSERT OR REPLACE INTO rounds (
                round_id, series_id, game_id, round_number, map_name,
                started_at, ended_at, winning_team_name, losing_team_name,
                end_reason, duration_seconds, tournament_name, tournament_year,
                ingested_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [
                round_id,
                series_id,
                game_id,
                round_number,
                map_name,
                started_at,
                ended_at,
                winning_team_name,
                losing_team_name,
                end_reason,
                duration_seconds,
                tournament_name,
                tournament_year,
            ],
        )

    def insert_event(
        self,
        event_id: str,
        occurred_at: str,
        series_id: str,
        game_id: str = None,
        round_id: str = None,
        event_type: str = None,
        actor_player_id: str = None,
        actor_player_name: str = None,
        actor_team_name: str = None,
        actor_agent_name: str = None,
        target_player_id: str = None,
        target_player_name: str = None,
        target_team_name: str = None,
        target_agent_name: str = None,
        action: str = None,
        tournament_name: str = None,
        tournament_year: int = None,
        map_name: str = None,
        weapon_name: str = None,
        weapon_type: str = None,
        is_headshot: bool = False,
        is_kill: bool = False,
        is_death: bool = False,
        is_assist: bool = False,
        is_first_blood: bool = False,
        is_plant: bool = False,
        is_defuse: bool = False,
        is_ability_use: bool = False,
        damage_dealt: float = None,
        metadata: str = None,
    ):
        """Insert base event.

        Args:
            event_id: Unique event ID
            occurred_at: Timestamp
            series_id: Parent series
            game_id: Parent game
            round_id: Parent round
            event_type: Type of event
            actor_player_id: Acting player ID
            actor_player_name: Acting player name
            actor_team_name: Acting team
            actor_agent_name: Acting agent
            target_player_id: Target player ID
            target_player_name: Target player name
            target_team_name: Target team
            target_agent_name: Target agent
            action: Action taken
            tournament_name: Tournament name
            tournament_year: Tournament year
            map_name: Map name
            weapon_name: Weapon used
            weapon_type: Weapon type
            is_headshot: Headshot flag
            is_kill: Kill flag
            is_death: Death flag
            is_assist: Assist flag
            is_first_blood: First blood flag
            is_plant: Spike plant flag
            is_defuse: Spike defuse flag
            is_ability_use: Ability use flag
            damage_dealt: Damage dealt
            metadata: JSON metadata
        """
        self.conn.execute(
            """
            INSERT OR REPLACE INTO base_events (
                event_id, occurred_at, series_id, game_id, round_id, event_type,
                actor_player_id, actor_player_name, actor_team_name, actor_agent_name,
                target_player_id, target_player_name, target_team_name, target_agent_name,
                action, tournament_name, tournament_year, map_name,
                weapon_name, weapon_type, is_headshot,
                is_kill, is_death, is_assist, is_first_blood,
                is_plant, is_defuse, is_ability_use, damage_dealt, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_id,
                occurred_at,
                series_id,
                game_id,
                round_id,
                event_type,
                actor_player_id,
                actor_player_name,
                actor_team_name,
                actor_agent_name,
                target_player_id,
                target_player_name,
                target_team_name,
                target_agent_name,
                action,
                tournament_name,
                tournament_year,
                map_name,
                weapon_name,
                weapon_type,
                is_headshot,
                is_kill,
                is_death,
                is_assist,
                is_first_blood,
                is_plant,
                is_defuse,
                is_ability_use,
                damage_dealt,
                metadata,
            ],
        )

    def bulk_insert_events(self, events: List[Dict]):
        """Bulk insert multiple events (much faster than individual inserts).

        Args:
            events: List of event dictionaries with all fields
        """
        if not events:
            return

        # Use executemany for batch insert
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO base_events (
                event_id, occurred_at, series_id, game_id, round_id, event_type,
                actor_player_id, actor_player_name, actor_team_name, actor_agent_name,
                target_player_id, target_player_name, target_team_name, target_agent_name,
                action, tournament_name, tournament_year, map_name,
                weapon_name, weapon_type, is_headshot,
                is_kill, is_death, is_assist, is_first_blood,
                is_plant, is_defuse, is_ability_use, damage_dealt, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                [
                    e.get('event_id'), e.get('occurred_at'), e.get('series_id'),
                    e.get('game_id'), e.get('round_id'), e.get('event_type'),
                    e.get('actor_player_id'), e.get('actor_player_name'),
                    e.get('actor_team_name'), e.get('actor_agent_name'),
                    e.get('target_player_id'), e.get('target_player_name'),
                    e.get('target_team_name'), e.get('target_agent_name'),
                    e.get('action'), e.get('tournament_name'), e.get('tournament_year'),
                    e.get('map_name'), e.get('weapon_name'), e.get('weapon_type'),
                    e.get('is_headshot', False), e.get('is_kill', False),
                    e.get('is_death', False), e.get('is_assist', False),
                    e.get('is_first_blood', False), e.get('is_plant', False),
                    e.get('is_defuse', False), e.get('is_ability_use', False),
                    e.get('damage_dealt'), e.get('metadata'),
                ]
                for e in events
            ]
        )

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

    def get_series_list(self) -> List[str]:
        """Get list of all series IDs in database.

        Returns:
            List of series IDs
        """
        results = self.conn.execute(
            "SELECT series_id FROM series ORDER BY ingested_at DESC"
        ).fetchall()
        return [row[0] for row in results]

    def get_database_stats(self) -> Dict[str, int]:
        """Get database statistics.

        Returns:
            Dictionary with counts of series, games, events, etc.
        """
        return {
            "series_count": self.conn.execute("SELECT COUNT(*) FROM series").fetchone()[0],
            "games_count": self.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0],
            "rounds_count": self.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0],
            "base_events_count": self.conn.execute("SELECT COUNT(*) FROM base_events").fetchone()[0],
        }
