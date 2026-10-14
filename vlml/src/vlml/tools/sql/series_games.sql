SELECT
    g.game_id,
    g.game_number,
    g.map_name,
    g.winning_team_name,
    g.total_rounds
FROM games g
WHERE g.series_id = ?
ORDER BY g.game_number
