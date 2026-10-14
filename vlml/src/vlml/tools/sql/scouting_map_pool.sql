SELECT
    tgs.map_name,
    COUNT(*) AS games,
    SUM(CASE WHEN tgs.game_won THEN 1 ELSE 0 END) AS wins
FROM agg_team_game_stats tgs
WHERE tgs.series_id IN ({series_clause})
  AND tgs.team_name ILIKE ?
  {map_filter}
GROUP BY tgs.map_name
ORDER BY games DESC
