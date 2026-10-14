-- Tournament Stats: Aggregated statistics per tournament
-- Grain: One row per (tournament_id, team_name) or (tournament_id, player_id)
-- Use case: Tournament leaderboards and summaries

CREATE TABLE IF NOT EXISTS agg_tournament_stats (
    tournament_id VARCHAR NOT NULL,
    tournament_name VARCHAR NOT NULL,
    entity_type VARCHAR NOT NULL,  -- 'team' or 'player'
    entity_id VARCHAR NOT NULL,  -- team_name or player_id
    entity_name VARCHAR NOT NULL,  -- team_name or player_name
    PRIMARY KEY (tournament_id, entity_type, entity_id),

    -- Denormalized dimensions
    tournament_year INTEGER,
    tournament_region VARCHAR,

    -- Tournament context
    tournament_start_date DATE,
    tournament_end_date DATE,
    placement INTEGER,  -- Final ranking (1 = champion)
    prize_money FLOAT,  -- If available

    -- Activity metrics
    series_played INTEGER DEFAULT 0,
    series_won INTEGER DEFAULT 0,
    maps_played INTEGER DEFAULT 0,
    maps_won INTEGER DEFAULT 0,
    rounds_played INTEGER DEFAULT 0,
    rounds_won INTEGER DEFAULT 0,

    -- Aggregate metrics (sum across entire tournament)
    kills INTEGER DEFAULT 0,
    deaths INTEGER DEFAULT 0,
    assists INTEGER DEFAULT 0,
    first_bloods INTEGER DEFAULT 0,
    first_deaths INTEGER DEFAULT 0,
    plants INTEGER DEFAULT 0,
    defuses INTEGER DEFAULT 0,
    damage_dealt FLOAT DEFAULT 0,
    damage_received FLOAT DEFAULT 0,

    -- Derived metrics
    kd_ratio FLOAT,  -- kills / deaths
    kda FLOAT,  -- (kills + assists) / deaths
    adr FLOAT,  -- average damage per round
    kpr FLOAT,  -- kills per round
    fk_percentage FLOAT,  -- first_bloods / rounds_played
    fd_percentage FLOAT,  -- first_deaths / rounds_played
    win_rate FLOAT,  -- series_won / series_played
    map_win_rate FLOAT,  -- maps_won / maps_played

    -- Player-specific (NULL for teams)
    agents_played JSON,  -- Array of agents played
    unique_agents_count INTEGER,
    team_name VARCHAR,  -- Player's team

    -- Team-specific (NULL for players)
    roster JSON,  -- Array of player names on team

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
