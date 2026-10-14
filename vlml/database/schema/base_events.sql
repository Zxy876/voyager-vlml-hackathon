-- Atomic events table (finest grain)
CREATE TABLE IF NOT EXISTS base_events (
    event_id VARCHAR PRIMARY KEY,
    occurred_at TIMESTAMP NOT NULL,

    -- Context IDs
    series_id VARCHAR NOT NULL,
    game_id VARCHAR,
    round_id VARCHAR,

    -- Event type
    event_type VARCHAR NOT NULL,

    -- Actor (player performing action)
    actor_player_id VARCHAR,
    actor_player_name VARCHAR,
    actor_team_name VARCHAR,
    actor_agent_name VARCHAR,

    -- Target (player affected by action)
    target_player_id VARCHAR,
    target_player_name VARCHAR,
    target_team_name VARCHAR,
    target_agent_name VARCHAR,
    target_side VARCHAR,

    -- Positional context (when available)
    actor_side VARCHAR,
    actor_pos_x FLOAT,
    actor_pos_y FLOAT,
    target_pos_x FLOAT,
    target_pos_y FLOAT,

    -- Action
    action VARCHAR,

    -- Ability context (when available)
    ability_id VARCHAR,
    ability_name VARCHAR,
    ability_type VARCHAR,

    -- Denormalized dimensions (for fast filtering)
    tournament_name VARCHAR,
    tournament_year INTEGER,
    map_name VARCHAR,

    -- Event flags (pre-calculated booleans)
    is_kill BOOLEAN DEFAULT FALSE,
    is_death BOOLEAN DEFAULT FALSE,
    is_assist BOOLEAN DEFAULT FALSE,
    is_first_blood BOOLEAN DEFAULT FALSE,
    is_plant BOOLEAN DEFAULT FALSE,
    is_defuse BOOLEAN DEFAULT FALSE,
    is_begin_defuse BOOLEAN DEFAULT FALSE,
    is_stop_defuse BOOLEAN DEFAULT FALSE,
    is_half_defuse BOOLEAN DEFAULT FALSE,
    is_defuse_complete BOOLEAN DEFAULT FALSE,
    is_bomb_exploded BOOLEAN DEFAULT FALSE,
    is_ability_use BOOLEAN DEFAULT FALSE,

    -- Metrics
    damage_dealt FLOAT,
    actor_loadout_value INTEGER,
    actor_net_worth INTEGER,
    team_loadout_value INTEGER,
    team_net_worth INTEGER,

    -- Weapon information (for kill events)
    weapon_name VARCHAR,
    weapon_type VARCHAR,  -- rifle/smg/pistol/sniper/shotgun/heavy/melee
    is_headshot BOOLEAN DEFAULT FALSE,
    is_wallbang BOOLEAN DEFAULT FALSE,
    hit_location VARCHAR,  -- head/body/legs

    -- Raw metadata
    metadata JSON
);

