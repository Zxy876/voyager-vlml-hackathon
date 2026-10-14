-- Player Game Stats: Aggregated player performance per game (map)
-- Grain: One row per (game_id, player_id)

CREATE TABLE IF NOT EXISTS agg_player_game_stats (
    game_id VARCHAR NOT NULL,
    player_id VARCHAR NOT NULL,
    player_name VARCHAR NOT NULL,
    PRIMARY KEY (game_id, player_id),

    -- Denormalized dimensions
    team_name VARCHAR,
    opponent_team_name VARCHAR,
    tournament_name VARCHAR,
    tournament_year INTEGER,
    map_name VARCHAR,
    agent_name VARCHAR,  -- Most played agent in this game

    -- Game context
    game_started_at TIMESTAMP,
    game_ended_at TIMESTAMP,
    game_won BOOLEAN,

    -- Aggregate metrics (sum across rounds)
    rounds_played INTEGER DEFAULT 0,
    rounds_won INTEGER DEFAULT 0,
    kills INTEGER DEFAULT 0,
    deaths INTEGER DEFAULT 0,
    assists INTEGER DEFAULT 0,
    first_bloods INTEGER DEFAULT 0,
    first_deaths INTEGER DEFAULT 0,
    plants INTEGER DEFAULT 0,
    defuses INTEGER DEFAULT 0,
    abilities_used INTEGER DEFAULT 0,
    damage_dealt FLOAT DEFAULT 0,
    damage_received FLOAT DEFAULT 0,

    -- Derived metrics
    kd_ratio FLOAT,  -- kills / deaths
    kda FLOAT,  -- (kills + assists) / deaths
    adr FLOAT,  -- average damage per round
    kpr FLOAT,  -- kills per round
    fk_percentage FLOAT,  -- first_bloods / rounds_played
    fd_percentage FLOAT,  -- first_deaths / rounds_played

    -- Composite scores
    kast_percentage FLOAT,
    impact_rating FLOAT,

    -- Trading aggregates
    trade_kills INTEGER DEFAULT 0,
    traded_deaths INTEGER DEFAULT 0,
    untraded_deaths INTEGER DEFAULT 0,
    trade_success_rate FLOAT,
    avg_trade_time FLOAT,

    -- Opening duel aggregates
    opening_kills INTEGER DEFAULT 0,
    opening_deaths INTEGER DEFAULT 0,
    fk_fd_differential INTEGER,
    opening_duel_win_rate FLOAT,

    -- Clutch aggregates
    clutches_attempted INTEGER DEFAULT 0,
    clutches_won INTEGER DEFAULT 0,
    clutch_win_rate FLOAT,
    clutches_1v1_won INTEGER DEFAULT 0,
    clutches_1v2_won INTEGER DEFAULT 0,
    clutches_1v3_won INTEGER DEFAULT 0,

    -- Multi-kill aggregates
    double_kills INTEGER DEFAULT 0,
    triple_kills INTEGER DEFAULT 0,
    quad_kills INTEGER DEFAULT 0,
    aces INTEGER DEFAULT 0,

    -- Economy performance
    eco_rounds_played INTEGER DEFAULT 0,
    eco_rounds_won INTEGER DEFAULT 0,
    eco_win_rate FLOAT,
    thrifty_count INTEGER DEFAULT 0,
    avg_loadout_value FLOAT,

    -- Consistency
    rating_variance FLOAT,
    first_half_rating FLOAT,
    second_half_rating FLOAT,
    half_diff FLOAT,

    -- Weapon aggregates
    total_headshot_kills INTEGER DEFAULT 0,
    headshot_kills_denom INTEGER DEFAULT 0,
    headshot_hits_total INTEGER DEFAULT 0,
    hits_total INTEGER DEFAULT 0,
    total_bodyshot_kills INTEGER DEFAULT 0,
    vandal_kills INTEGER DEFAULT 0,
    phantom_kills INTEGER DEFAULT 0,
    operator_kills INTEGER DEFAULT 0,
    sheriff_kills INTEGER DEFAULT 0,
    classic_kills INTEGER DEFAULT 0,
    rifle_kills INTEGER DEFAULT 0,
    smg_kills INTEGER DEFAULT 0,
    pistol_kills INTEGER DEFAULT 0,
    sniper_kills INTEGER DEFAULT 0,
    weapon_preference VARCHAR,
    is_operator_player BOOLEAN DEFAULT FALSE,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
