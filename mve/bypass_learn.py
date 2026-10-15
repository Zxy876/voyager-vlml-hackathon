#!/usr/bin/env python3
"""Bypass learning: accumulate skills by directly observing the referee (VLML0)'s standard
solution instead of doing the questions.

Why this bypass has to exist
------------------------
Human learners have two ways of accumulating ability: doing questions (being examined), and
learning without doing questions (studying worked examples, reading walkthroughs, reading the
standard solution). In the study companion the latter is the **ability module** (blueprint in
`adaptive_learning/cognitive_catalog.py` + delivery in `cognitive_delivery.py`): it is independent
of question answering and brings ability to the learner along
"cognitive defect → repair strategy → concrete action".

Before this, MVE had only the question-answering path, and that path had a deadlock:

    on a wrong answer nothing is written to the skill library —— what it did has been proven wrong
    by the referee, so storing it would be planting a landmine (`voyager.py:1103-1114`; this
    decision is correct)

So learning **can only happen when the answer is right**, and what gets written then is a template
(`voyager.py:1048-1050`). Result: the skill library holds one or two entries and little substance.

This bypass breaks the deadlock: **what is learned on the bypass is not Voyager's own approach but
the referee's approach** —— the referee's approach is computed by the deterministic layer and
already verified correct, so it is safe to solidify.

Division of labour with the exam channel (the key distinction the study companion contributes)
----------------------------------------
After grading, the study companion **feeds the reference answer back to the learner**
(`reference_answer` at `tutor_llm_agent_answer_evaluate.py:84` and `:123`) —— because its purpose is
"learning", not "passing". MVE's `learn()` deliberately gives no numbers (`voyager.py:1068`), because
what it wants is a comparable mastery level.

Both are right, they are just different channels:
    exam channel (doing questions) —— no answer given, coverage is graded, feeds mastery
    bypass channel (this module)   —— the standard solution is given, not graded, only the skill
                                      library is fed

How to run it
----
    python mve/bypass_learn.py --topic pistol_eco_pattern
    python mve/bypass_learn.py --topic series_totals --force   # re-run the referee
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vlml_env  # noqa: F401,E402  must bootstrap the environment first

import causal_timeline  # noqa: E402
import skill_store  # noqa: E402
import vlml0_referee  # noqa: E402
from llm_client import chat_json  # noqa: E402
from tasks import TASKS  # noqa: E402

BYPASS_SOURCE = "bypass"


def _prompt(task: Any, ref: Any, traj: list[str], dims: list[str]) -> str:
    rubric = "\n".join(f"- {p.point}｜dimension=`{p.dimension}`" for p in task.rubric)
    return f"""You are an analysis agent that is **observing** a standard solution, preparing to turn it
into an approach you can reuse yourself.

Question: {task.question}

The rubric points that must be covered:
{rubric}

The tool sequence the standard solution (already verified correct) used: {' → '.join(traj) or '(none)'}
The dimensions it retrieved: {', '.join(dims) or '(none)'}

Please distill one **approach that will be useful next time you meet a similar question** and output JSON:
{{"name": "skill name, in the fixed format 'tool name + what dimension you split it by', e.g. match_summary_report drill down by map",
  "lesson": "one or two sentences, only the reusable approach: which tool to call, which dimension to split it by, which layer to fetch first and which layer to drill into next. Do not restate the question and do not write concrete values",
  "defect": "which class of defect this approach solves; pick one of: missing_dimension (a dimension was missed) / wrong_granularity (the granularity is wrong) / tool_uncovered (the tool was never used) / value_form (the value shape is wrong)",
  "strategy": "the repair strategy; pick one of: drill_down (drill one level deeper) / switch_tool (switch tools) / combine_tools (combine several tools) / read_scope (read the report's scope metadata)"}}

The naming rule matters: a skill with the same name overwrites rather than adding, so the name must
be stable and summarise the approach; do not put the round number, the question text or concrete
values in it."""


def _solution_code(task: Any) -> str:
    """Turn VLML0's standard solution into **learnable source code** —— this is the original's
    `program_code`.

    The original `add_new_skill` stores `info["program_code"]`: the JS Voyager wrote itself and
    got running. MVE's Voyager produces no code, but here there is something the original does not
    have: **VLML0's standard solution is deterministic and already verified correct** (the
    server-side answer_spec + the referee's trajectory) —— it is naturally "correct source code". So
    the mapping becomes:

        original: write it myself → get it running → store the source
        MVE     : judged wrong → observe VLML0 → store its solution source

    The desensitization boundary follows the same principle as the knowledge graph:
    **give conventions and structure, never the answers**:
      - SQL goes through `_desensitize`, every string literal is wiped to `'?'`;
      - the argument values of tool calls are wiped too, leaving only parameter names and shapes.
    So what the model learns is "which table to query, how to layer it, which column / which path to
    take" rather than "what the answer is" —— the numbers must still be retrieved by itself.
    """
    try:
        from knowledge_graph import _desensitize
    except Exception:                                   # pragma: no cover
        def _desensitize(s: str) -> str:                # type: ignore[misc]
            return s or ""

    lines: list[str] = []
    for p in getattr(task, "rubric", None) or []:
        spec = getattr(p, "answer_spec", None)
        if spec is None:
            continue
        dim = str(getattr(p, "dimension", "") or "")
        point = str(getattr(p, "point", "") or "")
        sql = str(getattr(spec, "sql", "") or "")
        tool = str(getattr(spec, "tool", "") or "")
        if sql:
            body = _desensitize(sql).strip()
            tail = f"take column {getattr(spec, 'value_column', 0)}"
            base = getattr(spec, "base_column", None)
            if base is not None:
                tail += f", denominator is column {base}"
            if getattr(spec, "percent", False):
                tail += " (percentage: num/denom*100)"
            lines.append(f"[{dim}] {point}\n    {body}\n    # {tail}")
        elif tool:
            args = getattr(spec, "tool_args", None) or {}
            sig = ", ".join(f"{k}='?'" for k in (args or {})) or ""
            path = str(getattr(spec, "value_path", "") or "")
            base = str(getattr(spec, "base_path", "") or "")
            if getattr(spec, "percent", False):
                # The percentage convention must give **both the numerator and the denominator paths**.
                # Hit in practice: the source only wrote `...kast.num` and the model copied it, did
                # dig up 109 and handed that in —— it did not know it still had to divide by denom.
                # Giving one path amounts to letting it compute half, and that "half" looks exactly
                # like a normal number (unlike None, it cannot prove itself wrong).
                num_path = path or base
                den_path = base or (
                    path[: -len(".num")] + ".denom" if path.endswith(".num") else "")
                lines.append(
                    f"[{dim}] {point}\n    {tool}({sig})\n"
                    f"    # percentage: fetch two numbers and divide them\n"
                    f"    #   num   = dig(p, \"{num_path}\")\n"
                    f"    #   denom = dig(p, \"{den_path}\")\n"
                    f"    #   return {{\"num\": num, \"denom\": denom}} (do not compute it beforehand and return the result)")
            else:
                # Scalar metric. Hit once in practice, so 'scalar ≠ num/denom' must be spelled out:
                #   kast and kd both live under key_metrics.team.consistency (kast is a
                #   {num,denom}, kd is the scalar 1.24), and they look far too similar. This used to
                #   say only "take the value at the path", and when the model copied that it applied
                #   kast's num/denom pattern to kd and got 109/109 = 1.0 —— stuck at 50% for 48
                #   rounds straight. A path alone is not enough to prevent mismatching, so it must
                #   say explicitly "do not take .num/.denom".
                lines.append(
                    f"[{dim}] {point}\n    {tool}({sig})\n"
                    f"    # scalar: whatever dig returns is the final value, no arithmetic needed\n"
                    f"    #   value = dig(p, \"{path}\")\n"
                    f"    #   note: this metric is **not** a num/denom structure ——\n"
                    f"    #   do not reach for .num / .denom, and do not divide some\n"
                    f"    #   other metric's num/denom to get it; return the raw value")
    return "\n".join(lines)


async def learn_from_referee(topic_id: str, *, force: bool = False) -> dict[str, Any]:
    """Observe the referee answering this question → distill one reusable approach + **store its
    solution source** → write it into the skill library."""
    if topic_id not in TASKS:
        return {"ok": False, "error": f"unknown question {topic_id}"}
    task = TASKS[topic_id]

    ref = await vlml0_referee.answer(task, force=force)
    if not getattr(ref, "facts", None):
        return {"ok": False, "error": "the referee produced no facts, nothing to observe"}

    dims = [str(f.get("dimension")) for f in ref.facts]
    # Internal markers must not enter the skill library: measured, Voyager takes
    # "<deterministic-sql>" for a real tool name
    traj = [str(t) for t in (getattr(ref, "trajectory", None) or [])
            if not str(t).startswith("<")]

    out = chat_json(
        [
            {"role": "system",
             "content": "You are an analysis agent who distills a standard solution into a reusable approach."},
            {"role": "user", "content": _prompt(task, ref, traj, dims)},
        ],
        temperature=0.2,
    )
    name = str(out.get("name") or "").strip()
    lesson = str(out.get("lesson") or "").strip()
    if not lesson:
        return {"ok": False, "error": "the model did not distill any approach"}

    blueprint = {
        "defect": str(out.get("defect") or "").strip(),
        "strategy": str(out.get("strategy") or "").strip(),
        "trajectory": traj,
        "dimensions": dims,
        "source": BYPASS_SOURCE,
    }
    code = _solution_code(task)
    action, key = skill_store.add(
        topic_id, name or f"{topic_id} standard orchestration", lesson,
        source=BYPASS_SOURCE, blueprint=blueprint, code=code,
    )

    causal_timeline.append(
        causal_timeline.CONTROL,
        topic_id=topic_id,
        summary=f"bypass learning: observed the referee's solution → {action} {name or topic_id}"
                f" (stored {len(code)} characters of solution source)",
        detail={"trajectory": traj, "dimensions": dims, "action": action,
                "code_len": len(code),
                # Only the length, never the full text: the source contains server-side recipes and
                # persisting it would leak the exam. Read the full text in the skill library; do not
                # keep a copy in the log.
                "validated_target": False},
    )
    return {
        "ok": True, "topic_id": topic_id, "action": action, "key": key,
        "name": name, "lesson": lesson, "blueprint": blueprint,
        "code": code, "code_len": len(code),
        "trajectory": traj, "dimensions": dims,
    }


RETRY_SOURCE = "help"           # asking for help after a wrong answer → mastery counts it as "with help"

# The four-section structure of the walkthrough, copied from the headings in the study companion's
# _solution_structure.py:173:
#   question analysis / solution process / answer / transfer to a similar question
# Note that the third section is the **answer** —— in the study companion's walkthrough the answer is
# visible (the settings description puts it plainly: "only the question analysis, the answer and the
# transfer to a similar question are narrated").
#
# I used to design this as "explain the approach but give no values", on the grounds that it would
# leak the answer. That conflated two different things:
#   the study companion's "show hint" (ui.button.show_hint → main.js:3444 sets used_hint)
#     —— opened mid-question, its content is method/knowledge points (e.g. "remember: 180° equals
#        π radians"), and it does **not** contain this question's answer
#   the study companion's "walkthrough" (solution structure, four sections including the answer)
#     —— a **learning stage**: explain it thoroughly (including the answer) first, then practise
# The study companion's default path is onboarding.md:47 "enter practice from the walkthrough" ——
# learn first (the answer is visible), then practise.
# MVE only has the "do questions" path and wedged the help request into the middle of it, which
# produced a half-help that belonged to neither:
# it did not help solve the question (the model still did not know which value or which convention
# to take) and, unlike a walkthrough, taught nothing.
# Measured consequence: after three help requests on corrode_collapse, the next round instead
# reported "no tool called" with 0% coverage.
STUDY_SECTIONS = ("Question analysis", "Solution process", "Answer",
                  "Transfer to a similar question")


def _is_real_tool(name: str) -> bool:
    """Whether this tool name is an MCP tool Voyager can really call.

    The referee's standard solution mixes in its own internal tools (vlml0_referee::deterministic
    and the like); those are what the referee does its arithmetic with and Voyager cannot reach
    them. They must be told apart before being narrated to Voyager, otherwise it goes off to query
    a table that does not exist.
    """
    try:
        from action_code import TOOL_NAMES
        return str(name) in TOOL_NAMES
    except Exception:
        # When the tool list cannot be obtained, fall back: at least rule out prefixed internal names
        return "::" not in str(name) and not str(name).startswith("vlml0")


def _explain_prompt(task: Any, *, traj: list[str], dims_by_tool: dict[str, list[str]],
                    missing: list[str], my_plan: list[str],
                    facts_text: str, graph_text: str = "") -> str:
    mapping = "\n".join(
        f"- {tool} → {', '.join(dims) or '(none)'}" for tool, dims in dims_by_tool.items()
    ) or "(none)"
    # The walkthrough's conventions must follow the knowledge graph —— otherwise you get "the graph
    # says A, the walkthrough says B" and it does not know next round whom to believe. Dimensions
    # that are not in the graph are the only ones the walkthrough may improvise on.
    gblock = f"""
[Knowledge graph · the conventions for this question] The walkthrough must agree with the below
(this is a server-side recipe, not a suggestion):
{graph_text}
""" if graph_text else ""
    return f"""You are a walkthrough author, explaining the standard solution **once** to an analysis
agent that just got this question wrong.

Question: {task.question}

Its orchestration this time: {' → '.join(my_plan) or '(called no tools at all)'}
The dimensions it did not get: {', '.join(missing) or '(none)'}

The standard solution's orchestration: {' → '.join(traj) or '(none)'}
Which dimensions each tool is responsible for:
{mapping}

**Important**: anything in the orchestration marked "(internal to the referee, not a callable tool)"
is an internal structure the referee does its own arithmetic with, and **it cannot call it**. When
narrating the "solution process" you must mention only tools it can really call, otherwise it goes
off to query a table that does not exist (this happened in practice: the walkthrough mentioned
`vlml0_referee.deterministic` and next round it really did FROM that table → Catalog Error).

The **real values** the standard solution retrieved (computed by the referee's deterministic layer,
already verified, safe to narrate directly):
{facts_text or '(none)'}
{gblock}
Please narrate it in four sections and output JSON:
{{"analysis": "Question analysis: what this question is really asking and how the rubric points depend on each other",
  "process": "Solution process: what to call first, what you get, and why you still need to call the next one; be especially clear about the **convention** (filter conditions, grouping, ordering), since that is exactly where it went wrong",
  "answer": "Answer: organise the values above by rubric point and state clearly what each value is",
  "transfer": "Transfer to a similar question: next time a similar question appears, what to look at first and which tool to reach for first"}}

Requirements:
- `process` must make the **convention** clear (how to filter, group by what, sort by what), not just
  tool names.
- When the knowledge graph is given above, conventions, column names and value paths must all follow
  the graph; do not write a second set of your own.
- The values in `answer` may only come from the real values given above; you may not invent a single
  one.
- This is a **learning stage**, so being thorough is the point; when it redoes the question next
  round it must retrieve the evidence with its own tools and cannot copy this in."""


def _graph_text(task: Any) -> str:
    """This question's knowledge graph subgraph —— the walkthrough's conventions must be the very
    same ones that were visible at planning time.

    Why the walkthrough has to carry it too: the graph used to go only into the planning prompt and
    the walkthrough improvised off the referee's trajectory, so the two could give different
    conventions ("the graph says use AVG(fb_team_won), the walkthrough says use COUNT"). Next round
    it sees both at once and has to guess a second time.
    """
    try:
        from voyager import graph_context
        # mode="explain": the walkthrough / help stage does **not** get fed the confusable
        # dimensions (confusable / co_occurs). Following the study companion's response_mode split
        # (knowledge_graph_guidance.py:1302-1310) —— general_explanation keeps only
        # prerequisite/application/supports/extends/confusable, and during a walkthrough telling it
        # "what this is easily confused with" turns asking for help into leading it astray; only at
        # planning time is that needed as a guardrail.
        return graph_context(task, mode="explain")
    except Exception:
        return ""


async def explain_for_retry(
    task: Any,
    *,
    my_plan: list[str] | None = None,
    missing: list[str] | None = None,
) -> dict[str, Any]:
    """Asking for help after a wrong answer: **first walk through the standard solution (the learning
    stage), then let it redo the question carrying that**.

    Following the study companion's channel split
    ----------------
    The study companion separates "learning" and "passing" into two paths (onboarding.md:47
    "enter practice from the walkthrough"):

        walkthrough (learning stage) —— four sections: question analysis / solution process /
                                         **answer** / transfer to a similar question
                                         the answer is visible, because the purpose is learning
                                         (`_solution_structure.py:173`)
        practice (exam stage)        —— answer it yourself; the method hint is given only when you
                                         get it wrong (no answer inside);
                                         clicking the hint records a used_hint in mastery (×0.85)

    MVE's isomorphic version:
        learning stage (this function) —— the full walkthrough, values taken from the referee's
                                         deterministic facts, so they can be narrated
        redo stage (next round)       —— the prompt only carries the approach (orchestration +
                                         which dimensions each tool is responsible for); values are
                                         not fed back, and it still has to retrieve them itself

    Why the values are safe to give: MVE has **provenance verification** (loop_core's hallucination
    interception). Copying a number that was not retrieved with a tool fails the provenance check
    and coverage still does not climb —— copying the answer buys nothing, while it can genuinely
    learn the conventions (which is exactly what corrode_collapse was missing).
    """
    ref = await vlml0_referee.answer(task)
    if not getattr(ref, "facts", None):
        return {"ok": False, "error": "the referee produced no facts, nothing to explain"}

    traj = [str(t) for t in (getattr(ref, "trajectory", None) or [])
            if not str(t).startswith("<")]
    dims_by_tool: dict[str, list[str]] = {}
    for f in ref.facts:
        src = str((f.get("source") or {}).get("tool") or "")
        tool = src.split("::", 1)[1] if "::" in src else (src or "deterministic-sql")
        # The referee's internal tools (e.g. vlml0_referee::deterministic) are **not** MCP tools
        # Voyager can call. Unless they are marked, the walkthrough narrates them as "call this
        # next" —— measured: the model duly did FROM vlml0_referee.deterministic → Catalog Error,
        # so asking for help ended up voiding that round. Hence the explicit rename here plus a
        # flat prohibition in the prompt.
        if not _is_real_tool(tool):
            tool = f"{tool} (internal to the referee, not a callable tool)"
        dims_by_tool.setdefault(tool, [])
        d = str(f.get("dimension") or "")
        if d and d not in dims_by_tool[tool]:
            dims_by_tool[tool].append(d)

    my_plan = [str(t) for t in (my_plan or [])]
    missing = [str(m) for m in (missing or [])]

    # The values the walkthrough narrates: only the true values computed by the referee's
    # deterministic layer, never the ones the model made up
    facts_text = "\n".join(
        f"- {f.get('dimension')} = {f.get('value')}"
        + (f" (denominator {f.get('base')})" if f.get("base") else "")
        for f in ref.facts
    )
    out = chat_json(
        [
            {"role": "system",
             "content": "You are a walkthrough author who explains the standard solution thoroughly: "
                        "make the conventions explicit, and the answer may only cite the given true values."},
            {"role": "user", "content": _explain_prompt(
                task, traj=traj, dims_by_tool=dims_by_tool,
                missing=missing, my_plan=my_plan, facts_text=facts_text,
                graph_text=_graph_text(task))},
        ],
        temperature=0.2,
    )
    study = {
        "analysis": str(out.get("analysis") or "").strip(),
        "process": str(out.get("process") or "").strip(),
        "answer": str(out.get("answer") or "").strip(),
        "transfer": str(out.get("transfer") or "").strip(),
    }
    explain = study["process"] or study["analysis"]
    lesson = study["transfer"]
    if not any(study.values()):
        return {"ok": False, "error": "the model produced no walkthrough"}

    causal_timeline.append(
        causal_timeline.REFEREE,
        topic_id=getattr(task, "topic_id", ""),
        summary=f"studied the standard solution (VLML0, answer included): {explain[:52] or study['answer'][:52]}",
        detail={"trajectory": traj, "dims_by_tool": dims_by_tool,
                "my_plan": my_plan, "missing": missing,
                "study": study, "with_answer": True,
                "used_hint": True, "validated_target": False},
    )
    return {
        "ok": True,
        "topic_id": getattr(task, "topic_id", ""),
        # Backward-compatible fields: explain / lesson now mean "solution process" and "transfer to a
        # similar question"
        "explain": explain, "lesson": lesson,
        "study": study, "sections": list(STUDY_SECTIONS),
        "plan": traj, "dims_by_tool": dims_by_tool,
        # deliberately **not** fed back on the redo: the values must be retrieved by itself
        "facts": [
            {"dimension": f.get("dimension"), "value": f.get("value"),
             "base": f.get("base")}
            for f in ref.facts
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Bypass learning: accumulate skills by observing the referee's solution")
    ap.add_argument("--topic", required=True, help=f"question id, one of: {', '.join(TASKS)}")
    ap.add_argument("--force", action="store_true", help="re-run the referee (ignore the cache)")
    args = ap.parse_args()

    res = asyncio.run(learn_from_referee(args.topic, force=args.force))
    if not res.get("ok"):
        print(f"Bypass learning failed: {res.get('error')}")
        return 1
    print(f"Question     : {res['topic_id']}")
    print(f"Standard plan: {' → '.join(res['trajectory']) or '(none)'}")
    print(f"Dimensions   : {', '.join(res['dimensions'])}")
    print(f"Skill written: {res['action']} · {res['name']}")
    print(f"              {res['lesson']}")
    print(f"Blueprint    : defect={res['blueprint']['defect']} · strategy={res['blueprint']['strategy']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
