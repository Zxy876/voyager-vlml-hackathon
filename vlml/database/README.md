# VLML Database - OLAP Data Model for Valorant Esports Analytics

> An advanced coaching-focused OLAP database with 70+ metrics for Moneyball-style Valorant esports analysis.

## Overview

This database is the core VLML modeling layer. It converts raw GRID JSON data into a **denormalized, pre-aggregated** OLAP schema optimized for fast analytical queries at multiple grains (round → game → series → daily → tournament).

### Key Features

- **21 tables** across 5 layers (reference → atomic → aggregated → time-series → derived)
- **70+ coaching metrics** including:
  - Combat efficiency (KAST%, headshot totals, damage per kill)
  - Trading analysis (trade kills, traded deaths, revenge timings)
  - Opening duels (FK-FD differential, opening duel win rate)
  - Clutch performance (1v1, 1v2, 1v3, 1v4, 1v5 tracking)
  - Economy analysis (eco rounds, thrifty wins, loadout values)
  - Consistency metrics (performance variance, half differentials)
  - Weapon preferences (rifle/operator dependency, headshot totals)
  - Team composition (agent roles, double duelist detection)
  - Situational performance (5v4, 4v5, post-plant win rates)
- **Incremental loading** - Delete-and-rebuild pattern using `ingested_at` timestamps to detect new data (supports backfilling historical matches)
- **Data validation** - Automated duplicate detection across all tables
 - **GRID source data** - Raw JSON exports are standardized into consistent tables

## Directory Structure

```
database/
├── schema/              # DDL for all 21 tables
│   ├── series.sql
│   ├── games.sql
│   ├── rounds.sql
│   ├── base_events.sql
│   ├── agg_player_round_stats.sql
│   ├── agg_player_game_stats.sql
│   ├── agg_player_series_stats.sql
│   ├── agg_team_round_stats.sql
│   ├── agg_team_game_stats.sql
│   ├── agg_player_daily_stats.sql
│   ├── agg_tournament_stats.sql
│   ├── agg_first_blood_stats.sql
│   ├── agg_post_plant_stats.sql
│   ├── agg_team_round_summary.sql
│   ├── agg_team_map_stats.sql
│   ├── agg_team_series_stats.sql
│   └── agg_player_win_shares.sql
├── metadata/            # Metadata and dictionary sources
│   └── column_definitions.yaml  # Column dictionary
├── seeds/               # Lookup tables / seed data
│   ├── agent_roles.sql      # 26 agents with role mappings
│   ├── weapon_types.sql     # 20 weapons with type classifications
│   ├── map_zones.sql        # Map zones bounding boxes
│   ├── ref_win_probability_factors.sql  # Win share weights
│   └── ability_types.json   # Ability type mapping
├── transformations/     # Incremental ETL logic
│   ├── 01_agg_player_round_stats.sql
│   ├── 02_agg_player_game_stats.sql
│   ├── 03_agg_player_series_stats.sql
│   ├── 04_agg_team_round_stats.sql
│   ├── 05_agg_team_game_stats.sql
│   ├── 06_agg_player_daily_stats.sql
│   ├── 07_agg_tournament_stats.sql
│   ├── 08_agg_first_blood_stats.sql
│   ├── 09_agg_post_plant_stats.sql
│   ├── 10_agg_team_round_summary.sql
│   ├── 11_agg_team_map_stats.sql
│   ├── 12_agg_team_series_stats.sql
│   └── 13_agg_player_win_shares.sql
├── config/              # YAML configurations
│   ├── init_schema.yaml         # Schema initialization config
│   └── transformations.yaml     # Transformation pipeline config
├── scripts/             # Python pipeline scripts
│   ├── ingestion/
│   │   ├── download_raw_events.py   # Download from GRID API
│   │   ├── load_data.py             # Load JSONL → atomic tables
│   │   ├── parsers.py               # JSONL parsing helpers
│   │   ├── classifiers.py           # Ability/tournament classifiers
│   │   ├── db_loader.py             # DuckDB bulk loader
│   │   └── file_download_client.py  # File Download API client
│   ├── orchestration/
│   │   ├── run_pipeline.py          # 🎯 MASTER SCRIPT - Run complete pipeline
│   │   ├── init_schema.py           # Initialize database from YAML
│   │   └── run_transformations.py   # Run transformations from YAML
│   ├── maintenance/
│   │   ├── reset_schema.py          # Drop tables (full reset)
│   │   ├── seed_map_zones.py        # Seed map zone bounding boxes
│   │   ├── validate_data.py         # Run validation checks
│   │   ├── validate_data.sql        # SQL validation queries
│   │   ├── remove_unnecessary_indexes.sh
│   │   ├── check_data_availability.py
│   │   ├── check_recent_series_dates.py
│   │   ├── find_2025_data.py
│   │   ├── fix_tournament_names.py
│   │   ├── query_2025_tournaments.py
│   │   └── search_2025_series.py
│   └── manager.py               # Database connection manager
└── README.md            # This file
```

## Architecture Notes

### manager.py vs Direct DuckDB Operations

The project uses two approaches for database operations:

**manager.py (EventDatabase class)**
- **Purpose**: Abstraction layer for MCP server and query helpers
- **Use cases**: Interactive queries, database stats, connection management
- **When to use**: Read operations and exploration (query-focused)
- **Performance**: Optimized for convenience, not bulk operations

**Direct DuckDB Operations (in ingestion/load_data.py)**
- **Purpose**: High-performance bulk loading of 500K+ events
- **Use cases**: Data ingestion from JSONL files
- **When to use**: Loading raw data, bulk inserts, transformations
- **Performance**: Uses `executemany()` and transactions for maximum speed

The separation ensures clean abstraction for application code while maintaining raw performance for ETL pipelines.

## Database Schema

### Layer 0: Reference Tables

#### `agent_roles`
- **Grain**: One row per agent (26 agents)
- **Purpose**: Map agents to roles (duelist/initiator/controller/sentinel)
- **Usage**: JOIN for team composition analysis

#### `weapon_types`
- **Grain**: One row per weapon (20 weapons)
- **Purpose**: Classify weapons by type and cost
- **Usage**: JOIN for weapon preference analysis

#### `ref_win_probability_factors`
- **Grain**: One row per factor (9 factors)
- **Purpose**: Win probability lift weights derived from VCT Americas 2025
- **Usage**: Win Share calculation weights

### Layer 1: Atomic Tables

#### `series`
- **Grain**: One row per series
- **PK**: `series_id`
- **Contains**: Tournament info, teams, date, format (Bo3/Bo5)

#### `games`
- **Grain**: One row per game (map)
- **PK**: `game_id`
- **Contains**: Map name, start/end times, winner

#### `rounds`
- **Grain**: One row per round
- **PK**: `round_id`
- **Contains**: Round number, side, winner, spike planted/defused

#### `base_events`
- **Grain**: One row per event (kills, deaths, plants, defuses, abilities)
- **PK**: `event_id`
- **Contains**: Timestamp, player, team, event type, weapon info, headshot flag

### Layer 2: Aggregated Metrics

#### `agg_player_round_stats`
- **Grain**: One row per (round_id, player_id)
- **PK**: `(round_id, player_id)`
- **Contains**: 40+ metrics including:
  - Combat: kills, deaths, assists, damage, survived
  - Opening duels: is_opening_kill, is_opening_death, is_entry_fragger
  - Trading: is_traded, is_trade_kill, trade_kill_time, is_untraded_death
  - Clutch: is_1v1/1v2/1v3/1v4/1v5, clutch_won
  - Multi-kills: is_double_kill, is_triple_kill, is_quad_kill, is_ace
  - Economy: loadout_value, is_eco_round, is_force_buy, is_thrifty
  - Weapons: weapon_name, weapon_type, rifle/smg/pistol/sniper kills
  - Abilities: flash_assists, early_util

#### `agg_player_game_stats`
- **Grain**: One row per (game_id, player_id)
- **PK**: `(game_id, player_id)`
- **Contains**: Game-level aggregates:
  - Composite scores: kast_percentage, impact_rating
  - Trading: trade_success_rate, avg_trade_time, untraded_deaths
  - Opening duels: fk_fd_differential, opening_duel_win_rate
  - Clutches: clutches_attempted, clutch_win_rate, 1v1/1v2/1v3 wins
  - Multi-kills: double_kills, triple_kills, quad_kills, aces
  - Economy: eco_win_rate, thrifty_count, avg_loadout_value
  - Consistency: rating_variance, first_half_rating, half_diff
  - Weapons: vandal/phantom/operator kills, headshot totals, weapon_preference

#### `agg_player_series_stats`
- **Grain**: One row per (series_id, player_id)
- **PK**: `(series_id, player_id)`
- **Contains**: Series-level aggregates and career stats

#### `agg_team_round_stats`
- **Grain**: One row per (round_id, team_name)
- **PK**: `(round_id, team_name)`
- **Contains**: Team performance per round

#### `agg_team_game_stats`
- **Grain**: One row per (game_id, team_name)
- **PK**: `(game_id, team_name)`
- **Contains**: Team-level metrics:
  - Composition: num_duelists/initiators/controllers/sentinels, is_double_duelist
  - Opening duels: fk_conversion_rate, fd_loss_rate
  - Trading: team_trade_success_rate, team_untraded_deaths
  - Special rounds: pistol_win_rate, anti_eco_rounds_won
  - Situational: conversion_5v4, comeback_4v5, post_plant_win_rate
  - Momentum: longest_win_streak, wins_after_timeout

### Layer 3: Time-Series Aggregations

#### `agg_player_daily_stats`
- **Grain**: One row per (date, player_id)
- **PK**: `(date, player_id)`
- **Contains**: Daily performance trends

#### `agg_tournament_stats`
- **Grain**: One row per (tournament_id, entity_type, entity_id)
- **PK**: `(tournament_id, entity_type, entity_id)`
- **Contains**: Tournament-level stats for players and teams

### Layer 4: Derived Aggregates

#### `agg_first_blood_stats`
- **Grain**: One row per round (where first blood occurred)
- **PK**: `round_id`
- **Contains**: First blood events pre-joined with round outcomes, FB team/player, round winner

#### `agg_post_plant_stats`
- **Grain**: One row per round (where plant occurred)
- **PK**: `round_id`
- **Contains**: Plant events pre-joined with round outcomes, conversion rates, detonated/defused flags

#### `agg_team_round_summary`
- **Grain**: One row per (round_id, team_name)
- **PK**: `(round_id, team_name)`
- **Contains**: Team-level round aggregates from player stats (kills, deaths, FB, trading, clutches)

#### `agg_team_map_stats`
- **Grain**: One row per (team_name, map_name, tournament_name)
- **PK**: `(team_name, map_name, tournament_name)`
- **Contains**: Team performance by map (win rates, opening duels, trading efficiency)

#### `agg_team_series_stats`
- **Grain**: One row per (series_id, team_name) - both perspectives
- **PK**: `(series_id, team_name)`
- **Contains**: Team head-to-head records at series level (maps won/lost)

#### `agg_player_win_shares`
- **Grain**: One row per (player_id, game_id)
- **PK**: `(player_id, game_id)`
- **Contains**: Pre-calculated win shares using probability lift weights (FB, survival, trades, plants, defuses)

## Usage

### Schema Dictionary

Column definitions live in `database/metadata/column_definitions.yaml`.

### 🎯 Quick Start - Run Complete Pipeline

```bash
# 0. Set GRID credentials in .env
# GRID_API_KEY=api-key
# GRID_API_URL=https://api-op.grid.gg/central-data/graphql
# VALORANT_GAME_ID=valorant

# 0. Download raw events from GRID (writes to data/raw_events/)
python database/scripts/ingestion/download_raw_events.py --year 2025

# Optional: download only Masters or Champions
python database/scripts/ingestion/download_raw_events.py --year 2025 --preset masters
python database/scripts/ingestion/download_raw_events.py --year 2025 --preset champions

# Optional: keyword filter
python database/scripts/ingestion/download_raw_events.py --year 2025 --tournament-keywords "Masters,Champions"

# 1. Run everything for 2025 data (init + load + transform + validate)
python database/scripts/orchestration/run_pipeline.py --year 2025

```

That's it! One command runs the entire pipeline:
1. ✅ Download raw events into `data/raw_events/`
2. ✅ Initialize schema (if needed)
3. ✅ Load raw JSONL data into atomic tables
4. ✅ Run transformation models
5. ✅ Validate data integrity

### Advanced Usage

**Run specific steps:**

```bash
# Only transformations (data already loaded)
python database/scripts/orchestration/run_pipeline.py --skip-schema --skip-load

# Initialize and load, skip transforms
python database/scripts/orchestration/run_pipeline.py --year 2025 --skip-transforms
```

**Individual steps (if needed):**

```bash
# 0. Download raw events only
python database/scripts/ingestion/download_raw_events.py --year 2025

# 1. Initialize schema only
python database/scripts/orchestration/init_schema.py

# 2. Load raw data only
python database/scripts/ingestion/load_data.py --year 2025

# 3. Run transformations only
python database/scripts/orchestration/run_transformations.py

# 4. Run specific transformation models
python database/scripts/orchestration/run_transformations.py --models agg_player_round_stats

# 5. Full refresh (rebuild all target tables)
python database/scripts/orchestration/run_transformations.py --full-refresh
```

**Reset schema (drop all tables):**

```bash
python database/scripts/maintenance/reset_schema.py --db data/vlml_events.duckdb --all
```

### 4. Validate Data

Run validation checks:

```bash
python database/scripts/maintenance/validate_data.py --samples
```

Or run the raw SQL checks:

```bash
duckdb data/vlml_events.duckdb < database/scripts/maintenance/validate_data.sql
```

Expected output: All tables should show `duplicates = 0` and status `✓ PASS`.

## Example Queries

### Top Clutch Performers
```sql
SELECT player_name, clutch_win_rate, clutches_attempted
FROM agg_player_game_stats
WHERE clutches_attempted >= 5
ORDER BY clutch_win_rate DESC
LIMIT 10;
```

### Trading Discipline by Team
```sql
SELECT team_name, team_trade_success_rate, team_untraded_deaths
FROM agg_team_game_stats
ORDER BY team_trade_success_rate DESC;
```

### Opening Duel Impact
```sql
SELECT player_name, fk_fd_differential, opening_duel_win_rate
FROM agg_player_game_stats
ORDER BY fk_fd_differential DESC
LIMIT 10;
```

### Player Consistency
```sql
SELECT player_name, rating_variance, half_diff
FROM agg_player_game_stats
ORDER BY rating_variance ASC  -- Most consistent
LIMIT 10;
```

### Headshot Leaders
```sql
SELECT player_name,
       total_headshot_kills,
       headshot_kills_denom,
       ROUND(100.0 * total_headshot_kills / NULLIF(headshot_kills_denom, 0), 1) AS hs_kill_pct,
       headshot_hits_total,
       hits_total,
       ROUND(100.0 * headshot_hits_total / NULLIF(hits_total, 0), 1) AS hs_hit_pct
FROM agg_player_game_stats
WHERE headshot_kills_denom >= 10
ORDER BY hs_kill_pct DESC
LIMIT 10;
```

### Operator Dependency
```sql
SELECT player_name, operator_kills, kills,
       (operator_kills::FLOAT / kills) AS op_percentage
FROM agg_player_game_stats
WHERE kills >= 10
ORDER BY op_percentage DESC
LIMIT 10;
```

### Eco Round Effectiveness
```sql
SELECT player_name, pistol_kills, eco_rounds_played, eco_win_rate
FROM agg_player_game_stats
ORDER BY pistol_kills DESC
LIMIT 10;
```

## Data Quality

### Validation Rules

All tables enforce these rules:
- **No duplicate primary keys**: `total_rows - distinct_pk = 0`
- **Referential integrity**: All foreign keys must exist in parent tables
- **Non-null constraints**: Critical dimensions cannot be NULL

### Denormalization Strategy

The schema intentionally repeats dimensions across tables to avoid JOINs:
- `tournament_name`, `tournament_year` repeated in all aggregation tables
- `team_name`, `opponent_team_name` denormalized for easy filtering
- `map_name`, `agent_name` included for direct queries

This trades storage space for query performance.

## Roadmap

### Next Steps
- [ ] **Semantic Layer** - LookML-inspired metrics and dimension definitions
- [x] **Team transformation models** - 04_agg_team_round_stats.sql, 05_agg_team_game_stats.sql
- [x] **Daily/tournament models** - 06_agg_player_daily_stats.sql, 07_agg_tournament_stats.sql
- [ ] **Advanced metrics** - Win probability, positioning heatmaps, map control
- [ ] **Data lineage** - Track metric calculation logic and dependencies
- [ ] **Performance benchmarks** - Query performance testing suite

## Related Documentation

**Data Model:**
- [DATA_MODEL.md](DATA_MODEL.md) - Table grains and relationships
- [DATA_DICTIONARY.md](DATA_DICTIONARY.md) - Column definitions
- [DERIVED_TABLES.md](DERIVED_TABLES.md) - Derived aggregate tables (Layer 4)

**Methodology:**
- [docs/win_shares.md](../docs/win_shares.md) - Win Share methodology and benchmarks

**Development:**
- [docs/data_flow.md](../docs/data_flow.md) - End-to-end pipeline walkthrough
- [docs/contributing.md](../docs/contributing.md) - How to add metrics and tables
- [docs/troubleshooting.md](../docs/troubleshooting.md) - Common issues and solutions
- [src/vlml/tools/sql/README.md](../src/vlml/tools/sql/README.md) - SQL helper file index

## Design Principles

1. **Query Performance First** - Denormalized for fast analytical queries
2. **Pre-aggregation** - Calculate metrics once at ingestion time
3. **Multiple Grains** - Support analysis at round, game, series, and tournament levels
4. **Incremental Processing** - Only reprocess affected entities
5. **Self-Documenting** - Column names and comments explain metric calculations
6. **Data Integrity** - Automated validation prevents corrupt data

---

**Database Engine**: DuckDB (embedded OLAP database)
**Schema Version**: 1.0.0
**Last Updated**: 2026-01-08
