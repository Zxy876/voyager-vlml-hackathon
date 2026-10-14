SELECT
    series_id,
    tournament_name,
    start_time,
    team1_name,
    team2_name,
    winning_team_name
FROM series
WHERE series_id = ?
