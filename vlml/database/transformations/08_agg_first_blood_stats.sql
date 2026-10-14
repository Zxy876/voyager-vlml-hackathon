-- Transformation: agg_first_blood_stats
-- Pre-join first blood events with round outcomes
-- Incremental: Only processes rounds with new data

-- Step 1: Find rounds that need processing
CREATE TEMP TABLE new_rounds AS
SELECT DISTINCT e.round_id
FROM base_events e
LEFT JOIN agg_first_blood_stats fbs ON fbs.round_id = e.round_id
LEFT JOIN rounds r ON r.round_id = e.round_id
WHERE e.is_first_blood = true
  AND e.round_id IS NOT NULL
  AND (
      fbs.round_id IS NULL
      OR r.ingested_at > COALESCE(
          (SELECT MAX(calculated_at) FROM agg_first_blood_stats),
          '1900-01-01'::TIMESTAMP
      )
  );

-- Step 2: Delete affected rows
DELETE FROM agg_first_blood_stats
WHERE round_id IN (SELECT round_id FROM new_rounds);

-- Step 3: Insert new/updated rows
INSERT INTO agg_first_blood_stats
SELECT
    -- Primary key
    r.round_id,

    -- Context
    r.game_id,
    r.series_id,
    r.tournament_name,
    r.tournament_year,
    r.map_name,
    r.round_number,

    -- First blood details
    e.actor_team_name AS fb_team,
    e.target_team_name AS fd_team,
    e.actor_player_name AS fb_player,
    e.actor_player_id AS fb_player_id,
    e.actor_agent_name AS fb_agent,
    e.target_player_name AS fd_player,
    e.target_agent_name AS fd_agent,
    e.actor_side AS fb_side,

    -- Round outcome
    r.winning_team_name,
    r.losing_team_name,
    r.end_reason,

    -- Calculated flags
    CASE WHEN r.winning_team_name = e.actor_team_name THEN 1 ELSE 0 END AS fb_team_won,
    CASE WHEN r.winning_team_name = e.target_team_name THEN 1 ELSE 0 END AS fd_team_won,

    -- Metadata
    CURRENT_TIMESTAMP AS calculated_at

FROM base_events e
JOIN rounds r ON e.round_id = r.round_id
WHERE e.is_first_blood = true
  AND e.round_id IN (SELECT round_id FROM new_rounds);

-- Cleanup
DROP TABLE IF EXISTS new_rounds;

-- Report summary
SELECT
    'agg_first_blood_stats' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT fb_team) AS unique_teams,
    ROUND(AVG(fb_team_won) * 100, 1) AS avg_fb_conversion_rate
FROM agg_first_blood_stats;
