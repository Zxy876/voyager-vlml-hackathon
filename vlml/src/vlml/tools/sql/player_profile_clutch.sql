SELECT
    SUM(CASE WHEN prs.is_clutch THEN 1 ELSE 0 END) AS clutch_attempts,
    SUM(CASE WHEN prs.clutch_won THEN 1 ELSE 0 END) AS clutch_wins,
    SUM(CASE WHEN prs.is_1v1 THEN 1 ELSE 0 END) AS attempts_1v1,
    SUM(CASE WHEN prs.is_1v1 AND prs.clutch_won THEN 1 ELSE 0 END) AS wins_1v1,
    SUM(CASE WHEN prs.is_1v2 THEN 1 ELSE 0 END) AS attempts_1v2,
    SUM(CASE WHEN prs.is_1v2 AND prs.clutch_won THEN 1 ELSE 0 END) AS wins_1v2,
    SUM(CASE WHEN prs.is_1v3 THEN 1 ELSE 0 END) AS attempts_1v3,
    SUM(CASE WHEN prs.is_1v3 AND prs.clutch_won THEN 1 ELSE 0 END) AS wins_1v3,
    SUM(CASE WHEN prs.is_1v4 THEN 1 ELSE 0 END) AS attempts_1v4,
    SUM(CASE WHEN prs.is_1v4 AND prs.clutch_won THEN 1 ELSE 0 END) AS wins_1v4,
    SUM(CASE WHEN prs.is_1v5 THEN 1 ELSE 0 END) AS attempts_1v5,
    SUM(CASE WHEN prs.is_1v5 AND prs.clutch_won THEN 1 ELSE 0 END) AS wins_1v5
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  AND prs.player_name ILIKE ?
