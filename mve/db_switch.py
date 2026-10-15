#!/usr/bin/env python3
"""Database switching: point VLML at another .duckdb file.

Why this exists: SERIES / team names in the task registry are **hard-coded** (tasks.py's
SERIES='2843069', C9='Cloud9'), so swapping databases invalidates all those constants.
So this module does not pretend "swapping the database swaps the questions"; instead:

  1. **Health-check immediately** after a swap: can it open, how many tables, how many events,
     and is the series the original questions depend on still there.
  2. Report the health-check result honestly to the dashboard, and let the human decide whether to also wipe and restart.

The health check must happen **before** the switch — discovering an empty/wrong database only after switching leaves the human wondering where the previous data
went. So the flow is: probe(path) first → human confirms → then write db_config.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "db_config.json"
# Default database = rib.gg (decided by the user on 2026-10-13: "reconnect rib.gg and set it as the default database").
# Rationale: the rib database carries the ext_ layer (first-blood roundStats / economy roundEconomy / sideStats),
# giving more analyzable axes than a single GRID slice; the GRID slice `vlml_events.duckdb` remains an option.
#
# ⚠️ This constant is the **only** definition of the default database. Previously `vlml_env` / `anchor` /
# `entity_catalog` / `open_quiz` / `scene_context` each wrote their own
# `vlml/data/vlml_events.duckdb` literal — that is the breeding ground for bugs like "an empty db_path silently swaps the database"
# (observed on the server on 2026-10-13: the config was blanked, five of six modules silently
# fell back to GRID, and only the dashboard display still pointed at rib). So all those fallbacks import this constant;
# do not hard-code it again. To change the default database, edit only this line.
DEFAULT_DB = ROOT.parent / "vlml" / "data" / "vlml_rib.duckdb"


def _abs(p: str) -> str:
    """Relative paths resolve against the repo root (same convention as vlml_env / anchor / entity_catalog).

    ⚠️ Must not resolve against cwd: `vlml_env` switches cwd to `vlml/`, and a relative path gets joined twice
       (observed on the server on 2026-10-13: rib was configured but unreachable, erroring "database does not exist").
    """
    q = Path(p or "").expanduser()
    return str(q if q.is_absolute() else (ROOT.parent / q).resolve())


def current_path() -> str:
    try:
        obj = json.loads(CONFIG.read_text(encoding="utf-8"))
        p = str((obj or {}).get("db_path") or "")
    except Exception:
        p = ""
    return _abs(p) if p else str(DEFAULT_DB)


def is_default() -> bool:
    try:
        return Path(current_path()).resolve() == DEFAULT_DB.resolve()
    except Exception:
        return True


def write(path: str) -> dict[str, Any]:
    """Switch database. **Writes config only**, no validation — validation goes through probe().

    Two points that must hold:
    1. **Store absolute paths**. A relative path points at different files under different cwds (vlml_env switches
       cwd to vlml/), so storing absolute from the source removes all ambiguity.
    2. **Preserve other fields in the config**. Previously `{"db_path": p}` overwrote the whole thing, wiping
       `anchor` (the current series/team/map anchor) in the same config — every database swap,
       the anchor had to be re-set once.
    """
    p = str(path or "").strip()
    stored = _abs(p) if p else ""
    try:
        obj = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            obj = {}
    except Exception:
        obj = {}
    obj["db_path"] = stored
    CONFIG.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ⚠️ The anchor must follow the database — and via refresh() ("keep it if it exists in the database"),
    #    not by preserving the old value.
    #    The preserve-the-old-value version (fixed 2026-10-13) was meant to cure "the wholesale overwrite wipes anchor",
    #    but it over-corrected: the anchor stayed on the old database, so
    #
    #     db_path = ""                        → GRID database (only Cloud9 / NRG)
    #     anchor  = 1058 / Nongshim RedForce  → rib.gg entities
    #
    #    Observed consequence on the server (2026-10-14): the model's own words in a consumption record —
    #     "Voyager computes Cloud9's round win situation,
    #       while VLML computes Nongshim RedForce's round win situation"
    #    It even crashed the schema probe: `_default_args` takes SERIES/C9 (from anchor),
    #    asking the GRID database about rib's 1058 → "No data found for series 1058",
    #    6/10 tools probed back only an error.
    #
    #    `anchor.refresh()` semantics cover both sides: `pick(path, prefer=old anchor)`
    #    — if the old anchor is **still present** in the new database, keep it (a swap should not needlessly change the question); if not, re-pick by
    #    "the series/team/map with the most rounds". So refresh unconditionally here.
    anchor_note = ""
    try:
        import anchor
        before = dict(obj.get("anchor") or {})
        new_anchor = anchor.refresh(stored or str(DEFAULT_DB))
        # ⚠️ Must report **per field**, not dismiss it with one line "old anchor not found" (observed 2026-10-14):
        #    when switching to rib, series 1058 was kept from the database and only team changed
        #    (Nongshim RedForce → Karmine Corp, because pick chooses by "the team with the most rounds
        #    in that series"). Saying the whole thing as "old anchor not found" makes people think nothing was kept,
        #    and the next investigation follows the wrong lead.
        keys = ("series", "team", "map")
        changed = [k for k in keys
                   if str(before.get(k) or "") != str(new_anchor.get(k) or "")]
        kept = [k for k in keys if k not in changed and str(before.get(k) or "")]
        if not changed:
            anchor_note = "The anchor is still valid in the new database; all fields kept"
        else:
            anchor_note = (f"Anchor recomputed for the new database: {'/'.join(changed)} changed"
                           f" ({before.get(changed[0])} → {new_anchor.get(changed[0])})")
            if kept:
                anchor_note += f"; {'/'.join(kept)} still present in the new database, kept"
    except Exception as e:
        anchor_note = f"Anchor recompute failed ({type(e).__name__}); run python mve/anchor.py --refresh manually"

    return {"ok": True, "db_path": stored or str(DEFAULT_DB),
            "is_default": not bool(stored), "anchor_note": anchor_note}


def probe(path: str) -> dict[str, Any]:
    """Probe: is this database usable, and what is inside. Must run this before switching."""
    p = str(path or "").strip() or str(DEFAULT_DB)
    out: dict[str, Any] = {"path": p, "ok": False, "is_default": not bool(path)}
    f = Path(p)
    if not f.exists():
        out["error"] = "File does not exist"
        return out
    if f.suffix.lower() not in (".duckdb", ".db", ".ddb"):
        out["error"] = f"Extension {f.suffix} does not look like a DuckDB database (.duckdb / .db / .ddb)"
        return out

    try:
        import duckdb
        con = duckdb.connect(str(f), read_only=True)
    except Exception as e:
        out["error"] = f"Cannot open: {type(e).__name__}: {e}"[:200]
        return out

    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name").fetchall()]
        out["tables"] = tables
        # Event count: table names differ across schemas, so try both
        events = None
        for t in ("base_events", "events"):
            if t in tables:
                try:
                    events = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    break
                except Exception:
                    continue
        out["events"] = events

        # The critical compatibility check: is the series the questions depend on still there
        series_ids: list[str] = []
        try:
            from tasks import SERIES
            want = SERIES
        except Exception:
            want = ""
        for t in tables:
            try:
                cols = [r[0] for r in con.execute(
                    f"SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name='{t}'").fetchall()]
                if "series_id" not in cols:
                    continue
                rows = con.execute(
                    f"SELECT DISTINCT CAST(series_id AS VARCHAR) FROM {t} LIMIT 20"
                ).fetchall()
                for r in rows:
                    v = str(r[0])
                    if v and v not in series_ids:
                        series_ids.append(v)
            except Exception:
                continue
        out["series_ids"] = series_ids[:10]
        out["ok"] = bool(tables)
        out["warning"] = ""
        if want and series_ids and want not in series_ids:
            out["warning"] = (
                f"The series {want} that the original questions depend on is not in this database (present: {', '.join(series_ids[:5])})"
                " — after a database swap the series/team constants in the questions must be reconfigured, or every question will find no data"
            )
        elif want and not series_ids:
            out["warning"] = "No series_id column found at all; cannot tell whether the questions' series is still present"
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out


def anchor_status() -> dict[str, Any]:
    """Does the anchor (series / team / map) line up with the **current database**?

    ⚠️ Why this function exists separately (observed on the server 2026-10-14):
      In `db_config.json`, `anchor` and `db_path` are **two independent fields**;
      a database switch only writes `db_path`, leaving `anchor` as-is. So this state reproduces reliably:

        db_path = ""                       → falls back to the GRID default database
                                             (which contains only Cloud9 / NRG,
                                              Haven / Corrode / Lotus)
        anchor  = {series: "1058",
                   team: "Nongshim RedForce",
                   map: "Sunset"}          → all three are **rib.gg database** entities

      The consequence is not an error but a **silently wrong answer**: the question asks about "Nongshim RedForce" per the anchor,
      while the SQL queries the GRID database. The model's own words in a consumption record are the evidence:

        "Voyager computes Cloud9's round win situation,
         while VLML computes Nongshim RedForce's round win situation"

      So after a database swap / wipe, the anchor's allegiance must be computed and shown to the human, who decides whether to re-set it.
      This **never auto-deletes anchor** — that is the human's config; silently clearing it is another form of self-deception.
    """
    out: dict[str, Any] = {"anchor": {}, "checked": False, "stale": False,
                           "hint": ""}
    try:
        obj = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return out
    anchor = (obj or {}).get("anchor") or {}
    if not isinstance(anchor, dict) or not anchor:
        return out
    out["anchor"] = anchor

    path = current_path()
    try:
        import duckdb
        con = duckdb.connect(path, read_only=True)
    except Exception as e:
        out["hint"] = f"Cannot reach the current database; cannot verify the anchor: {type(e).__name__}: {e}"
        return out

    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main'").fetchall()]
    except Exception as e:
        con.close()
        out["hint"] = f"Failed to read the table list: {type(e).__name__}"
        return out

    def _has(cols_needed: tuple[str, ...], value: str) -> bool | None:
        """Does this value exist in the database. `None` = no column to check against (cannot query, does not mean absent)."""
        val = str(value or "").strip()
        if not val:
            return None
        for t in tables:
            try:
                cols = [r[0] for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name='{t}'").fetchall()]
            except Exception:
                continue
            for c in cols_needed:
                if c not in cols:
                    continue
                try:
                    n = con.execute(
                        f"SELECT COUNT(*) FROM {t} "
                        f"WHERE CAST({c} AS VARCHAR) = '{val}'").fetchone()[0]
                except Exception:
                    continue
                if n:
                    return True
        return False

    try:
        series_in = _has(("series_id",), str(anchor.get("series") or ""))
        team_in = _has(("team_name", "winning_team_name"),
                       str(anchor.get("team") or ""))
        map_in = _has(("map_name",), str(anchor.get("map") or ""))
        out["checked"] = True
        out["series_in_db"] = series_in
        out["team_in_db"] = team_in
        out["map_in_db"] = map_in
        # Only judge staleness for fields with a conclusive answer: None (no column to query) does not count as stale
        judged = [v for v in (series_in, team_in, map_in) if v is not None]
        out["stale"] = bool(judged) and not any(judged)
        if out["stale"]:
            out["hint"] = (
                f"Anchor {anchor.get('team')} / {anchor.get('map')} / series "
                f"{anchor.get('series')} are all missing from the current database — "
                "the question will be asked per the anchor while the SQL queries an empty database; the answer is 0 rather than an error."
                "Please re-set the anchor (dashboard \"Data source & database\") or switch back to the matching database."
            )
        elif judged and not all(judged):
            miss = [k for k, v in (("series", series_in), ("team", team_in),
                                   ("map", map_in)) if v is False]
            out["hint"] = "Anchor partially mismatched (not found: " + ", ".join(miss) + ")"
    finally:
        con.close()
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        print(json.dumps(probe(sys.argv[2] if len(sys.argv) > 2 else ""),
                         ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "set":
        print(json.dumps(write(sys.argv[2] if len(sys.argv) > 2 else ""),
                         ensure_ascii=False))
    else:
        print(json.dumps({"current": current_path(), "is_default": is_default(),
                          "probe": probe("")}, ensure_ascii=False, indent=2))
