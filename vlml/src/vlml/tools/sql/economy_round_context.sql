-- Economy Round Context: Round-by-round economy progression for LLM analysis
-- Provides chain context for identifying economy cascade patterns
-- Usage: Filtered by series/game_id and team_name (ILIKE)

WITH team_loadouts AS (
    -- Aggregate loadout values from player stats to team level
    SELECT
        prs.round_id,
        prs.team_name,
        SUM(prs.loadout_value) AS team_loadout_value,
        SUM(prs.net_worth) AS team_net_worth,
        MAX(CASE WHEN prs.is_eco_round THEN 1 ELSE 0 END) AS is_eco,
        MAX(CASE WHEN prs.is_force_buy THEN 1 ELSE 0 END) AS is_force,
        MAX(CASE WHEN prs.is_full_buy THEN 1 ELSE 0 END) AS is_full_buy
    FROM agg_player_round_stats prs
    JOIN rounds r ON r.round_id = prs.round_id
    JOIN games g ON g.game_id = r.game_id
    WHERE g.series_id IN ({series_clause})
    GROUP BY prs.round_id, prs.team_name
),
round_economy AS (
    SELECT
        trs.round_id,
        trs.team_name,
        g.game_id,
        g.game_number,
        g.map_name,
        trs.round_number,
        trs.side,
        trs.round_won,
        COALESCE(tl.team_loadout_value, 0) AS loadout_value,
        COALESCE(tl.team_net_worth, 0) AS net_worth,
        -- Classify buy type using player stats flags or loadout
        CASE
            WHEN trs.round_number IN (1, 13, 25) THEN 'pistol'
            WHEN tl.is_eco = 1 THEN 'eco'
            WHEN tl.is_force = 1 THEN 'force'
            WHEN tl.is_full_buy = 1 THEN 'full_buy'
            WHEN COALESCE(tl.team_loadout_value, 0) < 8000 THEN 'eco'
            WHEN COALESCE(tl.team_loadout_value, 0) < 20000 THEN 'force'
            ELSE 'full_buy'
        END AS buy_type,
        -- Previous round context (LAG)
        LAG(trs.round_number) OVER w AS prev_round_number,
        LAG(trs.round_won) OVER w AS prev_round_won,
        LAG(COALESCE(tl.team_loadout_value, 0)) OVER w AS prev_loadout_value,
        LAG(CASE
            WHEN trs.round_number IN (1, 13, 25) THEN 'pistol'
            WHEN tl.is_eco = 1 THEN 'eco'
            WHEN tl.is_force = 1 THEN 'force'
            WHEN tl.is_full_buy = 1 THEN 'full_buy'
            WHEN COALESCE(tl.team_loadout_value, 0) < 8000 THEN 'eco'
            WHEN COALESCE(tl.team_loadout_value, 0) < 20000 THEN 'force'
            ELSE 'full_buy'
        END) OVER w AS prev_buy_type,
        -- Win/loss streak (positive = wins, negative = losses)
        CASE
            WHEN trs.current_win_streak > 0 THEN trs.current_win_streak
            ELSE -COALESCE(trs.current_loss_streak, 0)
        END AS streak,
        -- Recent momentum (last 4 rounds)
        SUM(CASE WHEN trs.round_won THEN 1 ELSE -1 END) OVER (
            PARTITION BY g.game_id, trs.team_name
            ORDER BY trs.round_number
            ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
        ) AS recent_momentum,
        -- Opponent economy context
        COALESCE(opp_tl.team_loadout_value, 0) AS opp_loadout_value,
        CASE
            WHEN opp.round_number IN (1, 13, 25) THEN 'pistol'
            WHEN opp_tl.is_eco = 1 THEN 'eco'
            WHEN opp_tl.is_force = 1 THEN 'force'
            WHEN opp_tl.is_full_buy = 1 THEN 'full_buy'
            WHEN COALESCE(opp_tl.team_loadout_value, 0) < 8000 THEN 'eco'
            WHEN COALESCE(opp_tl.team_loadout_value, 0) < 20000 THEN 'force'
            ELSE 'full_buy'
        END AS opp_buy_type,
        -- Economy differential
        COALESCE(tl.team_loadout_value, 0) - COALESCE(opp_tl.team_loadout_value, 0) AS loadout_diff,
        -- End reason for context
        r.end_reason
    FROM agg_team_round_stats trs
    JOIN rounds r ON r.round_id = trs.round_id
    JOIN games g ON g.game_id = r.game_id
    JOIN agg_team_round_stats opp
        ON opp.round_id = trs.round_id
        AND opp.team_name != trs.team_name
    LEFT JOIN team_loadouts tl
        ON tl.round_id = trs.round_id
        AND tl.team_name = trs.team_name
    LEFT JOIN team_loadouts opp_tl
        ON opp_tl.round_id = trs.round_id
        AND opp_tl.team_name = opp.team_name
    WHERE g.series_id IN ({series_clause})
      AND trs.team_name ILIKE ?
      {map_filter}
    WINDOW w AS (PARTITION BY g.game_id, trs.team_name ORDER BY trs.round_number)
)
SELECT
    round_id,
    team_name,
    game_id,
    game_number,
    map_name,
    round_number,
    side,
    round_won,
    buy_type,
    loadout_value,
    net_worth,
    prev_round_number,
    prev_round_won,
    prev_loadout_value,
    prev_buy_type,
    streak,
    recent_momentum,
    opp_loadout_value,
    opp_buy_type,
    loadout_diff,
    end_reason
FROM round_economy
ORDER BY game_number, round_number
