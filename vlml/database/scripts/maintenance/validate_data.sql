-- Data Validation: Check for duplicate primary keys
-- Rule: total_rows - distinct_pk = 0 for all tables

SELECT 'VALIDATION RESULTS' AS header;
SELECT '==================' AS separator;

-- base_events (PK: event_id)
SELECT
    'base_events' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT event_id) AS distinct_pk,
    COUNT(*) - COUNT(DISTINCT event_id) AS duplicates,
    CASE WHEN COUNT(*) = COUNT(DISTINCT event_id) THEN '✓ PASS' ELSE '✗ FAIL' END AS status
FROM base_events;

-- series (PK: series_id)
SELECT
    'series' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT series_id) AS distinct_pk,
    COUNT(*) - COUNT(DISTINCT series_id) AS duplicates,
    CASE WHEN COUNT(*) = COUNT(DISTINCT series_id) THEN '✓ PASS' ELSE '✗ FAIL' END AS status
FROM series;

-- games (PK: game_id)
SELECT
    'games' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT game_id) AS distinct_pk,
    COUNT(*) - COUNT(DISTINCT game_id) AS duplicates,
    CASE WHEN COUNT(*) = COUNT(DISTINCT game_id) THEN '✓ PASS' ELSE '✗ FAIL' END AS status
FROM games;

-- rounds (PK: round_id)
SELECT
    'rounds' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT round_id) AS distinct_pk,
    COUNT(*) - COUNT(DISTINCT round_id) AS duplicates,
    CASE WHEN COUNT(*) = COUNT(DISTINCT round_id) THEN '✓ PASS' ELSE '✗ FAIL' END AS status
FROM rounds;

-- agg_player_round_stats (PK: round_id, player_id)
SELECT
    'agg_player_round_stats' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT (round_id, player_id)) AS distinct_pk,
    COUNT(*) - COUNT(DISTINCT (round_id, player_id)) AS duplicates,
    CASE WHEN COUNT(*) = COUNT(DISTINCT (round_id, player_id)) THEN '✓ PASS' ELSE '✗ FAIL' END AS status
FROM agg_player_round_stats;

-- agg_player_game_stats (PK: game_id, player_id)
SELECT
    'agg_player_game_stats' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT (game_id, player_id)) AS distinct_pk,
    COUNT(*) - COUNT(DISTINCT (game_id, player_id)) AS duplicates,
    CASE WHEN COUNT(*) = COUNT(DISTINCT (game_id, player_id)) THEN '✓ PASS' ELSE '✗ FAIL' END AS status
FROM agg_player_game_stats;

-- agg_player_series_stats (PK: series_id, player_id)
SELECT
    'agg_player_series_stats' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT (series_id, player_id)) AS distinct_pk,
    COUNT(*) - COUNT(DISTINCT (series_id, player_id)) AS duplicates,
    CASE WHEN COUNT(*) = COUNT(DISTINCT (series_id, player_id)) THEN '✓ PASS' ELSE '✗ FAIL' END AS status
FROM agg_player_series_stats;

-- agg_team_game_stats (PK: game_id, team_name)
SELECT
    'agg_team_game_stats' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT (game_id, team_name)) AS distinct_pk,
    COUNT(*) - COUNT(DISTINCT (game_id, team_name)) AS duplicates,
    CASE WHEN COUNT(*) = COUNT(DISTINCT (game_id, team_name)) THEN '✓ PASS' ELSE '✗ FAIL' END AS status
FROM agg_team_game_stats;

-- agg_team_round_stats (PK: round_id, team_name)
SELECT
    'agg_team_round_stats' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT (round_id, team_name)) AS distinct_pk,
    COUNT(*) - COUNT(DISTINCT (round_id, team_name)) AS duplicates,
    CASE WHEN COUNT(*) = COUNT(DISTINCT (round_id, team_name)) THEN '✓ PASS' ELSE '✗ FAIL' END AS status
FROM agg_team_round_stats;

-- agg_player_daily_stats (PK: date, player_id)
SELECT
    'agg_player_daily_stats' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT (date, player_id)) AS distinct_pk,
    COUNT(*) - COUNT(DISTINCT (date, player_id)) AS duplicates,
    CASE WHEN COUNT(*) = COUNT(DISTINCT (date, player_id)) THEN '✓ PASS' ELSE '✗ FAIL' END AS status
FROM agg_player_daily_stats;

-- agg_tournament_stats (PK: tournament_id, entity_type, entity_id)
SELECT
    'agg_tournament_stats' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT (tournament_id, entity_type, entity_id)) AS distinct_pk,
    COUNT(*) - COUNT(DISTINCT (tournament_id, entity_type, entity_id)) AS duplicates,
    CASE WHEN COUNT(*) = COUNT(DISTINCT (tournament_id, entity_type, entity_id)) THEN '✓ PASS' ELSE '✗ FAIL' END AS status
FROM agg_tournament_stats;

-- Summary
SELECT '==================' AS separator;
SELECT 'Validation complete. All tables should show duplicates = 0' AS summary;
