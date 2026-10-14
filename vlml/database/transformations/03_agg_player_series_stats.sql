-- Model: agg_player_series_stats
-- Source: agg_player_game_stats, series, games
-- Type: Incremental (re-aggregate series with new game stats)

-- Step 1: Find series that have new/updated game stats
CREATE TEMP TABLE new_series AS
SELECT DISTINCT g.series_id
FROM agg_player_game_stats pgs
LEFT JOIN games g ON pgs.game_id = g.game_id
WHERE g.series_id IS NOT NULL
  AND pgs.calculated_at > COALESCE(
      (SELECT MAX(calculated_at) FROM agg_player_series_stats),
      '1900-01-01'::TIMESTAMP
  );

-- Step 2: Delete existing stats for those series
DELETE FROM agg_player_series_stats
WHERE series_id IN (SELECT series_id FROM new_series);

-- Step 3: Re-aggregate ALL game stats for those series (old + new)
INSERT INTO agg_player_series_stats
SELECT
    g.series_id,
    pgs.player_id,
    pgs.player_name,

    -- Denormalized dimensions
    MAX(pgs.team_name) AS team_name,
    MAX(pgs.opponent_team_name) AS opponent_team_name,
    MAX(pgs.tournament_name) AS tournament_name,
    MAX(pgs.tournament_year) AS tournament_year,
    MAX(s.tournament_region) AS tournament_region,

    -- Series context
    MAX(s.start_time) AS series_started_at,
    CASE WHEN MAX(pgs.team_name) = MAX(s.winning_team_name) THEN TRUE ELSE FALSE END AS series_won,
    COUNT(DISTINCT pgs.game_id)::INTEGER AS maps_played,
    SUM(CASE WHEN pgs.game_won THEN 1 ELSE 0 END)::INTEGER AS maps_won,

    -- Aggregate metrics
    SUM(pgs.rounds_played)::INTEGER AS rounds_played,
    SUM(pgs.rounds_won)::INTEGER AS rounds_won,
    SUM(pgs.kills)::INTEGER AS kills,
    SUM(pgs.deaths)::INTEGER AS deaths,
    SUM(pgs.assists)::INTEGER AS assists,
    SUM(pgs.first_bloods)::INTEGER AS first_bloods,
    SUM(pgs.first_deaths)::INTEGER AS first_deaths,
    SUM(pgs.plants)::INTEGER AS plants,
    SUM(pgs.defuses)::INTEGER AS defuses,
    SUM(pgs.abilities_used)::INTEGER AS abilities_used,
    SUM(pgs.damage_dealt)::FLOAT AS damage_dealt,
    SUM(pgs.damage_received)::FLOAT AS damage_received,

    -- Derived metrics
    CASE WHEN SUM(pgs.deaths) > 0 THEN SUM(pgs.kills)::FLOAT / SUM(pgs.deaths)::FLOAT ELSE SUM(pgs.kills)::FLOAT END AS kd_ratio,
    CASE WHEN SUM(pgs.deaths) > 0 THEN (SUM(pgs.kills) + SUM(pgs.assists))::FLOAT / SUM(pgs.deaths)::FLOAT ELSE (SUM(pgs.kills) + SUM(pgs.assists))::FLOAT END AS kda,
    CASE WHEN SUM(pgs.rounds_played) > 0 THEN SUM(pgs.damage_dealt)::FLOAT / SUM(pgs.rounds_played)::FLOAT ELSE 0 END AS adr,
    CASE WHEN SUM(pgs.rounds_played) > 0 THEN SUM(pgs.kills)::FLOAT / SUM(pgs.rounds_played)::FLOAT ELSE 0 END AS kpr,
    CASE WHEN SUM(pgs.rounds_played) > 0 THEN SUM(pgs.first_bloods)::FLOAT / SUM(pgs.rounds_played)::FLOAT ELSE 0 END AS fk_percentage,
    CASE WHEN SUM(pgs.rounds_played) > 0 THEN SUM(pgs.first_deaths)::FLOAT / SUM(pgs.rounds_played)::FLOAT ELSE 0 END AS fd_percentage,
    NULL AS acs,

    -- Agent pool
    LIST(DISTINCT pgs.agent_name) FILTER (WHERE pgs.agent_name IS NOT NULL) AS agents_played,

    -- Metadata
    CURRENT_TIMESTAMP AS calculated_at

FROM agg_player_game_stats pgs
LEFT JOIN games g ON pgs.game_id = g.game_id
LEFT JOIN series s ON g.series_id = s.series_id
WHERE g.series_id IN (SELECT series_id FROM new_series)
GROUP BY
    g.series_id,
    pgs.player_id,
    pgs.player_name;

-- Report results
SELECT
    'Incremental load completed' AS status,
    (SELECT COUNT(*) FROM new_series) AS series_affected,
    (SELECT COUNT(*) FROM agg_player_series_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS rows_inserted,
    (SELECT MIN(series_started_at) FROM agg_player_series_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS min_date,
    (SELECT MAX(series_started_at) FROM agg_player_series_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS max_date;

-- Cleanup
DROP TABLE new_series;
