"""Tournament data models."""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class Tournament(BaseModel):
    """Tournament information."""

    tournament_id: str
    name: str
    region: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    status: Optional[str] = None
    prize_pool: Optional[str] = None


class TeamStanding(BaseModel):
    """Team standing in a tournament."""

    rank: int
    team_id: str
    team_name: str
    matches_played: int = 0
    matches_won: int = 0
    matches_lost: int = 0
    points: int = 0


class TournamentStandings(BaseModel):
    """Tournament standings."""

    tournament_id: str
    tournament_name: str
    standings: list[TeamStanding] = Field(default_factory=list)
