-- Player Series Stats: Aggregated player performance per series (match)
-- Grain: One row per (series_id, player_id)

CREATE TABLE IF NOT EXISTS agg_player_series_stats (
    series_id VARCHAR NOT NULL,
    player_id VARCHAR NOT NULL,
    player_name VARCHAR NOT NULL,
    PRIMARY KEY (series_id, player_id),

    -- Denormalized dimensions
    team_name VARCHAR,
    opponent_team_name VARCHAR,
    tournament_name VARCHAR,
    tournament_year INTEGER,
    tournament_region VARCHAR,

    -- Series context
    series_started_at TIMESTAMP,
    series_won BOOLEAN,
    maps_played INTEGER DEFAULT 0,
    maps_won INTEGER DEFAULT 0,

    -- Aggregate metrics (sum across all games)
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
    acs FLOAT,  -- average combat score (if available)

    -- Agent pool (JSON array of agents played)
    agents_played JSON,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
