-- Transformation: agg_team_series_stats
-- Team head-to-head records at series level
-- Creates two rows per series (one per team perspective)

-- Step 1: Find series that need processing
CREATE TEMP TABLE new_series AS
SELECT DISTINCT s.series_id
FROM series s
LEFT JOIN agg_team_series_stats tss ON tss.series_id = s.series_id
WHERE (
    tss.series_id IS NULL
    OR s.ingested_at > COALESCE(
        (SELECT MAX(calculated_at) FROM agg_team_series_stats),
        '1900-01-01'::TIMESTAMP
    )
);

-- Step 2: Delete affected rows
DELETE FROM agg_team_series_stats
WHERE series_id IN (SELECT series_id FROM new_series);

-- Step 3: Insert new/updated rows (team 1 perspective)
INSERT INTO agg_team_series_stats
SELECT
    s.series_id,
    s.team1_name AS team_name,
    s.tournament_name,
    s.tournament_year,
    s.start_time,
    s.team2_name AS opponent_name,

    CASE WHEN s.winning_team_name = s.team1_name THEN 1 ELSE 0 END AS series_won,
    CASE WHEN s.winning_team_name = s.team1_name THEN 0 ELSE 1 END AS series_lost,

    COUNT(DISTINCT g.game_id) AS maps_played,
    SUM(CASE WHEN g.winning_team_name = s.team1_name THEN 1 ELSE 0 END) AS maps_won,
    SUM(CASE WHEN g.winning_team_name = s.team2_name THEN 1 ELSE 0 END) AS maps_lost,

    CURRENT_TIMESTAMP AS calculated_at

FROM series s
LEFT JOIN games g ON s.series_id = g.series_id
WHERE s.series_id IN (SELECT series_id FROM new_series)
GROUP BY
    s.series_id, s.tournament_name, s.tournament_year, s.start_time,
    s.team1_name, s.team2_name, s.winning_team_name;

-- Step 4: Insert team 2 perspective
INSERT INTO agg_team_series_stats
SELECT
    s.series_id,
    s.team2_name AS team_name,
    s.tournament_name,
    s.tournament_year,
    s.start_time,
    s.team1_name AS opponent_name,

    CASE WHEN s.winning_team_name = s.team2_name THEN 1 ELSE 0 END AS series_won,
    CASE WHEN s.winning_team_name = s.team2_name THEN 0 ELSE 1 END AS series_lost,

    COUNT(DISTINCT g.game_id) AS maps_played,
    SUM(CASE WHEN g.winning_team_name = s.team2_name THEN 1 ELSE 0 END) AS maps_won,
    SUM(CASE WHEN g.winning_team_name = s.team1_name THEN 1 ELSE 0 END) AS maps_lost,

    CURRENT_TIMESTAMP AS calculated_at

FROM series s
LEFT JOIN games g ON s.series_id = g.series_id
WHERE s.series_id IN (SELECT series_id FROM new_series)
GROUP BY
    s.series_id, s.tournament_name, s.tournament_year, s.start_time,
    s.team1_name, s.team2_name, s.winning_team_name;

-- Cleanup
DROP TABLE IF EXISTS new_series;

-- Report summary
SELECT
    'agg_team_series_stats' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT team_name) AS unique_teams,
    SUM(series_won) AS total_series_wins
FROM agg_team_series_stats;
