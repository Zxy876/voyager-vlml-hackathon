-- Situation Benchmarks: Historical baselines for LLM reference
-- Provides clutch rates, retake rates, economy win rates from historical data
-- Usage: Filtered by series_ids to compute benchmarks from relevant dataset
-- Note: If no filter, computes benchmarks from entire database

-- Clutch win rates by situation
WITH clutch_stats AS (
    SELECT
        prs.clutch_opponents,
        COUNT(*) AS attempts,
        SUM(CASE WHEN prs.clutch_won THEN 1 ELSE 0 END) AS wins
    FROM agg_player_round_stats prs
    JOIN rounds r ON r.round_id = prs.round_id
    JOIN games g ON g.game_id = r.game_id
    WHERE prs.is_clutch = TRUE
      AND prs.clutch_opponents IS NOT NULL
      {series_filter}
    GROUP BY prs.clutch_opponents
),
-- Economy round win rates
economy_stats AS (
    SELECT
        CASE
            WHEN trs.round_number IN (1, 13, 25) THEN 'pistol'
            WHEN trs.loadout_value < 8000 THEN 'eco'
            WHEN trs.loadout_value < 16000 THEN 'force'
            ELSE 'full_buy'
        END AS buy_type,
        CASE
            WHEN opp.loadout_value < 8000 THEN 'eco'
            WHEN opp.loadout_value < 16000 THEN 'force'
            ELSE 'full_buy'
        END AS opp_buy_type,
        COUNT(*) AS rounds_played,
        SUM(CASE WHEN trs.round_won THEN 1 ELSE 0 END) AS rounds_won
    FROM agg_team_round_stats trs
    JOIN rounds r ON r.round_id = trs.round_id
    JOIN games g ON g.game_id = r.game_id
    JOIN agg_team_round_stats opp
        ON opp.round_id = trs.round_id
        AND opp.team_name != trs.team_name
    WHERE 1=1
      {series_filter}
    GROUP BY buy_type, opp_buy_type
),
-- Man advantage/disadvantage win rates
man_advantage_stats AS (
    SELECT
        -- Calculate man advantage at different points
        5 - trs.team_deaths AS team_alive_approx,
        5 - opp.team_deaths AS opp_alive_approx,
        trs.side,
        COUNT(*) AS rounds,
        SUM(CASE WHEN trs.round_won THEN 1 ELSE 0 END) AS wins
    FROM agg_team_round_stats trs
    JOIN rounds r ON r.round_id = trs.round_id
    JOIN games g ON g.game_id = r.game_id
    JOIN agg_team_round_stats opp
        ON opp.round_id = trs.round_id
        AND opp.team_name != trs.team_name
    WHERE 1=1
      {series_filter}
    GROUP BY team_alive_approx, opp_alive_approx, trs.side
),
-- First blood conversion rates
fb_conversion AS (
    SELECT
        trs.side,
        SUM(trs.first_bloods) AS total_fbs,
        SUM(CASE WHEN trs.first_bloods > 0 AND trs.round_won THEN 1 ELSE 0 END) AS fb_wins,
        SUM(trs.first_deaths) AS total_fds,
        SUM(CASE WHEN trs.first_deaths > 0 AND trs.round_won THEN 1 ELSE 0 END) AS fd_salvages
    FROM agg_team_round_stats trs
    JOIN rounds r ON r.round_id = trs.round_id
    JOIN games g ON g.game_id = r.game_id
    WHERE 1=1
      {series_filter}
    GROUP BY trs.side
),
-- Retake success rates (defense side, after plant)
retake_stats AS (
    SELECT
        5 - trs.team_deaths AS defenders_alive_approx,
        COUNT(*) AS retake_attempts,
        SUM(CASE WHEN trs.round_won THEN 1 ELSE 0 END) AS retake_wins
    FROM agg_team_round_stats trs
    JOIN rounds r ON r.round_id = trs.round_id
    JOIN games g ON g.game_id = r.game_id
    WHERE trs.side = 'defense'
      AND trs.retake_attempted_total > 0
      {series_filter}
    GROUP BY defenders_alive_approx
),
-- Total rounds for sample size
total_stats AS (
    SELECT
        COUNT(DISTINCT r.round_id) AS total_rounds,
        COUNT(DISTINCT g.game_id) AS total_games,
        COUNT(DISTINCT g.series_id) AS total_series,
        MIN(r.started_at) AS date_range_start,
        MAX(r.started_at) AS date_range_end
    FROM rounds r
    JOIN games g ON g.game_id = r.game_id
    WHERE 1=1
      {series_filter}
)
SELECT
    'clutch_stats' AS stat_type,
    clutch_opponents AS situation_key,
    NULL AS secondary_key,
    attempts AS denominator,
    wins AS numerator,
    ROUND(wins * 100.0 / NULLIF(attempts, 0), 1) AS rate
FROM clutch_stats
WHERE attempts >= 10
UNION ALL
SELECT
    'economy_matchup' AS stat_type,
    buy_type AS situation_key,
    opp_buy_type AS secondary_key,
    rounds_played AS denominator,
    rounds_won AS numerator,
    ROUND(rounds_won * 100.0 / NULLIF(rounds_played, 0), 1) AS rate
FROM economy_stats
WHERE rounds_played >= 20
UNION ALL
SELECT
    'fb_conversion_' || side AS stat_type,
    'fb_conversion' AS situation_key,
    NULL AS secondary_key,
    total_fbs AS denominator,
    fb_wins AS numerator,
    ROUND(fb_wins * 100.0 / NULLIF(total_fbs, 0), 1) AS rate
FROM fb_conversion
UNION ALL
SELECT
    'fd_salvage_' || side AS stat_type,
    'fd_salvage' AS situation_key,
    NULL AS secondary_key,
    total_fds AS denominator,
    fd_salvages AS numerator,
    ROUND(fd_salvages * 100.0 / NULLIF(total_fds, 0), 1) AS rate
FROM fb_conversion
UNION ALL
SELECT
    'retake' AS stat_type,
    CAST(defenders_alive_approx AS VARCHAR) || 'v5' AS situation_key,
    NULL AS secondary_key,
    retake_attempts AS denominator,
    retake_wins AS numerator,
    ROUND(retake_wins * 100.0 / NULLIF(retake_attempts, 0), 1) AS rate
FROM retake_stats
WHERE retake_attempts >= 10
UNION ALL
SELECT
    'sample_size' AS stat_type,
    'total_rounds' AS situation_key,
    NULL AS secondary_key,
    total_rounds AS denominator,
    total_games AS numerator,
    total_series AS rate
FROM total_stats
ORDER BY stat_type, situation_key, secondary_key
