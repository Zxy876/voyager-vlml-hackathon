-- Post-plant events with round outcomes
-- Pre-joins plant events with round context
-- One row per plant event (one per round where plant occurred)

CREATE TABLE IF NOT EXISTS agg_post_plant_stats (
    -- Primary key
    round_id VARCHAR PRIMARY KEY,

    -- Context
    game_id VARCHAR,
    series_id VARCHAR,
    tournament_name VARCHAR,
    tournament_year INTEGER,
    map_name VARCHAR,
    round_number INTEGER,

    -- Plant details
    planting_team VARCHAR,
    defending_team VARCHAR,
    planter VARCHAR,
    planter_id VARCHAR,
    planter_agent VARCHAR,

    -- Round outcome
    winning_team_name VARCHAR,
    end_reason VARCHAR,

    -- Calculated flags
    plant_converted INTEGER DEFAULT 0,
    detonated INTEGER DEFAULT 0,
    defused INTEGER DEFAULT 0,
    attacker_elim_win INTEGER DEFAULT 0,
    defender_elim_win INTEGER DEFAULT 0,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
