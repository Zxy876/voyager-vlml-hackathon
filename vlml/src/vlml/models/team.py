"""Team data models."""
from typing import Optional
from pydantic import BaseModel, Field


class TeamPlayer(BaseModel):
    """Team roster player."""

    player_id: str
    nickname: str
    role: Optional[str] = None


class TeamInfo(BaseModel):
    """Basic team information."""

    team_id: str
    name: str
    short_name: Optional[str] = None
    region: Optional[str] = None
    players: list[TeamPlayer] = Field(default_factory=list)


class TeamStatistics(BaseModel):
    """Team performance statistics."""

    team_id: str
    name: str
    matches_played: int = 0
    matches_won: int = 0
    matches_lost: int = 0
    win_rate: float = 0.0
    rounds_played: int = 0
    rounds_won: int = 0
    rounds_lost: int = 0
    attack_round_win_rate: float = 0.0
    defense_round_win_rate: float = 0.0


class MapStats(BaseModel):
    """Map-specific statistics."""

    map_name: str
    played: int = 0
    won: int = 0
    lost: int = 0
    win_rate: float = 0.0
    attack_round_win_rate: float = 0.0
    defense_round_win_rate: float = 0.0
    pick_rate: Optional[float] = None
    ban_rate: Optional[float] = None


class TeamMapPerformance(BaseModel):
    """Team performance breakdown by map."""

    team_id: str
    name: str
    maps: list[MapStats] = Field(default_factory=list)
