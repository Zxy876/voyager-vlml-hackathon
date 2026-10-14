SELECT
    trs.map_name,
    COUNT(*) AS rounds,
    SUM(CASE WHEN trs.round_won THEN 1 ELSE 0 END) AS rounds_won,
    SUM(trs.first_bloods) AS fb,
    AVG(trs.time_to_first_kill_s) AS avg_time_to_fk,
    SUM(trs.deaths_traded_total) AS deaths_traded,
    SUM(trs.deaths_untraded_total) AS deaths_untraded
FROM agg_team_round_stats trs
JOIN rounds r ON r.round_id = trs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  AND trs.team_name ILIKE ?
GROUP BY trs.map_name
ORDER BY rounds DESC
