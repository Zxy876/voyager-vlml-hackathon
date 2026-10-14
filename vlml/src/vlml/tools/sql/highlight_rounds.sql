SELECT
    g.game_number,
    r.map_name,
    r.round_number,
    prs.player_name,
    prs.team_name,
    prs.kills,
    CASE
        WHEN prs.is_ace THEN 'ACE'
        WHEN prs.is_quad_kill THEN '4K'
        WHEN prs.is_triple_kill THEN '3K'
        ELSE NULL
    END AS multikill_type,
    prs.clutch_won,
    prs.clutch_opponents
FROM agg_player_round_stats prs
JOIN rounds r ON r.round_id = prs.round_id
JOIN games g ON g.game_id = r.game_id
WHERE g.series_id = ?
  AND (prs.is_triple_kill OR prs.is_quad_kill OR prs.is_ace OR prs.clutch_won)
  {map_filter}
ORDER BY g.game_number, r.round_number
