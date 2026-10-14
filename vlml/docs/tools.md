# Tools Reference

These tools return **data-only metrics**. LLMs should generate narratives and recommendations.

## Insights Tools

### `match_analysis_report`
**Input**
```
{ "series_id": "string", "team_name"?: "string", "map_name"?: "string" }
```

**Output (v3.0)**
- `report_type`, `version`, `series_id`, `team_name`
- `metadata` (tournament, date, teams, winner)
- `games` (map list with scores)
- `scope` (maps, rounds, confidence)
- `team_comparison` (per-team summary including assists, opening duels, trading, timing, economy)
- `key_metrics` (team + opponent)
- `player_performance` (per player round totals, ADR, KAST%, headshot hits/total, multikills)
- `kast_impact_analysis` (deaths without KAST + loss rate)
- `opening_death_impact` (opening deaths + loss rate)
- `round_timeline` (per round FK/FD, winner, timings)
- `highlight_rounds` (multi-kills + clutches)
- `half_breakdown` (first/second half splits)

**Coaching Context (v3.0)**
- `economy_context` (round-by-round economy with LAG for cascade analysis)
- `round_situations` (rich per-round state: weapons, util, trades, next round projection)
- `attack_patterns` (execute timing, site selection, first contact info)
- `benchmarks` (historical baseline rates: clutch, retake, economy matchups)

See [Prompting Guide](prompting_guide.md) for how to use these sections with LLMs.

### `player_profile_report`
**Input**
```
{ "player_name": "string", "series_ids"?: ["..."], "last_n_series"?: 5, "map_name"?: "string", "agent_name"?: "string" }
```

**Output**
- `report_type`, `version`, `player_name`
- `metadata` (team, date range, series/games/rounds)
- `career_stats` (kills, deaths, assists, ADR, KAST%, FB/FD, win rate)
- `agent_performance`
- `map_performance`
- `recent_form`
- `kast_impact`
- `opening_death_impact`
- `clutch_performance` (overall + by clutch size)
- `round_type_performance` (pistol/post-pistol/gun)
- `multikills`

### `scouting_report`
**Input**
```
{ "team_name": "string", "series_ids"?: ["..."], "last_n_series"?: 5, "map_name"?: "string" }
```

**Output**
- `report_type`, `version`, `team_name`
- `metadata` (series/games/rounds analyzed)
- `recent_form`
- `map_pool`
- `roster` (player stats + KAST%)
- `player_agents` (agent usage snapshots)
- `kast_impact`
- `opening_death_impact`
- `team_tendencies` (halves, pistol, opening duels, trades)
- `map_tendencies` (win rate, FB rate, trade rates)
- `agent_comps`

### `pattern_detection_report`
**Input**
```
{ "team_name"?: "string", "player_name"?: "string", "series_ids"?: ["..."], "min_rounds"?: 200 }
```

**Output**
- `report_type`, `subject`
- `scope` (series, rounds, confidence)
- `summary` (facts only)
- `key_metrics`

## Database Tools

### `query_sql`
Execute a `SELECT` query on DuckDB.

**Output**
- `columns` (list)
- `rows` (list of rows)
- `row_count`

### `get_database_info`
Returns row counts, recent series, and table list.

**Output**
- `database_path`
- `statistics` (row counts, timestamps)
- `recent_series`
- `available_tables`
- `sample_recent_stats`
- `usage_tips`

## Related Documentation

- [Data Flow Walkthrough](data_flow.md) - How data flows from raw events to reports
- [Prompting Guide](prompting_guide.md) - How to prompt LLMs for coaching insights
- [SQL Helper Index](../src/vlml/tools/sql/README.md) - Query file mapping for each report
- [Database Schema](../database/README.md) - Table definitions and example queries
