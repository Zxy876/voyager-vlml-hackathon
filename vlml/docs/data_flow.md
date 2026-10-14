# Data Flow Walkthrough

This document explains how data flows through VLML from raw GRID JSON to coaching insights, covering each stage of the pipeline.

## Overview

```
GRID API → JSONL Files → Atomic Tables → Aggregation Pipeline → Derived Tables → MCP Tools → Coaching Insights
```

The pipeline transforms raw esports match data through 6 stages:

1. **Download** - Fetch raw events from GRID API
2. **Load** - Parse JSONL into atomic tables (series, games, rounds, base_events)
3. **Transform** - Run 13 aggregation models in dependency order
4. **Derive** - Generate pre-joined analytics tables
5. **Query** - MCP tools execute SQL against aggregated tables
6. **Insight** - Reports provide structured data for LLM analysis

---

## Stage 1: Download Raw Events

**Script:** `database/scripts/ingestion/download_raw_events.py`

The pipeline starts by downloading raw event data from GRID's GraphQL API.

```bash
python database/scripts/ingestion/download_raw_events.py --year 2025
```

**What happens:**
1. Authenticates with GRID API using `GRID_API_KEY` from `.env`
2. Queries for series matching year/tournament filters
3. Downloads event data as JSONL files to `data/raw_events/{year}/{tournament}/`

**Output structure:**
```
data/raw_events/
└── 2025/
    ├── VCT Americas 2025/
    │   ├── series_abc123.jsonl
    │   └── series_def456.jsonl
    └── VCT Masters Bangkok 2025/
        └── series_ghi789.jsonl
```

Each JSONL file contains one JSON object per line representing match events (kills, deaths, plants, defuses, abilities).

---

## Stage 2: Load into Atomic Tables

**Script:** `database/scripts/ingestion/load_data.py`

Raw JSONL files are parsed and loaded into 4 atomic tables.

```bash
python database/scripts/ingestion/load_data.py --year 2025
```

**Parsing flow:**
```
JSONL File
    ↓
parsers.py (extract series/game/round/event data)
    ↓
classifiers.py (classify abilities, tournaments)
    ↓
db_loader.py (bulk insert into DuckDB)
```

**Key files:**
- `parsers.py` - Extracts structured data from GRID JSON format
- `classifiers.py` - Maps ability IDs to types, normalizes tournament names
- `db_loader.py` - Uses DuckDB `executemany()` for fast bulk inserts

**Atomic tables populated:**

| Table | Grain | Key Columns |
|-------|-------|-------------|
| `series` | 1 row per series | series_id, tournament_name, teams, date |
| `games` | 1 row per map | game_id, map_name, winner |
| `rounds` | 1 row per round | round_id, round_number, winner, spike_planted |
| `base_events` | 1 row per event | event_id, event_type, player_id, weapon, headshot |

**Important:** Each row gets an `ingested_at` timestamp. This is used for incremental processing to detect newly loaded data (not `occurred_at` which is when the match happened).

---

## Stage 3: Aggregation Pipeline

**Script:** `database/scripts/orchestration/run_transformations.py`
**Config:** `database/config/transformations.yaml`

The transformation pipeline runs 13 SQL models in dependency order to aggregate base events into coaching metrics.

```bash
python database/scripts/orchestration/run_transformations.py
```

**Transformation dependency graph:**

```
Layer 1 (Atomic)                    Layer 2 (Aggregated)
─────────────────                   ────────────────────
base_events ─────┬──────────────────► 01_agg_player_round_stats
rounds ──────────┤                           │
agent_roles ─────┤                           ▼
weapon_types ────┘                    02_agg_player_game_stats
                                             │
base_events ─────┬──────────────────► 04_agg_team_round_stats
rounds ──────────┘                           │
                                             ▼
                                      05_agg_team_game_stats
                                             │
                                             ▼
                 Layer 3 (Time-Series)
                 ─────────────────────
                 06_agg_player_daily_stats
                 07_agg_tournament_stats
```

**Transformation models (executed in order):**

| # | Model | Source | Purpose |
|---|-------|--------|---------|
| 01 | `agg_player_round_stats` | base_events, rounds | 40+ metrics per player per round |
| 02 | `agg_player_game_stats` | agg_player_round_stats | Game-level player aggregates |
| 03 | `agg_player_series_stats` | agg_player_game_stats | Series-level player aggregates |
| 04 | `agg_team_round_stats` | base_events, rounds | Team metrics per round |
| 05 | `agg_team_game_stats` | agg_team_round_stats | Game-level team aggregates |
| 06 | `agg_player_daily_stats` | agg_player_game_stats | Daily performance trends |
| 07 | `agg_tournament_stats` | multiple | Tournament-level stats |

**Incremental processing:**

Each transformation uses a "delete-and-rebuild" pattern:
1. Find rounds with `ingested_at` > last `calculated_at`
2. Delete existing rows for affected entities
3. Rebuild only those entities
4. Update `calculated_at` timestamp

This allows backfilling historical matches loaded after recent ones.

---

## Stage 4: Derived Tables (Layer 4)

**Models 08-13** create pre-joined analytics tables optimized for specific query patterns.

| # | Model | Purpose |
|---|-------|---------|
| 08 | `agg_first_blood_stats` | FB events with round outcomes |
| 09 | `agg_post_plant_stats` | Plant events with outcomes |
| 10 | `agg_team_round_summary` | Team-level round aggregates |
| 11 | `agg_team_map_stats` | Team performance by map |
| 12 | `agg_team_series_stats` | Head-to-head records |
| 13 | `agg_player_win_shares` | Win share per player per game |

**Why derived tables?**

Instead of joining at query time, these tables pre-compute common joins:
- `agg_first_blood_stats` joins `base_events` (kill) with `rounds` (outcome)
- `agg_post_plant_stats` joins plant events with defuse/detonation outcomes
- `agg_player_win_shares` applies probability lift weights from `ref_win_probability_factors`

---

## Stage 5: MCP Tool Queries

**Module:** `src/vlml/tools/`

MCP tools query aggregated tables and return structured JSON for LLM consumption.

**Tool to table mapping:**

| Tool | Primary Tables | Purpose |
|------|----------------|---------|
| `match_analysis_report` | agg_team_round_stats, agg_player_round_stats | Single series deep-dive |
| `player_profile_report` | agg_player_game_stats, agg_player_round_stats | Player career stats |
| `scouting_report` | agg_team_game_stats, agg_player_game_stats | Team tendencies |
| `pattern_detection_report` | multiple | Cross-dataset patterns |
| `query_sql` | any | Custom SQL queries |

**Query flow:**

```python
# 1. Report function receives parameters
async def match_analysis_report(series_id, team_name=None, map_name=None):

    # 2. Open database connection
    with EventDatabase(read_only=True) as db:

        # 3. Load and parameterize SQL
        sql = load_sql("team_round_metrics.sql").format(
            series_clause=in_clause(series_ids),
            map_filter=map_filter,
        )

        # 4. Execute query
        rows = db.query(sql, params)

        # 5. Transform to structured output
        return {
            "report_type": "match_analysis",
            "key_metrics": {...},
            "round_timeline": [...],
        }
```

**SQL helper files:** 42 SQL files in `src/vlml/tools/sql/` support these queries. See [SQL README](../src/vlml/tools/sql/README.md) for the complete mapping.

---

## Stage 6: Coaching Insights

**Reference:** `insights_reference.md`

MCP tools return structured JSON that LLMs convert to coaching insights.

**Report structure pattern:**

```json
{
  "report_type": "match_analysis",
  "version": "2.0",
  "metadata": { "series_id": "...", "date": "...", "teams": [...] },
  "scope": { "rounds": 48, "confidence": "high" },
  "key_metrics": {
    "opening_duels": { "fb": {"num": 15, "denom": 48}, ... },
    "conversion": { "fb_conv": {"num": 12, "denom": 15}, ... },
    "impact": { "multikills": {...}, "clutches": {...} },
    "consistency": { "kast": {...}, "adr": {...} }
  },
  "round_timeline": [...],
  "highlight_rounds": [...]
}
```

**Key design principles:**

1. **Numerator/denominator format** - No precomputed percentages; LLM calculates rates
2. **Evidence-based** - Raw counts enable fact-checking
3. **Hierarchical metrics** - Organized by category for structured analysis
4. **Scope indicators** - Sample size and confidence levels included

---

## Complete Pipeline Example

```bash
# 1. Download VCT 2025 data
python database/scripts/ingestion/download_raw_events.py --year 2025

# 2. Run full pipeline (init + load + transform + validate)
python database/scripts/orchestration/run_pipeline.py --year 2025

# 3. Start MCP server
python -m vlml.server

# 4. Query via MCP tool
# match_analysis_report(series_id="abc123")
```

**Data volume at each stage (typical VCT tournament):**

| Stage | Table | Approximate Rows |
|-------|-------|------------------|
| Atomic | base_events | 500,000+ |
| Atomic | rounds | 5,000+ |
| Atomic | games | 200+ |
| Atomic | series | 50+ |
| Aggregated | agg_player_round_stats | 50,000+ |
| Aggregated | agg_player_game_stats | 2,000+ |
| Derived | agg_first_blood_stats | 5,000+ |

---

## Debugging the Pipeline

**Check data at each stage:**

```sql
-- Atomic: Raw events loaded?
SELECT COUNT(*) FROM base_events;
SELECT COUNT(DISTINCT series_id) FROM series;

-- Aggregated: Transformations ran?
SELECT COUNT(*) FROM agg_player_round_stats;
SELECT MAX(calculated_at) FROM agg_player_round_stats;

-- Derived: Win shares calculated?
SELECT COUNT(*) FROM agg_player_win_shares;
```

**Common issues:**
- Empty aggregated tables → Check `ingested_at` timestamps in rounds table
- Missing recent data → Re-run transformations with `--full-refresh`
- Query errors → Check SQL parameter count matches placeholders

See [Troubleshooting Guide](troubleshooting.md) for detailed error resolution.

---

## Related Documentation

- [Database README](../database/README.md) - Schema details and example queries
- [Data Model](../database/DATA_MODEL.md) - Table grains and relationships
- [SQL Helper Index](../src/vlml/tools/sql/README.md) - Query file mapping
- [Transformations Config](../database/config/transformations.yaml) - Model dependencies
