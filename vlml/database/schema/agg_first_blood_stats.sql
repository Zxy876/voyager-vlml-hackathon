-- First blood events with round outcomes
-- Pre-joins first blood events with round context
-- One row per first blood event (one per round)

CREATE TABLE IF NOT EXISTS agg_first_blood_stats (
    -- Primary key
    round_id VARCHAR PRIMARY KEY,

    -- Context
    game_id VARCHAR,
    series_id VARCHAR,
    tournament_name VARCHAR,
    tournament_year INTEGER,
    map_name VARCHAR,
    round_number INTEGER,

    -- First blood details
    fb_team VARCHAR,
    fd_team VARCHAR,
    fb_player VARCHAR,
    fb_player_id VARCHAR,
    fb_agent VARCHAR,
    fd_player VARCHAR,
    fd_agent VARCHAR,
    fb_side VARCHAR,

    -- Round outcome
    winning_team_name VARCHAR,
    losing_team_name VARCHAR,
    end_reason VARCHAR,

    -- Calculated flags
    fb_team_won INTEGER DEFAULT 0,
    fd_team_won INTEGER DEFAULT 0,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
