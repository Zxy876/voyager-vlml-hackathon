-- Team series stats
-- Head-to-head records at series level
-- Both team perspectives per series (2 rows per series)

CREATE TABLE IF NOT EXISTS agg_team_series_stats (
    -- Composite primary key
    series_id VARCHAR NOT NULL,
    team_name VARCHAR NOT NULL,
    PRIMARY KEY (series_id, team_name),

    -- Context
    tournament_name VARCHAR,
    tournament_year INTEGER,
    start_time TIMESTAMP,
    opponent_name VARCHAR,

    -- Series outcome
    series_won INTEGER DEFAULT 0,
    series_lost INTEGER DEFAULT 0,

    -- Map breakdown
    maps_played INTEGER DEFAULT 0,
    maps_won INTEGER DEFAULT 0,
    maps_lost INTEGER DEFAULT 0,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
