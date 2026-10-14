SELECT
    trs.round_id,
    trs.round_number,
    trs.map_name,
    trs.round_won
FROM agg_team_round_stats trs
JOIN rounds r ON r.round_id = trs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  AND trs.team_name ILIKE ?
  {map_filter}
ORDER BY trs.map_name, trs.round_number
