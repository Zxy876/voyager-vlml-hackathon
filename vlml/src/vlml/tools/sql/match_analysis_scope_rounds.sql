SELECT COUNT(*) AS rounds
FROM agg_team_round_stats trs
JOIN rounds r ON r.round_id = trs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id = ?
  AND trs.team_name ILIKE ?
