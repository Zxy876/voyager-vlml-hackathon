SELECT
    SUM(pistol_rounds_won) AS pistol_wins,
    SUM(pistol_rounds_played) AS pistol_rounds
FROM agg_team_game_stats tgs
WHERE tgs.series_id IN ({series_clause})
  AND tgs.team_name ILIKE ?
  {map_filter}
