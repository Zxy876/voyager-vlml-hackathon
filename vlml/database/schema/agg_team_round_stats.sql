-- Team Round Stats: Aggregated team performance per round
-- Grain: One row per (round_id, team_name)

CREATE TABLE IF NOT EXISTS agg_team_round_stats (
    round_id VARCHAR NOT NULL,
    team_name VARCHAR NOT NULL,
    PRIMARY KEY (round_id, team_name),

    -- Denormalized dimensions
    opponent_team_name VARCHAR,
    tournament_name VARCHAR,
    tournament_year INTEGER,
    map_name VARCHAR,
    round_number INTEGER,

    -- Round context
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    round_won BOOLEAN,
    side VARCHAR,  -- 'attack', 'defense'
    end_reason VARCHAR,  -- 'eliminated', 'defused', 'detonated', 'time'
    team_score_before INTEGER,
    enemy_score_before INTEGER,
    prev_round_won BOOLEAN,
    current_win_streak INTEGER,
    current_loss_streak INTEGER,
    is_match_point BOOLEAN,
    is_ot BOOLEAN,

    -- Aggregate metrics (sum across team members)
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

    -- Team-specific metrics
    players_alive_at_end INTEGER,  -- Survivors
    entry_kill BOOLEAN,  -- Team got first kill
    entry_death BOOLEAN,  -- Team got first death

    -- Economy (if available)
    team_credits_spent INTEGER,
    loadout_value INTEGER,
    net_worth INTEGER,
    eco_rounds_total INTEGER DEFAULT 0,
    force_buy_rounds_total INTEGER DEFAULT 0,
    full_buy_rounds_total INTEGER DEFAULT 0,

    -- Tempo
    round_duration_s FLOAT,
    time_to_first_contact_s FLOAT,
    time_to_first_kill_s FLOAT,
    time_to_first_death_s FLOAT,
    time_to_plant_s FLOAT,
    post_plant_duration_s FLOAT,

    -- First blood / entry (totals/denoms)
    fb_won_total INTEGER DEFAULT 0,
    fb_converted_total INTEGER DEFAULT 0,
    fb_attempts_total INTEGER DEFAULT 0,
    fb_traded_total INTEGER DEFAULT 0,
    fb_trade_delay_sum_s FLOAT DEFAULT 0,
    fb_trade_delay_denom INTEGER DEFAULT 0,

    -- Trades
    deaths_traded_total INTEGER DEFAULT 0,
    deaths_untraded_total INTEGER DEFAULT 0,
    avg_trade_delay_sum_s FLOAT DEFAULT 0,
    avg_trade_delay_denom INTEGER DEFAULT 0,

    -- Post-plant depth
    post_plant_kills_total INTEGER DEFAULT 0,
    post_plant_deaths_total INTEGER DEFAULT 0,
    retake_attempted_total INTEGER DEFAULT 0,
    retake_kills_total INTEGER DEFAULT 0,
    defuse_attempts_total INTEGER DEFAULT 0,
    defuse_denied_total INTEGER DEFAULT 0,
    defuse_denied_denom INTEGER DEFAULT 0,
    defuse_commit_total INTEGER DEFAULT 0,
    defuse_commit_success_total INTEGER DEFAULT 0,
    defuse_commit_denom INTEGER DEFAULT 0,
    half_defuse_taps_total INTEGER DEFAULT 0,
    half_defuse_bait_success_total INTEGER DEFAULT 0,
    half_defuse_bait_denom INTEGER DEFAULT 0,
    time_to_defuse_attempt_sum_s FLOAT DEFAULT 0,
    time_to_defuse_attempt_denom INTEGER DEFAULT 0,
    bomb_time_remaining_sum_s FLOAT DEFAULT 0,
    bomb_time_remaining_denom INTEGER DEFAULT 0,

    -- Utility effectiveness
    util_used_total INTEGER DEFAULT 0,
    flash_used_total INTEGER DEFAULT 0,
    smoke_used_total INTEGER DEFAULT 0,
    molly_used_total INTEGER DEFAULT 0,
    recon_used_total INTEGER DEFAULT 0,
    other_util_used_total INTEGER DEFAULT 0,
    util_effect_kills_total INTEGER DEFAULT 0,
    util_effect_kills_denom INTEGER DEFAULT 0,
    flash_assist_kills_total INTEGER DEFAULT 0,
    flash_assist_kills_denom INTEGER DEFAULT 0,
    self_flash_kills_total INTEGER DEFAULT 0,
    self_flash_kills_denom INTEGER DEFAULT 0,
    util_dump_rounds_total INTEGER DEFAULT 0,
    util_dump_rounds_denom INTEGER DEFAULT 0,

    -- Map control
    site_hit_A_total INTEGER DEFAULT 0,
    site_hit_B_total INTEGER DEFAULT 0,
    site_hit_mid_total INTEGER DEFAULT 0,
    site_hit_denom INTEGER DEFAULT 0,
    mid_control_total INTEGER DEFAULT 0,
    mid_control_denom INTEGER DEFAULT 0,
    fake_rounds_total INTEGER DEFAULT 0,
    fake_rounds_denom INTEGER DEFAULT 0,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
