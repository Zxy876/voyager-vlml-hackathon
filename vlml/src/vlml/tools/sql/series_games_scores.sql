SELECT game_id, team_name, rounds_won
FROM agg_team_game_stats
WHERE series_id = ?
