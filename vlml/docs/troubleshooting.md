# Troubleshooting Guide

Common issues and solutions for VLML pipeline and queries.

---

## Pipeline Failures

### Download Failures

**Error: "GRID_API_KEY not set"**

```
Solution: Create a .env file in the project root:
```

```bash
# .env
GRID_API_KEY=your-api-key-here
GRID_API_URL=https://api-op.grid.gg/central-data/graphql
VALORANT_GAME_ID=valorant
```

**Error: "No tournaments found for year X"**

```
Solution: Check available years in GRID:
```

```bash
# Try a different year or tournament filter
python database/scripts/ingestion/download_raw_events.py --year 2024
python database/scripts/ingestion/download_raw_events.py --year 2025 --preset masters
```

---

### Load Failures

**Error: "Raw events directory not found"**

```
Solution: Run download first:
```

```bash
python database/scripts/ingestion/download_raw_events.py --year 2025
```

**Error: "Table does not exist"**

```
Solution: Initialize schema before loading:
```

```bash
python database/scripts/orchestration/init_schema.py
# Then load
python database/scripts/ingestion/load_data.py --year 2025
```

**Error: "Duplicate primary key"**

```
Solution: The series may already be loaded. This is safe to ignore.
If you need to reload, reset the schema first:
```

```bash
python database/scripts/maintenance/reset_schema.py --db data/vlml_events.duckdb --all
python database/scripts/orchestration/run_pipeline.py --year 2025
```

---

### Transformation Failures

**Error: "Empty aggregated tables after transformation"**

```
This usually means the incremental logic didn't detect new data.
```

**Solution 1:** Check if atomic tables have data:

```bash
duckdb data/vlml_events.duckdb -c "SELECT COUNT(*) FROM base_events"
duckdb data/vlml_events.duckdb -c "SELECT COUNT(*) FROM rounds"
```

**Solution 2:** Run full refresh:

```bash
python database/scripts/orchestration/run_transformations.py --full-refresh
```

**Error: "Backfilled historical data not processing"**

```
This was a known issue where occurred_at was used instead of ingested_at.
Ensure you have the latest transformation SQL.
```

Check if `01_agg_player_round_stats.sql` uses `r.ingested_at`:

```sql
-- Should be:
OR r.ingested_at > COALESCE(...)

-- Not:
OR e.occurred_at > COALESCE(...)
```

---

## Query Errors

### SQL Parameter Errors

**Error: "Incorrect number of parameters"**

```
The number of ? placeholders doesn't match the params list.
```

**Solution:** Count placeholders vs parameters:

```python
# Count ? in SQL
series_clause = in_clause(series_ids)  # Returns "?, ?, ?" for 3 IDs
# Params should have: series_ids + other_params
params = list(series_ids) + [f"%{team_name}%"]
```

**Error: "Cannot bind parameter"**

```
Usually means a None value where a string was expected.
```

**Solution:** Add null checks:

```python
if team_name:
    params.append(f"%{team_name}%")
else:
    return {"error": "team_name required"}
```

---

### MCP Tool Errors

**Error: "No series found for player/team"**

```
The player or team name doesn't match any data.
```

**Solution:** Check exact spelling in database:

```bash
duckdb data/vlml_events.duckdb -c "SELECT DISTINCT player_name FROM agg_player_round_stats WHERE player_name ILIKE '%aspas%'"
```

**Error: "EventDatabase connection failed"**

```
Database file is missing or corrupted.
```

**Solution:** Check database path and rebuild if needed:

```bash
ls -la data/vlml_events.duckdb
# If missing, run pipeline
python database/scripts/orchestration/run_pipeline.py --year 2025
```

---

## Data Validation Issues

### Duplicate Primary Keys

**Symptom:** Validation shows duplicates

```bash
python database/scripts/maintenance/validate_data.py --samples
# Shows: ❌ agg_player_round_stats: 15 duplicate(s)
```

**Solution:** Reset and rebuild the table:

```bash
# Option 1: Full reset
python database/scripts/maintenance/reset_schema.py --db data/vlml_events.duckdb --all
python database/scripts/orchestration/run_pipeline.py --year 2025

# Option 2: Reset only aggregated tables
python database/scripts/maintenance/reset_schema.py --db data/vlml_events.duckdb --aggregated
python database/scripts/orchestration/run_transformations.py
```

### Missing Data

**Symptom:** Rounds count doesn't match expected

**Solution:** Check data at each stage:

```sql
-- Check atomic tables
SELECT COUNT(*) FROM series;
SELECT COUNT(*) FROM games;
SELECT COUNT(*) FROM rounds;
SELECT COUNT(*) FROM base_events;

-- Check aggregations
SELECT COUNT(*) FROM agg_player_round_stats;
SELECT MAX(calculated_at) FROM agg_player_round_stats;

-- If aggregations are stale, re-run transformations
```

---

## DuckDB Connection Problems

**Error: "Database is locked"**

```
Another process has the database open.
```

**Solution:** Close other connections:

```bash
# Find processes using the file
lsof data/vlml_events.duckdb

# If a Python script is stuck, kill it
pkill -f "vlml"
```

**Error: "Read-only database"**

```
Trying to write to a read-only connection.
```

**Solution:** Use `read_only=False`:

```python
with EventDatabase(read_only=False) as db:
    # Write operations
```

---

## Performance Issues

### Slow Queries

**Symptom:** Reports take >10 seconds

**Solution 1:** Use derived tables instead of base queries

```sql
-- Slow: Join from base_events
SELECT ... FROM base_events WHERE ...

-- Fast: Use pre-aggregated table
SELECT ... FROM agg_first_blood_stats WHERE ...
```

**Solution 2:** Add series_id filter early

```python
# Always filter by series first
WHERE series_id IN (?, ?, ?)
```

### Slow Transformations

**Symptom:** 01_agg_player_round_stats.sql takes >30 minutes

**Solution:** Check data volume:

```bash
duckdb data/vlml_events.duckdb -c "SELECT COUNT(*) FROM base_events"
```

For large datasets (>1M events), consider processing in batches by year.

---

## Debugging Tips

### Enable Verbose Output

```bash
# Python scripts
python -v database/scripts/orchestration/run_pipeline.py --year 2025
```

### Check Transformation Progress

```sql
-- See when tables were last updated
SELECT
    'agg_player_round_stats' AS table_name,
    MAX(calculated_at) AS last_updated,
    COUNT(*) AS rows
FROM agg_player_round_stats
UNION ALL
SELECT
    'agg_player_game_stats',
    MAX(calculated_at),
    COUNT(*)
FROM agg_player_game_stats;
```

### Test SQL Queries Directly

```bash
# Open interactive DuckDB shell
duckdb data/vlml_events.duckdb

# Test query
SELECT COUNT(*) FROM agg_player_round_stats WHERE series_id = 'test';
```

### Check Log Files

Pipeline output goes to stdout. Redirect to file:

```bash
python database/scripts/orchestration/run_pipeline.py --year 2025 2>&1 | tee pipeline.log
```

---

## Common Mistakes

1. **Forgetting to download before loading**
   - Run `download_raw_events.py` before `load_data.py`

2. **Not initializing schema**
   - Run `init_schema.py` or use `run_pipeline.py` which does both

3. **Using wrong database path**
   - Default is `data/vlml_events.duckdb`
   - Use `--db` flag to specify custom path

4. **Expecting precomputed percentages**
   - VLML outputs numerator/denominator pairs
   - Calculate rates in your code: `rate = num / denom`

5. **Case-sensitive name matching**
   - Use `ILIKE` with wildcards: `WHERE player_name ILIKE '%aspas%'`

---

## Getting Help

If you encounter issues not covered here:

1. Check [GitHub Issues](https://github.com/anthropics/claude-code/issues)
2. Review the [Data Flow Walkthrough](data_flow.md) for architecture understanding
3. Check the [Database README](../database/README.md) for schema details
