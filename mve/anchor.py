#!/usr/bin/env python3
"""Question anchors: which series/team/map in the current database "represents" it — **looked up live from the database, never hard-coded**.

Why it must exist
-----------------
`tasks.py` / `question_gen.py` / `voyager.py` used to hard-code:

    SERIES = "2843069"      # that series in the GRID slice
    C9     = "Cloud9"

After switching databases (GRID → vlr.gg → rib.gg), these two values **do not exist at all** in the database, so:

    of the seed questions' 17 scoring points, **0 can produce data** on the rib database

Observed (2026-10-08): `series_totals / map_rounds_split / fb_conversion_analysis /
corrode_collapse / lotus_win_rate / max_losing_streak_map` were all value=None or 0,
and 3 of them directly reported "SQL returned 0 rows — this question has no solution in this series' data".

`db_switch.probe()` gave this warning long ago ("the series 2843069 the original questions depend on is not in this
database… none of the questions can find data"), but it was only a **hint**, with no automatic reconfiguration —
this module fills in that step.

How anchors are picked (can't be picked arbitrarily, or questions still won't produce data)
------------------------------------------------------------------------------------------
- series: the one with the **most rounds** (most complete data, questions most likely solvable)
- team: the team with the **most rounds** in that series
- map: the map with the **most rounds** in that series

Persistence and invalidation
----------------------------
The result is written into the `anchor` field of `db_config.json` (alongside db_path), recomputed by
`db_switch.write()` when the database is switched. If it can't be read, connect and look it up live.

⚠️ Switching databases in-process doesn't take effect automatically (values are resolved at module import) —
the dashboard restarts the service when switching databases, which is enough. To force a refresh, run
`python mve/anchor.py --refresh`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DB_CONFIG = HERE / "db_config.json"
FALLBACK = {"series": "2843069", "team": "Cloud9", "map": ""}


def _cfg() -> dict[str, Any]:
    try:
        obj = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _db_path() -> Path:
    """Current database path. **Relative paths always resolve against the repo root**.

    ⚠️ Why it can't be returned as-is (observed on the server on 2026-10-13):
      `vlml_env` does `os.chdir(vlml/)` at startup (VLML internal queries depend on that cwd).
      So a relative path `vlml/data/vlml_rib.duckdb` gets joined by DuckDB into
      `<repo>/vlml/vlml/data/vlml_rib.duckdb` — **joined twice** — reporting
      `IO Error: database does not exist`. On the loop side what's seen is "three consecutive rounds
      with zero successful tool calls", triage files it under `linkage`, which looks like an inability
      to learn but is actually the path. Configs are written by humans, and when humans write relative
      paths they mean the repo root, so resolve against the repo root.

    ⚠️ The fallback must be `db_switch.DEFAULT_DB` (currently rib.gg, decided by the user on 2026-10-13),
      not another `vlml_events.duckdb` literal — each module writing its own copy of the default database
      is exactly the soil for "the database silently switches the moment db_path is empty". The single
      definition lives in db_switch; here we only import.
    """
    try:
        from db_switch import DEFAULT_DB
    except Exception as _e:                                  # pragma: no cover
        raise RuntimeError(
            f"cannot read db_switch.DEFAULT_DB (the single definition of the default database): {_e}") from _e
    p = str(_cfg().get("db_path") or "").strip()
    if p:
        q = Path(p).expanduser()
        return q if q.is_absolute() else (HERE.parent / q).resolve()
    return DEFAULT_DB


def pick(db_path: Path | str | None = None,
         prefer: dict[str, str] | None = None) -> dict[str, str]:
    """Look up anchors live from the database. Pick the series/team/map with the **most rounds**,
    so questions are guaranteed to produce data.

    `prefer` is the previous round's anchors: if they **still exist** in the database, keep them,
    to avoid all question texts changing pointlessly after a database switch (e.g. when re-running
    the pipeline).
    """
    path = Path(db_path) if db_path else _db_path()
    out = dict(FALLBACK)
    prefer = prefer or {}
    try:
        import duckdb
        con = duckdb.connect(str(path), read_only=True)
    except Exception:
        return out
    try:
        # ── series: the one with the most rounds ──
        try:
            row = con.execute(
                "SELECT CAST(series_id AS VARCHAR) FROM rounds "
                "GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        except Exception:
            row = None
        if not row:                       # no rounds in the database: fall back to picking any from the series table
            try:
                row = con.execute(
                    "SELECT CAST(series_id AS VARCHAR) FROM series LIMIT 1").fetchone()
            except Exception:
                row = None
        if row:
            sid = str(row[0])
            if str(prefer.get("series") or "") in _series_ids(con):
                sid = str(prefer["series"])
            out["series"] = sid

        # ── team / map: both restricted to this series ──
        sid = out["series"]
        # ⚠️ The team SQL contains **two** `?` placeholders (one each for winning / losing);
        # passing a single parameter throws and silently falls back to the fallback value
        # (observed picking Cloud9 — a team that doesn't exist in the rib database at all).
        # Match the number of parameters to the number of placeholders.
        # ⚠️ `ORDER BY` must have a second sort key (observed 2026-10-14: two runs of the same input
        #   produced two different results): ordering only by `COUNT(*) DESC` leaves ties between
        #   two teams / two maps up to DuckDB — the same "switch to rib.gg" run twice gave
        #   **Karmine Corp** once and **Nongshim RedForce** once; "switch back to GRID" gave
        #   NRG once and Cloud9 once. The cost of anchor wobble: question texts change on their
        #   own between restarts (asking about these two teams today, those two tomorrow), and
        #   during debugging you think something else changed. Add `, t` / `, map_name` to make it
        #   reproducible.
        for key, sql, args in (
            ("team",
             "SELECT t FROM (SELECT winning_team_name AS t FROM rounds "
             "WHERE CAST(series_id AS VARCHAR)=? UNION ALL "
             "SELECT losing_team_name FROM rounds "
             "WHERE CAST(series_id AS VARCHAR)=?) "
             "WHERE t IS NOT NULL AND t<>'' "
             "GROUP BY 1 ORDER BY COUNT(*) DESC, t LIMIT 1",
             [sid, sid]),
            ("map",
             "SELECT map_name FROM rounds WHERE CAST(series_id AS VARCHAR)=? "
             "AND map_name IS NOT NULL AND map_name<>'' "
             "GROUP BY 1 ORDER BY COUNT(*) DESC, map_name LIMIT 1",
             [sid]),
        ):
            try:
                r = con.execute(sql, args).fetchone()
            except Exception:
                r = None
            if r and str(r[0]):
                out[key] = str(r[0])
            elif prefer.get(key):
                out[key] = str(prefer[key])
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out


def _series_ids(con) -> set[str]:
    try:
        return {str(r[0]) for r in con.execute(
            "SELECT DISTINCT CAST(series_id AS VARCHAR) FROM rounds").fetchall()}
    except Exception:
        return set()


def refresh(db_path: Path | str | None = None) -> dict[str, str]:
    """Recompute anchors and write them back to db_config.json (keeping other fields like db_path)."""
    cfg = _cfg()
    path = Path(db_path) if db_path else _db_path()
    anchor = pick(path, prefer=cfg.get("anchor") or {})
    cfg["anchor"] = anchor
    if db_path:
        cfg["db_path"] = str(path)
    DB_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return anchor


def get() -> dict[str, str]:
    """Current anchors. Prefer reading the config (fast, no database connection); if unreadable, look it up live."""
    a = _cfg().get("anchor")
    if isinstance(a, dict) and a.get("series") and a.get("team"):
        return {"series": str(a["series"]), "team": str(a["team"]),
                "map": str(a.get("map") or "")}
    return pick()


def series() -> str:
    return get()["series"]


def team() -> str:
    return get()["team"]


def map_name() -> str:
    return get()["map"]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="question anchors (follows the database)")
    ap.add_argument("--refresh", action="store_true", help="recompute and write back to db_config.json")
    ap.add_argument("--db", default="", help="which database to compute against (defaults to the current database)")
    a = ap.parse_args()
    if a.refresh:
        r = refresh(a.db or None)
        print(f"anchors recomputed and written to db_config.json: {json.dumps(r, ensure_ascii=False)}")
    else:
        print(json.dumps(get(), ensure_ascii=False))
