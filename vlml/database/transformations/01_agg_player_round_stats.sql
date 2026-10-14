-- ============================================================================
-- Model: agg_player_round_stats
-- Source: base_events, rounds, agent_roles, weapon_types
-- Type: Incremental (delete-and-rebuild affected rounds)
-- Target: agg_player_round_stats (40+ metrics per player per round)
-- ============================================================================
--
-- PURPOSE:
--   This is the foundational transformation that converts raw base_events into
--   per-player per-round statistics. All downstream aggregations (game, series,
--   daily, tournament) depend on this table.
--
-- PROCESSING PATTERN:
--   1. Find rounds with new data (using ingested_at, not occurred_at)
--   2. Delete existing stats for those rounds
--   3. Rebuild all stats for affected rounds from base_events
--
-- KEY METRICS COMPUTED:
--   - Combat: kills, deaths, assists, damage_dealt, damage_received
--   - Opening duels: first_bloods, first_deaths, is_opening_kill/death
--   - Trading: is_traded, is_trade_kill, trade_kill_time, is_untraded_death
--   - Clutches: is_1v1 through is_1v5, clutch_won/lost, clutch_opponents
--   - Multi-kills: is_double_kill, is_triple_kill, is_quad_kill, is_ace
--   - Economy: loadout_value, is_eco_round, is_force_buy, is_thrifty
--   - Weapons: rifle_kills, sniper_kills, headshot metrics by weapon type
--   - Duels: duel_initiated_wins, duel_held_wins, by weapon category
--   - Positioning: iso_deaths, stack_deaths, repeek_deaths
--   - Utility: flash_assists, util_effect_kills, early_util
--
-- INCREMENTAL LOGIC:
--   Uses r.ingested_at (when data was loaded into DB) instead of e.occurred_at
--   (when match happened). This ensures backfilled historical matches are
--   properly detected. For example, 2023 matches loaded after 2025 matches
--   will be processed because their ingested_at is recent.
--
-- CTE STRUCTURE:
--   1. new_rounds - Identify rounds needing (re)processing
--   2. round_team_deaths - Count deaths per team per round
--   3. first_kill - Find opening kill of each round
--   4. player_sides - Determine attack/defense side per player
--   5. damage_received_agg - Sum damage taken per player
--   6. player_economy - Loadout values and net worth
--   7. weapon_events - Classify weapons used
--   8. team_weapon_counts - Determine eco/force/full buy
--   9. early_util_agg - Track utility used in first 15 seconds
--   10. kill_events - Extract all kills with positions
--   11. duel_* - Analyze 1v1 engagements and outcomes
--   12. trade_* - Identify trades (revenge kills within 4-5 seconds)
--   13. *_deaths - Categorize death types (iso, stack, repeek)
--   14. flash_* - Track flash assists and self-flash kills
--   15. util_effect_kills - Kills assisted by any utility
--
-- ============================================================================

-- Step 1: Find rounds that have new events
-- Note: Uses r.ingested_at (when data was loaded) instead of e.occurred_at (when match happened)
-- This ensures backfilled historical matches are properly detected and processed
CREATE TEMP TABLE new_rounds AS
SELECT DISTINCT e.round_id
FROM base_events e
LEFT JOIN agg_player_round_stats prs ON prs.round_id = e.round_id
LEFT JOIN rounds r ON r.round_id = e.round_id
WHERE e.round_id IS NOT NULL
  AND (
      prs.round_id IS NULL
      OR r.ingested_at > COALESCE(
          (SELECT MAX(calculated_at) FROM agg_player_round_stats),
          '1900-01-01'::TIMESTAMP
      )
  );

-- Step 2: Delete existing stats for those rounds
DELETE FROM agg_player_round_stats
WHERE round_id IN (SELECT round_id FROM new_rounds);

-- Step 3: Re-aggregate ALL events for those rounds
WITH
round_team_deaths AS (
    SELECT
        e.round_id,
        e.actor_team_name AS team_name,
        SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END)::INTEGER AS team_deaths
    FROM base_events e
    WHERE e.round_id IN (SELECT round_id FROM new_rounds)
      AND e.actor_team_name IS NOT NULL
    GROUP BY e.round_id, e.actor_team_name
),
first_kill AS (
    SELECT
        round_id,
        actor_player_id AS killer_id,
        target_player_id AS victim_id,
        actor_team_name AS killer_team,
        target_team_name AS victim_team,
        occurred_at AS first_kill_time
    FROM (
        SELECT
            round_id,
            actor_player_id,
            target_player_id,
            actor_team_name,
            target_team_name,
            occurred_at,
            ROW_NUMBER() OVER (PARTITION BY round_id ORDER BY occurred_at) AS rn
        FROM base_events
        WHERE round_id IN (SELECT round_id FROM new_rounds)
          AND is_kill = TRUE
          AND actor_player_id IS NOT NULL
          AND target_player_id IS NOT NULL
    )
    WHERE rn = 1
),
round_total_deaths AS (
    SELECT
        e.round_id,
        SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END)::INTEGER AS total_deaths
    FROM base_events e
    WHERE e.round_id IN (SELECT round_id FROM new_rounds)
    GROUP BY e.round_id
),
round_team_counts AS (
    SELECT
        rtd.round_id,
        rtd.team_name,
        rtd.team_deaths,
        GREATEST(COALESCE(rtt.total_deaths, 0) - rtd.team_deaths, 0) AS opponent_deaths
    FROM round_team_deaths rtd
    LEFT JOIN round_total_deaths rtt ON rtt.round_id = rtd.round_id
),
player_sides AS (
    SELECT
        round_id,
        actor_player_id AS player_id,
        MAX(actor_side) AS side
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND actor_player_id IS NOT NULL
      AND actor_side IS NOT NULL
    GROUP BY round_id, actor_player_id
),
damage_received_agg AS (
    SELECT
        round_id,
        target_player_id AS player_id,
        SUM(COALESCE(damage_dealt, 0))::FLOAT AS damage_received
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND target_player_id IS NOT NULL
      AND damage_dealt IS NOT NULL
    GROUP BY round_id, target_player_id
),
player_economy AS (
    SELECT
        round_id,
        actor_player_id AS player_id,
        MAX(actor_loadout_value) AS loadout_value,
        MAX(actor_net_worth) AS net_worth
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND actor_player_id IS NOT NULL
      AND (actor_loadout_value IS NOT NULL OR actor_net_worth IS NOT NULL)
    GROUP BY round_id, actor_player_id
),
player_teams AS (
    SELECT
        round_id,
        actor_player_id AS player_id,
        MAX(actor_team_name) AS team_name
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND actor_player_id IS NOT NULL
      AND actor_team_name IS NOT NULL
    GROUP BY round_id, actor_player_id
),
weapon_events AS (
    SELECT
        e.round_id,
        e.actor_player_id AS player_id,
        e.occurred_at,
        LOWER(COALESCE(e.weapon_type, wt.weapon_type)) AS weapon_type
    FROM base_events e
    LEFT JOIN weapon_types wt ON LOWER(e.weapon_name) = LOWER(wt.weapon_name)
    WHERE e.round_id IN (SELECT round_id FROM new_rounds)
      AND e.actor_player_id IS NOT NULL
      AND (e.is_kill = TRUE OR e.event_type = 'player-damaged-player')
      AND (e.weapon_type IS NOT NULL OR wt.weapon_type IS NOT NULL)
),
player_first_weapon AS (
    SELECT
        round_id,
        player_id,
        weapon_type
    FROM (
        SELECT
            round_id,
            player_id,
            weapon_type,
            ROW_NUMBER() OVER (
                PARTITION BY round_id, player_id
                ORDER BY occurred_at
            ) AS rn
        FROM weapon_events
    )
    WHERE rn = 1
),
team_weapon_counts AS (
    SELECT
        pt.round_id,
        pt.team_name,
        SUM(CASE WHEN pfw.weapon_type IN ('rifle', 'sniper', 'heavy') THEN 1 ELSE 0 END) AS primary_count,
        SUM(CASE WHEN pfw.weapon_type IN ('smg', 'shotgun') THEN 1 ELSE 0 END) AS light_count,
        SUM(CASE WHEN pfw.weapon_type = 'pistol' THEN 1 ELSE 0 END) AS pistol_count
    FROM player_teams pt
    LEFT JOIN player_first_weapon pfw
      ON pfw.round_id = pt.round_id
     AND pfw.player_id = pt.player_id
    GROUP BY pt.round_id, pt.team_name
),
opponent_weapon_counts AS (
    SELECT
        twc.round_id,
        twc.team_name,
        MAX(twc2.primary_count) AS opponent_primary_count
    FROM team_weapon_counts twc
    LEFT JOIN team_weapon_counts twc2
      ON twc2.round_id = twc.round_id
     AND twc2.team_name <> twc.team_name
    GROUP BY twc.round_id, twc.team_name
),
early_util_agg AS (
    SELECT
        e.round_id,
        e.actor_player_id AS player_id,
        CASE WHEN COUNT(*) > 0 THEN 1 ELSE 0 END AS early_util_flag
    FROM base_events e
    JOIN rounds r ON r.round_id = e.round_id
    WHERE e.round_id IN (SELECT round_id FROM new_rounds)
      AND e.is_ability_use = TRUE
      AND e.actor_player_id IS NOT NULL
      AND r.started_at IS NOT NULL
      AND EXTRACT(EPOCH FROM (e.occurred_at - r.started_at)) BETWEEN 0 AND 15
    GROUP BY e.round_id, e.actor_player_id
),
kill_events AS (
    SELECT
        e.round_id,
        e.occurred_at,
        e.actor_player_id AS killer_id,
        e.actor_team_name AS killer_team,
        e.target_player_id AS victim_id,
        e.target_team_name AS victim_team,
        e.actor_pos_x AS killer_x,
        e.actor_pos_y AS killer_y,
        e.target_pos_x AS victim_x,
        e.target_pos_y AS victim_y,
        e.map_name,
        LOWER(COALESCE(e.weapon_type, wt.weapon_type)) AS killer_weapon_type
    FROM base_events e
    LEFT JOIN weapon_types wt ON LOWER(e.weapon_name) = LOWER(wt.weapon_name)
    WHERE e.round_id IN (SELECT round_id FROM new_rounds)
      AND e.is_kill = TRUE
      AND e.actor_player_id IS NOT NULL
),
damage_events AS (
    SELECT
        e.round_id,
        e.occurred_at,
        e.actor_player_id AS attacker_id,
        e.target_player_id AS victim_id,
        LOWER(COALESCE(e.weapon_type, wt.weapon_type)) AS damage_weapon_type
    FROM base_events e
    LEFT JOIN weapon_types wt ON LOWER(e.weapon_name) = LOWER(wt.weapon_name)
    WHERE e.round_id IN (SELECT round_id FROM new_rounds)
      AND e.event_type = 'player-damaged-player'
      AND e.actor_player_id IS NOT NULL
      AND e.target_player_id IS NOT NULL
),
duel_damage AS (
    SELECT
        k.round_id,
        k.killer_id,
        k.victim_id,
        k.occurred_at AS kill_time,
        k.killer_weapon_type,
        d.attacker_id,
        d.victim_id AS damage_victim_id,
        d.occurred_at AS damage_time,
        CASE WHEN d.attacker_id = k.killer_id THEN 1 ELSE 0 END AS damage_by_killer
    FROM kill_events k
    JOIN damage_events d
      ON d.round_id = k.round_id
     AND ((d.attacker_id = k.killer_id AND d.victim_id = k.victim_id)
          OR (d.attacker_id = k.victim_id AND d.victim_id = k.killer_id))
     AND d.occurred_at <= k.occurred_at
     AND d.occurred_at >= k.occurred_at - INTERVAL '3 seconds'
),
duels AS (
    SELECT
        round_id,
        killer_id,
        victim_id,
        kill_time,
        killer_weapon_type,
        damage_time AS first_damage_time,
        damage_by_killer AS first_damage_by_killer
    FROM (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY round_id, killer_id, victim_id, kill_time
                ORDER BY damage_time
            ) AS rn
        FROM duel_damage
    ) dd
    WHERE rn = 1
),
duel_participants AS (
    SELECT
        round_id,
        killer_id AS player_id,
        1 AS is_killer,
        CASE WHEN first_damage_by_killer = 1 THEN 1 ELSE 0 END AS initiated_flag,
        killer_weapon_type,
        kill_time,
        first_damage_time
    FROM duels
    UNION ALL
    SELECT
        round_id,
        victim_id AS player_id,
        0 AS is_killer,
        CASE WHEN first_damage_by_killer = 0 THEN 1 ELSE 0 END AS initiated_flag,
        killer_weapon_type,
        kill_time,
        first_damage_time
    FROM duels
),
duel_agg AS (
    SELECT
        round_id,
        player_id,
        SUM(CASE WHEN initiated_flag = 1 THEN 1 ELSE 0 END)::INTEGER AS duel_initiated_total,
        SUM(CASE WHEN initiated_flag = 1 AND is_killer = 1 THEN 1 ELSE 0 END)::INTEGER AS duel_initiated_wins_total,
        SUM(CASE WHEN initiated_flag = 1 THEN 1 ELSE 0 END)::INTEGER AS duel_initiated_denom,
        SUM(CASE WHEN initiated_flag = 0 AND is_killer = 1 THEN 1 ELSE 0 END)::INTEGER AS duel_held_wins_total,
        SUM(CASE WHEN initiated_flag = 0 THEN 1 ELSE 0 END)::INTEGER AS duel_held_denom,
        SUM(EXTRACT(EPOCH FROM (kill_time - first_damage_time)))::FLOAT AS duel_resolution_time_sum_s,
        COUNT(*)::INTEGER AS duel_resolution_time_denom,
        SUM(CASE WHEN is_killer = 1 AND killer_weapon_type = 'rifle' THEN 1 ELSE 0 END)::INTEGER AS duel_wins_rifle_total,
        SUM(CASE WHEN is_killer = 0 AND killer_weapon_type = 'rifle' THEN 1 ELSE 0 END)::INTEGER AS duel_losses_rifle_total,
        SUM(CASE WHEN killer_weapon_type = 'rifle' THEN 1 ELSE 0 END)::INTEGER AS duel_rifle_denom,
        SUM(CASE WHEN is_killer = 1 AND killer_weapon_type = 'smg' THEN 1 ELSE 0 END)::INTEGER AS duel_wins_smg_total,
        SUM(CASE WHEN is_killer = 0 AND killer_weapon_type = 'smg' THEN 1 ELSE 0 END)::INTEGER AS duel_losses_smg_total,
        SUM(CASE WHEN killer_weapon_type = 'smg' THEN 1 ELSE 0 END)::INTEGER AS duel_smg_denom,
        SUM(CASE WHEN is_killer = 1 AND killer_weapon_type = 'pistol' THEN 1 ELSE 0 END)::INTEGER AS duel_wins_pistol_total,
        SUM(CASE WHEN is_killer = 0 AND killer_weapon_type = 'pistol' THEN 1 ELSE 0 END)::INTEGER AS duel_losses_pistol_total,
        SUM(CASE WHEN killer_weapon_type = 'pistol' THEN 1 ELSE 0 END)::INTEGER AS duel_pistol_denom,
        SUM(CASE WHEN is_killer = 1 AND killer_weapon_type = 'sniper' THEN 1 ELSE 0 END)::INTEGER AS duel_wins_sniper_total,
        SUM(CASE WHEN is_killer = 0 AND killer_weapon_type = 'sniper' THEN 1 ELSE 0 END)::INTEGER AS duel_losses_sniper_total,
        SUM(CASE WHEN killer_weapon_type = 'sniper' THEN 1 ELSE 0 END)::INTEGER AS duel_sniper_denom,
        SUM(CASE WHEN is_killer = 1 AND killer_weapon_type = 'shotgun' THEN 1 ELSE 0 END)::INTEGER AS duel_wins_shotgun_total,
        SUM(CASE WHEN is_killer = 0 AND killer_weapon_type = 'shotgun' THEN 1 ELSE 0 END)::INTEGER AS duel_losses_shotgun_total,
        SUM(CASE WHEN killer_weapon_type = 'shotgun' THEN 1 ELSE 0 END)::INTEGER AS duel_shotgun_denom
    FROM duel_participants
    GROUP BY round_id, player_id
),
kill_distances AS (
    SELECT
        round_id,
        killer_id AS player_id,
        SUM(
            CASE
                WHEN killer_x IS NOT NULL AND killer_y IS NOT NULL
                     AND victim_x IS NOT NULL AND victim_y IS NOT NULL
                THEN SQRT(POWER(killer_x - victim_x, 2) + POWER(killer_y - victim_y, 2))
                ELSE 0
            END
        ) AS kill_distance_sum,
        SUM(
            CASE
                WHEN killer_x IS NOT NULL AND killer_y IS NOT NULL
                     AND victim_x IS NOT NULL AND victim_y IS NOT NULL
                THEN 1 ELSE 0
            END
        ) AS kill_distance_denom
    FROM kill_events
    GROUP BY round_id, killer_id
),
trade_kills AS (
    SELECT
        k.round_id,
        k.killer_id AS player_id,
        k.killer_x,
        k.killer_y,
        k.victim_x,
        k.victim_y,
        CASE WHEN k_prev.killer_id IS NOT NULL THEN 1 ELSE 0 END AS is_trade
    FROM kill_events k
    LEFT JOIN kill_events k_prev
      ON k_prev.round_id = k.round_id
     AND k_prev.killer_id = k.victim_id
     AND k_prev.victim_team = k.killer_team
     AND ABS(EXTRACT(EPOCH FROM (k_prev.occurred_at - k.occurred_at))) <= 5.0
),
trade_distances AS (
    SELECT
        round_id,
        player_id,
        SUM(
            CASE
                WHEN is_trade = 1
                     AND killer_x IS NOT NULL AND killer_y IS NOT NULL
                     AND victim_x IS NOT NULL AND victim_y IS NOT NULL
                THEN SQRT(POWER(killer_x - victim_x, 2) + POWER(killer_y - victim_y, 2))
                ELSE 0
            END
        ) AS trade_kill_distance_sum,
        SUM(
            CASE
                WHEN is_trade = 1
                     AND killer_x IS NOT NULL AND killer_y IS NOT NULL
                     AND victim_x IS NOT NULL AND victim_y IS NOT NULL
                THEN 1 ELSE 0
            END
        ) AS trade_kill_distance_denom
    FROM trade_kills
    GROUP BY round_id, player_id
),
death_events AS (
    SELECT
        round_id,
        occurred_at,
        actor_player_id AS player_id,
        actor_team_name AS team_name,
        target_player_id AS killer_id,
        actor_pos_x AS death_x,
        actor_pos_y AS death_y
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND is_death = TRUE
      AND actor_player_id IS NOT NULL
),
traded_deaths AS (
    SELECT
        d.round_id,
        d.player_id,
        MIN(EXTRACT(EPOCH FROM (k.occurred_at - d.occurred_at))) AS trade_time
    FROM death_events d
    JOIN kill_events k
      ON k.round_id = d.round_id
     AND k.victim_id = d.killer_id
     AND k.killer_team = d.team_name
     AND k.killer_id <> d.player_id
     AND k.occurred_at >= d.occurred_at
     AND EXTRACT(EPOCH FROM (k.occurred_at - d.occurred_at)) <= 4.0
    GROUP BY d.round_id, d.player_id
),
trade_kill_events AS (
    SELECT
        k.round_id,
        k.killer_id AS player_id,
        MIN(EXTRACT(EPOCH FROM (k.occurred_at - d.occurred_at))) AS trade_kill_time
    FROM kill_events k
    JOIN death_events d
      ON d.round_id = k.round_id
     AND d.killer_id = k.victim_id
     AND d.team_name = k.killer_team
     AND k.occurred_at >= d.occurred_at
     AND EXTRACT(EPOCH FROM (k.occurred_at - d.occurred_at)) <= 4.0
    GROUP BY k.round_id, k.killer_id
),
player_death_time AS (
    SELECT
        round_id,
        actor_player_id AS player_id,
        MIN(occurred_at) AS death_time
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND is_death = TRUE
      AND actor_player_id IS NOT NULL
    GROUP BY round_id, actor_player_id
),
repeek_deaths AS (
    SELECT
        d.round_id,
        d.player_id,
        SUM(
            CASE
                WHEN ABS(EXTRACT(EPOCH FROM (k.occurred_at - d.occurred_at))) <= 3.0
                     AND k.killer_id = d.player_id
                     AND k.killer_x IS NOT NULL AND k.killer_y IS NOT NULL
                     AND d.death_x IS NOT NULL AND d.death_y IS NOT NULL
                     AND SQRT(POWER(k.killer_x - d.death_x, 2) + POWER(k.killer_y - d.death_y, 2)) <= 5.0
                THEN 1 ELSE 0
            END
        ) AS repeek_deaths_total
    FROM death_events d
    LEFT JOIN kill_events k ON k.round_id = d.round_id AND k.killer_id = d.player_id
    GROUP BY d.round_id, d.player_id
),
stack_deaths AS (
    SELECT
        d.round_id,
        d.player_id,
        CASE
            WHEN EXISTS (
                SELECT 1
                FROM death_events d2
                WHERE d2.round_id = d.round_id
                  AND d2.team_name = d.team_name
                  AND d2.player_id <> d.player_id
                  AND ABS(EXTRACT(EPOCH FROM (d2.occurred_at - d.occurred_at))) <= 2.0
                  AND d2.death_x IS NOT NULL AND d2.death_y IS NOT NULL
                  AND d.death_x IS NOT NULL AND d.death_y IS NOT NULL
                  AND SQRT(POWER(d2.death_x - d.death_x, 2) + POWER(d2.death_y - d.death_y, 2)) <= 5.0
            )
            THEN 1 ELSE 0
        END AS stack_death_flag
    FROM death_events d
),
stack_deaths_agg AS (
    SELECT
        round_id,
        player_id,
        SUM(stack_death_flag) AS stack_deaths_total
    FROM stack_deaths
    GROUP BY round_id, player_id
),
iso_deaths AS (
    SELECT
        d.round_id,
        d.player_id,
        CASE
            WHEN NOT EXISTS (
                SELECT 1
                FROM death_events d2
                WHERE d2.round_id = d.round_id
                  AND d2.team_name = d.team_name
                  AND d2.player_id <> d.player_id
                  AND ABS(EXTRACT(EPOCH FROM (d2.occurred_at - d.occurred_at))) <= 5.0
                  AND d2.death_x IS NOT NULL AND d2.death_y IS NOT NULL
                  AND d.death_x IS NOT NULL AND d.death_y IS NOT NULL
                  AND SQRT(POWER(d2.death_x - d.death_x, 2) + POWER(d2.death_y - d.death_y, 2)) <= 15.0
            )
            AND NOT EXISTS (
                SELECT 1
                FROM kill_events k
                WHERE k.round_id = d.round_id
                  AND k.killer_team = d.team_name
                  AND k.victim_id = d.killer_id
                  AND EXTRACT(EPOCH FROM (k.occurred_at - d.occurred_at)) BETWEEN 0 AND 5.0
            )
            THEN 1 ELSE 0
        END AS iso_death_flag
    FROM death_events d
),
iso_deaths_agg AS (
    SELECT
        round_id,
        player_id,
        SUM(iso_death_flag) AS iso_deaths_total
    FROM iso_deaths
    GROUP BY round_id, player_id
),
flash_events AS (
    SELECT
        round_id,
        occurred_at,
        actor_player_id AS player_id,
        actor_team_name AS team_name,
        actor_pos_x AS flash_x,
        actor_pos_y AS flash_y
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND event_type = 'player-used-ability'
      AND ability_type = 'flash'
      AND actor_player_id IS NOT NULL
),
flash_assist_kills AS (
    SELECT
        f.round_id,
        f.player_id,
        COUNT(*) AS flash_assist_kills_total
    FROM flash_events f
    JOIN kill_events k
      ON k.killer_team = f.team_name
     AND EXTRACT(EPOCH FROM (k.occurred_at - f.occurred_at)) BETWEEN 0 AND 3.0
     AND k.killer_x IS NOT NULL AND k.killer_y IS NOT NULL
     AND f.flash_x IS NOT NULL AND f.flash_y IS NOT NULL
     AND SQRT(POWER(k.killer_x - f.flash_x, 2) + POWER(k.killer_y - f.flash_y, 2)) <= 10.0
    GROUP BY f.round_id, f.player_id
),
self_flash_kills AS (
    SELECT
        f.round_id,
        f.player_id,
        COUNT(*) AS self_flash_kills_total
    FROM flash_events f
    JOIN kill_events k
      ON k.killer_id = f.player_id
     AND EXTRACT(EPOCH FROM (k.occurred_at - f.occurred_at)) BETWEEN 0 AND 3.0
     AND k.killer_x IS NOT NULL AND k.killer_y IS NOT NULL
     AND f.flash_x IS NOT NULL AND f.flash_y IS NOT NULL
     AND SQRT(POWER(k.killer_x - f.flash_x, 2) + POWER(k.killer_y - f.flash_y, 2)) <= 10.0
    GROUP BY f.round_id, f.player_id
),
ability_events AS (
    SELECT
        round_id,
        occurred_at,
        actor_player_id AS player_id,
        actor_team_name AS team_name,
        actor_pos_x AS util_x,
        actor_pos_y AS util_y
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND is_ability_use = TRUE
      AND actor_player_id IS NOT NULL
),
util_effect_kills AS (
    SELECT
        a.round_id,
        a.player_id,
        COUNT(*) AS util_effect_kills_total
    FROM ability_events a
    JOIN kill_events k
      ON k.killer_team = a.team_name
     AND EXTRACT(EPOCH FROM (k.occurred_at - a.occurred_at)) BETWEEN 0 AND 3.0
     AND k.killer_x IS NOT NULL AND k.killer_y IS NOT NULL
     AND a.util_x IS NOT NULL AND a.util_y IS NOT NULL
     AND SQRT(POWER(k.killer_x - a.util_x, 2) + POWER(k.killer_y - a.util_y, 2)) <= 10.0
    GROUP BY a.round_id, a.player_id
),
player_first_kill AS (
    SELECT
        round_id,
        killer_id AS player_id,
        MIN(occurred_at) AS first_kill_time
    FROM kill_events
    GROUP BY round_id, killer_id
)
INSERT INTO agg_player_round_stats (
    round_id,
    player_id,
    player_name,
    team_name,
    opponent_team_name,
    tournament_name,
    tournament_year,
    map_name,
    agent_name,
    round_number,
    started_at,
    ended_at,
    round_won,
    side,
    kills,
    deaths,
    assists,
    first_bloods,
    first_deaths,
    plants,
    defuses,
    abilities_used,
    damage_dealt,
    damage_received,
    survived,
    time_first_blood,
    time_first_death,
    time_alive,
    time_to_first_kill,
    agent_role,
    is_duelist,
    is_initiator,
    is_controller,
    is_sentinel,
    kast,
    damage_per_kill,
    overkill_damage,
    is_entry_fragger,
    is_opening_kill,
    is_opening_death,
    is_entry_denied,
    is_traded,
    is_trade_kill,
    trade_kill_time,
    is_untraded_death,
    multi_kill_count,
    is_double_kill,
    is_triple_kill,
    is_quad_kill,
    is_ace,
    is_clutch,
    is_1v1,
    is_1v2,
    is_1v3,
    is_1v4,
    is_1v5,
    clutch_won,
    clutch_lost,
    clutch_opponents,
    loadout_value,
    net_worth,
    is_eco_round,
    is_force_buy,
    is_full_buy,
    is_thrifty,
    flash_assists,
    early_util,
    weapon_name,
    weapon_type,
    total_headshot_kills,
    headshot_kills_denom,
    headshot_hits_total,
    hits_total,
    bodyshot_kills,
    rifle_kills,
    smg_kills,
    pistol_kills,
    sniper_kills,
    shotgun_kills,
    util_used_total,
    flash_used_total,
    util_effect_kills_total,
    util_effect_kills_denom,
    flash_assist_kills_total,
    flash_assist_kills_denom,
    self_flash_kills_total,
    self_flash_kills_denom,
    kill_distance_sum,
    kill_distance_denom,
    trade_kill_distance_sum,
    trade_kill_distance_denom,
    duel_initiated_total,
    duel_initiated_wins_total,
    duel_initiated_denom,
    duel_held_wins_total,
    duel_held_denom,
    duel_resolution_time_sum_s,
    duel_resolution_time_denom,
    duel_wins_rifle_total,
    duel_losses_rifle_total,
    duel_rifle_denom,
    duel_wins_smg_total,
    duel_losses_smg_total,
    duel_smg_denom,
    duel_wins_pistol_total,
    duel_losses_pistol_total,
    duel_pistol_denom,
    duel_wins_sniper_total,
    duel_losses_sniper_total,
    duel_sniper_denom,
    duel_wins_shotgun_total,
    duel_losses_shotgun_total,
    duel_shotgun_denom,
    repeek_deaths_total,
    repeek_deaths_denom,
    iso_deaths_total,
    iso_deaths_denom,
    stack_deaths_total,
    stack_deaths_denom,
    survival_time_sum_s,
    survival_time_denom,
    calculated_at
)
SELECT
    e.round_id,
    e.actor_player_id AS player_id,
    e.actor_player_name AS player_name,

    -- Denormalized dimensions
    MAX(e.actor_team_name) AS team_name,
    MAX(CASE WHEN e.target_team_name != e.actor_team_name THEN e.target_team_name END) AS opponent_team_name,
    MAX(e.tournament_name) AS tournament_name,
    MAX(e.tournament_year) AS tournament_year,
    MAX(e.map_name) AS map_name,
    MAX(e.actor_agent_name) AS agent_name,
    MAX(r.round_number) AS round_number,

    -- Round context
    MAX(r.started_at) AS started_at,
    MAX(r.ended_at) AS ended_at,
    MAX(CASE WHEN r.winning_team_name = e.actor_team_name THEN TRUE ELSE FALSE END) AS round_won,
    MAX(ps.side) AS side,

    -- Basic combat stats
    SUM(CASE WHEN e.is_kill = TRUE THEN 1 ELSE 0 END)::INTEGER AS kills,
    LEAST(SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END), 1)::INTEGER AS deaths,
    SUM(CASE WHEN e.is_assist = TRUE THEN 1 ELSE 0 END)::INTEGER AS assists,
    MAX(CASE WHEN e.is_first_blood = TRUE THEN 1 ELSE 0 END)::INTEGER AS first_bloods,
    MAX(CASE WHEN fk.victim_id = e.actor_player_id THEN 1 ELSE 0 END)::INTEGER AS first_deaths,
    SUM(CASE WHEN e.is_plant = TRUE THEN 1 ELSE 0 END)::INTEGER AS plants,
    SUM(CASE WHEN e.is_defuse = TRUE THEN 1 ELSE 0 END)::INTEGER AS defuses,
    SUM(CASE WHEN e.is_ability_use = TRUE THEN 1 ELSE 0 END)::INTEGER AS abilities_used,
    SUM(COALESCE(e.damage_dealt, 0))::FLOAT AS damage_dealt,
    MAX(COALESCE(dr.damage_received, 0))::FLOAT AS damage_received,
    CASE WHEN MAX(pdt.death_time) IS NULL THEN TRUE ELSE FALSE END AS survived,

    -- Timing metrics
    MAX(
        CASE
            WHEN fk.killer_id = e.actor_player_id THEN fk.first_kill_time
            ELSE NULL
        END
    ) AS time_first_blood,
    MAX(
        CASE
            WHEN fk.victim_id = e.actor_player_id THEN fk.first_kill_time
            ELSE NULL
        END
    ) AS time_first_death,
    CASE
        WHEN MAX(r.started_at) IS NULL OR MAX(r.ended_at) IS NULL THEN NULL
        WHEN MAX(pdt.death_time) IS NOT NULL
        THEN GREATEST(0, EXTRACT(EPOCH FROM (MAX(pdt.death_time) - MAX(r.started_at))))
        ELSE GREATEST(0, EXTRACT(EPOCH FROM (MAX(r.ended_at) - MAX(r.started_at))))
    END AS time_alive,
    CASE
        WHEN MIN(pfk.first_kill_time) IS NOT NULL
             AND MIN(r.started_at) IS NOT NULL
             AND MIN(r.ended_at) IS NOT NULL
             AND MIN(pfk.first_kill_time) <= MIN(r.ended_at)
        THEN GREATEST(0, EXTRACT(EPOCH FROM (MIN(pfk.first_kill_time) - MIN(r.started_at))))
        ELSE NULL
    END AS time_to_first_kill,

    -- Agent role (from lookup table)
    MAX(ar.agent_role) AS agent_role,
    MAX(ar.is_duelist)::BOOLEAN AS is_duelist,
    MAX(ar.is_initiator)::BOOLEAN AS is_initiator,
    MAX(ar.is_controller)::BOOLEAN AS is_controller,
    MAX(ar.is_sentinel)::BOOLEAN AS is_sentinel,

    -- Combat efficiency
    (SUM(CASE WHEN e.is_kill = TRUE THEN 1 ELSE 0 END) > 0
     OR SUM(CASE WHEN e.is_assist = TRUE THEN 1 ELSE 0 END) > 0
     OR SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END) = 0
     OR MAX(td2.trade_time) IS NOT NULL) AS kast,
    CASE
        WHEN SUM(CASE WHEN e.is_kill = TRUE THEN 1 ELSE 0 END) > 0
        THEN SUM(COALESCE(e.damage_dealt, 0))::FLOAT / SUM(CASE WHEN e.is_kill = TRUE THEN 1 ELSE 0 END)
        ELSE NULL
    END AS damage_per_kill,
    SUM(
        CASE
            WHEN e.is_kill = TRUE AND COALESCE(e.damage_dealt, 0) > 150
            THEN COALESCE(e.damage_dealt, 0) - 150
            ELSE 0
        END
    )::FLOAT AS overkill_damage,

    -- Performance flags (simplified for now)
    CASE
        WHEN MAX(fk.killer_id) = e.actor_player_id
             AND LOWER(COALESCE(MAX(ps.side), '')) IN ('attack', 'atk')
        THEN TRUE ELSE FALSE
    END AS is_entry_fragger,
    (SUM(CASE WHEN e.is_first_blood = TRUE THEN 1 ELSE 0 END) > 0) AS is_opening_kill,
    MAX(CASE WHEN fk.victim_id = e.actor_player_id THEN TRUE ELSE FALSE END) AS is_opening_death,
    CASE
        WHEN MAX(fk.victim_id) = e.actor_player_id
             AND LOWER(COALESCE(MAX(ps.side), '')) IN ('attack', 'atk')
        THEN TRUE ELSE FALSE
    END AS is_entry_denied,
    CASE WHEN MAX(td2.trade_time) IS NOT NULL THEN TRUE ELSE FALSE END AS is_traded,
    CASE WHEN MAX(tke.trade_kill_time) IS NOT NULL THEN TRUE ELSE FALSE END AS is_trade_kill,
    MAX(tke.trade_kill_time) AS trade_kill_time,
    CASE
        WHEN LEAST(SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END), 1) > 0
             AND MAX(td2.trade_time) IS NULL
        THEN TRUE ELSE FALSE
    END AS is_untraded_death,
    LEAST(SUM(CASE WHEN e.is_kill = TRUE THEN 1 ELSE 0 END), 5)::INTEGER AS multi_kill_count,

    -- Multi-kills
    (SUM(CASE WHEN e.is_kill = TRUE THEN 1 ELSE 0 END) >= 2) AS is_double_kill,
    (SUM(CASE WHEN e.is_kill = TRUE THEN 1 ELSE 0 END) >= 3) AS is_triple_kill,
    (SUM(CASE WHEN e.is_kill = TRUE THEN 1 ELSE 0 END) >= 4) AS is_quad_kill,
    (SUM(CASE WHEN e.is_kill = TRUE THEN 1 ELSE 0 END) >= 5) AS is_ace,

    -- Clutch situations (approximate 1vX at round end)
    CASE
        WHEN SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END) = 0
             AND MAX(rtc.team_deaths) = 4
        THEN TRUE ELSE FALSE
    END AS is_clutch,
    CASE
        WHEN SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END) = 0
             AND MAX(rtc.team_deaths) = 4
             AND (5 - MAX(rtc.opponent_deaths)) = 1
        THEN TRUE ELSE FALSE
    END AS is_1v1,
    CASE
        WHEN SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END) = 0
             AND MAX(rtc.team_deaths) = 4
             AND (5 - MAX(rtc.opponent_deaths)) = 2
        THEN TRUE ELSE FALSE
    END AS is_1v2,
    CASE
        WHEN SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END) = 0
             AND MAX(rtc.team_deaths) = 4
             AND (5 - MAX(rtc.opponent_deaths)) = 3
        THEN TRUE ELSE FALSE
    END AS is_1v3,
    CASE
        WHEN SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END) = 0
             AND MAX(rtc.team_deaths) = 4
             AND (5 - MAX(rtc.opponent_deaths)) = 4
        THEN TRUE ELSE FALSE
    END AS is_1v4,
    CASE
        WHEN SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END) = 0
             AND MAX(rtc.team_deaths) = 4
             AND (5 - MAX(rtc.opponent_deaths)) = 5
        THEN TRUE ELSE FALSE
    END AS is_1v5,
    CASE
        WHEN SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END) = 0
             AND MAX(rtc.team_deaths) = 4
        THEN MAX(CASE WHEN r.winning_team_name = e.actor_team_name THEN TRUE ELSE FALSE END)
        ELSE NULL
    END AS clutch_won,
    CASE
        WHEN SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END) = 0
             AND MAX(rtc.team_deaths) = 4
        THEN NOT MAX(CASE WHEN r.winning_team_name = e.actor_team_name THEN TRUE ELSE FALSE END)
        ELSE NULL
    END AS clutch_lost,
    CASE
        WHEN SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END) = 0
             AND MAX(rtc.team_deaths) = 4
        THEN (5 - MAX(rtc.opponent_deaths))
        ELSE NULL
    END AS clutch_opponents,

    -- Economy (simplified)
    MAX(pe.loadout_value) AS loadout_value,
    MAX(pe.net_worth) AS net_worth,
    CASE
        WHEN MAX(COALESCE(twc.primary_count, 0)) = 0
        THEN TRUE ELSE FALSE
    END AS is_eco_round,
    CASE
        WHEN MAX(COALESCE(twc.primary_count, 0)) BETWEEN 1 AND 2
        THEN TRUE ELSE FALSE
    END AS is_force_buy,
    CASE
        WHEN MAX(COALESCE(twc.primary_count, 0)) >= 3
        THEN TRUE ELSE FALSE
    END AS is_full_buy,
    CASE
        WHEN MAX(COALESCE(twc.primary_count, 0)) <= 2
             AND MAX(COALESCE(owc.opponent_primary_count, 0)) >= 3
             AND MAX(CASE WHEN r.winning_team_name = e.actor_team_name THEN TRUE ELSE FALSE END)
        THEN TRUE ELSE FALSE
    END AS is_thrifty,

    -- Ability usage
    MAX(COALESCE(fa.flash_assist_kills_total, 0)) AS flash_assists,
    CASE
        WHEN MAX(COALESCE(eu.early_util_flag, 0)) = 1 THEN TRUE
        ELSE FALSE
    END AS early_util,

    -- Weapon stats
    MAX(e.weapon_name) AS weapon_name,
    MAX(COALESCE(e.weapon_type, wt.weapon_type)) AS weapon_type,
    SUM(
        CASE
            WHEN e.is_kill = TRUE
                 AND (e.is_headshot = TRUE OR e.hit_location = 'head')
            THEN 1
            ELSE 0
        END
    )::INTEGER AS total_headshot_kills,
    SUM(CASE WHEN e.is_kill = TRUE THEN 1 ELSE 0 END)::INTEGER AS headshot_kills_denom,
    SUM(
        CASE
            WHEN e.event_type = 'player-damaged-player'
                 AND e.hit_location = 'head'
            THEN 1
            ELSE 0
        END
    )::INTEGER AS headshot_hits_total,
    SUM(
        CASE
            WHEN e.event_type = 'player-damaged-player'
                 AND e.hit_location IN ('head', 'body', 'leg')
            THEN 1
            ELSE 0
        END
    )::INTEGER AS hits_total,
    SUM(
        CASE
            WHEN e.is_kill = TRUE
                 AND (e.hit_location IN ('body', 'leg')
                      OR (e.is_headshot = FALSE AND e.hit_location IS NOT NULL))
            THEN 1
            ELSE 0
        END
    )::INTEGER AS bodyshot_kills,
    SUM(CASE WHEN e.is_kill = TRUE AND COALESCE(e.weapon_type, wt.weapon_type) = 'rifle' THEN 1 ELSE 0 END)::INTEGER AS rifle_kills,
    SUM(CASE WHEN e.is_kill = TRUE AND COALESCE(e.weapon_type, wt.weapon_type) = 'smg' THEN 1 ELSE 0 END)::INTEGER AS smg_kills,
    SUM(CASE WHEN e.is_kill = TRUE AND COALESCE(e.weapon_type, wt.weapon_type) = 'pistol' THEN 1 ELSE 0 END)::INTEGER AS pistol_kills,
    SUM(CASE WHEN e.is_kill = TRUE AND COALESCE(e.weapon_type, wt.weapon_type) = 'sniper' THEN 1 ELSE 0 END)::INTEGER AS sniper_kills,
    SUM(CASE WHEN e.is_kill = TRUE AND COALESCE(e.weapon_type, wt.weapon_type) = 'shotgun' THEN 1 ELSE 0 END)::INTEGER AS shotgun_kills,

    -- Utility effectiveness (totals/denoms)
    SUM(CASE WHEN e.is_ability_use = TRUE THEN 1 ELSE 0 END)::INTEGER AS util_used_total,
    SUM(CASE WHEN e.ability_type = 'flash' THEN 1 ELSE 0 END)::INTEGER AS flash_used_total,
    MAX(COALESCE(ue.util_effect_kills_total, 0)) AS util_effect_kills_total,
    SUM(CASE WHEN e.is_ability_use = TRUE THEN 1 ELSE 0 END)::INTEGER AS util_effect_kills_denom,
    MAX(COALESCE(fa.flash_assist_kills_total, 0)) AS flash_assist_kills_total,
    SUM(CASE WHEN e.ability_type = 'flash' THEN 1 ELSE 0 END)::INTEGER AS flash_assist_kills_denom,
    MAX(COALESCE(sf.self_flash_kills_total, 0)) AS self_flash_kills_total,
    SUM(CASE WHEN e.ability_type = 'flash' THEN 1 ELSE 0 END)::INTEGER AS self_flash_kills_denom,
    MAX(COALESCE(kd.kill_distance_sum, 0)) AS kill_distance_sum,
    MAX(COALESCE(kd.kill_distance_denom, 0)) AS kill_distance_denom,
    MAX(COALESCE(td.trade_kill_distance_sum, 0)) AS trade_kill_distance_sum,
    MAX(COALESCE(td.trade_kill_distance_denom, 0)) AS trade_kill_distance_denom,

    -- Duel mechanics (totals/denoms)
    MAX(COALESCE(duel.duel_initiated_total, 0)) AS duel_initiated_total,
    MAX(COALESCE(duel.duel_initiated_wins_total, 0)) AS duel_initiated_wins_total,
    MAX(COALESCE(duel.duel_initiated_denom, 0)) AS duel_initiated_denom,
    MAX(COALESCE(duel.duel_held_wins_total, 0)) AS duel_held_wins_total,
    MAX(COALESCE(duel.duel_held_denom, 0)) AS duel_held_denom,
    MAX(COALESCE(duel.duel_resolution_time_sum_s, 0)) AS duel_resolution_time_sum_s,
    MAX(COALESCE(duel.duel_resolution_time_denom, 0)) AS duel_resolution_time_denom,
    MAX(COALESCE(duel.duel_wins_rifle_total, 0)) AS duel_wins_rifle_total,
    MAX(COALESCE(duel.duel_losses_rifle_total, 0)) AS duel_losses_rifle_total,
    MAX(COALESCE(duel.duel_rifle_denom, 0)) AS duel_rifle_denom,
    MAX(COALESCE(duel.duel_wins_smg_total, 0)) AS duel_wins_smg_total,
    MAX(COALESCE(duel.duel_losses_smg_total, 0)) AS duel_losses_smg_total,
    MAX(COALESCE(duel.duel_smg_denom, 0)) AS duel_smg_denom,
    MAX(COALESCE(duel.duel_wins_pistol_total, 0)) AS duel_wins_pistol_total,
    MAX(COALESCE(duel.duel_losses_pistol_total, 0)) AS duel_losses_pistol_total,
    MAX(COALESCE(duel.duel_pistol_denom, 0)) AS duel_pistol_denom,
    MAX(COALESCE(duel.duel_wins_sniper_total, 0)) AS duel_wins_sniper_total,
    MAX(COALESCE(duel.duel_losses_sniper_total, 0)) AS duel_losses_sniper_total,
    MAX(COALESCE(duel.duel_sniper_denom, 0)) AS duel_sniper_denom,
    MAX(COALESCE(duel.duel_wins_shotgun_total, 0)) AS duel_wins_shotgun_total,
    MAX(COALESCE(duel.duel_losses_shotgun_total, 0)) AS duel_losses_shotgun_total,
    MAX(COALESCE(duel.duel_shotgun_denom, 0)) AS duel_shotgun_denom,
    LEAST(
        MAX(COALESCE(rd.repeek_deaths_total, 0)),
        LEAST(SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END), 1)
    ) AS repeek_deaths_total,
    LEAST(SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END), 1)::INTEGER AS repeek_deaths_denom,

    -- Positioning & spacing (totals/denoms)
    MAX(COALESCE(iso.iso_deaths_total, 0)) AS iso_deaths_total,
    SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END)::INTEGER AS iso_deaths_denom,
    MAX(COALESCE(sd.stack_deaths_total, 0)) AS stack_deaths_total,
    SUM(CASE WHEN e.is_death = TRUE THEN 1 ELSE 0 END)::INTEGER AS stack_deaths_denom,

    -- Survival
    CASE
        WHEN MAX(r.started_at) IS NULL OR MAX(r.ended_at) IS NULL THEN NULL
        WHEN MAX(pdt.death_time) IS NOT NULL
        THEN GREATEST(0, EXTRACT(EPOCH FROM (MAX(pdt.death_time) - MAX(r.started_at))))
        ELSE GREATEST(0, EXTRACT(EPOCH FROM (MAX(r.ended_at) - MAX(r.started_at))))
    END AS survival_time_sum_s,
    CASE
        WHEN MAX(r.started_at) IS NULL OR MAX(r.ended_at) IS NULL THEN 0
        ELSE 1
    END AS survival_time_denom,

    -- Metadata
    CURRENT_TIMESTAMP AS calculated_at

FROM base_events e
LEFT JOIN rounds r ON e.round_id = r.round_id
LEFT JOIN agent_roles ar ON LOWER(e.actor_agent_name) = LOWER(ar.agent_name)
LEFT JOIN weapon_types wt ON LOWER(e.weapon_name) = LOWER(wt.weapon_name)
LEFT JOIN round_team_counts rtc
  ON rtc.round_id = e.round_id
 AND rtc.team_name = e.actor_team_name
LEFT JOIN damage_received_agg dr
  ON dr.round_id = e.round_id
 AND dr.player_id = e.actor_player_id
LEFT JOIN player_sides ps
  ON ps.round_id = e.round_id
 AND ps.player_id = e.actor_player_id
LEFT JOIN player_death_time pdt
  ON pdt.round_id = e.round_id
 AND pdt.player_id = e.actor_player_id
LEFT JOIN player_first_kill pfk
  ON pfk.round_id = e.round_id
 AND pfk.player_id = e.actor_player_id
LEFT JOIN player_economy pe
  ON pe.round_id = e.round_id
 AND pe.player_id = e.actor_player_id
LEFT JOIN team_weapon_counts twc
  ON twc.round_id = e.round_id
 AND twc.team_name = e.actor_team_name
LEFT JOIN opponent_weapon_counts owc
  ON owc.round_id = e.round_id
 AND owc.team_name = e.actor_team_name
LEFT JOIN traded_deaths td2
  ON td2.round_id = e.round_id
 AND td2.player_id = e.actor_player_id
LEFT JOIN trade_kill_events tke
  ON tke.round_id = e.round_id
 AND tke.player_id = e.actor_player_id
LEFT JOIN first_kill fk ON fk.round_id = e.round_id
LEFT JOIN kill_distances kd
  ON kd.round_id = e.round_id
 AND kd.player_id = e.actor_player_id
LEFT JOIN trade_distances td
  ON td.round_id = e.round_id
 AND td.player_id = e.actor_player_id
LEFT JOIN repeek_deaths rd
  ON rd.round_id = e.round_id
 AND rd.player_id = e.actor_player_id
LEFT JOIN stack_deaths_agg sd
  ON sd.round_id = e.round_id
 AND sd.player_id = e.actor_player_id
LEFT JOIN iso_deaths_agg iso
  ON iso.round_id = e.round_id
 AND iso.player_id = e.actor_player_id
LEFT JOIN flash_assist_kills fa
  ON fa.round_id = e.round_id
 AND fa.player_id = e.actor_player_id
LEFT JOIN self_flash_kills sf
  ON sf.round_id = e.round_id
 AND sf.player_id = e.actor_player_id
LEFT JOIN util_effect_kills ue
  ON ue.round_id = e.round_id
 AND ue.player_id = e.actor_player_id
LEFT JOIN early_util_agg eu
  ON eu.round_id = e.round_id
 AND eu.player_id = e.actor_player_id
LEFT JOIN duel_agg duel
  ON duel.round_id = e.round_id
 AND duel.player_id = e.actor_player_id
WHERE e.round_id IN (SELECT round_id FROM new_rounds)
  AND e.actor_player_name IS NOT NULL
GROUP BY
    e.round_id,
    e.actor_player_id,
    e.actor_player_name;

-- Report results
SELECT
    'Incremental load completed' AS status,
    (SELECT COUNT(*) FROM new_rounds) AS rounds_affected,
    (SELECT COUNT(*) FROM agg_player_round_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS rows_inserted,
    (SELECT MIN(started_at) FROM agg_player_round_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS min_date,
    (SELECT MAX(started_at) FROM agg_player_round_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS max_date;

-- Cleanup
DROP TABLE new_rounds;
