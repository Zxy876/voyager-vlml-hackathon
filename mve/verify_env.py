#!/usr/bin/env python3
"""One-shot environment verification: is the synthetic data still there, do the agg tables have rows, can MCP tools return data.

Usage:
    python mve/verify_env.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VLML = ROOT / "vlml"
# VLML resolves data/vlml_events.duckdb via a relative path, so cwd must be switched to the repo root
os.chdir(VLML)
sys.path.insert(0, str(VLML / "src"))

# The sandbox environment raises PermissionError(EEXIST) on mkdir for an "already existing directory",
# while vlml/db/manager.py calls mkdir(data, exist_ok=True) on every connection.
# We patch on the MVE side here without touching the repo source: skip if the directory already exists.
_orig_mkdir = Path.mkdir


def _safe_mkdir(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    try:
        if self.exists() and self.is_dir():
            return None
    except OSError:
        pass
    return _orig_mkdir(self, *args, **kwargs)


Path.mkdir = _safe_mkdir  # type: ignore[assignment]

import duckdb  # noqa: E402

from vlml.tools.db_query_tools import execute_custom_sql, get_database_info  # noqa: E402
from vlml.tools.reports.match_analysis import match_summary_report  # noqa: E402

DB = VLML / "data" / "vlml_events.duckdb"
SERIES = "2843069"

ok = True


def mark(cond: bool, label: str, detail: str = "") -> None:
    global ok
    ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def main() -> int:
    print("=" * 64)
    print("  VLML synthetic environment verification")
    print("=" * 64)

    if not DB.exists():
        print(f"  database does not exist: {DB}")
        return 1
    con = duckdb.connect(str(DB), read_only=True)

    print("\n--- core tables ---")
    for t in ["series", "games", "rounds", "base_events"]:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        mark(n > 0, f"{t:14s}", f"{n} rows")

    print("\n--- aggregate tables (transformations output) ---")
    agg_tables = [
        "agg_player_round_stats", "agg_player_game_stats", "agg_player_series_stats",
        "agg_team_round_stats", "agg_team_game_stats", "agg_player_daily_stats",
        "agg_tournament_stats", "agg_first_blood_stats", "agg_post_plant_stats",
        "agg_team_round_summary", "agg_team_map_stats", "agg_team_series_stats",
        "agg_player_win_shares",
    ]
    empty = []
    for t in agg_tables:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        if n == 0:
            empty.append(t)
    mark(not empty, f"{len(agg_tables)} agg tables", "all have data" if not empty else f"empty tables: {empty}")

    print("\n--- do the planted patterns hold ---")
    rows = con.execute("""
        SELECT map_name, fb_team, COUNT(*) fb, SUM(fb_team_won) won,
               ROUND(AVG(fb_team_won)*100, 1) conv
        FROM agg_first_blood_stats GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()
    for m, team, fb, won, conv in rows:
        print(f"       {m:8s} {team:8s} first blood {fb:2d} → win {won:2d} = {conv}%")
    corrode = {(m, t): c for m, t, fb, w, c in rows}
    mark(
        corrode.get(("Corrode", "Cloud9"), 100) < 40,
        "Cloud9 first-blood conversion rate collapses on Corrode",
        f"{corrode.get(('Corrode', 'Cloud9'))}%",
    )
    fd = con.execute("""
        SELECT fd_player, COUNT(*) c FROM agg_first_blood_stats
        WHERE fd_team = 'Cloud9' GROUP BY 1 ORDER BY 2 DESC LIMIT 1
    """).fetchone()
    mark(fd[0] == "OXY", "OXY dies first most often on Cloud9", f"{fd[0]} {fd[1]} times")

    print("\n--- MCP tools ---")

    async def check_tools():
        d = await get_database_info()
        mark("available_tables" in d, "get_database_info", f"{len(d.get('available_tables', []))} tables")
        r = await execute_custom_sql("SELECT map_name, COUNT(*) AS n FROM rounds GROUP BY 1 ORDER BY 1")
        mark(r.get("row_count", 0) == 3, "execute_custom_sql", json.dumps(r["rows"], ensure_ascii=False))
        s = await match_summary_report(series_id=SERIES, team_name="Cloud9")
        km = (s.get("key_metrics") or {}).get("team", {})
        mark("opening_duels" in km, "match_summary_report", f"sections={len(s)}")
        od = km.get("opening_duels", {})
        print(f"       opening_duels: {json.dumps(od, ensure_ascii=False)}")
        # VLML provides num/denom natively — this is the data source for fact.base
        mark(
            "num" in json.dumps(od),
            "key_metrics provides num/denom natively",
            "→ fact.base can take denom directly, no need to compute it",
        )

    asyncio.run(check_tools())

    print("\n" + "=" * 64)
    print("  ✅ environment is ready, MVE can start running" if ok else "  ❌ problems found, see above")
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
