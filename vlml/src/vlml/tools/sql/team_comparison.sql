SELECT
    trs.team_name,
    COUNT(*) AS rounds,
    SUM(CASE WHEN trs.round_won THEN 1 ELSE 0 END) AS rounds_won,
    SUM(trs.first_bloods) AS fb,
    SUM(trs.first_deaths) AS fd,
    SUM(trs.fb_converted_total) AS fb_converted,
    SUM(trs.fb_attempts_total) AS fb_attempts,
    SUM(CASE WHEN trs.first_deaths = 1 AND trs.round_won THEN 1 ELSE 0 END) AS fd_salvaged,
    SUM(trs.first_deaths) AS fd_attempts,
    SUM(trs.team_kills) AS total_kills,
    SUM(trs.team_deaths) AS total_deaths,
    SUM(trs.team_assists) AS total_assists,
    SUM(trs.team_damage_dealt) AS team_damage_sum,
    SUM(trs.deaths_traded_total) AS deaths_traded,
    SUM(trs.deaths_untraded_total) AS deaths_untraded,
    AVG(trs.time_to_first_kill_s) AS avg_time_to_fk,
    AVG(trs.time_to_plant_s) AS avg_time_to_plant,
    AVG(trs.post_plant_duration_s) AS avg_post_plant_duration,
    SUM(trs.post_plant_kills_total) AS post_plant_kills,
    SUM(trs.post_plant_deaths_total) AS post_plant_deaths
FROM agg_team_round_stats trs
JOIN rounds r ON r.round_id = trs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id = ?
  {map_filter}
GROUP BY trs.team_name
