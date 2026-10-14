WITH per_game_comp AS (
    SELECT
        g.game_id,
        tgs.team_name,
        tgs.map_name,
        tgs.game_won,
        LIST(DISTINCT prs.agent_name) FILTER (WHERE prs.agent_name IS NOT NULL) AS agents
    FROM agg_team_game_stats tgs
    JOIN games g ON g.game_id = tgs.game_id
    JOIN rounds r ON r.game_id = g.game_id
    JOIN agg_player_round_stats prs
      ON prs.round_id = r.round_id
     AND prs.team_name = tgs.team_name
    WHERE g.series_id IN ({series_clause})
      AND tgs.team_name ILIKE ?
    GROUP BY g.game_id, tgs.team_name, tgs.map_name, tgs.game_won
)
SELECT
    map_name,
    agents,
    COUNT(*) AS times_played,
    SUM(CASE WHEN game_won THEN 1 ELSE 0 END) AS wins
FROM per_game_comp
GROUP BY map_name, agents
ORDER BY times_played DESC
