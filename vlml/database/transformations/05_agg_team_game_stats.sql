-- Model: agg_team_game_stats
-- Source: agg_team_round_stats, agg_player_round_stats, games, rounds, series
-- Type: Incremental (re-aggregate games with new round stats)

-- Step 1: Find games that have new or updated team round stats
CREATE TEMP TABLE new_games AS
SELECT DISTINCT r.game_id
FROM agg_team_round_stats trs
JOIN rounds r ON trs.round_id = r.round_id
WHERE trs.calculated_at > COALESCE(
    (SELECT MAX(calculated_at) FROM agg_team_game_stats),
    '1900-01-01'::TIMESTAMP
);

-- Step 2: Delete existing stats for those games
DELETE FROM agg_team_game_stats
WHERE game_id IN (SELECT game_id FROM new_games);

-- Step 3: Re-aggregate team round stats into game stats
WITH team_agents AS (
    SELECT
        r.game_id,
        prs.team_name,
        LIST(DISTINCT prs.agent_name) FILTER (WHERE prs.agent_name IS NOT NULL) AS agents_played,
        COUNT(DISTINCT CASE WHEN prs.is_duelist THEN prs.player_id END) AS num_duelists,
        COUNT(DISTINCT CASE WHEN prs.is_initiator THEN prs.player_id END) AS num_initiators,
        COUNT(DISTINCT CASE WHEN prs.is_controller THEN prs.player_id END) AS num_controllers,
        COUNT(DISTINCT CASE WHEN prs.is_sentinel THEN prs.player_id END) AS num_sentinels
    FROM agg_player_round_stats prs
    JOIN rounds r ON prs.round_id = r.round_id
    WHERE r.game_id IN (SELECT game_id FROM new_games)
    GROUP BY r.game_id, prs.team_name
),
multi_kills AS (
    SELECT
        r.game_id,
        prs.team_name,
        SUM(CASE WHEN prs.is_ace THEN 1 ELSE 0 END)::INTEGER AS aces_count,
        SUM(CASE WHEN prs.is_quad_kill THEN 1 ELSE 0 END)::INTEGER AS quad_kills_count,
        SUM(CASE WHEN prs.is_triple_kill THEN 1 ELSE 0 END)::INTEGER AS triple_kills_count
    FROM agg_player_round_stats prs
    JOIN rounds r ON prs.round_id = r.round_id
    WHERE r.game_id IN (SELECT game_id FROM new_games)
    GROUP BY r.game_id, prs.team_name
),
clutch_stats AS (
    SELECT
        r.game_id,
        prs.team_name,
        SUM(CASE WHEN prs.is_clutch THEN 1 ELSE 0 END)::INTEGER AS clutches_attempted,
        SUM(CASE WHEN prs.clutch_won THEN 1 ELSE 0 END)::INTEGER AS clutches_won
    FROM agg_player_round_stats prs
    JOIN rounds r ON prs.round_id = r.round_id
    WHERE r.game_id IN (SELECT game_id FROM new_games)
    GROUP BY r.game_id, prs.team_name
)
INSERT INTO agg_team_game_stats
SELECT
    r.game_id,
    trs.team_name,

    -- Denormalized dimensions
    MAX(g.series_id) AS series_id,
    MAX(trs.opponent_team_name) AS opponent_team_name,
    MAX(trs.tournament_name) AS tournament_name,
    MAX(trs.tournament_year) AS tournament_year,
    MAX(trs.map_name) AS map_name,

    -- Game context
    MIN(trs.started_at) AS game_started_at,
    MAX(trs.ended_at) AS game_ended_at,
    CASE
        WHEN MAX(g.winning_team_name) IS NOT NULL THEN MAX(g.winning_team_name) = trs.team_name
        ELSE (SUM(CASE WHEN trs.round_won THEN 1 ELSE 0 END) > COUNT(*) / 2.0)
    END AS game_won,
    SUM(CASE WHEN trs.round_won THEN 1 ELSE 0 END)::INTEGER AS rounds_won,
    SUM(CASE WHEN trs.round_won THEN 0 ELSE 1 END)::INTEGER AS rounds_lost,

    -- Side performance
    SUM(CASE WHEN LOWER(trs.side) IN ('attack', 'attacker', 'atk') AND trs.round_won THEN 1 ELSE 0 END)::INTEGER AS attack_rounds_won,
    SUM(CASE WHEN LOWER(trs.side) IN ('attack', 'attacker', 'atk') THEN 1 ELSE 0 END)::INTEGER AS attack_rounds_played,
    SUM(CASE WHEN LOWER(trs.side) IN ('defense', 'defender', 'def') AND trs.round_won THEN 1 ELSE 0 END)::INTEGER AS defense_rounds_won,
    SUM(CASE WHEN LOWER(trs.side) IN ('defense', 'defender', 'def') THEN 1 ELSE 0 END)::INTEGER AS defense_rounds_played,

    -- Aggregate metrics (sum across all rounds)
    SUM(trs.team_kills)::INTEGER AS team_kills,
    SUM(trs.team_deaths)::INTEGER AS team_deaths,
    SUM(trs.team_assists)::INTEGER AS team_assists,
    SUM(trs.first_bloods)::INTEGER AS first_bloods,
    SUM(trs.first_deaths)::INTEGER AS first_deaths,
    SUM(trs.plants)::INTEGER AS plants,
    SUM(trs.defuses)::INTEGER AS defuses,
    SUM(trs.abilities_used)::INTEGER AS abilities_used,
    SUM(trs.team_damage_dealt)::FLOAT AS team_damage_dealt,
    SUM(trs.team_damage_received)::FLOAT AS team_damage_received,

    -- Derived metrics
    CASE WHEN SUM(trs.team_deaths) > 0 THEN SUM(trs.team_kills)::FLOAT / SUM(trs.team_deaths) ELSE NULL END AS kd_ratio,
    CASE WHEN COUNT(*) > 0 THEN SUM(trs.team_damage_dealt)::FLOAT / COUNT(*) ELSE 0 END AS adr,
    CASE WHEN COUNT(*) > 0 THEN SUM(trs.team_kills)::FLOAT / COUNT(*) ELSE 0 END AS kpr,
    CASE WHEN SUM(CASE WHEN LOWER(trs.side) IN ('attack', 'attacker', 'atk') THEN 1 ELSE 0 END) > 0
        THEN SUM(CASE WHEN LOWER(trs.side) IN ('attack', 'attacker', 'atk') AND trs.round_won THEN 1 ELSE 0 END)::FLOAT /
             SUM(CASE WHEN LOWER(trs.side) IN ('attack', 'attacker', 'atk') THEN 1 ELSE 0 END)
        ELSE NULL
    END AS attack_win_rate,
    CASE WHEN SUM(CASE WHEN LOWER(trs.side) IN ('defense', 'defender', 'def') THEN 1 ELSE 0 END) > 0
        THEN SUM(CASE WHEN LOWER(trs.side) IN ('defense', 'defender', 'def') AND trs.round_won THEN 1 ELSE 0 END)::FLOAT /
             SUM(CASE WHEN LOWER(trs.side) IN ('defense', 'defender', 'def') THEN 1 ELSE 0 END)
        ELSE NULL
    END AS defense_win_rate,
    CASE WHEN COUNT(*) > 0 THEN SUM(trs.first_bloods)::FLOAT / COUNT(*) ELSE 0 END AS fk_percentage,
    CASE WHEN COUNT(*) > 0 THEN SUM(trs.first_deaths)::FLOAT / COUNT(*) ELSE 0 END AS fd_percentage,

    -- Agent composition (JSON array)
    ta.agents_played AS agents_played,

    -- Team composition metrics
    COALESCE(ta.num_duelists, 0) AS num_duelists,
    COALESCE(ta.num_initiators, 0) AS num_initiators,
    COALESCE(ta.num_controllers, 0) AS num_controllers,
    COALESCE(ta.num_sentinels, 0) AS num_sentinels,
    (COALESCE(ta.num_duelists, 0) >= 2) AS is_double_duelist,
    (COALESCE(ta.num_duelists, 0) = 0) AS is_no_duelist,
    CONCAT(
        COALESCE(ta.num_duelists, 0), 'D-',
        COALESCE(ta.num_initiators, 0), 'I-',
        COALESCE(ta.num_controllers, 0), 'C-',
        COALESCE(ta.num_sentinels, 0), 'S'
    ) AS agent_comp_string,

    -- Opening duels
    SUM(CASE WHEN trs.entry_kill THEN 1 ELSE 0 END)::INTEGER AS entry_duels_won,
    SUM(CASE WHEN trs.entry_death THEN 1 ELSE 0 END)::INTEGER AS entry_duels_lost,
    CASE
        WHEN (SUM(CASE WHEN trs.entry_kill THEN 1 ELSE 0 END) + SUM(CASE WHEN trs.entry_death THEN 1 ELSE 0 END)) > 0
        THEN SUM(CASE WHEN trs.entry_kill THEN 1 ELSE 0 END)::FLOAT /
             (SUM(CASE WHEN trs.entry_kill THEN 1 ELSE 0 END) + SUM(CASE WHEN trs.entry_death THEN 1 ELSE 0 END))
        ELSE NULL
    END AS opening_duel_win_rate,
    CASE
        WHEN SUM(CASE WHEN trs.entry_kill THEN 1 ELSE 0 END) > 0
        THEN SUM(CASE WHEN trs.entry_kill AND trs.round_won THEN 1 ELSE 0 END)::FLOAT /
             SUM(CASE WHEN trs.entry_kill THEN 1 ELSE 0 END)
        ELSE NULL
    END AS fk_conversion_rate,
    CASE
        WHEN SUM(CASE WHEN trs.entry_death THEN 1 ELSE 0 END) > 0
        THEN SUM(CASE WHEN trs.entry_death AND NOT trs.round_won THEN 1 ELSE 0 END)::FLOAT /
             SUM(CASE WHEN trs.entry_death THEN 1 ELSE 0 END)
        ELSE NULL
    END AS fd_loss_rate,

    -- Trading
    CASE
        WHEN (SUM(trs.deaths_traded_total) + SUM(trs.deaths_untraded_total)) > 0
        THEN SUM(trs.deaths_traded_total)::FLOAT /
             (SUM(trs.deaths_traded_total) + SUM(trs.deaths_untraded_total))
        ELSE NULL
    END AS team_trade_success_rate,
    SUM(trs.deaths_untraded_total)::INTEGER AS team_untraded_deaths,

    -- Special rounds
    SUM(CASE WHEN trs.round_number IN (1, 13, 25) AND trs.round_won THEN 1 ELSE 0 END)::INTEGER AS pistol_rounds_won,
    SUM(CASE WHEN trs.round_number IN (1, 13, 25) THEN 1 ELSE 0 END)::INTEGER AS pistol_rounds_played,
    CASE
        WHEN SUM(CASE WHEN trs.round_number IN (1, 13, 25) THEN 1 ELSE 0 END) > 0
        THEN SUM(CASE WHEN trs.round_number IN (1, 13, 25) AND trs.round_won THEN 1 ELSE 0 END)::FLOAT /
             SUM(CASE WHEN trs.round_number IN (1, 13, 25) THEN 1 ELSE 0 END)
        ELSE NULL
    END AS pistol_win_rate,
    0 AS bonus_rounds_won,
    0 AS anti_eco_rounds_won,

    -- Multi-kills
    COALESCE(mk.aces_count, 0) AS aces_count,
    COALESCE(mk.quad_kills_count, 0) AS quad_kills_count,
    COALESCE(mk.triple_kills_count, 0) AS triple_kills_count,

    -- Clutch performance
    COALESCE(cs.clutches_attempted, 0) AS clutches_attempted,
    COALESCE(cs.clutches_won, 0) AS clutches_won,
    CASE
        WHEN COALESCE(cs.clutches_attempted, 0) > 0
        THEN COALESCE(cs.clutches_won, 0)::FLOAT / cs.clutches_attempted
        ELSE NULL
    END AS clutch_win_rate,

    -- Situational
    0 AS rounds_5v4,
    0 AS wins_5v4,
    NULL AS conversion_5v4,
    0 AS rounds_4v5,
    0 AS wins_4v5,
    NULL AS comeback_4v5,
    SUM(CASE WHEN trs.plants > 0 THEN 1 ELSE 0 END)::INTEGER AS post_plant_rounds,
    SUM(CASE WHEN trs.plants > 0 AND trs.round_won THEN 1 ELSE 0 END)::INTEGER AS post_plant_wins,
    CASE
        WHEN SUM(CASE WHEN trs.plants > 0 THEN 1 ELSE 0 END) > 0
        THEN SUM(CASE WHEN trs.plants > 0 AND trs.round_won THEN 1 ELSE 0 END)::FLOAT /
             SUM(CASE WHEN trs.plants > 0 THEN 1 ELSE 0 END)
        ELSE NULL
    END AS post_plant_win_rate,

    -- Momentum
    MAX(trs.current_win_streak)::INTEGER AS longest_win_streak,
    MAX(trs.current_loss_streak)::INTEGER AS longest_loss_streak,
    0 AS rounds_after_timeout,
    0 AS wins_after_timeout,

    -- Metadata
    CURRENT_TIMESTAMP AS calculated_at

FROM agg_team_round_stats trs
JOIN rounds r ON trs.round_id = r.round_id
LEFT JOIN games g ON r.game_id = g.game_id
LEFT JOIN team_agents ta ON ta.game_id = r.game_id AND ta.team_name = trs.team_name
LEFT JOIN multi_kills mk ON mk.game_id = r.game_id AND mk.team_name = trs.team_name
LEFT JOIN clutch_stats cs ON cs.game_id = r.game_id AND cs.team_name = trs.team_name
WHERE r.game_id IN (SELECT game_id FROM new_games)
GROUP BY
    r.game_id,
    trs.team_name,
    ta.agents_played,
    ta.num_duelists,
    ta.num_initiators,
    ta.num_controllers,
    ta.num_sentinels,
    mk.aces_count,
    mk.quad_kills_count,
    mk.triple_kills_count,
    cs.clutches_attempted,
    cs.clutches_won;

-- Report results
SELECT
    'Team game aggregation completed' AS status,
    (SELECT COUNT(DISTINCT game_id) FROM new_games) AS games_affected,
    (SELECT COUNT(*) FROM agg_team_game_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS rows_inserted,
    (SELECT MIN(game_started_at) FROM agg_team_game_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS min_date,
    (SELECT MAX(game_started_at) FROM agg_team_game_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS max_date;

-- Cleanup
DROP TABLE new_games;
