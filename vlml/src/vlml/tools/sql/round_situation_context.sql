-- Round Situation Context: Rich per-round situation data for LLM reasoning
-- Provides context for "what if" analysis and retake/clutch scenarios
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
round_state AS (
    SELECT
        trs.round_id,
        g.game_id,
        g.game_number,
        g.map_name,
        trs.team_name,
        r.round_number,
        trs.side,
        trs.round_won,
        -- Score context
        trs.team_score_before,
        trs.enemy_score_before,
        CONCAT(trs.team_score_before, '-', trs.enemy_score_before) AS score_at_start,
        trs.is_match_point,
        trs.is_ot,
        -- Team state
        trs.players_alive_at_end,
        trs.team_kills AS team_kills,
        trs.team_deaths AS team_deaths,
        COALESCE(tl.team_loadout_value, 0) AS loadout_value,
        COALESCE(tl.team_net_worth, 0) AS net_worth,
        trs.team_damage_dealt,
        -- Economy classification using player stats
        CASE
            WHEN r.round_number IN (1, 13, 25) THEN 'pistol'
            WHEN tl.is_eco = 1 THEN 'eco'
            WHEN tl.is_force = 1 THEN 'force'
            WHEN tl.is_full_buy = 1 THEN 'full_buy'
            WHEN COALESCE(tl.team_loadout_value, 0) < 8000 THEN 'eco'
            WHEN COALESCE(tl.team_loadout_value, 0) < 20000 THEN 'force'
            ELSE 'full_buy'
        END AS buy_type,
        -- Utility usage
        trs.util_used_total,
        trs.flash_used_total,
        trs.smoke_used_total,
        trs.molly_used_total,
        -- Opening duel
        trs.entry_kill,
        trs.entry_death,
        trs.first_bloods,
        trs.first_deaths,
        -- Post-plant context
        trs.plants,
        trs.post_plant_kills_total,
        trs.post_plant_deaths_total,
        trs.retake_attempted_total,
        trs.retake_kills_total,
        -- Timing
        trs.time_to_first_kill_s,
        trs.time_to_first_death_s,
        trs.time_to_plant_s,
        trs.post_plant_duration_s,
        -- Outcome
        r.end_reason,
        r.winning_team_name,
        -- Opponent state (joined)
        COALESCE(opp_tl.team_loadout_value, 0) AS opp_loadout_value,
        opp.players_alive_at_end AS opp_alive_at_end,
        opp.util_used_total AS opp_util_used,
        opp.team_kills AS opp_kills,
        opp.team_deaths AS opp_deaths,
        CASE
            WHEN opp_tl.is_eco = 1 THEN 'eco'
            WHEN opp_tl.is_force = 1 THEN 'force'
            WHEN opp_tl.is_full_buy = 1 THEN 'full_buy'
            WHEN COALESCE(opp_tl.team_loadout_value, 0) < 8000 THEN 'eco'
            WHEN COALESCE(opp_tl.team_loadout_value, 0) < 20000 THEN 'force'
            ELSE 'full_buy'
        END AS opp_buy_type,
        -- Trade analysis
        trs.deaths_traded_total,
        trs.deaths_untraded_total,
        -- Streak context
        CASE
            WHEN trs.current_win_streak > 0 THEN trs.current_win_streak
            ELSE -COALESCE(trs.current_loss_streak, 0)
        END AS streak
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
),
player_weapons AS (
    -- Aggregate weapon info per round per team
    SELECT
        prs.round_id,
        prs.team_name,
        COUNT(DISTINCT prs.player_name) AS players_in_round,
        COUNT(DISTINCT CASE WHEN prs.weapon_type = 'rifle' THEN prs.player_name END) AS players_with_rifle,
        COUNT(DISTINCT CASE WHEN prs.weapon_type = 'sniper' THEN prs.player_name END) AS players_with_sniper,
        COUNT(DISTINCT CASE WHEN prs.weapon_type = 'smg' THEN prs.player_name END) AS players_with_smg,
        COUNT(DISTINCT CASE WHEN prs.weapon_type = 'shotgun' THEN prs.player_name END) AS players_with_shotgun,
        COUNT(DISTINCT CASE WHEN prs.weapon_type = 'pistol' THEN prs.player_name END) AS players_with_pistol
    FROM agg_player_round_stats prs
    JOIN rounds r ON r.round_id = prs.round_id
    JOIN games g ON g.game_id = r.game_id
    WHERE g.series_id IN ({series_clause})
      AND prs.team_name ILIKE ?
      {map_filter}
    GROUP BY prs.round_id, prs.team_name
),
next_round_economy AS (
    -- Get next round buying power from player stats
    SELECT
        prs.round_id,
        prs.team_name,
        r.round_number,
        g.game_id,
        SUM(prs.loadout_value) AS team_loadout_value,
        MAX(CASE WHEN prs.is_eco_round THEN 1 ELSE 0 END) AS is_eco,
        MAX(CASE WHEN prs.is_force_buy THEN 1 ELSE 0 END) AS is_force,
        MAX(CASE WHEN prs.is_full_buy THEN 1 ELSE 0 END) AS is_full_buy
    FROM agg_player_round_stats prs
    JOIN rounds r ON r.round_id = prs.round_id
    JOIN games g ON g.game_id = r.game_id
    WHERE g.series_id IN ({series_clause})
      AND prs.team_name ILIKE ?
      {map_filter}
    GROUP BY prs.round_id, prs.team_name, r.round_number, g.game_id
),
next_round_context AS (
    -- Get next round buying power using LEAD
    SELECT
        nre.round_id,
        nre.team_name,
        LEAD(nre.team_loadout_value) OVER (
            PARTITION BY nre.game_id, nre.team_name
            ORDER BY nre.round_number
        ) AS next_round_loadout,
        LEAD(CASE
            WHEN nre.is_eco = 1 THEN 'eco'
            WHEN nre.is_force = 1 THEN 'force'
            WHEN nre.is_full_buy = 1 THEN 'full_buy'
            WHEN COALESCE(nre.team_loadout_value, 0) < 8000 THEN 'eco'
            WHEN COALESCE(nre.team_loadout_value, 0) < 20000 THEN 'force'
            ELSE 'full_buy'
        END) OVER (
            PARTITION BY nre.game_id, nre.team_name
            ORDER BY nre.round_number
        ) AS next_round_buy_type
    FROM next_round_economy nre
)
SELECT
    rs.round_id,
    rs.game_id,
    rs.game_number,
    rs.map_name,
    rs.team_name,
    rs.round_number,
    rs.side,
    rs.round_won,
    rs.score_at_start,
    rs.team_score_before,
    rs.enemy_score_before,
    rs.is_match_point,
    rs.is_ot,
    -- Situation
    rs.players_alive_at_end,
    rs.opp_alive_at_end,
    rs.loadout_value,
    rs.opp_loadout_value,
    rs.buy_type,
    rs.opp_buy_type,
    -- Weapons
    pw.players_with_rifle,
    pw.players_with_sniper,
    pw.players_with_smg,
    -- Utility
    rs.util_used_total,
    rs.opp_util_used,
    rs.flash_used_total,
    rs.smoke_used_total,
    -- Opening duel
    rs.entry_kill,
    rs.entry_death,
    rs.first_bloods,
    rs.first_deaths,
    -- Post-plant
    rs.plants,
    rs.retake_attempted_total,
    rs.retake_kills_total,
    rs.post_plant_kills_total,
    rs.post_plant_deaths_total,
    -- Timing
    rs.time_to_first_kill_s,
    rs.time_to_plant_s,
    rs.post_plant_duration_s,
    -- Outcome
    rs.end_reason,
    rs.winning_team_name,
    -- Trades
    rs.deaths_traded_total,
    rs.deaths_untraded_total,
    rs.streak,
    -- Combat
    rs.team_kills,
    rs.team_deaths,
    rs.opp_kills,
    rs.opp_deaths,
    -- Next round projection
    nrc.next_round_loadout,
    nrc.next_round_buy_type
FROM round_state rs
LEFT JOIN player_weapons pw
    ON pw.round_id = rs.round_id
    AND pw.team_name = rs.team_name
LEFT JOIN next_round_context nrc
    ON nrc.round_id = rs.round_id
    AND nrc.team_name = rs.team_name
ORDER BY rs.game_number, rs.round_number
