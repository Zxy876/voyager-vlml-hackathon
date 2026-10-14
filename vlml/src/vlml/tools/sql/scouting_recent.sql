WITH series_teams AS (
    SELECT series_id, team_name
    FROM agg_team_game_stats
    WHERE series_id IN ({series_clause})
    GROUP BY series_id, team_name
),
team_series AS (
    SELECT series_id
    FROM series_teams
    WHERE team_name ILIKE ?
)
SELECT
    s.series_id,
    s.start_time,
    s.tournament_name,
    MAX(CASE WHEN st.team_name NOT ILIKE ? THEN st.team_name END) AS opponent,
    CASE WHEN s.winning_team_name ILIKE ? THEN 'W' ELSE 'L' END AS result
FROM series s
JOIN team_series ts ON ts.series_id = s.series_id
JOIN series_teams st ON st.series_id = s.series_id
GROUP BY s.series_id, s.start_time, s.tournament_name, s.winning_team_name
ORDER BY s.start_time DESC NULLS LAST
LIMIT 5
