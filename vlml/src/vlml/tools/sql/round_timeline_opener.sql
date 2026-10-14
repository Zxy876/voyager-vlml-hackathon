SELECT
    prs.round_id,
    MAX(CASE WHEN prs.first_bloods = 1 THEN prs.player_name END) AS opener,
    MAX(CASE WHEN prs.first_bloods = 1 THEN prs.team_name END) AS opener_team,
    MAX(CASE WHEN prs.first_deaths = 1 THEN prs.player_name END) AS victim,
    MAX(CASE WHEN prs.first_deaths = 1 THEN prs.team_name END) AS victim_team,
    MAX(prs.multi_kill_count) AS max_multi_kill,
    MAX(CASE WHEN prs.is_ace THEN 1 ELSE 0 END) AS ace_flag,
    MAX(CASE WHEN prs.is_clutch AND prs.clutch_won THEN 1 ELSE 0 END) AS clutch_win
FROM agg_player_round_stats prs
WHERE prs.round_id IN ({round_ids_clause})
GROUP BY prs.round_id
