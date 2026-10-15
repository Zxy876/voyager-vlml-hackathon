#!/usr/bin/env python3
"""Which entities **actually exist** in the data source (series / teams / maps / players) — probed live, never hand-written.

Why this module exists
------------------
The "entry" layer must bind concrete entities (series ids / team names / player names), so we must know which
values are actually fillable in the database. A hand-written list is the thing that rots most easily: one database swap and it is all wrong. So this is a
**live database probe**; when nothing is found it returns empty with a reason, and never guesses.

Why this is not done inside `knowledge_graph.py`
------------------------------------
The graph must stay lightweight — "only stdlib imports, no database connection" (the dashboard must open instantly). The
database work lives here, persisted as `entity_catalog.json`; the graph and other modules only read the cache.

Usage
----
    python mve/entity_catalog.py            # refresh and print
    python mve/entity_catalog.py --json     # JSON output only
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB_CONFIG = HERE / "db_config.json"
CATALOG = HERE / "entity_catalog.json"

# How each table is probed (entity kind, value column, extra display columns)
#
# ⚠️ `player` has **two candidate tables**, tried in order until one has values:
#   `agg_player_series_stats` is derived by VLML from **per-event** data (present in the GRID source);
#   `ext_player_game_stats` is the **aggregate-level** stats provided by vlr.gg / rib.gg.
#   After connecting a public source there are no kill events, so the former is empty — then fall back to the latter,
#   otherwise the "player" column would show 0 while player data does exist (just at a different grain).
#
# rib.gg's two tables (`ext_player_round_stats` / `ext_player_side_stats`) exist only in
# `vlml_rib.duckdb`, so they go last as a fallback: a missing table records a warning,
# without affecting earlier hits. The same kind is **first-come-first-served**, so the order must not be shuffled.
PROBES: list[tuple[str, str, str, list[str]]] = [
    # kind       table                        value column      extra
    ("series", "series", "series_id", ["tournament_name"]),
    ("team", "agg_team_series_stats", "team_name", []),
    ("team", "ext_game_economy", "team_name", []),
    ("player", "agg_player_series_stats", "player_name", []),
    ("player", "ext_player_game_stats", "player_name", []),
    ("player", "ext_player_round_stats", "player_name", []),
    ("map", "games", "map_name", []),
]

KIND_LABEL = {"series": "Series", "team": "Team", "map": "Map",
              "player": "Player"}


def _db_path() -> Path:
    """Which database is currently connected — switchable via `db_config.json` (see vlml_env.py:52).

    ⚠️ Relative paths are resolved against the **repo root**, not the current cwd: `vlml_env` switches cwd to
       `vlml/`, and returning a relative path as-is makes DuckDB resolve `vlml/data/x.duckdb` into
       `<repo>/vlml/vlml/data/x.duckdb` (hit on the server on 2026-10-13).

    ⚠️ The fallback goes through `db_switch.DEFAULT_DB` (currently rib.gg) — the default database is defined
       there and only there; this module no longer writes its own literal (one copy per module is the breeding ground for "silent database swaps").
    """
    try:
        cfg = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        p = str((cfg or {}).get("db_path") or "").strip()
        if p:
            q = Path(p).expanduser()
            return q if q.is_absolute() else (ROOT / q).resolve()
    except Exception:
        pass
    from db_switch import DEFAULT_DB
    return DEFAULT_DB


def refresh() -> dict:
    """Probe the database live for the selectable values of every entity kind. On failure return an empty shell with error, never guess."""
    path = _db_path()
    out: dict = {"db_path": str(path), "entities": {}, "counts": {}}
    try:
        import duckdb
    except Exception as exc:
        out["error"] = f"duckdb unavailable: {type(exc).__name__}: {exc}"[:200]
        return out
    if not path.exists():
        out["error"] = f"Database file does not exist: {path}"
        return out
    try:
        con = duckdb.connect(str(path), read_only=True)
    except Exception as exc:
        out["error"] = f"Database connection failed: {type(exc).__name__}: {exc}"[:200]
        return out
    try:
        for kind, table, col, extra in PROBES:
            # When a kind has multiple candidate tables, **first-come-first-served**: once an earlier table has values, do not switch
            # (see the two-tier fallback for player in PROBES).
            if out["entities"].get(kind):
                continue
            try:
                cols = ", ".join([col, *extra])
                rows = con.execute(
                    f'SELECT DISTINCT {cols} FROM "{table}" '
                    f'WHERE "{col}" IS NOT NULL ORDER BY 1').fetchall()
            except Exception as exc:
                out.setdefault("warnings", []).append(
                    f"{table}.{col} not found: {type(exc).__name__}: {str(exc)[:80]}")
                continue
            items = []
            seen: set[str] = set()
            for r in rows:
                v = str(r[0])
                # With DISTINCT plus extra columns the same value can appear multiple times (observed: 12 teams
                # probed as 16, 21 maps probed as 7 duplicates) — deduplicate by value.
                if not v or v in seen:
                    continue
                seen.add(v)
                d = {"value": v}
                for i, e in enumerate(extra, start=1):
                    if i < len(r) and r[i] is not None:
                        d[e] = str(r[i])
                items.append(d)
            out["entities"][kind] = items
            out["counts"][kind] = len(items)
            out.setdefault("sources", {})[kind] = table
    finally:
        con.close()
    return out


def catalog(*, force: bool = False) -> dict:
    """Read the cache; if absent or `force`, refresh first."""
    if not force and CATALOG.exists():
        try:
            cached = json.loads(CATALOG.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("entities"):
                return cached
        except Exception:
            pass
    data = refresh()
    try:
        CATALOG.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    except Exception:
        pass
    return data


def options(kind: str) -> list[str]:
    """The concrete values that can be filled in for an entry kind (for UI dropdowns / the question generator)."""
    return [str(it.get("value") or "")
            for it in (catalog().get("entities") or {}).get(kind, [])
            if it.get("value")]


def _main() -> int:
    as_json = "--json" in sys.argv
    data = catalog(force=True)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    if data.get("error"):
        print(f"❌ {data['error']}")
        return 1
    print(f"Data source: {data['db_path']}")
    for kind, items in (data.get("entities") or {}).items():
        vals = [str(it.get("value")) for it in items]
        print(f"\n{KIND_LABEL.get(kind, kind)} ({kind}) · {len(vals)}")
        print("  " + (", ".join(vals) if vals else "(none)"))
    for w in data.get("warnings") or []:
        print(f"⚠ {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
