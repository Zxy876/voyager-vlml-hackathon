SELECT g.series_id, MAX(pgs.game_started_at) AS last_game
FROM agg_player_game_stats pgs
JOIN games g ON g.game_id = pgs.game_id
WHERE pgs.player_name ILIKE ?
  AND g.series_id IS NOT NULL
GROUP BY g.series_id
ORDER BY last_game DESC NULLS LAST
LIMIT ?
