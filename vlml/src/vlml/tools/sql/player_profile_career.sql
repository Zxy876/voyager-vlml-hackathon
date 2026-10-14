SELECT
    SUM(prs.kills) AS kills,
    SUM(prs.deaths) AS deaths,
    SUM(prs.assists) AS assists,
    SUM(prs.damage_dealt) AS damage_dealt,
    SUM(CASE WHEN (prs.kills > 0 OR prs.assists > 0 OR prs.deaths = 0 OR prs.is_traded) THEN 1 ELSE 0 END) AS kast_num,
    COUNT(*) AS kast_denom,
    SUM(prs.first_bloods) AS fb,
    SUM(prs.first_deaths) AS fd,
    SUM(CASE WHEN prs.round_won THEN 1 ELSE 0 END) AS rounds_won,
    COUNT(*) AS rounds_played
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  AND prs.player_name ILIKE ?
  {map_filter}
  {agent_filter}
