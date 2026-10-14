"""Player data models."""
from typing import Optional
from pydantic import BaseModel, Field


class PlayerInfo(BaseModel):
    """Basic player information."""

    player_id: str
    nickname: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    nationality: Optional[str] = None
    team_name: Optional[str] = None
    team_id: Optional[str] = None


class PlayerStatistics(BaseModel):
    """Player performance statistics."""

    player_id: str
    nickname: str
    team_name: Optional[str] = None
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    kd_ratio: float = Field(default=0.0, description="Kill/Death ratio")
    average_combat_score: float = Field(default=0.0, alias="acs")
    average_damage_per_round: float = Field(default=0.0, alias="adr")
    headshot_percentage: Optional[float] = None
    first_kill_percentage: Optional[float] = Field(default=None, alias="fb_pct")
    first_death_percentage: Optional[float] = Field(default=None, alias="fd_pct")
    clutch_success_rate: Optional[float] = None

    class Config:
        populate_by_name = True


class AgentStats(BaseModel):
    """Agent-specific statistics."""

    agent_name: str
    agent_role: Optional[str] = None
    games_played: int = 0
    rounds_played: int = 0
    win_rate: float = 0.0
    average_kills: float = 0.0
    average_deaths: float = 0.0
    average_assists: float = 0.0
    average_combat_score: float = 0.0


class PlayerAgentPerformance(BaseModel):
    """Player performance breakdown by agent."""

    player_id: str
    nickname: str
    agents: list[AgentStats] = Field(default_factory=list)
