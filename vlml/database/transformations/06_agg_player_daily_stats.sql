-- Model: agg_player_daily_stats
-- Source: agg_player_game_stats, games, series
-- Type: Incremental (re-aggregate dates with new game stats)

-- Step 1: Find dates that have new or updated game stats
CREATE TEMP TABLE new_dates AS
SELECT DISTINCT CAST(game_started_at AS DATE) AS date
FROM agg_player_game_stats
WHERE calculated_at > COALESCE(
    (SELECT MAX(calculated_at) FROM agg_player_daily_stats),
    '1900-01-01'::TIMESTAMP
)
  AND game_started_at IS NOT NULL;

-- Step 2: Delete existing stats for those dates
DELETE FROM agg_player_daily_stats
WHERE date IN (SELECT date FROM new_dates);

-- Step 3: Re-aggregate player game stats into daily stats
WITH player_games AS (
    SELECT
        CAST(pgs.game_started_at AS DATE) AS game_date,
        pgs.player_id,
        pgs.player_name,
        pgs.team_name,
        pgs.tournament_name,
        pgs.tournament_year,
        pgs.game_id,
        pgs.game_won,
        pgs.rounds_played,
        pgs.rounds_won,
        pgs.kills,
        pgs.deaths,
        pgs.assists,
        pgs.first_bloods,
        pgs.first_deaths,
        pgs.plants,
        pgs.defuses,
        pgs.damage_dealt,
        pgs.damage_received,
        pgs.agent_name,
        g.series_id,
        s.winning_team_name
    FROM agg_player_game_stats pgs
    LEFT JOIN games g ON pgs.game_id = g.game_id
    LEFT JOIN series s ON g.series_id = s.series_id
    WHERE CAST(pgs.game_started_at AS DATE) IN (SELECT date FROM new_dates)
)
INSERT INTO agg_player_daily_stats
SELECT
    pg.game_date AS date,
    pg.player_id,
    pg.player_name,

    -- Denormalized dimensions
    MAX(pg.team_name) AS team_name,
    MAX(pg.tournament_name) AS tournament_name,
    MAX(pg.tournament_year) AS tournament_year,

    -- Daily activity
    COUNT(DISTINCT pg.series_id)::INTEGER AS series_played,
    COUNT(DISTINCT CASE WHEN pg.winning_team_name = pg.team_name THEN pg.series_id END)::INTEGER AS series_won,
    COUNT(DISTINCT pg.game_id)::INTEGER AS maps_played,
    SUM(CASE WHEN pg.game_won THEN 1 ELSE 0 END)::INTEGER AS maps_won,
    SUM(pg.rounds_played)::INTEGER AS rounds_played,
    SUM(pg.rounds_won)::INTEGER AS rounds_won,

    -- Aggregate metrics
    SUM(pg.kills)::INTEGER AS kills,
    SUM(pg.deaths)::INTEGER AS deaths,
    SUM(pg.assists)::INTEGER AS assists,
    SUM(pg.first_bloods)::INTEGER AS first_bloods,
    SUM(pg.first_deaths)::INTEGER AS first_deaths,
    SUM(pg.plants)::INTEGER AS plants,
    SUM(pg.defuses)::INTEGER AS defuses,
    SUM(pg.damage_dealt)::FLOAT AS damage_dealt,
    SUM(pg.damage_received)::FLOAT AS damage_received,

    -- Derived metrics
    CASE WHEN SUM(pg.deaths) > 0 THEN SUM(pg.kills)::FLOAT / SUM(pg.deaths) ELSE NULL END AS kd_ratio,
    CASE WHEN SUM(pg.deaths) > 0 THEN (SUM(pg.kills) + SUM(pg.assists))::FLOAT / SUM(pg.deaths) ELSE NULL END AS kda,
    CASE WHEN SUM(pg.rounds_played) > 0 THEN SUM(pg.damage_dealt)::FLOAT / SUM(pg.rounds_played) ELSE 0 END AS adr,
    CASE WHEN SUM(pg.rounds_played) > 0 THEN SUM(pg.kills)::FLOAT / SUM(pg.rounds_played) ELSE 0 END AS kpr,
    CASE WHEN SUM(pg.rounds_played) > 0 THEN SUM(pg.first_bloods)::FLOAT / SUM(pg.rounds_played) ELSE 0 END AS fk_percentage,
    CASE WHEN SUM(pg.rounds_played) > 0 THEN SUM(pg.first_deaths)::FLOAT / SUM(pg.rounds_played) ELSE 0 END AS fd_percentage,
    CASE
        WHEN COUNT(DISTINCT pg.series_id) > 0
        THEN COUNT(DISTINCT CASE WHEN pg.winning_team_name = pg.team_name THEN pg.series_id END)::FLOAT /
             COUNT(DISTINCT pg.series_id)
        ELSE NULL
    END AS win_rate,

    -- Agent diversity
    LIST(DISTINCT pg.agent_name) FILTER (WHERE pg.agent_name IS NOT NULL) AS agents_played,
    COUNT(DISTINCT pg.agent_name)::INTEGER AS unique_agents_count,

    -- Metadata
    CURRENT_TIMESTAMP AS calculated_at

FROM player_games pg
GROUP BY
    pg.game_date,
    pg.player_id,
    pg.player_name;

-- Report results
SELECT
    'Daily aggregation completed' AS status,
    (SELECT COUNT(*) FROM new_dates) AS dates_affected,
    (SELECT COUNT(*) FROM agg_player_daily_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS rows_inserted,
    (SELECT MIN(date) FROM agg_player_daily_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS min_date,
    (SELECT MAX(date) FROM agg_player_daily_stats WHERE calculated_at > CURRENT_TIMESTAMP - INTERVAL 1 MINUTE) AS max_date;

-- Cleanup
DROP TABLE new_dates;
