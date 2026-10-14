SELECT
    g.series_id,
    MAX(r.started_at) AS last_game,
    MAX(prs.opponent_team_name) AS opponent_team,
    MAX(CASE WHEN s.winning_team_name = prs.team_name THEN 'W' ELSE 'L' END) AS result,
    SUM(prs.kills) AS kills,
    SUM(prs.deaths) AS deaths,
    SUM(prs.damage_dealt) AS damage_dealt,
    SUM(prs.first_bloods) AS fb,
    SUM(prs.first_deaths) AS fd,
    COUNT(*) AS rounds
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
JOIN series s ON s.series_id = g.series_id
WHERE g.series_id IN ({series_clause})
  AND prs.player_name ILIKE ?
GROUP BY g.series_id
ORDER BY last_game DESC NULLS LAST
LIMIT 5
