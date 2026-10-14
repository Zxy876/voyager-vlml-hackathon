-- Model: agg_player_game_stats
-- Source: agg_player_round_stats
-- Type: Incremental (re-aggregate games with new round stats)

-- Step 1: Find games that have new or updated round stats
CREATE TEMP TABLE new_games AS
SELECT DISTINCT r.game_id
FROM agg_player_round_stats prs
JOIN rounds r ON prs.round_id = r.round_id
WHERE prs.calculated_at > COALESCE(
    (SELECT MAX(calculated_at) FROM agg_player_game_stats),
    '1900-01-01'::TIMESTAMP
);

-- Step 2: Delete existing stats for those games
DELETE FROM agg_player_game_stats
WHERE game_id IN (SELECT DISTINCT game_id FROM new_games);

-- Step 3: Re-aggregate round stats into game stats
INSERT INTO agg_player_game_stats
SELECT
    r.game_id,
    prs.player_id,
    prs.player_name,

    -- Denormalized dimensions
    MAX(prs.team_name) AS team_name,
    MAX(prs.opponent_team_name) AS opponent_team_name,
    MAX(prs.tournament_name) AS tournament_name,
    MAX(prs.tournament_year) AS tournament_year,
    MAX(prs.map_name) AS map_name,
    MAX(prs.agent_name) AS agent_name,

    -- Game context
    MIN(prs.started_at) AS game_started_at,
    MAX(prs.ended_at) AS game_ended_at,
    CASE
        WHEN MAX(g.winning_team_name) IS NOT NULL THEN MAX(g.winning_team_name) = MAX(prs.team_name)
        ELSE (SUM(CASE WHEN prs.round_won THEN 1 ELSE 0 END) > COUNT(*) / 2.0)
    END AS game_won,

    -- Aggregate metrics (sum across rounds)
    COUNT(*)::INTEGER AS rounds_played,
    SUM(CASE WHEN prs.round_won THEN 1 ELSE 0 END)::INTEGER AS rounds_won,
    SUM(prs.kills)::INTEGER AS kills,
    SUM(prs.deaths)::INTEGER AS deaths,
    SUM(prs.assists)::INTEGER AS assists,
    SUM(prs.first_bloods)::INTEGER AS first_bloods,
    SUM(prs.first_deaths)::INTEGER AS first_deaths,
    SUM(prs.plants)::INTEGER AS plants,
    SUM(prs.defuses)::INTEGER AS defuses,
    SUM(prs.abilities_used)::INTEGER AS abilities_used,
    SUM(prs.damage_dealt)::FLOAT AS damage_dealt,
    SUM(prs.damage_received)::FLOAT AS damage_received,

    -- Derived metrics
    CASE WHEN SUM(prs.deaths) > 0 THEN SUM(prs.kills)::FLOAT / SUM(prs.deaths) ELSE NULL END AS kd_ratio,
    CASE WHEN SUM(prs.deaths) > 0 THEN (SUM(prs.kills) + SUM(prs.assists))::FLOAT / SUM(prs.deaths) ELSE NULL END AS kda,
    SUM(prs.damage_dealt)::FLOAT / COUNT(*) AS adr,
    SUM(prs.kills)::FLOAT / COUNT(*) AS kpr,
    SUM(prs.first_bloods)::FLOAT / COUNT(*) AS fk_percentage,
    SUM(prs.first_deaths)::FLOAT / COUNT(*) AS fd_percentage,

    -- Composite scores
    SUM(CASE WHEN (prs.kills > 0 OR prs.assists > 0 OR prs.deaths = 0 OR prs.is_traded) THEN 1 ELSE 0 END)::FLOAT / COUNT(*) AS kast_percentage,
    NULL AS impact_rating,

    -- Trading aggregates
    SUM(CASE WHEN prs.is_trade_kill THEN 1 ELSE 0 END)::INTEGER AS trade_kills,
    SUM(CASE WHEN prs.is_traded THEN 1 ELSE 0 END)::INTEGER AS traded_deaths,
    SUM(CASE WHEN prs.is_untraded_death THEN 1 ELSE 0 END)::INTEGER AS untraded_deaths,
    CASE
        WHEN (SUM(CASE WHEN prs.is_traded THEN 1 ELSE 0 END) + SUM(CASE WHEN prs.is_untraded_death THEN 1 ELSE 0 END)) > 0
        THEN SUM(CASE WHEN prs.is_traded THEN 1 ELSE 0 END)::FLOAT /
             (SUM(CASE WHEN prs.is_traded THEN 1 ELSE 0 END) + SUM(CASE WHEN prs.is_untraded_death THEN 1 ELSE 0 END))
        ELSE NULL
    END AS trade_success_rate,
    AVG(prs.trade_kill_time) AS avg_trade_time,

    -- Opening duel aggregates
    SUM(CASE WHEN prs.is_opening_kill THEN 1 ELSE 0 END)::INTEGER AS opening_kills,
    SUM(CASE WHEN prs.is_opening_death THEN 1 ELSE 0 END)::INTEGER AS opening_deaths,
    (SUM(CASE WHEN prs.is_opening_kill THEN 1 ELSE 0 END) -
     SUM(CASE WHEN prs.is_opening_death THEN 1 ELSE 0 END))::INTEGER AS fk_fd_differential,
    CASE
        WHEN (SUM(CASE WHEN prs.is_opening_kill THEN 1 ELSE 0 END) + SUM(CASE WHEN prs.is_opening_death THEN 1 ELSE 0 END)) > 0
        THEN SUM(CASE WHEN prs.is_opening_kill THEN 1 ELSE 0 END)::FLOAT /
             (SUM(CASE WHEN prs.is_opening_kill THEN 1 ELSE 0 END) + SUM(CASE WHEN prs.is_opening_death THEN 1 ELSE 0 END))
        ELSE NULL
    END AS opening_duel_win_rate,

    -- Clutch aggregates
    SUM(CASE WHEN prs.is_clutch THEN 1 ELSE 0 END)::INTEGER AS clutches_attempted,
    SUM(CASE WHEN prs.clutch_won THEN 1 ELSE 0 END)::INTEGER AS clutches_won,
    CASE
        WHEN SUM(CASE WHEN prs.is_clutch THEN 1 ELSE 0 END) > 0
        THEN SUM(CASE WHEN prs.clutch_won THEN 1 ELSE 0 END)::FLOAT / SUM(CASE WHEN prs.is_clutch THEN 1 ELSE 0 END)
        ELSE NULL
    END AS clutch_win_rate,
    SUM(CASE WHEN prs.is_1v1 AND prs.clutch_won THEN 1 ELSE 0 END)::INTEGER AS clutches_1v1_won,
    SUM(CASE WHEN prs.is_1v2 AND prs.clutch_won THEN 1 ELSE 0 END)::INTEGER AS clutches_1v2_won,
    SUM(CASE WHEN prs.is_1v3 AND prs.clutch_won THEN 1 ELSE 0 END)::INTEGER AS clutches_1v3_won,

    -- Multi-kill aggregates
    SUM(CASE WHEN prs.is_double_kill THEN 1 ELSE 0 END)::INTEGER AS double_kills,
    SUM(CASE WHEN prs.is_triple_kill THEN 1 ELSE 0 END)::INTEGER AS triple_kills,
    SUM(CASE WHEN prs.is_quad_kill THEN 1 ELSE 0 END)::INTEGER AS quad_kills,
    SUM(CASE WHEN prs.is_ace THEN 1 ELSE 0 END)::INTEGER AS aces,

    -- Economy performance
    SUM(CASE WHEN prs.is_eco_round THEN 1 ELSE 0 END)::INTEGER AS eco_rounds_played,
    SUM(CASE WHEN prs.is_eco_round AND prs.round_won THEN 1 ELSE 0 END)::INTEGER AS eco_rounds_won,
    CASE
        WHEN SUM(CASE WHEN prs.is_eco_round THEN 1 ELSE 0 END) > 0
        THEN SUM(CASE WHEN prs.is_eco_round AND prs.round_won THEN 1 ELSE 0 END)::FLOAT /
             SUM(CASE WHEN prs.is_eco_round THEN 1 ELSE 0 END)
        ELSE NULL
    END AS eco_win_rate,
    SUM(CASE WHEN prs.is_thrifty THEN 1 ELSE 0 END)::INTEGER AS thrifty_count,
    AVG(prs.loadout_value) AS avg_loadout_value,

    -- Consistency (simplified for now)
    NULL AS rating_variance,
    NULL AS first_half_rating,
    NULL AS second_half_rating,
    NULL AS half_diff,

    -- Weapon aggregates
    SUM(prs.total_headshot_kills)::INTEGER AS total_headshot_kills,
    SUM(prs.headshot_kills_denom)::INTEGER AS headshot_kills_denom,
    SUM(prs.headshot_hits_total)::INTEGER AS headshot_hits_total,
    SUM(prs.hits_total)::INTEGER AS hits_total,
    SUM(prs.bodyshot_kills)::INTEGER AS total_bodyshot_kills,
    0 AS vandal_kills,
    0 AS phantom_kills,
    0 AS operator_kills,
    0 AS sheriff_kills,
    0 AS classic_kills,
    SUM(prs.rifle_kills)::INTEGER AS rifle_kills,
    SUM(prs.smg_kills)::INTEGER AS smg_kills,
    SUM(prs.pistol_kills)::INTEGER AS pistol_kills,
    SUM(prs.sniper_kills)::INTEGER AS sniper_kills,
    NULL AS weapon_preference,
    (SUM(prs.sniper_kills)::FLOAT / NULLIF(SUM(prs.kills), 0) > 0.3) AS is_operator_player,

    -- Metadata
    CURRENT_TIMESTAMP AS calculated_at

FROM agg_player_round_stats prs
JOIN rounds r ON prs.round_id = r.round_id
LEFT JOIN games g ON r.game_id = g.game_id
WHERE r.game_id IN (SELECT DISTINCT game_id FROM new_games)
GROUP BY
    r.game_id,
    prs.player_id,
    prs.player_name;

-- Report results
SELECT
    'Game aggregation completed' AS status,
    (SELECT COUNT(DISTINCT game_id) FROM new_games) AS games_affected,
    (SELECT COUNT(*) FROM agg_player_game_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS rows_inserted,
    (SELECT MIN(game_started_at) FROM agg_player_game_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS min_date,
    (SELECT MAX(game_started_at) FROM agg_player_game_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS max_date;

-- Cleanup
DROP TABLE new_games;
