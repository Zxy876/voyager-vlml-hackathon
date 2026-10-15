#!/usr/bin/env python3
"""Bootstrap the VLML runtime environment (cwd + sandbox mkdir patch) for reuse by other MVE modules."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VLML = ROOT / "vlml"

# VLML internal queries depend on cwd being at the repo root
os.chdir(VLML)
sys.path.insert(0, str(VLML / "src"))

# In the sandbox, mkdir(exist_ok=True) on an existing directory raises PermissionError(EEXIST),
# while vlml/db/manager.py calls it on every connection. We bypass on the MVE side without
# touching the repo source.
_orig_mkdir = Path.mkdir


def _safe_mkdir(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    try:
        if self.exists() and self.is_dir():
            return None
    except OSError:
        pass
    return _orig_mkdir(self, *args, **kwargs)


Path.mkdir = _safe_mkdir  # type: ignore[assignment]

# ---- Database switchable ----
# VLML's tools all hard-code `EventDatabase(read_only=True)` (without db_path),
# so they always land on its bundled default database vlml/data/vlml_events.duckdb (the GRID slice).
# Switching databases can only be patched here — same trick as the mkdir patch above:
# bypass on the MVE side, without modifying the VLML repo source.
#
# Use a config file rather than environment variables: run_mve is launched by the pilot, and the pilot
# is launched by the dashboard, so env vars would have to be threaded through layer by layer;
# a written file is read the same by every process.
#
# ⚠️ The fallback when db_path is empty must also be this project's default database
#    (rib.gg, see db_switch.DEFAULT_DB) — VLML's bundled GRID default must not be kept,
#    for otherwise, once the config is lost, this one module silently falls back to GRID while
#    the others follow the new default: that's how "the data source silently changed" happens
#    (observed on the server on 2026-10-13: db_config was cleared, the tools actually connected
#    to the GRID database, while db_switch.current_path() reported rib).
DB_CONFIG = ROOT / "mve" / "db_config.json"

# The single definition of the default database lives in db_switch (mve/ is on sys.path:
# inserted by the dashboard on line 33, and sys.path[0] is mve/ when a script runs directly).
# An import failure must **fail loudly** — a silent fallback means the default database can
# quietly fork, which is exactly what we're guarding against.
from db_switch import DEFAULT_DB  # noqa: E402


def _read_db_config() -> dict:
    try:
        import json
        obj = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _resolve_db_path(raw: str) -> str:
    """Resolve the configured database path to an **absolute path**.

    ⚠️ Why explicit resolution is mandatory (observed on the server on 2026-10-13):
      Line 14 of this module does `os.chdir(VLML)` (VLML internal queries depend on cwd being under vlml/).
      So when the config holds a relative path `vlml/data/vlml_rib.duckdb`, DuckDB joins it with the
      current cwd → `/root/mve/vlml/vlml/data/vlml_rib.duckdb` — **the path gets joined twice**,
      and it reports `IO Error: database does not exist`.
      The symptom is deeply misleading: on the loop side it shows up as "three consecutive rounds
      with zero successful tool calls" (triage reports `linkage`), which looks like "can't learn",
      but is actually **a relative path in the config**.
      Hence: always resolve relative paths against the **repo root** (when people write config they
      mean the repo root, not vlml/).
    """
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    return str(p)


_RAW_DB_PATH = str(_read_db_config().get("db_path") or "")
# db_path empty → fall back to **this project's** default database (rib.gg), not VLML's bundled GRID database.
ACTIVE_DB_PATH = (_resolve_db_path(_RAW_DB_PATH) if _RAW_DB_PATH
                  else str(DEFAULT_DB))

# ⚠️ If the database file is missing — **shout about it immediately** (both for the configured and the default-fallback case).
#   The cost of not shouting (observed on the server on 2026-10-13): the loop ran three rounds back to back,
#   each round all three tool calls failed, and the log held only one line `IO Error: database does not exist`;
#   while panel's triage filed it under `linkage` ("the pipeline didn't run through") — the classification was
#   right, but a human has to see it first. With this line, you know at startup it's a path problem,
#   without waiting for three tasks to finish and then working backwards.
if not Path(ACTIVE_DB_PATH).exists():
    _src = (f"db_config says {_RAW_DB_PATH!r} (relative paths resolve against repo root {ROOT})"
            if _RAW_DB_PATH else "db_config's db_path is empty; falling back to the default database")
    print(f"[vlml_env] ⚠️ the database file to connect to does not exist: {ACTIVE_DB_PATH} ({_src})",
          file=sys.stderr, flush=True)
if ACTIVE_DB_PATH:
    from vlml.db.manager import EventDatabase  # noqa: E402

    _orig_db_init = EventDatabase.__init__

    def _patched_db_init(self, db_path=None, read_only=False):  # type: ignore[no-untyped-def]
        # If db_path was passed explicitly, proceed unchanged; otherwise swap in this project's configured database
        _orig_db_init(self, db_path or ACTIVE_DB_PATH, read_only=read_only)

    EventDatabase.__init__ = _patched_db_init  # type: ignore[assignment]

# All 10 tools exposed by MCP, imported as-is.
# Note the naming: MCP tool name == Python function name, with the sole exception of custom SQL —
#   externally query_sql (vlml/src/vlml/server.py:93), the implementing function is execute_custom_sql
#   (vlml/src/vlml/tools/db_query_tools.py:239). Both names must be dispatchable.
from vlml.tools.db_query_tools import execute_custom_sql, get_database_info  # noqa: E402
from vlml.tools.insights_tools import (  # noqa: E402
    match_analysis_report,
    match_economy_report,
    match_players_report,
    match_rounds_report,
    match_summary_report,
    pattern_detection_report,
    player_profile_report,
    scouting_report,
)

__all__ = [
    "VLML",
    "execute_custom_sql",
    "get_database_info",
    "match_analysis_report",
    "match_economy_report",
    "match_players_report",
    "match_rounds_report",
    "match_summary_report",
    "pattern_detection_report",
    "player_profile_report",
    "scouting_report",
]
