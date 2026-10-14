# SQL Helper Files

This directory contains 46 SQL query files that support the VLML insights reports. Each file is loaded by `load_sql()` from `common/metrics.py` and parameterized at runtime.

## Directory Organization

SQL files are organized by the report they support:

| Prefix | Report | Module |
|--------|--------|--------|
| `match_analysis_*` | Match Analysis | `reports/match_analysis.py` |
| `player_profile_*` | Player Profile | `reports/player_profile.py` |
| `scouting_*` | Scouting Report | `reports/scouting.py` |
| `team_*` | Team metrics (match analysis) | `reports/match_analysis.py` |
| `round_timeline_*` | Round timeline | `reports/match_analysis.py` |
| `economy_*`, `round_situation_*`, `attack_*`, `situation_*` | Coaching context (v3.0) | `reports/match_analysis.py`, `common/data_fetch.py` |
| `series_*`, `fetch_*`, `kast_*`, `opening_*` | Shared utilities | `common/data_fetch.py` |

---

## File-to-Function Mapping

### Match Analysis Report (`match_analysis_report`)

| SQL File | Used By | Purpose |
|----------|---------|---------|
| `match_analysis_teams.sql` | `match_analysis_report()` | Get team names in a series |
| `match_analysis_scope_rounds.sql` | `match_analysis_report()` | Count rounds for scope/confidence |
| `match_analysis_scope_maps.sql` | `match_analysis_report()` | Get distinct maps in series |
| `team_round_metrics.sql` | `_team_round_metrics()` | Opening duels, conversions per team |
| `team_impact_metrics.sql` | `_team_impact_metrics()` | Multikills, clutches per team |
| `team_consistency_metrics.sql` | `_team_consistency_metrics()` | KAST, ADR, K/D ratio |
| `team_economy_pistol.sql` | `_team_economy_metrics()` | Pistol round performance |
| `team_economy_eco.sql` | `_team_economy_metrics()` | Eco/thrifty round wins |
| `team_comparison.sql` | `_team_comparison()` | Side-by-side team stats |
| `team_comparison_impact.sql` | `_team_comparison()` | Multikills/clutches for comparison |
| `team_comparison_pistol.sql` | `_team_comparison()` | Pistol/post-pistol for comparison |
| `player_performance.sql` | `_player_performance()` | Per-player stats in series |
| `round_timeline_rounds.sql` | `_round_timeline()` | List rounds for timeline |
| `round_timeline_opener.sql` | `_round_timeline()` | Opening duel info per round |
| `round_timeline_enhanced.sql` | `_round_timeline_enhanced()` | Full round timeline with timing |
| `highlight_rounds.sql` | `_highlight_rounds()` | Aces, clutches, multikills |
| `half_breakdown.sql` | `_half_breakdown()` | First/second half performance |

### Coaching Context (v3.0)

| SQL File | Used By | Purpose |
|----------|---------|---------|
| `economy_round_context.sql` | `_economy_context()` | Round-by-round economy with LAG for cascade analysis |
| `round_situation_context.sql` | `_round_situations()` | Rich per-round state (weapons, util, trades, projections) |
| `attack_pattern_context.sql` | `_attack_patterns()` | Execute timing and site selection patterns |
| `situation_benchmarks.sql` | `situation_benchmarks()` | Historical baseline rates (clutch, retake, economy) |

### Player Profile Report (`player_profile_report`)

| SQL File | Used By | Purpose |
|----------|---------|---------|
| `player_profile_metadata.sql` | `player_profile_report()` | Player name, team, date range |
| `player_profile_career.sql` | `player_profile_report()` | Career totals (K/D/A, ADR, KAST) |
| `player_profile_agent.sql` | `player_profile_report()` | Performance by agent |
| `player_profile_map.sql` | `player_profile_report()` | Performance by map |
| `player_profile_recent.sql` | `player_profile_report()` | Recent series form |
| `player_profile_clutch.sql` | `player_profile_report()` | Clutch breakdown (1v1-1v5) |
| `player_profile_round_types.sql` | `player_profile_report()` | Pistol/eco/gun round splits |
| `player_profile_multikill.sql` | `player_profile_report()` | 2k/3k/4k/ace counts |
| `player_key_metrics.sql` | `player_key_metrics()` | Key metrics structure for player |

### Scouting Report (`scouting_report`)

| SQL File | Used By | Purpose |
|----------|---------|---------|
| `scouting_metadata.sql` | `scouting_report()` | Series/games/rounds count |
| `scouting_recent.sql` | `scouting_report()` | Recent match history |
| `scouting_map_pool.sql` | `scouting_report()` | Map win rates |
| `scouting_roster.sql` | `scouting_report()` | Roster stats summary |
| `scouting_agents.sql` | `scouting_report()` | Player-agent combinations |
| `scouting_team_tendencies.sql` | `scouting_report()` | Half/pistol/opening duel patterns |
| `scouting_map_tendencies.sql` | `scouting_report()` | Map-specific tendencies |
| `scouting_comps.sql` | `scouting_report()` | Agent compositions per map |

### Shared Utilities (`common/data_fetch.py`)

| SQL File | Used By | Purpose |
|----------|---------|---------|
| `fetch_series_ids_for_team.sql` | `fetch_series_ids_for_team()` | Find recent series for team |
| `fetch_series_ids_for_player.sql` | `fetch_series_ids_for_player()` | Find recent series for player |
| `series_metadata.sql` | `series_metadata()` | Series info (tournament, date, teams) |
| `series_games.sql` | `series_games()` | Games in a series |
| `series_games_scores.sql` | `series_games()` | Round scores per game |
| `kast_impact.sql` | `kast_impact()` | KAST impact analysis |
| `opening_death_impact.sql` | `opening_death_impact()` | Opening death impact analysis |
| `vod_review_priority.sql` | (VOD queue) | Prioritize rounds for review |

---

## Query Parameter Conventions

All SQL files use **positional parameters** (`?`) and **format string placeholders** for dynamic clauses:

### Common Patterns

```python
# Series filtering (always first parameters)
series_clause = in_clause(series_ids)  # Returns "?, ?, ?" for 3 IDs
sql = load_sql("example.sql").format(series_clause=series_clause)
params = list(series_ids) + [other_params...]

# Team/player name filtering (uses ILIKE with wildcards)
params.append(f"%{team_name}%")

# Optional map filtering
map_filter = ""
if map_name:
    map_filter = " AND table.map_name = ?"
    params.append(map_name)
```

### Placeholder Reference

| Placeholder | Purpose | Example |
|-------------|---------|---------|
| `{series_clause}` | IN clause for series IDs | `WHERE series_id IN ({series_clause})` |
| `{map_filter}` | Optional map filter | `{map_filter}` appended to WHERE |
| `{agent_filter}` | Optional agent filter | `{agent_filter}` appended to WHERE |
| `{player_filter}` | Optional player filter | `{player_filter}` appended to WHERE |
| `{round_ids_clause}` | IN clause for round IDs | `WHERE round_id IN ({round_ids_clause})` |

---

## Adding New SQL Files

1. **Name the file** with the appropriate prefix based on which report it supports
2. **Use format placeholders** for dynamic clauses (see conventions above)
3. **Use positional parameters** (`?`) for values
4. **Query from aggregated tables** (not base_events) for performance
5. **Return columns in a consistent order** matching the Python code that parses them
6. **Add the mapping** to this README

### Example: Adding a new scouting query

```sql
-- scouting_new_metric.sql
SELECT
    tgs.team_name,
    SUM(tgs.some_metric) as metric_total
FROM agg_team_game_stats tgs
WHERE tgs.series_id IN ({series_clause})
  AND tgs.team_name ILIKE ?
  {map_filter}
GROUP BY tgs.team_name
```

Then in `scouting.py`:
```python
new_sql = load_sql("scouting_new_metric.sql").format(
    series_clause=series_clause,
    map_filter=map_filter,
)
rows = db.query(new_sql, params)
```

---

## Table Reference

SQL files query from these aggregated tables (see `database/DATA_DICTIONARY.md` for columns):

| Table | Grain | Common Use |
|-------|-------|------------|
| `agg_player_round_stats` | player × round | Per-round player metrics |
| `agg_player_game_stats` | player × game | Per-game player totals |
| `agg_team_round_stats` | team × round | Per-round team metrics |
| `agg_team_game_stats` | team × game | Per-game team totals |
| `agg_team_round_summary` | team × round | Team-level round aggregates |
| `rounds` | round | Round metadata (winner, end reason) |
| `games` | game | Game metadata (map, winner) |
| `series` | series | Series metadata (tournament, date) |
