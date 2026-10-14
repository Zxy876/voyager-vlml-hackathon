# Contributing Guide

This guide explains how to add new metrics, reports, and tables to VLML.

## Development Workflow

**Always create a new branch before making changes:**

```bash
git checkout -b feature/your-feature-name
# Or for fixes
git checkout -b fix/issue-description
```

Never commit directly to `main`.

---

## Adding a New Metric to Existing Tables

### 1. Identify the Target Table

Metrics are computed at different grains:

| Grain | Table | When to Use |
|-------|-------|-------------|
| Round | `agg_player_round_stats` | Per-round player metrics |
| Game | `agg_player_game_stats` | Aggregated per-game |
| Series | `agg_player_series_stats` | Aggregated per-series |
| Team-Round | `agg_team_round_stats` | Per-round team metrics |
| Team-Game | `agg_team_game_stats` | Aggregated per-game |

### 2. Add Column to Schema

Edit the schema DDL file:

```sql
-- database/schema/agg_player_round_stats.sql
CREATE TABLE IF NOT EXISTS agg_player_round_stats (
    -- existing columns...

    -- Add your new column
    new_metric_total INTEGER DEFAULT 0,
    new_metric_denom INTEGER DEFAULT 0,

    -- existing columns...
);
```

### 3. Update Transformation SQL

Edit the transformation file to compute the metric:

```sql
-- database/transformations/01_agg_player_round_stats.sql

-- Add a CTE if needed
new_metric_calc AS (
    SELECT
        round_id,
        actor_player_id AS player_id,
        COUNT(*) AS metric_value
    FROM base_events
    WHERE round_id IN (SELECT round_id FROM new_rounds)
      AND some_condition = TRUE
    GROUP BY round_id, actor_player_id
),

-- Add to the INSERT column list
INSERT INTO agg_player_round_stats (
    -- existing columns...
    new_metric_total,
    new_metric_denom,
    -- existing columns...
)

-- Add to the SELECT
SELECT
    -- existing columns...
    MAX(COALESCE(nmc.metric_value, 0)) AS new_metric_total,
    1 AS new_metric_denom,
    -- existing columns...
FROM base_events e
LEFT JOIN new_metric_calc nmc
  ON nmc.round_id = e.round_id
 AND nmc.player_id = e.actor_player_id
-- existing joins...
```

### 4. Update Data Dictionary

Add column definition to `database/metadata/column_definitions.yaml`:

```yaml
agg_player_round_stats:
  # existing columns...
  new_metric_total: "Total count of new_metric events"
  new_metric_denom: "Denominator for new_metric rate calculation"
```

### 5. Test the Change

```bash
# Reset and rebuild
python database/scripts/maintenance/reset_schema.py --db data/vlml_events.duckdb --all
python database/scripts/orchestration/run_pipeline.py --year 2025

# Verify the new column
duckdb data/vlml_events.duckdb -c "SELECT new_metric_total FROM agg_player_round_stats LIMIT 5"
```

---

## Adding a New Aggregation Table

### 1. Design the Table

Determine:
- **Grain**: What does one row represent?
- **Primary key**: Unique identifier columns
- **Dependencies**: Which tables does it read from?
- **Use case**: What queries will this optimize?

### 2. Create Schema File

Add `database/schema/agg_new_table.sql`:

```sql
CREATE TABLE IF NOT EXISTS agg_new_table (
    -- Primary key columns
    entity_id VARCHAR NOT NULL,
    dimension_id VARCHAR NOT NULL,
    PRIMARY KEY (entity_id, dimension_id),

    -- Denormalized dimensions
    entity_name VARCHAR,
    tournament_name VARCHAR,

    -- Metrics
    metric_total INTEGER DEFAULT 0,
    metric_denom INTEGER DEFAULT 0,

    -- Metadata
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 3. Create Transformation File

Add `database/transformations/NN_agg_new_table.sql`:

```sql
-- Model: agg_new_table
-- Source: agg_player_round_stats (or other source)
-- Type: Incremental

-- Step 1: Find affected entities
CREATE TEMP TABLE affected_entities AS
SELECT DISTINCT entity_id
FROM source_table
WHERE calculated_at > COALESCE(
    (SELECT MAX(calculated_at) FROM agg_new_table),
    '1900-01-01'::TIMESTAMP
);

-- Step 2: Delete existing rows
DELETE FROM agg_new_table
WHERE entity_id IN (SELECT entity_id FROM affected_entities);

-- Step 3: Insert new aggregates
INSERT INTO agg_new_table (entity_id, dimension_id, metric_total, metric_denom, calculated_at)
SELECT
    entity_id,
    dimension_id,
    SUM(metric) AS metric_total,
    COUNT(*) AS metric_denom,
    CURRENT_TIMESTAMP
FROM source_table
WHERE entity_id IN (SELECT entity_id FROM affected_entities)
GROUP BY entity_id, dimension_id;

-- Cleanup
DROP TABLE affected_entities;
```

### 4. Register in Configuration

Add to `database/config/init_schema.yaml`:

```yaml
layers:
  - name: "Aggregated"
    tables:
      # existing tables...
      - name: "agg_new_table"
        schema_file: "database/schema/agg_new_table.sql"
```

Add to `database/config/transformations.yaml`:

```yaml
models:
  # existing models...
  - name: "agg_new_table"
    description: "Your table description"
    file: "database/transformations/NN_agg_new_table.sql"
    depends_on:
      - "source_table"
    target_table: "agg_new_table"
```

### 5. Update Documentation

Add to `database/DATA_DICTIONARY.md` and `database/DATA_MODEL.md`.

---

## Adding a New Report Type

### 1. Create SQL Helper Files

Add SQL files to `src/vlml/tools/sql/`:

```sql
-- src/vlml/tools/sql/new_report_data.sql
SELECT
    column1,
    column2,
    SUM(metric) AS total
FROM agg_player_round_stats
WHERE series_id IN ({series_clause})
  AND player_name ILIKE ?
  {map_filter}
GROUP BY column1, column2
```

### 2. Create Report Module

Add `src/vlml/tools/reports/new_report.py`:

```python
"""New report generation.

This module generates [description of what the report provides].

Report Sections:
    - section1: Description
    - section2: Description

SQL Dependencies:
    See src/vlml/tools/sql/README.md
    Key files: new_report_*.sql
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from vlml.db.manager import EventDatabase

from ..common import in_clause, load_sql


async def new_report(
    param1: str,
    param2: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a new report.

    Args:
        param1: Description.
        param2: Optional description.

    Returns:
        Dict containing report sections.
    """
    with EventDatabase(read_only=True) as db:
        sql = load_sql("new_report_data.sql").format(
            series_clause=in_clause(series_ids),
            map_filter="",
        )
        rows = db.query(sql, params)

        return {
            "report_type": "new_report",
            "version": "1.0",
            "data": [...],
        }
```

### 3. Register in Server

Add to `src/vlml/server.py`:

```python
from vlml.tools.reports import new_report

@mcp.tool()
async def new_report_tool(param1: str, param2: str | None = None) -> dict:
    """Generate a new report."""
    return await new_report(param1, param2)
```

### 4. Export from Package

Update `src/vlml/tools/reports/__init__.py`:

```python
from .new_report import new_report

__all__ = [
    # existing exports...
    "new_report",
]
```

### 5. Update Documentation

- Add to `docs/tools.md`
- Add SQL files to `src/vlml/tools/sql/README.md`

---

## SQL Query Conventions

### Parameter Placeholders

```python
# Series filtering (always use in_clause helper)
series_clause = in_clause(series_ids)  # Returns "?, ?, ?"
params = list(series_ids) + [other_params]

# Name matching (use ILIKE with wildcards)
params.append(f"%{name}%")

# Optional filters (use format strings)
map_filter = ""
if map_name:
    map_filter = " AND table.map_name = ?"
    params.append(map_name)
```

### Numerator/Denominator Pattern

Always output raw counts, not precomputed percentages:

```sql
-- Good: Output numerator and denominator
SELECT
    SUM(CASE WHEN clutch_won THEN 1 ELSE 0 END) AS clutch_wins,
    SUM(CASE WHEN is_clutch THEN 1 ELSE 0 END) AS clutch_attempts

-- Bad: Precomputed percentage
SELECT
    ROUND(SUM(clutch_won) * 100.0 / NULLIF(COUNT(*), 0), 1) AS clutch_rate
```

---

## Testing Requirements

### Before Submitting

1. **Run the pipeline** to verify transformations work:
   ```bash
   python database/scripts/orchestration/run_pipeline.py --year 2025
   ```

2. **Validate data** to check for duplicates:
   ```bash
   python database/scripts/maintenance/validate_data.py --samples
   ```

3. **Test queries** against real data:
   ```bash
   duckdb data/vlml_events.duckdb -c "SELECT * FROM your_new_table LIMIT 10"
   ```

4. **Check report output** if adding a new report:
   ```python
   # In Python REPL
   from vlml.tools.reports import new_report
   import asyncio
   result = asyncio.run(new_report("test_param"))
   print(result)
   ```

---

## Documentation Standards

### Code Comments

- Add module-level docstrings to all Python files
- Include docstrings for all public functions
- Document SQL files with header comments explaining purpose

### Data Dictionary Updates

For every new column, add to `database/metadata/column_definitions.yaml`:
- Column name
- One-line description
- Whether it's a numerator or denominator (if applicable)

### README Updates

Update relevant READMEs when adding:
- New tables → `database/README.md`, `database/DATA_MODEL.md`
- New metrics → `database/DATA_DICTIONARY.md`
- New reports → `docs/tools.md`
- New SQL files → `src/vlml/tools/sql/README.md`
