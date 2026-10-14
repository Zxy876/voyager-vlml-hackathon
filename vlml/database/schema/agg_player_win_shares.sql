-- Player win shares
-- Pre-calculated win shares per player per game
-- One row per (player_id, game_id)

CREATE TABLE IF NOT EXISTS agg_player_win_shares (
    -- Composite primary key
    player_id VARCHAR NOT NULL,
    game_id VARCHAR NOT NULL,
    PRIMARY KEY (player_id, game_id),

    -- Context
    player_name VARCHAR,
    team_name VARCHAR,
    opponent_team_name VARCHAR,
    tournament_name VARCHAR,
    tournament_year INTEGER,
    map_name VARCHAR,
    agent_name VARCHAR,
    agent_role VARCHAR,

    -- Raw counts
    rounds_played INTEGER DEFAULT 0,
    rounds_won INTEGER DEFAULT 0,

    -- Win share components (raw counts)
    first_bloods INTEGER DEFAULT 0,
    first_deaths INTEGER DEFAULT 0,
    trade_kills INTEGER DEFAULT 0,
    survivals INTEGER DEFAULT 0,
    deaths_traded INTEGER DEFAULT 0,
    multi_kills INTEGER DEFAULT 0,
    plants INTEGER DEFAULT 0,
    defuses INTEGER DEFAULT 0,

    -- Win share components (weighted)
    fb_win_share FLOAT DEFAULT 0,
    trade_kill_share FLOAT DEFAULT 0,
    survival_share FLOAT DEFAULT 0,
    traded_death_share FLOAT DEFAULT 0,
    multikill_share FLOAT DEFAULT 0,
    plant_share FLOAT DEFAULT 0,
    defuse_share FLOAT DEFAULT 0,

    -- Total win share
    total_win_share FLOAT DEFAULT 0,
    win_share_per_round FLOAT DEFAULT 0,

    -- Efficiency metrics
    opening_duel_efficiency FLOAT,
    survival_rate FLOAT,
    trade_efficiency FLOAT,

    -- Additional context
    adr FLOAT,
    kd_ratio FLOAT,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
