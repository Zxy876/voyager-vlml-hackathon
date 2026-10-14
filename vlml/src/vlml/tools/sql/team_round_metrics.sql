SELECT
    COUNT(*) AS rounds_played,
    SUM(CASE WHEN trs.round_won THEN 1 ELSE 0 END) AS rounds_won,
    SUM(trs.first_bloods) AS first_bloods,
    SUM(trs.first_deaths) AS first_deaths,
    SUM(trs.fb_converted_total) AS fb_converted_total,
    SUM(trs.fb_attempts_total) AS fb_attempts_total,
    SUM(CASE WHEN trs.first_deaths = 1 AND trs.round_won THEN 1 ELSE 0 END) AS fd_salvage_total,
    SUM(trs.first_deaths) AS fd_attempts_total
FROM agg_team_round_stats trs
JOIN rounds r ON r.round_id = trs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  AND trs.team_name ILIKE ?
  {map_filter}
