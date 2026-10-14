SELECT
    prs.player_name,
    prs.agent_name,
    COUNT(*) AS rounds,
    SUM(prs.kills) AS kills,
    SUM(prs.deaths) AS deaths,
    SUM(prs.damage_dealt) AS damage_dealt
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  AND prs.team_name ILIKE ?
GROUP BY prs.player_name, prs.agent_name
ORDER BY rounds DESC
