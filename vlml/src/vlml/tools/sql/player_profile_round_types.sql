SELECT
    CASE
        WHEN r.round_number IN (1, 13) THEN 'pistol'
        WHEN r.round_number IN (2, 14) THEN 'post_pistol'
        ELSE 'gun'
    END AS round_type,
    COUNT(*) AS rounds,
    SUM(prs.kills) AS kills,
    SUM(prs.deaths) AS deaths,
    SUM(prs.damage_dealt) AS damage_dealt,
    SUM(prs.first_bloods) AS fb,
    SUM(CASE WHEN prs.round_won THEN 1 ELSE 0 END) AS rounds_won
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id IN ({series_clause})
  AND prs.player_name ILIKE ?
GROUP BY round_type
