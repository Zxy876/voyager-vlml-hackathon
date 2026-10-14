SELECT DISTINCT team_name
FROM agg_team_game_stats
WHERE series_id = ?
ORDER BY team_name
