#!/usr/bin/env python3
"""MVE core: fact set → coverage scoring → mastery.

All three sections copy the paper design and the study companion's thresholds verbatim; line-number evidence is in each function's docstring.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable


# --------------------------------------------------------------------------
# 1. Facts
# --------------------------------------------------------------------------
# Paper design: fact = {subject, dimension, value, unit, base, scope, source}
# diff unit = (subject, dimension) pair


def fact_key(f: dict[str, Any]) -> tuple[str, str]:
    """A fact's comparison key: normalized subject + dimension."""
    s = f.get("subject") or {}
    if isinstance(s, dict):
        subj = "|".join(f"{k}={s[k]}" for k in sorted(s) if s[k] is not None)
    else:
        subj = str(s)
    return (subj, str(f.get("dimension")))


def _key_match(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """Do two fact_keys refer to the same fact — **'?' is a wildcard**.

    Why the wildcard is necessary: the semantics of `map: '?'` in subject is "this dimension is unspecified"
    (the question generator uses it to mean "whichever map you find"). But facts obtained by the referee running recipes
    carry '?' through verbatim (`series=2843069/team=Cloud9/map=?`), while the rubric point declares
    subject with the true value `map=Lotus` — the keys never match.

    Observed consequence: `max_losing_streak_map` ran 5 rounds in a row, with the referee recording
    `unjudgeable` every time ("the referee itself could not answer either"), coverage=0, missing=[],
    rejected=[] — the model **scored nothing no matter what it did**, and critique stayed word-for-word identical for five rounds.
    This is not "cannot learn"; the comparison layer judged it "unjudgeable".
    """
    if a == b:
        return True
    if a[1] != b[1]:
        return False
    sa, sb = a[0], b[0]
    if sa == sb:
        return True
    pa, pb = sa.split("|"), sb.split("|")
    if len(pa) != len(pb):
        return False
    for x, y in zip(pa, pb):
        if x == y:
            continue
        xk, _, xv = x.partition("=")
        yk, _, yv = y.partition("=")
        if xk != yk:
            return False
        if xv == "?" or yv == "?":     # One side unspecified → treated as the same
            continue
        return False
    return True


def _lookup(index: dict, key: tuple[str, str]) -> Any | None:
    """Exact lookup first, then a '?' wildcard lookup if not found."""
    hit = index.get(key)
    if hit is not None:
        return hit
    for k, v in index.items():
        if _key_match(k, key):
            return v
    return None


def norm_subject(value: Any) -> dict[str, Any]:
    """Normalize the subject returned by the model into a cross-key dictionary.

    Observed: the model returns a string (e.g. "series=2843069/map=Corrode") instead of an object,
    which does not crash on entry to fact_key, but subsequent dashboard rendering and comparison both go wrong.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        # Try parsing the "k=v/k=v" form; if unparseable, treat the whole thing as one label
        parts = [p for p in text.split("/") if "=" in p]
        if parts:
            out: dict[str, Any] = {}
            for p in parts:
                k, _, v = p.partition("=")
                if k.strip():
                    out[k.strip()] = v.strip()
            return out
        return {"label": text}
    return {}


def make_fact(
    *,
    subject: dict[str, Any],
    dimension: str,
    value: Any,
    unit: str = "raw",
    base: int | None = None,
    scope: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "subject": norm_subject(subject),
        "dimension": dimension,
        "value": value,
        "unit": unit,
        "base": base,
        "scope": scope or {},
        "source": source or {},
    }


def from_sql_result(
    rows: dict[str, Any],
    *,
    subject_keys: Iterable[str],
    dimension_key: str,
    value_key: str,
    base_key: str | None = None,
    tool: str = "execute_custom_sql",
    sql: str = "",
    fixed_subject: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand an execute_custom_sql result set into facts: one row × one column = one fact.

    This is the key to "any SQL can be swallowed" — only the result is recorded, not which table the column belongs to.
    """
    cols = list(rows.get("columns") or [])
    out: list[dict[str, Any]] = []
    for row in rows.get("rows") or []:
        rec = dict(zip(cols, row))
        subject = dict(fixed_subject or {})
        for k in subject_keys:
            if k in rec and rec[k] is not None:
                subject[k] = rec[k]
        out.append(make_fact(
            subject=subject,
            dimension=str(rec.get(dimension_key)),
            value=rec.get(value_key),
            base=int(rec[base_key]) if base_key and rec.get(base_key) is not None else None,
            source={"tool": tool, "args": {"sql": sql}},
        ))
    return out


def from_key_metrics(
    metrics: dict[str, Any],
    *,
    subject: dict[str, Any],
    prefix: str = "",
    tool: str = "",
    scope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand key_metrics from a VLML report into facts.

    Key point: VLML itself organizes values as {"num":..,"denom":..},
    so fact.base takes denom directly, with no self-computation.
    """
    out: list[dict[str, Any]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if "num" in node and "denom" in node:
                out.append(make_fact(
                    subject=subject,
                    dimension=path,
                    value=node.get("num"),
                    base=node.get("denom"),
                    unit="count",
                    scope=dict(scope or {}),
                    source={"tool": tool, "section": "key_metrics"},
                ))
                return
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            out.append(make_fact(
                subject=subject,
                dimension=path,
                value=node,
                unit="raw",
                scope=dict(scope or {}),
                source={"tool": tool, "section": "key_metrics"},
            ))

    walk(metrics, prefix)
    return out


# --------------------------------------------------------------------------
# 2. Scoring: coverage comparison + verdict
# --------------------------------------------------------------------------


@dataclass
class AnswerSpec:
    """A server-private deterministic answer recipe.

    Mirrors the study companion's NumericToleranceEvaluator contract (deterministic_evaluators.py:158-167):
    expected and tolerance are **read only from the server-private answer_spec**, never from the learner's answer,
    and never replaced with a model-generated reference answer (tutor_llm_agent_answer_evaluate.py:82-84).

    Why it must exist: initially I wrote the standard answers as constants in the rubric (25.0 / 8 / 7),
    which I hand-wrote = me being the referee. Later I switched to having the LLM referee run once, and it read the report
    wrong (said losing streak 5, starting R2; the true values are 8, R7) — LLMs fabricate.
    So anything computable with SQL must be computed with SQL; this part is deterministic and passes through no model.
    """

    sql: str = ""                     # Deterministic SQL, must start with SELECT (CTE not supported)
    value_column: int = 0
    base_column: int | None = None
    numeric_tolerance: float = 0.0
    closed_world: bool = True

    # ---- Tool path: either this or sql ----
    # Some metrics are produced by **internal aggregation** in VLML tools (pistol / eco / confidence labels),
    # with no corresponding single SQL statement. Forcing SQL reproduction means hand-writing the tool logic once myself —
    # which becomes "me being the referee" again. So here we call the tool directly and read values by path:
    # the standard answer is still produced independently by the server (the original VLML), passing through neither model nor human.
    tool: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    value_path: str = ""              # dotted path, e.g. key_metrics.economy.pistol.num
    base_path: str = ""               # path to the denominator
    percent: bool = False             # True → value = round(num/denom*100, 1)


def dig(obj: Any, path: str) -> Any:
    """Read a value from a tool response by dotted path. Returns None when not found (never guess, never fall back)."""
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


@dataclass
class RubricPoint:
    """One rubric point. Expects one (subject, dimension) to be covered."""

    point: str
    subject: dict[str, Any]
    dimension: str
    weight: float
    min_base: int = 20          # The question's self-declared sample threshold (paper design: written into the rubric, not globally hard-coded)
    expected_value: float | None = None
    tolerance: float = 0.05
    # Server-private: Voyager cannot see it; only the referee (VLML0) uses it to compute the standard answer
    answer_spec: AnswerSpec | None = None


@dataclass
class Evaluation:
    verdict: str                 # correct / partial / wrong / dont_know
    score: int                   # 0-100
    coverage: float              # 0-1
    covered_points: list[str]
    missing_points: list[str]
    rejected_low_base: list[str]
    evidence_status: str         # collected / below_threshold / none
    facts_count: int
    judge: str = "referee"       # referee (VLML0 referee) | rubric (old built-in answers, deprecated)
    unjudgeable: list[str] = field(default_factory=list)   # Rubric points the referee has no answer for either
    no_tool_calls: bool = False  # True = no tool call succeeded this round, so facts have no source
    # **Voyager criterion-layer boolean self-verification** (mirrors the original critic.check_task_success):
    #   A task (=one question) goal achieved ⇔ all rubric points confirmed covered by the referee, with correct values.
    #   Division of labor with verdict: verdict is the study companion's four-band score (correct derived from coverage≥0.95,
    #   and 95% in the original's eyes means "not fully achieved = not success"); success is the only criterion recognized by "store skill /
    #   stop same topic / advance to next topic / transfer exam". The two are no longer conflated.
    success: bool = False


def evaluate_vs_referee(
    facts: list[dict[str, Any]],
    rubric: list[RubricPoint],
    referee_facts: list[dict[str, Any]],
    *,
    tool_calls: int = -1,
) -> Evaluation:
    """Voyager's fact set vs VLML0 referee's fact set.

    This is the algorithm for "degree of mastery": the standard answer is run independently by the original VLML,
    neither hand-written by the question author nor a model-generated reference answer.

    The deterministic layer runs first (mirroring the order of the study companion's AssessmentEngine.try_assess):
      0. **Tool-call gate**: not a single tool call succeeded → judge as no evidence, not counted in mastery.
         Observed pitfall: when all tools fail, the model fabricates 5 "facts" out of thin air,
         with dimensions that even match (because the question text said them), but all values fabricated. Without this gate,
         "fabrication" earns partial credit.
      1. base gate — insufficient denominator means a correct value does not count
      2. (subject, dimension) hit
      3. Value tolerance comparison (tolerance comes from the server-side answer_spec)
    Points that cannot be judged go to unjudgeable, silently counted neither right nor wrong.
    """
    voyager_index: dict[tuple[str, str], dict[str, Any]] = {}
    for f in facts:
        voyager_index[fact_key(f)] = f

    ref_index: dict[tuple[str, str], dict[str, Any]] = {}
    for f in referee_facts or []:
        ref_index[fact_key(f)] = f

    covered: list[str] = []
    missing: list[str] = []
    rejected: list[str] = []
    unjudgeable: list[str] = []
    got = 0.0
    total = sum(p.weight for p in rubric) or 1.0
    no_evidence = tool_calls == 0

    for p in rubric:
        key = fact_key({"subject": p.subject, "dimension": p.dimension})
        ref = _lookup(ref_index, key)
        if ref is None:
            # The referee itself could not answer either — this question is flawed, and it cannot count as Voyager's mistake
            unjudgeable.append(p.point)
            continue
        got_fact = _lookup(voyager_index, key)
        if got_fact is None:
            missing.append(p.point)
            continue

        base = got_fact.get("base")
        if base is not None and base < p.min_base:
            rejected.append(f"{p.point}(base={base}<{p.min_base})")
            missing.append(p.point)
            continue

        # Value comparison: against the referee's value, tolerance from the rubric point's declaration or answer_spec
        ref_value = ref.get("value")
        got_value = got_fact.get("value")
        # **A blank submission ≠ a wrong answer**. Observation shows this case dominates: the model fills null when it has not seen the number,
        # and it used to be compared as an "answer", recorded as "value incomparable" or "value mismatch 0≠59" —
        # that whitewashes "not retrieved" as "retrieved but wrong", masking the real disease (incomplete evidence gathering).
        if got_value is None:
            missing.append(f"{p.point}(value not retrieved: model submitted null)")
            continue
        tol = p.tolerance if p.answer_spec is None else p.answer_spec.numeric_tolerance
        try:
            if abs(float(got_value) - float(ref_value)) > tol:
                missing.append(f"{p.point}(value mismatch: {got_value}≠{ref_value})")
                continue
        except (TypeError, ValueError):
            if str(got_value) != str(ref_value):
                # **Same type ≠ a form problem**. Observed with `confidence`: got="insufficient",
                # ref="moderate", both str, falling into this branch only because float() cannot convert,
                # so it got recorded as "value incomparable" — but that is a **wrong value** (picked the wrong enum),
                # not "form mismatch". The two need different remedies:
                #   form wrong → align the answer's form; value wrong → re-judge / change the metric.
                # So they must be written separately here, otherwise next_action in the feedback points the wrong way.
                if type(got_value) is type(ref_value):
                    missing.append(f"{p.point}(value mismatch: {got_value}≠{ref_value})")
                else:
                    # Write both sides' **types and prototypes** into the log: writing only "value incomparable" says nothing,
                    # and the human cannot tell whether the model copied the form wrong or the referee's value was never numeric.
                    missing.append(
                        f"{p.point}(value incomparable: got={got_value!r}<{type(got_value).__name__}>"
                        f"≠ref={ref_value!r}<{type(ref_value).__name__}>)"
                    )
                continue

        covered.append(p.point)
        got += p.weight

    coverage = got / total

    # Not a single tool call succeeded → the facts in hand have no source, so the whole round is judged no-evidence.
    # Mirroring the study companion's ui_api.py:41: "no evidence = unassessed + None, not 0%".
    if no_evidence:
        return Evaluation(
            verdict="dont_know", score=0, coverage=0.0,
            covered_points=[], missing_points=[p.point for p in rubric],
            rejected_low_base=rejected, evidence_status="none",
            facts_count=len(facts), judge="referee",
            unjudgeable=unjudgeable,
            no_tool_calls=True,
        )

    if not facts:
        evidence = "none"
    elif not covered and (rejected or missing):
        evidence = "below_threshold"
    elif covered:
        evidence = "collected"
    else:
        evidence = "below_threshold"

    if evidence == "none":
        verdict = "dont_know"
    elif coverage >= 0.95:
        verdict = "correct"
    elif coverage > 0:
        verdict = "partial"
    else:
        verdict = "wrong"

    # ---- Voyager criterion-layer success (self-verification) ----
    # Decoupled from verdict: coverage≥0.95 can be judged correct, but as long as any rubric point is missing /
    # rejected / unjudgeable, the task is not "fully achieved", and success must be False.
    # Per the original: success = the critic confirms the task goal in the environment was truly achieved (boolean),
    # 95% achieved = not achieved. unjudgeable (the referee itself could not answer) likewise does not count as achieved —
    # that question's goal cannot be verified, and "the question is flawed" must not become "Voyager succeeded".
    success = (
        evidence == "collected"
        and not missing
        and not rejected
        and not unjudgeable
    )

    return Evaluation(
        verdict=verdict,
        score=int(round(coverage * 100)),
        coverage=round(coverage, 4),
        covered_points=covered,
        missing_points=missing,
        rejected_low_base=rejected,
        evidence_status=evidence,
        facts_count=len(facts),
        judge="referee",
        unjudgeable=unjudgeable,
        success=success,
    )


def task_done(
    facts: list[dict[str, Any]],
    *,
    tool_calls: int = 0,
    errors: list[str] | None = None,
) -> bool:
    """Voyager's ruler: task done (=one question's goal achieved).

    Mirroring the original critic.check_task_success (critic.py:116): it judges "observable
    goal achievement in the environment", not "how many rubric points were covered". In MVE task = answering a question, and goal achieved =
    orchestration ran (successful tool calls) + data retrieved (non-empty facts) + no crash (no tool errors).
    **Coverage points not counted** — coverage points are VLML's ruler, used only for exams (exam transfer/final)
    and mastery evidence; the learning stage (store skill / stop same topic / advance to next topic) recognizes only this boolean.
    """
    if tool_calls <= 0:
        return False
    if not facts:
        return False
    if errors:
        return False
    return True


def evaluate(
    facts: list[dict[str, Any]],
    rubric: list[RubricPoint],
) -> Evaluation:
    """Legacy path: compare against the rubric's built-in expected_value.

    ⚠️ Deprecated. expected_value is hand-written by the question author / model-generated, not run independently by the server,
    and using it to compute mastery is a source of false positives. Kept only for comparison experiments; the formal path is evaluate_vs_referee.
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for f in facts:
        index[fact_key(f)] = f

    covered: list[str] = []
    missing: list[str] = []
    rejected: list[str] = []
    got = 0.0
    total = sum(p.weight for p in rubric) or 1.0

    for p in rubric:
        key = fact_key({"subject": p.subject, "dimension": p.dimension})
        f = index.get(key)
        if f is None:
            missing.append(p.point)
            continue
        base = f.get("base")
        if base is not None and base < p.min_base:
            rejected.append(f"{p.point}(base={base}<{p.min_base})")
            missing.append(p.point)
            continue
        if p.expected_value is not None:
            try:
                if abs(float(f["value"]) - p.expected_value) > p.tolerance:
                    missing.append(f"{p.point}(value mismatch)")
                    continue
            except (TypeError, ValueError):
                missing.append(f"{p.point}(value incomparable)")
                continue
        covered.append(p.point)
        got += p.weight

    coverage = got / total

    if not facts:
        evidence = "none"
    elif not covered and rejected:
        evidence = "below_threshold"
    elif covered:
        evidence = "collected"
    else:
        evidence = "below_threshold"

    if evidence == "none":
        verdict = "dont_know"
    elif coverage >= 0.95:
        verdict = "correct"
    elif coverage > 0:
        verdict = "partial"
    else:
        verdict = "wrong"

    return Evaluation(
        verdict=verdict,
        score=int(round(coverage * 100)),
        coverage=round(coverage, 4),
        covered_points=covered,
        missing_points=missing,
        rejected_low_base=rejected,
        evidence_status=evidence,
        facts_count=len(facts),
        judge="rubric",
    )


# --------------------------------------------------------------------------
# 3. Mastery — the V2 model (mve/mastery_model.py, mirroring the study companion's mastery_v2.py)
# --------------------------------------------------------------------------
#
# The old version here mirrored the study companion's **V1** (knowledge_tracker.py:252-286). V1 has two hard flaws,
# which the study companion itself fixed in V2, and we followed:
#   1. `confidence = 1-exp(-attempts/5)` only counts attempts → V2 computes from the sum of evidence weights
#   2. `recency = 1.0` hard-coded (V1:266) → V2 introduces time decay with a 60-day half-life
# V2 also brings: each evidence has a weight (evaluator confidence × response reliability × time decay),
# weighted average instead of simple average, and using the concrete score when available (no longer degenerating into three verdict bands).
# The old V1 formula has been deleted; for comparison see `掌握度调研-猫娘伴学对照.md`.

import mastery_model  # noqa: E402
from datetime import datetime  # noqa: E402

from mastery_model import (  # noqa: E402
    DEFAULT_POLICY,
    MASTERY_INSUFFICIENT_EVIDENCE,
    MASTERY_MASTERED,
    MASTERY_PROGRESSING,
    MasteryEvidence,
    MasteryPolicy,
    MasterySnapshot,
    calculate_mastery,
)

MASTERY_THRESHOLD = DEFAULT_POLICY.mastered_threshold
MASTERY_MIN_ATTEMPTS = DEFAULT_POLICY.mastery_min_attempts
UNRESOLVED_WRONG_CAP = DEFAULT_POLICY.unresolved_wrong_mastery_cap


def update_mastery(
    topic_id: str,
    evidence: list[MasteryEvidence],
    *,
    unresolved_wrong_count: int = 0,
    has_active_wrong_question: bool = False,
    as_of: datetime | str | None = None,
    policy: MasteryPolicy | None = None,
) -> MasterySnapshot:
    """Mastery projection (a thin wrapper; the body is mastery_model.calculate_mastery).

    `evidence` is this topic's **entire** history of evidence (rebuilt cross-process from run_log),
    not "this round's single entry" — V2's weighting and time decay must see the full sequence to be correct.
    """
    return calculate_mastery(
        topic_id,
        evidence,
        unresolved_wrong_count=unresolved_wrong_count,
        has_active_wrong_question=has_active_wrong_question,
        as_of=as_of,
        policy=policy or DEFAULT_POLICY,
    )



# --------------------------------------------------------------------------
# 4. Question generator
# --------------------------------------------------------------------------


@dataclass
class Task:
    """A blocking task (= one practice question in the study companion)."""

    topic_id: str
    question: str
    rubric: list[RubricPoint]
    difficulty: int = 2
    validated_target: bool = True      # False for human-imported questions → not counted in mastery
    min_base: int = 20
    # Which MCP tools this question needs. The question generator does **tool coverage** accordingly:
    # a tool never used → prioritize questions needing it, or coverage stays a paper argument forever.
    requires_tools: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "question": self.question,
            "difficulty": self.difficulty,
            "validated_target": self.validated_target,
            "requires_tools": list(self.requires_tools),
            "key_points": [p.point for p in self.rubric],
            "rubric": {p.point: p.weight for p in self.rubric},
        }


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
