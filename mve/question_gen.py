#!/usr/bin/env python3
"""Automatic question generation (mirrors the study companion's four-stage closed loop of `question_generate` + `question_validate`).

The study companion's four stages (evidence per stage)
--------------------------------------------------
1. `question_type_mapping.resolve_target_question_type` (:131)
   —— the question type is decided by the **server**: the seed's teaching style → `MACHINE_QUESTION_TYPES`.
   The comment is blunt: "a teaching style is pedagogical data, not an LLM-selected
   output format". The LLM is not allowed to pick the question type itself.
2. `tutor_llm_agent_question_generate._normalize_question` (:46)
   —— the LLM produces the question text, **simultaneously producing** `answer` / `accepted_answers` / `key_points` /
   `rubric` / `solution_steps` / `difficulty`.
3. `tutor_llm_agent_question_validate._normalize_question_validation` (:18)
   —— a **second LLM call** independently validates three booleans: `relevant` / `answer_supported` /
   `difficulty_appropriate`, plus `reason`; `retry` must be consistent with the three,
   otherwise it errors out with `SdkError` directly. If it fails, regenerate.
4. `question_type_mapping.enforce_mapped_question_type` (:259)
   —— finally force `question_type` back to the server-selected one, preventing the LLM from changing the question type.

MVE's key change: what is generated is a **recipe**, not an answer
--------------------------------------------------
The study companion treats the LLM's `answer` as the ground truth (`accepted_answers`).
MVE **must not** do this —— `loop_core.py:165` hardcodes it: expected and tolerance
are only read from the server-private `answer_spec`, never replaced with a model-generated reference answer.

My previous round concluded from this that "MVE cannot auto-generate questions, a hard boundary" —— **this inference was wrong**.
The mistake was equating "cannot trust the model's answer value" with "cannot let the model produce questions".
The correct approach: what the LLM produces is an **executable data-retrieval recipe** (`answer_spec`:
SQL, or tool + arguments + value path), not the answer value.

The recipe is executable → the referee runs it once and gets the truth from VLML. So in the study companion's
step 3, the `answer_supported` "judged by a second LLM" becomes **deterministic validation** in MVE:

    Does it run? (executes without error)
    Is the value non-empty? (not NULL / empty string)
    Is the sample size sufficient? (base ≥ min_base, per the base gate)
    Does it duplicate an existing question? (voided if the same (subject, dimension) already exists)

**This is stronger than the study companion**: the study companion's "is the answer supported" is another model's
opinion (which can even be self-contradictory, hence its `_answer_reference_answer_consistent` consistency flag);
MVE's "does this question hold up" is a data fact — one run tells you.

Usage
----
    python mve/question_gen.py --about "Cloud9's pistol rounds" --difficulty 3
    python mve/question_gen.py --about "first-blood conversion" --adopt     # merge into the question bank once validated
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import vlml_env  # noqa: E402  bootstrap the environment

from llm_client import chat_json  # noqa: E402
from loop_core import AnswerSpec, RubricPoint, Task, dig  # noqa: E402

# Generated questions land here (runtime artifact, not committed to the repo; tasks.py merges at startup)
STORE = HERE / "generated_tasks.json"

# Kept consistent with action_code.TOOL_NAMES (the externally exposed MCP tools)
TOOL_NAMES = [
    "match_summary_report", "match_analysis_report", "match_players_report",
    "match_rounds_report", "match_economy_report", "pattern_detection_report",
    "player_profile_report", "scouting_report", "query_sql", "get_database_info",
]

# ⚠️ No longer hardcoded: after the database swap (GRID → vlr.gg → rib.gg), 2843069 / Cloud9
# **do not exist at all** in the new database —— on the rib database, 0 of the seed questions'
# 17 rubric points actually yielded data.
# The anchor follows the database, queried live from the database (see anchor.py). The old database still picked these two values.
try:
    import anchor as _anchor
    SERIES = _anchor.series()
    C9 = _anchor.team()
except Exception:                                        # pragma: no cover
    SERIES = "2843069"
    C9 = "Cloud9"


# --------------------------------------------------------------------------
# Stage 0: convention declaration (server-side fact, not decided by the model)
# --------------------------------------------------------------------------
_PCT_NAME = ("_pct", "_rate", "_conv", "_percent", "pct_")


def _dim_specs() -> dict[str, dict[str, Any]]:
    """What **kind of quantity** each dimension is and **whether it needs a denominator** —— declared by the server, not from the model.

    The study companion does not let students/models pick question types ("a teaching style is pedagogical data, not an
    LLM-selected output format"); MVE likewise: whether a dimension is a percentage or a count, and where the denominator comes from,
    is a server-side fact.

    Source: existing questions' `answer_spec` (hard fact); dimension names only serve as a fallback.

    ⚠️ The percent semantics of the two paths are **different** (this is existing referee behavior, not a bug):
      · Tool path + `percent=True` → value is the **numerator**, base is the **denominator**,
        the referee computes `num/denom*100` itself (`vlml0_referee.py:224`).
      · SQL path → value is the **final value** (the SQL already does `ROUND(AVG(...)*100,1)`),
        base is only the sample size, the referee **does not convert** (`vlml0_referee.py:265`).

    Failing to distinguish these two produces dead questions like "the question text asks for a win rate, the recipe fetches a count" —— the
    2nd generation run passed exactly like this: `pistol.num` = 3 was treated as a win rate, while the true value is 3/6 = 50%.
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        from tasks import TASKS
    except Exception:                                        # pragma: no cover
        return out
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception:                                        # pragma: no cover
        g = None
    for t in TASKS.values():
        for p in t.rubric:
            d = str(p.dimension or "")
            if not d:
                continue
            s = getattr(p, "answer_spec", None)
            rec = out.setdefault(d, {
                "kind": "count", "needs_base": False, "example": "",
                "by_tool": False, "by_sql": False,
            })
            if s is None:
                continue
            if s.tool:
                rec["by_tool"] = True
            elif s.sql:
                rec["by_sql"] = True
            if bool(getattr(s, "percent", False)):
                rec["kind"] = "percent"
                rec["needs_base"] = True
                if s.tool and s.base_path:
                    rec["example"] = f"base_path={s.base_path}"
                elif s.sql and s.base_column is not None:
                    rec["example"] = f"SQL column {s.base_column} as denominator/sample size"
    # ---- new dimensions adopted from human import ----
    # Without this section, `_graph_blueprint()`'s `if not specs.get(d,{}).get("by_sql")`
    # would skip entire human-imported dimensions —— observed: the knowledge graph clearly had a
    # `plant_success_rate` node, yet the question-generation point still picked `map_fb_conv`,
    # producing a question unrelated to the human's (the human asked about plant success rate, out came first-blood conversion).
    # These dimensions are **answered by VLML via SQL**, so by_sql must be true —— that is not a guess.
    try:
        import knowledge_graph
        for irec in knowledge_graph.load_imported_links():
            if not irec.get("covered"):
                continue                 # directions the tool set cannot cover do not qualify as question-generation directions
            d = str(irec.get("dimension") or "").strip()
            if not d or d in out:
                continue
            low = d.lower()
            is_pct = any(k in low for k in ("rate", "ratio", "pct", "conv",
                                            "success", "win"))
            out[d] = {
                "kind": "percent" if is_pct else "count",
                "needs_base": bool(is_pct),
                "example": "compute the percentage inside the SQL, and take an extra column as the sample size",
                "by_tool": bool(irec.get("tool")) and not bool(irec.get("sql")),
                "by_sql": bool(irec.get("sql")),
                # Human-imported dimensions have no dim node; desc will not come from a node —— use
                # the human's original wording here, so `_relevant` has something to compare against (see `_human_desc`'s comment)
                "desc": _human_desc(str(irec.get("question") or "")),
                "origin": "human_import",
            }
    except Exception:                                        # pragma: no cover
        pass
    for d, rec in out.items():
        pt = ""
        if g is not None:
            n = g.nodes.get(f"dim:{d}")
            pt = str((n.detail.get("point") if n else "") or "")
        # Only take the main name before the parenthesis: "pistol-round win rate (percentage 0-100, e.g. 50.0)" → pistol-round win rate
        # `or rec.get("desc")` —— human-imported dimensions have no node; desc was already filled with the human's
        # original wording above, and here it must not fall back to the English original name (that would make validation gate 0b reject it as irrelevant).
        # ⚠️ `point` is **that question's rubric point text**, bound to specific values ("Cloud9's win rate on Corrode")
        # —— it cannot be used directly as the dimension definition, otherwise new questions generated with different values would all be judged
        # irrelevant by gate 0b.
        # Abstraction: strip the specific entity values, keep only the convention. Human-imported desc is already the human's original wording, untouched.
        rec["desc"] = (rec.get("desc")
                       or _abstract_desc(pt.split("（")[0].split("(")[0].strip(), d))
        # The dimension's value path in the knowledge graph —— used to **reverse-look-up dimensions by recipe** (see enforce)
        vp = ""
        if g is not None:
            n = g.nodes.get(f"dim:{d}")
            vp = str((n.detail.get("value_path") if n else "") or "")
        rec["value_path"] = vp
        rec["producers"] = (list(g.who_produces(d)) if g is not None else [])
        if rec["kind"] != "count":
            continue
        if any(k in d for k in _PCT_NAME):
            # The referee judges unit by name anyway (referee.py:278), so mirror it here
            rec["kind"] = "percent"
            rec["needs_base"] = True
        elif "ratio" in d:
            rec["kind"] = "ratio"
        elif "confidence" in d:
            rec["kind"] = "label"
    return out


_STOP = {"what", "which", "how", "many", "much", "respectively", "compute",
         "calculate", "total", "please", "data", "metric", "value", "result",
         "performance", "the", "and", "for", "with", "that", "this", "from",
         "team", "series"}


def _subject_values(key: str) -> list[str]:
    """The **concrete values that can be filled in** under this subject —— queried live from the data source, never handwritten.

    Why live queries: a handwritten list goes stale with every database swap. `entity_catalog` connects to
    the database pointed to by `db_config.json` (the default database `db_switch.DEFAULT_DB` when empty,
    currently rib.gg) and queries the actual series / team / map / player values, caching them.
    When nothing is found, return empty (no questions can be asked on this subject), **never fabricate a value**.

    Add another layer of **human-registered subjects** (`subjects.py`) on top: after swapping data sources (connecting a new public source),
    a human first registers a subject, and the question generator must be able to ask questions on it —— otherwise after a source swap, the question generator
    would still be producing values from the old database. ⚠️ To state it honestly: if a human-registered value has no corresponding data
    in the data source, this question will not produce a value, and it will be caught by the validation gate, never slipping into the question bank.
    """
    vals: list[str] = []
    try:
        import entity_catalog
        vals = [v for v in entity_catalog.options(key) if v]
    except Exception:
        vals = []
    try:
        import subjects
        for v in subjects.values(key):
            if v and v not in vals:
                vals.append(v)
    except Exception:
        pass
    return vals


def _human_desc(question: str) -> str:
    """Human-imported dimensions have no server-side definition —— use **the human's original wording** as its definition.

    Without this, validation gate 0b (`_relevant`: keyword overlap between question text and dimension definition) would inevitably
    judge it irrelevant. Observed (server e2e): the dimension `plant_success_rate`'s desc fell back to the English
    original name, while the question text was phrased differently ("Cloud9's per-round plant success rate on Lotus"), with zero
    token overlap → both generations were **2/2 rejected by this gate**, and question generation never succeeded once.

    Parentheses must be **removed, not truncated**: an old v1 lesson —— truncating before the parenthesis would lose
    the word "success rate", leaving "…plant", which still would not match the question text.
    """
    t = str(question or "").strip()
    t = re.sub(r"[（(][^）)]*[）)]", "", t)          # remove parentheses and their contents
    t = re.sub(r"^(please|pls|hi|hey|can you|could you|i want to know|i wonder|help me)\b[，,：:\s]*", "", t, flags=re.I)
    t = re.sub(r"[？?。！!]+$", "", t)
    t = re.sub(r"(\?+$|^how many\b[^?]{0,40}$|^what (?:is|was|are|were)\b[^?]{0,40}$"
               r"|^how (?:do (?:you|we)|would (?:you|we)|to)\b[^?]{0,40}$"
               r"|^how (?:is|was) it calculated\b[^?]{0,40}$)", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" ，,、")
    return t


def _relevant(point: str, desc: str) -> bool:
    """Is the question text relevant to the dimension definition —— the study companion's `relevant`, but judged **deterministically**.

    The study companion's step is a **second LLM's opinion** (hence it needs an extra consistency flag against self-contradiction).
    MVE uses keyword overlap:
      · dimension definition "pistol-round win rate" + question text "Cloud9's pistol-round win rate" → relevant ✅
      · dimension definition "confidence label" + question text "Cloud9's force-buy win rate" → irrelevant ❌
    The third run hit the second case: the model filled the force-buy win rate question's dimension
    as `pattern_confidence`, which yields a value ("moderate") and is a legal dimension ——
    only this gate can stop it.
    """
    def toks(t: str) -> set[str]:
        en = {w for w in re.findall(r"[a-z_]{4,}", t.lower())}
        zh = "".join(re.findall(r"[\u4e00-\u9fff]+", t))
        grams = {zh[i:i + 2] for i in range(max(0, len(zh) - 1))}
        return (en | grams) - _STOP
    return bool(toks(point) & toks(desc))


def _known_entity_values() -> set[str]:
    """The **specific entity values** seen in the database (map names / team names / players / series ids), used to abstract dimension definitions."""
    out: set[str] = set()
    try:
        import json as _json
        from pathlib import Path as _P
        cat = _json.loads((_P(__file__).resolve().parent / "entity_catalog.json")
                          .read_text(encoding="utf-8"))
        for vals in (cat.get("entities") or {}).values():
            if isinstance(vals, list):
                out |= {str(v) for v in vals if str(v)}
    except Exception:
        pass
    return out


def _abstract_desc(pt: str, d: str) -> str:
    """Strip the **specific entity values** from a rubric point text, keeping only the abstract convention.

    ⚠️ Pitfall hit: the dimension definition directly took a seed question's rubric `point` (e.g.
    "Cloud9's win rate on Corrode" / "Corrode round count"), which is text **bound to that GRID-era
    dataset**. After a database swap, the question text becomes "Karmine Corp's round win rate on Abyss",
    with **zero word overlap** against the definition → validation gate 0b judges it "irrelevant", and 2 consecutive runs were wasted.

    A dimension definition must be an **abstract convention** ("a team's win rate on a map"), not one question's rubric point.
    After stripping values, it still blocks real mismatches (question text says "force-buy win rate", dimension filled as "confidence label" is still irrelevant).
    """
    out = str(pt or "")
    for v in _known_entity_values():
        if len(v) >= 3 and v in out:
            out = out.replace(v, "")
    # Fallback rule: **capitalized / digit-containing English words** are almost all entity names (Corrode, Cloud9,
    # NRG, 2843069), while convention words are lowercase common nouns. Use it to strip old entities not in the catalog
    # (after a database swap, the GRID-era map/team names are no longer in the catalog, so the step above cannot strip them).
    out = re.sub(r"\b[A-Z][A-Za-z0-9_]{2,}\b", "", out)
    out = re.sub(r"\b\d{4,}\b", "", out)
    # Also strip the current anchors (which may not be in the catalog, e.g. a freshly swapped database)
    try:
        import anchor as _anc
        for v in (_anc.series(), _anc.team(), _anc.map_name()):
            if v and len(str(v)) >= 3:
                out = out.replace(str(v), "")
    except Exception:
        pass
    out = re.sub(r"[（(]\s*[）)]", "", out)          # strip empty parentheses
    out = re.sub(r"\s+", " ", out).strip()
    # ⚠️ Second pitfall: stripping entities **leaves orphaned function words**, degrading the definition to "on the win rate" ——
    # it looks present, but the only keywords left are "on / of", and gate 0b still
    # judges it irrelevant against the question text's bigrams (2 consecutive runs wasted in practice). Function words must be cleared too,
    # keeping only the convention's content words.
    for _ in range(3):
        out2 = re.sub(r"(^| )(in|on|at|of|the|a|an|for|to|by|with|and|or|is|are|was|were|"
                      r"as|per|from|during|that|this)(?= |$)", " ", out, flags=re.I)
        out2 = re.sub(r"\s+", " ", out2).strip()
        if out2 == out:
            break
        out = out2
    # A possessive left stranded by the entity strip ("'s") is a function word too.
    out = re.sub(r"(^|\s)(?:'s|s')\s*", " ", out, flags=re.I)
    out = re.sub(r"\s+", " ", out).strip(" ·,。-—'")
    return out or d


def _suggest_dims(point: str, specs: dict[str, dict[str, Any]],
                  limit: int = 3) -> list[str]:
    """Recommend the few dimensions most similar to the question text —— just saying "pick another dimension" does not make the model switch correctly."""
    hits = [d for d, r in specs.items()
            if _relevant(point, str(r.get("desc") or d))]
    return sorted(hits)[:limit]


def _dim_tag(d: str, specs: dict[str, dict[str, Any]]) -> str:
    """The convention declaration after a dimension (goes straight into the prompt, so the model gets it right in one shot instead of relying on retries)."""
    rec = specs.get(d) or {}
    kind = str(rec.get("kind") or "count")
    if kind == "percent":
        ex = str(rec.get("example") or "")
        if rec.get("by_tool") and not rec.get("by_sql"):
            return ("【Percentage: the tool path requires percent=true, value_path pointing at .num, "
                    f"base_path pointing at .denom{f' (existing question {ex})' if ex else ''}】")
        if rec.get("by_sql") and not rec.get("by_tool"):
            return ("【Percentage: on the SQL path the value column must already be a computed percentage"
                    " (e.g. ROUND(AVG(...)*100,1)), with the denominator column as the sample size"
                    f"{f' (existing question {ex})' if ex else ''}】")
        return "【Percentage: a denominator must be given as well】"
    if kind == "ratio":
        return "【Ratio: do not give a denominator】"
    if kind == "label":
        return "【Enum label: copy the value from the tool's returned field, do not compute】"
    return "【Count: integer】"


# --------------------------------------------------------------------------
# Stage 1: the convention skeleton is given by the **server** (mirrors resolve_target_question_type)
# --------------------------------------------------------------------------
# The SQL path only exposes these tables (the other tables have too many columns; giving them all would bloat the prompt)
_SQL_TABLES = ("rounds", "games", "series", "agg_first_blood_stats",
               "agg_team_game_stats", "agg_team_series_stats",
               "agg_team_map_stats", "agg_player_series_stats")
_COLS_CACHE: dict[str, list[str]] = {}
# When a table has more columns than this, keep only the "convention-relevant" ones
_KEEP = ("win", "rate", "round", "kill", "death", "kast", "adr", "map", "team",
         "series", "pistol", "eco", "fb_", "clutch", "plant", "defuse",
         "streak", "score", "side", "_id", "name")


async def _sql_catalog() -> str:
    """The real tables/columns for the SQL path (pulled from information_schema, never handwritten).

    Same as with tool signatures: **without the real column names, it can only guess**.
    Observed: without the table structure, a third of the model's SQL returned 0 rows —— not a logic error,
    but it used non-existent column names.

    Column names are cached into `_COLS_CACHE` along the way: when validation reports "0 rows", use it to **name**
    which identifier is not a column; just saying "copy the table structure" does not move the model (observed: three consecutive rounds all added the same non-existent column).
    """
    global _COLS_CACHE
    inlist = ", ".join(f"'{t}'" for t in _SQL_TABLES)
    try:
        res = await vlml_env.execute_custom_sql(
            "SELECT table_name, column_name FROM information_schema.columns "
            f"WHERE table_name IN ({inlist}) ORDER BY table_name, ordinal_position")
    except Exception as e:                                   # pragma: no cover
        return f"(table-structure probe failed: {type(e).__name__})"
    from collections import defaultdict
    cols: dict[str, list[str]] = defaultdict(list)
    for t, c in (res.get("rows") or []):
        cols[str(t)].append(str(c))
    _COLS_CACHE = {str(t): list(cs) for t, cs in cols.items()}
    blocks = []
    for t in _SQL_TABLES:
        cs = cols.get(t) or []
        if len(cs) > 25:
            cs = [c for c in cs if any(k in c.lower() for k in _KEEP)]
        blocks.append(f"  {t}({', '.join(cs)})")
    return "\n".join(blocks)


_SQL_KEYS = frozenset("""
select from where and or as count sum avg round case when then else end
group order by limit in not null is on join left inner distinct min max
having asc desc between like cast integer double varchar true false
over partition row_number lag lead rank dense_rank union intersect except
with exists all any some cross outer full natural using offset fetch
first last nulls rows range preceding following current unbounded
subquery subquery1 subquery2 subquery3 derived alias
""".split())


def _bad_columns(sql: str, table: str = "") -> list[str]:
    """The identifiers in the SQL that **are not column names of the target table** —— most likely columns invented by the model.

    ⚠️ Two pitfalls (both hit before):
      · Table names themselves must not count as bad columns —— otherwise `FROM rounds` would report `rounds` as a bad column.
      · Must **judge only against the FROM table**, not the union of all tables: `team_name` exists in
        agg_team_game_stats but not in rounds; using the union would miss it.
    """
    if not _COLS_CACHE:
        return []
    pool = set(_COLS_CACHE.get(table) or []) if table else set()
    allcols: set[str] = set()
    for cs in _COLS_CACHE.values():
        allcols |= set(cs)
    tables = set(_COLS_CACHE) | set(_SQL_TABLES)
    # Strip string literals first, otherwise 'Cloud9' / 'Lotus' would be treated as identifiers
    stripped = re.sub(r"'[^']*'", " ", sql or "")
    toks = set(re.findall(r"[a-z_][a-z0-9_]*", stripped.lower()))
    cand = [t for t in toks
            if t not in tables and t not in _SQL_KEYS and len(t) > 3]
    if pool:
        bad = [t for t in cand if t not in pool]
    else:
        bad = [t for t in cand if t not in allcols]
    return sorted(bad)


async def _server_skeleton() -> str:
    """The "what can be asked" given by the knowledge graph + **real tool signatures and return structures**.

    The study companion does not let the LLM pick question types (that is pedagogical data); MVE likewise:
    **which dimensions exist, where they come from, how tools take arguments** are all declared by the server, not imagined by the model.

    Why signatures and structures must be given together: on the very first run, the model's
    `pattern_detection_report → key_metrics.economy.pistol.num` was a **real path**
    (that is how the existing question fetches it), yet it got `{"error": ...}` for not passing `team_name`,
    and was judged "unsolvable" by validation. Exactly the same lesson as the `action_code` path:
    **without the real signature and return structure, it can only guess argument names**.
    """
    import action_code
    specs = _dim_specs()
    lines: list[str] = []
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
        for d in sorted(g.dimensions()):
            prod = g.who_produces(d)
            node = g.nodes.get(f"dim:{d}")
            det = (getattr(node, "detail", None) or {})
            vp = str(det.get("value_path") or "")
            cols = det.get("value_columns") or []
            tag = _dim_tag(d, specs)
            if prod and vp:
                lines.append(f"  {d} → tool {'/'.join(prod)}, path {vp} {tag}")
            elif prod:
                lines.append(f"  {d} → tool {'/'.join(prod)} {tag}")
            else:
                # SQL dimensions: the **table / columns / convention shape / typical mistakes** must all be given.
                #
                # Previously this only said "write your own SQL, take column N", swallowing the tables /
                # columns_by_table / semantics /
                # typical_errors that already existed in detail. So the model could only guess —— all three observed
                # generations wrote `FROM match_summary_report` (that is a **tool name**)
                # and `MAX(conversion.fb_conv)` (treating the **dimension name** as a column name).
                # The declarations were in the knowledge graph all along; they just were not shown to it.
                #
                # Isomorphic: the study companion's knowledge-point nodes come with question_types and
                # typical_misconceptions, and question generation **follows the node declarations**
                # (question_type_mapping.py:131 "map it without LLM input").
                tbl = (det.get("tables") or ["?"])[0]
                cbt = det.get("columns_by_table") or {}
                cols_txt = ", ".join((cbt.get(tbl) or det.get("columns") or []))
                sem = str(det.get("semantics") or "").strip()
                lines.append(
                    f"  {d} → write your own SQL: FROM **{tbl}**"
                    + (f", available columns: {cols_txt}" if cols_txt else "")
                    + (f"; convention shape: {sem[:80]}" if sem else "")
                    + f" {tag}")
                for e in (det.get("typical_errors") or [])[:2]:
                    lines.append(f"      typical mistake: {str(e)[:90]}")
                # Modeling declaration (table nodes, from VLML's DATA_MODEL.md / DERIVED_TABLES.md
                # / column_definitions.yaml): what a row is, what each column means.
                # The consequence of giving only column names has been observed: the model wrote fb_player as player_name,
                # with the critique listing the real columns every round, and it still fabricated from common sense three times.
                tnode = g.nodes.get(f"table:{tbl}")
                tdet = (getattr(tnode, "detail", None) or {})
                if tdet.get("pk"):
                    lines.append(
                        f"      {tbl} primary key: {', '.join(tdet['pk'])}"
                        f" (one row = {tdet.get('grain') or tdet.get('grain_doc') or ''})")
                desc = tdet.get("column_desc") or {}
                named = [c for c in (cbt.get(tbl) or det.get("columns") or [])
                         if c in desc]
                if named:
                    lines.append("      column meanings: "
                                 + ";".join(
                                     f"{c}={str((desc[c] or {}).get('desc') or '')[:50]}"
                                     for c in named[:5]))
    except Exception as e:                                   # pragma: no cover
        lines.append(f"(knowledge graph unavailable: {type(e).__name__})")

    # Real signatures (reflected from functions, never handwritten)
    sigs = "\n".join(f"  await mcp.{s}"
                     for s in action_code.TOOL_SIGNATURES.values())
    # Real return structures (probe cache)
    try:
        schemas = await action_code.probe_schemas(list(action_code.TOOL_SIGNATURES))
        blocks = []
        for t, paths in schemas.items():
            shown = "\n".join(f"    {p}" for p in paths[:40])
            blocks.append(f"  {t} return structure (first {min(40, len(paths))} entries):\n{shown}")
        struct = "\n".join(blocks)
    except Exception as e:                                   # pragma: no cover
        struct = f"(structure probe failed: {type(e).__name__})"

    dims = sorted(specs)
    return ("""Dimensions (declared by the server):
""" + "\n".join(lines) + f"""

★ The dimension field **may only** pick one value from the enum below, copied verbatim.
  Do not fill in tool names, do not fill in Chinese, do not invent your own (observed: the model fills tool names in as dimensions):
""" + ", ".join(dims) + """

Real tool signatures (copy verbatim; a wrong argument name gets no data):
""" + sigs + """

Tool return structures (leaf paths, **paths must be copied verbatim**):
""" + struct + """

Tables and **real column names** available on the SQL path (a wrong column name means 0 rows):
""" + await _sql_catalog())


_PROPOSE_SYSTEM = """You are a question-generation agent. Produce one **data-answerable** question for a "data-analysis agent".

Hard rules (violating them makes a broken question):
1. What you produce is **not the answer**, it is a **data-retrieval recipe**. Every rubric point must give one of the following:
   a) `sql`: a SQL statement starting with SELECT (duckdb, no CTE support), plus which column to take;
   b) `tool` + `tool_args` + `value_path`: which tool to call, what arguments to pass, which dot path to read the value from.
2. **Never** write the answer's value in the response. Values are fetched by the server running your recipe ——
   any number you write is treated as a hallucination; writing one gets the question rejected.
3. You may only use the dimensions/tools/tables listed under "What can be asked" below. Do not invent non-existent fields.
4. Only produce questions whose values can be retrieved in this match's data (series_id={SERIES}).

What can be asked (declared by the server):
{skeleton}

Available tools: {tools}

Output JSON:
{{
  "topic_id": "short lowercase-underscore English id",
  "question": "question text (English, including the convention note: team overall or a specific team, which map, what denominator)",
  "difficulty": 1-4,
  "requires_tools": ["tools used"],
  "rubric": [
    {{
      "point": "rubric point text",
      "subject": {{"series": "{SERIES}", "team": "{C9}"}},
      "dimension": "pick one from the ★ enum above, e.g. pistol_win_rate",
      "weight": 50,
      "min_base": 1,
      "answer_spec": {{
        "sql": "SELECT ...", "value_column": 0, "base_column": null,
        "numeric_tolerance": 0.05
      }}
    }}
  ]
}}
subject's keys may only be real dimensions such as series / team / map / player; do not invent them.
★ subject must include **all qualifiers from the question text**: if the question text mentions a map, write
  "map": "Lotus"; if it mentions a specific player, write "player": "name".
  Writing only the series + team keys (missing map / player) will collide with an existing question and get rejected;
  this is the most common reason for rejection.

Convention rules (decided by the server; getting them wrong makes a broken question) —— the two paths' percentages are **not the same thing**:
· Via **tool** and the dimension is marked 【Percentage】: you must set "percent": true,
  with value_path pointing at .num and base_path pointing at .denom. The referee computes num/denom*100 itself.
  ★ Giving only .num without .denom = asking for a win rate but fetching a count = broken question.
· Via **SQL** and the dimension is marked 【Percentage】: the value_column must **already be a computed percentage**
  (written as ROUND(AVG(CASE WHEN ... THEN 1.0 ELSE 0.0 END)*100, 1)),
  with base_column pointing at the sample-size column. The referee **will not convert again**.
  ★ Writing just COUNT(*) as the numerator = broken question.
· Dimensions marked 【Count】【Ratio】 take no denominator; values marked 【Enum label】 are copied from the tool's return.
"""


def _subject_key_of(col: str) -> str:
    """Column name → semantics that can serve as a subject key (following existing subject naming in the knowledge graph)."""
    c = str(col or "").lower()
    if "player" in c:
        return "player"
    if c.startswith("map") or c.endswith("_map"):
        return "map"
    if "team" in c:
        return "team"
    if "series" in c:
        return "series"
    if "game" in c:
        return "game"
    return ""


_ROWS_CACHE: dict[str, int] = {}


_DIM_MASTERY_CACHE: dict[str, dict[str, Any]] = {}


def _dim_mastery() -> dict[str, dict[str, Any]]:
    """Dimension-level mastery (cached). Data source = **exam_log** (the exam, true ability without scaffolding).

    ⚠️ run_log must not be used: practice runs with the knowledge graph, so coverage is a fake flat line pinned at 100%,
    measuring the scaffolding rather than the ability (a core project design). Using it as mastery is self-deception.
    """
    if _DIM_MASTERY_CACHE:
        return _DIM_MASTERY_CACHE
    try:
        import mastery_model as _mm
        _DIM_MASTERY_CACHE.update(_mm.dimension_mastery() or {})
    except Exception:                                    # pragma: no cover
        pass
    return _DIM_MASTERY_CACHE


def _table_rows(table: str) -> int:
    """How many rows this table has in the current database (cached, one query per process).

    ⚠️ After a data-source swap, **tables referenced in the knowledge graph may be empty**: rib/vlr have no kill
    events, so `agg_first_blood_stats` has 0 rows and `base_events.is_kill` has 0 rows —— while the knowledge graph
    was built in the GRID era, and its dimension nodes still point at them. Picking an empty table for question generation means the model
    can write it perfectly and still yield no data (observed: 2 consecutive generations rejected by validation for "the recipe ran empty").
    So before picking a question point, first confirm this table **really has data** in the current database.
    """
    t = str(table or "").strip()
    if not t:
        return 0
    if t in _ROWS_CACHE:
        return _ROWS_CACHE[t]
    n = 0
    try:
        import duckdb
        import anchor as _anc
        con = duckdb.connect(str(_anc._db_path()), read_only=True)
        try:
            n = int(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        finally:
            con.close()
    except Exception:
        n = 0
    _ROWS_CACHE[t] = n
    return n


_SEED_SQL_CACHE: dict[str, str] = {}


def _seed_skeleton(dim: str) -> str:
    """**Skeleton fallback** when the knowledge graph has no recipe: take the human-reviewed correct SQL from the seed question.

    ⚠️ This is the isomorphic slot of the study companion's `examples` —— **give correct example questions for the model to copy**, rather than blocking it afterward.
    A previous wrong turn: the knowledge graph's `recipe` only exists for some dimensions (map_win_rate is empty), and
    with an empty skeleton the model could only invent its own WHERE, so it wrote `winning_team_name` into the
    WHERE, making the win rate always 100% and the question dead; for that I added a "convention self-cannibalization" gate to block it afterward,
    and the model wrote **identical** SQL for two consecutive rounds —— blocking afterward does not work; only giving the right thing beforehand does.

    And **the correct formulation has been in the seed question all along** (tasks.py:229):
        SELECT ROUND(AVG(CASE WHEN winning_team_name='<team>' ...)), COUNT(*)
        FROM rounds WHERE series_id='<series>' AND map_name='<map>'
    The WHERE is clean, with win/loss decided by CASE. It is human-reviewed and yields data.
    """
    d = str(dim or "").strip()
    if not d:
        return ""
    if d in _SEED_SQL_CACHE:
        return _SEED_SQL_CACHE[d]
    out = ""
    try:
        from tasks import TASKS
        best = ""
        for _t in TASKS.values():
            for _p in (getattr(_t, "rubric", None) or []):
                if str(getattr(_p, "dimension", "") or "") != d:
                    continue
                _s = str(getattr(getattr(_p, "answer_spec", None), "sql", "") or "")
                if len(_s) > len(best):
                    best = _s
        if best:
            out = best
            # Desensitize: replace specific entity values with '?' (the skeleton only gives shape; values are for the model to look up in the database)
            try:
                import anchor as _anc
                for v in (_anc.series(), _anc.team(), _anc.map_name(), SERIES, C9):
                    if v and len(str(v)) >= 3:
                        out = out.replace(str(v), "?")
            except Exception:
                pass
            for v in _known_entity_values():
                if len(v) >= 3 and v in out:
                    out = out.replace(v, "?")
            out = re.sub(r"'[A-Z][^']{2,}'", "'?'", out)
    except Exception:                                        # pragma: no cover
        out = ""
    _SEED_SQL_CACHE[d] = out
    return out


_SCOPED_CACHE: dict[tuple[str, str], list[str] | None] = {}


def _scoped_values(kind: str, series_id: str) -> list[str] | None:
    """Entity values that **really exist in this match**. Returns None when not found / not applicable (meaning "unconstrained").

    ⚠️ The third solvability check (the first two: non-empty table, existing columns). What `_subject_values()` gives
    is the **whole-database** value domain, which does not equal what this match played —— observed: 1058 played only Sunset /
    Haven / Summit, yet **Abyss** was picked from the whole database's 7 maps, so
    `WHERE series_id='1058' AND map_name='Abyss'` had 0 rounds → the question is unsolvable, and 3 consecutive generations were wasted.
    So values must be scoped once more by series.
    """
    k = str(kind or "").strip()
    sid = str(series_id or "").strip()
    if not k or not sid:
        return None
    if (k, sid) in _SCOPED_CACHE:
        return _SCOPED_CACHE[(k, sid)]
    out: list[str] | None = None
    if k in ("map", "team"):
        try:
            import duckdb
            import anchor as _anc
            con = duckdb.connect(str(_anc._db_path()), read_only=True)
            try:
                if k == "map":
                    rows = con.execute(
                        "SELECT DISTINCT map_name FROM rounds "
                        "WHERE CAST(series_id AS VARCHAR)=? "
                        "AND map_name IS NOT NULL", [sid]).fetchall()
                else:
                    rows = con.execute(
                        "SELECT DISTINCT t FROM (SELECT winning_team_name AS t "
                        "FROM rounds WHERE CAST(series_id AS VARCHAR)=? "
                        "UNION ALL SELECT losing_team_name FROM rounds "
                        "WHERE CAST(series_id AS VARCHAR)=?) "
                        "WHERE t IS NOT NULL AND t<>''", [sid, sid]).fetchall()
                out = [str(r[0]) for r in rows if r[0]]
            finally:
                con.close()
        except Exception:
            out = None
    _SCOPED_CACHE[(k, sid)] = out
    return out


def _graph_blueprint(difficulty: int = 2,
                     avoid: set[tuple[str, str]] | None = None,
                     prefer: str = "") -> dict[str, Any]:
    """The server **picks the question point from the knowledge graph** —— per the study companion, the question point is not chosen by the LLM.

    The study companion's approach (stated most bluntly in `question_type_mapping.py:131`'s docstring):
        "Choose the first declared teaching style and **map it without
         LLM input**. Seed ordering is preserved as the author-provided
         priority."
    I.e.: **knowledge-point nodes come with question_types / difficulty / typical_misconceptions,
    and the question generator picks that node; the question type and convention are decided by the node declarations**, with the model only responsible for writing the question text.

    MVE isomorphic: the knowledge graph's dimension nodes' detail already has `tables` / `columns_by_table` /
    `semantics` / `recipe` / `typical_errors`, and the server uses them to fix
    "which dimension × which subject key × which table × which columns × what convention shape",
    **hardcoding these into the prompt**, with the model only filling in the WHERE values and question text.

    Why this step is necessary (lessons observed): putting just the "real column list" into the critique is not enough
    —— in three generations, the critique listed `fb_player` among the real columns every time, yet the model still insisted on writing
    the non-existent `player_name`. **Let it pick, and it will fabricate from common sense**; the only reliable way is not to let it pick.
    """
    avoid = avoid or set()
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception:                                        # pragma: no cover
        return {}
    specs = _dim_specs()
    cands: list[tuple[int, str, dict[str, Any]]] = []
    for d in sorted(g.dimensions()):
        node = g.nodes.get(f"dim:{d}")
        det = getattr(node, "detail", None) or {}
        tables = det.get("tables") or []
        if not tables:
            continue                     # tool dimensions: the convention is bounded by tool signatures; not used for new questions yet
        if not specs.get(d, {}).get("by_sql"):
            continue
        # Pick the table that **really has data in the current database**, not the first one in knowledge-graph order
        tbl, cols = "", []
        for t in tables:
            if _table_rows(t) == 0:
                continue
            tbl = t
            cols = ((det.get("columns_by_table") or {}).get(t)
                    or det.get("columns") or [])
            break
        if not tbl or not cols:
            continue                 # this dimension has no solvable table in the current database
        # Which unused subject keys this dimension can support
        keys = [k for k in (_subject_key_of(c) for c in cols) if k]
        for k in dict.fromkeys(keys):
            # subject **values** are queried live from the data source (entity_catalog), not handwritten ——
            # previously this wrote `subj[k] = "?"`, handing the choice to the model, so it always produced
            # the same Cloud9 / 2843069 question; now every fillable value is an independent candidate.
            vals = _subject_values(k)
            if k in ("series", "team"):
                # Existing questions fix series=2843069 / team=Cloud9.
                # Only swapping to **other entities** counts as a new question (also transfer: does the same knowledge point work with a different team)
                vals = [v for v in vals if v not in (SERIES, C9)]
            if not vals:
                continue
            for v in vals:
                subj = {"series": SERIES, "team": C9}
                subj[k] = v
                # ---- subject must be **this dimension's own grain**; it cannot always carry team ----
                # Observed: map_rounds only uses map_name + series_id, but subject
                # carried team=Cloud9, and the model wrote `AND team_name='...'` accordingly ——
                # the rounds table has no team_name at all, 0 rows, the question is unsolvable.
                # The study companion's isomorphic slot is `project_target_topic_evidence()`: project only
                # the fields **this knowledge point declares**, nothing more.
                grain = set(keys) | {"series"}
                subj = {kk: vv for kk, vv in subj.items() if kk in grain}
                if not subj.get("series"):
                    subj["series"] = SERIES
                # ---- Third solvability check: the value must **really exist in this match** ----
                _sid = str(subj.get("series") or SERIES)
                if k == "series":
                    # Swapping to **another match**; the team must swap too —— otherwise
                    # `WHERE series_id=<new match> AND team_name=Cloud9` directly gives 0 rows
                    _teams = _scoped_values("team", str(v)) or []
                    if _teams:
                        subj["team"] = C9 if C9 in _teams else _teams[0]
                else:
                    _sc = _scoped_values(k, _sid)
                    if _sc and str(v) not in _sc:
                        continue        # this match did not play this map / has no such team
                key = (json.dumps(subj, sort_keys=True), d)
                if key in avoid:
                    continue
                score = 0
                if prefer and d == prefer:
                    score -= 10          # the planner-specified direction takes priority
                # Difficulty matching: read the knowledge graph's own **difficulty** (in the study companion, difficulty lives on the knowledge-point
                # node, not in the question generator). Previously this counted recipe keywords itself, while
                # recipe is just a desensitized skeleton often lacking GROUP BY, so a question that clearly needs grouping
                # was judged tier 1.
                own = int(det.get("difficulty") or 0)
                if not own:                  # only fall back to counting keywords for old graphs without this field
                    low = str(det.get("recipe") or det.get("semantics") or "").lower()
                    own = 1
                    if "over (" in low or "over(" in low or "from (select" in low:
                        own = 4
                    elif " join " in low or "case when" in low:
                        own = 3
                    elif "group by" in low:
                        own = 2
                # Difficulty matching must factor in **mastery**, not just the static seed difficulty.
                # Per the study companion `difficulty_policy.py:94-139`:
                #     combined = (seed difficulty normalized + mastery) / 2 → _level() → 2/3/4
                # I.e. "how hard this knowledge point should be asked now" is a **joint function of seed difficulty and mastery**,
                # not a constant written on the node. MVE previously only read `det["difficulty"]`
                # (seed difficulty), missing "where Voyager is now" → the gradient was one dimension short,
                # so the curve could only step 0→100 (observed over 42 logs).
                own_m = float((_dim_mastery().get(d) or {}).get("mastery") or 0.0)
                try:
                    from difficulty import seed_unit as _su, _level as _lv
                    fit = _lv((_su(own) + own_m) / 2.0)
                except Exception:                        # pragma: no cover
                    fit = own
                score += abs(fit - int(difficulty or 2)) * 3
                # Study companion readiness: what is not yet mastered comes first (a failed prerequisite does not advance to the next tier).
                # Low-mastery dimensions take priority in **low tiers** but not in high tiers —— otherwise
                # a hard question on an unknown dimension right away produces another step change.
                if own_m < 0.5:
                    score += (-4 if int(difficulty or 2) <= 2 else +4)
                cands.append((score, d, {
                    "dimension": d,
                    "subject": subj,
                    "subject_key": k,
                    "table": tbl,
                    "columns": list(cols),
                    "semantics": str(det.get("semantics") or ""),
                    # ↓↓↓ the KNOWLEDGE group of the study companion's `project_target_topic_evidence()`.
                    # These four fields **were copied into the knowledge graph along with the scaffolding** (question_types 15/15,
                    # examples 15/15, typical_misconceptions 13/15, skills 9/15 have values),
                    # but the question generator never read them —— the material was moved over but nobody used it.
                    "question_types": list(det.get("question_types") or []),
                    "skills": list(det.get("skills") or []),
                    "examples": list(det.get("examples") or [])[:2],
                    # ⚠️ Misconceptions must use `typical_misconceptions` (**the full version**), not
                    # `typical_errors` (the truncated one). Observed: one item was missing, and the missing one was exactly the
                    # easiest to trip over: fb_conv's "agg_first_blood_stats is already one row per round_id,
                    # do not JOIN rounds to dedupe, it will inflate the row count".
                    "misconceptions": list(
                        det.get("typical_misconceptions")
                        or det.get("typical_errors") or [])[:4],
                    # Structural skeleton: **whether difficulty 4 can be produced depends entirely on giving it**.
                    # Observed: without a skeleton, 4 consecutive tries all had the model writing
                    # `COUNT(*) OVER (ORDER BY ...)` (a cumulative count, not a consecutive-run count)
                    # or writing `team_name` into the rounds table that lacks that column —— 4/4 all dead.
                    # Yet the correct shape of gaps-and-islands **is right there in the knowledge graph** (the desensitized
                    # recipe, which even marks where the WHERE should go). Let the model modify values per the skeleton
                    # instead of inventing from zero —— this is the last layer that "question points belong to the server" should cover.
                    # For dimensions without a recipe in the knowledge graph, fall back to the human-reviewed SQL from the seed question.
                    "skeleton": str(det.get("recipe") or _seed_skeleton(d))[:700],
                    "own_difficulty": own,
                    # Percentage dimensions must fetch the sample size along with the value (see the require_base hint in propose)
                    "require_base": bool(specs.get(d, {}).get("needs_base")),
                }))

    # ---- Candidate source 2: the **edges** from human import (no nodes created; see knowledge_graph.link_import) ----
    #
    # Why this section is necessary: the knowledge graph's dimension nodes all derive from `TASKS[].rubric` (only questions that have been
    # asked enter the graph), while human-imported questions **deliberately create no nodes** —— so the loop above never picks up
    # human directions, and adaptive question generation circles back to old questions. For human-defined questions to influence question generation, the entry point can only be on
    # edges: the edge records the tables/columns/skeleton it actually used (from that real run's SQL).
    try:
        import knowledge_graph as _kg
        for lk in _kg.load_imported_links():
            if not lk.get("covered"):
                continue
            d = str(lk.get("dimension") or "").strip()
            if not d or not specs.get(d, {}).get("by_sql"):
                continue                 # directions that never really ran SQL are not used for question generation
            sql = str(lk.get("sql") or "")
            shape = _kg.sql_shape(sql, list(lk.get("tables") or []))
            if not shape["tables"]:
                continue
            tbl = shape["tables"][0]
            cols = (shape["columns_by_table"].get(tbl)
                    or shape["columns"] or [])
            # The human-imported edge itself carries no teaching material; if the dimension name matches an existing node, borrow it
            lk_det: dict[str, Any] = {}
            _node = g.nodes.get(f"dim:{d}")
            if _node is not None:
                lk_det = getattr(_node, "detail", None) or {}
            # The human's question text comes with its own subject (the grain of that data retrieval); that is where the "new" is
            subj = dict(lk.get("subject") or {}) or {"series": SERIES}
            key = (json.dumps(subj, sort_keys=True), d)
            if key in avoid:
                continue
            score = -6                   # human-imported directions take priority (weaker than prefer's -10)
            if prefer and d == prefer:
                score -= 10
            own = int(shape["difficulty"] or 2)
            score += abs(own - int(difficulty or 2)) * 3
            cands.append((score, d, {
                "dimension": d,
                "subject": subj,
                "subject_key": next((k for k in subj
                                     if k not in ("series", "team")), "series"),
                "table": tbl,
                "columns": list(cols),
                "semantics": shape["semantics"],
                # The edge has no teaching-material fields (it records that real run's SQL). But the human's question
                # often lands on an **existing dimension** —— then borrow that node's material,
                # otherwise human-imported directions would always be "bare-skeleton question generation", one layer short of seed directions.
                "question_types": list(lk_det.get("question_types") or []),
                "skills": list(lk_det.get("skills") or []),
                "examples": list(lk_det.get("examples") or [])[:2],
                "misconceptions": list(
                    lk_det.get("typical_misconceptions")
                    or lk_det.get("typical_errors") or [])[:4],
                "skeleton": str(shape["recipe"] or "")[:700],
                "own_difficulty": own,
                "require_base": bool(specs.get(d, {}).get("needs_base")),
                "source": "human_link",           # provenance: this direction comes from a human-imported edge
                "seed_question": str(lk.get("question") or "")[:300],
                "cover_level": str(lk.get("cover_level") or ""),
                "insights": list(lk.get("insights") or []),
            }))
    except Exception:                                        # pragma: no cover
        pass

    if not cands:
        return {}
    cands.sort(key=lambda x: (x[0], x[1]))
    return cands[0][2]


async def propose(*, about: str = "", difficulty: int = 2,
                  critique: str = "", avoid: str = "",
                  blueprint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stage 2: the LLM produces the question text + data-retrieval recipe (**no answer values included**).

    `critique` is **the previous round's validation failure reason**, fed back verbatim —— this is the original Voyager's
    iterative prompting / critic, and the move already proven effective in `action_code.collect()`:
    a retry without a reason only makes the model make the same mistake in a different way.

    `avoid` is the **list of already-produced (subject, dimension) pairs**. Giving it beforehand is cheaper than blocking afterward:
    observed: without it, the model collides with existing questions every round, burning two calls for nothing.
    """
    band = DIFFICULTY_RUBRIC.get(int(difficulty) or 2) or {}
    band_txt = ""
    if band:
        band_txt = (
            f"\n\nWhat difficulty {difficulty} means (the server will **count your SQL against this line word by word**,"
            f"and reject anything below it): {band.get('needs', '')}."
            f"The recipe must contain one of {' / '.join(band.get('any_of') or {})}.")
    user = (f"Produce one question around \"{about or 'this match'}\" at difficulty {difficulty}."
            "Remember: give the data-retrieval recipe, not the answer values." + band_txt)
    if blueprint:
        # The server has already picked the question point —— the model **may not change it**, only fill in the values.
        # This is the isomorphic counterpart of the study companion's `resolve_target_question_type` + `enforce_mapped_question_type`
        # pair: question points belong to the server, question text belongs to the model.
        bp = (
            f"\n\n【Question point already picked by the server —— do not change it, produce the question per it】"
            f"\n  The dimension field must be written as: {blueprint.get('dimension')}"
            f"\n  subject must be written as: {json.dumps(blueprint.get('subject') or {}, ensure_ascii=False)}"
            f"  (for the {blueprint.get('subject_key')} value, look up a real one in the database and fill it in)"
            f"\n  FROM must be this table: {blueprint.get('table')}"
            f"\n  Only these columns may be used: {', '.join(blueprint.get('columns') or [])}"
            f"\n  Follow this convention shape: {str(blueprint.get('semantics') or '')[:160]}"
            f"\n  **Do not** write column names that do not appear above, and do not switch tables. The table's columns are exactly these;"
            f"there are no common-sense columns like player_name / team_name —— the player column is fb_player, the team column is fb_team."
        )
        # ── The question type is decided by the **knowledge-point node declaration**, not via the model ──
        # Study companion `question_type_mapping.py:131`: take the node's own question_types
        # first item, mapped to a machine question type, "map it without LLM input".
        qt = [str(x) for x in (blueprint.get("question_types") or []) if x]
        if qt:
            kind = ("write SQL" if "sql" in qt[0]
                    else "call a tool to fetch the value" if "tool" in qt[0] else qt[0])
            bp += (f"\n  Answer recipe type (**declared by the knowledge-point node, may not be changed**): {qt[0]}"
                   f" → answer_spec must give {kind}")
        sk = [str(x) for x in (blueprint.get("skills") or []) if x]
        if sk:
            bp += f"\n  Skills tested by this point: {', '.join(sk[:4])}"

        # Misconceptions use the **full version** (typical_misconceptions), not the truncated typical_errors.
        # Question generation should "elicit" these mistakes, not give the answer away in the question text.
        mis = [str(x) for x in (blueprint.get("misconceptions") or []) if x]
        if mis:
            bp += "\n  ★ This knowledge point's **typical misconceptions** (your question must distinguish \"really knows\" from \"guessed right\";"
            bp += "do not give the answer away directly in the question text):"
            for m in mis:
                bp += f"\n    - {m}"
        # Percentage conventions must **fetch an extra column** as the sample size. Without this line, the model only fetches the
        # numerator every time (observed: 4 consecutive rejections by validation for "no denominator/sample size", burning 4 calls for nothing).
        # Saying it clearly beforehand is cheaper than feeding critique back afterward.
        if blueprint.get("require_base"):
            bp += (f"\n  ★ This dimension is a **percentage**: besides the ratio itself, the SQL must fetch an extra column"
                   f"as the sample size/denominator, and mark which column it is with `base_column` in answer_spec"
                   f"(counting from 0). Questions giving only the ratio without the sample size are always rejected.")
        skel = str(blueprint.get("skeleton") or "").strip()
        if skel:
            # The skeleton is **the correct shape given by the server** (desensitized, '?' is a placeholder).
            # Observed without a skeleton: difficulty 4 rejected 4 consecutive times (written as cumulative count / fabricated column).
            bp += (
                "\n  ★ Write your SQL per this **structural skeleton** (replace '?' with the real values you look up;"
                "the shape may not be changed):\n" + skel +
                "\n  The **position of WHERE in the skeleton is the convention**: number in the inner layer, filter in the outer layer."
                "Do not stuff all the filter conditions into the innermost layer —— stuff them in and the run breaks.")
        # ⚠️ Pitfall hit in practice: when the convention judges win/loss with CASE WHEN, the model **also writes that column into WHERE**
        # (`WHERE winning_team_name='X'`) → only the won rounds remain, so the win rate is always 100 or directly
        # empty. WHERE only filters the scope (series/map); win/loss must be judged by CASE.
        if "case when" in (str(blueprint.get("semantics") or "")
                           + str(blueprint.get("skeleton") or "")).lower():
            bp += ("\n  ★★ Win/loss must be judged by **CASE WHEN**;"
                   "**do not** write the win/loss-judging column into WHERE again —— write it in and only won"
                   " rounds remain, so the win rate is always 100 or the query runs empty. WHERE is only used to limit the scope"
                   " (series_id / map_name); win/loss is judged in SELECT.")
        # Example question: what the node's own **correct convention looks like**. The skeleton is a desensitized shape; the example question has real values;
        # together they give the model both shape and landing (values still need to be looked up in the database by it).
        ex = [str(x) for x in (blueprint.get("examples") or []) if x]
        if ex:
            bp += ("\n  Reference example (**follow its convention shape**, replace values with the real ones you look up;"
                   "do not copy the literals verbatim):\n" + str(ex[0])[:320])
        user += bp
    if avoid:
        user += ("\n\nThe following (subject, dimension) combinations **already have questions; do not produce them again**:\n"
                 + avoid)
    if critique:
        user += ("\n\nThe previous round's question was blocked by **deterministic validation**, with the reasons listed one by one."
                 "Every one must be fixed before producing a new question (do not change the packaging but not the substance):\n" + critique)
    out = chat_json(
        [
            {"role": "system", "content": _PROPOSE_SYSTEM.format(
                skeleton=await _server_skeleton(), tools=", ".join(TOOL_NAMES),
                SERIES=SERIES, C9=C9)},
            {"role": "user", "content": user},
        ],
        temperature=0.4,
    )
    return out if isinstance(out, dict) else {}


# --------------------------------------------------------------------------
# Stage 3: deterministic validation (the study companion's answer_supported is judged by a second LLM —— here we use data)
# --------------------------------------------------------------------------
async def _run_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Run the data-retrieval recipe once, returning {value, base, error}. **This is the entire basis of validation**."""
    sql = str(spec.get("sql") or "").strip()
    if sql:
        if not sql.lower().startswith("select"):
            return {"error": "sql must start with SELECT"}
        try:
            res = await vlml_env.execute_custom_sql(sql_query=sql)
        except Exception as e:
            return {"error": f"SQL execution failed: {type(e).__name__}: {str(e)[:120]}"}
        rows = (res or {}).get("rows") or []
        if not rows:
            return {"error": "SQL returned 0 rows —— this question is unsolvable in this match's data"}
        row = rows[0]
        vc = int(spec.get("value_column") or 0)
        if vc >= len(row):
            return {"error": f"value_column={vc} exceeds the returned column count {len(row)}"}
        value = row[vc]
        base = None
        bc = spec.get("base_column")
        if bc is None and len(row) >= 2:
            # The model often produces two columns "value + COUNT(*) sample size" but forgets to fill base_column
            # (observed: round 3 was missing exactly this field). The server can see the column count, so fill it in for it:
            # take the last column that is not value_column.
            others = [i for i in range(len(row)) if i != vc]
            if others:
                bc = others[-1]
        if bc is not None and int(bc) < len(row):
            base = row[int(bc)]
        # **The row count must be returned to the caller**: the referee can only judge one scalar (value_column takes row[0]), so
        # returning multiple rows means the truth is only "whichever row happens to be first" —— see gate 3c below.
        return {"value": value, "base": base, "base_column": bc,
                "n_rows": len(rows)}

    tool = str(spec.get("tool") or "").strip()
    if tool:
        fn = getattr(vlml_env, tool, None)
        if fn is None:
            return {"error": f"no tool {tool}"}
        args = dict(spec.get("tool_args") or {})
        try:
            res = await fn(**args)
        except Exception as e:
            return {"error": f"tool call failed: {type(e).__name__}: {str(e)[:120]}"
                             f" (the arguments passed were {args})"}
        if isinstance(res, dict) and "error" in res and len(res) <= 2:
            # Observed: pattern_detection_report returns {"error": ...} without team_name,
            # at which point no path can fetch anything —— it must be stated clearly that it is an **argument** problem, not a path problem.
            return {"error": f"tool returned error {str(res.get('error'))[:80]}"
                             f" —— most likely a required argument was not passed (currently passed {args})"}
        vp = str(spec.get("value_path") or "")
        if not vp:
            return {"error": "took the tool path but gave no value_path"}
        value = dig(res, vp)
        bp = str(spec.get("base_path") or "")
        base = dig(res, bp) if bp else None
        got = {"value": value, "base": base}
        if _empty(value):
            got["hint"] = ("these top-level sections are in the return:"
                           + ", ".join(list((res or {}).keys())[:8]))
        return got

    return {"error": "answer_spec has neither sql nor tool"}


_ROUNDS_TOTAL: int | None = None


async def _max_rounds() -> int:
    """Total rounds across the match —— the magnitude cap for count-type dimensions.

    Why it is needed: observed: the model wrote `SELECT SUM(round_number) FROM rounds ...`,
    treating a **sum of round numbers** as "how many rounds were played", producing 145 —— non-empty, runnable, and a legal dimension,
    passing the first six gates. Only the gate "the value cannot exceed the total rounds of the match" can stop it.
    """
    global _ROUNDS_TOTAL
    if _ROUNDS_TOTAL is None:
        try:
            r = await vlml_env.execute_custom_sql(
                f"SELECT COUNT(*) FROM rounds WHERE series_id='{SERIES}'")
            _ROUNDS_TOTAL = int((r.get("rows") or [[59]])[0][0])
        except Exception:                                    # pragma: no cover
            _ROUNDS_TOTAL = 59
    return _ROUNDS_TOTAL


def _fix_for_error(err: str, sql: str = "") -> str:
    """Translate execution errors into **actionable fixes** (reporting only symptoms does not move the model; observed: three rounds all unmoved).

    Isomorphic to that critic in `action_code.collect()`: just saying "what you wrote is not in the return"
    does not work; the real candidate paths must be given out along with it.
    """
    if "0 rows" in err or "empty" in err:
        # Judge the table name first: observed: the model's favorite is to treat a **tool name** as a table name (match_rounds_report and the like);
        # just saying "copy the table structure" makes it swap in another tool name and keep making the same mistake.
        m = re.search(r"\bfrom\s+([a-z_][a-z0-9_]*)", (sql or "").lower())
        tbl = m.group(1) if m else ""
        if tbl and _COLS_CACHE and tbl not in _COLS_CACHE:
            return (f"`{tbl}` in FROM is not a table name (that is a **tool name**; tools can only go through the tool path,"
                    f"not in FROM). SQL can only query these tables: {', '.join(_SQL_TABLES)}."
                    f"\n  rounds table's columns: {', '.join(_COLS_CACHE.get('rounds', [])[:16])}")
        bad = _bad_columns(sql, tbl)
        if bad:
            # Name the fabricated columns, and give the real columns of the table it came FROM
            near = _COLS_CACHE.get(tbl) or _COLS_CACHE.get("rounds") or []
            tip = ""
            if any("team" in b for b in bad):
                tip = (" the rounds table has no team_name column; filter teams with winning_team_name;"
                       "for team-level aggregates go to agg_team_game_stats.")
            return (f"These in the SQL are not columns of `{tbl or 'the target table'}`: {', '.join(bad)}."
                    f"{tip}\n  `{tbl or 'rounds'}`'s real columns: {', '.join(near[:18])}")
        return ("SQL returned 0 rows: table and column names must be copied from the ★ table-structure list;"
                f"use series_id '{SERIES}' in WHERE, and do not make the conditions too strict.")
    if "required argument" in err or "tool returned error" in err:
        return "Add the required arguments to tool_args (copy the argument names from \"real tool signatures\")."
    if "denominator" in err or "value_path" in err:
        return "value_path must be copied from the leaf paths in \"tool return structures\" (no leading dot)."
    if "no tool" in err:
        return "tool must be chosen from the \"available tools\" list."
    if "clamp" in err or "value_column" in err:
        return "value_column refers to which column of the SELECT (counting from 0); do not exceed the column count."
    return ("Check \"real tool signatures\", \"table structure\", and \"return structure\" word by word;"
            "do not invent table names, column names, or field names.")


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _difficulty_gap(candidate: dict[str, Any], want: int) -> str:
    """Does this question's recipe justify its self-declared difficulty? Returns the gap description, or an empty string when justified.

    The criterion must be **countable** (counting keywords in the SQL), not model self-assessment ——
    self-assessment means letting it grade itself.
    """
    need = DIFFICULTY_RUBRIC.get(int(want) or 2)
    if not need:
        return ""
    rubric = [rp for rp in (candidate.get("rubric") or []) if isinstance(rp, dict)]
    sqls = []
    for rp in rubric:
        spec = rp.get("answer_spec") if isinstance(rp, dict) else None
        if isinstance(spec, dict):
            s = str(spec.get("sql") or "")
            if s:
                sqls.append(s.lower())
    if not sqls:
        # Tool path: no SQL to count; can only look at the number of rubric points (weak criterion, does not block)
        return ""
    joined = " \n ".join(sqls)
    has_group = "group by" in joined
    for feat, kws in (need.get("any_of") or {}).items():
        if any(k in joined for k in kws):
            return ""
    # Weak criterion: the same tier can also be reached by "splitting more finely" —— grouping + enough rubric points.
    # Without this, difficulty 2 would kill "two rubric points but no GROUP BY" questions by mistake (observed:
    # `SELECT COUNT(*)...` judged below standard, when it is in fact a proper tier-2 question).
    alt = int(need.get("or_points") or 0)
    if alt and len(rubric) >= alt and (has_group or not need.get("or_points_need_group")):
        return ""
    return f"must contain one of {' / '.join(need.get('any_of') or {})}" \
           + (f", or grouping with no fewer than {alt} rubric points" if alt else "")


# Difficulty tiers: each tier spells out "what counts as this difficulty".
#
# Why it must be hardcoded: previously the prompt had only the line `difficulty {n}`, and the schema only had
# `"difficulty": 1-4` —— the model interpreted freely and the server never validated. So the question generator passed in
# the difficulty computed by the gradient, yet the generated question might still be a single `SELECT COUNT(*)`,
# making "generate different questions by difficulty" exist in name only.
#
# Mirroring the study companion's approach: the study companion's difficulty is **laid out across the question bank** (82 knowledge points × 3 tiers, each tier's
# question text and rubric point count differ), not improvised at generation time. MVE does not have that question bank,
# so it compresses "what each tier looks like" into countable SQL features, counted one by one after generation.
DIFFICULTY_RUBRIC: dict[int, dict[str, Any]] = {
    1: {"needs": "single-table single-metric aggregation",
        "any_of": {"aggregate function": ["count(", "avg(", "sum(", "max(", "min("]}},
    2: {"needs": "grouped aggregation (GROUP BY) or multiple rubric points",
        "any_of": {"grouping": ["group by"]},
        "or_points": 2},
    3: {"needs": "grouping + conditional filtering, or multi-table JOIN",
        "any_of": {"multi-table JOIN": [" join "],
                   "conditional branch": ["case when", " having ", "coalesce("]},
        "or_points": 3, "or_points_need_group": True},
    4: {"needs": "window function / self-join / nested subquery (gaps-and-islands, longest consecutive, ranking)",
        "any_of": {"window function": ["over (", "over("],
                   "nested subquery": ["from (select", "from(select"]}},
}


# ---------------------------------------------------------------------------
# The criterion for whether a question **passes** (set on 2026-10-08, do not add more gates)
# ---------------------------------------------------------------------------
#   The passing line is a single one: **let VLML run it once; a non-empty value means it passes**.
#
# Why this line: questions are for Voyager to do, and Voyager's answers are compared against the "truth VLML produces".
# A question that cannot produce a value has no truth at all, equivalent to giving Voyager an ungradeable question —— this is
# the only one that must be blocked.
#
# The study companion's isomorphic slot is `answer_supported` (the answer can be supported by the material), judged by a **second LLM's
# opinion**; MVE has a real data source and can judge **deterministically** —— one run is enough, no need to ask the
# model. This is where MVE is stronger than the study companion, not a place to catch up with it.
#
# The few gates below in `STRICT_MODE` (percentage out of range / magnitude / non-numeric value / "consecutive" shape)
# are **sanity backstops**, not the passing line. They were added one by one when hitting pitfalls (each one's comment
# records which question was killed at the time), but **the correct usage is to let them retire gradually**: once the skeleton is given correctly,
# the model stops writing wildly, and these gates should catch nothing. To use only the passing line, run `--loose`.
# ---------------------------------------------------------------------------
STRICT_MODE = True


async def validate(candidate: dict[str, Any], *,
                   existing: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """Validation. **Does not ask the model**, only asks the data —— let VLML run it once; a non-empty value means it passes.

    Returns {ok, checks, reasons}. `checks` lists each rubric point's validation result one by one ——
    the validation conclusion must be reviewable, not just a boolean.
    """
    existing = existing or set()
    specs = _dim_specs()
    reasons: list[str] = []
    checks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()      # in-question dedup (existing questions distinguish maps by subject)

    if not str(candidate.get("question") or "").strip():
        return {"ok": False, "checks": [], "reasons": ["no question text"]}
    rubric = candidate.get("rubric")
    if not isinstance(rubric, list) or not rubric:
        return {"ok": False, "checks": [], "reasons": ["no rubric points"]}

    for rp in rubric:
        point = str(rp.get("point") or "").strip() or "(no text)"
        dim = str(rp.get("dimension") or "").strip()
        subj = rp.get("subject") if isinstance(rp.get("subject"), dict) else {}
        spec = rp.get("answer_spec")
        row: dict[str, Any] = {"point": point, "dimension": dim}

        if not dim:
            row["ok"] = False
            row["why"] = "no dimension"
            checks.append(row)
            reasons.append(f"「{point}」has no dimension")
            continue
        key = (json.dumps(subj, sort_keys=True, ensure_ascii=False), dim)
        if key in seen:
            # In-question collision: existing questions distinguish maps by subject (one
            # map_rounds each for Corrode/Haven/Lotus), so two identical (subject, dimension) pairs in one question
            # mean the model wrote two rubric points as the same query.
            row["ok"] = False
            row["fix"] = ("This question already has the same (subject, dimension) pair."
                          "The two rubric points must use different dimensions, or be distinguished by subject"
                          "(e.g. add a map / player key).")
            row["why"] = "duplicates another rubric point in this question"
            checks.append(row)
            reasons.append(f"「{point}」duplicates another rubric point in this question ({dim})")
            continue
        seen.add(key)
        if key in existing:
            row["ok"] = False
            row["fix"] = (f"This (subject, dimension) pair already has a question: "
                          f"subject={json.dumps(subj, ensure_ascii=False)}, "
                          f"dimension={dim}. Write subject in more detail (add a map or "
                          "player key), or switch to a different dimension.")
            row["why"] = "a (subject, dimension) pair duplicating an existing question"
            checks.append(row)
            reasons.append(f"「{point}」duplicates an existing question ({dim})")
            continue
        if not isinstance(spec, dict):
            row["ok"] = False
            row["why"] = "no answer_spec (cannot trust the model's answer value)"
            checks.append(row)
            reasons.append(f"「{point}」has no data-retrieval recipe")
            continue
        # The model sneaks in an answer value → rejected directly (the study companion accepts it, MVE does not)
        if any(spec.get(k) is not None for k in ("expected", "expected_value",
                                                 "answer", "value")):
            row["ok"] = False
            row["why"] = "the recipe smuggles in an answer value —— rejected"
            checks.append(row)
            reasons.append(f"「{point}」the recipe smuggles in an answer value")
            continue

        # Gate 0: the dimension must be declared by the server (the model may not invent conventions)
        if dim not in specs:
            row["ok"] = False
            row["fix"] = (f"dimension must be chosen from the ★ enum (the {dim} just filled is not in it). "
                          f"Available: {', '.join(sorted(specs))}")
            row["why"] = f"dimension {dim} is not among the server-declared conventions"
            checks.append(row)
            reasons.append(f"「{point}」dimension {dim} is not declared by the server")
            continue
        # Gate 0b: the question text must be relevant to the dimension definition (the study companion's relevant; here judged by keyword overlap)
        dspec = specs[dim]
        desc = str(dspec.get("desc") or dim)
        if not _relevant(point, desc):
            guess = _suggest_dims(point, specs)
            row["ok"] = False
            row["fix"] = (f"dimension={dim}'s server definition is \"{desc}\", which does not match the question text. "
                          + (f"Judging from the question text, more likely: {', '.join(guess)}. "
                             if guess else "the point text must be written around this dimension's definition."))
            row["why"] = f"the question text is irrelevant to the dimension (this dimension's server definition is \"{desc}\")"
            checks.append(row)
            reasons.append(f"「{point}」is irrelevant to dimension {dim} (definition: {desc})")
            continue
        # Gate 1: percentage conventions must give a denominator (blocks "asks for a win rate but fetches a count")
        wants_pct = str(dspec.get("kind")) == "percent" and bool(dspec.get("needs_base"))
        if wants_pct:
            # Which path is actually taken is decided by `_run_spec`'s judgment (SQL if there is sql)
            by_tool = not str(spec.get("sql") or "").strip() and \
                bool(str(spec.get("tool") or "").strip())
            has_base = bool(str(spec.get("base_path") or "").strip()) or \
                spec.get("base_column") is not None
            # The SQL path allows **runtime backfill** (the model producing two columns but forgetting base_column is commonplace;
            # the server can see the column count and can fill it in for it), so no pre-check here; check after running whether it got backfilled.
            # The tool path has no concept of "columns" and cannot be inferred; base_path must be declared in spec.
            if by_tool and not has_base:
                row["ok"] = False
                row["fix"] = "Via tool: add base_path pointing at the same-level .denom."
                row["why"] = "percentage convention but no denominator given (asks for a win rate but fetches a count)"
                checks.append(row)
                reasons.append(f"「{point}」percentage convention missing a denominator")
                continue
            if by_tool and not bool(spec.get("percent")):
                row["ok"] = False
                row["fix"] = "The tool-path percentage convention must write \"percent\": true."
                row["why"] = "the tool-path percentage convention must write percent=true"
                checks.append(row)
                reasons.append(f"「{point}」tool path missing percent=true")
                continue

        got = await _run_spec(spec)
        if got.get("error"):
            row["ok"] = False
            row["fix"] = _fix_for_error(str(got["error"]),
                                        str(spec.get("sql") or ""))
            row["why"] = got["error"]
            checks.append(row)
            reasons.append(f"「{point}」{got['error']}")
            continue
        # After running, a percentage convention must obtain a denominator/sample size (the SQL path's auto-backfill pays off here)
        if wants_pct and _empty(got.get("base")):
            row["ok"] = False
            row["fix"] = (
                "Via tool: base_path must point at the same-level .denom."
                if str(spec.get("tool") or "").strip() else
                "Via SQL: SELECT must return two columns —— the percentage value + COUNT(*) as the sample size, "
                "with base_column pointing at the sample-size column.")
            row["why"] = "percentage convention: denominator/sample size not obtained"
            checks.append(row)
            reasons.append(f"「{point}」percentage convention: denominator not obtained"
            )
            continue
        if _empty(got.get("value")):
            why = "the recipe ran empty —— this question is unsolvable in this match's data"
            if got.get("hint"):
                why += f" ({got['hint']})"
            row["ok"] = False
            row["why"] = why
            checks.append(row)
            reasons.append(f"「{point}」the value is empty"
            )
            continue

        min_base = int(rp.get("min_base") or 1)
        base = got.get("base")
        if base is not None and not _empty(base):
            try:
                if float(base) < min_base:
                    row["ok"] = False
                    row["why"] = f"sample size {base} < min_base {min_base}"
                    checks.append(row)
                    reasons.append(f"「{point}」insufficient sample size ({base} < {min_base})"
                    )
                    continue
            except (TypeError, ValueError):
                pass
        # Gate 2: the computed value must be a legal percentage (tool path value/base*100, SQL path used directly)
        if wants_pct:
            v, b = got.get("value"), got.get("base")
            try:
                if str(spec.get("tool") or "").strip():
                    pct = float(v) / float(b) * 100
                else:
                    pct = float(v)
            except (TypeError, ValueError, ZeroDivisionError) as e:
                row["ok"] = False
                row["why"] = f"percentage convention: cannot compute a value ({type(e).__name__})"
                checks.append(row)
                reasons.append(f"「{point}」percentage convention: cannot compute a value"
                )
                continue
            if STRICT_MODE and not (0.0 <= pct <= 100.0):
                row["ok"] = False
                row["why"] = f"the computed percentage {round(pct, 1)} is not in 0-100 —— the convention is wrong"
                checks.append(row)
                reasons.append(f"「{point}」percentage out of range ({round(pct, 1)})"
                )
                continue
            row["truth"] = round(pct, 1)

        # Gate 3 (strict): magnitude —— a count-type dimension's value cannot exceed the total rounds of the match
        if STRICT_MODE and str(dspec.get("kind")) == "count":
            try:
                v = float(got.get("value"))
            except (TypeError, ValueError):
                # The fetched value is not a number (observed fetching 'Haven' —— value_column pointed at
                # the map_name column). At this point pass must not let it through: a map name masquerading as the "longest losing
                # streak round count" slipping into the question bank would make the referee treat it as the truth, wasting the whole question's scoring.
                row["ok"] = False
                row["fix"] = (f"the value the recipe fetched is {got.get('value')!r}, not a number —— "
                              "value_column points at the wrong column (probably at label columns like map_name / "
                              "player_name). Change it to the numeric column.")
                row["why"] = f"the fetched value {got.get('value')!r} is not a number"
                checks.append(row)
                reasons.append(f"「{point}」the fetched value is not a number ({got.get('value')!r})"
                )
                continue
            else:
                cap = await _max_rounds()
                if v > cap:
                    row["ok"] = False
                    row["fix"] = (
                        f"value {got.get('value')} exceeds the total rounds of the match {cap}; the magnitude is wrong. "
                        "Probably SUM(round_number) was used as COUNT(*) —— "
                        "always use COUNT(*) to count rounds.")
                    row["why"] = f"the count value {got.get('value')} exceeds the cap {cap}"
                    checks.append(row)
                    reasons.append(f"「{point}」magnitude out of range ({got.get('value')} > {cap})"
                    )
                    continue

        # Gate 3b (general): as long as this rubric point is **compared numerically** (has numeric_tolerance,
        # or the dimension declaration is not enum/label), the value produced must be usable as a number.
        #
        # Why gate 3 above (count-only) alone is not enough: observed `max_losing_streak_map`
        # slipped in exactly like this —— its SQL is
        #   `SELECT map_name, MAX(cnt) ... GROUP BY map_name` + `value_column: 0`
        # fetching the **map name** 'Lotus', while the question text asks for the "longest losing streak round count" with tolerance=0.05.
        # The referee treats 'Lotus' as the truth, so any number the model submits is judged wrong → 5 straight rounds at 0%,
        # with the critique unchanged. That is not "can't learn", it is a **broken question**.
        # A broken question is worse than no question: it disguises itself as "the learning curve never took off", leading people in the wrong direction.
        if STRICT_MODE and str(dspec.get("kind")) != "count" and spec.get("sql"):
            try:
                float(got.get("value"))
            except (TypeError, ValueError):
                row["ok"] = False
                row["fix"] = (
                    f"the value the recipe fetched is {got.get('value')!r}, not a number —— "
                    "value_column points at the wrong column (probably at label columns like map_name / player_name). "
                    "Change it to the numeric column; "
                    "if that column is itself a grouping key, remove it from SELECT or move it later.")
                row["why"] = f"the fetched value {got.get('value')!r} is not a number (the question text is scored numerically)"
                checks.append(row)
                reasons.append(f"「{point}」the fetched value is not a number ({got.get('value')!r})")
                continue

        # Gate 3c (general, the decisive one): **the referee can only judge a single scalar**.
        # `_run_spec` takes the truth as `rows[0]` (question_gen.py:1133), so once the SQL returns
        # multiple rows, the truth is "whichever row happens to be first" —— the truth changes with row order.
        # Observed `plant_success_rate_lotus`: `GROUP BY round_id` returned 10 rows
        # [(1,1),(1,0),...], the referee took row0 → truth 100%, while the per-round success rate the question text asks for
        # aggregated over the whole match is 7/10=70% → **the question is mathematically unsolvable**, 15 practice rounds all at 0%,
        # disguised as "voyager can't learn anything". A broken question is worse than no question.
        # The only exemption: explicit ORDER BY + LIMIT 1 (the truth is decided by the sort, not by row-order luck).
        if STRICT_MODE and spec.get("sql"):
            n_rows = int(got.get("n_rows") or 1)
            sql_low = str(spec.get("sql")).lower()
            deterministic_single = (
                "limit 1" in sql_low and " order by " in sql_low)
            if n_rows > 1 and not deterministic_single:
                row["ok"] = False
                row["fix"] = (
                    f"this SQL returned {n_rows} rows, but the referee only takes the 1st row as the truth —— "
                    "the truth drifts with row order, so the question is dead. It must return exactly one row: "
                    "collapse the aggregation (wrap another SUM/AVG/COUNT outside), "
                    "or remove the GROUP BY; "
                    "if you really need 'the first row', write ORDER BY … LIMIT 1.")
                row["why"] = f"SQL returned {n_rows} rows, truth takes only the 1st row (row-order luck)"
                checks.append(row)
                reasons.append(f"「{point}」SQL returned multiple rows ({n_rows} rows), "
                               "the referee's truth is uncertain —— the question text and recipe convention disagree")
                continue

        row["ok"] = True
        row["why"] = "the recipe produces a non-empty value"
        row["value"] = got.get("value")
        row["base"] = base
        checks.append(row)

    ok = bool(checks) and all(c.get("ok") for c in checks)

    # ---- Gate 8 (question-level): difficulty meets the bar ----
    # Just stuffing the phrase "difficulty 4" into the prompt is useless: the model does not know what 4 means,
    # and nobody checks after generation —— so "generate different questions by difficulty" exists in name only; the question bank grew
    # but difficulty did not. Two things are done together here:
    #   (a) hardcode each tier's **verifiable features** in the server (see DIFFICULTY_RUBRIC);
    #   (b) after generation, **actually count**: whether the SQL has window functions / subqueries / JOIN.
    #
    # **Computed independently of ok**: previously this was inside `if ok:`, so the model died at earlier gates
    # (wrong table name, duplicate rubric points) and never received the "difficulty below the bar" feedback ——
    # observed: all three generations were `GROUP BY player_name`, with none of the structure difficulty 4 requires,
    # yet not a word of it in the critique. The difficulty gap must be **computed every time and fed back every time**,
    # so the model fixes "table name" and "difficulty shape" together, rather than learning of the next one only after fixing the current one.
    gap = _difficulty_gap(candidate, int(candidate.get("difficulty") or 2))
    if gap:
        reasons.append(f"difficulty below the bar (tier {candidate.get('difficulty')} required): {gap}")

    # ---- Gate 9 (question-level, strict): "consecutive" semantics must have a countable gaps-and-islands shape ----
    # `COUNT(*) OVER (ORDER BY ...)` is also a window function and can pass gate 8, but what it computes is
    # **a cumulative count** (how many have been lost up to the current row), and MAX of it = the total rounds lost,
    # which is not the "longest **consecutive** losing streak" at all. Observed `max_losing_streak_map` was generated exactly like this:
    # the difficulty gate passed, the structure gate passed, yet the convention is wrong —— so this question can never be answered correctly.
    #
    # The gaps-and-islands shape is countable; these two are recognized:
    #   (a) difference method: ROW_NUMBER appears ≥2 times (subtracting two numbers gives the run id)
    #   (b) reset method: LAG / SUM(...) OVER for breakpoint accumulation
    # Neither present → rejected, and this shape requirement is fed back to the model verbatim in fix.
    text = " ".join(
        [str(candidate.get("question") or "")]
        + [str(rp.get("point") or "")
           for rp in (candidate.get("rubric") or []) if isinstance(rp, dict)]
    ).lower()
    if STRICT_MODE and any(k in text for k in ("streak", "consecutive", "consecutively",
                                               "in a row", "losing streak", "winning streak",
                                               "win streak", "loss streak")):
        sqls = " \n ".join(
            str((rp.get("answer_spec") or {}).get("sql") or "").lower()
            for rp in (candidate.get("rubric") or []) if isinstance(rp, dict))
        if sqls.strip():
            n_rn = sqls.count("row_number")
            has_reset = ("lag(" in sqls or "lead(" in sqls
                         or ("sum(" in sqls and " over " in sqls))
            if n_rn < 2 and not has_reset:
                gap_msg = ("\"Consecutive/losing streak\" must use gaps-and-islands: "
                           "subtract two ROW_NUMBER()s to get the run id "
                           "(ROW_NUMBER() OVER (ORDER BY round_number) "
                           "- ROW_NUMBER() OVER (PARTITION BY <team> ORDER BY round_number)), "
                           "then use LAG/SUM() OVER for breakpoint reset. "
                           "COUNT(*) OVER (ORDER BY ...) is a **cumulative count**, not a consecutive-run count.")
                reasons.append(gap_msg)
                for c in checks:
                    c["ok"] = False
                    c.setdefault("fix", gap_msg)
                ok = False

    return {"ok": ok, "checks": checks, "reasons": reasons,
            "difficulty": candidate.get("difficulty"),
            "difficulty_gap": gap}


# --------------------------------------------------------------------------
# Stage 4: forcing the convention source (mirrors enforce_mapped_question_type)
# --------------------------------------------------------------------------
def _norm_args(raw: Any) -> dict[str, Any]:
    """Normalize the possibly-malformed tool_args into a dict.

    Observed: the model writes `{"team_name": "Cloud9"}` as `"team_name=Cloud9"`,
    or even as a JSON string —— `dict(...)` would throw ValueError and crash the question generator.
    Per the spirit of the study companion's `enforce_mapped_question_type`: malformed model formats are fixed by the server.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        s = raw.strip()
        if s.startswith("{"):
            try:
                got = json.loads(s)
                return dict(got) if isinstance(got, dict) else {}
            except Exception:
                return {}
        if "=" in s:
            out: dict[str, Any] = {}
            for part in s.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    out[k.strip()] = v.strip()
            return out
    return {}


def enforce(candidate: dict[str, Any]) -> dict[str, Any]:
    """Fix back the places the LLM may have broken to the server's conventions.

    The study companion forces `question_type` back; what MVE forces is **the ownership of the convention**:
    tools declared in the recipe must be in the whitelist, SQL must start with SELECT,
    tool_args must be a dict (otherwise the tool call crashes outright).
    """
    out = dict(candidate)
    # topic_id must be lowercase-underscore English —— the model may give Chinese (observed giving
    # one with a Chinese word for "granularity" appended); this id goes into the question bank, logs, and dashboard URLs,
    # so Chinese cannot stay. What the server can backfill, it does not reject over (per the study companion's enforce spirit).
    tid = str(out.get("topic_id") or "").strip()
    if tid:
        slug = re.sub(r"[^a-z0-9_]+", "_", tid.lower()).strip("_")
        slug = re.sub(r"_+", "_", slug)
        if not slug:
            slug = "gen_" + re.sub(r"[^a-z0-9]+", "", str(
                out.get("question") or "").lower())[:24] or "gen_topic"
        out["topic_id"] = slug
    specs = _dim_specs()
    rubric = []
    for rp in (out.get("rubric") or []):
        if not isinstance(rp, dict):
            continue
        rp = dict(rp)
        dim = str(rp.get("dimension") or "").strip()
        rec = specs.get(dim) or {}
        spec = rp.get("answer_spec")
        if isinstance(spec, dict):
            spec = dict(spec)
            tool = str(spec.get("tool") or "").strip()
            if tool and tool not in TOOL_NAMES:
                spec.pop("tool", None)       # not in the whitelist → removed; validation will reject
            sql = str(spec.get("sql") or "").strip()
            if sql and not sql.lower().startswith("select"):
                spec.pop("sql", None)
                sql = ""
            if sql:
                # SQL and tool paths are **mutually exclusive**: keeping two mutually contradictory recipes means the referee and validation
                # each interpret their own way (observed: the fix hint therefore went down the wrong branch).
                for k in ("tool", "tool_args", "value_path", "base_path"):
                    spec.pop(k, None)
            spec["tool_args"] = _norm_args(spec.get("tool_args"))
            # Paths must not carry a leading dot (the model likes writing ".key_metrics...")
            for k in ("value_path", "base_path"):
                if str(spec.get(k) or "").startswith("."):
                    spec[k] = str(spec[k]).lstrip(".")
            # Convention ownership is decided by the server, not by whether the model remembers to write it: the dimension declares a percentage,
            # and the recipe gives a denominator, then percent must be true (per the study companion's
            # enforce_mapped_question_type approach of forcing back to the server-selected value).
            if str(rec.get("kind")) == "percent" and bool(rec.get("needs_base")):
                has_base = bool(str(spec.get("base_path") or "").strip()) or \
                    spec.get("base_column") is not None
                if has_base:
                    spec["percent"] = True
            rp["answer_spec"] = spec
        # Dimension filled wrong but **the recipe is right** → reverse-look-up by value path; the server fixes it back.
        # Observed: the model filled the pistol-round question as kast_pct three rounds in a row, while its value_path
        # was key_metrics.economy.pistol.num —— the path is a hard fact,
        # far more reliable than letting the model guess the dimension (per the study companion's enforce_mapped_question_type:
        # malformed ownership from the model is forced back by the server).
        desc0 = str(rec.get("desc") or dim)
        if dim and str(rp.get("point") or "") and not _relevant(
                str(rp.get("point") or ""), desc0):
            vpath = str((spec or {}).get("value_path") or "")
            if vpath:
                for d2, r2 in specs.items():
                    if d2 != dim and str(r2.get("value_path") or "") == vpath:
                        rp["dimension"] = d2
                        dim = d2
                        rec = r2
                        break
        rp["answer_spec"] = spec if isinstance(spec, dict) else rp.get("answer_spec")

        # Only correct tools and arguments under the **tool path** (do not stuff in a contradictory recipe when on SQL)
        if isinstance(spec, dict) and not str(spec.get("sql") or "").strip():
            # Wrong tool name: whoever the knowledge graph says produces this dimension, use them (observed: the model wrote
            # pattern_detection_report as match_summary_report, then reported
            # "missing 1 required positional argument: series_id").
            prods = [p for p in (rec.get("producers") or []) if p in TOOL_NAMES]
            if prods and str(spec.get("tool") or "") not in prods:
                spec["tool"] = prods[0]
            # Forgotten arguments: subject declares series / team / map, and the server backfills them per the signature.
            # "No team_name passed means no data" is the most frequent observed cause of rejected questions.
            subj = rp.get("subject") if isinstance(rp.get("subject"), dict) else {}
            args = dict(spec.get("tool_args") or {})
            try:
                import action_code
                sig = str(action_code.TOOL_SIGNATURES.get(
                    str(spec.get("tool") or ""), ""))
            except Exception:                                # pragma: no cover
                sig = ""
            for k_dst, k_src in (("series_id", "series"),
                                 ("team_name", "team"),
                                 ("map_name", "map"),
                                 ("player_name", "player")):
                if k_dst in sig and not args.get(k_dst) and subj.get(k_src):
                    args[k_dst] = subj[k_src]
            if "series_id: 'str'" in sig and not args.get("series_id"):
                args["series_id"] = SERIES
            spec["tool_args"] = args
            if str(rec.get("kind")) == "percent" and bool(rec.get("needs_base")):
                has_base = bool(str(spec.get("base_path") or "").strip()) or \
                    spec.get("base_column") is not None
                if has_base:
                    spec["percent"] = True
            rp["answer_spec"] = spec

        rubric.append(rp)
    out["rubric"] = rubric
    out["requires_tools"] = [
        t for t in (out.get("requires_tools") or []) if t in TOOL_NAMES]
    return out


# --------------------------------------------------------------------------
# Persist / load
# --------------------------------------------------------------------------
def load_generated() -> list[dict[str, Any]]:
    try:
        raw = json.loads(STORE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def adopt(candidate: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist a validated question (with validation evidence, so "why it passed at the time" can be reviewed afterward)."""
    items = load_generated()
    rec = dict(candidate)
    rec["_validation"] = {
        "checks": checks,
        "adopted_at": __import__("datetime").datetime.now().isoformat(
            timespec="seconds"),
    }
    items = [it for it in items
             if str(it.get("topic_id")) != str(rec.get("topic_id"))]
    items.append(rec)
    STORE.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    return rec


async def revalidate_store() -> list[dict[str, Any]]:
    """Retroactively re-check **already-stored** generated questions —— new gates do not reach the existing stock, so an extra pass is needed.

    Why it is necessary: validation rules were added incrementally, while questions were stored first. Observed:
    `max_losing_streak_map` was persisted before the new gate ("the fetched value must be a number") was added,
    and afterward, no matter how many rounds were rerun, it quietly sat in the bank as the "never answerable question":
    5 straight rounds at 0% with the critique unchanged —— it looked like "the learning curve never took off",
    when in fact **one broken question pinned the whole curve to the floor**.
    """
    items = load_generated()
    bad: list[dict[str, Any]] = []
    changed = False
    for rec in items:
        topic = str(rec.get("topic_id") or "")
        if not topic:
            continue
        try:
            vr = await validate(rec)
        except Exception as e:                               # pragma: no cover
            rec["invalid"] = True
            rec["invalid_reason"] = f"error during re-check: {type(e).__name__}: {e}"
            bad.append(rec); changed = True
            continue
        if not vr.get("ok") or vr.get("difficulty_gap"):
            rec["invalid"] = True
            rec["invalid_reason"] = ";".join(
                str(r) for r in (vr.get("reasons") or []))[:400]
            rec["invalid_note"] = ("rejected on re-check (not deleted): the recipe cannot pass under the **current** validation rules. "
                                   "Kept for review, but no longer enters the question bank.")
            bad.append(rec); changed = True
        elif rec.get("invalid"):
            # Restored once fixed —— re-checking goes both ways
            rec.pop("invalid", None); rec.pop("invalid_reason", None)
            rec.pop("invalid_note", None)
            changed = True
    if changed:
        STORE.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    return bad


def to_task(rec: dict[str, Any]) -> Task:
    """A persisted question → Task object (for tasks.py to merge into TASKS)."""
    rubric = []
    for rp in (rec.get("rubric") or []):
        spec = rp.get("answer_spec") if isinstance(rp.get("answer_spec"), dict) \
            else None
        aspec = None
        if spec is not None:
            aspec = AnswerSpec(
                sql=str(spec.get("sql") or ""),
                value_column=int(spec.get("value_column") or 0),
                base_column=spec.get("base_column"),
                numeric_tolerance=float(spec.get("numeric_tolerance") or 0.0),
                tool=str(spec.get("tool") or ""),
                tool_args=dict(spec.get("tool_args") or {}),
                value_path=str(spec.get("value_path") or ""),
                base_path=str(spec.get("base_path") or ""),
                percent=bool(spec.get("percent")),
            )
        rubric.append(RubricPoint(
            point=str(rp.get("point") or ""),
            subject=dict(rp.get("subject") or {}),
            dimension=str(rp.get("dimension") or ""),
            weight=float(rp.get("weight") or 50),
            min_base=int(rp.get("min_base") or 1),
            answer_spec=aspec,
        ))
    return Task(
        topic_id=str(rec.get("topic_id") or ""),
        question=str(rec.get("question") or ""),
        rubric=rubric,
        difficulty=int(rec.get("difficulty") or 2),
        # Auto-generated questions **default to not entering mastery** (per the study companion: only validated questions count).
        # It must first have its convention confirmed by a human or the referee —— generated ≠ active.
        validated_target=bool(rec.get("validated_target") or False),
        min_base=1,
        requires_tools=list(rec.get("requires_tools") or []),
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Automatic question generation: produce data-retrieval recipes + deterministic validation")
    ap.add_argument("--about", default="", help="what to produce questions around")
    ap.add_argument("--difficulty", type=int, default=2)
    ap.add_argument("--tries", type=int, default=3, help="maximum generation attempts")
    ap.add_argument("--adopt", action="store_true", help="merge into the bank once validated")
    args = ap.parse_args()

def existing_pairs() -> tuple[set[tuple[str, str]], str]:
    """The (subject, dimension) set of existing questions + the list shown to the model."""
    try:
        from tasks import TASKS
    except Exception:                                        # pragma: no cover
        return set(), ""
    pairs = {
        (json.dumps(p.subject, sort_keys=True, ensure_ascii=False),
         str(p.dimension))
        for t in TASKS.values() for p in t.rubric
    }
    text = "\n".join(
        f"  - {json.dumps(json.loads(s), ensure_ascii=False)} × {d}"
        for s, d in sorted(pairs))
    return pairs, text


def _critique_of(vr: dict[str, Any]) -> str:
    """Translate validation results into "what is wrong + how to fix" (the study companion's `generation_feedback`).

    Study companion: `entry_tutor_question_entries.py:1885` stuffs the previous round's `validation_failure`
    verbatim into `generation_feedback`. MVE is isomorphic, but adds one more line "how to fix" ——
    reporting only symptoms was observed to not move the model for three rounds (it makes the same mistake in a different way).
    """
    lines = [
        f"  - Rubric point 「{c.get('point')}」 wrong at: {c.get('why')}"
        + (f"\n      How to fix: {c['fix']}" if c.get("fix") else "")
        for c in vr.get("checks") or [] if not c.get("ok")
    ]
    # The difficulty gap is independent of the rubric-point gates —— see the comment on gate 8 in `validate`:
    # placed inside `if ok:`, the model would never receive this feedback.
    gap = str(vr.get("difficulty_gap") or "").strip()
    if gap:
        lines.append(
            f"  - 【Difficulty below the bar】tier {vr.get('difficulty')} required: {gap}. "
            "Either rewrite the SQL into that tier's shape, or honestly lower difficulty —— "
            "do not bluff your way through by changing the question text.")
    return "\n".join(lines)


async def generate_adopt(*, about: str = "", difficulty: int = 2,
                         tries: int = 3, do_adopt: bool = False,
                         verbose: bool = True,
                         focus_dimension: str = "") -> tuple[dict[str, Any] | None,
                                                             list[dict[str, Any]]]:
    """The **single entry point** for question-generation orchestration: generate → enforce → validate → (feed back and retry on failure) → persist.

    Both the CLI and `run_mve` go through here —— there is only one orchestration, so the two cannot diverge.
    Returns (persisted record or None, each round's validation details).
    """
    existing, avoid_txt = existing_pairs()
    # The question point is **picked by the server from the knowledge graph**, not handed to the model (per the study companion: the question type is declared by the knowledge-point node).
    bp = _graph_blueprint(difficulty, avoid=existing,
                          prefer=str(focus_dimension or ""))
    if bp:
        # Since the server has already picked it, the "scope" requirement the planner gave in `about` must yield ——
        # observed conflict between the two: `about` said "use player-level scope", while the knowledge graph picked
        # max_losing_streak, a **round-level** convention (the rounds table has no player column at all),
        # and the model caught in the middle wrote fb_player into the rounds table, failing all three times.
        # Question points belong to the server, question text belongs to the model —— cannot listen to both.
        about = (f"Produce a new question around the \"{bp.get('dimension')}\" convention, at "
                 f"{bp.get('subject_key')} granularity")
        # When the direction comes from a human-imported **edge**, give the human's original question to the model as the parent —— otherwise it sees only
        # a bare dimension name, and the question it produces is not what the human asked (this is where "influencing adaptive
        # question generation" truly lands).
        if bp.get("source") == "human_link" and bp.get("seed_question"):
            about += f". Reference the human-imported original question: {bp['seed_question']}"
        if verbose:
            print(f"  Knowledge-graph question point : {bp.get('dimension')} × {bp.get('subject_key')}"
                  f"｜table {bp.get('table')}"
                  f"｜columns {', '.join(bp.get('columns') or [])[:80]}")
    critique = ""
    trace: list[dict[str, Any]] = []
    for i in range(1, tries + 1):
        cand = enforce(await propose(about=about, difficulty=difficulty,
                                     critique=critique, avoid=avoid_txt,
                                     blueprint=bp))
        if not cand:
            if verbose:
                print(f"Attempt {i}: the model did not return usable JSON")
            continue
        if verbose:
            print(f"\n=== Generation attempt {i} ===")
            print(f"  topic_id  : {cand.get('topic_id')}")
            print(f"  Question  : {str(cand.get('question'))[:100]}")
            print(f"  Difficulty: {cand.get('difficulty')}")
            for rp in (cand.get("rubric") or []):
                sp = rp.get("answer_spec") or {}
                how = (f"SQL takes column {sp.get('value_column')}" if sp.get("sql")
                       else f"{sp.get('tool')} → {sp.get('value_path')}")
                print(f"    · {rp.get('point')}｜{rp.get('dimension')}｜{how}")
                if sp.get("sql"):
                    print(f"        SQL: {str(sp.get('sql'))[:220]}")
                if sp.get("tool_args"):
                    print(f"        args: {sp.get('tool_args')}")
                if sp.get("base_path") or sp.get("base_column") is not None:
                    print(f"        Denominator: {sp.get('base_path') or sp.get('base_column')}"
                          f"｜percent={bool(sp.get('percent'))}")

        vr = await validate(cand, existing=existing)
        trace.append({"round": i, "candidate": cand, "validation": vr})
        if verbose:
            for c in vr["checks"]:
                mark = "✅" if c.get("ok") else "❌"
                extra = ""
                if c.get("ok"):
                    extra = f" (ran {c.get('value')!r}"
                    if c.get("base") is not None:
                        extra += f", base={c.get('base')}"
                    if c.get("truth") is not None:
                        extra += f" → truth {c.get('truth')}"
                    extra += ")"
                print(f"    Validation {mark} {c.get('point')} — {c.get('why')}{extra}")
                if not c.get("ok") and c.get("fix"):
                    print(f"         Fix: {c['fix']}")
        if vr["ok"]:
            if verbose:
                print("  ✅ Validation passed: every rubric point's recipe produces a non-empty value from VLML")
            rec = adopt(cand, vr["checks"]) if do_adopt else cand
            if do_adopt and verbose:
                print(f"  Merged into the question bank → {STORE}")
            return rec, trace
        if verbose:
            print("  ❌ Validation failed: " + ";".join(vr["reasons"])[:300])
        critique = _critique_of(vr)
        # Hitting a wall twice in a row on the same direction → switch direction; do not burn all the tries on one dead end.
        # Observed: the about the question generator gave was "produce a question around kast_pct at player-level scope",
        # while kast_pct's server-side convention is **team-level** —— the model could only force a query on
        # match_players_report (a tool name, not a table), failing all three times on the same mistake.
        # Just saying "change it again" makes it rephrase and repeat; it must be explicitly told to switch direction.
        if i >= 2:
            critique += ("\n  - 【Switch direction】this angle has now failed "
                         f"{i} times in a row; it very likely does not hold in the current data at all "
                         "(e.g. some dimension only supports team/match level, with no player-level convention). "
                         "Please produce a new question with a dimension or subject key **the server has actually declared**, "
                         "do not rephrase on the original direction.")
    return None, trace


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Automatic question generation: produce data-retrieval recipes + deterministic validation")
    ap.add_argument("--about", default="", help="what to produce questions around")
    ap.add_argument("--difficulty", type=int, default=2)
    ap.add_argument("--tries", type=int, default=3, help="maximum generation attempts")
    ap.add_argument("--adopt", action="store_true", help="merge into the bank once validated")
    ap.add_argument("--revalidate", action="store_true",
                    help="retroactively re-check stored generated questions, marking as rejected those that cannot pass under the new validation rules")
    ap.add_argument("--loose", action="store_true",
                    help="use only the passing line (VLML runs once and produces a non-empty value), turning off the sanity backstop gates")
    args = ap.parse_args()

    if args.revalidate:
        if args.loose:
            global STRICT_MODE
            STRICT_MODE = False
        bad = asyncio.run(revalidate_store())
        if not bad:
            print("Existing-stock re-check: all passed (no rejected questions)")
            return 0
        print(f"Existing-stock re-check: {len(bad)} questions cannot pass under the new validation rules, marked invalid (not deleted, evidence kept)")
        for r in bad:
            print(f"  ✗ {r.get('topic_id')}: {str(r.get('invalid_reason'))[:200]}")
        return 1

    if args.loose:
        STRICT_MODE = False
        print("(--loose: only the passing line —— VLML runs once and produces a non-empty value to pass)")
    rec, _ = asyncio.run(generate_adopt(
        about=args.about, difficulty=args.difficulty,
        tries=args.tries, do_adopt=args.adopt))
    if rec is None:
        print("\nNo valid question was produced in these attempts (this is an honest result, not a bug: "
              "fields the model fabricates that do not exist in the data will be blocked by deterministic validation)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
