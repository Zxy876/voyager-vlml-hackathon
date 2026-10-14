-- Team Game Stats: Aggregated team performance per game (map)
-- Grain: One row per (game_id, team_name)

CREATE TABLE IF NOT EXISTS agg_team_game_stats (
    game_id VARCHAR NOT NULL,
    team_name VARCHAR NOT NULL,
    PRIMARY KEY (game_id, team_name),

    -- Denormalized dimensions
    series_id VARCHAR NOT NULL,
    opponent_team_name VARCHAR,
    tournament_name VARCHAR,
    tournament_year INTEGER,
    map_name VARCHAR,

    -- Game context
    game_started_at TIMESTAMP,
    game_ended_at TIMESTAMP,
    game_won BOOLEAN,
    rounds_won INTEGER DEFAULT 0,
    rounds_lost INTEGER DEFAULT 0,

    -- Side performance
    attack_rounds_won INTEGER DEFAULT 0,
    attack_rounds_played INTEGER DEFAULT 0,
    defense_rounds_won INTEGER DEFAULT 0,
    defense_rounds_played INTEGER DEFAULT 0,

    -- Aggregate metrics (sum across all rounds)
    team_kills INTEGER DEFAULT 0,
    team_deaths INTEGER DEFAULT 0,
    team_assists INTEGER DEFAULT 0,
    first_bloods INTEGER DEFAULT 0,
    first_deaths INTEGER DEFAULT 0,
    plants INTEGER DEFAULT 0,
    defuses INTEGER DEFAULT 0,
    abilities_used INTEGER DEFAULT 0,
    team_damage_dealt FLOAT DEFAULT 0,
    team_damage_received FLOAT DEFAULT 0,

    -- Derived metrics
    kd_ratio FLOAT,  -- team_kills / team_deaths
    adr FLOAT,  -- average damage per round
    kpr FLOAT,  -- kills per round
    attack_win_rate FLOAT,  -- attack_rounds_won / attack_rounds_played
    defense_win_rate FLOAT,  -- defense_rounds_won / defense_rounds_played
    fk_percentage FLOAT,  -- first_bloods / rounds_played
    fd_percentage FLOAT,  -- first_deaths / rounds_played

    -- Agent composition (JSON array)
    agents_played JSON,

    -- Team composition metrics
    num_duelists INTEGER DEFAULT 0,
    num_initiators INTEGER DEFAULT 0,
    num_controllers INTEGER DEFAULT 0,
    num_sentinels INTEGER DEFAULT 0,
    is_double_duelist BOOLEAN DEFAULT FALSE,
    is_no_duelist BOOLEAN DEFAULT FALSE,
    agent_comp_string VARCHAR,  -- e.g., "2D-1I-1C-1S"

    -- Opening duels
    entry_duels_won INTEGER DEFAULT 0,  -- Team got first blood
    entry_duels_lost INTEGER DEFAULT 0,  -- Team got first death
    opening_duel_win_rate FLOAT,  -- entry_won / (entry_won + entry_lost)
    fk_conversion_rate FLOAT,  -- Win% when getting FK
    fd_loss_rate FLOAT,  -- Loss% when getting FD

    -- Trading
    team_trade_success_rate FLOAT,
    team_untraded_deaths INTEGER DEFAULT 0,

    -- Special rounds
    pistol_rounds_won INTEGER DEFAULT 0,  -- Rounds 1, 13, 25
    pistol_rounds_played INTEGER DEFAULT 0,
    pistol_win_rate FLOAT,
    bonus_rounds_won INTEGER DEFAULT 0,  -- Rounds after pistol
    anti_eco_rounds_won INTEGER DEFAULT 0,  -- Full buy vs eco wins

    -- Multi-kills
    aces_count INTEGER DEFAULT 0,  -- 5k rounds
    quad_kills_count INTEGER DEFAULT 0,  -- 4k rounds
    triple_kills_count INTEGER DEFAULT 0,  -- 3k rounds

    -- Clutch performance
    clutches_attempted INTEGER DEFAULT 0,
    clutches_won INTEGER DEFAULT 0,
    clutch_win_rate FLOAT,

    -- Situational
    rounds_5v4 INTEGER DEFAULT 0,
    wins_5v4 INTEGER DEFAULT 0,
    conversion_5v4 FLOAT,
    rounds_4v5 INTEGER DEFAULT 0,
    wins_4v5 INTEGER DEFAULT 0,
    comeback_4v5 FLOAT,
    post_plant_rounds INTEGER DEFAULT 0,
    post_plant_wins INTEGER DEFAULT 0,
    post_plant_win_rate FLOAT,

    -- Momentum
    longest_win_streak INTEGER DEFAULT 0,
    longest_loss_streak INTEGER DEFAULT 0,
    rounds_after_timeout INTEGER DEFAULT 0,
    wins_after_timeout INTEGER DEFAULT 0,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
