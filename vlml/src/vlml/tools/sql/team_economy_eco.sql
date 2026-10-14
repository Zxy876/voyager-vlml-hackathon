SELECT
    SUM(CASE WHEN is_eco_round = 1 THEN 1 ELSE 0 END) AS eco_rounds,
    SUM(CASE WHEN is_eco_round = 1 AND round_won = 1 THEN 1 ELSE 0 END) AS eco_wins,
    SUM(CASE WHEN is_thrifty = 1 THEN 1 ELSE 0 END) AS thrifty_wins
FROM (
    SELECT
        prs.round_id,
        prs.team_name,
        MAX(CASE WHEN prs.is_eco_round THEN 1 ELSE 0 END) AS is_eco_round,
        MAX(CASE WHEN prs.is_thrifty THEN 1 ELSE 0 END) AS is_thrifty,
        MAX(CASE WHEN prs.round_won THEN 1 ELSE 0 END) AS round_won
    FROM agg_player_round_stats prs
    JOIN rounds r ON r.round_id = prs.round_id
    JOIN games g ON g.game_id = r.game_id
    WHERE g.series_id IN ({series_clause})
      AND prs.team_name ILIKE ?
      {map_filter}
    GROUP BY prs.round_id, prs.team_name
) rounds
