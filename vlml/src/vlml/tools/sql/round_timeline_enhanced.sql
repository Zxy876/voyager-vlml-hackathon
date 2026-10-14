SELECT
    g.game_number,
    g.map_name,
    r.round_number,
    r.winning_team_name,
    r.end_reason,
    fb.player_name AS fb_player,
    fb.team_name AS fb_team,
    fd.player_name AS fd_player,
    fd.team_name AS fd_team,
    t.time_to_first_kill_s,
    t.time_to_plant_s,
    t.post_plant_duration_s
FROM rounds r
JOIN games g ON g.game_id = r.game_id
LEFT JOIN agg_player_round_stats fb ON fb.round_id = r.round_id AND fb.first_bloods = 1
LEFT JOIN agg_player_round_stats fd ON fd.round_id = r.round_id AND fd.first_deaths = 1
LEFT JOIN agg_team_round_stats t ON t.round_id = r.round_id AND t.team_name = r.winning_team_name
WHERE g.series_id = ?
  {map_filter}
ORDER BY g.game_number, r.round_number
