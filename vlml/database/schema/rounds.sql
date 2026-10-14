-- Rounds table: Per-round metadata
-- Grain: One row per round

CREATE TABLE IF NOT EXISTS rounds (
    round_id VARCHAR PRIMARY KEY,
    series_id VARCHAR NOT NULL,
    game_id VARCHAR NOT NULL,
    round_number INTEGER NOT NULL,

    -- Round context
    map_name VARCHAR,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    duration_seconds INTEGER,

    -- Outcome
    winning_team_name VARCHAR,
    losing_team_name VARCHAR,
    end_reason VARCHAR,  -- 'eliminated', 'defused', 'detonated', 'time'

    -- Denormalized dimensions
    tournament_name VARCHAR,
    tournament_year INTEGER,

    -- Metadata
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
