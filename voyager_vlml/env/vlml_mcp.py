"""VLML MCP environment: mounts VLML's MCP tools into Voyager's execution environment.

User instruction ③: "install the VLMLMCP dependency in the voyager environment (DB, RIB, VLGG)"

What it does:
  · Bootstraps the VLML runtime (mve/vlml_env.py: cwd + mkdir patch + swappable db_path)
  · Exposes VLML's MCP tools as the environment's callable action space:
      - SQL primitives: execute_custom_sql (query_sql), get_database_info
      - Report tools: match_summary / match_rounds / match_players / match_economy /
                 match_analysis / scouting / player_profile / pattern_detection
  · Returns an **enumerable observation** after each call: {ok, tool, rows, error, data}
    This is exactly the "environment feedback returned to Voyager" in the design:
    whether the tool call succeeded or failed, and how many rows were fetched this time.

⚠️ This module **produces no VLML verdict and no reference answer**: the environment only handles
   "you go fetch the data, and I report back honestly whatever you got".
   Scoring (the result of VLML running a pass) lives only in the dashboard, not in this environment feedback.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MVE_DIR = _REPO_ROOT / "mve"
if str(_MVE_DIR) not in sys.path:
    sys.path.insert(0, str(_MVE_DIR))


def _bootstrap_vlml() -> None:
    """Bootstrap the VLML runtime (cwd / sandbox mkdir patch / swappable database)."""
    import vlml_env  # noqa: F401  # the side effect is the bootstrap


class VLMLMCPEnv:
    """VLML MCP environment. The interface is deliberately small: call(name, args) / run_sql(sql)."""

    def __init__(self) -> None:
        _bootstrap_vlml()
        self._tools: dict[str, Any] = {}
        self._load_tools()

    # ---------- tool loading ----------
    def _load_tools(self) -> None:
        """Load the VLML MCP tools (SQL primitives + report tools). A tool that fails to load is
        marked unavailable, so it does not drag the environment down."""
        specs = [
            # (external name, module, function)
            ("query_sql", "vlml.tools.db_query_tools", "execute_custom_sql"),
            ("get_database_info", "vlml.tools.db_query_tools", "get_database_info"),
            ("match_summary_report", "vlml.tools.reports.match_analysis", "match_summary_report"),
            ("match_rounds_report", "vlml.tools.reports.match_analysis", "match_rounds_report"),
            ("match_players_report", "vlml.tools.reports.match_analysis", "match_players_report"),
            ("match_economy_report", "vlml.tools.reports.match_analysis", "match_economy_report"),
            ("match_analysis_report", "vlml.tools.reports.match_analysis", "match_analysis_report"),
            ("scouting_report", "vlml.tools.reports.scouting", "scouting_report"),
            ("player_profile_report", "vlml.tools.reports.player_profile", "player_profile_report"),
            ("pattern_detection_report", "vlml.tools.reports.pattern_detection", "pattern_detection_report"),
        ]
        import importlib

        for name, mod, fn in specs:
            try:
                self._tools[name] = getattr(importlib.import_module(mod), fn)
            except Exception as e:  # pragma: no cover
                print(f"(VLML MCP tool {name} failed to load: {type(e).__name__}: {e})")

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def has(self, name: str) -> bool:
        return name in self._tools

    # ---------- data source (DB / RIB / VLGG) ----------
    def data_source(self) -> str:
        """The data source corresponding to the current database. Decided by the database file name
        (existing mve convention: vlml_vlr→vlr.gg / vlml_rib→rib.gg, otherwise→GRID/DB)."""
        try:
            import vlml_env

            path = str(getattr(vlml_env, "ACTIVE_DB_PATH", "") or "")
        except Exception:
            path = ""
        name = os.path.basename(path).lower()
        if "rib" in name:
            return "rib.gg"
        if "vlr" in name:
            return "vlr.gg"
        return "GRID/DB"

    # ---------- execution ----------
    @staticmethod
    def _nested_error(res: Any) -> str:
        """Dig out the **nested error** from a tool response.

        ⚠️ Hit this in practice: when a VLML tool fails it **does not raise**, it returns
        `{"error": "Query execution failed: Binder Error: ..."}` (with the error key at the top level).
        This module originally used only try/except, so the error response was judged as
        `ok=True, rows=1` —— the SQL clearly blew up, yet the loop treated it as "fetched 1 row":
          · practice side: false success → the wrong SQL gets stored as a skill in the library;
          · consumption side: what was fetched is an error object, and the fact assembler sees an
            error, not data (returning an empty array is correct — the "no mismatching" constraint
            took effect), so "the tool succeeded, 1 row fetched" but the human gets no answer at all.
        """
        if not isinstance(res, dict):
            return ""
        err = res.get("error")
        if not err:
            inner = res.get("result") or res.get("data")
            if isinstance(inner, dict):
                err = inner.get("error")
        return str(err or "")

    async def call(self, name: str, **args: Any) -> dict[str, Any]:
        """Call one VLML MCP tool and return an enumerable observation.

        Returns {ok, tool, rows, error, data}:
          ok    —— whether the tool call succeeded or failed (feedback item ① in the design)
          rows  —— how many rows were fetched this time (feedback item ② in the design)
        """
        fn = self._tools.get(name)
        if fn is None:
            return {"ok": False, "tool": name, "rows": 0,
                    "error": f"unloaded tool {name}", "data": None}
        try:
            res = await fn(**args)
        except Exception as e:
            return {"ok": False, "tool": name, "rows": 0,
                    "error": f"{type(e).__name__}: {e}", "data": None}
        err = self._nested_error(res)
        if err:
            return {"ok": False, "tool": name, "rows": 0,
                    "error": err[:300], "data": res}
        return {"ok": True, "tool": name, "rows": _rows_of(res),
                "error": "", "data": res}

    async def run_sql(self, sql: str) -> dict[str, Any]:
        """Execute one SQL statement (the data-retrieval code written by the model runs here)."""
        return await self.call("query_sql", sql_query=sql)

    async def schema_block(self, max_cols: int = 20, max_tables: int = 24) -> str:
        """Query the current database's tables and columns live and render them into a schema text
        block for the model.

        Following the companion's entity_catalog "live-query, don't hardcode" principle: question
        generation and SQL writing may only reference table/column names that actually exist here,
        to stop the model from inventing them.

        ⚠️ max_cols=20 was tuned in practice on 2026-10-14: at 14 columns,
        `ext_player_round_stats`'s `first_kill` (the 16th column) **got truncated exactly** ——
        the model can't see the first-blood column, so first-blood questions are never written correctly.
        The column budget must cover the widest table.
        """
        obs = await self.run_sql(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='main' ORDER BY table_name, ordinal_position"
        )
        if not obs.get("ok"):
            return ""
        data = obs.get("data") or {}
        rows = data.get("rows") if isinstance(data, dict) else data
        cols: dict[str, list[str]] = {}
        types: dict[str, dict[str, str]] = {}
        for r in rows or []:
            if len(r) >= 2:
                t = str(r[0])
                cols.setdefault(t, []).append(str(r[1]))
                if len(r) >= 3:
                    types.setdefault(t, {})[str(r[1])] = _short_type(str(r[2]))

        # ⚠️ Must also give the model **how many rows each table has** (a pitfall hit in practice on 2026-10-13):
        #   in the current database (rib), `agg_player_round_stats` has **0 rows**, while the real data
        #   is all in `ext_player_round_stats` (2850 rows). If we only give table and column names, the model
        #   sees the agg_* table name as more "official" and queries it → gets 0 rows → reports "no data fetched" →
        #   the human sees "Voyager can't answer", even though the data is right there in the adjacent table.
        #   So empty tables must be **stated explicitly**: querying them only yields 0 rows, don't go there.
        counts: dict[str, int] = {}
        names = [t for t, _ in list(cols.items())[:max_tables]]
        if names:
            union = " UNION ALL ".join(
                f"SELECT '{t}' AS t, COUNT(*) AS n FROM \"{t}\"" for t in names)
            try:
                obs2 = await self.run_sql(union)
                if obs2.get("ok"):
                    d2 = obs2.get("data") or {}
                    r2 = d2.get("rows") if isinstance(d2, dict) else d2
                    for r in r2 or []:
                        if len(r) >= 2:
                            counts[str(r[0])] = int(r[1] or 0)
            except Exception:
                counts = {}

        # ⚠️ Must also give **one row of real data** (the 4th pitfall on 2026-10-13):
        #   with only column names, the model guesses what values the columns hold —— in practice it wrote
        #   `WHERE round_won = 'win'`, `= 'team'` (comparing a boolean column as a string);
        #   whether `side` is attack/defense or T/CT can only be guessed from the column name.
        #   VLML's own column-level definitions (`column_definitions.yaml`) **cover only 3 tables**,
        #   so they can't be relied on. Giving one sample row = letting the values speak for themselves,
        #   which beats a hundred lines of definition notes.
        #   Take only the 8 tables with the most rows (the schema has a length cap; better fewer-but-accurate
        #   than squeezing the samples out).
        samples: dict[str, str] = {}
        for t in sorted([x for x in names if counts.get(x)],
                        key=lambda x: -counts.get(x, 0))[:8]:
            try:
                o = await self.run_sql(f'SELECT * FROM "{t}" LIMIT 1')
                if not o.get("ok"):
                    continue
                dd = o.get("data") or {}
                rr = dd.get("rows") if isinstance(dd, dict) else dd
                if not rr:
                    continue
                first = rr[0]
                if not isinstance(first, (list, tuple)):
                    continue
                pairs = []
                for name, v in zip(cols.get(t) or [], first):
                    sv = "" if v is None else str(v)
                    pairs.append(f"{name}={sv[:16] or 'NULL'}")
                if pairs:
                    samples[t] = ", ".join(pairs[:8])
            except Exception:
                continue

        def _line(t: str, c: list[str]) -> str:
            ty = types.get(t) or {}
            head = ", ".join(
                f"{n}" + (f"::{ty[n]}" if n in ty else "") for n in c[:max_cols])
            n = counts.get(t)
            if n is None:
                base = f"- {t}: {head}"
            elif n == 0:
                base = f"- {t} (**0 rows · empty table, querying it only yields 0 rows, do not query**): {head}"
            else:
                base = f"- {t} ({n} rows): {head}"
            smp = samples.get(t)
            return base + (f"\n    sample: {smp}" if smp else "")

        # Tables with data go first, empty tables sink to the end: the schema text has a length cap (max_tables),
        # so if it really gets truncated, we'd rather drop empty tables than lose the only data-bearing ext_* tables
        # (they sort later alphabetically and would be truncated first without sorting).
        #
        # ⚠️ Structural fix on 2026-10-14 (Nemotron, in practice): warning "don't query empty tables" in the
        # prompt alone is not enough —— the model picks the table whose name looks "right" (agg_first_blood_stats
        # is the table born for first blood) even with a 0-row mark next to it; in practice it wouldn't switch
        # across 3 iterations of revision. So we **remove empty tables from the selectable list** and list them
        # separately as a banned list: choosing the wrong table becomes structurally impossible rather than
        # relying on the model's self-discipline.
        #   The original Voyager doesn't need this — MineDojo's env API is fixed,
        #   with no "which table has data" problem; MVE faces DuckDB directly and must manage it itself.
        if counts:
            live = [(t, c) for t, c in cols.items() if (counts.get(t) or 0) > 0]
            dead = sorted(t for t in cols if not (counts.get(t) or 0))
            live.sort(key=lambda kv: -(counts.get(kv[0]) or 0))
            lines = [_line(t, c) for t, c in live[:max_tables]]
            if dead:
                lines.append(
                    "\n⚠️ The following tables are **0-row empty tables** — referencing them in SQL is forbidden "
                    "(querying them only yields (NULL, 0), a vacuum value that looks like a result and fools the "
                    "row-count criterion):\n"
                    + ", ".join(dead))
            return "\n".join(lines)
        ordered = sorted(cols.items(), key=lambda kv: -(counts.get(kv[0]) or 0))
        return "\n".join(_line(t, c) for t, c in ordered[:max_tables])


def _short_type(t: str) -> str:
    """Shorten DuckDB type names to save the schema's character budget.

    Why types are mandatory (verified in practice on 2026-10-13): with only column names, the model
    guesses what values the columns hold —— it writes `WHERE round_won = 'win'`, `= 'team'`,
    comparing a boolean column as a string. Adding `::bool` makes such guesses disappear on the spot.
    Shortening is to fit more tables into the same character count: the schema text block has a length
    cap, and writing full type names would squeeze out the tables that follow.
    """
    s = (t or "").strip().upper()
    if not s:
        return ""
    if s.startswith("VARCHAR") or s.startswith("CHAR") or s == "TEXT":
        return "str"
    if s in {"BOOLEAN", "BOOL"}:
        return "bool"
    if any(s.startswith(p) for p in ("DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL")):
        return "float"
    if s.endswith("INT"):  # BIGINT / SMALLINT / UTINYINT / HUGEINT … integer-family fallback
        return "int"
    if s.startswith("TIMESTAMP"):
        return "ts"
    if s == "DATE":
        return "date"
    if s == "TIME":
        return "time"
    if s.startswith("BLOB") or s.startswith("BYTEA"):
        return "blob"
    return s.lower()[:12]


def _rows_of(res: Any) -> int:
    """Count "how many rows were fetched" from a tool response, tolerating several response shapes."""
    if res is None:
        return 0
    if isinstance(res, dict):
        for key in ("rows", "data", "results", "records", "items"):
            v = res.get(key)
            if isinstance(v, list):
                return len(v)
        # some report tools nest the result
        for v in res.values():
            if isinstance(v, list):
                return len(v)
        return 1 if res else 0
    if isinstance(res, list):
        return len(res)
    return 0


def _main() -> int:
    """Smoke test: list loaded tools + data source + run one SQL to inspect the observation."""
    import asyncio

    env = VLMLMCPEnv()
    print("data source :", env.data_source())
    print("loaded      :", env.tool_names)

    async def go() -> None:
        obs = await env.run_sql("SELECT COUNT(*) AS n FROM rounds")
        print("SQL obs     :", {k: obs[k] for k in ("ok", "tool", "rows", "error")})
        if obs["ok"]:
            print("return frag :", str(obs["data"])[:200])

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
