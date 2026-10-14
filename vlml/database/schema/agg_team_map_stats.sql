-- Team map stats
-- Team performance aggregated by map and opponent
-- One row per (team_name, opponent_team_name, map_name, tournament_name)

CREATE TABLE IF NOT EXISTS agg_team_map_stats (
    -- Composite primary key
    team_name VARCHAR NOT NULL,
    opponent_team_name VARCHAR,
    map_name VARCHAR NOT NULL,
    tournament_name VARCHAR NOT NULL,
    PRIMARY KEY (team_name, map_name, tournament_name),

    -- Game level stats
    games_played INTEGER DEFAULT 0,
    games_won INTEGER DEFAULT 0,
    games_lost INTEGER DEFAULT 0,
    map_win_rate FLOAT,

    -- Round level stats
    total_rounds INTEGER DEFAULT 0,
    rounds_won INTEGER DEFAULT 0,
    round_win_rate FLOAT,

    -- Performance metrics
    avg_adr FLOAT,
    avg_kd FLOAT,
    avg_kast FLOAT,

    -- Opening duels
    total_fb INTEGER DEFAULT 0,
    total_fd INTEGER DEFAULT 0,
    avg_opening_wr FLOAT,

    -- Trading
    avg_trade_rate FLOAT,

    -- Clutches
    total_clutches_won INTEGER DEFAULT 0,
    total_clutches_attempted INTEGER DEFAULT 0,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
