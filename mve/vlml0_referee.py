#!/usr/bin/env python3
"""VLML0 -- the referee. The original VLML, with no Voyager involvement.

Why it must exist:
    In the previous version I wrote the standard answers as constants in the rubric (expected_value=25.0 / 8 / 7),
    which I hand-wrote after checking with SQL myself -- equivalent to **me being the referee**. This violates the companion study's hard contract:

        tutor_llm_agent_answer_evaluate.py:82-84
        "The evaluator may explain its verdict, but it must never replace the
         server-held expected answer with a model-generated reference answer."

    That is: the standard answer must be produced independently by the server; it can neither be given by the model nor hand-written by a human.
    In the MVE, the "server" is the original VLML -- it does not read Voyager's skill library, does not read Voyager's
    trajectory, and only takes the same question and the same set of MCP tools to orchestrate and retrieve data itself.

The difference between VLML0 and VLML1 (Voyager's arena):
    VLML0  The referee. Allowed to know all dimension hints of the rubric (it is the question setter and inherently knows what the answer looks like)
    VLML1  Voyager's arena. In blind mode, only the natural-language question stem is given; dimensions must be thought of by itself
    The two share the same TOOL_CATALOG, the same tool dispatch, and the same database -- only "whether the scoring points are known" differs. This way the comparison is fair: the gap is exactly how much Voyager's orchestration ability falls short.

Artifacts are cached on disk by topic_id: the same question is only run once.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import vlml_env  # noqa: F401  bootstrap the environment first

from loop_core import dig, fact_key, make_fact  # noqa: E402
from llm_client import chat_json  # noqa: E402
from voyager import LOCAL_SQL_NOTES, TOOL_CATALOG, TOOL_REGISTRY, VLML_USAGE_TIPS  # noqa: E402

STORE = Path(__file__).resolve().parent / "referee_store.json"
# The human-facing VLML perspective (narrative + LLM estimates) is stored separately, isolated from the objective referee -- never fed to Voyager
JUDGE_STORE = Path(__file__).resolve().parent / "referee_judge_store.json"


@dataclass
class RefereeAnswer:
    """The standard answer given by the referee: fact set + narrative (the narrative does not participate in comparison)."""

    topic_id: str
    facts: list[dict[str, Any]]
    narrative: str = ""
    trajectory: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""

    @property
    def keys(self) -> set[tuple[str, str]]:
        """Which (subject, dimension) the standard answer covers."""
        return {fact_key(f) for f in self.facts}

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "facts": self.facts,
            "narrative": self.narrative,
            "trajectory": self.trajectory,
            "ok": self.ok,
            "error": self.error,
        }


def _load_store() -> dict[str, Any]:
    if not STORE.exists():
        return {}
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_store(data: dict[str, Any]) -> None:
    STORE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def spec_fingerprint(task: Any) -> str:
    """The **data-retrieval recipe** fingerprint for this question -- the cache-expiry criterion.

    Why it is needed: the cache was keyed only by `topic_id`, but generated questions get
    **regenerated** (same topic_id, different recipe). Measured: after `max_losing_streak_map`
    was regenerated, the referee still held the old truth `'Corrode'` from cache (the old
    recipe's value_column pointed at map_name) and compared it against the new answer -- the
    model's `3` was **correct** yet judged "not comparable", 0% across 5 runs. The question
    changed but the referee didn't know -- the same disease as the knowledge-graph cache bug.
    """
    import hashlib
    parts = [str(getattr(task, "topic_id", ""))]
    for p in (getattr(task, "rubric", None) or []):
        s = getattr(p, "answer_spec", None)
        parts.append(f"{p.dimension}|{p.subject}|"
                     f"{getattr(s, 'sql', '') or ''}"
                     f"{getattr(s, 'tool', '') or ''}{getattr(s, 'value_path', '') or ''}"
                     f"{getattr(s, 'value_column', '')}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def cached(topic_id: str, fingerprint: str = "") -> RefereeAnswer | None:
    """Read the objective referee (STORE). Defensively strip facts sourced from `vlml0_referee.llm`
    -- even if an old version mixed LLM estimates into STORE, Voyager never sees them when comparing."""
    raw = _load_store().get(topic_id)
    if not raw:
        return None
    # recipe changed -> this cache is the answer to the **old question**, must not reuse
    if fingerprint and str(raw.get("spec_fingerprint") or "") != fingerprint:
        return None
    det_facts = [
        f for f in (raw.get("facts") or [])
        if (f.get("source") or {}).get("tool") != "vlml0_referee.llm"
    ]
    return RefereeAnswer(
        topic_id=raw.get("topic_id", topic_id),
        facts=det_facts,
        narrative=raw.get("narrative", ""),
        trajectory=raw.get("trajectory") or [],
        ok=bool(det_facts),
        error=raw.get("error", "") if det_facts else "referee produced no DB facts",
    )


def reset() -> None:
    if STORE.exists():
        STORE.unlink()
    if JUDGE_STORE.exists():
        JUDGE_STORE.unlink()
    print(f"Cleared referee cache: {STORE} + {JUDGE_STORE}")


def _load_judge_store() -> dict[str, Any]:
    if not JUDGE_STORE.exists():
        return {}
    try:
        return json.loads(JUDGE_STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_judge_store(data: dict[str, Any]) -> None:
    JUDGE_STORE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _save_judge(topic_id: str, fp: str,
                llm_facts: list[dict[str, Any]],
                narrative: str, traj: list[str]) -> None:
    """Persist the human-facing VLML view separately, isolated from the objective referee (STORE)."""
    store = _load_judge_store()
    store[topic_id] = {
        "topic_id": topic_id,
        "spec_fingerprint": fp,
        "llm_facts": llm_facts,
        "narrative": narrative,
        "trajectory": traj,
    }
    _save_judge_store(store)


def cached_judge(topic_id: str, fingerprint: str = "") -> dict[str, Any] | None:
    """Read the human-facing VLML view (narrative + LLM estimates). Not used in Voyager comparison."""
    raw = _load_judge_store().get(topic_id)
    if not raw:
        return None
    if fingerprint and str(raw.get("spec_fingerprint") or "") != fingerprint:
        return None
    return raw


# ---------------------------------------------------------------------------
# Referee's orchestration: same toolset as Voyager, but given the full prompt
# ---------------------------------------------------------------------------

REFEREE_PLAN_SCHEMA = """Output exactly one JSON object:
{"thought": "one sentence on how you plan to query",
 "calls": [{"tool": "tool_name", "args": {"arg_name": "arg_value"}}]}
At most 3 calls. Provide the queries that support ALL scoring points; do not omit any."""


def _plan_prompt(task: Any) -> str:
    points = "\n".join(
        f"- {p.point}｜dimension must be written as `{p.dimension}`｜"
        f"subject must be written as {json.dumps(p.subject, ensure_ascii=False)}"
        for p in task.rubric
    )
    return f"""You are the original VLML analysis engine (the referee). Retrieve the data needed for the **standard answer** of the question below.

Question:
{task.question}

Scoring points that must all be covered (each needs supporting data):
{points}

{TOOL_CATALOG}

VLML's own usage tips:
{VLML_USAGE_TIPS}

{LOCAL_SQL_NOTES}

{REFEREE_PLAN_SCHEMA}"""


def _extract_prompt(task: Any, obs: list[dict[str, Any]]) -> str:
    points = "\n".join(
        f"- {p.point}｜dimension=`{p.dimension}`｜subject={json.dumps(p.subject, ensure_ascii=False)}"
        for p in task.rubric
    )
    payload = json.dumps(
        [{"tool": o["tool"], "args": o.get("args"),
          "result": o.get("result"), "error": o.get("error")} for o in obs],
        ensure_ascii=False,
    )[:14000]
    return f"""Real data returned by the tools:

{payload}

Scoring points:
{points}

Organize into a fact array; each fact:
{{"subject": {{...}}, "dimension": "...", "value": <number>,
  "unit": "percent|count|ratio|raw", "base": <denominator>}}

Requirements:
- at least one fact per scoring point; dimension and subject must match the scoring point **verbatim**
- value must be a number that actually exists in the data; no inference, no fabrication
- base is the denominator (required for percentages)

Output JSON only: {{"facts":[...], "narrative":"one-sentence conclusion"}}"""


async def _execute(calls: list[dict[str, Any]],
                   limit: int = 3) -> tuple[list[dict[str, Any]], list[str]]:
    """Run the calls. limit must be driven by the **number of scoring points** -- hard-coding 3
    would make questions with more than 3 scoring points never complete their standard answer
    (same pitfall as voyager._execute)."""
    obs: list[dict[str, Any]] = []
    traj: list[str] = []
    for call in (calls or [])[:limit]:
        tool = str(call.get("tool", ""))
        args = dict(call.get("args") or {})
        fn = TOOL_REGISTRY.get(tool)
        if fn is None:
            obs.append({"tool": tool, "error": f"unknown tool {tool}"})
            continue
        try:
            if fn is vlml_env.execute_custom_sql:
                sql = str(args.pop("sql_query", "") or args.pop("sql", ""))
                if not sql.lower().lstrip().startswith("select"):
                    obs.append({"tool": tool, "error": "only SELECT allowed"})
                    continue
                res = await fn(sql)
                args = {"sql_query": sql}
            else:
                res = await fn(**args)
            obs.append({"tool": tool, "args": args, "result": res})
            traj.append(tool)
        except Exception as e:
            obs.append({"tool": tool, "error": f"{type(e).__name__}: {e}"})
    return obs, traj


async def _tool_facts(task: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Deterministic layer · tool-path version: call VLML tools directly, fetch standard answer by path.

    Complements _deterministic_facts (SQL version). Any scoring point whose metric is aggregated
    inside a tool (no equivalent SQL) goes here -- avoiding "I hand-write SQL following the tool
    logic", which would itself act as the referee.
    """
    facts: list[dict[str, Any]] = []
    done: list[str] = []
    for p in task.rubric:
        spec = getattr(p, "answer_spec", None)
        if spec is None or not spec.tool:
            continue
        fn = TOOL_REGISTRY.get(spec.tool)
        if fn is None:
            print(f"  [referee·tool-layer] {p.point} unknown tool {spec.tool}")
            continue
        try:
            res = await fn(**dict(spec.tool_args))
        except Exception as e:
            print(f"  [referee·tool-layer] {p.point} call failed: {type(e).__name__}: {e}")
            continue
        if isinstance(res, dict) and res.get("error"):
            print(f"  [referee·tool-layer] {p.point} tool error: {str(res['error'])[:120]}")
            continue
        value = dig(res, spec.value_path)
        base = dig(res, spec.base_path) if spec.base_path else None
        if value is None:
            print(f"  [referee·tool-layer] {p.point} path {spec.value_path} cannot get value")
            continue
        if spec.percent and isinstance(base, (int, float)) and base:
            value = round(float(value) / float(base) * 100, 1)
        facts.append(make_fact(
            subject=dict(p.subject),
            dimension=p.dimension,
            value=value,
            unit="percent" if spec.percent else "raw",
            base=int(base) if isinstance(base, (int, float)) else None,
            source={"tool": f"vlml0_referee::{spec.tool}", "args": dict(spec.tool_args)},
        ))
        done.append(p.point)
    return facts, done


async def _deterministic_facts(task: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Deterministic layer: anything SQL-can-compute is never asked of the model.

    The LLM miscounts when reading reports (measured: called a losing streak of 8 as 5,
    starting round R7 as R2), so every scoring point with an answer_spec is computed directly in SQL.
    """
    facts: list[dict[str, Any]] = []
    done: list[str] = []
    for p in task.rubric:
        spec = getattr(p, "answer_spec", None)
        # tool-path scoring points are handled by _tool_facts; do not duplicate here
        if spec is None or spec.tool:
            continue
        try:
            res = await vlml_env.execute_custom_sql(spec.sql)
        except Exception as e:
            print(f"  [referee·deterministic] {p.point} SQL failed: {type(e).__name__}: {e}")
            continue
        if res.get("error"):
            print(f"  [referee·deterministic] {p.point} SQL error: {res['error'][:120]}")
            continue
        rows = res.get("rows") or []
        if not rows:
            print(f"  [referee·deterministic] {p.point} SQL returned empty")
            continue
        row = rows[0]
        try:
            value = row[spec.value_column]
        except (IndexError, TypeError):
            continue
        base = None
        if spec.base_column is not None:
            try:
                base = int(row[spec.base_column])
            except (IndexError, TypeError, ValueError):
                base = None
        facts.append(make_fact(
            subject=dict(p.subject),
            dimension=p.dimension,
            value=value,
            unit="percent" if "percent" in str(p.dimension) or "conv" in str(p.dimension) else "count",
            base=base,
            source={"tool": "vlml0_referee.deterministic", "args": {"sql": spec.sql[:120]}},
        ))
        done.append(p.point)
    return facts, done


async def answer(task: Any, *, force: bool = False) -> RefereeAnswer:
    """Asynchronously run the referee to get the standard answer -- **feed Voyager only the DB-retrieved results**.

    Boundary (user correction 2026-10-12): Voyager's success/failure feedback must come 100% from DB retrieval,
    never let the VLML/LLM referee leak into the feedback loop. The LLM miscounts and fabricates (measured:
    called a losing streak of 8 as 5); using its output as the referee truth is exactly putting a
    "model-given reference answer" back into the comparison loop -- forbidden by the companion study's hard
    contract (tutor_llm_agent_answer_evaluate.py:82-84).

    Therefore this function:
      · the `facts` returned / persisted to `STORE` = deterministic layer (SQL direct calc + tool-path fetch), **pure DB**;
      · the LLM layer's narrative and estimate facts go only to `JUDGE_STORE`, **human-facing on the panel only**,
        never into `ref.facts`, never into `evaluate_vs_referee`.
    Order follows the companion study AssessmentEngine: deterministic layer first, LLM layer only supplements the human view.
    """
    fp = spec_fingerprint(task)
    if not force:
        hit = cached(task.topic_id, fp)
        if hit is not None:
            return hit

    # ---- 1 deterministic layer ---- (SQL direct calc + tool-path fetch, complementary) · the only facts fed to Voyager
    det_facts, det_done = await _deterministic_facts(task)
    tool_facts, tool_done = await _tool_facts(task)
    det_facts = det_facts + tool_facts
    det_keys = {fact_key(f) for f in det_facts}

    # ---- 2 LLM layer ---- (human-facing narrative + estimate only, not in fact comparison)
    # Scoring points the deterministic layer didn't cover have no DB truth: they can be neither right nor
    # wrong for Voyager; the LLM only produces a "human-facing" estimate narrative, never a comparison baseline.
    remaining = [
        p for p in task.rubric
        if fact_key({"subject": p.subject, "dimension": p.dimension}) not in det_keys
    ]
    # This orchestration still runs: the resulting trajectory is the "reference retrieval path" shown to humans / the graph
    # (cf. companion study reference_answer, tutor_llm_agent_answer_evaluate.py:84).
    # Only the tool sequence is given, never the numbers as referee truth.
    sub = _SubTask(task, remaining or list(task.rubric))
    plan = chat_json([
        {"role": "system", "content": "You are the original VLML analysis engine. You only retrieve data, no subjective inference."},
        {"role": "user", "content": _plan_prompt(sub)},
    ])
    obs, traj = await _execute(plan.get("calls") or [],
                               limit=max(3, min(8, len(task.rubric or []))))

    llm_facts: list[dict[str, Any]] = []
    narrative = ""
    if remaining:
        out = chat_json(
            [
                {"role": "system", "content": "You are a rigorous data organizer; output JSON only, never fabricate numbers."},
                {"role": "user", "content": _extract_prompt(sub, obs)},
            ],
        )
        for f in (out.get("facts") or []):
            if not isinstance(f, dict) or "subject" not in f or "dimension" not in f:
                continue
            llm_facts.append(make_fact(
                subject=f.get("subject") or {},
                dimension=str(f.get("dimension")),
                value=f.get("value"),
                unit=str(f.get("unit") or "raw"),
                base=f.get("base"),
                source={"tool": "vlml0_referee.llm", "args": {}},
            ))
        narrative = str(out.get("narrative", ""))

    if not narrative:
        narrative = (
            f"DB baseline referee: deterministic layer covers {len(det_facts)} scoring points"
            + (f"; {len(remaining)} more have no DB truth, VLML only estimates and does not score."
               if remaining else "; all scoring points have DB truth.")
        )

    # persist 1: objective referee (STORE) -- DB truth only, no VLML estimates
    ans = RefereeAnswer(
        topic_id=task.topic_id,
        facts=det_facts,                       # <- pure DB, for Voyager comparison
        narrative=narrative,                    # scoring narrative (with 'estimate-only' note)
        trajectory=(["<deterministic-sql>"] if det_facts else []) + traj,
        ok=bool(det_facts),
        error="" if det_facts else "referee produced no DB facts",
    )
    store = _load_store()
    rec = ans.as_dict()
    rec["spec_fingerprint"] = fp        # recipe changed -> must rerun (see spec_fingerprint)
    store[task.topic_id] = rec
    _save_store(store)

    # persist 2: human-facing VLML view (JUDGE_STORE) -- never fed to Voyager
    _save_judge(task.topic_id, fp, llm_facts, narrative, traj)
    return ans


class _SubTask:
    """Task view keeping only remaining scoring points, for the LLM layer."""

    def __init__(self, task: Any, rubric: list[Any]) -> None:
        self.topic_id = task.topic_id
        self.question = task.question
        self.rubric = rubric
        self.difficulty = getattr(task, "difficulty", 2)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tasks import TASKS
    from run_mve import TASK, TASK_HARD

    if "--judge-all" in sys.argv:
        # backfill: run referee on all seed questions, fill both objective referee (STORE) and VLML view (JUDGE_STORE)
        for tid, task in TASKS.items():
            res = asyncio.run(answer(task, force=True))
            print(f"  {tid}: DB facts {len(res.facts)}, ok={res.ok}")
        print("judge-all done")
    else:
        task = TASK_HARD if "--hard" in sys.argv else TASK
        res = asyncio.run(answer(task, force="--force" in sys.argv))
        print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2)[:3000])
