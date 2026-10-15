#!/usr/bin/env python3
"""VLML's **structural discovery**: how 21 tables are layered, which tables 46 insights read,
and which insights compose the 9 tools.

Why this file exists on its own
-------------------------------
The user asked one crucial question: "the relationship between the 22 tables is generally
modelled as MCP tools splitting further into insights".
Before this, the graph held only 2 tables (rounds / agg_first_blood_stats), because it was
reverse-derived from **our own rubric SQL** —— that amounts to "only the part we have asked
questions about enters the graph"; the real structure of VLML had not entered at all.

The actual structure (forensics taken from the VLML source) is four layers:

    MCP tools (9 report tools)
      └─ section / insight (e.g. key_metrics.economy.pistol, scope.confidence)
           └─ SQL files (46, tools/sql/*.sql, loaded by load_sql())
                └─ tables (21)

And a section's name **is** the prefix of answer_spec.value_path in the rubric ——
the missing "insight" layer is precisely why the model "knows which tool to call but not
which section the value comes from".

The basis for layering (the isomorphic counterpart of the study companion's `stage`)
------------------------------------------------------------------------------
The study companion's knowledge points carry a stage (primary / junior_high / senior_high),
indexed by `stage_to_ids` —— what you must already know before learning something is a
**learning order**. MVE's isomorphic counterpart is the **data-flow order**: what you must
already have before computing something.

    raw      base_events (the raw event stream)
    meta     series / games / rounds + 4 reference tables (metadata and dictionaries)
    agg      13 derived aggregate tables agg_* (computed from raw/meta, see transformations/)
    insight  46 insight SQL units
    tool     9 report tools
    dimension MVE's scoring dimensions (the top layer, declared by the rubric)

Following the study companion's rule: **everything queryable is queried live, nothing
is handwritten**.
Column names and the table list come from information_schema (queried live); derivation
relationships come from transformations/*.sql; insights from tools/sql/*.sql; ownership
from the load_sql() call sites in the source.

How to run
----------
    python mve/vlml_schema.py            # print the discovered structure
    python mve/vlml_schema.py --tables   # tables and layering only
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

VLML_ROOT = HERE.parent / "vlml"
SQL_DIR = VLML_ROOT / "src" / "vlml" / "tools" / "sql"
REPORTS_DIR = VLML_ROOT / "src" / "vlml" / "tools" / "reports"
TRANSFORM_DIR = VLML_ROOT / "database" / "transformations"
SCHEMA_DIR = VLML_ROOT / "database" / "schema"

# ---------------------------------------------------------------------------
# Stage: mirrors the study companion's stage (primary / junior_high / senior_high)
# ---------------------------------------------------------------------------
STAGE_RAW = "raw"
STAGE_META = "meta"
STAGE_AGG = "agg"
STAGE_INSIGHT = "insight"
STAGE_TOOL = "tool"
STAGE_DIMENSION = "dimension"

STAGE_LABEL = {
    STAGE_RAW: "raw event",
    STAGE_META: "metadata/dictionary",
    STAGE_AGG: "derived aggregate",
    STAGE_INSIGHT: "insight",
    STAGE_TOOL: "tool",
    STAGE_DIMENSION: "scoring dimension",
}

# The learning order = the data order: the lower the index, the earlier it must exist
STAGE_ORDER = [STAGE_RAW, STAGE_META, STAGE_AGG, STAGE_INSIGHT,
               STAGE_TOOL, STAGE_DIMENSION]

_FAKE_TABLES = {
    "select", "where", "group", "order", "limit", "having", "union",
    "values", "set", "table", "lateral",
}

_RE_FROM = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Tables: information_schema queried live
# ---------------------------------------------------------------------------
def _stage_of_table(name: str) -> str:
    """Layer the table by VLML's own naming convention (traceable, not a guess)."""
    if name == "base_events":
        return STAGE_RAW
    if name.startswith("agg_"):
        return STAGE_AGG
    return STAGE_META


def _grain_of_table(name: str) -> str:
    """The table's grain (the isomorphic counterpart of the study companion's `unit`).

    Derived from the **table-name convention**: agg_{subject}_{grain}_stats.
    This is VLML's own convention (see the file names in database/transformations/),
    not something I invented.
    """
    subject = {"player": "player", "team": "team", "tournament": "tournament"}
    grain = {"round": "per round", "game": "per map", "series": "per series",
             "map": "per map", "daily": "per day", "series_stats": "per series"}
    if name == "base_events":
        return "one row per event"
    if name == "rounds":
        return "one row per round"
    if name == "games":
        return "one row per map"
    if name == "series":
        return "one row per series"
    m = re.match(r"agg_(\w+?)_(round|game|series|map|daily)_stats$", name)
    if m:
        return f"{subject.get(m.group(1), m.group(1))} · {grain.get(m.group(2), m.group(2))}"
    if name.startswith("agg_"):
        # NOTE: knowledge_graph.py compares against this exact string, so the two
        # must stay aligned (knowledge_graph.py: "aggregate table" / "reference/dictionary table").
        return "aggregate table"
    return "reference/dictionary table"


async def _q(sql: str) -> list[tuple]:
    import vlml_env  # noqa: F401
    from vlml_env import execute_custom_sql
    try:
        res = await execute_custom_sql(sql)
    except Exception:
        return []
    return list((res or {}).get("rows") or [])


async def discover_tables() -> dict[str, dict[str, Any]]:
    """Query the table list + column names + primary keys + row counts live."""
    rows = await _q("SELECT table_name, column_name FROM information_schema.columns "
                    "ORDER BY table_name, ordinal_position")
    cols: dict[str, list[str]] = {}
    for t, c in rows:
        cols.setdefault(str(t), []).append(str(c))

    out: dict[str, dict[str, Any]] = {}
    for name in sorted(cols):
        try:
            cnt = await _q(f"SELECT COUNT(*) FROM {name}")
            n = int(cnt[0][0]) if cnt else 0
        except Exception:
            n = 0
        out[name] = {
            "stage": _stage_of_table(name),
            "grain": _grain_of_table(name),
            "columns": cols[name],
            "rows": n,
            "source": "information_schema (queried live)",
        }
    return out


def discover_lineage(known: set[str]) -> dict[str, list[str]]:
    """Which tables a derived table comes from —— parse transformations/*.sql.

    These files have the shape INSERT INTO agg_x SELECT ... FROM base_events ...,
    and the **non-self** table names appearing in FROM/JOIN are the upstream. Self-loops
    (DELETE FROM the table itself) must be excluded, otherwise every table becomes its
    own upstream.
    """
    out: dict[str, list[str]] = {}
    if not TRANSFORM_DIR.exists():
        return out
    for path in sorted(TRANSFORM_DIR.glob("*.sql")):
        target = re.sub(r"^\d+_", "", path.stem)
        text = path.read_text(encoding="utf-8", errors="replace")
        ups = {m.lower() for m in _RE_FROM.findall(text)}
        ups = {u for u in ups if u in known and u != target
               and u not in _FAKE_TABLES}
        if ups:
            out[target] = sorted(ups)
    return out


def discover_insights(known: set[str]) -> dict[str, dict[str, Any]]:
    """The 46 insight SQL files: which tables each reads, which report it belongs to, what it is for."""
    out: dict[str, dict[str, Any]] = {}
    if not SQL_DIR.exists():
        return out
    purpose = _readme_purpose()
    owner = _sql_owner()
    for path in sorted(SQL_DIR.glob("*.sql")):
        name = path.stem
        text = path.read_text(encoding="utf-8", errors="replace")
        tbls = {m.lower() for m in _RE_FROM.findall(text)}
        tbls = {t for t in tbls if t in known and t not in _FAKE_TABLES}
        reports = sorted(owner.get(name) or [])
        out[name] = {
            "stage": STAGE_INSIGHT,
            "tables": sorted(tbls),
            "reports": reports,
            "purpose": purpose.get(name, ""),
            "file": f"tools/sql/{path.name}",
            "source": "tools/sql (parse FROM/JOIN)",
        }
    return out


def _readme_purpose() -> dict[str, str]:
    """The `| SQL File | Used By | Purpose |` table in the README —— VLML's own documentation."""
    out: dict[str, str] = {}
    readme = SQL_DIR / "README.md"
    if not readme.exists():
        return out
    for line in readme.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\|\s*`([a-z0-9_]+)\.sql`\s*\|\s*`([^`]+)`\s*\|\s*([^|]+)\|", line)
        if m:
            out[m.group(1)] = m.group(3).strip()
    return out


def _sql_owner() -> dict[str, set[str]]:
    """Which report module loads this SQL (parse load_sql("xxx.sql") in the source)."""
    out: dict[str, set[str]] = {}
    module_to_report = {
        "scouting.py": "scouting_report",
        "player_profile.py": "player_profile_report",
        "match_analysis.py": "match_analysis_report",
        "data_fetch.py": "",      # shared utility, belongs to no single report
    }
    for path in list(REPORTS_DIR.glob("*.py")):
        report = module_to_report.get(path.name, "")
        for m in re.finditer(r'load_sql\(\s*"([a-z0-9_]+\.sql)"',
                             path.read_text(encoding="utf-8", errors="replace")):
            out.setdefault(Path(m.group(1)).stem, set())
            if report:
                out[Path(m.group(1)).stem].add(report)
    return out


# ---------------------------------------------------------------------------
# Tool → section (insight): parse the return structure of the report functions
# ---------------------------------------------------------------------------
def discover_tools() -> dict[str, dict[str, Any]]:
    """Which sections each report tool is composed of.

    Where this comes from: the key names inside the report function's `return {...}`
    are the sections; a section's value mostly comes from `_xxx()`, and which SQL those
    functions load internally gives the section → SQL ownership (matched by **the same
    function name**).
    """
    # Scan **all** `async def *_report(` under reports/ —— do not hardcode file names.
    # Hardcoding once missed pattern_detection_report (it lives in reports/pattern_detection.py),
    # so its sections stayed empty and the crucial piece of information
    # "pistol_win_rate reads the key_metrics section" could never be produced.
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(REPORTS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"async def ([a-z0-9_]+)\(", text):
            func = m.group(1)
            if not func.endswith("_report"):
                continue
            tool = func
            # Function body = up to the next top-level def (a hardcoded length misses long functions)
            nxt = re.search(r"\n(?:async def |def )", text[m.end():])
            body = text[m.end(): m.end() + (nxt.start() if nxt else 20000)]
            # It must be a **multi-line** return {: the `return {"error": ...}` at the top of the
            # function is an early-return error branch written on one line, not the report structure
            # we are after.
            rm = re.search(r"return\s*\{\s*\n", body)
            if not rm:
                continue
            depth, i = 1, rm.end()
            while i < len(body) and depth > 0:
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                i += 1
            block = body[rm.end(): i - 1]
            # Only top-level keys indented ≥8 (the sections); filter out meta fields
            # like report_type/version
            skip = {"report_type", "version", "series_id", "team_name",
                    "player_name", "map_name", "error"}
            sections = [s for s in re.findall(r'^\s{8,}"([a-z_]+)":', block, re.M)
                        if s not in skip]
            doc = re.search(r'"""(.*?)"""', text[:m.end()], re.S)
            returns = ""
            if doc:
                rdoc = re.search(r"Returns:\s*\n(.*?)(?:\n\s*\n|\"\"\")",
                                 doc.group(1), re.S)
                if rdoc:
                    returns = rdoc.group(1)
            out[tool] = {
                "stage": STAGE_TOOL,
                "sections": sections,
                "section_desc": dict(list(
                    dict(re.findall(r"-\s*([a-z_]+):\s*(.+)", returns)).items())[:12]),
                "source": f"tools/reports/{path.name} (parse the return structure)",
            }
    return out


_SKIP_CALLS = {
    "if", "for", "while", "return", "not", "and", "or", "in", "is", "len",
    "int", "str", "float", "bool", "round", "sorted", "list", "dict", "set",
    "min", "max", "sum", "abs", "print", "super", "lambda", "next", "zip",
    "enumerate", "isinstance", "getattr", "format", "range", "tuple",
}


def _func_graph(text: str) -> dict[str, dict[str, list[str]]]:
    """function → {SQLs loaded, functions called}.

    The **call relationships** must be stored too: key_metrics goes through two levels
    (focus_metrics → assemble_team_metrics → _team_economy_metrics → load_sql);
    recording only "who loaded SQL" yields an empty set —— the first attempt did miss
    key_metrics, and it is exactly the section pistol_win_rate / eco_win_rate live in.
    """
    out: dict[str, dict[str, list[str]]] = {}
    for m in re.finditer(r"(?:async\s+)?def ([a-z_0-9]+)\(", text):
        name = m.group(1)
        nxt = re.search(r"\n(?:async def |def |class )", text[m.end():])
        body = text[m.end(): m.end() + (nxt.start() if nxt else 20000)]
        sqls = sorted({Path(s).stem
                       for s in re.findall(r'load_sql\(\s*"([a-z0-9_]+\.sql)"', body)})
        calls = sorted({c for c in re.findall(r"([a-z_][a-z_0-9]*)\s*\(", body)
                        if c not in _SKIP_CALLS and c != name})
        out[name] = {"sqls": sqls, "calls": calls}
    return out


def _collect_sqls(fn: str, graph: dict[str, dict[str, list[str]]],
                  depth: int = 3, seen: set[str] | None = None) -> list[str]:
    """Expand recursively: which SQLs this function (including what it calls) uses in total."""
    seen = seen or set()
    if fn in seen or depth < 0:
        return []
    seen.add(fn)
    node = graph.get(fn) or {}
    out = list(node.get("sqls") or [])
    for c in node.get("calls") or []:
        out += _collect_sqls(c, graph, depth - 1, seen)
    return out


def section_insights() -> dict[str, dict[str, list[str]]]:
    """Tool → section → which insight SQLs compose it.

    The static chain (how VLML really writes it):
        return {"key_metrics": {"team": focus_metrics, ...}}
        focus_metrics = assemble_team_metrics(...)
        assemble_team_metrics() → _team_round_metrics / _team_economy_metrics ...
        _team_economy_metrics() → load_sql("team_economy_pistol.sql")

    So it takes three steps: the section's expression → variable → function → the SQLs it
    loads. Without this layer, "insight" is just a batch of nodes in the graph and never
    reaches the prompt.
    """
    result: dict[str, dict[str, list[str]]] = {}
    # The function graph must be built **across files**: in pattern_detection.py there is
    # `key_metrics = assemble_team_metrics(...)`, while assemble_team_metrics is
    # defined in match_analysis.py —— building the graph per file finds nothing, so
    # every section of pattern_detection_report ends up empty.
    graph: dict[str, dict[str, list[str]]] = {}
    texts: dict[str, str] = {}
    for path in sorted(REPORTS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        texts[path.name] = text
        for fn, node in _func_graph(text).items():
            slot = graph.setdefault(fn, {"sqls": [], "calls": []})
            slot["sqls"] = sorted(set(slot["sqls"]) | set(node["sqls"]))
            slot["calls"] = sorted(set(slot["calls"]) | set(node["calls"]))

    for fname, text in texts.items():
        for m in re.finditer(r"async def ([a-z0-9_]+)\(", text):
            tool = m.group(1)
            if not tool.endswith("_report"):
                continue
            nxt = re.search(r"\n(?:async def |def )", text[m.end():])
            body = text[m.end(): m.end() + (nxt.start() if nxt else 20000)]
            rm = re.search(r"return\s*\{\s*\n", body)
            if not rm:
                continue
            depth, i = 1, rm.end()
            while i < len(body) and depth > 0:
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                i += 1
            block = body[rm.end(): i - 1]
            # variable → function (focus_metrics = assemble_team_metrics(...))
            var2func = dict(re.findall(r"^\s*([a-z_0-9]+)\s*=\s*([a-z_0-9]+)\s*\(",
                                       body, re.M))
            sections: dict[str, list[str]] = {}
            for sm in re.finditer(r'^\s{8,}"([a-z_]+)":\s*(.*)$', block, re.M):
                sec, expr = sm.group(1), sm.group(2)
                if sec in ("report_type", "version", "series_id", "team_name",
                           "player_name", "map_name", "error"):
                    continue
                # Nested block ("scope": { newline …) → take everything up to the matching },
                # otherwise the expression is just "{" and no function call is visible.
                if expr.strip().endswith("{"):
                    depth, k = 1, sm.end(2)
                    while k < len(block) and depth > 0:
                        if block[k] == "{":
                            depth += 1
                        elif block[k] == "}":
                            depth -= 1
                        k += 1
                    expr = block[sm.end(2): k]
                # Both forms are needed: `_team_comparison(db, ...)` is a function call,
                # while `key_metrics,` is a **bare variable** (its value comes from an earlier
                # assignment); taking only the former misses cases.
                names = set(re.findall(r"([a-z_][a-z_0-9]*)\s*\(", expr))
                names |= set(re.findall(r"([a-z_][a-z_0-9]{3,})", expr))
                sqls: list[str] = []
                for n in sorted(names):
                    fn = var2func.get(n, n)
                    sqls += _collect_sqls(fn, graph)
                if sqls:
                    sections[sec] = sorted(set(sqls))
            if sections:
                result[tool] = sections
    return result


# --------------------------------------------------------------------------
# Modelling declarations: the modelling documents VLML **wrote itself**
# --------------------------------------------------------------------------
# Division of labour with `discover()`
# -------------------------------------
# `discover()` **queries the database / parses the source** → it yields "what is in the
# database right now" (column names, row counts, derivation relationships). Without a
# database connection it yields nothing —— measured: when the database was down all 21
# tables in the graph came out empty.
#
# But the study companion's knowledge points are not "queried live": its 457 nodes **carry**
# difficulty / question_types / typical_misconceptions / prerequisites, declared by the author.
# The same layer of "author declarations" in VLML is these four modelling documents under
# `database/`:
#
#   DATA_MODEL.md              each table's Grain + Use cases + core relationship chain
#   DERIVED_TABLES.md          the 7 derived tables' grain/upstream/purpose/key columns/sample queries
#   metadata/column_definitions.yaml   column-level conventions (141 lines)
#   DATA_DICTIONARY.json       pk / column types / metric formulas for 13 tables
#
# These are **files**, always present. Parsing them into structured declarations and writing
# them into the graph is what lets the graph actually take on the role the study companion's
# knowledge_seeds plays: the question generator reads the graph, not the database, and not
# the questions.
DATA_DIR = VLML_ROOT / "database"
MODEL_FILES = (
    DATA_DIR / "DATA_MODEL.md",
    DATA_DIR / "DERIVED_TABLES.md",
    DATA_DIR / "metadata" / "column_definitions.yaml",
    DATA_DIR / "DATA_DICTIONARY.json",
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _parse_core_lineage(text: str) -> dict[str, dict[str, Any]]:
    """DATA_MODEL.md's Core Relationships: series → games → rounds → base_events.

    This is VLML's **core lineage** (who is whose child table), the isomorphic counterpart
    of the study companion's `prerequisites`: to compute a round-level metric you must
    first have rounds; to compute an event-level one you must first have base_events.
    """
    m = re.search(r"##\s+Core Relationships\s*```(.*?)```", text, re.S)
    if not m:
        return {}
    out: dict[str, dict[str, Any]] = {}
    stack: list[tuple[int, str]] = []
    for raw in m.group(1).splitlines():
        if not raw.strip():
            continue
        nm = re.search(r"([a-z_][a-z_0-9]*)\s*\(", raw)
        if not nm:
            continue
        name = nm.group(1)
        keys: list[str] = []
        km = re.search(r"\(([^)]*)\)", raw)
        if km:
            keys = [k.strip() for k in km.group(1).split(",") if k.strip()]
        indent = len(raw) - len(raw.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        out[name] = {"parent": stack[-1][1] if stack else "", "keys": keys}
        stack.append((indent, name))
    return out


def _parse_data_model(text: str) -> dict[str, dict[str, Any]]:
    """`### Title (`tbl`)` + `**Grain:**` + `**Use cases:**`。"""
    out: dict[str, dict[str, Any]] = {}
    for m in re.finditer(
            r"^###\s+(.+?)\s*\(`([a-z_0-9]+)`\)\s*$(.*?)(?=^###\s|\Z)",
            text, re.S | re.M):
        tbl, body = m.group(2), m.group(3)
        rec: dict[str, Any] = {"title": m.group(1).strip()}
        gm = re.search(r"\*\*Grain:\*\*\s*(.+)", body)
        um = re.search(r"\*\*Use cases:\*\*\s*(.+)", body)
        if gm:
            rec["grain"] = gm.group(1).strip()
        if um:
            rec["use_cases"] = um.group(1).strip()
        if len(rec) > 1:
            out[tbl] = rec
    # Reference tables are in bullet form: "- `agent_roles`: one row per agent, role mapping"
    rm = re.search(r"##\s+Reference Tables(.*?)(?=^##\s|\Z)", text, re.S | re.M)
    if rm:
        for lm in re.finditer(r"^-\s+`([a-z_0-9]+)`:\s*(.+)$", rm.group(1), re.M):
            rec = out.setdefault(lm.group(1), {"title": lm.group(1)})
            rec.setdefault("use_cases", lm.group(2).strip())
    return out


def _parse_derived_tables(text: str) -> dict[str, dict[str, Any]]:
    """DERIVED_TABLES.md: the overview table (grain/upstream/purpose) + the details of each `###` block."""
    out: dict[str, dict[str, Any]] = {}
    for lm in re.finditer(
            r"^\|\s*`([a-z_0-9]+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|",
            text, re.M):
        tbl, grain, src, purpose = lm.groups()
        if tbl in ("Table", "table") or set(tbl) <= set("-: "):
            continue                       # header row
        out[tbl] = {"grain": grain.strip(), "upstream_raw": src.strip(),
                    "purpose": purpose.strip()}
    for m in re.finditer(r"^###\s+([a-z_0-9]+)\s*$(.*?)(?=^###\s|\Z)",
                         text, re.S | re.M):
        tbl, body = m.group(1), m.group(2)
        rec = out.setdefault(tbl, {})
        pm = re.search(r"\*\*Primary Key:\*\*\s*`?([^`\n]+)`?", body)
        if pm:
            rec["pk_text"] = pm.group(1).strip().strip("`")
        # The first prose paragraph under the heading = the purpose
        for line in body.splitlines():
            s = line.strip()
            if s and not s.startswith(("**", "-", "|", "```", "#")):
                rec.setdefault("use_cases", s)
                break
        kc = re.search(r"\*\*Key Columns:\*\*(.*?)(?=\*\*|\Z)", body, re.S)
        if kc:
            cols: dict[str, str] = {}
            # `\s+` would swallow the newline, so "- a, b - desc" would glue across lines
            # onto the next bullet (measured: the description of games_played turned into the
            # next line's column name) —— only eat spaces.
            for cm in re.finditer(r"^-\s+(.+?)[ \t]+-[ \t]+(.+)$",
                                  kc.group(1), re.M):
                for n in re.findall(r"`([a-z_0-9]+)`", cm.group(1)):
                    cols[n] = cm.group(2).strip()
            if cols:
                rec["key_columns"] = cols
        rc = re.search(r"\*\*Row count:\*\*\s*(.+)", body)
        if rc:
            rec["row_count_text"] = rc.group(1).strip()
        ex = re.search(r"```sql(.*?)```", body, re.S)
        if ex:
            rec["example_sql"] = ex.group(1).strip()
    return out


def _parse_column_yaml(path: Path) -> dict[str, dict[str, Any]]:
    """metadata/column_definitions.yaml: column-level conventions (handwritten by VLML, finer than the dictionary)."""
    try:
        import yaml
        raw = yaml.safe_load(_read(path)) or {}
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for tbl, body in (raw.get("tables") or {}).items():
        if not isinstance(body, dict):
            continue
        out[str(tbl)] = {
            "description": str(body.get("description") or ""),
            "columns": {str(k): str(v)
                        for k, v in (body.get("columns") or {}).items()},
        }
    return out


def _parse_data_dictionary(path: Path) -> dict[str, dict[str, Any]]:
    """DATA_DICTIONARY.json: pk / column types / flag / unit / **metric formulas**."""
    try:
        raw = json.loads(_read(path))
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for e in raw or []:
        tbl = str(e.get("table") or "")
        if not tbl:
            continue
        cols: dict[str, dict[str, Any]] = {}
        for c, v in (e.get("columns") or {}).items():
            if isinstance(v, dict):
                item = {k: v[k] for k in ("desc", "type", "unit")
                        if v.get(k)}
                if v.get("flag"):
                    item["flag"] = True
                cols[str(c)] = item
            elif v:
                cols[str(c)] = {"desc": str(v)}
        out[tbl] = {
            "grain": str(e.get("grain") or ""),
            "pk": [str(x) for x in (e.get("pk") or [])],
            "columns": cols,
            "metrics": {str(k): str(v)
                        for k, v in (e.get("metrics") or {}).items()},
        }
    return out


def _upstream_of(rec: dict[str, Any], parent: str, known: set[str]) -> list[str]:
    """Upstream tables: DERIVED_TABLES' Source column + the core parent table.

    `seed` is not an upstream (it means "handwritten seed data"), nor is the table itself.
    """
    got: list[str] = []
    for tok in re.split(r"[,\s]+", str(rec.get("upstream_raw") or "")):
        t = tok.strip().strip("`")
        if t and t != "seed" and t in known and t not in got:
            got.append(t)
    if parent and parent not in got:
        got.append(parent)
    return got


def _layer_of(name: str, core: dict[str, dict[str, Any]],
              derived: dict[str, Any]) -> str:
    """This table's role in the model (the isomorphic counterpart of the study companion's `chapter`)."""
    if name in core:
        return "core"                 # core: series / games / rounds / base_events
    if derived:
        return "derived"              # the 7 declared separately in DERIVED_TABLES.md
    if name.startswith(("agent_", "weapon_", "map_", "ability_", "ref_")):
        return "ref"                  # reference/dictionary table
    return "agg"


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(VLML_ROOT))
    except Exception:            # path not under the VLML root (e.g. it was redirected) → use the absolute path
        return str(p)


def model_specs() -> dict[str, Any]:
    """Parse the four modelling documents into structured declarations for each table.

    Returns `{"tables": {name: {...}}, "fingerprint": str, "files": [...]}`.
    The fingerprint is used to tell whether the cache is stale —— change a modelling document
    and the graph must be rebuilt, otherwise the new declarations never reach the already
    persisted `knowledge_graph.json`.
    """
    dm_p, dt_p = MODEL_FILES[0], MODEL_FILES[1]
    cd_p, dd_p = MODEL_FILES[2], MODEL_FILES[3]
    dm_text, dt_text = _read(dm_p), _read(dt_p)
    core = _parse_core_lineage(dm_text)
    model = _parse_data_model(dm_text)
    derived = _parse_derived_tables(dt_text)
    ycols = _parse_column_yaml(cd_p)
    ddic = _parse_data_dictionary(dd_p)

    names = set(core) | set(model) | set(derived) | set(ycols) | set(ddic)
    tables: dict[str, dict[str, Any]] = {}
    for n in sorted(names):
        a, b = model.get(n) or {}, derived.get(n) or {}
        c, d = ycols.get(n) or {}, ddic.get(n) or {}

        # Column conventions: the yaml is handwritten (finer), the dictionary fills in type/flag/unit
        coldesc: dict[str, dict[str, Any]] = {}
        for col, txt in (c.get("columns") or {}).items():
            coldesc[str(col)] = {"desc": str(txt)}
        for col, meta in (d.get("columns") or {}).items():
            cur = coldesc.setdefault(str(col), {})
            if meta.get("desc") and not cur.get("desc"):
                cur["desc"] = str(meta["desc"])
            for k in ("type", "flag", "unit"):
                if meta.get(k) is not None:
                    cur[k] = meta[k]
        # Derived tables are not in DATA_DICTIONARY (its 13 tables do not cover the 7 derived
        # tables), so column conventions can only come from DERIVED_TABLES.md's Key Columns ——
        # and agg_first_blood_stats is precisely the table questions lean on most, whose
        # `fb_team_won` convention is written right there.
        for col, txt in (b.get("key_columns") or {}).items():
            cur = coldesc.setdefault(str(col), {})
            if not cur.get("desc"):
                cur["desc"] = str(txt)
                cur["origin"] = "DERIVED_TABLES"
        coldesc = {k: v for k, v in coldesc.items() if v.get("desc")}

        pk: list[str] = []
        pkm = re.findall(r"[a-z_][a-z_0-9]*", str(b.get("pk_text") or ""))
        if pkm:
            pk = pkm
        elif d.get("pk"):
            pk = list(d["pk"])

        # Backticks are markdown's inline-code markers, not part of the grain
        grain = re.sub(r"`", "", str(
            a.get("grain") or b.get("grain") or d.get("grain") or "")).strip()
        tables[n] = {
            "title": str(a.get("title") or n),
            # Prefer the **full prose sentence** for the purpose ("Pre-joined first blood events with
            # round outcomes."); the overview table's "FB events + outcomes" is an abbreviation
            "purpose": (str(a.get("use_cases") or "")
                        or str(b.get("use_cases") or "")
                        or str(c.get("description") or "")
                        or str(b.get("purpose") or "")),
            "purpose_short": str(b.get("purpose") or ""),
            "description": str(c.get("description") or ""),
            "use_cases": (str(a.get("use_cases") or "")
                          or str(b.get("use_cases") or "")),
            "grain": grain,
            "pk": pk,
            "parent": str((core.get(n) or {}).get("parent") or ""),
            "keys": list((core.get(n) or {}).get("keys") or []),
            "upstream": _upstream_of(b, (core.get(n) or {}).get("parent", ""),
                                     names),
            "column_desc": coldesc,
            "metrics": dict(d.get("metrics") or {}),
            "key_columns": dict(b.get("key_columns") or {}),
            "example_sql": str(b.get("example_sql") or "")[:900],
            "row_count_text": str(b.get("row_count_text") or ""),
        # Layer: core (series→games→rounds→base_events) / derived (the 7 declared in
        # DERIVED_TABLES) / reference dictionary / everything else falls back to the naming
        # convention's aggregate table
            "layer": _layer_of(n, core, b),
        }

    h = hashlib.sha1()
    for p in MODEL_FILES:
        try:
            h.update(p.read_bytes())
        except Exception:
            h.update(str(p).encode("utf-8"))
    return {
        "tables": tables,
        "fingerprint": h.hexdigest()[:16],
        "files": [_rel(p) for p in MODEL_FILES],
    }


async def discover() -> dict[str, Any]:
    tables = await discover_tables()
    known = set(tables)
    return {
        "tables": tables,
        "lineage": discover_lineage(known),
        "insights": discover_insights(known),
        "tools": discover_tools(),
        "section_insights": section_insights(),
    }


def _main() -> int:
    argv = sys.argv[1:]
    data = asyncio.run(discover())
    if "--json" in argv:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    tables = data["tables"]
    print(f"{len(tables)} tables (information_schema queried live)")
    by_stage: dict[str, list[str]] = {}
    for name, info in tables.items():
        by_stage.setdefault(info["stage"], []).append(name)
    for st in STAGE_ORDER:
        if st not in by_stage:
            continue
        print(f"\n[{st}] {STAGE_LABEL.get(st, st)}")
        for name in sorted(by_stage[st]):
            t = tables[name]
            print(f"  {name:28} {t['grain']:14} {t['rows']:>7} rows "
                  f"{len(t['columns'])} cols")
    print(f"\nDerivation relationships (parsed from transformations)")
    for target, ups in sorted(data["lineage"].items()):
        print(f"  {target:28} ← {', '.join(ups)}")
    ins = data["insights"]
    print(f"\n{len(ins)} insights (parsed from tools/sql)")
    if "--tables" not in argv:
        for name, info in sorted(ins.items())[:12]:
            print(f"  {name:30} reads {', '.join(info['tables']) or '(none)'}"
                  + (f"  [{','.join(info['reports'])}]" if info["reports"] else ""))
    print(f"\n{len(data['tools'])} tools (reports' return structures parsed)")
    for name, info in sorted(data["tools"].items()):
        print(f"  {name:26} sections: {', '.join(info['sections'][:8])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
