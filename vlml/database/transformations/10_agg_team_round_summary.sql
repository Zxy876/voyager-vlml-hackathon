-- Transformation: agg_team_round_summary
-- Aggregate player round stats to team level
-- Incremental: Only processes rounds with new data

-- Step 1: Find rounds that need processing
CREATE TEMP TABLE new_rounds AS
SELECT DISTINCT prs.round_id
FROM agg_player_round_stats prs
LEFT JOIN agg_team_round_summary trs ON trs.round_id = prs.round_id AND trs.team_name = prs.team_name
WHERE prs.round_id IS NOT NULL
  AND (
      trs.round_id IS NULL
      OR prs.calculated_at > COALESCE(
          (SELECT MAX(calculated_at) FROM agg_team_round_summary),
          '1900-01-01'::TIMESTAMP
      )
  );

-- Step 2: Delete affected rows
DELETE FROM agg_team_round_summary
WHERE round_id IN (SELECT round_id FROM new_rounds);

-- Step 3: Insert new/updated rows
INSERT INTO agg_team_round_summary
SELECT
    -- Composite primary key
    prs.round_id,
    prs.team_name,

    -- Context (use MAX for fields that should be consistent but may have NULLs)
    MAX(prs.opponent_team_name) AS opponent_team_name,
    r.game_id,
    MAX(prs.tournament_name) AS tournament_name,
    MAX(prs.tournament_year) AS tournament_year,
    MAX(prs.map_name) AS map_name,
    MAX(prs.round_number) AS round_number,
    MAX(prs.side) AS side,

    -- Outcome
    MAX(prs.round_won::int) AS round_won,

    -- Team aggregates
    SUM(prs.kills) AS team_kills,
    SUM(prs.deaths) AS team_deaths,
    SUM(prs.assists) AS team_assists,
    SUM(prs.damage_dealt) AS team_damage,
    ROUND(SUM(prs.damage_dealt) / 5, 1) AS team_adr,

    -- Opening duels
    SUM(prs.first_bloods) AS team_fb,
    SUM(prs.first_deaths) AS team_fd,
    SUM(prs.first_bloods) - SUM(prs.first_deaths) AS fb_differential,

    -- Survival & trading
    SUM(prs.survived::int) AS team_survivors,
    SUM(CASE WHEN prs.is_traded THEN 1 ELSE 0 END) AS team_deaths_traded,
    SUM(CASE WHEN prs.is_trade_kill THEN 1 ELSE 0 END) AS team_trade_kills,
    SUM(CASE WHEN prs.is_untraded_death THEN 1 ELSE 0 END) AS team_untraded_deaths,

    -- Multi-kills
    SUM(CASE WHEN prs.multi_kill_count >= 2 THEN 1 ELSE 0 END) AS team_multikills,
    SUM(CASE WHEN prs.is_ace THEN 1 ELSE 0 END) AS team_aces,

    -- Objectives
    SUM(prs.plants) AS team_plants,
    SUM(prs.defuses) AS team_defuses,

    -- Utility
    SUM(prs.abilities_used) AS team_abilities,
    SUM(CASE WHEN prs.early_util THEN 1 ELSE 0 END) AS team_early_util,

    -- Clutches
    SUM(CASE WHEN prs.is_clutch THEN 1 ELSE 0 END) AS team_clutch_situations,
    SUM(CASE WHEN prs.clutch_won THEN 1 ELSE 0 END) AS team_clutches_won,

    -- KAST
    SUM(CASE WHEN prs.kast THEN 1 ELSE 0 END) AS team_kast_count,

    -- Metadata
    CURRENT_TIMESTAMP AS calculated_at

FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
WHERE prs.round_id IN (SELECT round_id FROM new_rounds)
GROUP BY
    prs.round_id, prs.team_name, r.game_id;

-- Cleanup
DROP TABLE IF EXISTS new_rounds;

-- Report summary
SELECT
    'agg_team_round_summary' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT team_name) AS unique_teams,
    ROUND(AVG(round_won) * 100, 1) AS avg_win_rate
FROM agg_team_round_summary;
