-- Player performance per round (pre-aggregated)
CREATE TABLE IF NOT EXISTS agg_player_round_stats (
    -- Composite primary key
    round_id VARCHAR NOT NULL,
    player_id VARCHAR NOT NULL,
    player_name VARCHAR NOT NULL,
    PRIMARY KEY (round_id, player_id),

    -- Dimensions (denormalized)
    team_name VARCHAR,
    opponent_team_name VARCHAR,
    tournament_name VARCHAR,
    tournament_year INTEGER,
    map_name VARCHAR,
    agent_name VARCHAR,
    round_number INTEGER,

    -- Round context
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    round_won BOOLEAN,
    side VARCHAR,  -- 'attack' or 'defense'

    -- Pre-calculated metrics
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
    survived BOOLEAN,

    -- Timing metrics
    time_first_blood TIMESTAMP,
    time_first_death TIMESTAMP,
    time_alive FLOAT,  -- Seconds alive in round
    time_to_first_kill FLOAT,  -- Seconds until first kill

    -- Agent role
    agent_role VARCHAR,  -- duelist/initiator/controller/sentinel
    is_duelist BOOLEAN DEFAULT FALSE,
    is_initiator BOOLEAN DEFAULT FALSE,
    is_controller BOOLEAN DEFAULT FALSE,
    is_sentinel BOOLEAN DEFAULT FALSE,

    -- Combat efficiency
    kast BOOLEAN,  -- Kill/Assist/Survive/Trade
    damage_per_kill FLOAT,
    overkill_damage FLOAT,

    -- Performance flags
    is_entry_fragger BOOLEAN DEFAULT FALSE,  -- First kill on attack side
    is_opening_kill BOOLEAN DEFAULT FALSE,  -- First kill of round (any side)
    is_opening_death BOOLEAN DEFAULT FALSE,  -- First death of round
    is_entry_denied BOOLEAN DEFAULT FALSE,  -- First death on attack
    is_traded BOOLEAN DEFAULT FALSE,  -- Died but teammate got revenge
    is_trade_kill BOOLEAN DEFAULT FALSE,  -- Got revenge kill
    trade_kill_time FLOAT,  -- Seconds after teammate death
    is_untraded_death BOOLEAN DEFAULT FALSE,
    multi_kill_count INTEGER DEFAULT 0,  -- 0-5 (ace)

    -- Multi-kills
    is_double_kill BOOLEAN DEFAULT FALSE,
    is_triple_kill BOOLEAN DEFAULT FALSE,
    is_quad_kill BOOLEAN DEFAULT FALSE,
    is_ace BOOLEAN DEFAULT FALSE,

    -- Clutch situations
    is_clutch BOOLEAN DEFAULT FALSE,  -- In any 1vX situation
    is_1v1 BOOLEAN DEFAULT FALSE,
    is_1v2 BOOLEAN DEFAULT FALSE,
    is_1v3 BOOLEAN DEFAULT FALSE,
    is_1v4 BOOLEAN DEFAULT FALSE,
    is_1v5 BOOLEAN DEFAULT FALSE,
    clutch_won BOOLEAN,  -- NULL if not clutch
    clutch_lost BOOLEAN,  -- NULL if not clutch
    clutch_opponents INTEGER,
    -- Economy
    loadout_value INTEGER,
    net_worth INTEGER,
    is_eco_round BOOLEAN DEFAULT FALSE,
    is_force_buy BOOLEAN DEFAULT FALSE,
    is_full_buy BOOLEAN DEFAULT FALSE,
    is_thrifty BOOLEAN DEFAULT FALSE,

    -- Ability usage
    flash_assists INTEGER DEFAULT 0,
    early_util BOOLEAN DEFAULT FALSE,

    -- Weapon stats
    weapon_name VARCHAR,
    weapon_type VARCHAR,
    total_headshot_kills INTEGER DEFAULT 0,
    headshot_kills_denom INTEGER DEFAULT 0,
    headshot_hits_total INTEGER DEFAULT 0,
    hits_total INTEGER DEFAULT 0,
    total_hits INTEGER DEFAULT 0,
    bodyshot_kills INTEGER DEFAULT 0,
    rifle_kills INTEGER DEFAULT 0,
    smg_kills INTEGER DEFAULT 0,
    pistol_kills INTEGER DEFAULT 0,
    sniper_kills INTEGER DEFAULT 0,
    shotgun_kills INTEGER DEFAULT 0,

    -- Utility effectiveness (totals/denoms)
    util_used_total INTEGER DEFAULT 0,
    flash_used_total INTEGER DEFAULT 0,
    util_effect_kills_total INTEGER DEFAULT 0,
    util_effect_kills_denom INTEGER DEFAULT 0,
    flash_assist_kills_total INTEGER DEFAULT 0,
    flash_assist_kills_denom INTEGER DEFAULT 0,
    self_flash_kills_total INTEGER DEFAULT 0,
    self_flash_kills_denom INTEGER DEFAULT 0,
    kill_distance_sum FLOAT DEFAULT 0,
    kill_distance_denom INTEGER DEFAULT 0,
    trade_kill_distance_sum FLOAT DEFAULT 0,
    trade_kill_distance_denom INTEGER DEFAULT 0,

    -- Duel mechanics (totals/denoms)
    duel_initiated_total INTEGER DEFAULT 0,
    duel_initiated_wins_total INTEGER DEFAULT 0,
    duel_initiated_denom INTEGER DEFAULT 0,
    duel_held_wins_total INTEGER DEFAULT 0,
    duel_held_denom INTEGER DEFAULT 0,
    duel_resolution_time_sum_s FLOAT DEFAULT 0,
    duel_resolution_time_denom INTEGER DEFAULT 0,
    duel_wins_rifle_total INTEGER DEFAULT 0,
    duel_losses_rifle_total INTEGER DEFAULT 0,
    duel_rifle_denom INTEGER DEFAULT 0,
    duel_wins_smg_total INTEGER DEFAULT 0,
    duel_losses_smg_total INTEGER DEFAULT 0,
    duel_smg_denom INTEGER DEFAULT 0,
    duel_wins_pistol_total INTEGER DEFAULT 0,
    duel_losses_pistol_total INTEGER DEFAULT 0,
    duel_pistol_denom INTEGER DEFAULT 0,
    duel_wins_sniper_total INTEGER DEFAULT 0,
    duel_losses_sniper_total INTEGER DEFAULT 0,
    duel_sniper_denom INTEGER DEFAULT 0,
    duel_wins_shotgun_total INTEGER DEFAULT 0,
    duel_losses_shotgun_total INTEGER DEFAULT 0,
    duel_shotgun_denom INTEGER DEFAULT 0,
    repeek_deaths_total INTEGER DEFAULT 0,
    repeek_deaths_denom INTEGER DEFAULT 0,

    -- Positioning & spacing (totals/denoms)
    iso_deaths_total INTEGER DEFAULT 0,
    iso_deaths_denom INTEGER DEFAULT 0,
    stack_deaths_total INTEGER DEFAULT 0,
    stack_deaths_denom INTEGER DEFAULT 0,

    -- Survival (sum/denom for averages)
    survival_time_sum_s FLOAT DEFAULT 0,
    survival_time_denom INTEGER DEFAULT 0,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
