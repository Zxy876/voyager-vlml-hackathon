# Setup

This project is designed to provide **data-only metrics** for LLMs (Claude/Gemini/etc.) to generate coaching insights.

## 1) Environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Copy and edit the environment file:

```bash
cp .env.example .env
```

Required in `.env`:

```
GRID_API_KEY=your-key
GRID_API_URL=https://api-op.grid.gg/central-data/graphql
VALORANT_GAME_ID=valorant
```

### Get a GRID API Key

This project specifically requires the **GRID Historical Data API** (via the Cloud9 Hackathon partnership).

1.  **Register for the Hackathon**: Join the [Sky's The Limit - Cloud9 x JetBrains Hackathon](https://cloud9.devpost.com/).
2.  **Request Data Access**: Apply for the [GRID Open Access program](https://grid.gg/get-valorant/) to get your API key.
    *   *Note: Approval may take up to 48 hours.*
3.  **Verify Access**: Ensure your key has permissions for the `central-data` Graph.


## 2) Build the Database

Download raw events (writes to `data/raw_events/{year}/...`):

```bash
python database/scripts/ingestion/download_raw_events.py --year 2025
```

Run the pipeline (schema + load + transforms + validate):

```bash
python database/scripts/orchestration/run_pipeline.py --year 2025
```

This creates `data/vlml_events.duckdb`, which the MCP server reads.

## 3) Run the MCP Server (Local)

```bash
vlml
```

Or:

```bash
.venv/bin/python -m vlml.server
```

## 4) Claude Desktop Setup

Claude Desktop reads MCP configs from:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\\Claude\\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

Example configuration:

```json
{
  "mcpServers": {
    "vlml": {
      "command": "/bin/zsh",
      "args": [
        "-lc",
        "cd /Users/yourname/proj/vlml && .venv/bin/python -m vlml.server"
      ],
      "env": {
        "GRID_API_KEY": "your-key",
        "GRID_API_URL": "https://api-op.grid.gg/central-data/graphql",
        "VALORANT_GAME_ID": "valorant"
      }
    }
  }
}
```

Replace the path and environment variables with your own. Ensure the database exists before launching Claude. Quit and relaunch Claude Desktop after saving.

## 5) Optional: Run Tests

```bash
.venv/bin/python -m pytest tests/test_insights_tools.py
```
