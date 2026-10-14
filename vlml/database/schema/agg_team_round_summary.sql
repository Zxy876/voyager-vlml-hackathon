-- Team round summary
-- Aggregates player round stats to team level
-- One row per (round_id, team_name)

CREATE TABLE IF NOT EXISTS agg_team_round_summary (
    -- Composite primary key
    round_id VARCHAR NOT NULL,
    team_name VARCHAR NOT NULL,
    PRIMARY KEY (round_id, team_name),

    -- Context
    opponent_team_name VARCHAR,
    game_id VARCHAR,
    tournament_name VARCHAR,
    tournament_year INTEGER,
    map_name VARCHAR,
    round_number INTEGER,
    side VARCHAR,

    -- Outcome
    round_won INTEGER DEFAULT 0,

    -- Team aggregates
    team_kills INTEGER DEFAULT 0,
    team_deaths INTEGER DEFAULT 0,
    team_assists INTEGER DEFAULT 0,
    team_damage FLOAT DEFAULT 0,
    team_adr FLOAT DEFAULT 0,

    -- Opening duels
    team_fb INTEGER DEFAULT 0,
    team_fd INTEGER DEFAULT 0,
    fb_differential INTEGER DEFAULT 0,

    -- Survival & trading
    team_survivors INTEGER DEFAULT 0,
    team_deaths_traded INTEGER DEFAULT 0,
    team_trade_kills INTEGER DEFAULT 0,
    team_untraded_deaths INTEGER DEFAULT 0,

    -- Multi-kills
    team_multikills INTEGER DEFAULT 0,
    team_aces INTEGER DEFAULT 0,

    -- Objectives
    team_plants INTEGER DEFAULT 0,
    team_defuses INTEGER DEFAULT 0,

    -- Utility
    team_abilities INTEGER DEFAULT 0,
    team_early_util INTEGER DEFAULT 0,

    -- Clutches
    team_clutch_situations INTEGER DEFAULT 0,
    team_clutches_won INTEGER DEFAULT 0,

    -- KAST
    team_kast_count INTEGER DEFAULT 0,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
