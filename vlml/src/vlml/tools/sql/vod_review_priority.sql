WITH round_data AS (
    SELECT
        g.game_number,
        r.map_name,
        r.round_number,
        r.winning_team_name,
        fb.team_name AS fb_team,
        fb.player_name AS fb_player,
        fd.player_name AS fd_player
    FROM rounds r
    JOIN games g ON g.game_id = r.game_id
    LEFT JOIN agg_player_round_stats fb
      ON fb.round_id = r.round_id AND fb.first_bloods = 1
    LEFT JOIN agg_player_round_stats fd
      ON fd.round_id = r.round_id AND fd.first_deaths = 1
    WHERE g.series_id = ?
      {map_filter}
)
SELECT
    game_number,
    map_name,
    round_number,
    'FB_CONVERSION_FAIL' AS reason,
    fb_team AS fb_team,
    fb_player AS fb_player,
    fd_player AS fd_player,
    winning_team_name
FROM round_data
WHERE fb_team IS NOT NULL AND fb_team <> winning_team_name
ORDER BY game_number, round_number
