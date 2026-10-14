SELECT series_id, MAX(game_started_at) AS last_game
FROM agg_team_game_stats
WHERE team_name ILIKE ?
GROUP BY series_id
ORDER BY last_game DESC NULLS LAST
LIMIT ?
