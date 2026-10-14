-- Transformation: agg_player_win_shares
-- Pre-calculated win shares per player per game
-- Uses weights from ref_win_probability_factors

-- Win Share Weights (from VCT Americas 2025 analysis):
-- first_blood: 0.2303, survival: 0.5946, death_traded: 0.3380
-- multi_kill: 0.3123, trade_kill: 0.0957, plant: 0.1979, defuse: 0.5121

-- Step 1: Find games that need processing
CREATE TEMP TABLE new_games AS
SELECT DISTINCT r.game_id
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
LEFT JOIN agg_player_win_shares pws ON pws.game_id = r.game_id AND pws.player_id = prs.player_id
WHERE r.game_id IS NOT NULL
  AND (
      pws.game_id IS NULL
      OR prs.calculated_at > COALESCE(
          (SELECT MAX(calculated_at) FROM agg_player_win_shares),
          '1900-01-01'::TIMESTAMP
      )
  );

-- Step 2: Delete affected rows
DELETE FROM agg_player_win_shares
WHERE game_id IN (SELECT game_id FROM new_games);

-- Step 3: Insert new/updated rows
INSERT INTO agg_player_win_shares
SELECT
    -- Composite primary key
    prs.player_id,
    r.game_id,

    -- Context (use MAX for fields that should be consistent but may have NULLs)
    MAX(prs.player_name) AS player_name,
    MAX(prs.team_name) AS team_name,
    MAX(prs.opponent_team_name) AS opponent_team_name,
    MAX(prs.tournament_name) AS tournament_name,
    MAX(prs.tournament_year) AS tournament_year,
    MAX(prs.map_name) AS map_name,
    MAX(prs.agent_name) AS agent_name,
    MAX(prs.agent_role) AS agent_role,

    -- Raw counts
    COUNT(*) AS rounds_played,
    SUM(CASE WHEN prs.round_won THEN 1 ELSE 0 END) AS rounds_won,

    -- Win share components (raw)
    SUM(prs.first_bloods) AS first_bloods,
    SUM(prs.first_deaths) AS first_deaths,
    SUM(CASE WHEN prs.is_trade_kill THEN 1 ELSE 0 END) AS trade_kills,
    SUM(CASE WHEN prs.survived THEN 1 ELSE 0 END) AS survivals,
    SUM(CASE WHEN prs.is_traded THEN 1 ELSE 0 END) AS deaths_traded,
    SUM(CASE WHEN prs.multi_kill_count >= 2 THEN 1 ELSE 0 END) AS multi_kills,
    SUM(prs.plants) AS plants,
    SUM(prs.defuses) AS defuses,

    -- Win share components (weighted)
    ROUND(SUM(prs.first_bloods) * 0.2303, 4) AS fb_win_share,
    ROUND(SUM(CASE WHEN prs.is_trade_kill THEN 1 ELSE 0 END) * 0.0957, 4) AS trade_kill_share,
    ROUND(SUM(CASE WHEN prs.survived THEN 1 ELSE 0 END) * 0.5946, 4) AS survival_share,
    ROUND(SUM(CASE WHEN prs.is_traded THEN 1 ELSE 0 END) * 0.338, 4) AS traded_death_share,
    ROUND(SUM(CASE WHEN prs.multi_kill_count >= 2 THEN 1 ELSE 0 END) * 0.3123, 4) AS multikill_share,
    ROUND(SUM(prs.plants) * 0.1979, 4) AS plant_share,
    ROUND(SUM(prs.defuses) * 0.5121, 4) AS defuse_share,

    -- Total win share
    ROUND(
        SUM(prs.first_bloods) * 0.2303 +
        SUM(CASE WHEN prs.is_trade_kill THEN 1 ELSE 0 END) * 0.0957 +
        SUM(CASE WHEN prs.survived THEN 1 ELSE 0 END) * 0.5946 +
        SUM(CASE WHEN prs.is_traded THEN 1 ELSE 0 END) * 0.338 +
        SUM(CASE WHEN prs.multi_kill_count >= 2 THEN 1 ELSE 0 END) * 0.3123 +
        SUM(prs.plants) * 0.1979 +
        SUM(prs.defuses) * 0.5121
    , 4) AS total_win_share,

    -- Win share per round
    ROUND((
        SUM(prs.first_bloods) * 0.2303 +
        SUM(CASE WHEN prs.is_trade_kill THEN 1 ELSE 0 END) * 0.0957 +
        SUM(CASE WHEN prs.survived THEN 1 ELSE 0 END) * 0.5946 +
        SUM(CASE WHEN prs.is_traded THEN 1 ELSE 0 END) * 0.338 +
        SUM(CASE WHEN prs.multi_kill_count >= 2 THEN 1 ELSE 0 END) * 0.3123 +
        SUM(prs.plants) * 0.1979 +
        SUM(prs.defuses) * 0.5121
    ) / COUNT(*), 4) AS win_share_per_round,

    -- Efficiency metrics
    ROUND(SUM(prs.first_bloods) * 1.0 / NULLIF(SUM(prs.first_bloods) + SUM(prs.first_deaths), 0), 4) AS opening_duel_efficiency,
    ROUND(SUM(CASE WHEN prs.survived THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS survival_rate,
    ROUND(SUM(CASE WHEN prs.is_trade_kill THEN 1 ELSE 0 END) * 1.0 / NULLIF(SUM(CASE WHEN prs.deaths > 0 THEN 1 ELSE 0 END), 0), 4) AS trade_efficiency,

    -- Additional context
    ROUND(SUM(prs.damage_dealt) / COUNT(*), 1) AS adr,
    ROUND(SUM(prs.kills) * 1.0 / NULLIF(SUM(prs.deaths), 0), 2) AS kd_ratio,

    -- Metadata
    CURRENT_TIMESTAMP AS calculated_at

FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
WHERE r.game_id IN (SELECT game_id FROM new_games)
GROUP BY
    prs.player_id, r.game_id;

-- Cleanup
DROP TABLE IF EXISTS new_games;

-- Report summary
SELECT
    'agg_player_win_shares' AS table_name,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT player_name) AS unique_players,
    ROUND(AVG(win_share_per_round), 3) AS avg_ws_per_round,
    ROUND(AVG(survival_rate), 3) AS avg_survival_rate
FROM agg_player_win_shares;
