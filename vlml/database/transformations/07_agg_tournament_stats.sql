-- Model: agg_tournament_stats
-- Source: agg_player_series_stats, agg_team_game_stats, games, series
-- Type: Incremental (re-aggregate tournaments with new stats)

-- Step 1: Find tournaments that have new or updated stats
CREATE TEMP TABLE new_tournaments AS
SELECT DISTINCT s.tournament_id
FROM (
    SELECT DISTINCT g.series_id
    FROM agg_player_series_stats pss
    JOIN games g ON pss.series_id = g.series_id
    WHERE pss.calculated_at > COALESCE(
        (SELECT MAX(calculated_at) FROM agg_tournament_stats),
        '1900-01-01'::TIMESTAMP
    )
    UNION
    SELECT DISTINCT tgs.series_id
    FROM agg_team_game_stats tgs
    WHERE tgs.calculated_at > COALESCE(
        (SELECT MAX(calculated_at) FROM agg_tournament_stats),
        '1900-01-01'::TIMESTAMP
    )
) src
JOIN series s ON s.series_id = src.series_id
WHERE s.tournament_id IS NOT NULL;

-- Step 2: Delete existing stats for those tournaments
DELETE FROM agg_tournament_stats
WHERE tournament_id IN (SELECT tournament_id FROM new_tournaments);

-- Step 3: Build tournament boundaries
WITH tournament_bounds AS (
    SELECT
        tournament_id,
        MIN(start_time)::DATE AS tournament_start_date,
        MAX(start_time)::DATE AS tournament_end_date,
        MAX(tournament_name) AS tournament_name,
        MAX(tournament_year) AS tournament_year,
        MAX(tournament_region) AS tournament_region
    FROM series
    WHERE tournament_id IN (SELECT tournament_id FROM new_tournaments)
    GROUP BY tournament_id
),
player_agents AS (
    SELECT
        s.tournament_id,
        pgs.player_id,
        LIST(DISTINCT pgs.agent_name) FILTER (WHERE pgs.agent_name IS NOT NULL) AS agents_played,
        COUNT(DISTINCT pgs.agent_name)::INTEGER AS unique_agents_count
    FROM agg_player_game_stats pgs
    JOIN games g ON pgs.game_id = g.game_id
    JOIN series s ON g.series_id = s.series_id
    WHERE s.tournament_id IN (SELECT tournament_id FROM new_tournaments)
    GROUP BY s.tournament_id, pgs.player_id
),
team_rosters AS (
    SELECT
        s.tournament_id,
        pss.team_name,
        LIST(DISTINCT pss.player_name) FILTER (WHERE pss.player_name IS NOT NULL) AS roster
    FROM agg_player_series_stats pss
    JOIN series s ON pss.series_id = s.series_id
    WHERE s.tournament_id IN (SELECT tournament_id FROM new_tournaments)
    GROUP BY s.tournament_id, pss.team_name
)
INSERT INTO agg_tournament_stats
SELECT
    pss_tournament.tournament_id,
    pss_tournament.tournament_name,
    pss_tournament.entity_type,
    pss_tournament.entity_id,
    pss_tournament.entity_name,
    pss_tournament.tournament_year,
    pss_tournament.tournament_region,
    pss_tournament.tournament_start_date,
    pss_tournament.tournament_end_date,
    pss_tournament.placement,
    pss_tournament.prize_money,
    pss_tournament.series_played,
    pss_tournament.series_won,
    pss_tournament.maps_played,
    pss_tournament.maps_won,
    pss_tournament.rounds_played,
    pss_tournament.rounds_won,
    pss_tournament.kills,
    pss_tournament.deaths,
    pss_tournament.assists,
    pss_tournament.first_bloods,
    pss_tournament.first_deaths,
    pss_tournament.plants,
    pss_tournament.defuses,
    pss_tournament.damage_dealt,
    pss_tournament.damage_received,
    pss_tournament.kd_ratio,
    pss_tournament.kda,
    pss_tournament.adr,
    pss_tournament.kpr,
    pss_tournament.fk_percentage,
    pss_tournament.fd_percentage,
    pss_tournament.win_rate,
    pss_tournament.map_win_rate,
    pss_tournament.agents_played,
    pss_tournament.unique_agents_count,
    pss_tournament.team_name,
    pss_tournament.roster,
    CURRENT_TIMESTAMP AS calculated_at
FROM (
    -- Player rows
    SELECT
        tb.tournament_id,
        tb.tournament_name,
        'player' AS entity_type,
        pss.player_id AS entity_id,
        MAX(pss.player_name) AS entity_name,
        tb.tournament_year,
        tb.tournament_region,
        tb.tournament_start_date,
        tb.tournament_end_date,
        NULL AS placement,
        NULL AS prize_money,
        COUNT(DISTINCT pss.series_id)::INTEGER AS series_played,
        SUM(CASE WHEN pss.series_won THEN 1 ELSE 0 END)::INTEGER AS series_won,
        SUM(pss.maps_played)::INTEGER AS maps_played,
        SUM(pss.maps_won)::INTEGER AS maps_won,
        SUM(pss.rounds_played)::INTEGER AS rounds_played,
        SUM(pss.rounds_won)::INTEGER AS rounds_won,
        SUM(pss.kills)::INTEGER AS kills,
        SUM(pss.deaths)::INTEGER AS deaths,
        SUM(pss.assists)::INTEGER AS assists,
        SUM(pss.first_bloods)::INTEGER AS first_bloods,
        SUM(pss.first_deaths)::INTEGER AS first_deaths,
        SUM(pss.plants)::INTEGER AS plants,
        SUM(pss.defuses)::INTEGER AS defuses,
        SUM(pss.damage_dealt)::FLOAT AS damage_dealt,
        SUM(pss.damage_received)::FLOAT AS damage_received,
        CASE WHEN SUM(pss.deaths) > 0 THEN SUM(pss.kills)::FLOAT / SUM(pss.deaths) ELSE NULL END AS kd_ratio,
        CASE WHEN SUM(pss.deaths) > 0 THEN (SUM(pss.kills) + SUM(pss.assists))::FLOAT / SUM(pss.deaths) ELSE NULL END AS kda,
        CASE WHEN SUM(pss.rounds_played) > 0 THEN SUM(pss.damage_dealt)::FLOAT / SUM(pss.rounds_played) ELSE 0 END AS adr,
        CASE WHEN SUM(pss.rounds_played) > 0 THEN SUM(pss.kills)::FLOAT / SUM(pss.rounds_played) ELSE 0 END AS kpr,
        CASE WHEN SUM(pss.rounds_played) > 0 THEN SUM(pss.first_bloods)::FLOAT / SUM(pss.rounds_played) ELSE 0 END AS fk_percentage,
        CASE WHEN SUM(pss.rounds_played) > 0 THEN SUM(pss.first_deaths)::FLOAT / SUM(pss.rounds_played) ELSE 0 END AS fd_percentage,
        CASE WHEN COUNT(DISTINCT pss.series_id) > 0 THEN SUM(CASE WHEN pss.series_won THEN 1 ELSE 0 END)::FLOAT / COUNT(DISTINCT pss.series_id) ELSE NULL END AS win_rate,
        CASE WHEN SUM(pss.maps_played) > 0 THEN SUM(pss.maps_won)::FLOAT / SUM(pss.maps_played) ELSE NULL END AS map_win_rate,
        MAX(pa.agents_played) AS agents_played,
        MAX(pa.unique_agents_count) AS unique_agents_count,
        MAX(pss.team_name) AS team_name,
        NULL AS roster
    FROM agg_player_series_stats pss
    JOIN series s ON pss.series_id = s.series_id
    JOIN tournament_bounds tb ON tb.tournament_id = s.tournament_id
    LEFT JOIN player_agents pa ON pa.tournament_id = s.tournament_id AND pa.player_id = pss.player_id
    WHERE s.tournament_id IN (SELECT tournament_id FROM new_tournaments)
    GROUP BY
        tb.tournament_id,
        tb.tournament_name,
        tb.tournament_year,
        tb.tournament_region,
        tb.tournament_start_date,
        tb.tournament_end_date,
        pss.player_id

    UNION ALL

    -- Team rows
    SELECT
        tb.tournament_id,
        tb.tournament_name,
        'team' AS entity_type,
        tgs.team_name AS entity_id,
        tgs.team_name AS entity_name,
        tb.tournament_year,
        tb.tournament_region,
        tb.tournament_start_date,
        tb.tournament_end_date,
        NULL AS placement,
        NULL AS prize_money,
        COUNT(DISTINCT tgs.series_id)::INTEGER AS series_played,
        COUNT(DISTINCT CASE WHEN s.winning_team_name = tgs.team_name THEN tgs.series_id END)::INTEGER AS series_won,
        COUNT(DISTINCT tgs.game_id)::INTEGER AS maps_played,
        SUM(CASE WHEN tgs.game_won THEN 1 ELSE 0 END)::INTEGER AS maps_won,
        SUM(tgs.rounds_won + tgs.rounds_lost)::INTEGER AS rounds_played,
        SUM(tgs.rounds_won)::INTEGER AS rounds_won,
        SUM(tgs.team_kills)::INTEGER AS kills,
        SUM(tgs.team_deaths)::INTEGER AS deaths,
        SUM(tgs.team_assists)::INTEGER AS assists,
        SUM(tgs.first_bloods)::INTEGER AS first_bloods,
        SUM(tgs.first_deaths)::INTEGER AS first_deaths,
        SUM(tgs.plants)::INTEGER AS plants,
        SUM(tgs.defuses)::INTEGER AS defuses,
        SUM(tgs.team_damage_dealt)::FLOAT AS damage_dealt,
        SUM(tgs.team_damage_received)::FLOAT AS damage_received,
        CASE WHEN SUM(tgs.team_deaths) > 0 THEN SUM(tgs.team_kills)::FLOAT / SUM(tgs.team_deaths) ELSE NULL END AS kd_ratio,
        CASE WHEN SUM(tgs.team_deaths) > 0 THEN (SUM(tgs.team_kills) + SUM(tgs.team_assists))::FLOAT / SUM(tgs.team_deaths) ELSE NULL END AS kda,
        CASE WHEN SUM(tgs.rounds_won + tgs.rounds_lost) > 0 THEN SUM(tgs.team_damage_dealt)::FLOAT / SUM(tgs.rounds_won + tgs.rounds_lost) ELSE 0 END AS adr,
        CASE WHEN SUM(tgs.rounds_won + tgs.rounds_lost) > 0 THEN SUM(tgs.team_kills)::FLOAT / SUM(tgs.rounds_won + tgs.rounds_lost) ELSE 0 END AS kpr,
        CASE WHEN SUM(tgs.rounds_won + tgs.rounds_lost) > 0 THEN SUM(tgs.first_bloods)::FLOAT / SUM(tgs.rounds_won + tgs.rounds_lost) ELSE 0 END AS fk_percentage,
        CASE WHEN SUM(tgs.rounds_won + tgs.rounds_lost) > 0 THEN SUM(tgs.first_deaths)::FLOAT / SUM(tgs.rounds_won + tgs.rounds_lost) ELSE 0 END AS fd_percentage,
        CASE WHEN COUNT(DISTINCT tgs.series_id) > 0 THEN COUNT(DISTINCT CASE WHEN s.winning_team_name = tgs.team_name THEN tgs.series_id END)::FLOAT / COUNT(DISTINCT tgs.series_id) ELSE NULL END AS win_rate,
        CASE WHEN COUNT(DISTINCT tgs.game_id) > 0 THEN SUM(CASE WHEN tgs.game_won THEN 1 ELSE 0 END)::FLOAT / COUNT(DISTINCT tgs.game_id) ELSE NULL END AS map_win_rate,
        NULL AS agents_played,
        NULL AS unique_agents_count,
        tgs.team_name AS team_name,
        tr.roster AS roster
    FROM agg_team_game_stats tgs
    JOIN series s ON tgs.series_id = s.series_id
    JOIN tournament_bounds tb ON tb.tournament_id = s.tournament_id
    LEFT JOIN team_rosters tr ON tr.tournament_id = s.tournament_id AND tr.team_name = tgs.team_name
    WHERE s.tournament_id IN (SELECT tournament_id FROM new_tournaments)
    GROUP BY
        tb.tournament_id,
        tb.tournament_name,
        tb.tournament_year,
        tb.tournament_region,
        tb.tournament_start_date,
        tb.tournament_end_date,
        tgs.team_name,
        tr.roster
) pss_tournament;

-- Report results
SELECT
    'Tournament aggregation completed' AS status,
    (SELECT COUNT(*) FROM new_tournaments) AS tournaments_affected,
    (SELECT COUNT(*) FROM agg_tournament_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS rows_inserted;

-- Cleanup
DROP TABLE new_tournaments;
