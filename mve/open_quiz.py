#!/usr/bin/env python3
"""MVE's open-ended question generator (ported from the study companion's
tutor_llm_agent_question_generate).

Question generation logic (ported; the companion runtime is not imported, the cross-process path
goes only through the HTTP bridge)
----------------------------------------------------------
1. Topic selection is `topic_graph.TopicGraph.select_next`'s job (ordered curriculum + completion
   gating + the give-up mechanism).
2. For the chosen topic (one of the 29 knowledge points from the companion's build_vlml_seed), take
   its `examples` (prompt = how it is asked / answer_outline = the steps to answer it from the
   tables), its `typical_misconceptions` (the pitfalls found in practice) and its `skills`, and
   assemble them into the question-generation prompt.
3. **Question-type rotation** (ported from question_type_mapping.select_question_style): while
   practising the same knowledge point repeatedly, rotate through the declared question_types and
   **explicitly tell the model "do not produce the same question as last time"** —— this is exactly
   what solves "it produced the same question again".
4. Go through the HTTP LLM: by default MVE's own Zhipu (llm_client); setting the env var
   `MVE_QUIZ_BASE_URL` switches to the study companion's OpenAI-compatible bridge in one move (the
   previously pinned "cross-process goes only through the HTTP bridge, never fork").

Output normalization (the shape of the ported _normalize_question)
-------------------------------------------
{question, answer, accepted_answers, key_points, solution_steps,
 question_type, math_equivalence_engine, difficulty, topic, screen_type}
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from topic_graph import MasteryStore, TopicGraph

HERE = Path(__file__).resolve().parent

# Fillable question style -> machine question type (ported from question_type_mapping, minimal
# usable version).
# The style keys below MUST match the knowledge graph's `question_types` values verbatim
# (e.g. static/knowledge_seeds/valorant.json, which uses English: "computation"/"interpretation"),
# otherwise every lookup misses and silently falls back to _DEFAULT_MACHINE.
_STYLE_TO_MACHINE = {
    "computation": "math_exact", "interpretation": "short_answer", "concept": "short_answer",
    "definition": "short_answer", "cross-table": "math_reasoning", "comparison": "short_answer",
}
_MACHINE_TYPES = ("short_answer", "math_exact", "math_reasoning")
_DEFAULT_MACHINE = "short_answer"


def _clamp_int(v: Any, lo: int = 1, hi: int = 5, default: int = 3) -> int:
    try:
        x = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


def _as_str(v: Any, fallback: str = "") -> str:
    return "" if v is None else str(v).strip()


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _machine_type_of(topic: dict[str, Any], attempt: int) -> tuple[str, str]:
    """Question-type rotation: the first attempt takes the first declared type, afterwards it rotates
    through the declared types; a retry avoids the previous question."""
    styles = [_as_str(s) for s in _as_list(topic.get("question_types")) if _as_str(s)]
    if not styles:
        return _DEFAULT_MACHINE, ""
    machine = _STYLE_TO_MACHINE.get(styles[attempt % len(styles)].lower(), _DEFAULT_MACHINE)
    return machine, styles[attempt % len(styles)]


# ---------------------------------------------------------------------------
# LLM calls (hackathon side branch: everything goes through the voyager_vlml.llm gateway)
# ---------------------------------------------------------------------------
# ⚠️ 2026-10-14 side-branch change: this used to carry its own Zhipu / study-companion bridge
#   endpoint logic. The hackathon rules require the submission to run on Nebius Token Factory +
#   NVIDIA open models, so the LLM calls for question generation and grading are delegated to
#   voyager_vlml/llm.py as well (Nebius first, keys in place → Nebius) —— this repository now has
#   exactly one LLM exit point, so "every call runs on Nebius" can be verified in one place.
def _quiz_http(messages: list[dict[str, str]], *, temperature: float = 0.6,
               max_tokens: int = 1600, timeout: float = 90.0) -> str:
    from voyager_vlml.llm import chat as _llm_chat

    return _llm_chat(messages, temperature=temperature,
                     max_tokens=max_tokens, timeout=timeout)


def _quiz_json(messages: list[dict[str, str]], **kw) -> dict:
    text = _quiz_http(messages, **kw)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        return json.loads(raw[s:e + 1])
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Real entities of the current database (anchors that follow the database; open-source questions
# must revolve around entities that really exist, otherwise they are unanswerable)
# ---------------------------------------------------------------------------
def _current_entities() -> dict[str, str]:
    try:
        import anchor
        return anchor.get()
    except Exception:
        return {"series": "", "team": "", "map": ""}


def _entity_block() -> str:
    e = _current_entities()
    s, t, m = e.get("series", ""), e.get("team", ""), e.get("map", "")
    if not (s and t):
        return ""
    return (
        f"\n[Real entities in the current database — question generation and the reference answer "
        f"must revolve around these; do not invent other teams/maps]\n"
        f"series={s} | team={t}" + (f" | representative map={m}" if m else "")
    )


# ---------------------------------------------------------------------------
# The real schema of the current database (the single source of truth for the reference SQL;
# it follows the database, so swapping the database updates it automatically)
# ---------------------------------------------------------------------------
def _active_db() -> Path:
    cfg = HERE / "db_config.json"   # db_config.json sits in the same directory as open_quiz.py (mve/)
    try:
        obj = json.loads(Path(cfg).read_text(encoding="utf-8"))
        p = str(obj.get("db_path") or "")
        if p:
            # ⚠️ A relative path is resolved against the **repository root** and must not be
            #    returned as-is (same convention as vlml_env / anchor / entity_catalog): vlml_env
            #    switches cwd to vlml/, so returning it verbatim makes the path get doubled up
            #    there (measured on the server, 2026-10-13).
            q = Path(p).expanduser()
            return q if q.is_absolute() else (HERE.parent / q).resolve()
    except Exception:
        pass
    # The fallback goes through the single point of definition (currently rib.gg, the user picked
    # it as the default database on 2026-10-13) —— no more vlml_events.duckdb literals scattered
    # around.
    from db_switch import DEFAULT_DB
    return DEFAULT_DB


_SCHEMA_CACHE: str | None = None


def _schema_block() -> str:
    """Every real table and column of the current database (queried live from the connection;
    following the study companion's entity_catalog principle of "query it live, never hardcode")."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    try:
        import duckdb
        con = duckdb.connect(str(_active_db()), read_only=True)
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY 1").fetchall()]
        lines = []
        for t in tables:
            try:
                cols = [r[0] for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name=? ORDER BY 1", [t]).fetchall()]
            except Exception:
                cols = []
            lines.append(f"- {t}: {', '.join(cols)}")
        con.close()
        block = "\n".join(lines)
    except Exception as e:
        block = f"(cannot read the database schema: {e})"
    _SCHEMA_CACHE = block
    return block


_SCHEMA_CAVEATS = (
    "[The real schema of the current database and its common pitfalls — reference_sql must follow "
    "these strictly]\n"
    "- There is no table named `teams`. Team names are scattered across: games(team1_name, "
    "team2_name, winning_team_name), rounds(winning_team_name, losing_team_name), "
    "series(team1_name, team2_name), and the agg_* tables use team_name / opponent_team_name / "
    "winning_team_name / losing_team_name.\n"
    "- The map column is always called `map_name` (not map).\n"
    "- `agent_roles` is a static dimension table with only agent_name / agent_role / "
    "is_controller / is_duelist / is_initiator / is_sentinel and no team / map / player columns —— "
    "it can only answer \"what role does a given agent have\", not filter by team/map/player.\n"
    "- To ask \"which agents in a given role (Initiator/Sentinel/Duelist/Controller) did a given "
    "team pick on a given map\", use **agg_player_round_stats** (it has team_name, map_name, "
    "agent_name, agent_role). Note that agg_player_game_stats has only agent_name and "
    "**no agent_role column** — do not pick the wrong table.\n"
    "- Player names: agg_player_* and ext_player_* use player_name; base_events uses "
    "actor_player_name / target_player_name (there is no player_name column).\n"
    "- The team columns of base_events are **actor_team_name / target_team_name** (not team_name).\n"
    "- In a multi-table JOIN, every **duplicate column name must be prefixed with its table** "
    "(e.g. base_events.weapon_name), otherwise it reports an Ambiguous reference.\n"
    "- `agg_team_round_stats` has **no series_id column** (only game_id / round_id); to filter by "
    "series, first `JOIN games ON game_id` to get series_id, or use agg_team_game_stats instead "
    "(it has series_id).\n"
    "- The team columns of `agg_post_plant_stats` are planting_team / defending_team / "
    "winning_team_name (there is no team_name).\n"
    "- Do not mix up the types: series_id is an integer (e.g. 1058) while tournament_id is a string "
    "(e.g. 'valorant-champions-2026'). Filtering \"by series\" uses series_id; do not query "
    "tournament_id with 1058.\n"
    "- reference_sql must be a single SELECT, and the table and column names must come from the "
    "[real schema] below; questions whose SQL does not run are eliminated outright, so always "
    "check the column names before writing it.\n"
)


def _validate_sql(sql: str) -> tuple[bool, str]:
    """Run the SELECT once against the current database and return (does it execute and return a
    non-empty row set?, the error message). This is the hard gate on a generated question's
    reference SQL: if it does not run, the question has no objective ground truth (VLML cannot
    judge it a success)."""
    s = str(sql or "").strip()
    if not s.lower().lstrip().startswith("select"):
        return False, "reference_sql does not start with SELECT"
    try:
        import duckdb
        con = duckdb.connect(str(_active_db()), read_only=True)
        try:
            rows = con.execute(s).fetchall()
        finally:
            con.close()
        if not rows:
            return False, "reference_sql returned 0 rows (this question has no answer in the current database)"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
def recall_context(task_text: str, limit: int = 5) -> list[str]:
    """Task 3: recall experience relevant to this question from mem0 long-term memory (with the
    memory system consumed).

    Graceful degradation: mem0 not installed / not seeded / unreachable → return an empty list and
    do not block question generation. The memory content is 4 cross-domain planning principles plus
    failure-correction experience (see mem0_backend).
    """
    try:
        import mem0_backend
        return mem0_backend.recall(task_text, limit=limit)
    except Exception:
        return []


def _build_messages(topic: dict[str, Any], *, attempt: int, last_question: str = "",
                    memories: list[str] | None = None,
                    scene_text: str = "", sql_error: str = "") -> list[dict[str, str]]:
    name = _as_str(topic.get("name"), topic.get("id", ""))
    chapter = _as_str(topic.get("chapter"))
    skills = _as_list(topic.get("skills"))
    examples = _as_list(topic.get("examples"))
    mis = _as_list(topic.get("typical_misconceptions"))

    ex_block = ""
    if examples:
        lines = []
        for ex in examples[:2]:
            if isinstance(ex, dict):
                p = _as_str(ex.get("prompt"))
                ao = _as_list(ex.get("answer_outline"))
                if p:
                    lines.append(f"- Example of how it is asked: '{p}'")
                if ao:
                    lines.append(f"  How to answer it from the tables: "
                                 f"{' → '.join(_as_str(x) for x in ao[:3])}")
        ex_block = "\n".join(lines)

    mis_block = "\n".join(f"- {_as_str(x)}" for x in mis[:4]) if mis else "(nothing recorded)"
    skill_block = "\n".join(f"- {_as_str(s)}" for s in skills) if skills else "(none)"

    machine_type, style = _machine_type_of(topic, attempt)
    # The style name comes from the knowledge graph's own `question_types` value and is therefore
    # deliberately shown verbatim, so the model can see which declared type this attempt rotates to.
    style_hint = (f"question type style for this attempt: {style} (machine question type "
                  f"{machine_type})") if style else f"machine question type: {machine_type}"

    # Convention lock: prevents the generator from bleeding across granularities (measured:
    # grain_game was generated as a round-counting question ("how many rounds") —— because the
    # scene/entity context carried in the wording and entities of the previous grain_round question,
    # and the model kept the previous question's counting granularity). The granularity marker is
    # taken from the parentheses in the knowledge point name (rounds/games/series/first_blood/
    # economy…), and the question is forced to lock onto this granularity.
    gran = ""
    _gm = re.search(r"[（(]([^（）()]+)[）)]", name)
    if _gm:
        gran = _gm.group(1).strip()
    # The granularity marker is often itself a table name (rounds / games / series / base_events /
    # agg_tournament_stats…), so naming it directly as the data-retrieval anchor is stricter than
    # merely saying "lock the granularity": the model is insensitive to an abstract "granularity"
    # but is sensitive to concrete constraints like "you may not FROM this table" (measured: when
    # only the granularity lock was mentioned, grain_series was still generated as a round question
    # doing FROM rounds).
    gran_hint = ""
    if gran:
        gran_hint = (
            f"\n  The granularity marker of this question is `{gran}`: reference_sql must retrieve "
            f"data around `{gran}` (if that granularity corresponds to a reporting tool, call it "
            f"with an entry parameter such as series_id); you are **strictly forbidden** to count "
            f"with tables whose granularity differs from `{gran}` —— for example if the granularity "
            f"is not rounds, do not write FROM rounds, and if it is not games, do not write FROM "
            f"games."
        )
    gran_lock = (
        f"\n[Convention lock] The knowledge point of this question is '{name}'"
        + (f", and its granularity marker is {gran}" if gran else "")
        + ". The **counting / analysis target of the question you produce must lock strictly onto "
        "this knowledge point's granularity (follow the conventions in the [example of how it is "
        "asked] above, e.g. counting maps / games / series / rounds / first blood / economy — do "
        "not mix them). It is strictly forbidden to graft another granularity's phrasing onto this "
        "question (e.g. when this granularity is 'map/game' ask 'how many rounds', or when it is "
        "'round' ask 'how many maps'), and do not copy the previous question's wording."
        + gran_hint
    )

    repeat_guard = ""
    if last_question:
        repeat_guard = (
            f"\n⚠️ The question practised last time was: '{last_question}'\n"
            "You must **change the angle / change the map / change the entity / switch the counting "
            "granularity**, and must not duplicate last time (in particular, do not keep counting "
            "what the previous question counted)."
        )

    system = (
        "You are the practice coach for Valorant esports data analysis. Given a [knowledge point], "
        "produce **one** practice question that the student answers using VLML data (tables agg_* / "
        "rounds / base_events and the like). You must return a JSON object with these fields:\n"
        "  question: the question (natural language, stating the convention clearly, e.g. 'which "
        "map' / 'which team')\n"
        "  answer: the reference answer (concise)\n"
        "  accepted_answers: a list of acceptable answers (including equivalent phrasings / key "
        "numbers)\n"
        "  key_points: a list of scoring points (used for grading)\n"
        "  solution_steps: a list of solution steps (confirm the granularity first → pick the table "
        "→ write SQL / call a tool → report the number)\n"
        "  question_type: the machine question type (short_answer / math_exact / math_reasoning)\n"
        "  difficulty: an integer 1-5\n"
        "  reference_sql: a single SELECT statement returning this question's **target value** (it "
        "must run in the current database, start with SELECT, and may only query tables and columns "
        "that really exist). It exists so the 'VLML0 referee' can independently compute the gold "
        "answer —— the number is computed by the database and is never one you made up; you are only "
        "responsible for writing a query that can retrieve it\n"
        "  dimension: a short identifier for the metric dimension (in English, e.g. event_count / "
        "win_rate / kill_count)\n"
        "  subject: a dictionary of entities, e.g. {\"series\":\"2843069\",\"team\":\"NRG\","
        "\"map\":\"Lotus\"} (take it from the real entities of the current database, do not invent "
        "any; the keys may only be series/team/map)\n"
        "  value_column: which column of the result holds the target value (0-based, default 0)\n"
        "  numeric_tolerance: the numeric tolerance (default 0.5)\n"
        "Do not invent tables or columns that do not exist; the convention must be verifiable; "
        "reference_sql must actually run."
    )
    system += (
        "\n\n[The real schema — the table/column names for reference_sql may only be chosen from "
        f"here]\n{_schema_block()}\n\n"
        f"{_SCHEMA_CAVEATS}"
    )
    scene_line = ""
    if scene_text:
        scene_line = f"\n{scene_text}\n"
    sql_err_line = ""
    if sql_error:
        sql_err_line = (
            f"\n\n⚠️ The reference_sql generated in the previous round raised an error when executed "
            f"against the current database. **Please fix it and produce a new question** (you may "
            f"change the phrasing), making sure the new SQL runs in the current database:\n{sql_error}\n"
        )
    user = (
        f"[Knowledge point] {name} (chapter: {chapter})\n"
        f"{style_hint}\n"
        f"\n[Skill requirements]\n{skill_block}\n"
        f"\n[Phrasing and how to answer it from the tables (a reference, do not copy the original "
        f"question)]\n{ex_block}\n"
        f"\n[Common pitfalls found in practice (bake a trap into the question, or warn explicitly)]\n"
        f"{mis_block}\n"
        f"\nSuggested difficulty: {topic.get('difficulty')}"
        f"{scene_line}"
        f"{gran_lock}"
        f"{_entity_block()}"
        f"{repeat_guard}"
        f"{sql_err_line}"
        + (_memory_block(memories) if memories else "")
        + "\nPlease return JSON."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Normalization (the shape of the ported _normalize_question)
# ---------------------------------------------------------------------------
def _normalize(raw: dict[str, Any], topic: dict[str, Any], *, attempt: int) -> dict[str, Any]:
    question = _as_str(raw.get("question")) or _as_str(raw.get("prompt"))
    if not question:
        raise ValueError("the model returned no question")
    machine_type, style = _machine_type_of(topic, attempt)
    answer = _as_str(raw.get("answer")) or _as_str(raw.get("reference_answer"))
    accepted = [_as_str(x) for x in _as_list(raw.get("accepted_answers")) if _as_str(x)]
    if answer and not accepted:
        accepted = [answer]
    key_points = [_as_str(x) for x in _as_list(raw.get("key_points")) if _as_str(x)]
    steps = [_as_str(x) for x in _as_list(raw.get("solution_steps")) if _as_str(x)]
    mis = _as_list(topic.get("typical_misconceptions"))
    hint = "；".join(_as_str(x) for x in mis[:3]) if mis else ""
    reference_sql = _as_str(raw.get("reference_sql"))
    dimension = _as_str(raw.get("dimension"))
    subject = raw.get("subject") if isinstance(raw.get("subject"), dict) else {}
    value_column = _clamp_int(raw.get("value_column"), 0, 20, 0)
    try:
        numeric_tolerance = float(_as_str(raw.get("numeric_tolerance")) or 0.5)
    except (TypeError, ValueError):
        numeric_tolerance = 0.5
    return {
        "question": question,
        "answer": answer,
        "accepted_answers": accepted,
        "key_points": key_points,
        "solution_steps": steps,
        "hint": hint,
        "question_type": _as_str(raw.get("question_type")) or machine_type,
        "math_equivalence_engine": {"enabled": False},
        "difficulty": _clamp_int(raw.get("difficulty"), default=_clamp_int(topic.get("difficulty", 3) * 5, default=3)),
        "topic": topic.get("id"),
        "topic_name": _as_str(topic.get("name"), topic.get("id", "")),
        "screen_type": "open_quiz",
        "reference_sql": reference_sql,
        "dimension": dimension,
        "subject": subject,
        "value_column": value_column,
        "numeric_tolerance": numeric_tolerance,
    }


def _memory_block(memories: list[str] | None) -> str:
    if not memories:
        return ""
    bullets = "\n".join(f"  - {m}" for m in memories[:6])
    return (
        f"\n\n[Experience recalled from past memory (mem0 recall; you may refer to its data-retrieval "
        f"ideas and pitfalls when generating the question)]\n"
        f"{bullets}"
    )


def generate_for(topic_id: str, *, attempt: int = 0, last_question: str = "",
                 memories: bool = True, scene: bool = True,
                 sql_error: str = "") -> dict[str, Any]:
    """Generate one open-ended question for a knowledge point. Raises on failure; the caller
    degrades.

    With memories=True, past experience is first recalled from mem0 by the knowledge point's text
    and injected into the question-generation prompt (Task 3: skill consumption with a memory
    system —— the human consumption path also gets memory).
    With scene=True, the "initial scene (data world)" block is injected (Task 2: progressive
    disclosure —— the model only sees the tables/tools the current progress tier allows).
    sql_error: fed back when the previous round's reference SQL raised an error, so the model
    retries carrying its mistake (see generate_validated).
    """
    graph = TopicGraph()
    topic = graph.topics.get(topic_id)
    if topic is None:
        raise ValueError(f"unknown knowledge point {topic_id}")
    mem = recall_context(_memory_query(topic)) if memories else []
    scene_text = ""
    if scene:
        try:
            import scene_context
            scene_text = scene_context.scene_injection(topic_id)
        except Exception:
            scene_text = ""
    messages = _build_messages(topic, attempt=attempt, last_question=last_question,
                               memories=mem, scene_text=scene_text, sql_error=sql_error)
    raw = _quiz_json(messages)
    if not raw:
        raise ValueError("the question-generation LLM returned empty / non-JSON output")
    q = _normalize(raw, topic, attempt=attempt)
    q["memories_recalled"] = len(mem)
    return q


def _real_tables() -> set[str]:
    """Parse the real table names of the current database out of _schema_block (reuses the cache, does
    not reconnect to the database)."""
    tabs: set[str] = set()
    for line in _schema_block().splitlines():
        line = line.strip()
        if line.startswith("- ") and ":" in line:
            tabs.add(line[2:line.index(":")].strip())
    return tabs


def _gran_table(topic: dict[str, Any]) -> str:
    """The **data-retrieval table** corresponding to this question's granularity; returns it only
    when the granularity marker really is a table name, otherwise an empty string.

    Data-driven, never hardcoded: the granularity marker comes from the parentheses in the knowledge
    point name (rounds / games / series / base_events / agg_tournament_stats…), and whether it is
    genuinely a table is decided by the current database's schema.
    """
    name = _as_str(topic.get("name"), topic.get("id", ""))
    m = re.search(r"[（(]([^（）()]+)[）)]", name)
    if not m:
        return ""
    gran = m.group(1).strip()
    return gran if gran in _real_tables() else ""


def _sql_tables(sql: str) -> set[str]:
    """Extract the FROM / JOIN main table names of reference_sql, for the granularity gate's
    comparison."""
    out: set[str] = set()
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)",
                         str(sql or ""), re.I):
        out.add(m.group(1).strip('"'))
    return out


def generate_validated(topic_id: str, *, max_tries: int = 3, attempt: int = 0,
                       last_question: str = "", memories: bool = True,
                       scene: bool = True) -> tuple[dict[str, Any], str | None]:
    """Question generation + a self-check on the reference SQL: right after generating a question,
    validate its reference_sql against the real database, and if it does not run, feed the
    **specific SQL error** back to the model for a retry (at most max_tries times).

    Returns (q, error): error is None when reference_sql is already executable; otherwise it is the
    last error (the caller should drop the question and pick another). This way the objective
    referee can always obtain a ground truth, so there is no "the question was right but the SQL did
    not run → the referee cannot judge → forever 0% and never learns" deadlock.
    """
    sql_error = ""
    last_q: dict[str, Any] = {}
    # Granularity gate: if this question's granularity marker is a real table, reference_sql must
    # land on it
    try:
        topic = TopicGraph().topics.get(topic_id) or {}
    except Exception:
        topic = {}
    gran_tbl = _gran_table(topic)
    for i in range(max_tries):
        try:
            q = generate_for(topic_id, attempt=attempt, last_question=last_question,
                             memories=memories, scene=scene, sql_error=sql_error)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if i == max_tries - 1:
                return {}, err
            sql_error = f"the previous round's question generation itself failed: {err}"
            continue
        last_q = q
        sql = str(q.get("reference_sql") or "").strip()
        if not sql:
            # Explicitly no SQL: leave it to the caller to handle as "no reference SQL", no retry here
            return q, "no reference_sql"
        ok, err = _validate_sql(sql)
        if ok:
            # —— Granularity gate: the SQL running is not enough, the data-retrieval layer must land
            # on this question's granularity ——
            # measured: grain_series's SQL executed but queried rounds (round granularity), so the
            # question text and the knowledge point were mismatched → the referee graded by the
            # series convention → coverage=0% for certain. The prompt layer cannot hold this (the
            # model is insensitive to "lock the granularity"), so it must be caught at runtime and
            # retried.
            if gran_tbl:
                used = _sql_tables(sql)
                if gran_tbl not in used:
                    sql_error = (
                        f"The previous reference_sql ran fine, but the **data-retrieval granularity "
                        f"did not match this question's knowledge point** (the granularity table "
                        f"for this question should be `{gran_tbl}`, but you actually queried "
                        f"{sorted(used) or 'unknown'}). Please rewrite it: both question and "
                        f"reference_sql must count at the `{gran_tbl}` level, and must not borrow "
                        f"tables from another granularity.\n"
                        f"The wrong SQL: {sql}"
                    )
                    print(f"  (question self-check {topic_id} attempt {i+1}: granularity mismatch — "
                          f"expected {gran_tbl}, got {sorted(used)} → retry)")
                    continue
            return q, None
        sql_error = (
            f"The reference_sql generated last time raised an error when executed; please fix it "
            f"and generate a new one:\n{sql}\n"
            f"The error: {err}"
        )
        print(f"  (question self-check {topic_id} attempt {i+1}: reference SQL does not run: "
              f"{err} → retry)")
    return last_q, sql_error or "the reference SQL is still not executable after several attempts"


def _memory_query(topic: dict[str, Any]) -> str:
    """Assemble the task text used for semantic recall (knowledge point name + skill requirements)."""
    name = _as_str(topic.get("name"), topic.get("id", ""))
    skills = "；".join(_as_str(s) for s in _as_list(topic.get("skills"))[:4])
    return f"{name}｜{skills}" if skills else name


def generate_next(*, scope: dict[str, Any] | None = None, explicit: str | None = None,
                  last_question: str = "", memories: bool = True,
                  scene: bool = True) -> dict[str, Any]:
    """Pick the next knowledge point and generate a question for it, returning the selection
    rationale alongside."""
    graph = TopicGraph()
    sel = graph.select_next(scope=scope, explicit=explicit)
    # No "can be taken" task left within the scope (by design the caller should generate new tasks)
    # → return the selection rationale directly without calling generate_for (otherwise tid=""
    # would raise).
    if sel.get("exhausted"):
        return {"selection": sel, "exhausted": True,
                "question": "", "reference_sql": "", "topic": ""}
    tid = sel["topic_id"]
    # attempt count = how many times this knowledge point has been tried (used for question-type
    # rotation)
    mrow = graph.mastery.get(tid)
    attempt = int((mrow or {}).get("attempts", 0))
    q = generate_for(tid, attempt=attempt, last_question=last_question,
                     memories=memories, scene=scene)
    q["selection"] = sel
    return q


def record_correction_on_failure(topic_id: str, *, user_answer: str = "",
                                 grade: dict[str, Any] | None = None) -> int:
    """Task 3: the failure → correction loop of the human consumption path. When the answer is
    wrong (score<0.6), write this question's "common pitfalls + missed points" as a mem0 correction
    memory, which later question generation / answering can recall.

    Graceful degradation: mem0 not installed / not seeded → return 0 and do not block the main flow.
    The difference from failure_correction_loop: that one records "the SQL exception the model wrote
    by itself" (the evidence-retrieval loop), while this one records "the knowledge point pitfall
    the human's wrong answer exposed" (the human consumption loop).
    """
    score = float((grade or {}).get("score", 1.0))
    if score >= 0.6:
        return 0
    try:
        import mem0_backend
    except Exception:
        return 0
    graph = TopicGraph()
    topic = graph.topics.get(topic_id) or {}
    mis = _as_list(topic.get("typical_misconceptions"))[:3]
    misses = _as_list((grade or {}).get("misses"))[:4]
    if not (mis or misses):
        return 0
    entries = [{
        "error": f"the human answered knowledge point {topic_id} wrong (score {score:.2f}): "
                 + (_as_str(user_answer)[:60] or "no answer given"),
        "fix": "the key to this question: " + "; ".join([*mis, *misses]),
        "condition": f"involves question generation / answering for {topic_id}",
        "verified": True,
    }]
    try:
        added = mem0_backend.add_correction(entries, force=False)
        return len(added)
    except Exception:
        return 0


def record_result(topic_id: str, *, score: float) -> dict[str, Any]:
    """Record one answering score (0-1) and update mastery."""
    graph = TopicGraph()
    return graph.mastery.update(topic_id, score=score)


# ---------------------------------------------------------------------------
# Grading (semantic grading over accepted_answers + key_points, porting the study companion's
# accepted_answers idea)
# ---------------------------------------------------------------------------
def grade_answer(question_payload: dict[str, Any], user_answer: str) -> dict[str, Any]:
    """Grade with the LLM: returns {score(0-1), feedback, hits, misses}.

    For math_exact questions whose accepted_answers contain numbers, a numeric-tolerance fast path
    runs first (a strong signal that does not pollute the failure → correction loop); everything
    else goes to semantic grading.
    """
    q_text = _as_str(question_payload.get("question"))
    accepted = _as_list(question_payload.get("accepted_answers"))
    key_points = _as_list(question_payload.get("key_points"))
    ua = _as_str(user_answer)
    if not ua:
        return {"score": 0.0, "feedback": "no answer given", "hits": [], "misses": key_points}

    # Numeric fast path (math_exact)
    if str(question_payload.get("question_type")) == "math_exact":
        nums = [n for a in accepted for n in _extract_numbers(a)]
        unum = _extract_numbers(ua)
        if nums and unum:
            if any(abs(n - u) <= max(0.5, abs(n) * 0.05) for n in nums for u in unum):
                return {"score": 1.0, "feedback": "the number is correct",
                        "hits": ["number"], "misses": []}

    messages = [
        {"role": "system", "content": (
            "You are a strict grader. Judge [the student's answer] against [the question] "
            "[the reference answer / accepted answers] [the scoring points]. Return JSON: "
            "{score: a decimal from 0 to 1, feedback: a brief comment, "
            "hits: the points that were hit, misses: the points that were missed or wrong}."
        )},
        {"role": "user", "content": (
            f"[Question] {q_text}\n"
            f"[Accepted answers] {json.dumps(accepted, ensure_ascii=False)}\n"
            f"[Scoring points] {json.dumps(key_points, ensure_ascii=False)}\n"
            f"[The student's answer] {ua}\n"
            "Please return JSON."
        )},
    ]
    raw = _quiz_json(messages, temperature=0.2)
    try:
        score = float(raw.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    return {
        "score": round(score, 3),
        "feedback": _as_str(raw.get("feedback")) or ("pass" if score >= 0.6 else "fail"),
        "hits": _as_list(raw.get("hits")),
        "misses": _as_list(raw.get("misses")),
    }


def _extract_numbers(text: str) -> list[float]:
    out = []
    for m in re.findall(r"-?\d+(?:\.\d+)?", text):
        try:
            out.append(float(m))
        except ValueError:
            pass
    return out


def attempt(topic_id: str, user_answer: str, *, attempt: int = 0,
            last_question: str = "") -> dict[str, Any]:
    """Pick / generate a question → grade it → record mastery, all in one step. Returns the full
    result."""
    q = generate_for(topic_id, attempt=attempt, last_question=last_question)
    g = grade_answer(q, user_answer)
    q["grade"] = g
    if topic_id:
        record_result(topic_id, score=g["score"])
    return q


if __name__ == "__main__":
    import sys
    tid = sys.argv[1] if len(sys.argv) > 1 else TopicGraph().select_next()["topic_id"]
    try:
        q = generate_for(tid, attempt=0)
        print(json.dumps(q, ensure_ascii=False, indent=2))
    except Exception as e:
        print("Question generation failed: ", e)
