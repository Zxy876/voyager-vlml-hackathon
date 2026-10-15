#!/usr/bin/env python3
"""Human-imported question → answered and explained by VLML (the coach channel).

This channel **does not go through Voyager**:

    human-imported question → original VLML orchestration retrieves data → LLM explains
                                                 → memory (enters the timeline)
                                                 → feeds back into the question generator

Following the study companion's two hard rules:

1. The supplier of the "explanation output" is VLML plus its own system LLM, **not something run
   by Voyager** (the midwife's third-round checklist: the explanation output is a replacement part).

2. A human-imported question has `validated_target = False`.
   Following the decision chain in `target_binding.py:22-31` —— only when all of the following hold
     question_source == "current_question"
     source == "targeted_question"
     target_binding.validation_status == "passed"
     generated_at is non-empty
     bound_topic_id == the currently selected question
   is it True (i.e. the question generator produced it). A human-imported question satisfies none
   of these → False.

   The consequence of False (`practice_outcome.py:58-65`):
     **a verdict can be graded, but mastery_status is forced to insufficient_evidence**
   → "can be graded" and "can be assessed for mastery" are cut apart by a single boolean.

3. Not counting towards mastery ≠ nothing happened: it writes an action into the causal timeline
   (control fact), and the question generator reads that line to decide the next question.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import vlml_env  # noqa: F401  bootstrap the environment first

import causal_timeline  # noqa: E402
from llm_client import chat_json  # noqa: E402
from loop_core import make_fact  # noqa: E402
import narrator  # noqa: E402
from voyager import LOCAL_SQL_NOTES, TOOL_CATALOG, TOOL_REGISTRY, VLML_USAGE_TIPS  # noqa: E402
from tasks import TASKS, infer_topic  # noqa: E402  (tasks does not pull in VLML, so importing it is safe)

# A human-imported question is always validated_target=False —— this comes from the study
# companion's decision chain, it is not a configurable option
VALIDATED_TARGET_IMPORTED = False

# The dataset context held server-side. Without it the model passes a map name as series_id
# (measured: asked "how did it collapse on Corrode" → it passed series_id='Corrode' straight away).
DATASET_CONTEXT = """
There is only one series in this database; use it for any question:
  series_id = '2843069' (Cloud9 vs NRG)
  team_name is either 'Cloud9' or 'NRG'
  map_name is one of 'Haven', 'Corrode', 'Lotus'
A map name is map_name, **not** series_id.

The final result of this series is **Cloud9 lost and NRG won**.
A pitfall hit in practice: without stating which side won, the model reads "lost 5 rounds in a row"
as "kept winning five rounds straight" —— the number is right and the direction is reversed, which
is more dangerous than an error. So before concluding anything about a winning or losing streak,
confirm who the subject is and who won.
"""

PLAN_SCHEMA = """Output a single JSON object:
{"thought": "one sentence saying how you plan to query",
 "calls": [{"tool": "tool name", "args": {"param_name": "param_value"}}]}
At most 3 calls."""

# Human-imported history, persisted on disk; the dashboard reads it to show "what I imported and
# how VLML answered"
COACH_LOG = Path(__file__).resolve().parent / "coach_log.jsonl"


def recent_imports(limit: int = 10) -> list[dict[str, Any]]:
    """The most recent human imports (for the dashboard; does not import vlml_env, just reads the
    file)."""
    if not COACH_LOG.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in COACH_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-limit:]


def _log_import(result: dict[str, Any]) -> None:
    """Persist one human-import record. The full facts are not stored (the dashboard only reads the
    summary plus the explanation)."""
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "question": result.get("question", ""),
        "mode": result.get("mode", "vlml"),
        "voyager": (result.get("voyager_context") or {}).get("memory_count", 0),
        "topic_id": result.get("topic_id", ""),
        "topic_matched": result.get("topic_matched", False),
        "topic_hit": result.get("topic_hit", ""),
        # what landed on the graph side (edges created / cover level / number of insights hit) ——
        # "was it recorded or not" has to be reviewable
        "graph_link": result.get("graph_link") or {},
        "validated_target": result.get("validated_target", False),
        "trajectory": result.get("trajectory") or [],
        "facts_count": len(result.get("facts") or []),
        "narrative": result.get("narrative", ""),
        "insights": result.get("insights") or [],
        "caveats": result.get("caveats") or [],
        "comparable": result.get("comparable", False),
        "skipped": result.get("skipped", ""),
        "errors": (result.get("errors") or [])[:2],
        # —— Fields newly added by the consumption path (`voyager_vlml/consume.py`) ——
        # Older records (pure VLML explanations) have none of these → the dashboard must tolerate
        # their absence (show "—" when it cannot read them) and must not wipe the whole history
        # just because a field is missing.
        "answer": result.get("answer", ""),
        "reused": bool(result.get("reused")),
        "used_skills": result.get("used_skills") or [],
        "ok_tool_calls": int(result.get("ok_tool_calls") or 0),
        "rows": int(result.get("rows") or 0),
        # Auditing has to be able to look back at "how it answered": the data-retrieval SQL and the
        # derived rubric points must be persisted. Hit in practice: when the facts came out empty
        # there was nowhere to look up "what the rubric points look like".
        "sql": result.get("sql", ""),
        "derived_points": result.get("derived_points") or [],
        "referee_trajectory": result.get("referee_trajectory") or [],
        # Coverage / mastery are computed **only on the consumption side** (practice is not
        # graded) —— these are quality numbers for human eyes and must never be fed back into
        # Voyager's feedback.
        "coverage": result.get("coverage"),
        "score": result.get("score"),
        "verdict": result.get("verdict", ""),
        # Mastery = growth of the capability set: answered → this question joins the capability
        # library → mastered. mastered_total is the **cumulative** number mastered, and it is what
        # the progress curve plots.
        "mastered": bool(result.get("mastered")),
        "mastered_total": result.get("mastered_total"),
        "ability_total": result.get("ability_total"),
        "skill_action": result.get("skill_action", ""),
        "mastery": result.get("mastery"),
        # —— Method-vs-approach comparison (the consumption side's main ruler, the judging method
        # fixed on 2026-10-13) ——
        # Grading no longer relies on "each side tidies its facts and compares them by key"; it
        # directly compares the two SQL statements and the two approaches. These fields are the
        # conclusion of that comparison, and the dashboard must present them verbatim (a human has
        # to see "on what basis this score was given").
        "judge": result.get("judge", ""),
        "method_reason": result.get("method_reason", ""),
        "method_diff": result.get("method_diff", ""),
        "same_metric": bool(result.get("same_metric")),
        "voyager_sound": bool(result.get("voyager_sound")),
        "judged": bool(result.get("judged")),
        # What the referee (VLML) independently retrieved —— disclosed alongside Voyager's path
        "referee_sql": result.get("referee_sql", ""),
        "referee_rows": int(result.get("referee_rows") or 0),
        # The hard ruler (only runs when this utterance routed onto a question that has a rubric),
        # alongside the main ruler
        "coverage_referee": result.get("coverage_referee"),
        "verdict_referee": result.get("verdict_referee", ""),
        "covered_points": result.get("covered_points") or [],
        "missing_points": result.get("missing_points") or [],
    }
    with COACH_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _plan_prompt(question: str) -> str:
    return f"""You are the original VLML analysis engine. Someone has asked you a question; please
retrieve the data needed to answer it.

Question:
{question}

{DATASET_CONTEXT}

{TOOL_CATALOG}

VLML's own usage tips:
{VLML_USAGE_TIPS}

{LOCAL_SQL_NOTES}

{PLAN_SCHEMA}"""


def _extract_prompt(question: str, obs: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        [{"tool": o.get("tool"), "args": o.get("args"),
          "result": o.get("result"), "error": o.get("error")} for o in obs],
        ensure_ascii=False,
        default=str,   # VLML's returns carry datetime, which fails to serialise unhandled
    )[:14000]
    return f"""The real data the tools returned:

{payload}

Question:
{question}

Please organise the data into an array of facts, each fact:
{{"subject": {{...}}, "dimension": "...", "value": a number,
  "unit": "percent|count|ratio|raw", "base": the denominator}}

subject must at least identify who it is about:
  - if a team is involved include "team"; if a map is involved include "map" (use the real map
    name, never a number)
dimension must make the direction visible: write something like max_losing_streak (the longest
losing streak), not a name like round_streak that cannot tell a loss from a win —— a reversed
direction is more dangerous than an error.

Output only JSON: {{"facts":[...]}}
Never invent numbers that are not in the data."""


async def _execute(calls: list[dict[str, Any]],
                   limit: int = 8) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    # limit must not be hardcoded to 3 (the same pitfall as voyager._execute): when there are more
    # than 3 rubric points, calls after the 4th are silently dropped and the explanation loses a
    # piece.
    obs: list[dict[str, Any]] = []
    traj: list[str] = []
    errors: list[str] = []
    for call in (calls or [])[:limit]:
        tool = str(call.get("tool", ""))
        args = dict(call.get("args") or {})
        fn = TOOL_REGISTRY.get(tool)
        if fn is None:
            errors.append(f"unknown tool {tool}")
            continue
        try:
            if fn is vlml_env.execute_custom_sql:
                sql = str(args.pop("sql_query", "") or args.pop("sql", ""))
                if not sql.lower().lstrip().startswith("select"):
                    errors.append(f"{tool}: only SELECT is allowed")
                    continue
                res = await fn(sql)
                args = {"sql_query": sql}
            else:
                res = await fn(**args)
            if isinstance(res, dict) and res.get("error"):
                errors.append(f"{tool}: {res['error']}"[:200])
                obs.append({"tool": tool, "error": res["error"]})
                continue
            obs.append({"tool": tool, "args": args, "result": res})
            traj.append(tool)
        except Exception as e:
            errors.append(f"{tool}: {type(e).__name__}: {e}"[:200])
    return obs, traj, errors


async def _task_from_text(question: str,
                          facts: list[dict[str, Any]] | None = None
                          ) -> tuple[str, str]:
    """When it cannot be routed onto an existing question, use this text to generate a new one.
    Returns (topic_id, explanation).

    On failure it honestly returns empty —— the question-generator line is broken, but **the
    explanation itself is still returned as usual**, and the content the human sees is unaffected.

    `facts` is the answer VLML just produced. It is the key to this chain:

      the human's question does not route → but VLML can answer it (dimensions, values, the SQL
      used)
      → judge "does the toolset cover this" → if it is covered, **record the question on an edge**
        of the graph
      → the question generator picks directions from those edges → the human's intent enters
        adaptive question generation

    Without facts, the question-generation point can only be picked by `_graph_blueprint()` from
    **existing** dimension nodes, and nearly every existing dimension has already had a question
    generated for it (all of the graph's dimension nodes derive from TASKS[].rubric), so what comes
    out is still an old direction —— the human's imported intent gets eaten.
    Measured: when importing "spike success rate" VLML answered with `plant_success_rate`, but the
    topic_id was still empty and the question generator did not react at all.

    ⚠️ The recording writes an **edge, not a node** (`knowledge_graph.link_import`): the graph is
    fully covered at the "table × toolset (insight)" layer, so a human question qualifies for
    recording as long as VLML can build it with that toolset; there is no need to fabricate a
    dimension node just to hold a place.
    """
    # 1) First record the question the human asked onto an **edge** of the graph (no node created)
    dims: list[str] = []
    for f in (facts or []):
        d = str((f or {}).get("dimension") or "").strip()
        if d and d not in dims:
            dims.append(d)
    tool = ""
    if facts:
        tool = str((facts[0] or {}).get("tool") or "")
    focus = ""
    covered = False
    for d in dims:
        try:
            import knowledge_graph
            rec = knowledge_graph.link_import(
                question=question,
                dimension=d,
                sql=str((facts[0] or {}).get("sql") or ""),
                tool=tool,
                subject=dict((facts[0] or {}).get("subject") or {}),
                value=(facts[0] or {}).get("value"),
            )
            if not rec.get("adopted"):
                # Say so honestly when it is not covered —— same as the study companion: if it
                # does not map, do not force it, hand it back to the human
                print(f"  [import] not recorded: {str(rec.get('reason') or '')[:160]}")
                continue
            covered = True
            focus = focus or d
            print(f"  [import] graph gained a link edge (no node created): {d}"
                  f" | cover level={rec.get('cover_level')}"
                  f" | {len(rec.get('insights') or [])} insights"
                  f" | {len(rec.get('tables') or [])} tables")
        except Exception as exc:                             # pragma: no cover
            print(f"  [import] failed to write into the graph: {type(exc).__name__}: {str(exc)[:120]}")

    # 2) Generate a question around this direction (the question's point belongs server-side; the
    #    model only writes the question text)
    if not covered:
        # Generating a question anyway when it is not covered would just make the question
        # generator pick some old dimension to pad the numbers —— better to generate nothing,
        # rather than manufacture the appearance of "the human import took effect".
        return "", "(the toolset does not cover it; not recorded in the graph and no question generated from it)"
    try:
        import question_gen
        rec, _ = await question_gen.generate_adopt(
            about=question, difficulty=2, tries=2, do_adopt=True, verbose=False,
            focus_dimension=focus)
    except Exception as exc:                                 # pragma: no cover
        print(f"  [import] failed to generate a new question: {type(exc).__name__}: {str(exc)[:120]}")
        return "", ""
    if not rec:
        return "", ""
    new_id = str(rec.get("topic_id") or "").strip()
    if not new_id:
        return "", ""
    try:
        TASKS[new_id] = question_gen.to_task(rec)
    except Exception as exc:                                 # pragma: no cover
        return "", ""
    # Write the question id back onto that **edge**: dimensions create no nodes, so the graph hints
    # during answering have to come from the payload on the edge
    # (`knowledge_graph._render_from_links`). Without the write-back the scaffold is empty when the
    # new question is attempted —— measured, `render_for_prompt` returns length 0.
    try:
        import knowledge_graph
        for lk in knowledge_graph.load_imported_links():
            if (str(lk.get("question") or "")[:300] == question[:300]
                    and not str(lk.get("topic_id") or "").strip()):
                knowledge_graph.link_import(
                    question=str(lk.get("question") or ""),
                    dimension=str(lk.get("dimension") or ""),
                    sql=str(lk.get("sql") or ""),
                    tool=str(lk.get("tool") or ""),
                    topic_id=new_id,
                    subject=dict(lk.get("subject") or {}),
                    value=lk.get("value"),
                )
    except Exception:                                        # pragma: no cover
        pass
    return new_id, "(the imported text does not route onto an existing question; a new one was generated)"


def _voyager_context(question: str, topic_id: str) -> dict[str, Any]:
    """Collect the capabilities Voyager has already accumulated (memory / skills / scene) for the
    human import to consume.

    The consumption form the user defined (2026-10-12): the human asks an imported question and
    **Voyager answers it** (with its skill library, memory and scene), while VLML's parse (tool
    trajectory + facts) is also disclosed. This is where the "Voyager capabilities" come from:
      · mem0 recall: semantic-recall principles + **deterministically retrieved correction
        memories** (correction fragments cannot rely on similarity top-N, they get squeezed out
        —— the same lesson as failure_correction_loop);
      · skill_store.retrieve: same-topic first, then lexically relevant skills top-4 (cross-topic
        experience has a relevance floor, so unrelated experience cannot lead it astray);
      · scene_context.scene_injection: the data world (base·ext layering of tools/tables + real
        entity anchors); the orchestration may only use the tools/tables visible in the scene.

    Any of these failing degrades gracefully (an empty block) and never blocks the explanation.
    """
    mem_texts: list[str] = []
    try:
        import mem0_backend
        sem = mem0_backend.recall(question, limit=4)
        corr = mem0_backend.recall(question, category="correction", limit=4)
        seen: set[str] = set()
        for t in [*sem, *corr]:
            if t and t not in seen:
                seen.add(t)
                mem_texts.append(t)
    except Exception:                                        # pragma: no cover
        pass
    skills: list[dict[str, Any]] = []
    try:
        import skill_store
        skills = skill_store.retrieve(topic_id or "", question, top_k=4)
    except Exception:                                        # pragma: no cover
        pass
    scene_text = ""
    try:
        import scene_context
        scene_text = scene_context.scene_injection(topic_id)
    except Exception:                                        # pragma: no cover
        pass
    parts: list[str] = []
    if mem_texts:
        parts.append("[Past experience memory (recalled by mem0; when orchestrating, avoid the pitfalls recorded in there)]\n"
                     + "\n".join(f"- {t}" for t in mem_texts[:6]))
    if skills:
        parts.append("[Skills already learned (reuse their data-retrieval approach first)]\n"
                     + "\n".join(f"- {s.get('name', '')}：{str(s.get('text', ''))[:160]}"
                                 for s in skills[:4]))
    if scene_text:
        parts.append(scene_text)
    return {
        "text": "\n\n".join(parts),
        "memory_count": len(mem_texts),
        "memory_texts": mem_texts[:6],
        "skills": [{"name": s.get("name", ""), "topic": s.get("topic", "")}
                   for s in skills[:4]],
        "scene": bool(scene_text),
    }


async def explain(question: str, *, topic_id: str = "",
                  auto_task: bool = True, voyager: bool = False) -> dict[str, Any]:
    """A human imports a question, and VLML answers it with an explanation (with voyager=True,
    Voyager answers it).

    voyager=False (the old default): pure VLML orchestration, retrieval and explanation —— the
    human only consumes VLML itself.
    voyager=True (the consumption form the user defined on 2026-10-12): **Voyager answers the
    human's question** —— before orchestrating, the capabilities it has already accumulated (mem0
    memory + skill library + scene) are collected and injected into the prompts of both the
    orchestration and the tidying steps; the return value carries `voyager_context` so the dashboard
    can disclose "which capabilities were consumed". VLML's parse (tool trajectory + facts) is
    still disclosed as before.

    The returned structure is isomorphic to Voyager's answer sheet (facts + narrative), but:
      - validated_target = False → does not count towards mastery
      - it writes one control fact into the causal timeline → visible to the question generator

    About topic_id: if it is left empty, this control fact's topic_id is empty, and the question
    generator's `latest_topics_by_kind()` skips an empty topic —— the human import happened but the
    question generator cannot see it, and the "influences adaptive question generation" line
    breaks here. So the entry point routes it onto a question first.

    `auto_task`: when it does **not** route onto any existing question, use this text to **generate
    a new question**.

    This step supplies the **opposite** of the study companion's "import learning content →
    MaterialTopicMapper maps it onto an existing knowledge point": when the study companion cannot
    map it, it makes the human pick one in the graph (`baseline_topic_prompt`); MVE additionally has
    `question_gen` and can turn this text straight into a question, so "a human import →
    influences question generation" does not break when routing fails —— it actually adds one way
    to grow new questions.

    About timing: **when routing fails, do not rush to generate the question before retrieval**.
    The new dimension a human asks about is often one VLML has just answered (before retrieval you
    have no idea what it is called), so retrieve first, get the facts, and only then use that
    dimension to seed a graph node + generate a question — that is what matches the human's
    direction. Generating before retrieval only makes `_graph_blueprint()` pick an old existing
    dimension (measured: importing "spike success rate" produced a question with nothing to do
    with plants).
    """
    inferred, hit = "", ""
    if topic_id:
        hit = "(specified by the dashboard)"
    else:
        inferred, hit = infer_topic(question)
        topic_id = inferred
        # when routing fails, do not generate a question here —— wait until the dimensions are in
        # hand after retrieval (see the docstring)

    # The Voyager consumption chain: collect capabilities (memory/skills/scene) before
    # orchestrating. Topic routing comes first, so that skill retrieval can benefit from the
    # same-topic boost.
    vctx: dict[str, Any] | None = None
    if voyager:
        vctx = _voyager_context(question, topic_id)

    base: dict[str, Any] = {
        "question": question,
        "topic_id": topic_id,
        "topic_matched": bool(topic_id),
        "topic_hit": hit,
        "topic_inferred": bool(inferred),
        "source": "human_import",           # only the question generator's output is targeted_question
        "validated_target": VALIDATED_TARGET_IMPORTED,
        "mode": "voyager" if voyager else "vlml",
    }
    if voyager and vctx is not None:
        base["voyager_context"] = {
            "memory_count": vctx["memory_count"],
            "memory_texts": vctx["memory_texts"],
            "skills": vctx["skills"],
            "scene": vctx["scene"],
        }

    plan_user = _plan_prompt(question)
    if vctx and vctx.get("text"):
        plan_user += ("\n\nBelow are the capabilities Voyager has already accumulated (memory / skills / data "
                      "world). When orchestrating, reuse the skills' approach first, avoid the "
                      "pitfalls recorded in memory, and use only the tools and tables visible in "
                      "the scene:\n" + str(vctx["text"]))
    plan = chat_json([
        {"role": "system", "content": "You are the original VLML analysis engine; you only retrieve "
                                      "data and never make subjective inferences."},
        {"role": "user", "content": plan_user},
    ])
    obs, traj, errors = await _execute(plan.get("calls") or [])

    # The data-retrieval SQL and tools **actually used** by this human import —— when a new dimension
    # enters the graph they are what fill in tables / columns / recipe, and without those three
    # `_graph_blueprint()` cannot pick it.
    used_sql, used_tool = "", ""
    for o in obs:
        a = o.get("args") or {}
        if not used_sql and a.get("sql_query"):
            used_sql = str(a.get("sql_query") or "")
            used_tool = str(o.get("tool") or "")
    if not used_tool and traj:
        used_tool = str(traj[0])

    # The tool-call gate (the same as on Voyager's path): no facts may be produced if not a single
    # call succeeded. Hit in practice: when every tool errored the model still made up 3 "facts"
    # whose values were entirely its own invention.
    if not traj:
        result = {
            **base,
            "trajectory": [],
            "errors": errors or ["the plan produced no valid tool calls"],
            "facts": [],
            "narrative": "",
            "insights": [],
            "caveats": [],
            "comparable": False,
            "skipped": "no data was retrieved this round, so no explanation is produced "
                       "(to avoid dressing an error up as an answer)",
        }
        # A failed retrieval still has to enter the timeline: the fact that the human raised this
        # intent is a fact, and the question generator must be able to see it. It simply has no
        # facts, so it cannot serve as evidence for question generation — only a direction signal.
        causal_timeline.append(
            causal_timeline.CONTROL,
            topic_id=topic_id,
            summary=f"human-imported question (no data retrieved): {question[:80]}",
            detail={"trajectory": [], "facts": 0, "validated_target": False},
        )
        _log_import(result)
        return result

    extract_user = _extract_prompt(question, obs)
    if vctx and vctx.get("text"):
        extract_user += ("\n\n(When tidying the facts, refer to the conventions and pitfalls in Voyager's memory, "
                         "but the numbers must always follow what the tools returned: memory affects "
                         "how you ask, never the numbers.)\n"
                         + str(vctx["text"]))
    out = chat_json([
        {"role": "system", "content": "You are a rigorous data tidier; you output only JSON and "
                                      "never invent numbers."},
        {"role": "user", "content": extract_user},
    ])

    facts = []
    raw_facts: list[dict[str, Any]] = []
    for f in (out.get("facts") or []):
        if not isinstance(f, dict) or "subject" not in f or "dimension" not in f:
            continue
        # The raw facts must carry this round's retrieval SQL / tool: a new dimension entering the graph
        # relies on them to fill in tables / columns / recipe (without those three the question
        # generator cannot pick a new node).
        raw_facts.append({**f, "sql": used_sql, "tool": used_tool})
        facts.append(make_fact(
            subject=f.get("subject") or {},
            dimension=str(f.get("dimension")),
            value=f.get("value"),
            unit=str(f.get("unit") or "raw"),
            base=f.get("base"),
            source={"tool": "vlml_coach", "args": {}},
        ))

    # Does not route onto an existing question → use the dimension VLML just answered to seed a graph
    # node + generate a question around it. Placed after retrieval: the dimension name only exists
    # once data has been retrieved, and generating beforehand just loops back to an old dimension.
    if not topic_id and auto_task:
        topic_id, hit = await _task_from_text(question, raw_facts)
        base["topic_id"] = topic_id
        base["topic_matched"] = bool(topic_id)
        base["topic_hit"] = hit
        # What exactly landed on the graph side is handed back for the dashboard to display ——
        # "was it recorded or not" must be verifiable, not just one line in the log.
        try:
            import knowledge_graph as _kg
            hit_lk = [l for l in _kg.load_imported_links()
                      if str(l.get("question") or "")[:300] == question[:300]]
            if hit_lk:
                lk = hit_lk[-1]
                base["graph_link"] = {
                    "covered": bool(lk.get("covered")),
                    "cover_level": str(lk.get("cover_level") or ""),
                    "insights": list(lk.get("insights") or []),
                    "insights_total": int(lk.get("insights_total") or 0),
                    "tables": list(lk.get("tables") or []),
                }
        except Exception:                                    # pragma: no cover
            pass

    # The explanation comes from the LLM (VLML README:97: metrics come from the tools, insights from
    # the LLM)
    story = narrator.narrate(None, facts, verdict="") if facts else {
        "narrative": "", "insights": [], "caveats": [],
        "comparable": False,
        "skipped": "no data was retrieved, so no explanation is produced",
    }

    result = {
        **base,
        "trajectory": traj,
        "errors": errors,
        "facts": facts,
        "narrative": story.get("narrative", ""),
        "insights": story.get("insights") or [],
        "caveats": story.get("caveats") or [],
        "comparable": False,
    }

    # Enters the timeline (causal), does not enter mastery (grading)
    causal_timeline.append(
        causal_timeline.CONTROL,
        topic_id=topic_id,
        summary=f"human-imported question: {question[:80]}",
        detail={"trajectory": traj, "facts": len(facts),
                "validated_target": False},
    )
    _log_import(result)
    return result


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "Which map did Cloud9 collapse on the most?"
    res = asyncio.run(explain(q))
    print(json.dumps({k: v for k, v in res.items() if k != "facts"},
                     ensure_ascii=False, indent=2))
    print(f"[facts: {len(res['facts'])}]")
    for f in res["facts"][:8]:
        subj = "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items())
        print(f"  {subj} · {f['dimension']} = {f['value']} (base={f.get('base')})")
