-- Attack Pattern Context: First contact location and timing for attack pattern analysis
-- Provides site selection tendencies and execute timing patterns
-- Usage: Filtered by series_ids and team_name (ILIKE), side = 'attack'

WITH first_kills AS (
    -- Get first kill of each round with timing and position
    SELECT
        be.round_id,
        be.game_id,
        be.actor_player_name AS first_killer,
        be.actor_team_name AS first_kill_team,
        be.target_player_name AS first_victim,
        be.target_team_name AS first_death_team,
        be.actor_pos_x,
        be.actor_pos_y,
        be.target_pos_x,
        be.target_pos_y,
        be.map_name,
        -- Time into round (approximate from round start)
        EXTRACT(EPOCH FROM (be.occurred_at - r.started_at)) AS time_into_round_s,
        ROW_NUMBER() OVER (PARTITION BY be.round_id ORDER BY be.occurred_at) AS kill_order
    FROM base_events be
    JOIN rounds r ON r.round_id = be.round_id
    JOIN games g ON g.game_id = r.game_id
    WHERE be.is_kill = TRUE
      AND g.series_id IN ({series_clause})
    ORDER BY be.occurred_at
),
plant_events AS (
    -- Get plant timing and location
    SELECT
        be.round_id,
        be.actor_team_name AS planting_team,
        be.actor_pos_x AS plant_x,
        be.actor_pos_y AS plant_y,
        EXTRACT(EPOCH FROM (be.occurred_at - r.started_at)) AS time_to_plant_s
    FROM base_events be
    JOIN rounds r ON r.round_id = be.round_id
    JOIN games g ON g.game_id = r.game_id
    WHERE be.is_plant = TRUE
      AND g.series_id IN ({series_clause})
),
attack_rounds AS (
    -- Get all rounds for the team - filter to likely attack rounds
    -- Note: side may be NULL in some datasets, so we include all and filter by plants
    SELECT
        trs.round_id,
        g.game_id,
        g.game_number,
        g.map_name,
        trs.team_name,
        r.round_number,
        trs.round_won,
        trs.loadout_value,
        trs.side,
        -- Site hit data (if plant occurred)
        trs.site_hit_A_total,
        trs.site_hit_B_total,
        trs.site_hit_mid_total,
        -- Timing
        trs.time_to_first_contact_s,
        trs.time_to_first_kill_s,
        trs.time_to_plant_s,
        -- Plants
        trs.plants,
        -- Outcome
        r.end_reason,
        r.winning_team_name
    FROM agg_team_round_stats trs
    JOIN rounds r ON r.round_id = trs.round_id
    JOIN games g ON g.game_id = r.game_id
    WHERE g.series_id IN ({series_clause})
      AND trs.team_name ILIKE ?
      AND (trs.side = 'attack' OR trs.plants > 0 OR trs.side IS NULL)
      {map_filter}
)
SELECT
    ar.round_id,
    ar.game_id,
    ar.game_number,
    ar.map_name,
    ar.team_name,
    ar.round_number,
    ar.round_won,
    ar.loadout_value,
    ar.side,
    -- First contact info
    CASE
        WHEN fk.time_into_round_s IS NOT NULL THEN
            CASE
                WHEN fk.time_into_round_s < 30 THEN 'early'
                WHEN fk.time_into_round_s < 60 THEN 'mid'
                ELSE 'late'
            END
        ELSE NULL
    END AS execute_timing,
    ROUND(fk.time_into_round_s::DECIMAL, 1) AS first_contact_time_s,
    -- First blood context
    CASE
        WHEN fk.first_kill_team = ar.team_name THEN 'team'
        ELSE 'opponent'
    END AS first_blood_for,
    fk.first_killer,
    fk.first_victim,
    -- Site hit determination
    CASE
        WHEN ar.site_hit_A_total > 0 THEN 'A'
        WHEN ar.site_hit_B_total > 0 THEN 'B'
        WHEN ar.site_hit_mid_total > 0 THEN 'Mid'
        WHEN ar.plants > 0 THEN 'Unknown'
        ELSE 'No Plant'
    END AS site_hit,
    ar.plants,
    -- Plant timing
    ROUND(pe.time_to_plant_s::DECIMAL, 1) AS plant_time_s,
    CASE
        WHEN pe.time_to_plant_s IS NOT NULL THEN
            CASE
                WHEN pe.time_to_plant_s < 45 THEN 'fast'
                WHEN pe.time_to_plant_s < 75 THEN 'normal'
                ELSE 'slow'
            END
        ELSE NULL
    END AS plant_speed,
    -- Round outcome
    ar.end_reason,
    ar.winning_team_name,
    -- Execute timing flag (>60s = late execute)
    CASE
        WHEN fk.time_into_round_s > 60 THEN TRUE
        ELSE FALSE
    END AS late_execute
FROM attack_rounds ar
LEFT JOIN first_kills fk
    ON fk.round_id = ar.round_id
    AND fk.kill_order = 1
LEFT JOIN plant_events pe
    ON pe.round_id = ar.round_id
    AND pe.planting_team = ar.team_name
ORDER BY ar.game_number, ar.round_number
