# VLML Data Model

This document describes table grains and relationships. Use `database/metadata/column_definitions.yaml` for column-level details.

## Core Relationships

```
series (series_id)
  └─ games (game_id, series_id)
       └─ rounds (round_id, game_id)
            └─ base_events (event_id, round_id)
```

## Reference Tables

- `agent_roles`: one row per agent, role mapping
- `weapon_types`: one row per weapon, type/cost mapping
- `map_zones`: map bounding boxes for site/default zones
- `ability_types`: ability name/type mapping (seed JSON)
- `ref_win_probability_factors`: win probability lift weights (VCT Americas 2025)

## Aggregation Tables

### Player Round (`agg_player_round_stats`)
**Grain:** `(round_id, player_id)`  
**Use cases:** opening duels, trades, clutch flags, utility impact, weapon usage.

### Player Game (`agg_player_game_stats`)
**Grain:** `(game_id, player_id)`  
**Use cases:** per-map performance, K/D, ADR, KAST, opening duel and trade rollups.

### Player Series (`agg_player_series_stats`)
**Grain:** `(series_id, player_id)`  
**Use cases:** multi-map summaries, consistency over a match.

### Player Daily (`agg_player_daily_stats`)
**Grain:** `(date, player_id)`  
**Use cases:** trend analysis over time.

### Team Round (`agg_team_round_stats`)
**Grain:** `(round_id, team_name)`  
**Use cases:** round-by-round results, entry conversion, trades, tempo, economy.

### Team Game (`agg_team_game_stats`)
**Grain:** `(game_id, team_name)`  
**Use cases:** team map performance, composition, opening duels, post-plant stats.

### Tournament (`agg_tournament_stats`)
**Grain:** `(tournament_id, entity_type, entity_id)`
**Use cases:** tournament-level rollups for teams and players.

## Derived Aggregates

### First Blood Stats (`agg_first_blood_stats`)
**Grain:** `round_id`
**Use cases:** first blood conversion analysis, FB team win correlation.

### Post-Plant Stats (`agg_post_plant_stats`)
**Grain:** `round_id`
**Use cases:** post-plant conversion rates, detonation vs defuse analysis.

### Team Round Summary (`agg_team_round_summary`)
**Grain:** `(round_id, team_name)`
**Use cases:** team-level round aggregates, fast queries without player-level joins.

### Team Map Stats (`agg_team_map_stats`)
**Grain:** `(team_name, map_name, tournament_name)`
**Use cases:** map pool analysis, team map win rates and performance.

### Team Series Stats (`agg_team_series_stats`)
**Grain:** `(series_id, team_name)`
**Use cases:** head-to-head records, series-level team performance.

### Player Win Shares (`agg_player_win_shares`)
**Grain:** `(player_id, game_id)`
**Use cases:** player impact quantification using probability lift weights.
