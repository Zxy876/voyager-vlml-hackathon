SELECT
    prs.player_name,
    prs.team_name,
    SUM(CASE WHEN prs.is_opening_death THEN 1 ELSE 0 END) AS opening_deaths,
    SUM(CASE WHEN prs.is_opening_death AND prs.round_won THEN 1 ELSE 0 END) AS team_won_after_od,
    SUM(CASE WHEN prs.is_opening_death AND prs.round_won = FALSE THEN 1 ELSE 0 END) AS team_lost_after_od,
    LIST(r.round_number) FILTER (WHERE prs.is_opening_death) AS rounds_list
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  {player_filter}
GROUP BY prs.player_name, prs.team_name
HAVING SUM(CASE WHEN prs.is_opening_death THEN 1 ELSE 0 END) >= ?
ORDER BY opening_deaths DESC
