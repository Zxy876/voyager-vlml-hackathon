SELECT
    r.map_name,
    trs.team_name,
    SUM(CASE WHEN r.round_number <= 12 THEN 1 ELSE 0 END) AS first_half_rounds,
    SUM(CASE WHEN r.round_number <= 12 AND trs.round_won THEN 1 ELSE 0 END) AS first_half_wins,
    SUM(CASE WHEN r.round_number > 12 THEN 1 ELSE 0 END) AS second_half_rounds,
    SUM(CASE WHEN r.round_number > 12 AND trs.round_won THEN 1 ELSE 0 END) AS second_half_wins
FROM agg_team_round_stats trs
JOIN rounds r ON r.round_id = trs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id = ?
  {map_filter}
GROUP BY r.map_name, trs.team_name
ORDER BY r.map_name, trs.team_name
