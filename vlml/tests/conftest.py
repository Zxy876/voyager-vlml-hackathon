"""Shared pytest fixtures for VLML tests."""
from __future__ import annotations

from datetime import datetime
from typing import Any, List

import duckdb
import pytest


class MockEventDatabase:
    """Mock EventDatabase for testing with in-memory DuckDB."""

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn
        self.db_path = ":memory:"
        self.read_only = False

    def query(self, sql: str, params: List[Any] = None) -> List[tuple]:
        """Execute a SQL query and return results."""
        if params:
            return self.conn.execute(sql, params).fetchall()
        return self.conn.execute(sql).fetchall()

    def close(self):
        """Close database connection."""
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def create_test_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create minimal schema for testing."""
    conn.execute("""
        CREATE TABLE series (
            series_id VARCHAR PRIMARY KEY,
            tournament_name VARCHAR,
            team1_name VARCHAR,
            team2_name VARCHAR,
            start_time TIMESTAMP,
            winning_team_name VARCHAR,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE games (
            game_id VARCHAR PRIMARY KEY,
            series_id VARCHAR,
            game_number INTEGER,
            map_name VARCHAR,
            winning_team_name VARCHAR,
            total_rounds INTEGER,
            game_started_at TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE rounds (
            round_id VARCHAR PRIMARY KEY,
            game_id VARCHAR,
            round_number INTEGER,
            winning_team_name VARCHAR,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE agg_player_game_stats (
            game_id VARCHAR,
            player_id VARCHAR,
            player_name VARCHAR,
            team_name VARCHAR,
            game_started_at TIMESTAMP,
            kills INTEGER DEFAULT 0,
            deaths INTEGER DEFAULT 0,
            assists INTEGER DEFAULT 0,
            PRIMARY KEY (game_id, player_id)
        )
    """)

    conn.execute("""
        CREATE TABLE agg_team_game_stats (
            game_id VARCHAR,
            team_name VARCHAR,
            series_id VARCHAR,
            game_started_at TIMESTAMP,
            rounds_won INTEGER DEFAULT 0,
            rounds_played INTEGER DEFAULT 0,
            PRIMARY KEY (game_id, team_name)
        )
    """)

    conn.execute("""
        CREATE TABLE agg_player_round_stats (
            round_id VARCHAR,
            player_id VARCHAR,
            player_name VARCHAR,
            team_name VARCHAR,
            kills INTEGER DEFAULT 0,
            deaths INTEGER DEFAULT 0,
            assists INTEGER DEFAULT 0,
            is_traded BOOLEAN DEFAULT FALSE,
            round_won BOOLEAN DEFAULT FALSE,
            is_opening_death BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (round_id, player_id)
        )
    """)


def insert_test_data(conn: duckdb.DuckDBPyConnection) -> None:
    """Insert minimal test data."""
    now = datetime.now()

    # Insert series
    conn.execute("""
        INSERT INTO series VALUES
        ('series-001', 'VCT Masters', 'Team Alpha', 'Team Beta', ?, 'Team Alpha', ?),
        ('series-002', 'VCT Champions', 'Team Alpha', 'Team Gamma', ?, 'Team Gamma', ?)
    """, [now, now, now, now])

    # Insert games
    conn.execute("""
        INSERT INTO games VALUES
        ('game-001', 'series-001', 1, 'Bind', 'Team Alpha', 24, ?),
        ('game-002', 'series-001', 2, 'Haven', 'Team Beta', 26, ?),
        ('game-003', 'series-002', 1, 'Ascent', 'Team Gamma', 22, ?)
    """, [now, now, now])

    # Insert rounds (4 per game)
    rounds_data = []
    for game_num in range(1, 4):
        game_id = f"game-00{game_num}"
        for round_num in range(1, 5):
            round_id = f"round-{game_num:03d}-{round_num:02d}"
            winner = "Team Alpha" if round_num % 2 == 1 else "Team Beta"
            if game_num == 3:
                winner = "Team Gamma" if round_num % 2 == 1 else "Team Alpha"
            rounds_data.append((round_id, game_id, round_num, winner, now))

    conn.executemany("""
        INSERT INTO rounds VALUES (?, ?, ?, ?, ?)
    """, rounds_data)

    # Insert agg_player_game_stats
    players = [
        ("player-001", "Alice", "Team Alpha"),
        ("player-002", "Bob", "Team Alpha"),
        ("player-003", "Charlie", "Team Beta"),
        ("player-004", "Diana", "Team Beta"),
        ("player-005", "Eve", "Team Gamma"),
    ]

    for game_id in ["game-001", "game-002", "game-003"]:
        for player_id, player_name, team_name in players:
            # Skip Team Gamma players for series-001 games
            if team_name == "Team Gamma" and game_id in ["game-001", "game-002"]:
                continue
            # Skip Team Beta players for series-002 games
            if team_name == "Team Beta" and game_id == "game-003":
                continue
            conn.execute("""
                INSERT INTO agg_player_game_stats VALUES (?, ?, ?, ?, ?, 10, 8, 5)
            """, [game_id, player_id, player_name, team_name, now])

    # Insert agg_team_game_stats
    conn.execute("""
        INSERT INTO agg_team_game_stats VALUES
        ('game-001', 'Team Alpha', 'series-001', ?, 13, 24),
        ('game-001', 'Team Beta', 'series-001', ?, 11, 24),
        ('game-002', 'Team Alpha', 'series-001', ?, 12, 26),
        ('game-002', 'Team Beta', 'series-001', ?, 14, 26),
        ('game-003', 'Team Alpha', 'series-002', ?, 9, 22),
        ('game-003', 'Team Gamma', 'series-002', ?, 13, 22)
    """, [now, now, now, now, now, now])

    # Insert agg_player_round_stats (minimal data for KAST and opening death tests)
    player_round_data = []
    for game_num in range(1, 4):
        game_id = f"game-00{game_num}"
        for round_num in range(1, 5):
            round_id = f"round-{game_num:03d}-{round_num:02d}"
            round_won = round_num % 2 == 1  # Odd rounds won by first team

            for player_id, player_name, team_name in players:
                # Skip teams not in this game
                if team_name == "Team Gamma" and game_id in ["game-001", "game-002"]:
                    continue
                if team_name == "Team Beta" and game_id == "game-003":
                    continue

                # Player stats
                kills = 1 if round_num % 3 == 0 else 0
                deaths = 1 if round_num % 2 == 0 else 0
                assists = 1 if round_num == 1 else 0
                is_traded = round_num == 2
                is_opening_death = player_name == "Alice" and round_num == 1
                team_won = (team_name == "Team Alpha" and round_won) or \
                           (team_name == "Team Beta" and not round_won and game_num < 3) or \
                           (team_name == "Team Gamma" and round_won and game_num == 3)

                player_round_data.append((
                    round_id, player_id, player_name, team_name,
                    kills, deaths, assists, is_traded, team_won, is_opening_death
                ))

    conn.executemany("""
        INSERT INTO agg_player_round_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, player_round_data)


@pytest.fixture
def mock_db():
    """Create in-memory DuckDB with test data."""
    conn = duckdb.connect(":memory:")
    create_test_schema(conn)
    insert_test_data(conn)
    db = MockEventDatabase(conn)
    yield db
    conn.close()


@pytest.fixture
def empty_mock_db():
    """Create in-memory DuckDB with schema but no data."""
    conn = duckdb.connect(":memory:")
    create_test_schema(conn)
    db = MockEventDatabase(conn)
    yield db
    conn.close()
