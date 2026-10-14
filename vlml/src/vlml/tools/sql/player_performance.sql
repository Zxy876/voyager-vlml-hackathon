SELECT
    prs.player_name,
    prs.team_name,
    prs.agent_name,
    COUNT(*) AS rounds,
    SUM(prs.kills) AS kills,
    SUM(prs.deaths) AS deaths,
    SUM(prs.assists) AS assists,
    SUM(prs.first_bloods) AS fb,
    SUM(prs.first_deaths) AS fd,
    SUM(CASE WHEN prs.is_opening_kill THEN 1 ELSE 0 END) AS opening_kills,
    SUM(CASE WHEN prs.is_opening_death THEN 1 ELSE 0 END) AS opening_deaths,
    SUM(prs.damage_dealt) AS damage_dealt,
    SUM(prs.headshot_hits_total) AS headshot_hits_total,
    SUM(prs.hits_total) AS hits_total,
    SUM(CASE WHEN (prs.kills > 0 OR prs.assists > 0 OR prs.deaths = 0 OR prs.is_traded) THEN 1 ELSE 0 END) AS kast_num,
    COUNT(*) AS kast_denom,
    SUM(CASE WHEN prs.is_clutch THEN 1 ELSE 0 END) AS clutch_attempts,
    SUM(CASE WHEN prs.clutch_won THEN 1 ELSE 0 END) AS clutches_won,
    SUM(CASE WHEN prs.is_double_kill THEN 1 ELSE 0 END) AS double_kills,
    SUM(CASE WHEN prs.is_triple_kill THEN 1 ELSE 0 END) AS triple_kills,
    SUM(CASE WHEN prs.is_quad_kill THEN 1 ELSE 0 END) AS quad_kills,
    SUM(CASE WHEN prs.is_ace THEN 1 ELSE 0 END) AS aces
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id = ?
  {map_filter}
  {team_filter}
GROUP BY prs.player_name, prs.team_name, prs.agent_name
ORDER BY prs.team_name, kills DESC
