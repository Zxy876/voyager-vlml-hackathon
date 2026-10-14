"""Match/Series data models."""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class TeamScore(BaseModel):
    """Team score in a match."""

    team_id: str
    team_name: str
    score: int
    is_winner: bool = False


class MatchSummary(BaseModel):
    """Brief match summary."""

    series_id: str
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    tournament_name: Optional[str] = None
    teams: list[TeamScore] = Field(default_factory=list)


class PlayerGameStats(BaseModel):
    """Player stats for a single game."""

    player_nickname: str
    agent_name: Optional[str] = None
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    combat_score: int = 0
    first_kills: int = 0
    first_deaths: int = 0


class GameDetails(BaseModel):
    """Single game/map details."""

    game_id: str
    map_name: str
    start_time: Optional[datetime] = None
    teams: list[TeamScore] = Field(default_factory=list)
    players: list[PlayerGameStats] = Field(default_factory=list)


class SeriesDetails(BaseModel):
    """Full series details with all games."""

    series_id: str
    format: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    tournament_name: Optional[str] = None
    teams: list[TeamScore] = Field(default_factory=list)
    games: list[GameDetails] = Field(default_factory=list)
