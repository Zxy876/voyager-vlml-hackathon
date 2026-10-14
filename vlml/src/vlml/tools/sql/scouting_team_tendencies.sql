SELECT
    SUM(CASE WHEN r.round_number <= 12 THEN 1 ELSE 0 END) AS first_half_rounds,
    SUM(CASE WHEN r.round_number <= 12 AND trs.round_won THEN 1 ELSE 0 END) AS first_half_wins,
    SUM(CASE WHEN r.round_number > 12 THEN 1 ELSE 0 END) AS second_half_rounds,
    SUM(CASE WHEN r.round_number > 12 AND trs.round_won THEN 1 ELSE 0 END) AS second_half_wins,
    SUM(CASE WHEN r.round_number IN (1, 13) THEN 1 ELSE 0 END) AS pistol_rounds,
    SUM(CASE WHEN r.round_number IN (1, 13) AND trs.round_won THEN 1 ELSE 0 END) AS pistol_wins,
    SUM(trs.first_bloods) AS fb,
    SUM(trs.first_deaths) AS fd,
    SUM(trs.deaths_traded_total) AS deaths_traded,
    SUM(trs.deaths_untraded_total) AS deaths_untraded
FROM agg_team_round_stats trs
JOIN rounds r ON r.round_id = trs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  AND trs.team_name ILIKE ?
