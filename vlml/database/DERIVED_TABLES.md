# Derived Aggregate Tables

Layer 4 tables that pre-compute expensive joins and aggregations for fast analytics queries.

## Overview

These tables reduce typical analysis from 40+ queries to 10-15 by pre-joining events with outcomes and pre-aggregating player stats to team level.

| Table | Grain | Source | Purpose |
|-------|-------|--------|---------|
| `ref_win_probability_factors` | factor | seed | Win Share weights |
| `agg_first_blood_stats` | round | base_events | FB events + outcomes |
| `agg_post_plant_stats` | round | base_events | Plant events + outcomes |
| `agg_team_round_summary` | round × team | agg_player_round_stats | Team-level aggregates |
| `agg_team_map_stats` | team × map | agg_player_game_stats | Map performance |
| `agg_team_series_stats` | series × team | series, games | Head-to-head records |
| `agg_player_win_shares` | game × player | agg_player_round_stats | Win Share metrics |

## Table Details

### ref_win_probability_factors

Reference table with win probability weights derived from VCT Americas 2025 analysis.

**Columns:**
- `factor_name` (PK) - Event name (first_blood, survival, etc.)
- `factor_type` - Event or state
- `win_rate_with` - Win rate when factor present
- `win_rate_without` - Win rate when factor absent
- `probability_lift` - Difference (weight derivation)
- `weight` - Win Share weight
- `sample_size` - Observations in analysis

**Row count:** 9

---

### agg_first_blood_stats

Pre-joined first blood events with round outcomes.

**Primary Key:** `round_id`

**Key Columns:**
- `fb_team`, `fd_team` - Teams involved
- `fb_player`, `fb_agent` - First blood details
- `fd_player`, `fd_agent` - First death details
- `fb_side` - Attacker/defender
- `winning_team_name`, `end_reason` - Round outcome
- `fb_team_won` - Conversion flag (1/0)

**Row count:** ~5,500

**Example Query - FB Conversion by Team:**
```sql
SELECT
    fb_team,
    COUNT(*) AS first_bloods,
    SUM(fb_team_won) AS converted,
    ROUND(SUM(fb_team_won) * 100.0 / COUNT(*), 1) AS conversion_rate
FROM agg_first_blood_stats
GROUP BY fb_team
ORDER BY conversion_rate DESC;
```

---

### agg_post_plant_stats

Pre-joined plant events with round outcomes.

**Primary Key:** `round_id`

**Key Columns:**
- `planting_team`, `defending_team` - Teams involved
- `planter`, `planter_agent` - Plant details
- `winning_team_name`, `end_reason` - Round outcome
- `plant_converted` - Attacker win flag
- `detonated`, `defused` - End reason flags

**Row count:** ~3,800

**Example Query - Post-Plant Win Rate:**
```sql
SELECT
    planting_team,
    COUNT(*) AS plants,
    SUM(plant_converted) AS wins,
    ROUND(SUM(plant_converted) * 100.0 / COUNT(*), 1) AS post_plant_wr
FROM agg_post_plant_stats
GROUP BY planting_team
ORDER BY post_plant_wr DESC;
```

---

### agg_team_round_summary

Team-level aggregation of player round stats.

**Primary Key:** `(round_id, team_name)`

**Key Columns:**
- `round_won` - Outcome flag
- `team_kills`, `team_deaths`, `team_damage`, `team_adr`
- `team_fb`, `team_fd`, `fb_differential` - Opening duels
- `team_survivors`, `team_deaths_traded`, `team_trade_kills` - Trading
- `team_multikills`, `team_aces` - Multi-kills
- `team_plants`, `team_defuses` - Objectives
- `team_clutch_situations`, `team_clutches_won` - Clutches

**Row count:** ~11,000

**Example Query - Team Round Performance:**
```sql
SELECT
    team_name,
    COUNT(*) AS rounds,
    SUM(round_won) AS wins,
    ROUND(SUM(round_won) * 100.0 / COUNT(*), 1) AS win_rate,
    ROUND(AVG(team_adr), 1) AS avg_adr
FROM agg_team_round_summary
GROUP BY team_name
ORDER BY win_rate DESC;
```

---

### agg_team_map_stats

Team performance aggregated by map.

**Primary Key:** `(team_name, map_name, tournament_name)`

**Key Columns:**
- `games_played`, `games_won`, `games_lost`, `map_win_rate`
- `total_rounds`, `rounds_won`, `round_win_rate`
- `avg_adr`, `avg_kd`, `avg_kast` - Performance metrics
- `total_fb`, `total_fd`, `avg_opening_wr` - Opening duels
- `avg_trade_rate` - Trading efficiency
- `total_clutches_won`, `total_clutches_attempted`

**Row count:** ~300

**Example Query - Map Pool:**
```sql
SELECT
    map_name,
    games_played,
    games_won,
    map_win_rate,
    avg_adr
FROM agg_team_map_stats
WHERE team_name = 'Cloud9'
ORDER BY games_played DESC;
```

---

### agg_team_series_stats

Team head-to-head records at series level. Two rows per series (one per team perspective).

**Primary Key:** `(series_id, team_name)`

**Key Columns:**
- `opponent_name` - Opposing team
- `series_won`, `series_lost` - Series outcome
- `maps_played`, `maps_won`, `maps_lost` - Map breakdown

**Row count:** ~210

**Example Query - Head-to-Head:**
```sql
SELECT
    opponent_name,
    SUM(series_won) AS wins,
    SUM(series_lost) AS losses
FROM agg_team_series_stats
WHERE team_name = 'Cloud9'
GROUP BY opponent_name
ORDER BY (SUM(series_won) + SUM(series_lost)) DESC;
```

---

### agg_player_win_shares

Pre-calculated Win Shares per player per game.

**Primary Key:** `(player_id, game_id)`

**Key Columns:**
- `rounds_played`, `rounds_won` - Game participation
- `first_bloods`, `survivals`, `deaths_traded`, `multi_kills`, `plants`, `defuses` - Raw counts
- `fb_win_share`, `survival_share`, `traded_death_share`, etc. - Weighted contributions
- `total_win_share`, `win_share_per_round` - Totals
- `opening_duel_efficiency`, `survival_rate`, `trade_efficiency` - Efficiency metrics
- `adr`, `kd_ratio` - Context metrics

**Row count:** ~2,600

**Example Query - Top Win Share Players:**
```sql
SELECT
    player_name,
    team_name,
    SUM(rounds_played) AS total_rounds,
    ROUND(AVG(win_share_per_round), 3) AS avg_ws_per_round,
    ROUND(AVG(survival_rate), 3) AS avg_survival
FROM agg_player_win_shares
GROUP BY player_name, team_name
HAVING SUM(rounds_played) > 200
ORDER BY avg_ws_per_round DESC
LIMIT 10;
```

## Transformation Pipeline

These tables are populated by transformations 08-13:
- `08_agg_first_blood_stats.sql`
- `09_agg_post_plant_stats.sql`
- `10_agg_team_round_summary.sql`
- `11_agg_team_map_stats.sql`
- `12_agg_team_series_stats.sql`
- `13_agg_player_win_shares.sql`

Run with:
```bash
python database/scripts/orchestration/run_transformations.py
```

## Refresh Notes

- Tables rebuild incrementally based on `calculated_at` timestamps
- Full refresh: `--full-refresh` flag
- Reference table (`ref_win_probability_factors`) only changes if weights are recalculated

---

*See [docs/win_shares.md](../docs/win_shares.md) for Win Share methodology and benchmarks.*
