SELECT
    COUNT(DISTINCT tgs.series_id) AS series_count,
    COUNT(DISTINCT tgs.game_id) AS game_count,
    SUM(tgs.rounds_won + tgs.rounds_lost) AS rounds_count
FROM agg_team_game_stats tgs
WHERE tgs.series_id IN ({series_clause})
  AND tgs.team_name ILIKE ?
  {map_filter}
