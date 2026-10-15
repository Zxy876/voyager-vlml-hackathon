#!/usr/bin/env python3
"""Code-based evidence gathering: Voyager produces **code that calls MCP tools**, instead of transcribing numbers itself.

Why this module exists (the user's own words)
---------------------------------------------
"For evidence gathering, I believe the 'fetch' has to go back to Voyager × MCP: Voyager's layer of
practically calling tools, and Voyager × dashboard; Voyager's bypass learning part… for these you
should look at how the original Voyager produces the relevant JS operation code."

The original MineDojo/Voyager's approach (`voyager/agents/action.py`) is a complete chain:

  1. `render_system_message()` splices the **control primitives' source code** into the system prompt
     (`load_control_primitives_context(base_skills) + skills`);
  2. the model outputs per `action_response_format.txt`:
         Explain: ...
         Plan:
         1) ...
         Code:
         ```javascript
         // helper functions (only if needed, try to avoid them)
         async function yourMainFunctionName(bot) { ... }
         ```
  3. `process_ai_message()` uses a regex to extract the ```js block → **babel parses the AST** →
     walks `program.body` for `FunctionDeclaration` → takes the **last async function**
     as the main function → asserts its **sole parameter is named `bot`** → assembles
     `program_code` and `exec_code = f"await {name}(bot);"` (3 retries on parse failure);
  4. the execution result is produced by the **interpreter**, and the observation reads the events
     field back verbatim (`render_human_message`).

Isomorphic mapping onto MVE
---------------------------
| Original | MVE |
|---|---|
| `bot` (Mineflayer bot, the handle for acting on the world) | `mcp` (the VLML MCP tool handle) |
| ```javascript code block | ```python code block (the host language is Python) |
| `babel.parse`'s AST | `ast.parse` |
| the last `async function` | the last `async def` |
| assert the sole parameter name `bot` | assert the sole parameter name `mcp` |
| `exec_code = await name(bot);` | `await name(mcp)` |

Why this step fixes "incomplete evidence gathering"
---------------------------------------------------
The previous chain was "Voyager calls the tool → **the LLM reads the tool's return and transcribes it into facts** → grading reads facts".
The transcription step loses things: observed `eco_win_rate` didn't exist in the observation at all,
yet the model still submitted a `None`, and grading compared against it as if it were an answer —
recorded as "value incomparable".

After the code-based change: **values are fetched from the raw return by path in code** (`dig()`),
never passing through the LLM's mouth.

    async def collect(mcp):
        s = await mcp.match_summary_report(series_id="2843069", team_name="Cloud9")
        p = await mcp.pattern_detection_report(series_id="2843069")
        return {"eco_win_rate": dig(p, "key_metrics.economy.eco.num"), ...}

A `None` in the return value means **not fetched** (the tool really doesn't have that field),
not "the model missed it while copying". That's exactly the line in 《取证通路-原版Voyager对照.md》:
**the original sends "the original document" into grading, while MVE was sending "a transcript"**.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import vlml_env  # noqa: E402  bootstrap the environment

# --------------------------------------------------------------------------
# MCP handle: the 10 externally exposed tools (MCP tool name == Python function name,
# sole exception: externally query_sql / implemented as execute_custom_sql, see vlml_env.py:65-67)
# --------------------------------------------------------------------------
_IMPL_NAME = {"query_sql": "execute_custom_sql"}

# The 10 tools MCP exposes externally (the @mcp.tool list in vlml/server.py)
TOOL_NAMES = [
    "match_summary_report", "match_analysis_report", "match_players_report",
    "match_rounds_report", "match_economy_report", "pattern_detection_report",
    "player_profile_report", "scouting_report", "query_sql", "get_database_info",
]

SERIES = "2843069"
C9 = "Cloud9"
# Map-name anchor. There used to be only SERIES / C9, so the model **hard-coded** map names in SQL —
# after a database switch that map doesn't exist at all, and skill reuse runs 0 rows every time
# (see the observed behavior of `stale_literals`).
try:                                                     # pragma: no cover
    import anchor as _anchor
    MAP = str(_anchor.map_name() or "")
    SERIES = str(_anchor.series() or SERIES)
    C9 = str(_anchor.team() or C9)
except Exception:
    MAP = ""


def _tool_signatures() -> dict[str, str]:
    """Reflect signatures from the real functions — **never hand-write them**.

    Observed consequence of hand-writing signatures: I wrote `pattern_detection_report` as
    `(series_id, team_name=None)`, while the real signature is
    `(team_name=None, player_name=None, tournament_name=None,
      series_ids=None, min_rounds=200)` — the parameter names don't match at all,
    the probe call throws TypeError outright, the structural docs can't be fetched, and the model
    is left to keep guessing.
    """
    import inspect
    out: dict[str, str] = {}
    for name in TOOL_NAMES:
        fn = getattr(vlml_env, _IMPL_NAME.get(name, name), None)
        if fn is None:
            continue
        out[name] = f"{name}{inspect.signature(fn)}"
    return out


# Reflect once at module import (vlml_env is imported above)
TOOL_SIGNATURES: dict[str, str] = _tool_signatures()


class MCPHandle:
    """The tool handle for the model's code (the isomorphic counterpart of the original's `bot`).

    Every method is async and returns the tool's **raw return** (no processing, no summarizing).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def _fn(self, name: str) -> Any:
        return getattr(vlml_env, _IMPL_NAME.get(name, name), None)

    def __getattr__(self, name: str) -> Any:
        if name not in TOOL_SIGNATURES:
            raise AttributeError(f"MCP has no tool {name}")
        fn = self._fn(name)
        if fn is None:
            raise AttributeError(f"tool {name} not loaded")

        async def _call(**kwargs: Any) -> Any:
            try:
                res = await fn(**kwargs)
            except TypeError as e:
                # Wrong parameter names must be surfaced — the original relies on this to make
                # the model correct itself next round
                self.calls.append({"tool": name, "args": kwargs,
                                   "error": f"bad arguments {e}"})
                raise
            self.calls.append({"tool": name, "args": kwargs, "ok": True})
            return res

        return _call


# --------------------------------------------------------------------------
# prompt (mirroring the structure of the original's action_response_format.txt)
# --------------------------------------------------------------------------
# The main function name must **not** be a placeholder like `your_main_function_name`: observed,
# the model copies placeholder names verbatim (saved into the skill library as
# `your_main_function_name`, and at review time you can't tell which question's solution it is).
# Give a concrete example name and the model will invent a real one.
RESPONSE_FORMAT = """Explain: one or two sentences on how you plan to fetch these numbers
Plan:
1) ...
2) ...
...
Code:
```python
# helper functions (only if needed, try to avoid them)
...
# main function after the helper functions
async def collect_stats(mcp):
    # correct usage example: first get the tool's return, then use dig to fetch by **dotted path**
    p = await mcp.<tool_name>(<args>)
    return {
        "<dimension_name>": dig(p, "<path copied from the structural docs>"),
    }
```
"""


# --------------------------------------------------------------------------
# Tool return structure docs (the isomorphic counterpart of the original's control_primitives)
# --------------------------------------------------------------------------
# The original splices the control primitives' **source code** into the system prompt, so the model
# knows what methods exist on `bot` and what they return. If MVE doesn't do the same, the model can
# only **guess** the field names in the tool returns — observed consequence: all four dimensions
# dug to None, because the guessed path (`economy.pistol_win_rate`) is nothing like the real path
# (`key_metrics.economy.pistol.num`).
#
# This isn't "leaking the answer": what's given is the **structure** (which field is where), not the **value**.
# In real scenarios an agent has API docs, and the original Voyager provides them too.
SCHEMA_CACHE = HERE / "tool_schemas.json"

# Extraction limit (the full skeleton written into the cache).
# Hit this in practice: this used to be 60, and `match_analysis_report` has exactly 61 entries, so
# `key_metrics.team.consistency.kast.num` **was cut at the extraction stage** —
# the model never sees the correct path in the prompt, and can only pick from the visible 60 one
# that looks similar (`team_comparison.Cloud9.kast.num`, which even drops consistency),
# and three critic retries just circle on the same wrong path. The extraction stage must not cut.
_MAX_PATHS = 400
_MAX_DEPTH = 6

# Display budget (how many entries enter the prompt). Keeping everything in extraction ≠ showing everything:
# cramming all 61 entries into the prompt buries the correct one among irrelevant branches like
# `team_comparison.NRG.*` (observed: the model was led astray by exactly those). So focus on
# "relevant to this question's dimensions".
_RENDER_BUDGET = 60   # 一个维度都没命中时的兜底展示条数
_FOCUS_BUDGET = 40    # 命中维度时的聚焦展示条数
_SIBLINGS_PER_PARENT = 8   # 同父兄弟的配额


def _leaf_paths(obj: Any, prefix: str = "", depth: int = 0,
                out: list[str] | None = None) -> list[str]:
    out = out if out is not None else []
    if len(out) >= _MAX_PATHS or depth > _MAX_DEPTH:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)) and v:
                _leaf_paths(v, p, depth + 1, out)
            else:
                if p not in out:
                    out.append(p)
    elif isinstance(obj, list) and obj:
        _leaf_paths(obj[0], f"{prefix}.0", depth + 1, out)
    return out


def load_schemas() -> dict[str, list[str]]:
    try:
        raw = json.loads(SCHEMA_CACHE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# "Focused trimming" of the structural docs: pick out the correct paths, rather than truncating the whole return skeleton
# --------------------------------------------------------------------------
# The original Voyager doesn't need this step — control_primitives is a few dozen small functions,
# so giving them all is fine. MVE is different: `match_analysis_report` alone has 61+ leaf paths;
# giving them all buries the correct one, while truncating at a fixed count cuts the correct one
# (both directions have crashed in practice). So search by **dimension name**: hits first →
# add same-parent siblings (so "both kinds mixed at the same level" is visible) →
# add the top-level skeleton (so the overall shape is comprehensible) → only leftover budget goes
# to other branches.
_STOP_TOKENS = {"pct", "rate", "ratio", "avg", "mean", "total", "num", "denom",
                "count", "value", "index", "score", "check", "analysis"}


def _tokens(dim: str) -> list[str]:
    """Dimension name → words for matching. Strip convention suffixes, keep only the metric core.

    `kast_pct` → ["kast"], `kd_ratio` → ["kd"] — `pct`/`ratio` are conventions, not
    metric names; keeping them matches a swath of irrelevant paths.
    """
    parts = re.split(r"[^a-z0-9]+", str(dim or "").lower())
    return [p for p in parts if len(p) >= 2 and p not in _STOP_TOKENS]


def _stem_of(path: str) -> str:
    """Strip the num/denom convention suffix, keep the metric core: `....kast.num` → `....kast`."""
    for suf in (".num", ".denom"):
        if path.endswith(suf):
            return path[: -len(suf)]
    return path


def _uses_declared(used: list[str], declared: str) -> bool:
    """Whether the model fetched per the declared convention.

    For a percentage metric the declaration is `...kast.num`; the model writing
    `pct(p, "...kast.num")` counts as a hit, and writing `pct(p, "...kast")` letting pct derive
    denom itself also counts (both compare down to the stem).
    """
    stem = _stem_of(declared)
    return any(u == declared or _stem_of(u) == stem for u in used)


def _same_metric(a: str, b: str) -> bool:
    """Whether two paths point to the same metric (`kd` and `kd_ratio` count as the same)."""
    ta = set(_tokens(_stem_of(a).rsplit(".", 1)[-1]))
    tb = set(_tokens(_stem_of(b).rsplit(".", 1)[-1]))
    return bool(ta & tb)


def _candidates(schemas: dict[str, list[str]], dims: list[str],
                limit: int = 12) -> list[str]:
    """For critic feedback: paths that really exist in the return and whose names contain the dimension's words."""
    out: list[str] = []
    for d in dims:
        toks = _tokens(d)
        if not toks:
            continue
        for paths in schemas.values():
            for p in paths:
                if p in out:
                    continue
                if any(t in p.lower() for t in toks):
                    out.append(p)
    return out[:limit]


def _parent(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


def focus_paths(paths: list[str], dims: list[str] | None,
                prefer: list[str] | None = None,
                budget: int = _FOCUS_BUDGET) -> tuple[list[str], list[str]]:
    """Split paths into (dimension-relevant, the rest).

    How the two segments are displayed is up to the caller — the relevant segment must be given
    **in full**, while the rest is given only when budget allows (it's the source of distractors).
    `prefer` is the question's declared convention paths, always sorted first and starred.
    """
    if not paths:
        return [], []
    toks: list[str] = []
    for d in (dims or []):
        for t in _tokens(d):
            if t not in toks:
                toks.append(t)
    if not toks:
        return [], list(paths)

    hit = [p for p in paths if any(t in p.lower() for t in toks)]
    if not hit:
        return [], list(paths)

    # Same-parent siblings: let the model see the mixing of "under the same parent node kast is
    # num/denom while kd is a scalar". Giving only the hit line, the model still applies kast's
    # pattern to kd. Cap at 8 per parent: uncapped, a single parent like `player_performance.0`
    # can pull in 20 player fields, drowning out the few actually needed.
    by_parent: dict[str, list[str]] = {}
    for p in paths:
        by_parent.setdefault(_parent(p), []).append(p)
    sibs: list[str] = []
    for par in {_parent(p) for p in hit}:
        for p in by_parent.get(par, [])[:_SIBLINGS_PER_PARENT]:
            if p not in sibs:
                sibs.append(p)
    # Top-level skeleton (depth<=1): let the model know roughly what the return looks like,
    # so it doesn't invent paths with no root
    head = [p for p in paths if p and "." not in p][:8]

    focus: list[str] = []
    for p in hit + sibs + head:
        if p not in focus:
            focus.append(p)
    # The question's declared conventions sort to the front: whichever line it sits at in the doc,
    # the model writes to that line — observed: when the declared path sat after
    # `team_comparison.Cloud9...`, the model picked the latter.
    if prefer:
        focus = [p for p in prefer if p in focus] + \
                [p for p in focus if p not in set(prefer)]
    rest = [p for p in paths if p not in set(focus)]
    return focus[:budget], rest


_FAIL_TAG = "<probe failed"


def _is_failed(v: list[str] | None) -> bool:
    """Is this cache entry a **failure placeholder** (not a real structure)?"""
    return bool(v) and str(v[0]).startswith(_FAIL_TAG)


async def probe_schemas(tools: list[str], *, force: bool = False,
                        stats: dict[str, list[str]] | None = None
                        ) -> dict[str, list[str]]:
    """Call each tool once, extract the **structural skeleton** of its return (leaf paths, no values), and cache it.

    `stats` (optional) is backfilled with this round's real results, in three buckets:
      `probed_ok` really probed this round / `failed` failed this round with no old value to keep /
      `retained` failed this round, **kept the old cache** (in a database-switch scenario this is
      the old database's structure).

    ⚠️ Why `retained` must be split out separately (observed 2026-10-14, I hit this myself):
      "failure doesn't overwrite success" preserves good data, but if the caller judges success
      only by "is there a value", the retained old value gets treated as "obtained this round" —
      observed printing `✓ match_analysis_report 61 entries`, when those 61 were probed under the
      **old database's** old limit (60), this round actually failed, and the new database really
      has 306. ✓ calling "retained" "obtained" is textbook self-deception.

    ⚠️ Two "no self-deception" points that must be held (both observed on the server on
    2026-10-14, both hit in practice):

    1. **A tool returning error ≠ having probed the structure**. Previously only thrown exceptions
       were caught, so `{"error": "No data found for series 1058"}` was treated as a normal return,
       extracting a single leaf path `["error"]` into the cache — the CLI counted 1 entry and
       marked ✓, with 10/10 observed "successes" of which 6 were actually just one error line.
       So here `res["error"]` is explicitly recognized and recorded as a failure.

    2. **Failure must not overwrite success**. Those 6 failures above overwrote a 22 KB real
       structure cache (38~161 entries per tool) with garbage — and while it's regenerable,
       rebuilding requires a database connection. So on `force` re-probing, if a tool fails this
       round but the **old cache is good**, keep the old one. When everything fails with no old
       value to keep, don't persist at all (writing garbage is worse than writing nothing).
    """
    cache = load_schemas()
    old = dict(cache)          # keep a base copy, for "failure doesn't overwrite success"
    todo = [t for t in tools if force or t not in cache]
    if not todo:
        return {t: cache[t] for t in tools if t in cache}

    if stats is not None:
        stats.clear()
        for k in ("probed_ok", "retained", "failed"):
            stats.setdefault(k, [])

    handle = MCPHandle()
    for t in todo:
        # Fill default args per the **real signature**: series_id / series_ids / team_name each differ,
        # and judging by "does the string contain series_id" goes wrong (series_ids contains that
        # substring too).
        args = _default_args(t)
        try:
            fn = getattr(handle, t)
            res = await fn(**args)
            if isinstance(res, dict) and res.get("error"):
                # The probe args (taken from the anchors) find nothing in the current database —
                # no structure obtainable, record a failure.
                raise RuntimeError(str(res["error"])[:120])
            cache[t] = _leaf_paths(res if isinstance(res, dict) else {"result": res})
            if stats is not None:
                stats["probed_ok"].append(t)
        except Exception as e:
            if force and t in old and not _is_failed(old[t]):
                cache[t] = old[t]      # 这次没探到，但旧的还是好的 → 留旧的
                if stats is not None:
                    stats["retained"].append(t)
                continue
            cache[t] = [f"{_FAIL_TAG}: {type(e).__name__}: {str(e)[:120]}>"]
            if stats is not None:
                stats["failed"].append(t)

    # Total wipeout (with no old value to keep) → don't persist; don't overwrite a good cache with failure placeholders
    if todo and all(_is_failed(cache.get(t)) for t in todo):
        return {t: cache[t] for t in tools if t in cache}

    SCHEMA_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    return {t: cache[t] for t in tools if t in cache}


def _sample_player() -> str:
    """Fetch a real player name from the **current database** to use as the probe's argument.

    ⚠️ Why an empty string won't do (observed after switching to rib on 2026-10-13):
      `player_profile_report`'s player_name is required, and the probe used to fill `""`,
      so the tool directly returned `{'error': 'No series found for player '}` —
      an empty string isn't "not passed", it's passing a player that can't be found. On the GRID
      database it happened to return a structure (probably via a fallback branch), but on the rib
      database it errors, so rib's schema rebuild came out 9/10, with `player_profile_report`
      only able to retain the old database's structure.
      Only a real value gets the structure — same reasoning as team_name=C9 / series_id=SERIES:
      probe arguments must be **entities that exist in the database**.
    """
    try:
        import duckdb
        import db_switch
        con = duckdb.connect(db_switch.current_path(), read_only=True)
    except Exception:
        return ""
    try:
        have = {t[0] for t in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main'").fetchall()}
        for t in ("agg_player_series_stats", "ext_player_game_stats",
                  "ext_player_round_stats", "ext_player_side_stats"):
            if t not in have:
                continue
            try:
                row = con.execute(
                    f"SELECT player_name FROM {t} "
                    "WHERE player_name IS NOT NULL AND player_name<>'' "
                    "LIMIT 1").fetchone()
            except Exception:
                continue
            if row and row[0]:
                return str(row[0])
        return ""
    finally:
        try:
            con.close()
        except Exception:
            pass


def _default_args(tool: str) -> dict[str, Any]:
    """Fill probe default values per the real parameter names (following inspect.signature, not string guessing)."""
    import inspect
    fn = getattr(vlml_env, _IMPL_NAME.get(tool, tool), None)
    if fn is None:
        return {}
    params = inspect.signature(fn).parameters
    args: dict[str, Any] = {}
    for name, p in params.items():
        required = p.default is inspect._empty
        # The probe's goal is to **get the return structure**, so optional parameters need
        # meaningful values too: observed, pattern_detection_report has defaults for all
        # parameters, and passing none returns an error (no structure obtainable); the referee
        # passes team_name=C9, so follow that.
        if name == "team_name":
            args[name] = C9
        elif name == "series_id":
            args[name] = SERIES
        elif name == "player_name" and required:
            # Required → give a real player from the current database (an empty string gets
            # reported as "player not found" with an error, see _sample_player's observed note)
            args[name] = _sample_player()
        elif name == "sql_query":
            args[name] = "SELECT 1"
        elif required:
            args[name] = SERIES if "series" in name else ""
    return args


def render_tool_docs(schemas: dict[str, list[str]],
                     dims: list[str] | None = None,
                     prefer: list[str] | None = None) -> str:
    """Splice the tool return structure into the prompt — **focused by dimension, no longer truncated at a fixed count**."""
    if not schemas:
        return ""
    stars = set(prefer or [])
    blocks = []
    for t, paths in schemas.items():
        focus, rest = focus_paths(paths, dims, prefer)
        shown = "\n".join(f"    {'★ ' if p in stars else '  '}{p}"
                          for p in focus[:_FOCUS_BUDGET])
        if focus:
            head = (f"await mcp.{TOOL_SIGNATURES.get(t, t)} return structure"
                    f" —— **paths relevant to this question's dimensions** (★ = the question's "
                    f"declared convention, which must be used):")
            blocks.append(f"{head}\n{shown}")
            if rest:
                # Other branches get only a count hint, not a full listing: a full listing is
                # a distractor (observed: `team_comparison.NRG.*` led the model astray).
                blocks.append(
                    f"    (plus {len(rest)} paths irrelevant to this question's dimensions, "
                    f"mostly opponent/other branches — do not use)")
        else:
            shown = "\n".join(f"    {p}" for p in paths[:_RENDER_BUDGET])
            blocks.append(f"await mcp.{TOOL_SIGNATURES.get(t, t)} return structure"
                          f" (leaf paths, {len(paths)} total, showing the first "
                          f"{min(len(paths), _RENDER_BUDGET)}):\n{shown}")
    # Leaf paths alone aren't enough — the original's control_primitives contain **usage examples**,
    # and the model only writes correctly by following them. Hit this in practice: query_sql's return
    # is {"columns": [...], "rows": [[...]]}, yet the model wrote res["rounds"],
    # and all three retries died on the same KeyError, 0% for the whole question.
    if "query_sql" in schemas:
        blocks.append(
            "⚠️ query_sql's return is **not** a dict organized by table name; it is always:\n"
            "    {\"columns\": [\"col1\", \"col2\"], \"rows\": [[v1, v2], [v3, v4]]}\n"
            "    fetching values must go through rows:\n"
            "        res = await mcp.query_sql(sql_query=\"SELECT ...\")\n"
            "        first_row = res[\"rows\"][0]        # the first row\n"
            "        value    = first_row[0]            # column 0\n"
            "    do not write res[\"rounds\"] / res[\"rows\"][0][\"cnt\"] — rows holds **arrays**, not dicts.")
    return "\n\n".join(blocks)


def graph_hints(dims: list[str] | None, task: Any = None) -> str:
    """The knowledge graph's "dimension → produced by which tool + fetched via which path".

    Without it the model picks the wrong tool (observed: economy-type dimensions went to
    match_economy_report, while the graph declares they're produced by pattern_detection_report).
    """
    if not dims:
        return ""
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception:
        g = None
    lines = []
    for d in dims:
        prod = list(g.who_produces(d)) if g else []
        # Must give the **value_path**, not just the tool name.
        # Hit this in practice: saying only "kast_pct is produced by match_analysis_report", the model
        # picked a similar-looking path from the return structure `team_comparison.Cloud9...`,
        # while the true convention is in `key_metrics.team...` — picking the wrong branch digs to None.
        # Giving paths is "giving structure" not "giving values" (the graph has always given them),
        # so it doesn't count as leaking the answer.
        node = g.nodes.get(f"dim:{d}") if g else None
        vp = str((node.detail.get("value_path") if node else "") or "")
        if not prod or not vp:
            # Fallback when the graph lacks this dimension: **the question's own rubric is the
            # authoritative convention source**. The graph is tasks.py's cache (`build()` reads
            # answer_spec), and a stale cache shouldn't send the model back to guessing paths —
            # observed: kast_adr_check's hint segment was entirely empty because the graph wasn't
            # rebuilt, and the model retried on the wrong path for three rounds straight.
            spec = _spec_of(task, d)
            if spec:
                prod = prod or [spec[0]]
                vp = vp or spec[1]
        line = f"    {d} → call {' or '.join(prod)}" if prod else ""
        if vp:
            line += (f", fetch by path {vp}"
                     + (" (percentage: a sibling .denom at the same level serves as the denominator)"
                        if spec_percent(task, d)
                        else " (scalar: whatever dig returns is the final value, do not divide again)"))
        if line:
            lines.append(line)
    if not lines:
        return ""
    return "\n\nknowledge graph declaration (which tool produces each dimension):\n" + "\n".join(lines)


def declared_paths(task: Any, dims: list[str]) -> dict[str, str]:
    """The question's declared conventions: dimension → fetch path.

    The authoritative source is the knowledge graph (built from tasks.py's answer_spec); when the
    graph lacks a dimension, read the question's own rubric directly — observed: after
    `kast_adr_check` was added to tasks.py the graph wasn't rebuilt, the hint segment was entirely
    empty, and the model retried on the wrong branch for three rounds straight.

    Why this must be a **hard constraint** rather than a hint: in `match_analysis_report`,
    `team_comparison.*.consistency` is a **placeholder-value** branch (Cloud9's and NRG's
    kd_ratio are both 1.0, their adr denominators both 59), while the authoritative section
    `key_metrics.team` is really 1.24 / 109. Both paths "return a value", and looking only at
    "empty or not" can't tell them apart at all — so the declared convention must be used to
    filter branches, not merely to suggest.
    """
    out: dict[str, str] = {}
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception:
        g = None
    for d in dims:
        vp = ""
        if g is not None:
            node = g.nodes.get(f"dim:{d}")
            vp = str((node.detail.get("value_path") if node else "") or "")
        if not vp:
            spec = _spec_of(task, d)
            vp = spec[1] if spec else ""
        if vp:
            out[d] = vp
    return out


def _spec_of(task: Any, dim: str) -> tuple[str, str] | None:
    """Read (tool, value_path) from the **question's own rubric** — no reliance on the graph cache."""
    for p in (getattr(task, "rubric", None) or []):
        if str(getattr(p, "dimension", "") or "") != dim:
            continue
        s = getattr(p, "answer_spec", None)
        if s is None:
            continue
        tool = str(getattr(s, "tool", "") or "")
        vp = str(getattr(s, "value_path", "") or "")
        if tool and vp:
            return tool, vp
    return None


def spec_percent(task: Any, dim: str) -> bool:
    for p in (getattr(task, "rubric", None) or []):
        if str(getattr(p, "dimension", "") or "") != dim:
            continue
        s = getattr(p, "answer_spec", None)
        if s is not None and bool(getattr(s, "percent", False)):
            return True
    return False


def render_system_message(tool_docs: str = "", dim_hints: str = "",
                          skills_text: str = "") -> str:
    """Mirroring the original's `render_system_message`: splice the available "control primitives" source code into the system.

    The original splices in `load_control_primitives_context(base_skills) + skills`
    (`voyager/agents/action.py`) — **both**: first the stable API (primitives),
    then the **source code of already-learned skills**. Previously this had only the primitives half
    (tool_docs + graph_hints), with the solution source learned into the skill library never coming
    in, so "learning" and "using" were cut into two pieces: source learned via the bypass sat in
    the skill library, invisible when writing code.

    `dim_hints` is the graph's "which dimension is produced by which tool" — the original has no
    such item (it's expressed via JS function names in the skill library); MVE has the graph, so
    give it directly and save the model from picking the wrong tool.
    """
    sigs = "\n".join(f"await mcp.{s}" for s in TOOL_SIGNATURES.values())
    return f"""You are a data-analysis agent that gathers evidence by writing code that calls MCP tools.

Available MCP tools (all async, returning dict):
{sigs}
{tool_docs}

Value-fetching helper functions are ready (**two entry points split by metric type, do not mix them**):
    scalar(obj, "a.b.c")              —— **scalar** (e.g. kd = 1.24): whatever dig returns is the final value
    pct(obj, "a.b.num", "a.b.denom")  —— **percentage** (e.g. kast): returns {{"num":..,"denom":..}}
    dig(obj, "a.b.0.c")               —— generic dotted-path fetch, returns None when not found

⚠️ First judge whether the metric is scalar or percentage, then decide which to use:
   The two are often mixed under the same parent node (observed: under consistency, kast is num/denom
   while kd is a scalar). Passing a scalar path to pct() gets denom=None — switch to scalar() then.
   **Do not divide out the current metric using another metric's num/denom** — that's fetching wrong,
   not computing right.

**Paths must be copied verbatim from the return structure above**; do not invent field names.

⚠️ Fetching values may only use dig — **do not write obj.get("a.b.c")**: a dotted path is not a key,
and dict keys contain no dots. Hit this in practice: the model wrote
`r.get("key_metrics.economy.eco.num", 0)`, and when get failed it fell back to the default 0,
so what was submitted was **0 rather than "not fetched"** — which is worse than leaving None,
because it disguises "not fetched" as "fetched, value is 0".

Predefined constants (use directly, do not redefine them yourself):
    SERIES = "{SERIES}"      # this series' series_id
    C9     = "{C9}"          # team name
    MAP    = "{MAP}"         # map name
{dim_hints}
{skills_text}
Hard requirements:
1. The main function must be async, and its **sole parameter must be named mcp**;
2. Return a dict via return, with keys being dimension names and values being the **original values fetched** from tool returns;
3. **Do not hand-write numbers in the code** — all values must come from tool returns;
4. If it can't be fetched, let it return None — that means "this dimension isn't in the observation",
   which is far more useful than making up a number;
5. **Entity values must always use the constants above, never hard-coded literals** — this is the
   watershed of "can the skill be reused". The original Voyager's skill functions take parameters
   (`mineBlock(bot, name, count)`, with values passed at the call site), so the same skill works with
   a different material; hard-coding the value degrades the skill into "the answer to that one time".
   Observed: hard-coding in a skill
   `series_id=2843069 / winning_team_name='Cloud9' / map_name='Lotus'`, after a database switch
   none of these values exist → every reuse returns 0 rows → exam coverage falls back to 0
   (dashboard: baseline exam 16% → final exam 0%).
   Correct form (splicing into SQL via f-string):
       f"... WHERE series_id='{{SERIES}}' AND map_name='{{MAP}}'"
   Wrong form: `... WHERE series_id='2843069'`

Output format:
{RESPONSE_FORMAT}"""


def render_human_message(*, task: Any = None, code: str = "", error: str = "",
                         critique: str = "", missing: list[str] | None = None,
                         dims: list[str] | None = None) -> str:
    """照原版 `render_human_message`：缺哪项就显式写 None，不留想象空间。"""
    lines = [
        f"任务：{getattr(task, 'question', '') or '（无）'}",
        "",
        f"需要覆盖的维度：{', '.join(dims or []) or '（无）'}",
        f"上一轮没取到的维度：{', '.join(missing or []) or 'None'}",
        "",
        f"上一轮的代码：\n{code}" if code else "上一轮的代码：No code in the first round",
        "",
        f"执行错误：\n{error}" if error else "执行错误：No error",
        "",
        f"Critique：{critique}" if critique else "Critique：None",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 解析（原版 process_ai_message 的同构物）
# --------------------------------------------------------------------------
_CODE_BLOCK = re.compile(r"```(?:python|py)(.*?)```", re.DOTALL)


def is_empty(value: Any) -> bool:
    """维度算不算"没取到"。

    pct() 的返回是 {"num","denom"} 两个数，**两个都空才算没取到**；
    只有一个为空是另一回事（口径上分母本来就可能不存在），不能当成取错。
    """
    if value is None:
        return True
    if isinstance(value, dict):
        keys = {"num", "denom"}
        if keys & set(value):
            return all(value.get(k) is None for k in keys & set(value))
        return len(value) == 0
    if isinstance(value, str):
        return not value.strip()
    return False


def paths_in_code(code: str) -> list[str]:
    """把模型代码里写过的取值路径抽出来（AST，不靠正则）。

    用途：critic 回灌时要能指着说"你用的这条路径不在返回里"。
    只能说"路径不对"而不给候选，模型就只能原地重试 —— 实测三轮回灌，
    它三次交出一模一样的 `team_comparison.Cloud9.kast.num`：
    它不知道还有别的路可走。
    """
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("dig", "pct", "scalar"):
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    if a.value not in out:
                        out.append(a.value)
    return out


class CodeParseError(ValueError):
    """解析失败。必须抛出来让模型下一轮改正，不能静默降级。"""


def parse_code(text: str) -> dict[str, Any]:
    """抽代码块 → ast 解析 → 找主函数 → 断言参数名 → 生成 exec_code。

    与原版 `process_ai_message` 一一对应，只是 AST 库换成 `ast`。
    """
    blocks = _CODE_BLOCK.findall(text or "")
    code = "\n\n".join(b.strip() for b in blocks if b.strip())
    if not code:
        # 宽容一次：没有围栏时把整段当代码（原版只认围栏，这里略放宽）
        code = (text or "").strip()
    if not code:
        raise CodeParseError("没有找到 ```python 代码块")

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise CodeParseError(f"语法错误 {e}") from e

    funcs = [n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not funcs:
        raise CodeParseError("没有找到任何函数定义")

    # 找最后一个 async 函数作主函数（照原版 "find the last async function"）
    main = None
    for f in reversed(funcs):
        if isinstance(f, ast.AsyncFunctionDef):
            main = f
            break
    if main is None:
        raise CodeParseError("主函数必须是 async def（原版：Your main function must be async）")

    params = [a.arg for a in main.args.args]
    if params != ["mcp"]:
        raise CodeParseError(
            f"主函数 {main.name} 必须只接受一个名为 mcp 的参数，实际是 {params}"
            "（原版：must take a single argument named 'bot'）")

    return {
        "program_code": code,
        "program_name": main.name,
        "exec_code": f"await {main.name}(mcp)",
    }


_SAFE_BUILTINS = {
    "abs": abs, "min": min, "max": max, "sum": sum, "len": len, "round": round,
    "int": int, "float": float, "str": str, "bool": bool, "list": list,
    "dict": dict, "set": set, "tuple": tuple, "sorted": sorted, "range": range,
    "enumerate": enumerate, "zip": zip, "isinstance": isinstance, "print": print,
}


def dig(obj: Any, path: str) -> Any:
    """按点分路径取值（与 loop_core.dig 同一份语义）。"""
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, (list, tuple)) and part.isdigit():
            i = int(part)
            if i >= len(cur):
                return None
            cur = cur[i]
        else:
            return None
    return cur


def scalar(obj: Any, path: str) -> Any:
    """标量指标：dig 到的就是最终值，不用再算。

    与 `pct` 分开成两个函数，是刻意的——实测踩过一次，代价是连着 48 轮停在 50%：
    `key_metrics.team.consistency` 下 kast 是 {num,denom}、kd 是标量 1.24，
    两者长得太像，模型把 kast 的 num/denom 套路套到标量 kd 上，交出 109/109=1.0。
    只靠注释说"这个不是 num/denom"挡不住；**类型层面分开两个入口才挡得住**——
    标量路径传给 pct() 拿不到 denom，模型就只能改调 scalar()。
    """
    return dig(obj, path)


def pct(obj: Any, num_path: str, den_path: str = "") -> dict[str, Any]:
    """百分比指标：返回 {"num":..,"denom":..}，**绝不自己算好再返回一个数**。

    为什么必须返回两个数：insights_reference.md:130 是硬规则——
    67% (16/24) 与 67% (2/3) 数值相同但不是同一个事实，分母参与评分。
    只交分子的实测后果：模型 dig 到 num=3 就交上来（3 ≠ 50.0）。
    """
    num = dig(obj, num_path)
    den = dig(obj, den_path) if den_path else None
    if den is None and str(num_path).endswith(".num"):
        den = dig(obj, str(num_path)[: -len(".num")] + ".denom")
    return {"num": num, "denom": den}


# 全大写的 SQL 关键字/函数名：它们长得像"大写开头的实体名"，但根本不是值。
# 少了这张表，`ORDER BY cnt DESC` 里的 'DESC' 会被当成图名去查库、查不到
# 就整条技能判过期 —— 那是误杀。
_SQL_STOPWORDS = frozenset("""
SELECT FROM WHERE GROUP ORDER BY HAVING LIMIT OFFSET JOIN LEFT RIGHT INNER
OUTER ON AS AND OR NOT IN IS NULL LIKE BETWEEN CASE WHEN THEN ELSE END
ASC DESC COUNT SUM AVG MIN MAX DISTINCT OVER PARTITION ROW_NUMBER RANK LAG
LEAD COALESCE CAST ROUND TRUE FALSE UNION ALL WITH INTERVAL DATE EXTRACT
""".split())

_IN_DB_CACHE: dict[str, bool] = {}


def _value_in_db(value: str) -> bool:
    """这个实体值在**当前库**里到底有没有（带缓存）。"""
    v = str(value or "").strip()
    if not v or v in _IN_DB_CACHE:
        return _IN_DB_CACHE.get(v, False)
    ok = False
    try:
        import duckdb
        import anchor as _anc
        con = duckdb.connect(str(_anc._db_path()), read_only=True)
        try:
            n = int(con.execute(
                "SELECT COUNT(*) FROM rounds WHERE "
                "CAST(series_id AS VARCHAR)=? OR winning_team_name=? "
                "OR losing_team_name=? OR map_name=?",
                [v, v, v, v]).fetchone()[0])
            ok = n > 0
        finally:
            con.close()
    except Exception:
        ok = True          # 查不动就别误杀，交给执行结果去判
    _IN_DB_CACHE[v] = ok
    return ok


def stale_literals(code: str) -> list[str]:
    """技能代码里**写死但当前库已经没有**的实体值 —— 空列表表示没过期。

    为什么必须有这条：技能库里存的是"当时跑通的那段代码"，值写死在里面。
    换数据源（events → vlr → rib）之后，旧值（2843069 / Cloud9 / Lotus /
    Corrode）在当前库里**一个都不存在**，但检索照样会把这条技能捞出来、
    照样 exec、照样跑完不报错 —— 只是**每一行都是 0 行**。
    实测：考核 26 次里 16 次复用了技能，覆盖率却还是
    `0.0, 0.25, 0.0, ...` 一路回零 —— 复用成功 ≠ 做得对。

    判据（"这个字面量像不像实体值"）：
      · 4 位以上纯数字 → 赛事号
      · 大写开头的英文串 → 队名 / 图名（排除 SQL 关键字）
      · entity_catalog 里登记过的值
    **不能只认 catalog**：换库之后 GRID 那批队名图名（Cloud9 / Lotus /
    Corrode）已经从 catalog 里消失了，只查 catalog 会一条都检不出来
    （第一版就是这个毛病，实测 8 条技能判出 0 条过期）。
    """
    s = str(code or "")
    if not s:
        return []
    known: set[str] = set()
    try:
        cat = json.loads((Path(__file__).resolve().parent
                          / "entity_catalog.json").read_text(encoding="utf-8"))
        for vals in (cat.get("entities") or {}).values():
            if isinstance(vals, list):
                known |= {str(v) for v in vals if str(v)}
    except Exception:
        pass
    out: list[str] = []
    # 裸数字也要查：实测 `series_id=2843069` 是**不带引号**写的，
    # 只扫引号会整条漏掉（lotus_win_rate 就只判出 'Cloud9' 一个）。
    lits = set(re.findall(r"'([^']{3,40})'", s)) \
        | set(re.findall(r'"([^"]{3,40})"', s)) \
        | set(re.findall(r"\b(\d{4,})\b", s))
    for lit in lits:
        if lit in _SQL_STOPWORDS:
            continue
        looks_entity = (re.fullmatch(r"\d{4,}", lit) is not None
                        or re.fullmatch(r"[A-Z][A-Za-z0-9 .'\-]{2,}", lit) is not None
                        or lit in known)
        if not looks_entity:
            continue                       # 'num' / 'denom' 这类路径，不查
        if not _value_in_db(lit):
            out.append(lit)
    return sorted(out)


def _subject_of(task: Any) -> dict[str, Any]:
    """从题的 rubric 里收集当前实体（series / team / map）。"""
    out: dict[str, Any] = {}
    for p in (getattr(task, "rubric", None) or []):
        for k, v in ((getattr(p, "subject", None) or {}) or {}).items():
            if isinstance(v, (list, tuple)):
                v = v[0] if v else None
            if v not in (None, ""):
                out.setdefault(str(k), v)
    return out


def _subject_env(subject: dict[str, Any] | None) -> dict[str, str]:
    """把当前这道题的 subject 翻成注入常量。

    原版 Voyager 是两层：主函数 `main(bot)` 每次生成，**技能函数可带参数**
    （`mineBlock(bot, name, count)`，值在调用点传）—— 所以技能是通用方法。
    MVE 的主函数被硬约束成单参 `mcp`（`_parse` 里 `params != ["mcp"]` 就报
    错），改签名要动整条链路。同构的等价做法是：**把值放进exec 的命名空间**，
    技能代码引用 `SERIES` / `C9` / `MAP`，值由**调用方**注入 —— 同样是
    "值在调用点给"，只是载体从参数换成环境。
    """
    env = {"SERIES": SERIES, "C9": C9, "MAP": MAP}
    for k, name in (("series", "SERIES"), ("team", "C9"), ("map", "MAP")):
        v = (subject or {}).get(k)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if v not in (None, ""):
            env[name] = str(v)
    return env


async def execute(program_code: str, program_name: str,
                  subject: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行模型产出的代码，返回它 return 的原始 dict。

    照原版：执行结果由**解释器**产出，不经过任何模型加工。

    `subject` = 当前题的实体（series / team / map）。传了就按它覆盖注入
    常量 —— 同一个技能换个队伍/换张图也能跑，这才是"学会了"而不是"背下了"。
    """
    mcp = MCPHandle()
    env = _subject_env(subject)
    ns: dict[str, Any] = {"__builtins__": dict(_SAFE_BUILTINS),
                          "mcp": mcp, "dig": dig,
                          "scalar": scalar, "pct": pct,
                          # 预置常量：不预置的话模型会自己写 `series_id=...`
                          # 然后 NameError（实测踩过）。
                          **env}
    exec(program_code, ns)  # noqa: S102  —— 原版就是这样执行模型代码的
    fn = ns.get(program_name)
    if fn is None:
        raise CodeParseError(f"代码里没有 {program_name}")
    try:
        result = await fn(mcp)
    except CodeParseError:
        raise
    except Exception as e:
        # **所有**执行期异常都要转成可回灌的错误，不能崩出去。
        # 实测踩过：模型写了未定义的变量（NameError），只捕 TypeError 的话
        # 异常会一路抛穿 collect()，重试机制根本没机会让模型改。
        raise CodeParseError(
            f"执行失败：{type(e).__name__}: {e}"
            + ("（参数名写错？工具签名见上）" if isinstance(e, TypeError) else
               "（用了未定义的名字？series_id 等常量已预置，见 system 提示）")
        ) from e
    return {
        "values": result if isinstance(result, dict) else {"_result": result},
        "calls": mcp.calls,
        "tools": [c["tool"] for c in mcp.calls if c.get("ok")],
    }


# --------------------------------------------------------------------------
# 一次完整的代码化取证（带重试，照原版 retry=3）
# --------------------------------------------------------------------------
async def collect(task: Any, *, dims: list[str] | None = None,
                  code: str = "", error: str = "", critique: str = "",
                  missing: list[str] | None = None,
                  retries: int = 3, tools: list[str] | None = None,
                  skills_text: str = "", hint: str = "") -> dict[str, Any]:
    """让模型写一段调 MCP 的代码，执行它，拿回原始值。

    `hint` 是出题器按掌握度算出来的**支架档位**（见 `difficulty.py`）：
    full 给足（★ 声明口径 + critic 给候选），none 全收（只说取空了）。
    MVE 的题目难度是写死的，梯度只能落在支架上 —— 从扶到放。
    """
    import difficulty
    from llm_client import chat

    # 先确定这份题该看哪些工具的结构文档：题目声明的 + 图谱里点名的。
    # 不 probe 的话模型只能猜字段名（实测四个维度全 dig 到 None）。
    if tools is None:
        tools = list(getattr(task, "requires_tools", []) or [])
        try:
            import knowledge_graph
            g = knowledge_graph.KnowledgeGraph.load()
            for d in (dims or []):
                tools += list(g.who_produces(d))
        except Exception:
            pass
        tools = sorted(set(t for t in tools if t in TOOL_SIGNATURES)) or list(TOOL_SIGNATURES)

    schemas = await probe_schemas(tools)
    # 支架档位决定「给多少」：会了就收，不会才扶。
    # 空串（未指定）按 full 处理 —— 保持旧行为，不回退。
    hint = str(hint or "") or difficulty.HINT_FULL
    declared = declared_paths(task, dims or []) \
        if difficulty.hint_allows(hint, "declared") else {}
    messages = [
        {"role": "system", "content": render_system_message(
            render_tool_docs(
                schemas,
                dims if difficulty.hint_allows(hint, "focus") else None,
                list(declared.values())),
            graph_hints(dims, task) if difficulty.hint_allows(hint, "declared") else "",
            skills_text)},
        {"role": "user", "content": render_human_message(
            task=task, code=code, error=error, critique=critique,
            missing=missing, dims=dims)},
    ]
    last_err = ""
    for attempt in range(retries):
        text = chat(messages, temperature=0.2)
        try:
            parsed = parse_code(text)
        except CodeParseError as e:
            last_err = str(e)
            # 把解析错误回灌，让模型下一轮改正（照原版解析失败也重试）
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user",
                             "content": f"解析失败：{last_err}\n请重新输出，注意格式要求。"})
            continue
        try:
            out = await execute(parsed["program_code"], parsed["program_name"],
                                subject=_subject_of(task))
        except CodeParseError as e:
            last_err = str(e)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user",
                             "content": f"执行失败：{last_err}\n请修正后重出一段完整代码。"})
            continue

        # ---- 原版 critic 环节的同构物：跑通了 ≠ 取对了 ----
        #
        # 此前只在「代码跑不起来」时重试；代码跑通就直接返回，于是
        # "路径取错、值取成 None"这类错误**根本没有修正机会** —— 它既不是
        # 解析错误也不是执行错误，静悄悄地就交上去了。
        # 原版是：执行 → critic 判断有没有完成 → 没完成就把观察回灌 → 再写。
        # 这里同构：执行 → 看维度是不是空/是不是取错分支 → 回灌 → 再写。
        used = paths_in_code(parsed["program_code"])
        missing_now = [d for d in (dims or [])
                       if is_empty(out.get("values", {}).get(d))]
        # 取到值 ≠ 取对分支：`team_comparison.*` 那支是占位值（kd_ratio 恒 1.0），
        # 照样能 dig 出数字。所以口径不符要**单独**判，不能只看空不空。
        off_spec = [d for d in (dims or [])
                    if d in declared and not _uses_declared(used, declared[d])]
        problem = sorted(set(missing_now) | set(off_spec))
        if problem and attempt < retries - 1:
            # 反馈必须带**候选路径**：只说"路径不对"，模型没有可改的方向，
            # 实测三轮交出一模一样的错路径。这里把返回里真实存在的、
            # 名字里含该维度词的路径列出来 —— 这是"执行层观察"的回灌
            # （原版 critic 回灌的也是世界状态，不是"你错了"三个字）。
            # 候选路径也是支架的一部分：会了就只说"取空了"，不给答案方向
            cands = (_candidates(schemas, problem, limit=12)
                     if difficulty.hint_allows(hint, "candidates") else [])
            unknown = [p for p in used
                       if not any(p in paths for paths in schemas.values())]
            msg = []
            if missing_now:
                msg.append(f"代码跑通了，但这些维度取到的是空：{', '.join(missing_now)}")
            if off_spec:
                # 点名"你用的是哪条、声明的是哪条" —— 只说"分支不对"没有可改方向。
                msg.append("这几个维度**没有用题干声明的口径**：")
                for d in off_spec:
                    mine = [p for p in used if _same_metric(p, declared[d])] or used
                    msg.append(f"    {d}：声明按 `{declared[d]}` 取值，"
                               f"你用了 `{'、'.join(mine[:2]) or '（未取）'}`")
                msg.append("    同一份返回里有多支同名字段，只有声明的那一支是"
                           "权威口径；别的分支即使取到数字也不可信。")
            if unknown:
                msg.append("你写的这些路径**不在工具返回里**："
                           + "、".join(unknown[:6]))
            if cands:
                msg.append("返回里真实存在的候选路径（从中挑，不要自己编段名）：")
                msg += [f"    {c}" for c in cands]
            msg += [
                "自查三条：",
                "  1. 这个维度是**标量**还是**百分比**？标量用 scalar(obj, path)，"
                "百分比用 pct(obj, num_path, den_path)；",
                "  2. 是 team 还是 opponent？是队伍整体还是某个选手？"
                "（题目口径优先）；",
                "  3. 有没有拿**别的指标**的 num/denom 去除出它（那是取错）。",
                "取不到就让它空着也不要编，改的是路径不是数。",
            ]
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "\n".join(msg)})
            missing = missing_now
            continue

        out["program_code"] = parsed["program_code"]
        out["program_name"] = parsed["program_name"]
        out["attempts"] = attempt + 1
        return out

    return {"error": f"代码化取证失败（重试 {retries} 次）：{last_err}",
            "values": {}, "calls": [], "tools": []}


def _rebuild_schemas() -> int:
    """重建 `tool_schemas.json` —— 换库清档之后必须跑这一步。

    ⚠️ 为什么必须单开一个 CLI（2026-10-14 服务器实测）：
      `reset_all.py --db-switch` 会把 `tool_schemas.json` 清掉（它是**从旧库
      派生**的：表名 / 列名 / 队名跟库走），但 `load_schemas()` 读不到文件时
      **只返回 {}、不报错、也不重建**（action_code.py:227-232）。
      于是模型拿不到工具的结构文档，只能**猜**字段路径 ——
      实测后果：四个维度全 dig 到 None（猜的 `economy.pistol_win_rate`
      跟真实路径 `key_metrics.economy.pistol.num` 根本不一样）。

      而唯一的重建入口 `probe_schemas()` 是 `async` 且**没有 CLI**，
      所以换库清档之后没有任何一条路径会把它补回来 —— 缺口就在这里。

    另一个诚实限制：探针的默认参数取自 `SERIES` / `C9`，而这两个值来自
    `anchor.py` —— 也就是 **db_config.json 的 anchor 字段**，不是 db_path。
    锚点与当前库不符时（换库但没重算锚点），整轮探针都会扑空：
    实测拿 rib 的 `1058` 去问 GRID 库 → `No data found for series 1058`，
    6/10 个工具探不到。所以这里会先报锚点归属，再报每个工具拿到几条。
    """
    import db_switch

    print("=" * 68)
    print("  重建工具 schema 缓存")
    print("=" * 68)
    print(f"  当前连的库：{db_switch.current_path()}")
    before = load_schemas()
    print(f"  重建前缓存：{len(before)} 个工具")

    # 探针参数取自锚点（SERIES / C9），锚点若与当前库不符，这一整轮都会失败 ——
    # 先把锚点的归属说清楚，免得人只看见"6 个工具探不到"却不知为何。
    try:
        st = db_switch.anchor_status()
        if st.get("stale"):
            print(f"  ⚠️ 锚点过期：{st.get('hint')}")
            print("     探针参数就是照锚点填的，所以它必然探不到 —— "
                  "先 `python mve/anchor.py --refresh`。")
    except Exception:
        pass

    stats: dict[str, list[str]] = {}
    got = asyncio.run(probe_schemas(TOOL_NAMES, force=True, stats=stats))
    probe_ok = set(stats.get("probed_ok") or [])
    retained = set(stats.get("retained") or [])

    for t in TOOL_NAMES:
        v = got.get(t) or []
        if t in probe_ok:
            mark = "✓"        # 本轮真探到的
        elif t in retained:
            mark = "~"        # 本轮失败，沿用了旧值（换库时这份属于旧库）
        else:
            mark = "✗"        # 没有结构可用
        head = v[0] if v else "（空）"
        print(f"  {mark} {t:28s} {len(v):>4} 条   {str(head)[:58]}")

    n_ok, n_ret = len(probe_ok), len(retained)
    print(f"\n  重建完成：{n_ok}/{len(TOOL_NAMES)} 个工具**本轮探到**"
          + (f"；{n_ret} 个沿用旧缓存" if n_ret else "")
          + f"（写到 {SCHEMA_CACHE.name}）")
    print("  ✓ 本轮探到  ~ 本轮失败、沿用了旧值  ✗ 无结构")
    if n_ret:
        print(f"  ⚠️ 沿用的 {n_ret} 个是**旧库**的结构（失败不覆盖成功保下来的）："
              "比空强，但对新库来说可能是错的路径。")
    return 0 if n_ok else 1


def _main() -> int:
    import argparse
    from tasks import TASKS

    ap = argparse.ArgumentParser(description="代码化取证：模型写调 MCP 的代码，执行拿原值")
    ap.add_argument("--topic", help=f"题目 id，可选：{', '.join(TASKS)}")
    ap.add_argument("--rebuild-schemas", action="store_true",
                    help="重建工具 schema 缓存（换库清档后必跑）")
    args = ap.parse_args()

    if args.rebuild_schemas:
        return _rebuild_schemas()

    if not args.topic:
        ap.error("需要 --topic 或 --rebuild-schemas")

    task = TASKS.get(args.topic)
    if task is None:
        print(f"未知题目 {args.topic}")
        return 1
    dims = sorted({str(p.dimension) for p in task.rubric})
    out = asyncio.run(collect(task, dims=dims))
    print(f"题目    : {args.topic}")
    print(f"需要维度: {', '.join(dims)}")
    if out.get("error"):
        print(f"失败    : {out['error']}")
        return 1
    print(f"调用工具: {' → '.join(out['tools']) or '（无）'}   "
          f"（尝试 {out.get('attempts')} 次）")
    print(f"主函数  : {out.get('program_name')}")
    print("取到的原始值：")
    for k, v in (out.get("values") or {}).items():
        flag = "" if v is not None else "   ← 没取到（该维度不在观测里）"
        print(f"  {k:<24} = {v!r}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
