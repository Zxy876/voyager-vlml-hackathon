SELECT
    MAX(prs.player_name) AS player_name,
    MAX(prs.team_name) AS team_name,
    MIN(r.started_at) AS first_game,
    MAX(r.started_at) AS last_game,
    COUNT(DISTINCT g.series_id) AS total_series,
    COUNT(DISTINCT g.game_id) AS total_games,
    COUNT(*) AS total_rounds
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  AND prs.player_name ILIKE ?
  {map_filter}
  {agent_filter}
