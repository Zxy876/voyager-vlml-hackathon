SELECT
    COUNT(*) AS rounds_played,
    SUM(CASE WHEN prs.round_won THEN 1 ELSE 0 END) AS rounds_won,
    SUM(prs.first_bloods) AS first_bloods,
    SUM(prs.first_deaths) AS first_deaths,
    SUM(CASE WHEN prs.is_clutch THEN 1 ELSE 0 END) AS clutch_attempts,
    SUM(CASE WHEN prs.clutch_won THEN 1 ELSE 0 END) AS clutch_wins,
    SUM(CASE WHEN prs.is_double_kill THEN 1 ELSE 0 END) AS double_kills,
    SUM(CASE WHEN prs.is_triple_kill THEN 1 ELSE 0 END) AS triple_kills,
    SUM(CASE WHEN prs.is_quad_kill THEN 1 ELSE 0 END) AS quad_kills,
    SUM(CASE WHEN prs.is_ace THEN 1 ELSE 0 END) AS aces,
    SUM(prs.kills) AS kills,
    SUM(prs.deaths) AS deaths,
    SUM(CASE WHEN (prs.kills > 0 OR prs.assists > 0 OR prs.deaths = 0 OR prs.is_traded) THEN 1 ELSE 0 END) AS kast_num,
    COUNT(*) AS kast_denom,
    SUM(prs.damage_dealt) AS adr_num,
    COUNT(*) AS adr_denom
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  AND prs.player_name ILIKE ?
  {map_filter}
  {agent_filter}
