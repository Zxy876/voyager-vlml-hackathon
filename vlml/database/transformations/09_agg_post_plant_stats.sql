-- Transformation: agg_post_plant_stats
-- Pre-join plant events with round outcomes
-- Incremental: Only processes rounds with new data

-- Step 1: Find rounds that need processing
CREATE TEMP TABLE new_rounds AS
SELECT DISTINCT e.round_id
FROM base_events e
LEFT JOIN agg_post_plant_stats pps ON pps.round_id = e.round_id
LEFT JOIN rounds r ON r.round_id = e.round_id
WHERE e.is_plant = true
  AND e.round_id IS NOT NULL
  AND (
      pps.round_id IS NULL
      OR r.ingested_at > COALESCE(
          (SELECT MAX(calculated_at) FROM agg_post_plant_stats),
          '1900-01-01'::TIMESTAMP
      )
  );

-- Step 2: Delete affected rows
DELETE FROM agg_post_plant_stats
WHERE round_id IN (SELECT round_id FROM new_rounds);

-- Step 3: Insert new/updated rows
INSERT INTO agg_post_plant_stats
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

    -- Plant details
    e.actor_team_name AS planting_team,
    e.target_team_name AS defending_team,
    e.actor_player_name AS planter,
    e.actor_player_id AS planter_id,
    e.actor_agent_name AS planter_agent,

    -- Round outcome
    r.winning_team_name,
    r.end_reason,

    -- Calculated flags
    CASE WHEN r.winning_team_name = e.actor_team_name THEN 1 ELSE 0 END AS plant_converted,
    CASE WHEN r.end_reason = 'detonated' THEN 1 ELSE 0 END AS detonated,
    CASE WHEN r.end_reason = 'defused' THEN 1 ELSE 0 END AS defused,
    CASE WHEN r.end_reason = 'eliminated' AND r.winning_team_name = e.actor_team_name THEN 1 ELSE 0 END AS attacker_elim_win,
    CASE WHEN r.end_reason = 'eliminated' AND r.winning_team_name != e.actor_team_name THEN 1 ELSE 0 END AS defender_elim_win,

    -- Metadata
    CURRENT_TIMESTAMP AS calculated_at

FROM base_events e
JOIN rounds r ON e.round_id = r.round_id
WHERE e.is_plant = true
  AND e.round_id IN (SELECT round_id FROM new_rounds);

-- Cleanup
DROP TABLE IF EXISTS new_rounds;

-- Report summary
SELECT
    'agg_post_plant_stats' AS table_name,
    COUNT(*) AS total_rows,
    ROUND(AVG(plant_converted) * 100, 1) AS avg_post_plant_wr,
    SUM(detonated) AS total_detonations,
    SUM(defused) AS total_defuses
FROM agg_post_plant_stats;
