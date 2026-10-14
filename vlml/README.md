# VLML — Valorant Analytics Modeling Layer

VLML is a structured modeling layer for Valorant esports analytics.
It standardizes metrics and relationships between datasets, then exposes them through MCP so AI tools can generate accurate and explainable insights.

## Why This Exists

- **The data model is the product.** VLML is built around an analytics data model with pre-computed metrics, so analysis is fast and consistent.
- **MCP is just the bridge.** The server delivers structured, data-only payloads to Claude, Gemini, and other LLMs — the AI generates the coaching insights, not the server.
- **Raw data becomes the model.** Source data comes from GRID JSON exports, and VLML transforms it into a structured analytics model.

## Architecture at a Glance

![VLML Architecture](docs/infographics.png)

1. **Modeling layer (DuckDB)**: Atomic events plus aggregated round/game/series tables optimized for analytics.
2. **MCP tools (bridge)**: Tools that return structured metrics only — no narratives, no opinions.
3. **LLM layer (insights)**: Generates narrative, recommendations, and VOD priorities.

## What You Get

- **Match analysis**: Team comparison, round timelines, impact metrics, VOD review targets.
- **Coaching context (v3.0)**: Economy cascades, round situations, attack patterns, historical benchmarks.
- **Player profiling**: Career stats, agent/map splits, clutch performance, trend signals.
- **Scouting reports**: Map pool, roster tendencies, opening duels, trade quality.
- **Deep-dive queries**: Use `query_sql` for ad-hoc analysis directly against the analytics tables.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Download raw events and build the database:

```bash
python database/scripts/ingestion/download_raw_events.py --year 2025
python database/scripts/orchestration/run_pipeline.py --year 2025
```

Run the MCP server:

```bash
vlml
```

Or:

```bash
.venv/bin/python -m vlml.server
```

## Tools 

Insights tools:
- `match_analysis_report`
- `player_profile_report`
- `scouting_report`
- `pattern_detection_report`

Database tools:
- `query_sql`
- `get_database_info`

See [docs/tools.md](docs/tools.md) for input/output details.

## Documentation

**Getting Started:**
- Setup: [docs/setup.md](docs/setup.md)
- Data flow: [docs/data_flow.md](docs/data_flow.md)
- Troubleshooting: [docs/troubleshooting.md](docs/troubleshooting.md)

**Reference:**
- Tools: [docs/tools.md](docs/tools.md)
- Architecture: [docs/architecture.md](docs/architecture.md)
- Prompting Guide: [docs/prompting_guide.md](docs/prompting_guide.md)
- Win Shares: [docs/win_shares.md](docs/win_shares.md)
- Report standard: [insights_reference.md](insights_reference.md)

**Database:**
- Pipeline guide: [database/README.md](database/README.md)
- Data model: [database/DATA_MODEL.md](database/DATA_MODEL.md)
- Derived tables: [database/DERIVED_TABLES.md](database/DERIVED_TABLES.md)

**Contributing:**
- Contributor guide: [docs/contributing.md](docs/contributing.md)
- SQL helper index: [src/vlml/tools/sql/README.md](src/vlml/tools/sql/README.md)

## Notes

- No prebuilt database is shipped. Use the pipeline to build `data/vlml_events.duckdb`.
- Raw input data comes from GRID JSON exports and is transformed into VLML analytics tables.
- All reports return metrics and evidence only. LLMs should generate insights and recommendations.
