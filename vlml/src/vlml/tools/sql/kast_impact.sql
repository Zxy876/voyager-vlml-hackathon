SELECT
    prs.player_name,
    prs.team_name,
    SUM(CASE WHEN NOT (prs.kills > 0 OR prs.assists > 0 OR prs.deaths = 0 OR prs.is_traded) AND prs.deaths > 0 THEN 1 ELSE 0 END) AS deaths_no_kast,
    SUM(CASE WHEN NOT (prs.kills > 0 OR prs.assists > 0 OR prs.deaths = 0 OR prs.is_traded) AND prs.deaths > 0 AND prs.round_won = FALSE THEN 1 ELSE 0 END) AS lost_when_no_kast
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  {player_filter}
GROUP BY prs.player_name, prs.team_name
HAVING SUM(CASE WHEN NOT (prs.kills > 0 OR prs.assists > 0 OR prs.deaths = 0 OR prs.is_traded) AND prs.deaths > 0 THEN 1 ELSE 0 END) >= ?
ORDER BY lost_when_no_kast DESC
