-- Transformation: agg_team_map_stats
-- Team performance aggregated by map
-- Incremental: Only processes games with new data

-- Step 1: Find teams/maps that need processing
CREATE TEMP TABLE new_team_maps AS
SELECT DISTINCT team_name, map_name, tournament_name
FROM agg_player_game_stats pgs
WHERE pgs.map_name IS NOT NULL
  AND pgs.calculated_at > COALESCE(
      (SELECT MAX(calculated_at) FROM agg_team_map_stats),
      '1900-01-01'::TIMESTAMP
  );

-- Step 2: Delete affected rows
DELETE FROM agg_team_map_stats
WHERE (team_name, map_name, tournament_name) IN (
    SELECT team_name, map_name, tournament_name FROM new_team_maps
);

-- Step 3: Insert new/updated rows
INSERT INTO agg_team_map_stats
SELECT
    team_name,
    NULL AS opponent_team_name,  -- Aggregated across all opponents
    map_name,
    tournament_name,

    -- Game level stats
    COUNT(DISTINCT game_id) AS games_played,
    SUM(game_won::int) / 5 AS games_won,
    COUNT(DISTINCT game_id) - SUM(game_won::int) / 5 AS games_lost,
    ROUND(SUM(game_won::int) * 1.0 / COUNT(*), 4) AS map_win_rate,

    -- Round level stats
    SUM(rounds_played) / 5 AS total_rounds,
    SUM(rounds_won) / 5 AS rounds_won,
    ROUND(SUM(rounds_won) * 1.0 / NULLIF(SUM(rounds_played), 0), 4) AS round_win_rate,

    -- Performance metrics
    ROUND(AVG(adr), 1) AS avg_adr,
    ROUND(AVG(kd_ratio), 2) AS avg_kd,
    ROUND(AVG(kast_percentage), 4) AS avg_kast,

    -- Opening duels
    SUM(first_bloods) AS total_fb,
    SUM(first_deaths) AS total_fd,
    ROUND(AVG(opening_duel_win_rate), 4) AS avg_opening_wr,

    -- Trading
    ROUND(AVG(trade_success_rate), 4) AS avg_trade_rate,

    -- Clutches
    SUM(clutches_won) AS total_clutches_won,
    SUM(clutches_attempted) AS total_clutches_attempted,

    -- Metadata
    CURRENT_TIMESTAMP AS calculated_at

FROM agg_player_game_stats
WHERE map_name IS NOT NULL
  AND (team_name, map_name, tournament_name) IN (
      SELECT team_name, map_name, tournament_name FROM new_team_maps
  )
GROUP BY team_name, map_name, tournament_name;

-- Cleanup
DROP TABLE IF EXISTS new_team_maps;

-- Report summary
SELECT
    'agg_team_map_stats' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT team_name) AS unique_teams,
    COUNT(DISTINCT map_name) AS unique_maps
FROM agg_team_map_stats;
