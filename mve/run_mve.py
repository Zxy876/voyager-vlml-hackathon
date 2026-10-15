#!/usr/bin/env python3
"""MVE main loop: question generation → Voyager data retrieval → fact set → coverage → verdict → mastery → learning.

Usage:
    python mve/run_mve.py

The question to verify: **can Voyager, by accumulating skills, answer the same question more completely.**
Observation method: the same question three rounds in a row, watching whether coverage and mastery rise with skill accumulation.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vlml_env  # noqa: F401,E402  must bootstrap the environment first

import causal_timeline  # noqa: E402
import difficulty  # noqa: E402
import bypass_learn  # noqa: E402
import mastery_model  # noqa: E402
import mastery_retention as retention  # noqa: E402
import narrator  # noqa: E402
import planner  # noqa: E402
import run_log  # noqa: E402
import skill_store  # noqa: E402
import vlml0_referee  # noqa: E402
from loop_core import (  # noqa: E402
    AnswerSpec,
    RubricPoint,
    Task,
    evaluate_vs_referee,
    fact_key,
    task_done,
    update_mastery,
)
from voyager import LLMVoyager, ScriptedVoyager  # noqa: E402

from tasks import SERIES, TASKS, TASK, TASK_HARD, TASK_MID, TASK_PATTERN, next_topic  # noqa: E402

# The criterion for "task done" (mirroring the original Voyager's self-verification):
#   done = Voyager's own ruler: orchestration ran (successful tool calls) + data retrieved (non-empty facts)
#          + no crash (no tool errors) — **coverage points not counted**.
# The user decided: the learning stage (store skill / stop same topic / advance to next topic) does not count coverage points — coverage points are
# VLML's ruler, used only for exams (exam transfer/final) and mastery evidence. The original critic
# judges exactly "observable task-goal achievement in the environment", not "how many rubric points were covered".


def _stable_skill_name(task: Any) -> str:
    """A skill's **stable primary key** = the indexed graph question type (mirroring the original's `program_name`).

    Design: the skill primary key uses the "graph question type" (e.g. `c9_win_rate`), not the model's phrasing for that turn.
    The original's primary key is the function name — stable, so same names go through Rewriting to update the same entry. MVE previously used
    one LLM sentence per round as the name, so any phrasing change was a "new skill" (observed versions rising to 49,
    bloat rate 17.5, with no version ever truly updated). The name must be decided by the **question/topic**.
    """
    return f"{getattr(task, 'topic_id', '') or 'task'}"


def _plan_program(topic_id: str, calls: list[dict[str, Any]]) -> tuple[str, str]:
    """Render the **call sequence** that ran through the plan path into a readable program (an exec-able form).

    Why it is needed: SQL-type questions take the plan path, which does not produce `program_code`,
    so "it ran" contributes nothing to the skill library. Its program is this call sequence — it must be stored to be replayed.
    Rendering it in a human-readable form is so a post-mortem can see at a glance "how exactly it retrieved data back then";
    actual reuse goes through `blueprint.calls` (`voyager._replay_saved`).
    """
    fn = "".join(ch if (ch.isalnum() or ch == "_") else "_"
                 for ch in str(topic_id)) + "_solve"
    lines = [f"# Self-run orchestration ({topic_id}) — replay it; the metric has been verified by the referee",
             f"async def {fn}(mcp):", "    obs = []"]
    for i, c in enumerate(calls):
        args = ", ".join(f"{k}={v!r}" for k, v in (c.get("args") or {}).items())
        lines.append(f"    r{i} = await mcp.{c.get('tool')}({args})")
        lines.append(f"    obs.append(r{i})")
    lines.append("    return {'_obs': obs}")
    return "\n".join(lines), fn


def _successful_calls(voyager: Any) -> list[dict[str, Any]]:
    """This round's calls that **actually succeeded** (tool + args only, no return values).

    Each trajectory item also carries the tool's raw response (`result`) — that is an entire table,
    and storing it in the skill library bloats the json to several MB, while being useless for reuse.
    """
    out: list[dict[str, Any]] = []
    for t in (getattr(voyager, "trajectory", None) or []):
        if not isinstance(t, dict) or not t.get("tool"):
            continue
        args = {k: v for k, v in (t.get("args") or {}).items()
                if isinstance(v, (str, int, float, bool))}
        out.append({"tool": str(t["tool"]), "args": args})
    return out


def _deterministic_ratio(ref_facts: list[dict[str, Any]]) -> float:
    """What share of referee facts comes from "deterministic sources" — used by V2 evidence weight's evaluator_confidence.

    The study companion only has the single number "evaluator confidence defaults to 0.75" (it has no way to distinguish); MVE's referee facts
    carry source labels, so it can be quantified: direct SQL / tool path = deterministic, llm = model-filled.
    """
    if not ref_facts:
        return 0.0
    det = 0
    for f in ref_facts:
        tool = str(((f.get("source") or {}).get("tool")) or "")
        if "deterministic" in tool or tool.startswith("vlml0_referee::"):
            det += 1
    return det / len(ref_facts)


# ===========================================================================
# Voyager isomorphic learning loop (the cockpit's "▶ Start Voyager" goes here, landed 2026-10-12)
# ---------------------------------------------------------------------------
# Isomorphism reference (original Voyager learn/rollout/step):
#   curriculum_agent.propose_next_task  →  topic_graph.select_next (novelty/give-up/scene conditioning)
#   rollout (max N retries per question)        →  max rounds attempts per topic
#   step (critic verifies done)            →  open_quiz.grade_answer (study-companion-style semantic scoring)
#   done = success or retries exhausted          →  same; success→store SQL skill, failure→stale skip
#   add_new_skill (stores program_code verbatim)→  skill_store.add(code=call sequence)
# The question generator (planner) / topic recommendation / scaffolding (hint) / original mve memory injection are all removed;
# the graph is isomorphic to "spawn-point metadata" = scene_context (biome/nearby_blocks/inventory/chests).
# Scoring comes only from grade_answer (verdict/score); advancement is decided by the done condition, not decided for voyager.
# ===========================================================================

_PILOT_STATE = Path(__file__).resolve().parent / "pilot_state.json"


def _pilot_stop_requested() -> bool:
    try:
        return bool(json.loads(_PILOT_STATE.read_text(encoding="utf-8"))
                    .get("stop_requested"))
    except Exception:
        return False


def _stop_and_wait(*, why: str) -> None:
    """Design ⑫: multiple consecutive rounds without evidence → stop and wait for human intervention. Writes pilot_state (the dashboard reads it), and prints a clear reason.

    The key distinction: "pipeline not working" (not a single tool call succeeded / the retrieved dataset is empty) vs
    "not learned" (tools were called but values are wrong, coverage low) — the former is a broken evidence path, not a model capability
    problem, and continuing to spin only replays the same failure. Stop and wait for a human to fix the data source / entity values / tool connectivity before restarting.
    """
    state: dict[str, Any] = {}
    try:
        state = json.loads(_PILOT_STATE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    state.update({
        "running": False,
        "stop_requested": True,
        "why": why,
        "stopped_at": datetime.now().isoformat(timespec="seconds"),
    })
    try:
        _PILOT_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    except Exception:
        pass
    print(f"\n[BLOCK] Loop stopped, waiting for human intervention: {why}\n")


def _answer_from(result: dict[str, Any]) -> str:
    """Splice Voyager's retrieval results into a piece of "answer text" that can be scored.

    Generated questions have no rubric, and scoring is semantic (grade_answer compares accepted_answers/key_points),
    so feeding the grader the agent's conclusion (narrative) + retrieved facts + planning thought together
    is more robust than giving only a one-sentence conclusion.
    """
    narr = result.get("narrative") or ""
    if isinstance(narr, dict):
        narrative = str(narr.get("text") or "")
    else:
        narrative = str(narr)
    facts = result.get("facts") or []
    facts_text = "；".join(
        f"{f.get('dimension')}={f.get('value')}"
        for f in facts[:10] if isinstance(f, dict))
    thought = result.get("thought") or ""
    return "\n".join(x for x in (narrative,
                                 (f"Data: {facts_text}" if facts_text else ""),
                                 (f"Thought: {thought}" if thought else ""))
                     if x).strip()


def _store_skill_for(voyager: Any, topic_id: str, task: Any, result: dict[str, Any],
                    score: float, question: str) -> str | None:
    """When judged correct, solidify this round's successful orchestration into **reusable SQL/tool code** for the skill library.

    The original add_new_skill stores program_code verbatim; MVE's equivalent is:
      · program_code path (code-based evidence) → store the source directly
      · plan path (SQL-type, the majority) → render the successful call sequence into a replayable program
    Both paths land in the `code` field, not talk/one or two sentences. Returns the skill name or None.
    """
    try:
        prog = str(result.get("program_code") or "")
        if prog:
            _saved = skill_store.add(
                topic_id, _stable_skill_name(task),
                f"Self-run orchestration (score {score:.0%}): {question[:60]}",
                code=prog,
                blueprint={"source": "practice", "coverage": score,
                           "program_name": result.get("program_name") or ""},
                source="practice")
            return _saved[1]
        calls = _successful_calls(voyager)
        if calls:
            code, fn = _plan_program(str(topic_id), calls)
            _saved = skill_store.add(
                topic_id, _stable_skill_name(task),
                f"Self-run orchestration (score {score:.0%}): "
                f"{' → '.join(str(c['tool']) for c in calls)}",
                code=code,
                blueprint={"source": "practice", "coverage": score,
                           "program_name": fn, "calls": calls},
                source="practice")
            return _saved[1]
    except Exception as e:                 # pragma: no cover
        print(f"  (Failed to store program: {type(e).__name__}: {e})")
    return None


class _GenTask:
    """Minimal pseudo-Task: LLMVoyager.run only uses question / rubric / topic_id /
    subject / validated_target. The reference SQL attached to a generated question is assembled into a rubric
    — non-empty rubric → Voyager takes the **execution path** (really running SQL to retrieve data), and VLML0
    the referee can independently compute the objective standard answer from answer_spec.sql."""

    def __init__(self, question: str, topic_id: str,
                 rubric: list[Any] | None = None) -> None:
        self.question = question
        self.topic_id = topic_id
        self.rubric: list[Any] = list(rubric or [])
        self.subject: dict[str, Any] = {}
        self.validated_target = True
        self.difficulty = 3


def _build_rubric(q: dict[str, Any], topic_id: str) -> list[Any]:
    """Assemble the reference SQL / dimensions / entities attached to a generated question into a Voyager-executable rubric.

    Key point: answer_spec.sql is the "server-side standard-answer recipe" — the referee (VLML0) executes it
    to compute the true value, never relying on model-given numbers (satisfying the study companion's server-held expected answer contract).
    """
    sql = str(q.get("reference_sql") or "").strip()
    if not sql:
        return []
    dim = str(q.get("dimension") or "value").strip() or "value"
    subj = q.get("subject") if isinstance(q.get("subject"), dict) else {}
    try:
        vc = int(q.get("value_column") or 0)
    except (TypeError, ValueError):
        vc = 0
    try:
        tol = float(q.get("numeric_tolerance") or 0.5)
    except (TypeError, ValueError):
        tol = 0.5
    try:
        return [RubricPoint(
            point=str(q.get("question") or topic_id)[:60],
            subject=dict(subj),
            dimension=dim,
            weight=100,
            min_base=1,
            answer_spec=AnswerSpec(sql=sql, value_column=vc, numeric_tolerance=tol),
        )]
    except Exception:
        return []


def _resolve_scope() -> dict[str, Any] | None:
    """Practice scope (dashboard practice_scope) → curriculum scope, handed to select_next for filtering.

    Design: switching questions / changing scope only decides "in which sub-curriculum to advance in order", and does not change the loop rhythm
    (only stopping can change the rhythm); a wrong scope will not crash the loop either.
    """
    try:
        import practice_scope
        sc = practice_scope.get_scope()
        if isinstance(sc, dict) and sc:
            return sc
    except Exception:
        pass
    return None


async def _generate_runtime_task() -> Any | None:
    """Design: when there is no "winnable" task in scope, keep generating new tasks to continue (the round does not end).

    Use question_gen's deterministic question-validation entry to generate a new question **with a true-value recipe**, convert it to a Task
    and answer directly; also register a runtime node in the in-memory graph, so that node's "completed/skills" can be tracked.
    (Runtime nodes are not persisted to the seed file — a restart clears current library data, consistent with the design's "restart convergence".)
    """
    try:
        import question_gen
        from topic_graph import TopicGraph
        rec, _ = await question_gen.generate_adopt(
            difficulty=3, tries=3, do_adopt=True, verbose=False)
        if not rec:
            return None
        task = question_gen.to_task(rec)
        task.validated_target = True
        tid = str(task.topic_id or "")
        if tid:
            graph = TopicGraph()
            if tid not in graph.topics:
                graph.topics[tid] = {
                    "id": tid, "name": str(rec.get("question") or tid),
                    "stage": "runtime", "subject": "valorant",
                    "chapter": "runtime", "course_family": "vlml0",
                    "depth": 5, "difficulty": 3,
                    "prerequisites": [], "related": [], "skills": [],
                    "question_types": [], "examples": [],
                    "typical_misconceptions": [], "curriculum_version": 1,
                    "exam_region": "", "exam_type": "",
                }
            try:
                scene_text = scene_context.scene_injection(tid)
                if scene_text:
                    task.question = str(task.question or "") + "\n\n" + scene_text
            except Exception:
                pass
        return task
    except Exception as e:
        print(f"  (Failed to generate new task: {type(e).__name__}: {e})")
        return None


async def run_curriculum(rounds: int, topic_id: str = "", *,
                         blind: bool = False, use_llm: bool = True,
                         help_policy: str = "auto") -> None:
    import open_quiz
    import scene_context
    from topic_graph import TopicGraph

    graph = TopicGraph()
    voyager: Any = (LLMVoyager(blind=blind, hint_level="")
                   if use_llm else ScriptedVoyager())

    stopped = {"flag": False}

    def _on_sigterm(_signum, _frame):
        stopped["flag"] = True
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except Exception:
        pass

    def _should_stop() -> bool:
        return stopped["flag"] or _pilot_stop_requested()

    print("=" * 72)
    print("  MVE: Voyager isomorphic learning loop (topic_graph curriculum + VLML0 objective referee)")
    print("=" * 72)
    print("  Feedback mechanism: Voyager really executes SQL to retrieve data -> compared objectively with the standard answer independently computed by the VLML0 referee")
    print("  -> task done/not done. Semantic scoring is no longer used (it only teaches it to please the grader).")
    print()
    seen = 0
    scope = _resolve_scope()
    while not _should_stop():
        sel = graph.select_next(explicit=topic_id or None, scope=scope)
        if sel.get("exhausted"):
            # Design: no "winnable" task in scope → keep generating new tasks to continue (the round does not end).
            print(f"\n----- No \"winnable\" task in scope  -  attempt {seen + 1} -----")
            print(f"  {sel.get('explanation')}")
            rt = await _generate_runtime_task()
            if rt is None:
                _stop_and_wait(
                    why="The curriculum has no winnable task left in scope, and new tasks cannot be generated"
                         "(check the LLM key / database connection / question-validation pipeline). Please fix and restart.")
                stopped["flag"] = True
                break
            task = rt
            tid = task.topic_id
            question = task.question
            print(f"\n----- Generated new task {tid}  -  attempt {seen + 1} -----")
            print(f"  Question: {question}")
        else:
            tid = sel["topic_id"]
            mrow = graph.mastery.get(tid) or {}
            attempt = int(mrow.get("attempts", 0))
            # —— Question generation: topic_graph selects a topic → open_quiz immediately generates a concrete question ——
            # generate_validated first validates the reference SQL against the real database; if it does not run, the specific SQL error
            # is fed back to the model for a retry (at most 3 times), guaranteeing the objective referee always gets the true value.
            q, sql_err = open_quiz.generate_validated(tid, attempt=attempt,
                                                      memories=True, scene=True)
            if sql_err is not None:
                print(f"  [WARN] {tid} reference SQL still not executable after retries ({sql_err}) -> eliminate and switch questions")
                graph.mastery.update(tid, score=0.0)
                seen += 1
                continue
            # A generated question must carry "server-executable reference SQL" to have an objective true value (VLML runs it once to judge success)
            rubric = _build_rubric(q, tid)
            if not rubric:
                print(f"  [WARN] {tid} generated question has no reference SQL (cannot score objectively) -> eliminate and switch questions")
                graph.mastery.update(tid, score=0.0)
                seen += 1
                continue

            question = q.get("question") or ""
            # —— Spawn-point metadata (the graph's isomorphic counterpart) injected into the agent's question text ——
            scene_text = ""
            try:
                scene_text = scene_context.scene_injection(tid)
            except Exception:
                scene_text = ""
            full_q = question + ("\n\n" + scene_text if scene_text else "")
            task = _GenTask(full_q, tid, rubric=rubric)
            print(f"\n----- Selected topic {tid} ({sel.get('reason')})  -  attempt {seen + 1} -----")
            print(f"  Question: {question}")

        # —— Objective truth: the VLML0 referee executes the reference SQL to compute the standard answer (produced independently server-side) ——
        ref_task = asyncio.create_task(vlml0_referee.answer(task))

        # —— rollout: max rounds attempts per topic (Voyager's rhythm, left untouched) ——
        done = False
        last_ev = None
        no_ev_rounds = 0            # Design ⑫: this question's consecutive "no evidence" rounds
        for r in range(1, rounds + 1):
            if _should_stop():
                break
            result = await voyager.run(task)
            facts = result.get("facts") or []
            tool_calls = sum(1 for t in voyager.trajectory
                            if not str(t.get("error") or ""))
            ref = await ref_task
            ev = evaluate_vs_referee(facts, task.rubric, ref.facts, tool_calls=tool_calls)
            last_ev = ev
            # No-evidence determination (design ⑫): not a single tool call succeeded, or judged dont_know with zero coverage.
            # This is "pipeline not working", different in nature from "not learned" (calls made but values wrong).
            _no_ev = bool(getattr(ev, "no_tool_calls", False)) or (
                str(getattr(ev, "verdict", "")) == "dont_know"
                and not getattr(ev, "covered_points", []))
            if _no_ev:
                no_ev_rounds += 1
            # —— Objective feedback: task completion (not a semantic score). What is fed back to Voyager is what it **actually
            #    did**: which rubric points were covered / which were missed / whether tools were reached — this is exactly
            #    "what Voyager did in the environment" + interaction with the graph, not pleasing the grader.
            verdict_label = {"correct": "Done", "partial": "Partially achieved",
                             "wrong": "Not achieved", "dont_know": "No evidence"}.get(
                                 ev.verdict, ev.verdict)
            print(f"  Round {r} [{verdict_label}] coverage={ev.coverage:.0%}"
                  f"  -  covered={ev.covered_points}  -  missing={ev.missing_points[:3]}")
            graph.mastery.update(tid, score=float(ev.coverage))
            try:
                voyager.learn(
                    covered=[str(c) for c in ev.covered_points],
                    missing=[str(m) for m in ev.missing_points],
                    task=task, verdict=ev.verdict)
            except Exception:
                pass
            if ev.success:
                done = True
                # Design: task ran through → mark the graph's current node complete (no re-planning).
                # source="practice" = dedup bit, not mastery; mastery is only recorded on the consumption line.
                graph.mastery.mark_completed(tid, source="practice")
                name = _store_skill_for(voyager, tid, task, result,
                                        float(ev.coverage), question)
                # Design: the skill library and the graph are the same storage — after a skill is persisted, hang its pointer on the graph node,
                # each node accumulating skills (voyager unlocks milestones by graph node and stacks skills on nodes).
                if name:
                    try:
                        graph.mastery.add_skill(tid, name)
                    except Exception:
                        pass
                print(f"  [OK] Objectively done (VLML0 verification passed) -> marking node complete + storing skill"
                      + (f": {name}" if name else "(no program to solidify)"))
                break
        if not done:
            if no_ev_rounds >= rounds:
                # Design ⑫: all rounds of this question had no evidence → the evidence path is broken; stop and wait for human intervention.
                # Clearly distinguished from "not learned" (tool calls made but values mismatched), to avoid spinning on the same failure.
                try:
                    graph.mastery.add_failure(
                        tid, "no_evidence",
                        f"Pipeline not working: {rounds} consecutive rounds with no tool calls / empty dataset")
                except Exception:
                    pass
                _stop_and_wait(
                    why=f"Pipeline not working: {rounds} consecutive rounds with no tool calls / empty dataset"
                        f" (not \"not learned\"). Check the data source connection, whether entity values are in the database, "
                        f"whether all 10 tools are reachable, then clear pilot_state.stop_requested and restart.")
                stopped["flag"] = True
                break
            cov = getattr(last_ev, "coverage", 0.0) if last_ev else 0.0
            # Design ⑨: give up (retries exhausted and still objectively not achieved) → record a skip (with reason) + failure,
            # into context so observers can tell "not learned" vs "pipeline not working". Next round the give-up mechanism skips to another question.
            try:
                verdict = getattr(last_ev, "verdict", "wrong") or "wrong"
                graph.mastery.add_failure(
                    tid, verdict,
                    f"Objectively not achieved (coverage={cov:.0%}), marked as given up")
                graph.mastery.add_skip(
                    tid, f"Retries exhausted and still objectively not achieved (coverage={cov:.0%}) → give up and switch questions")
            except Exception:
                pass
            print(f"  [WARN] Retries exhausted and still objectively not achieved (coverage={cov:.0%}) -> "
                  f"marked stale; next round the give-up mechanism skips to another question")
        seen += 1
        _log_curriculum(tid, question, last_ev, done, use_llm, voyager)
    print(f"\n===== Cockpit Voyager loop finished  -  {seen} topic selections processed =====")


def _log_curriculum(topic_id: str, question: str, ev: Any, done: bool,
                   use_llm: bool, voyager: Any) -> None:
    """Append one run_log line, with fields aligned to what panel_data reads (topic_id/round/coverage/
    score/verdict/success), so the progress curve / exported md do not break.
    ev = the Evaluation from evaluate_vs_referee (objective); None means this question was eliminated and switched."""
    verdict = getattr(ev, "verdict", "dont_know") if ev else "dont_know"
    cov = float(getattr(ev, "coverage", 0.0) or 0.0) if ev else 0.0
    success = bool(getattr(ev, "success", False)) if ev else False
    evidence = getattr(ev, "evidence_status", "none") if ev else "none"
    no_tool = bool(getattr(ev, "no_tool_calls", False)) if ev else False
    unjudgeable = getattr(ev, "unjudgeable", []) if ev else []
    covered = getattr(ev, "covered_points", []) if ev else []
    missing = getattr(ev, "missing_points", []) if ev else []
    rejected = getattr(ev, "rejected_low_base", []) if ev else []
    try:
        run_log.append({
            "mode": "curriculum",
            "topic_id": topic_id,
            "task": topic_id,
            "question": question,
            "round": 1,
            "hint": "",
            "coverage": round(cov, 4),
            "score": round(cov, 4),
            "verdict": verdict,
            "success": success,
            "done": bool(done),
            "evidence_status": evidence,
            "judge": "vlml0_referee",
            "no_tool_calls": no_tool,
            "feedback": {},
            "used_hint": False,
            "help_wasted": False,
            "help_decision": None,
            "ref_plan": [],
            "difficulty": 3,
            "tool_calls": len(getattr(voyager, "trajectory", []) or []),
            "evaluator_confidence": round(0.6 + 0.35 * 1.0, 4),
            "errors": [],
            "unjudgeable": unjudgeable,
            "referee_facts": len(covered),
            "covered": covered,
            "missing": missing,
            "rejected_low_base": rejected,
            "narrative": "",
            "insights": [],
            "caveats": [],
            "narrative_comparable": False,
            "narrative_based_on": 0,
            "facts_count": len(getattr(voyager, "trajectory", []) or []),
            "trajectory": [t.get("tool") for t in getattr(voyager, "trajectory", []) or []],
            "skill_count": len(voyager.memory) if use_llm else len(voyager.skills),
            "skill_stats": skill_store.stats() if use_llm else None,
            "hallucinations": [],
            "hallucination_count": 0,
            "mastery": None, "level": None, "status": None, "flags": [],
            "mastery_recency": None, "mastery_confidence": None,
            "mastery_evidence_count": 0, "mastery_model": "topic_graph",
        })
    except Exception:
        pass


async def main() -> None:
    argv = sys.argv[1:]

    # Wipe: skill library + run log together. Clearing only one leaves mismatched curves.
    if "--reset" in argv:
        skill_store.reset()
        run_log.reset()
        vlml0_referee.reset()
        return

    if "--next" in argv:
        b = planner.next_brief()
        print(f"Next question: {b['question']}")
        print(f"  topic_id  : {b['topic_id']}")
        print(f"  Recommendation reason  : [{b['reason_label']}] {b['explanation']}")
        print(f"  Difficulty      : question {b['difficulty']} -> target {b['difficulty_target']}"
              f"({b['difficulty_why']})")
        print(f"  Scaffold level  : {b['hint']} - {b['hint_label']}")
        print(f"  Usage      : {b['cmd']}")
        return

    use_llm = "--llm" in argv
    blind = "--blind" in argv

    # Network-genius style (--review): give the original question but not the rubric points; Voyager decomposes itself,
    # verifies indirectly through tools, and approximates step by step, with the whole approximation process transparent (questions/reasoning/conclusion
    # all shown to the human). The criterion still follows Voyager's ruler (done: ran + retrieved data + no crash).
    review = "--review" in argv

    # Help policy: after a wrong answer, Voyager itself decides whether to check the explanation via the bypass (default auto).
    #   auto   — decided by the model (try untried tools on its own first)
    #   always — give the explanation on every wrong answer (for comparison: shows how much mastery is discounted)
    #   never  — never ask for help (pure self-study baseline)
    help_policy = "auto"
    for pol in ("auto", "always", "never"):
        if f"--help-policy={pol}" in argv:
            help_policy = pol

    # Scaffold-level override (by default the question generator computes it from mastery; see difficulty.py).
    # Provided so A/B is possible: run the same question once each at full / partial / none,
    # and if the scaffold truly works, coverage should drop with the level — if it does not drop, the level is decoration.
    hint_override = ""
    for lv in (difficulty.HINT_FULL, difficulty.HINT_PARTIAL, difficulty.HINT_NONE):
        if f"--hint={lv}" in argv:
            hint_override = lv

    # Round cap. Note this is only a **cap**: break on a correct answer (mirroring Voyager rollout's done=success),
    # so it never runs the full count by force. The dashboard/cockpit uses it to control how long one run lasts.
    rounds = 3
    if "--rounds" in argv:
        try:
            rounds = max(1, min(10, int(argv[argv.index("--rounds") + 1])))
        except (ValueError, IndexError):
            rounds = 3

    # ---- Voyager isomorphic learning loop (the cockpit's "▶ Start Voyager" new path, taking priority over the old single-question path) ----
    # Question generation = topic_graph auto curriculum; scoring = study-companion-style semantic scoring; advancement decided by done.
    # The old single-question path (planner + vlml0_referee) is kept for CLI comparison (without this switch).
    if "--curriculum" in argv:
        _ctopic = ""
        if "--topic" in argv:
            _ctopic = argv[argv.index("--topic") + 1]
        await run_curriculum(
            rounds, _ctopic,
            blind=blind, use_llm=use_llm, help_policy=help_policy)
        return

    # Topic selection: mirroring the study companion's practice_scope two modes
    #   --topic <id>  → explicit_topic: pin one question for consecutive exams
    #   not given     → explicit_scope: the question generator self-selects by the priority chain
    topic_id = None
    if "--topic" in argv:
        topic_id = argv[argv.index("--topic") + 1]
        if topic_id not in TASKS:
            print(f"Unknown question {topic_id}. Options: {', '.join(TASKS)}")
            return
    elif "--hard" in argv:
        topic_id = TASK_HARD.topic_id
    elif "--mid" in argv:
        topic_id = TASK_MID.topic_id
    elif "--pattern" in argv:
        topic_id = TASK_PATTERN.topic_id

    # ---- Adaptive question generation ----
    # Mirroring the study companion's planner.select_practice_selection priority chain:
    #   wrong_retry > due_review > weak_topic(human import) > blocked_diagnostic > recommended > default
    # Each carries reason + explanation. The previous version had ordering only, without reasons, which does not count as adaptive.
    selection = planner.select_next(explicit_topic_id=topic_id or "")

    # ---- No advanceable question → generate a new one (the study companion's weak_topic → generate_question) ----
    # The division of labor mirrors the study companion: planner only says clearly "what to practice", **generating the question text is the entry layer's job**
    # (entry_tutor_question_entries.py:1289 _generate_question_payload_impl).
    # The study companion uses action=generate_question when selection_reason != due_review,
    # and MVE fills in this link — otherwise "no advanceable question" just stalls (this is exactly the exit for the earlier
    # "stuck at 50% for 10 consecutive times" symptom).
    if "--no-gen" not in argv and selection.get("action") == "generate_question":
        sug = selection.get("suggestion") or {}
        if sug:
            print(f"  [WARN] {selection['explanation']}")
            print(f"  -> Following the study companion's weak_topic, trying to generate a new question: {sug.get('why', '')}")
            try:
                import question_gen
                rec, _ = await question_gen.generate_adopt(
                    about=str(sug.get("about") or ""),
                    difficulty=int(sug.get("difficulty") or 2),
                    focus_dimension=str(sug.get("focus_dimension") or ""),
                    tries=3, do_adopt=True, verbose=True)
            except Exception as exc:                         # pragma: no cover
                print(f"  [WARN] Automatic question generation failed (continuing with existing questions): {type(exc).__name__}: {exc}")
                rec = None
            if rec:
                new_id = str(rec.get("topic_id") or "")
                # Merge in place — planner reads this same dict
                TASKS[new_id] = question_gen.to_task(rec)
                print(f"  [OK] New question admitted: {new_id}")
                selection = planner.select_next(explicit_topic_id=new_id)
            else:
                print("  [WARN] No valid question generated in three tries - continuing with existing questions (an honest result, "
                      "not a bug)")

    task = TASKS[selection["topic_id"]]

    print(f"  Question generator    : {task.topic_id}")
    print(f"  Recommendation reason  : [{planner.REASON_LABEL.get(selection['reason'], selection['reason'])}] "
          f"{selection['explanation']}")
    print(f"  Difficulty gradient  : question {task.difficulty} -> target "
          f"{selection.get('difficulty_target')} ({selection.get('difficulty_why','')})")
    print(f"  Scaffold level  : {selection.get('hint')} - {selection.get('hint_label','')}")
    if hint_override:
        print(f"  [WARN] Scaffold override  : the question generator computed {selection.get('hint')}, "
              f"forcing {hint_override} this round (for A/B)")
        selection["hint"] = hint_override
        selection["hint_label"] = difficulty.HINT_LABEL[hint_override]
    if selection["blocked"]:
        print("  [WARN] No evidence obtainable - per the study companion's rule, do not generate a new question now; repeat this one until evidence is obtained.")
    print()

    voyager: Any = LLMVoyager(blind=blind,
                              hint_level=str(selection.get("hint") or "")) if use_llm \
        else ScriptedVoyager()
    mode = ("LLM+BLIND" if blind else "LLM") if use_llm else "SCRIPTED"
    # The mastery sequence continues across processes: restore this question's historical **evidence** from run_log.
    # Without restoring it, "run one question and stop" is a new process every time, so V2 sees only the current round,
    # and the weight sum stays at a single entry — both weighting and time decay lose their meaning.
    history: list[float] = run_log.recent_scores(task.topic_id)
    evidence: list[mastery_model.MasteryEvidence] = run_log.evidence_rows(task.topic_id)
    snapshots = []
    coverages: list[float] = []
    successes: list[bool] = []   # VLML observation: whether each round achieved all rubric points (exam ruler)
    dones: list[bool] = []       # Criterion layer: whether each round's task was done (Voyager's ruler, coverage points not counted)

    print("=" * 72)
    print(f"  MVE: Voyager answering questions on VLML   [{mode}]  referee=VLML0 (original)")
    print("=" * 72)
    print(f"\nQuestion: {task.question}")
    if review:
        print("Rubric points: hidden (network-genius style - give the original question, not the answer points; Voyager approximates itself)")
    else:
        print(f"Rubric points: {[p.point for p in task.rubric]}")
    print(f"validated_target = {task.validated_target} (True -> counted in mastery)")

    # ---- The referee starts asynchronously: overlapping with Voyager's retrieval, non-blocking ----
    # The standard answer must come from the original VLML (with no Voyager participation), not hand-written by the question author,
    # and not a model-generated reference answer (study companion tutor_llm_agent_answer_evaluate.py:82-84).
    ref_task = asyncio.create_task(vlml0_referee.answer(task))
    print("Referee VLML0 has started in the background...\n")

    for round_no in range(1, rounds + 1):
        print("-" * 72)
        print(f"Round {round_no}")
        print("-" * 72)

        # Whether this round's answer carries the requested explanation — must be taken **before** run():
        # the help request happens at the end of the previous round, and this round is "redo with the explanation", so this round counts as helped.
        used_hint = bool(getattr(voyager, "used_hint", False))
        if used_hint:
            print("  [INFO] Redoing this round with the requested explanation -> mastery counted as \"with help\" (0.85 factor, "
                  "and a correct answer does not lengthen the half-life either)")

        result = await voyager.run_review(task) if review else await voyager.run(task)
        facts = result["facts"]

        if review:
            # —— The approximation process is fully transparent (network-genius style): every indirect question + reasoning + synthesis, shown to the human ——
            print(f"  [Approach] Decomposed question topics: {result.get('review_topics')}")
            for i, q in enumerate(result.get("questions_asked") or [], 1):
                print(f"  [Approach #{i}] Why asked: {q['intent']}")
                print(f"            Asked with: {q['tool']}({q.get('args')})")
                print(f"            What it returned: {q.get('result')}")
            for line in result.get("reasoning") or []:
                print(f"  [Reasoning] {line}")
            print(f"  [Synthesis] {result.get('conclusion', '').splitlines()[0] if result.get('conclusion') else '(none)'}")
            for cl in (result.get('conclusion') or '').splitlines()[1:]:
                print(f"         {cl}")

        # The referee only needs to be ready by here (the retrieval time of the earlier rounds was borrowed for it)
        ref = await ref_task
        if not hasattr(main, "_ref_shown"):
            main._ref_shown = True
            print(f"[Referee VLML0] standard answer {len(ref.facts)} facts, trajectory {ref.trajectory}")
            for f in ref.facts:
                subj = "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items())
                print(f"              {subj}  -  {f['dimension']} = {f['value']} (base={f.get('base')})")
            print()

        # Count only tool calls that "actually succeeded"; failures do not count as evidence
        tool_calls = sum(1 for t in voyager.trajectory if not str(t.get("error") or ""))
        ev = evaluate_vs_referee(facts, task.rubric, ref.facts, tool_calls=tool_calls)
        # Voyager's ruler: task done = orchestration ran + data retrieved + no crash (**coverage points not counted**)
        done = task_done(facts, tool_calls=tool_calls, errors=result.get("errors"))

        for e in (result.get("errors") or []):
            print(f"  [WARN] Tool error  : {e[:150]}")
        for h in (result.get("hallucinations") or []):
            print(f"  [STOP] Hallucination blocked  : {h[:150]}")
        print(f"  Trajectory      : {[t['tool'] for t in voyager.trajectory]}")
        if use_llm:
            print(f"  Thought      : {result.get('thought', '')[:90]}")
            st = skill_store.stats()
            print(f"  Skill library    : {st['skills']} entries ({st['writes']} writes -> "
                  f"overwritten {st['rewrites']}  -  discarded {st['skipped']}  -  bloat rate {st['bloat']})"
                  f"  injected this round {len(getattr(voyager, '_injected_keys', []))}/{st['top_k']} entries")
            for ev_ in getattr(voyager, "skill_events", [])[-2:]:
                print(f"                -  Last round's write: {ev_['label']} {ev_['key']}")
            for s in skill_store.all_skills():
                print(f"                -  {s['text'][:100]}")
        else:
            print(f"  Skill      : {result['skill_used']}   "
                  f"skill library={[s.name for s in voyager.skills]}")
        print(f"  Fact count    : {ev.facts_count}   evidence={ev.evidence_status}")
        if result.get("reused_skill"):
            # The learned program was **run**, not read — this is the observable form of "learned".
            print(f"  Reused skill  : {result['reused_skill']}"
                  f"(executed the program from the skill library directly; no code regenerated this round)")
        print(f"  Covered    : {ev.covered_points}")
        print(f"  Missing      : {ev.missing_points}")
        if ev.rejected_low_base:
            print(f"  Base gate : {ev.rejected_low_base}")
        if ev.unjudgeable:
            print(f"  Referee missing answer  : {ev.unjudgeable} (not counted as Voyager's mistake)")
        print(f"  verdict   : {ev.verdict}   score={ev.score}   coverage={ev.coverage:.0%}"
              f"   judge={ev.judge}")

        snap = None
        if ev.evidence_status == "none":
            print("  -> No evidence obtained this round: not counted in the mastery sequence")
        else:
            history.append({"correct": 1.0, "partial": 0.6}.get(ev.verdict, 0.0))
            # This round's evidence: **append first**, then project — V2 must see the full sequence.
            # evaluator_confidence takes the deterministic share of referee facts — the study companion only has "default 0.75",
            # while MVE's referee facts have explicit sources (deterministic / tool path / llm),
            # giving 0.95 for fully deterministic and 0.6 for fully LLM.
            det_ratio = _deterministic_ratio(ref.facts)
            evidence.append(mastery_model.MasteryEvidence(
                attempt_id=f"{datetime.now().isoformat(timespec='seconds')}#{round_no}",
                verdict=ev.verdict,
                score=ev.score,
                difficulty=task.difficulty,
                # used_hint = **whether this round's answer used an explanation** (VLML0 help).
                # Previously it always passed None, so the hint discount never took effect (`hint_unknown_modifier=1.0`);
                # and the retention side maps it to "skill-library lessons were injected" — both are wrong conventions.
                # The study companion's isomorphic item is explicit: used_hint is "did this correct answer rely on help"
                # (mastery_v2.py:109 + :391-396); injecting skills is a normal part of answering,
                # and only **asking for help** counts as helped.
                used_hint=used_hint,
                response_time_ms=None,
                evaluator_confidence=0.6 + 0.35 * det_ratio,
                submitted_at=datetime.now().isoformat(timespec="seconds"),
                tool_calls=tool_calls,
            ))
            # Mirroring the study companion's mastery_v2.py:283-285: undigested wrong answers cap mastery.
            # Without passing this, mastery rises monotonically with evidence weight via the confidence term — a false positive.
            unresolved = (0 if ev.verdict == "correct"
                          else run_log.unresolved_wrongs(task.topic_id) + 1)
            snap = update_mastery(
                task.topic_id, evidence,
                unresolved_wrong_count=unresolved,
            )
            snapshots.append(snap)

            # Retention (forgetting): study companion mastery_retention.py:46-51 —
            #   correct and **not helped** → half-life lengthened (solid learning)
            #   wrong/dont_know       → half-life shortened
            #   others (including correct but helped) → half-life **unchanged**
            # That is, "correct with help": the score takes a 0.85 factor, but forgetting is not accelerated — not a punishment, just not full credit.
            try:
                retention.STORE.apply_attempt(
                    task.topic_id, verdict=ev.verdict, scores=history,
                    confidence=float(getattr(snap, "confidence", 0.0) or 0.0),
                    used_hint=used_hint,
                )
            except Exception as e:
                print(f"  (Retention not updated: {type(e).__name__}: {e})")

            print(f"  mastery   : {snap.mastery:.3f}  {snap.level}  "
                  f"status={snap.status}  flags={snap.flags or '-'}"
                  + (f"  (undigested wrong answers {unresolved} -> capped at 0.79)" if unresolved else ""))
            print(f"  Evidence weight  : {len(evidence)} entries  -  referee determinism {det_ratio:.0%}"
                  f"  -  tool calls this round {tool_calls}"
                  f"  -  recency={getattr(snap, 'recency', 0):.3f}")

        # ---- Narrative insights: run only when "there are facts recognized by the referee" ----
        # The order must not be reversed: compare first, narrate after. Otherwise it wraps mis-transcribed numbers into fluent conclusions.
        covered_keys = {
            fact_key({"subject": p.subject, "dimension": p.dimension})
            for p in task.rubric if p.point in ev.covered_points
        }
        verified = [f for f in facts if fact_key(f) in covered_keys]
        story = narrator.narrate(task, verified, verdict=ev.verdict)
        if story.get("narrative"):
            print(f"  Narrative insight  : {story['narrative'][:110]}")
            for ins in story.get("insights") or []:
                print(f"                -  {ins[:100]}")
        elif story.get("skipped"):
            print(f"  Narrative insight  : skipped - {story['skipped']}")

        # ---- Both the referee verdict and the narrative enter the causal timeline ----
        # What the question generator reads is this line (causal) + mastery (scoring); human-imported questions enter only the former.
        causal_timeline.append(
            causal_timeline.REFEREE,
            topic_id=task.topic_id,
            summary=f"Referee verdict {ev.verdict}, coverage {ev.coverage:.0%}",
            detail={"covered": ev.covered_points, "missing": ev.missing_points,
                    "no_tool_calls": ev.no_tool_calls},
        )
        if story.get("narrative"):
            causal_timeline.append(
                causal_timeline.NARRATIVE,
                topic_id=task.topic_id,
                summary=f"Produced narrative, based on {story.get('based_on', 0)} verified facts",
                detail={"insights": story.get("insights") or []},
            )

        # Voyager's actions also enter the timeline — the question generator must see "what it did"
        #
        # The **raw SQL** must be stored too: previously only the tool name and verdict were stored,
        # so "what exactly did it write wrong" could not be reviewed after the fact. When diagnosing "the graph gave the skeleton, why did it not write accordingly",
        # what was missing is this line — without the SQL, one can only guess whether it did not read it or read it but found it useless.
        causal_timeline.append(
            causal_timeline.ATTEMPT,
            topic_id=task.topic_id,
            summary="Voyager orchestration: "
                    + (" → ".join(t["tool"] for t in voyager.trajectory) or "(not called)"),
            detail={"facts": ev.facts_count, "verdict": ev.verdict,
                    "errors": (result.get("errors") or [])[:2],
                    "sqls": [
                        str((a.get("args") or {}).get("sql_query")
                            or (a.get("args") or {}).get("sql") or "")[:400]
                        for a in (getattr(voyager, "attempted", None) or [])
                        if str(a.get("tool", "")) in ("query_sql", "execute_custom_sql")
                    ][:4],
                    # Store the **arguments** too: with only tool names, failures like "it called the right tool but passed no arguments"
                    # are invisible entirely (observed pistol_eco_pattern failing exactly here).
                    "calls": [
                        {"tool": str(t.get("tool") or ""),
                         "args": {k: str(v)[:60] for k, v in
                                  (t.get("args") or {}).items()}}
                        for t in (getattr(voyager, "trajectory", None) or [])
                    ][:4]},
        )

        # Feedback must be computed **before** persistence — both the dashboard and offline recomputation read this package,
        # and not persisting it means "feedback only ever existed in-process", making "what exactly was said at the time" unreviewable after the fact.
        det_ratio = _deterministic_ratio(ref.facts)
        critique = voyager.learn(
            covered=ev.covered_points, missing=ev.missing_points,
            task=task, referee=ref, verdict=ev.verdict,
            rejected_low_base=list(ev.rejected_low_base),
            confidence=round(0.6 + 0.35 * det_ratio, 4),
        ) or ""     # The scripted baseline's learn produces no critique (returns None); caught here
        print(f"  critique  : {critique[:110]}" if critique else "  critique  : (scripted baseline, none)")
        fb = getattr(voyager, "last_feedback", None) or {}
        if fb.get("error_type"):
            print(f"  Error type  : {fb['error_type']}")
        # Learned but did not gather evidence: watched the explanation last round, and not a single tool call succeeded this round — the help was wasted.
        # Observed corrode_collapse showing consecutive "orchestration (not called), fact count 0" after asking for help,
        # which is worse than not asking. Call it out on the spot, otherwise people only see yet another 0%.
        if used_hint and ev.no_tool_calls:
            print("  [WARN] Help ineffective: learned the standard solution last round, yet not a single tool call succeeded this round"
                  " - this help does not count toward progress (recorded in help_wasted)")
        if fb.get("next_action"):
            print(f"  Next step    : {fb['next_action'][:150]}")

        # ---- Learning ①: ran it myself → store the program **I wrote** myself ----
        # This is the body of the original's `add_new_skill`:
        #    original: write JS myself → run it → store program_code → run directly next time
        # Previously MVE only stored VLML0's solution (observed), and never stored the part it ran itself
        # — so "it ran" contributed nothing to the skill library.
        # The criterion follows the original: task done (Voyager's ruler = orchestration ran + data retrieved + no crash,
        # **coverage points not counted** — coverage points are VLML's ruler, used only for exams and mastery evidence)
        # is required to store; storing something that did not run solidifies the wrong approach.
        try:
            prog = str(result.get("program_code") or "")
            if prog and done:
                _saved = skill_store.add(
                    task.topic_id,
                    _stable_skill_name(task),
                    f"Self-run orchestration (coverage {ev.coverage:.0%}): "
                    f"{result.get('program_name') or ''}",
                    code=prog,
                    # The main function name must be stored too: reuse needs `execute(code, name)` to
                    # find the entry point. Storing only the source without the function name means the program cannot run once retrieved.
                    blueprint={"source": "practice",
                               "coverage": ev.coverage,
                               "program_name": result.get("program_name") or ""},
                    source="practice")
                print(f"  Learned skill  : {_saved[0]} "
                      f"(self-written program, {len(prog)} characters, stored)")
            elif done:
                # ---- plan path: no program_code, but the **call sequence that ran is its program** ----
                #
                # 6 of the 8 questions are SQL-type, taking the plan path, which never produces code —
                # so for those 60% of questions "it ran" contributed nothing to the skill library: when re-examining without the graph, one could only
                # rewrite the SQL from scratch, then make the same metric mistake again (observed corrode_collapse
                # practicing to 100% but still 0% on re-exam, missing the same losing_team_name at the wrong layer).
                # The original's criterion is **whether it ran**, not what this thing looks like.
                calls = _successful_calls(voyager)
                if calls:
                    code, fn = _plan_program(str(task.topic_id), calls)
                    _saved = skill_store.add(
                        task.topic_id,
                        _stable_skill_name(task),
                        f"Self-run orchestration (coverage {ev.coverage:.0%}): "
                        f"{' → '.join(str(c['tool']) for c in calls)}",
                        code=code,
                        blueprint={"source": "practice",
                                   "coverage": ev.coverage,
                                   "program_name": fn,
                                   "calls": calls},
                        source="practice")
                    print(f"  Learned skill  : {_saved[0]} "
                          f"(successful call sequence, {len(calls)} steps, stored and replayable)")
        except Exception as e:                # pragma: no cover
            print(f"  (Failed to store program: {type(e).__name__}: {e})")


        # ---- Help-request loop: after a wrong answer, Voyager itself decides whether to check the explanation via the bypass ----
        # The order matters: first try once on its own with the tool names the graph suggests, and ask for help only when truly stuck.
        # Each help request must be noted in mastery (used_hint → 0.85 factor),
        # so the explanation must not be given on every wrong answer, or mastery would be entirely "with help".
        if fb.get("error_type") and round_no < rounds and help_policy != "never":
            decision = voyager.decide_help(task=task, fb=fb)
            print(f"  Help decision  : {'go to the bypass for an explanation' if decision.get('need_help') else 'try on my own first'}"
                  f" - {decision.get('reason', '')[:70]}")
            if decision.get("need_help") or help_policy == "always":
                try:
                    pack = await bypass_learn.explain_for_retry(
                        task,
                        my_plan=[t["tool"] for t in voyager.trajectory],
                        missing=[m.get("dimension") or m.get("point")
                                 for m in (fb.get("missing_detail") or [])],
                    )
                    if pack.get("ok"):
                        voyager.accept_help(pack)
                        study = pack.get("study") or {}
                        print(f"  === Studying the standard solution (with answer, following the study companion's four-part explanation) ===")
                        for i, sec in enumerate(pack.get("sections") or
                                                ["Question analysis", "Solution process", "Answer", "Transfer"]):
                            key = ["analysis", "process", "answer", "transfer"][i]
                            body = str(study.get(key) or "").replace("\n", " ")
                            print(f"  {sec}: {body[:150]}")
                        print(f"  On redo, bring only the approach (orchestration {' -> '.join(pack.get('plan') or [])}); retrieve values yourself")

                        # ---- Learning ②: did not run through → observe VLML0's standard solution ----
                        # This is the isomorphic counterpart of the original's `add_new_skill`, and also the previous core:
                        #    original: write JS myself → run it → store program_code
                        #    MVE ：wrong answer (learning signal) → observe VLML0 → store its solution source
                        # Being wrong is itself a signal and needs no separate invention; and "what was learned" must land in
                        # the skill library, or it cannot be obtained next round — previously learn_from_referee
                        # was only run manually in the CLI and never called once in the main loop,
                        # so **what the bypass channel learned never entered the skill library**.
                        try:
                            learned = await bypass_learn.learn_from_referee(
                                task.topic_id)
                            if learned.get("ok"):
                                print(f"  Learned skill  : {learned.get('action')} "
                                      f"{learned.get('name')} "
                                      f"(solution source, {learned.get('code_len')} characters"
                                      f" -> enters the prompt next round)")
                            else:
                                print(f"  (Nothing learned: {learned.get('error')})")
                        except Exception as e:                  # pragma: no cover
                            print(f"  (Learning failed: {type(e).__name__}: {e})")
                    else:
                        print(f"  (Explanation not retrieved: {pack.get('error')})")
                except Exception as e:
                    print(f"  (Help request failed: {type(e).__name__}: {e})")

        # Append one log line: the dashboard's progress curve reads this
        run_log.append({
            "mode": mode,
            "topic_id": task.topic_id,
            "task": topic_id,
            "question": task.question,
            "round": round_no,
            # This round's scaffold level — level progression (at most ±1 per round) must be compared across processes
            # against "what level the last round used"; without persisting this, the constraint is empty talk (see planner._prev_hint).
            "hint": str(getattr(voyager, "hint_level", "") or ""),
            "coverage": round(ev.coverage, 4),
            "score": round(ev.score, 4),
            "verdict": ev.verdict,
            "success": bool(ev.success),   # VLML observation: all rubric points achieved (exam ruler)
            "done": bool(done),            # Criterion-layer boolean: task done (Voyager's ruler, coverage points not counted)
            "evidence_status": ev.evidence_status,
            "judge": ev.judge,
            "no_tool_calls": ev.no_tool_calls,
            # --- Structured feedback package (mirroring the study companion's answer_evaluate): criterion + next step + wrong hypothesis ---
            # Without it, one can only see "coverage 25%" after the fact, not "why, and what to do next".
            "feedback": fb,
            # Whether this round was "helped" (used the VLML0 explanation). Persisting it answers after the fact
            # "was this mastery earned on its own or with help" — the study companion's used_hint isomorphic item.
            "used_hint": bool(used_hint),
            # Learned but did not gather evidence: watched the explanation last round, and not a single tool call this round → this help was wasted.
            # Observed corrode_collapse's rounds 52/53 after asking for help being exactly this (orchestration "(not called)",
            # fact count 0), worse than not asking. Persisting it lets such rounds be picked out after the fact.
            "help_wasted": bool(used_hint and ev.no_tool_calls),
            "help_decision": getattr(voyager, "last_help_decision", None),
            # The referee's orchestration sequence: needed when recomputing feedback offline (otherwise "tools I never called" cannot be computed)
            "ref_plan": [str(t) for t in (getattr(ref, "trajectory", None) or [])
                         if not str(t).startswith("<")],
            # --- Three fields required by V2 mastery: without them, the next process cannot rebuild evidence ---
            "difficulty": task.difficulty,
            "tool_calls": tool_calls,
            "evaluator_confidence": round(0.6 + 0.35 * det_ratio, 4),
            "errors": (result.get("errors") or [])[:3],
            "unjudgeable": ev.unjudgeable,
            "referee_facts": len(ref.facts),
            "covered": ev.covered_points,
            "missing": ev.missing_points,
            "rejected_low_base": ev.rejected_low_base,
            # Narrative section: comparable always False, not part of comparison
            "narrative": story.get("narrative", ""),
            "insights": story.get("insights") or [],
            "caveats": story.get("caveats") or [],
            "narrative_comparable": story.get("comparable", False),
            "narrative_based_on": story.get("based_on", 0),
            "facts_count": ev.facts_count,
            "trajectory": [t["tool"] for t in voyager.trajectory],
            "skill_count": len(voyager.memory) if use_llm else len(voyager.skills),
            "skill_stats": skill_store.stats() if use_llm else None,
            "hallucinations": (result.get("hallucinations") or [])[:3],
            "hallucination_count": len(result.get("hallucinations") or []),
            "mastery": round(snap.mastery, 4) if snap else None,
            "level": snap.level if snap else None,
            "status": snap.status if snap else None,
            "flags": list(snap.flags) if snap else [],
            "mastery_recency": round(getattr(snap, "recency", 0.0), 4) if snap else None,
            "mastery_confidence": round(getattr(snap, "confidence", 0.0), 4) if snap else None,
            "mastery_evidence_count": getattr(snap, "evidence_count", 0) if snap else 0,
            "mastery_model": getattr(snap, "mastery_model_version", "") if snap else "",
        })

        coverages.append(ev.coverage)
        successes.append(ev.success)
        dones.append(done)
        print()

        # Mirroring Voyager rollout (voyager.py:269-273):
        #   done = (rollout_num_iter >= action_agent_task_max_retries or success)
        # **Stop once the task is done**. Previously it ran the full 3 rounds by force — doing it right and still redoing it, which is wasteful,
        # and lets the "failure lessons" of later rounds keep being written into the skill library.
        # The criterion uses done (Voyager's ruler: orchestration ran + data retrieved + no crash),
        # **not VLML coverage points** (coverage is only an exam ruler / mastery evidence).
        if done:
            print("  [OK] Task done (Voyager's ruler: ran + retrieved data + no crash)"
                  "- per Voyager rollout's `done = success`, this question is not redone.")
            break

    print("=" * 72)
    print("  Conclusion")
    print("=" * 72)
    # Criterion layer (Voyager): done = the task-done boolean (orchestration ran + data retrieved + no crash,
    # coverage points not counted); skill storage / stopping / advancing all recognize only it.
    # Mastery (study companion) cannot serve as a learning criterion: in V2, confidence rises with the sum of evidence weights
    # (weights are pulled down by time decay and evaluator confidence), but repeated answering still pushes it up,
    # and using it to judge "learned" yields false positives — mastery is only an independent assessment.
    print(f"  done trajectory  : {[str(d) for d in dones]}  <- criterion layer (Voyager's ruler)")
    print(f"  success   : {[str(s) for s in successes]}  <- VLML observation (all rubric points achieved)")
    # Coverage demoted to observation: a continuous reading of achievement, feeding mastery evidence, not entering right/wrong judgments
    print(f"  Coverage observation : {[f'{c:.0%}' for c in coverages]}  <- achievement observation only")
    if snapshots:
        print(f"  mastery trajectory: {[f'{s.mastery:.3f}' for s in snapshots]}"
              f"  (note: it rises naturally with evidence weight and cannot serve as learning evidence)")
        last = snapshots[-1]
        print(f"  Evidence/confidence   : {getattr(last, 'evidence_count', 0)} entries  -  "
              f"recency={getattr(last, 'recency', 0):.3f}  -  "
              f"confidence={getattr(last, 'confidence', 0):.3f}")
        try:
            cur = retention.STORE.current(task.topic_id)
            print(f"  Retention     : baseline {cur['baseline']:.3f}  -  half-life "
                  f"{cur['half_life_sessions']:.1f} sessions  -  {cur['retained']:.3f} remaining now")
        except Exception:
            pass
    if use_llm:
        st = skill_store.stats()
        print(f"  Skill library     : {st['skills']} entries / {st['writes']} cumulative writes "
              f"(overwritten {st['rewrites']}, discarded {st['skipped']})")
        # Bloat rate = write count / actual entries. v1 was 15 writes → 15 entries (1.0, not blocked at all)
        print(f"  Bloat rate     : {st['bloat']}  (>1 means dedup is working; =1 means every write became a new entry)")
        print(f"  Retrieval cap   : at most {st['top_k']} entries injected into the prompt per question")
    else:
        print(f"  Skill library     : {[s.name for s in voyager.skills]}")
    print()
    # The criterion for whether learning happened: done going from False → True (task going from not done → done,
    # Voyager's ruler, coverage points not counted). Coverage is only an observation hint.
    learned = len(dones) >= 2 and (dones[-1] and not dones[0])
    if coverages and coverages[0] >= 1.0:
        print("  [WARN] Perfect score in the first round - this task is too easy for the model to measure a learning curve.")
        print("     This does not mean \"cannot learn\", only \"no need to learn\". To measure learning, a harder question is required (--hard).")
    elif learned:
        print(f"  [OK] done: {dones[0]} -> {dones[-1]}"
              f"(coverage observation {coverages[0]:.0%} -> {coverages[-1]:.0%}):")
        print("     After Voyager wrote lessons into the skill library, the same question went from \"pipeline not working\" to "
              "\"ran + retrieved data + no crash\".")
        print("     This is the observable form of \"learned the orchestration\" in this prototype (coverage points not counted).")
    elif use_llm and len(voyager.memory) >= 2 and coverages[-1] <= coverages[0]:
        print("  [WARN] The skill library has content but coverage did not improve - the model read the lessons but did not change behavior")
    elif not use_llm and any(s.name == "分图下钻" for s in voyager.skills):
        print("  [WARN] The skill was added but coverage did not move - check the skill execution logic")
    elif not use_llm:
        print("  [FAIL] The skill library did not add \"分图下钻\" - the learning logic did not trigger")
    print()
    if use_llm:
        print("  This round used LLMVoyager: planning, extraction, and reflection are all done by the model itself.")
        print("  If success goes from False to True across rounds, the model truly learned from missing_points")
        print("  to change its orchestration and achieve all rubric points (coverage is only an achievement observation).")
    else:
        print("  This round used ScriptedVoyager (the skill library is rule-based), verifying the scoring pipeline.")
        print("  Add --llm to test \"can the model itself learn the orchestration\".")


if __name__ == "__main__":
    asyncio.run(main())
