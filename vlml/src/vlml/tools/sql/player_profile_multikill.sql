SELECT
    SUM(CASE WHEN prs.is_double_kill THEN 1 ELSE 0 END) AS double_kills,
    SUM(CASE WHEN prs.is_triple_kill THEN 1 ELSE 0 END) AS triple_kills,
    SUM(CASE WHEN prs.is_quad_kill THEN 1 ELSE 0 END) AS quad_kills,
    SUM(CASE WHEN prs.is_ace THEN 1 ELSE 0 END) AS aces
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  AND prs.player_name ILIKE ?
