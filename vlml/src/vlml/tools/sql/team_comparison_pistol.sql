SELECT
    trs.team_name,
    SUM(CASE WHEN r.round_number IN (1, 13) AND trs.round_won THEN 1 ELSE 0 END) AS pistol_wins,
    SUM(CASE WHEN r.round_number IN (1, 13) THEN 1 ELSE 0 END) AS pistol_rounds,
    SUM(CASE WHEN r.round_number IN (2, 14) AND trs.round_won THEN 1 ELSE 0 END) AS post_pistol_wins,
    SUM(CASE WHEN r.round_number IN (2, 14) THEN 1 ELSE 0 END) AS post_pistol_rounds
FROM agg_team_round_stats trs
JOIN rounds r ON r.round_id = trs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id = ?
  {map_filter}
GROUP BY trs.team_name
