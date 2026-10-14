-- Model: agg_team_round_stats
-- Source: base_events
-- Type: Incremental (re-aggregate rounds with new events)

-- Step 1: Find rounds that have new events
-- Note: Uses r.ingested_at (when data was loaded) instead of e.occurred_at (when match happened)
-- This ensures backfilled historical matches are properly detected and processed
CREATE TEMP TABLE new_rounds AS
SELECT DISTINCT e.round_id
FROM base_events e
LEFT JOIN agg_team_round_stats trs ON trs.round_id = e.round_id
LEFT JOIN rounds r ON r.round_id = e.round_id
WHERE e.round_id IS NOT NULL
  AND (
      trs.round_id IS NULL
      OR r.ingested_at > COALESCE(
          (SELECT MAX(calculated_at) FROM agg_team_round_stats),
          '1900-01-01'::TIMESTAMP
      )
  );

-- Step 2: Delete existing stats for those rounds
DELETE FROM agg_team_round_stats
WHERE round_id IN (SELECT round_id FROM new_rounds);

-- Step 3: Re-aggregate ALL events for those rounds
WITH round_bounds AS (
    SELECT
        round_id,
        series_id,
        game_id,
        round_number,
        map_name,
        tournament_name,
        tournament_year,
        started_at,
        ended_at,
        end_reason,
        winning_team_name,
        losing_team_name,
        duration_seconds
    FROM rounds
    WHERE round_id IN (SELECT round_id FROM new_rounds)
),
teams_in_round AS (
    SELECT round_id, team_name
    FROM (
        SELECT round_id, winning_team_name AS team_name
        FROM rounds
        WHERE round_id IN (SELECT round_id FROM new_rounds)
          AND winning_team_name IS NOT NULL
        UNION
        SELECT round_id, losing_team_name AS team_name
        FROM rounds
        WHERE round_id IN (SELECT round_id FROM new_rounds)
          AND losing_team_name IS NOT NULL
        UNION
        SELECT r.round_id, g.team1_name AS team_name
        FROM rounds r
        JOIN games g ON r.game_id = g.game_id
        WHERE r.round_id IN (SELECT round_id FROM new_rounds)
          AND g.team1_name IS NOT NULL
        UNION
        SELECT r.round_id, g.team2_name AS team_name
        FROM rounds r
        JOIN games g ON r.game_id = g.game_id
        WHERE r.round_id IN (SELECT round_id FROM new_rounds)
          AND g.team2_name IS NOT NULL
        UNION
        SELECT round_id, actor_team_name AS team_name
        FROM base_events
        WHERE round_id IN (SELECT round_id FROM new_rounds)
          AND actor_team_name IS NOT NULL
        UNION
        SELECT round_id, target_team_name AS team_name
        FROM base_events
        WHERE round_id IN (SELECT round_id FROM new_rounds)
          AND target_team_name IS NOT NULL
    ) t
    GROUP BY round_id, team_name
),
opponents AS (
    SELECT
        t1.round_id,
        t1.team_name,
        MAX(CASE WHEN t2.team_name != t1.team_name THEN t2.team_name END) AS opponent_team_name
    FROM teams_in_round t1
    JOIN teams_in_round t2 ON t2.round_id = t1.round_id
    GROUP BY t1.round_id, t1.team_name
),
team_round_base AS (
    SELECT
        t.round_id,
        t.team_name,
        o.opponent_team_name,
        rb.series_id,
        rb.game_id,
        rb.round_number,
        rb.map_name,
        rb.tournament_name,
        rb.tournament_year,
        rb.started_at,
        rb.ended_at,
        rb.duration_seconds,
        rb.end_reason,
        rb.winning_team_name
    FROM teams_in_round t
    JOIN round_bounds rb ON rb.round_id = t.round_id
    LEFT JOIN opponents o ON o.round_id = t.round_id AND o.team_name = t.team_name
),
team_round_flags AS (
    SELECT
        *,
        (winning_team_name = team_name) AS win_flag,
        (winning_team_name != team_name) AS loss_flag,
        CASE WHEN round_number > 24 THEN TRUE ELSE FALSE END AS is_ot
    FROM team_round_base
),
team_round_scores AS (
    SELECT
        round_id,
        team_name,
        opponent_team_name,
        series_id,
        game_id,
        round_number,
        map_name,
        tournament_name,
        tournament_year,
        started_at,
        ended_at,
        duration_seconds,
        end_reason,
        winning_team_name,
        win_flag AS round_won,
        COALESCE(
            SUM(CASE WHEN win_flag THEN 1 ELSE 0 END)
            OVER (PARTITION BY game_id, team_name ORDER BY round_number ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
            0
        ) AS team_score_before,
        COALESCE(
            SUM(CASE WHEN winning_team_name = opponent_team_name THEN 1 ELSE 0 END)
            OVER (PARTITION BY game_id, team_name ORDER BY round_number ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
            0
        ) AS enemy_score_before,
        LAG(win_flag) OVER (PARTITION BY game_id, team_name ORDER BY round_number) AS prev_round_won,
        CASE
            WHEN win_flag THEN ROW_NUMBER() OVER (PARTITION BY game_id, team_name, win_group ORDER BY round_number)
            ELSE 0
        END AS current_win_streak,
        CASE
            WHEN loss_flag THEN ROW_NUMBER() OVER (PARTITION BY game_id, team_name, loss_group ORDER BY round_number)
            ELSE 0
        END AS current_loss_streak,
        CASE
            WHEN COALESCE(
                SUM(CASE WHEN win_flag THEN 1 ELSE 0 END)
                OVER (PARTITION BY game_id, team_name ORDER BY round_number ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
                0
            ) >= 12
             AND COALESCE(
                SUM(CASE WHEN winning_team_name = opponent_team_name THEN 1 ELSE 0 END)
                OVER (PARTITION BY game_id, team_name ORDER BY round_number ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
                0
            ) < COALESCE(
                SUM(CASE WHEN win_flag THEN 1 ELSE 0 END)
                OVER (PARTITION BY game_id, team_name ORDER BY round_number ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
                0
            )
        THEN TRUE ELSE FALSE
        END AS is_match_point,
        is_ot
    FROM (
        SELECT
            *,
            SUM(CASE WHEN win_flag THEN 0 ELSE 1 END) OVER (PARTITION BY game_id, team_name ORDER BY round_number) AS win_group,
            SUM(CASE WHEN loss_flag THEN 0 ELSE 1 END) OVER (PARTITION BY game_id, team_name ORDER BY round_number) AS loss_group
        FROM team_round_flags
    ) t
),
team_sides AS (
    SELECT
        round_id,
        actor_team_name AS team_name,
        MAX(actor_side) AS side
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND event_type = 'round-started-freezetime'
      AND actor_team_name IS NOT NULL
    GROUP BY round_id, actor_team_name
),
kill_events AS (
    SELECT
        round_id,
        occurred_at,
        actor_team_name AS killer_team,
        target_team_name AS victim_team,
        actor_player_id AS killer_id,
        target_player_id AS victim_id,
        is_first_blood
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND is_kill = TRUE
      AND actor_team_name IS NOT NULL
      AND target_team_name IS NOT NULL
),
first_kill AS (
    SELECT
        round_id,
        killer_team,
        victim_team,
        occurred_at AS first_kill_time
    FROM (
        SELECT
            round_id,
            killer_team,
            victim_team,
            occurred_at,
            ROW_NUMBER() OVER (PARTITION BY round_id ORDER BY occurred_at) AS rn
        FROM kill_events
    )
    WHERE rn = 1
),
contact_events AS (
    SELECT
        round_id,
        team_name,
        MIN(occurred_at) AS first_contact_time
    FROM (
        SELECT round_id, actor_team_name AS team_name, occurred_at
        FROM base_events
        WHERE round_id IN (SELECT round_id FROM new_rounds)
          AND (event_type = 'player-damaged-player' OR is_kill = TRUE)
          AND actor_team_name IS NOT NULL
        UNION ALL
        SELECT round_id, target_team_name AS team_name, occurred_at
        FROM base_events
        WHERE round_id IN (SELECT round_id FROM new_rounds)
          AND (event_type = 'player-damaged-player' OR is_kill = TRUE)
          AND target_team_name IS NOT NULL
    ) t
    GROUP BY round_id, team_name
),
first_kill_by_team AS (
    SELECT round_id, killer_team AS team_name, MIN(occurred_at) AS first_kill_time
    FROM kill_events
    GROUP BY round_id, killer_team
),
first_death_by_team AS (
    SELECT round_id, victim_team AS team_name, MIN(occurred_at) AS first_death_time
    FROM kill_events
    GROUP BY round_id, victim_team
),
trades AS (
    SELECT
        k.round_id,
        k.victim_team AS team_name,
        k.killer_id,
        k.occurred_at AS death_time,
        MIN(k2.occurred_at) AS trade_time
    FROM kill_events k
    LEFT JOIN kill_events k2
      ON k2.round_id = k.round_id
     AND k2.killer_team = k.victim_team
     AND k2.victim_id = k.killer_id
     AND k2.occurred_at BETWEEN k.occurred_at AND k.occurred_at + INTERVAL 5 SECOND
    GROUP BY k.round_id, k.victim_team, k.killer_id, k.occurred_at
),
trade_agg AS (
    SELECT
        round_id,
        team_name,
        COUNT(*) AS team_deaths,
        SUM(CASE WHEN trade_time IS NOT NULL THEN 1 ELSE 0 END) AS deaths_traded_total,
        SUM(CASE WHEN trade_time IS NULL THEN 1 ELSE 0 END) AS deaths_untraded_total,
        SUM(CASE WHEN trade_time IS NOT NULL THEN EXTRACT(EPOCH FROM (trade_time - death_time)) ELSE 0 END) AS avg_trade_delay_sum_s,
        SUM(CASE WHEN trade_time IS NOT NULL THEN 1 ELSE 0 END) AS avg_trade_delay_denom
    FROM trades
    GROUP BY round_id, team_name
),
first_blood_trade AS (
    SELECT
        fk.round_id,
        fk.victim_team AS team_name,
        MIN(t.trade_time) AS trade_time
    FROM first_kill fk
    LEFT JOIN trades t
      ON t.round_id = fk.round_id
     AND t.team_name = fk.victim_team
     AND t.death_time = fk.first_kill_time
    GROUP BY fk.round_id, fk.victim_team
),
plants AS (
    SELECT round_id, actor_team_name AS team_name, MIN(occurred_at) AS plant_time
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND is_plant = TRUE
      AND actor_team_name IS NOT NULL
    GROUP BY round_id, actor_team_name
),
defuses AS (
    SELECT round_id, actor_team_name AS team_name, COUNT(*) AS defuses
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND is_defuse = TRUE
      AND actor_team_name IS NOT NULL
    GROUP BY round_id, actor_team_name
),
defuse_events AS (
    SELECT
        round_id,
        actor_team_name AS team_name,
        actor_player_id,
        occurred_at,
        is_begin_defuse,
        is_stop_defuse,
        is_half_defuse,
        is_defuse_complete
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND actor_team_name IS NOT NULL
      AND (
          is_begin_defuse = TRUE
          OR is_stop_defuse = TRUE
          OR is_half_defuse = TRUE
          OR is_defuse_complete = TRUE
      )
),
defuse_agg AS (
    SELECT
        round_id,
        team_name,
        SUM(CASE WHEN is_begin_defuse THEN 1 ELSE 0 END) AS defuse_attempts_total,
        SUM(CASE WHEN is_defuse_complete THEN 1 ELSE 0 END) AS defuse_commit_success_total,
        SUM(CASE WHEN is_begin_defuse THEN 1 ELSE 0 END) AS defuse_commit_total,
        SUM(CASE WHEN is_half_defuse THEN 1 ELSE 0 END) AS half_defuse_taps_total
    FROM defuse_events
    GROUP BY round_id, team_name
),
ability_agg AS (
    SELECT
        round_id,
        actor_team_name AS team_name,
        SUM(CASE WHEN is_ability_use THEN 1 ELSE 0 END) AS util_used_total,
        SUM(CASE WHEN ability_type = 'flash' THEN 1 ELSE 0 END) AS flash_used_total,
        SUM(CASE WHEN ability_type = 'smoke' THEN 1 ELSE 0 END) AS smoke_used_total,
        SUM(CASE WHEN ability_type = 'molly' THEN 1 ELSE 0 END) AS molly_used_total,
        SUM(CASE WHEN ability_type = 'recon' THEN 1 ELSE 0 END) AS recon_used_total,
        SUM(CASE WHEN is_ability_use AND ability_type IS NULL THEN 1 ELSE 0 END) AS other_util_used_total
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND actor_team_name IS NOT NULL
    GROUP BY round_id, actor_team_name
),
economy_snapshots AS (
    SELECT
        round_id,
        actor_team_name AS team_name,
        MAX(team_loadout_value) AS loadout_value,
        MAX(team_net_worth) AS net_worth
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND event_type = 'round-started-freezetime'
      AND actor_team_name IS NOT NULL
    GROUP BY round_id, actor_team_name
)
INSERT INTO agg_team_round_stats (
    round_id,
    team_name,
    opponent_team_name,
    tournament_name,
    tournament_year,
    map_name,
    round_number,
    started_at,
    ended_at,
    round_won,
    side,
    end_reason,
    team_score_before,
    enemy_score_before,
    prev_round_won,
    current_win_streak,
    current_loss_streak,
    is_match_point,
    is_ot,
    team_kills,
    team_deaths,
    team_assists,
    first_bloods,
    first_deaths,
    plants,
    defuses,
    abilities_used,
    team_damage_dealt,
    team_damage_received,
    players_alive_at_end,
    entry_kill,
    entry_death,
    loadout_value,
    net_worth,
    round_duration_s,
    time_to_first_contact_s,
    time_to_first_kill_s,
    time_to_first_death_s,
    time_to_plant_s,
    post_plant_duration_s,
    fb_won_total,
    fb_converted_total,
    fb_attempts_total,
    fb_traded_total,
    fb_trade_delay_sum_s,
    fb_trade_delay_denom,
    deaths_traded_total,
    deaths_untraded_total,
    avg_trade_delay_sum_s,
    avg_trade_delay_denom,
    post_plant_kills_total,
    post_plant_deaths_total,
    retake_attempted_total,
    retake_kills_total,
    defuse_attempts_total,
    defuse_denied_total,
    defuse_denied_denom,
    defuse_commit_total,
    defuse_commit_success_total,
    defuse_commit_denom,
    half_defuse_taps_total,
    half_defuse_bait_success_total,
    half_defuse_bait_denom,
    time_to_defuse_attempt_sum_s,
    time_to_defuse_attempt_denom,
    util_used_total,
    flash_used_total,
    smoke_used_total,
    molly_used_total,
    recon_used_total,
    other_util_used_total
)
SELECT
    t.round_id,
    t.team_name,
    trs.opponent_team_name,
    trs.tournament_name,
    trs.tournament_year,
    trs.map_name,
    trs.round_number,
    trs.started_at,
    trs.ended_at,
    trs.round_won,
    ts.side,
    trs.end_reason,
    trs.team_score_before,
    trs.enemy_score_before,
    trs.prev_round_won,
    trs.current_win_streak,
    trs.current_loss_streak,
    trs.is_match_point,
    trs.is_ot,
    LEAST(SUM(CASE WHEN e.is_kill = TRUE AND e.actor_team_name = t.team_name THEN 1 ELSE 0 END), 5)::INTEGER AS team_kills,
    LEAST(SUM(CASE WHEN e.is_death = TRUE AND e.actor_team_name = t.team_name THEN 1 ELSE 0 END), 5)::INTEGER AS team_deaths,
    SUM(CASE WHEN e.is_assist = TRUE AND e.actor_team_name = t.team_name THEN 1 ELSE 0 END)::INTEGER AS team_assists,
    MAX(CASE WHEN e.is_first_blood = TRUE AND e.actor_team_name = t.team_name THEN 1 ELSE 0 END)::INTEGER AS first_bloods,
    MAX(
        CASE
            WHEN fk.first_kill_time IS NOT NULL
                 AND fk.victim_team = t.team_name
            THEN 1
            ELSE 0
        END
    )::INTEGER AS first_deaths,
    SUM(CASE WHEN e.is_plant = TRUE AND e.actor_team_name = t.team_name THEN 1 ELSE 0 END)::INTEGER AS plants,
    SUM(CASE WHEN e.is_defuse = TRUE AND e.actor_team_name = t.team_name THEN 1 ELSE 0 END)::INTEGER AS defuses,
    SUM(CASE WHEN e.is_ability_use = TRUE AND e.actor_team_name = t.team_name THEN 1 ELSE 0 END)::INTEGER AS abilities_used,
    SUM(CASE WHEN e.actor_team_name = t.team_name THEN COALESCE(e.damage_dealt, 0) ELSE 0 END)::FLOAT AS team_damage_dealt,
    SUM(CASE WHEN e.target_team_name = t.team_name THEN COALESCE(e.damage_dealt, 0) ELSE 0 END)::FLOAT AS team_damage_received,
    CASE
        WHEN ta.team_deaths IS NOT NULL THEN GREATEST(5 - ta.team_deaths, 0)
        ELSE NULL
    END AS players_alive_at_end,
    CASE WHEN fk.killer_team = t.team_name THEN TRUE ELSE FALSE END AS entry_kill,
    CASE WHEN fk.victim_team = t.team_name THEN TRUE ELSE FALSE END AS entry_death,
    es.loadout_value,
    es.net_worth,
    trs.duration_seconds::FLOAT AS round_duration_s,
    CASE
        WHEN ce.first_contact_time IS NOT NULL AND trs.started_at IS NOT NULL
             AND trs.duration_seconds IS NOT NULL
             AND EXTRACT(EPOCH FROM (ce.first_contact_time - trs.started_at)) <= trs.duration_seconds
        THEN EXTRACT(EPOCH FROM (ce.first_contact_time - trs.started_at))
        ELSE NULL
    END AS time_to_first_contact_s,
    CASE
        WHEN fkbt.first_kill_time IS NOT NULL AND trs.started_at IS NOT NULL
             AND trs.duration_seconds IS NOT NULL
             AND EXTRACT(EPOCH FROM (fkbt.first_kill_time - trs.started_at)) <= trs.duration_seconds
        THEN EXTRACT(EPOCH FROM (fkbt.first_kill_time - trs.started_at))
        ELSE NULL
    END AS time_to_first_kill_s,
    CASE
        WHEN fdbt.first_death_time IS NOT NULL AND trs.started_at IS NOT NULL
             AND trs.duration_seconds IS NOT NULL
             AND EXTRACT(EPOCH FROM (fdbt.first_death_time - trs.started_at)) <= trs.duration_seconds
        THEN EXTRACT(EPOCH FROM (fdbt.first_death_time - trs.started_at))
        ELSE NULL
    END AS time_to_first_death_s,
    CASE
        WHEN p.plant_time IS NOT NULL AND trs.started_at IS NOT NULL
             AND trs.duration_seconds IS NOT NULL
             AND EXTRACT(EPOCH FROM (p.plant_time - trs.started_at)) <= trs.duration_seconds
        THEN EXTRACT(EPOCH FROM (p.plant_time - trs.started_at))
        ELSE NULL
    END AS time_to_plant_s,
    CASE
        WHEN p.plant_time IS NOT NULL AND trs.ended_at IS NOT NULL
             AND trs.ended_at >= p.plant_time
             AND trs.duration_seconds IS NOT NULL
             AND EXTRACT(EPOCH FROM (trs.ended_at - p.plant_time)) <= trs.duration_seconds
        THEN EXTRACT(EPOCH FROM (trs.ended_at - p.plant_time))
        ELSE NULL
    END AS post_plant_duration_s,
    CASE WHEN fk.killer_team = t.team_name THEN 1 ELSE 0 END AS fb_won_total,
    CASE
        WHEN fk.killer_team = t.team_name
             AND trs.round_won = TRUE
        THEN 1 ELSE 0
    END AS fb_converted_total,
    CASE
        WHEN fk.killer_team = t.team_name OR fk.victim_team = t.team_name
        THEN 1 ELSE 0
    END AS fb_attempts_total,
    CASE WHEN fbt.trade_time IS NOT NULL THEN 1 ELSE 0 END AS fb_traded_total,
    CASE
        WHEN fbt.trade_time IS NOT NULL
        THEN EXTRACT(EPOCH FROM (fbt.trade_time - fk.first_kill_time))
        ELSE 0
    END AS fb_trade_delay_sum_s,
    CASE WHEN fbt.trade_time IS NOT NULL THEN 1 ELSE 0 END AS fb_trade_delay_denom,
    COALESCE(ta.deaths_traded_total, 0) AS deaths_traded_total,
    COALESCE(ta.deaths_untraded_total, 0) AS deaths_untraded_total,
    COALESCE(ta.avg_trade_delay_sum_s, 0) AS avg_trade_delay_sum_s,
    COALESCE(ta.avg_trade_delay_denom, 0) AS avg_trade_delay_denom,
    SUM(
        CASE
            WHEN p.plant_time IS NOT NULL
                 AND e.is_kill = TRUE
                 AND e.actor_team_name = t.team_name
                 AND e.occurred_at >= p.plant_time
            THEN 1 ELSE 0
        END
    )::INTEGER AS post_plant_kills_total,
    SUM(
        CASE
            WHEN p.plant_time IS NOT NULL
                 AND e.is_death = TRUE
                 AND e.actor_team_name = t.team_name
                 AND e.occurred_at >= p.plant_time
            THEN 1 ELSE 0
        END
    )::INTEGER AS post_plant_deaths_total,
    CASE
        WHEN opp_plant.plant_time IS NOT NULL
             AND SUM(
                CASE
                    WHEN e.is_kill = TRUE
                         AND e.actor_team_name = t.team_name
                         AND e.occurred_at >= opp_plant.plant_time
                    THEN 1 ELSE 0
                END
             ) > 0
        THEN 1 ELSE 0
    END AS retake_attempted_total,
    SUM(
        CASE
            WHEN opp_plant.plant_time IS NOT NULL
                 AND e.is_kill = TRUE
                 AND e.actor_team_name = t.team_name
                 AND e.occurred_at >= opp_plant.plant_time
            THEN 1 ELSE 0
        END
    )::INTEGER AS retake_kills_total,
    COALESCE(da.defuse_attempts_total, 0) AS defuse_attempts_total,
    GREATEST(COALESCE(da.defuse_attempts_total, 0) - COALESCE(da.defuse_commit_success_total, 0), 0) AS defuse_denied_total,
    COALESCE(da.defuse_attempts_total, 0) AS defuse_denied_denom,
    COALESCE(da.defuse_commit_total, 0) AS defuse_commit_total,
    COALESCE(da.defuse_commit_success_total, 0) AS defuse_commit_success_total,
    COALESCE(da.defuse_commit_total, 0) AS defuse_commit_denom,
    COALESCE(da.half_defuse_taps_total, 0) AS half_defuse_taps_total,
    0 AS half_defuse_bait_success_total,
    COALESCE(da.half_defuse_taps_total, 0) AS half_defuse_bait_denom,
    NULL AS time_to_defuse_attempt_sum_s,
    0 AS time_to_defuse_attempt_denom,
    COALESCE(aa.util_used_total, 0) AS util_used_total,
    COALESCE(aa.flash_used_total, 0) AS flash_used_total,
    COALESCE(aa.smoke_used_total, 0) AS smoke_used_total,
    COALESCE(aa.molly_used_total, 0) AS molly_used_total,
    COALESCE(aa.recon_used_total, 0) AS recon_used_total,
    COALESCE(aa.other_util_used_total, 0) AS other_util_used_total
FROM teams_in_round t
JOIN team_round_scores trs ON trs.round_id = t.round_id AND trs.team_name = t.team_name
LEFT JOIN team_sides ts ON ts.round_id = t.round_id AND ts.team_name = t.team_name
LEFT JOIN base_events e ON e.round_id = t.round_id
LEFT JOIN trade_agg ta ON ta.round_id = t.round_id AND ta.team_name = t.team_name
LEFT JOIN first_kill fk ON fk.round_id = t.round_id
LEFT JOIN first_kill_by_team fkbt ON fkbt.round_id = t.round_id AND fkbt.team_name = t.team_name
LEFT JOIN first_death_by_team fdbt ON fdbt.round_id = t.round_id AND fdbt.team_name = t.team_name
LEFT JOIN first_blood_trade fbt ON fbt.round_id = t.round_id AND fbt.team_name = t.team_name
LEFT JOIN contact_events ce ON ce.round_id = t.round_id AND ce.team_name = t.team_name
LEFT JOIN plants p ON p.round_id = t.round_id AND p.team_name = t.team_name
LEFT JOIN plants opp_plant ON opp_plant.round_id = t.round_id AND opp_plant.team_name != t.team_name
LEFT JOIN defuse_agg da ON da.round_id = t.round_id AND da.team_name = t.team_name
LEFT JOIN ability_agg aa ON aa.round_id = t.round_id AND aa.team_name = t.team_name
LEFT JOIN economy_snapshots es ON es.round_id = t.round_id AND es.team_name = t.team_name
GROUP BY
    t.round_id,
    t.team_name,
    trs.opponent_team_name,
    trs.tournament_name,
    trs.tournament_year,
    trs.map_name,
    trs.round_number,
    trs.started_at,
    trs.ended_at,
    trs.round_won,
    trs.end_reason,
    trs.duration_seconds,
    trs.team_score_before,
    trs.enemy_score_before,
    trs.prev_round_won,
    trs.current_win_streak,
    trs.current_loss_streak,
    trs.is_match_point,
    trs.is_ot,
    ts.side,
    fk.killer_team,
    fk.victim_team,
    fk.first_kill_time,
    fkbt.first_kill_time,
    fdbt.first_death_time,
    fbt.trade_time,
    ce.first_contact_time,
    p.plant_time,
    opp_plant.plant_time,
    da.defuse_attempts_total,
    da.defuse_commit_success_total,
    da.defuse_commit_total,
    da.half_defuse_taps_total,
    aa.util_used_total,
    aa.flash_used_total,
    aa.smoke_used_total,
    aa.molly_used_total,
    aa.recon_used_total,
    aa.other_util_used_total,
    ta.team_deaths,
    ta.deaths_traded_total,
    ta.deaths_untraded_total,
    ta.avg_trade_delay_sum_s,
    ta.avg_trade_delay_denom,
    es.loadout_value,
    es.net_worth;

-- Report results
SELECT
    'Team round aggregation completed' AS status,
    (SELECT COUNT(*) FROM new_rounds) AS rounds_affected,
    (SELECT COUNT(*) FROM agg_team_round_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS rows_inserted;

-- Safety clamp for any negative post-plant durations
UPDATE agg_team_round_stats
SET post_plant_duration_s = NULL
WHERE post_plant_duration_s < 0;

-- Cleanup
DROP TABLE new_rounds;
