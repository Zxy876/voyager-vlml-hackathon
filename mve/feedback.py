#!/usr/bin/env python3
"""Grading-feedback structure (mirrors the study companion's `tutor_llm_agent_answer_evaluate.py:96-131`).

Why this module exists
----------------------
The user's own words: "the content written back from feedback into Voyager's learning side is not
done well; it does not provide retrievable, useful value."

Previously the critique produced by `voyager.learn()` was a hard-coded template — one sentence for
right, one for wrong, with only the dimension name swapped each round:

    right: "My fact set covers all of the referee's scoring points; the orchestration is effective."
    wrong: "I thought orchestrating it this way would cover every scoring point, but after the
            referee's comparison the verdict was wrong; the dimension that was missed is
            eco_win_rate. Next time I should drill down once more on those dimensions."

**"Next time I should drill down on those dimensions" is the floor of executable value** — it does
not say:
- whether the dimension was **never retrieved** (it isn't in the observations at all) or was
  **retrieved but wrong**;
- which tool should be added;
- whether any of the already-called tools were called for nothing.

And the remedy for "never retrieved" and for "retrieved wrongly" are **completely different**:
the former needs another tool, the latter needs a different metric definition. Collapsing both into
one sentence "drill down once more" means the model keeps retrying in the wrong direction — which is
exactly the disease measured in `取证通路-原版Voyager对照.md` (got=None being treated as an answer and
compared).

How the study companion does it
-------------------------------
After grading, the study companion returns **a structured bundle**, not a single comment:
    verdict / score / error_type / feedback / next_action /
    covered_points / missing_points / misconceptions / step_feedback /
    related_topics / reference_answer / confidence

Three of those carry the value:
1. `error_type` — which class of error it was, deciding the remedy direction;
2. `next_action` — the **concrete** next step;
3. `misconceptions` — records the wrong assumption "I thought X", not just "I was wrong".

The mapping in this module
--------------------------
MVE has no mathematical "error types", but it has its own set (all of them come from the measured
suffixes recorded at judging time in `loop_core.py`, none of them are invented):

    unmeasured        the model submitted null — this dimension **is not in the observations**
                      (evidence gathering incomplete)
    missing_dimension the fact behind the scoring point was never submitted
    wrong_value       retrieved but the number is wrong (metric definition / filter condition wrong)
    value_form        types are incomparable (numeric vs enum)
    low_base          sample size too small, rejected by the referee
    no_evidence       no evidence at all

`error_type → next_action` is this module's core table: **every entry must be directly executable**.

On top of that: the study companion returns the reference answer `reference_answer` after grading,
because what it wants is "learning". MVE's exam channel deliberately withholds the numbers (otherwise
mastery would not be comparable) and returns only the **orchestration sequence** (corresponding to
`reference_plan`) — the method, not the answer.

Division of labour with bypass learning
--------------------------------------
The skills distilled by `bypass_learn.py` carry `defect` / `strategy` (coarse-grained: which class of
defect it fixes). The `error_type` here is fine-grained (what exactly went wrong this round). The two
are aligned into one vocabulary via `ERROR_TO_DEFECT`, otherwise the skill library ends up with two
different vocabularies.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Vocabulary: error_type (where this round went wrong, fine-grained)
# --------------------------------------------------------------------------
# Keys are matched against loop_core.py's `missing` / `rejected_low_base` entries and are shared
# with the referee-side tooling — never translate the keys, only the human-readable values.
ERROR_TYPES: dict[str, str] = {
    "unmeasured": "value not retrieved (the model submitted null) — this dimension is not in the observations, evidence gathering is incomplete",
    "missing_dimension": "dimension missing — the fact behind the scoring point was never submitted",
    "wrong_value": "value mismatch — retrieved but the number is wrong (metric definition / filter condition wrong)",
    "value_form": "value form incomparable — the types don't line up (numeric vs enum/string)",
    "low_base": "sample size too small, rejected by the referee",
    "no_evidence": "no evidence at all",
}

# --------------------------------------------------------------------------
# Vocabulary: defect / strategy (used by skill-library blueprints, coarse-grained; the same set as
# bypass_learn)
# --------------------------------------------------------------------------
DEFECTS: dict[str, str] = {
    "missing_dimension": "dimension missing",
    "wrong_granularity": "granularity / metric definition wrong",
    "tool_uncovered": "tool never used",
    "value_form": "value form wrong",
}
STRATEGIES: dict[str, str] = {
    "drill_down": "drill down one level",
    "switch_tool": "switch tool",
    "combine_tools": "combine several tools",
    "read_scope": "read the report's scope metadata",
}

# Fine → coarse: guarantees feedback and bypass_learn speak of the same set of defects
ERROR_TO_DEFECT: dict[str, str] = {
    "unmeasured": "tool_uncovered",
    "missing_dimension": "missing_dimension",
    "wrong_value": "wrong_granularity",
    "value_form": "value_form",
    "low_base": "wrong_granularity",
    "no_evidence": "tool_uncovered",
}

# Severity: the most severe one becomes the primary error_type (unmeasured is the one to fix first —
# it means the comparison never even happened, so only after adding a tool does right/wrong even
# come into play)
ERROR_SEVERITY: dict[str, int] = {
    "no_evidence": 5,
    "unmeasured": 4,
    "missing_dimension": 3,
    "wrong_value": 2,
    "value_form": 1,
    "low_base": 0,
}

# --------------------------------------------------------------------------
# Parsing: the reason suffixes carried in the missing entries (inserted by loop_core.py at judging
# time). These patterns must match the suffixes loop_core.py literally appends:
#   loop_core.py:358  f"{p.point}(value not retrieved: model submitted null)"
#   loop_core.py:363  f"{p.point}(value mismatch: {got_value}≠{ref_value})"
#   loop_core.py:379  f"{p.point}(value incomparable: got=...<type>≠ref=...<type>)"
#   loop_core.py:347  f"{p.point}(base={base}<{p.min_base})"
# --------------------------------------------------------------------------
_RE_UNMEASURED = re.compile(
    r"^(?P<point>.+?)\(value not retrieved:?\s*(?P<got>[^)]*)\)\s*$"
)
_RE_WRONG = re.compile(
    r"^(?P<point>.+?)\(value mismatch:?\s*(?P<got>.*?)[≠](?P<ref>.*?)\)\s*$"
)
_RE_FORM = re.compile(
    r"^(?P<point>.+?)\(value incomparable:?\s*got=(?P<got>.*?)<(?P<gottype>[A-Za-z_]+)>"
    r"[≠]ref=(?P<ref>.*?)<(?P<reftype>[A-Za-z_]+)>\)\s*$"
)
_RE_LOWBASE = re.compile(r"^(?P<point>.+?)\(base=.*\)\s*$")


@dataclass
class MissingItem:
    """One missing scoring point + why it is missing."""
    point: str
    dimension: str = ""
    error_type: str = "missing_dimension"
    got: str = ""
    ref: str = ""
    got_type: str = ""
    ref_type: str = ""

    @property
    def defect(self) -> str:
        return ERROR_TO_DEFECT.get(self.error_type, "missing_dimension")

    def to_dict(self) -> dict[str, Any]:
        # producers: "which tool produces this dimension" in the knowledge graph. Carrying it lets
        # Voyager judge "have I already tried the tool that was named" — without it, it can only
        # guess whether help is needed.
        producers: list[str] = []
        g = graph()
        if g is not None:
            try:
                producers = list(g.who_produces(self.dimension or self.point))
            except Exception:
                producers = []
        return {
            "point": self.point, "dimension": self.dimension,
            "error_type": self.error_type, "defect": self.defect,
            "got": self.got, "ref": self.ref,
            "got_type": self.got_type, "ref_type": self.ref_type,
            "producers": producers,
        }


def parse_missing(entry: str, *, dim_by_point: dict[str, str] | None = None) -> MissingItem:
    """Split an entry such as `xxx(value mismatch: 0≠59)` into one structured record.

    The suffix is inserted at judging time (`loop_core.py:358/363/379`), so this is **reading**, not
    guessing.
    """
    e = str(entry or "").strip()
    dim_by_point = dim_by_point or {}

    for rx, etype in ((_RE_UNMEASURED, "unmeasured"),
                      (_RE_FORM, "value_form"),
                      (_RE_WRONG, "wrong_value"),
                      (_RE_LOWBASE, "low_base")):
        m = rx.match(e)
        if m:
            point = m.group("point").strip()
            item = MissingItem(point=point, dimension=dim_by_point.get(point, point),
                               error_type=etype)
            if etype == "unmeasured":
                # loop_core.py:358 writes the fixed literal "model submitted null" after the
                # colon; normalise it back to "null" so the text stays machine-comparable.
                raw = (m.group("got") or "").strip()
                item.got = "" if raw in ("", "model submitted null") else raw
                item.got = item.got or "null"
            elif etype == "wrong_value":
                item.got = (m.group("got") or "").strip()
                item.ref = (m.group("ref") or "").strip()
            elif etype == "value_form":
                item.got = (m.group("got") or "").strip()
                item.ref = (m.group("ref") or "").strip()
                item.got_type = m.group("gottype")
                item.ref_type = m.group("reftype")
            return item

    point = e.split("(")[0].strip()
    return MissingItem(point=point, dimension=dim_by_point.get(point, point),
                       error_type="missing_dimension")


def point_name(entry: str) -> str:
    """The scoring point's original text: strip the judging suffix (`(value mismatch: got≠ref)` /
    `(value not retrieved…)` / `(base=…)`).

    The suffix is appended at judging time and carries the **referee's reference value** — it may
    only go to the dashboard/log (reconciliation info for humans), and must never enter the
    model-visible context, otherwise the next round simply copies the answer (extension of
    convention ⑧, user correction on 2026-10-12). The `point` group of parse_missing is precisely
    that suffix-stripped original text.
    """
    return parse_missing(entry).point


# --------------------------------------------------------------------------
# Knowledge graph (lazy): dimension → which tool produces it
# --------------------------------------------------------------------------
_GRAPH: Any = None          # None=not loaded / False=unavailable / KnowledgeGraph=loaded


def graph() -> Any | None:
    """Fetch the knowledge graph. It reads the persisted JSON and **does not import vlml_env** —
    so it also works when recomputing offline (replaying historical logs via --topic)."""
    global _GRAPH
    if _GRAPH is None:
        try:
            import knowledge_graph
            _GRAPH = knowledge_graph.KnowledgeGraph.load()
        except Exception:
            _GRAPH = False
    return _GRAPH or None


def reset_graph() -> None:
    global _GRAPH
    _GRAPH = None


# --------------------------------------------------------------------------
# error_type → next_action (this module's core table: every entry must be directly executable)
# --------------------------------------------------------------------------
def _sql_recipe(dim: str, subject: dict[str, Any] | None = None) -> str:
    """"What to query" for SQL-path dimensions — it is stored in the graph, yet feedback never
    handed it over.

    **This is the root cause of "no matter how many rounds it runs, it never learns"**:
    `map_fb_conv` (first blood conversion on Haven) was never retrieved in 23 rounds, and
    `map_win_rate` (map win rate) was retrieved only 5 times in 43 rounds. Both are **SQL-path**
    dimensions — the graph's `who_produces` is empty for them, so feedback carries `producers=[]`
    and the model receives the same sentence ("first confirm which tool can produce it, then add a
    call") every round — **not one piece of actionable information** — so round 43 learned exactly as
    much as round 1.

    Yet the graph does hold the complete teaching information (`knowledge_graph.py:1258-1289`):
    `tables` / `columns_by_table` / `semantics` / `typical_errors` — it was rendered into the graph
    prompt up front, and the feedback afterwards used not one word of it. Adding that here.

    What is given is **structure, not answers** (the same boundary as the `action_code` path):
    table names, column names, common mistakes, the shape of the metric definition; no numbers at
    all.
    """
    g = graph()
    if g is None:
        return ""
    node = g.nodes.get(f"dim:{dim}")
    det = dict(node.detail) if node else {}
    tables = [str(t) for t in (det.get("tables") or []) if str(t)]
    if not tables:
        return ""
    cols_map = det.get("columns_by_table") or {}
    cols = [str(c) for c in (det.get("columns") or [])]
    parts = [f"{dim} is a **SQL metric** (no tool produces it directly, you must write the SQL "
             f"yourself)"]
    for t in tables[:2]:
        cs = [str(c) for c in (cols_map.get(t) or cols)]
        parts.append(f"table `{t}`" + (f", relevant columns {', '.join(cs[:8])}" if cs else ""))
    errs = [str(e) for e in (det.get("typical_errors") or [])][:2]
    if errs:
        parts.append("common mistakes: " + "; ".join(errs))
    if isinstance(subject, dict) and subject:
        # Every key of subject must appear in the WHERE clause — this is precisely the antidote to
        # "forgot to filter by map"
        keys = ", ".join(f"{k}={v}" for k, v in subject.items() if v not in (None, ""))
        if keys:
            parts.append(f"the scope of this scoring point is {{{keys}}} — the WHERE clause must "
                         "carry every one of them; querying the aggregate and drilling down "
                         "afterwards is not equivalent")
    sem = str(det.get("semantics") or "")
    if sem:
        parts.append(f"metric definition shape: {sem}")
    return "; ".join(parts) + "."


def _next_action(item: MissingItem, *, ref_plan: list[str] | None = None,
                 my_tools: list[str] | None = None,
                 subject: dict[str, Any] | None = None) -> str:
    dim = item.dimension or item.point
    plan = " → ".join(ref_plan or [])
    tools = ", ".join(my_tools or []) or "(no tools called)"

    if item.error_type == "unmeasured":
        # The class most worth fixing: the dimension simply isn't in the observations, so adding a
        # metric definition is useless — a tool has to be added.
        #
        # Which tool to add comes from two sources, the graph first:
        #   1. the knowledge graph's produced_by edge (declarative: the retrieval recipe declared in
        #      `answer_spec.tool`) — queryable up front, and answerable even before the referee runs;
        #   2. the referee's trajectory difference (reverse-engineered after the fact) — used only
        #      when the graph has no such edge, and it says "the referee called it and I did not",
        #      not "it ought to be produced by it".
        g = graph()
        producers: list[str] = []
        if g is not None:
            try:
                producers = [t for t in g.who_produces(dim) if t not in (my_tools or [])]
            except Exception:
                producers = []
        if producers:
            return (f"{dim} is not in the observations (I submitted null), it is not a computation "
                    f"error — it is **produced by {', '.join(producers)}** (declared by the "
                    f"knowledge graph), and I did not call that tool this time.")
        gap = [t for t in (ref_plan or []) if t not in (my_tools or [])]
        if gap:
            return (f"{dim} is not in the observations (I submitted null), it is not a computation "
                    f"error — a tool that can produce it must be called first; the ones the referee "
                    f"called and I did not are {', '.join(gap)}"
                    + (f" (the referee's full orchestration: {plan})." if plan else "."))
        return (f"{dim} is not in the observations (I submitted null), it is not a computation "
                f"error — the tools currently called are {tools}, they do not produce this "
                f"dimension, so the tool has to change.")

    if item.error_type == "missing_dimension":
        g = graph()
        if g is not None:
            try:
                producers = [t for t in g.who_produces(dim)
                             if t not in (my_tools or [])]
                if producers:
                    return (f"{dim} did not even submit a null — it is **produced by "
                            f"{', '.join(producers)}** (declared by the knowledge graph), "
                            f"add a call to it.")
            except Exception:
                pass
        # The graph names no producer = this is a SQL-path dimension. Previously the code stopped
        # here with just "first confirm which tool can produce it" — which is empty talk for a SQL
        # dimension, and measured unchanged over 43 rounds.
        recipe = _sql_recipe(dim, subject)
        if recipe:
            return recipe
        tail = f" The referee's standard orchestration is {plan}." if plan else ""
        return (f"{dim} has no facts at all, not even a null — first confirm which tool can produce "
                f"it, then add a call.{tail}")

    if item.error_type == "wrong_value":
        # ⚠️ Extension of the isomorphic session's convention ⑧ (user correction on 2026-10-12):
        # the referee's **reference value** must never enter feedback.
        # Measured leak: wrong_value feedback was written as "skuba≠initiator" — the standard answer
        # was handed straight to the model, which copied the answer on the next round, voiding the
        # evidence-gathering path. Only say "the metric definitions disagree", so the model goes back
        # to the environment and re-gathers evidence instead of waiting for the referee to feed it
        # the answer. `got` is the model's own observation and may stay.
        return (f"{dim} was retrieved but disagrees with the referee's metric definition (what I "
                f"submitted was {item.got or '?'}) — this is a problem of filter conditions / scope "
                f"(time range, map, team, round type); **adding tools will not fix it**. Go back "
                f"over the call parameters and re-gather the evidence.")

    if item.error_type == "value_form":
        # Same: give only **form** guidance (numeric vs enum), never the referee's reference value.
        return (f"{dim} has a mismatched form (what I gave was {item.got or '?'}<{item.got_type}>, "
                f"the referee wanted the <{item.ref_type}> form) — align the form of the answer "
                f"(numeric vs enum/string); it is not a wrong number.")

    if item.error_type == "low_base":
        return (f"{dim} was rejected for an insufficient sample size — loosen the filter conditions "
                f"or switch to a broader scope and retrieve again.")

    return (f"{dim}: no evidence at all this round (current tools {tools}), rebuild the "
            f"evidence-gathering path first.")


def _strategy_for(defect: str, *, ref_plan: list[str] | None = None,
                  my_tools: list[str] | None = None) -> str:
    """defect → remedy strategy (the same four as bypass_learn)."""
    if defect == "tool_uncovered":
        # The referee used several tools and I used only one → combine; otherwise switch tool
        if ref_plan and my_tools and len([t for t in ref_plan if t not in my_tools]) >= 1 \
                and len(set(ref_plan)) > 1:
            return "combine_tools"
        return "switch_tool"
    if defect == "missing_dimension":
        return "drill_down"
    if defect == "value_form":
        return "read_scope"
    return "drill_down"


# --------------------------------------------------------------------------
# Feedback bundle
# --------------------------------------------------------------------------
@dataclass
class Feedback:
    """A bundle of grading feedback (fields aligned with the study companion's `answer_evaluate`
    return value)."""
    verdict: str = ""
    score: int = 0
    coverage: float = 0.0
    error_type: str = ""                       # primary error type (the most severe one)
    error_types: list[str] = field(default_factory=list)
    feedback: str = ""                         # plain-language comment
    next_action: str = ""                      # executable next step
    lesson: str = ""                           # what can be crystallised (non-empty only when correct)
    covered_points: list[str] = field(default_factory=list)
    missing_points: list[str] = field(default_factory=list)   # clean scoring point names
    missing_detail: list[dict[str, Any]] = field(default_factory=list)
    misconceptions: list[str] = field(default_factory=list)   # "I thought X"
    step_feedback: list[dict[str, Any]] = field(default_factory=list)
    related_topics: list[str] = field(default_factory=list)   # for the question generator to pick the next question
    reference_plan: list[str] = field(default_factory=list)   # orchestration demo (no values)
    my_plan: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict, "score": self.score,
            "coverage": round(self.coverage, 4),
            "error_type": self.error_type, "error_types": self.error_types,
            "feedback": self.feedback, "next_action": self.next_action,
            "lesson": self.lesson,
            "covered_points": self.covered_points,
            "missing_points": self.missing_points,
            "missing_detail": self.missing_detail,
            "misconceptions": self.misconceptions,
            "step_feedback": self.step_feedback,
            "related_topics": self.related_topics,
            "reference_plan": self.reference_plan, "my_plan": self.my_plan,
            "confidence": round(self.confidence, 4),
        }


def build_feedback(
    *,
    verdict: str = "",
    score: int = 0,
    coverage: float = 0.0,
    covered: list[str] | None = None,
    missing: list[str] | None = None,
    rejected_low_base: list[str] | None = None,
    dim_by_point: dict[str, str] | None = None,
    ref_plan: list[str] | None = None,
    my_plan: list[str] | None = None,
    confidence: float = 0.0,
    subj_by_point: dict[str, dict[str, Any]] | None = None,
) -> Feedback:
    """Assemble a bundle of feedback from the judging result. Pure function: the same input always
    yields the same output.

    `subj_by_point`: scoring point → subject. When an SQL-path dimension is missed, this is what
    lets the feedback say "the WHERE clause must carry a filter like map=Haven" — without it the
    feedback is empty talk.
    """
    subj_by_point = subj_by_point or {}
    covered = [str(c) for c in (covered or [])]
    missing_raw = [str(m) for m in (missing or [])]
    dim_by_point = dim_by_point or {}
    ref_plan = [str(t) for t in (ref_plan or []) if not str(t).startswith("<")]
    my_plan = [str(t) for t in (my_plan or []) if not str(t).startswith("<")]

    items = [parse_missing(m, dim_by_point=dim_by_point) for m in missing_raw]
    # Points rejected for an insufficient sample are added separately — judging puts them in
    # rejected_low_base, not in missing
    for p in (rejected_low_base or []):
        point = str(p).split("(")[0].strip()
        items.append(MissingItem(point=point, dimension=dim_by_point.get(point, point),
                                 error_type="low_base"))

    fb = Feedback(
        verdict=str(verdict), score=int(score), coverage=float(coverage),
        covered_points=covered,
        missing_points=[i.point for i in items],
        missing_detail=[i.to_dict() for i in items],
        reference_plan=ref_plan, my_plan=my_plan, confidence=float(confidence),
    )

    # ⚠️ The criterion must be the **judging result**, not "whether the missing list is empty".
    #
    # Pitfall actually measured: a question with verdict=wrong, coverage=0, covered=[] but missing
    # also [] (the facts were rejected as `below_threshold`, so they took the rejected path rather
    # than the missing path). So the code fell into the "full coverage" branch and printed:
    #     "My fact set covers all 0 of the referee's scoring points; the orchestration is effective.
    #      This orchestration can be crystallised — write it into the skill library and reuse it
    #      directly next time on a similar question."
    # **Reporting failure as success.** This is how it feeds back to the model — the model reads
    # "effective, can be crystallised" every round, and so repeats the very same mistake for five
    # rounds running. This line is precisely where the learning curve's failure to lift comes from:
    # it is not that the model cannot learn, it is that the feedback is lying to it.
    judged_ok = (str(verdict).lower() in ("correct", "ok", "true")
                 or float(coverage) >= 1.0)
    if not items and judged_ok:
        fb.error_type = ""
        fb.error_types = []
        fb.feedback = (f"Covered all {len(covered)} of the referee's scoring points; the "
                       f"orchestration is effective.")
        fb.next_action = ("This orchestration can be crystallised — write it into the skill library "
                          "as 'tool name + which dimension it splits by', and reuse it directly "
                          "next time on a similar question.")
        # lesson must be **this concrete orchestration**, not a template like "fetch the overall
        # report first, then drill down" — a template written into the skill library says nothing at
        # all (that is exactly where it came from: the template at voyager.py:1048).
        dims = sorted({dim_by_point.get(c, c) for c in covered})
        if my_plan:
            fb.lesson = (
                f"Orchestration {' → '.join(my_plan)}: first fetch {my_plan[0]} for the overall "
                f"picture"
                + (f", then complete it with {', '.join(my_plan[1:])}" if len(my_plan) > 1 else "")
                + f". Covered {len(covered)} scoring points"
                + (f" ({', '.join(dims)})" if dims else "")
                + ".")
        else:
            fb.lesson = (f"Covered {len(covered)} scoring points without calling any tool "
                         f"(suspicious — check whether cached facts were used).")
        fb.step_feedback = _step_feedback(my_plan, ref_plan, [])
        fb.related_topics = dims
        return fb

    if not items:
        # **No dimension missing, yet not one fact was accepted** (rejected / wrong value /
        # insufficient evidence).
        # This used to fall through to `ranked[0]` below and raise IndexError directly — and even
        # earlier it was simply treated as "full coverage", broadcasting failure as success. It
        # needs its own branch that states clearly that **nothing was accepted**.
        fb.error_type = "not_accepted"
        fb.error_types = ["not_accepted"]
        # ⚠️ Hard design constraint (isomorphic session convention ⑧): figures such as score /
        # coverage — "verdict numbers produced by running VLML" — **must not enter feedback**,
        # because they push the model's focus away from "retrieve the facts" and towards
        # "game the coverage". Here we only give the **number** of scoring points covered (a count,
        # which cannot be gamed by any single value) and never a percentage. score/coverage stay in
        # the Feedback structure and to_dict() for the dashboard/log.
        fb.feedback = (
            f"Not one of the facts submitted was accepted by the referee (verdict {verdict}, "
            f"covered {len(covered)} scoring points). "
            "**Submitted ≠ evidence obtained** — this does not mean the orchestration is effective.")
        why: list[str] = []
        if rejected_low_base:
            why.append("some facts were rejected for an insufficient sample size ("
                       + ", ".join(str(p) for p in list(rejected_low_base)[:3])
                       + "): loosen the filters or switch to a coarser granularity, don't just "
                         "grab one row")
        if my_plan and ref_plan:
            lack = [t for t in ref_plan if t not in my_plan]
            if lack:
                why.append(f"the referee's orchestration is {' → '.join(ref_plan)}, while you only "
                           f"did {' → '.join(my_plan)} — still missing {', '.join(lack)}")
        fb.next_action = ("First work out why nothing was accepted, then re-run: "
                          + "; ".join(why)) if why else (
            "the facts failed the referee's validation — it is not a missing dimension, it is "
            "**not accepted**. Try a different retrieval approach; re-running the exact same query "
            "as last round is pointless: the same SQL returns the same result.")
        fb.misconceptions.append(
            "I treated \"submitting the facts\" as \"obtaining the evidence\" — yet not one of the "
            "facts submitted was accepted by the referee. Repeating the same orchestration will "
            "not make it right.")
        fb.step_feedback = _step_feedback(my_plan, ref_plan, [])
        fb.related_topics = sorted({dim_by_point.get(c, c) for c in covered})
        return fb

    # Primary error type = the most severe one
    ranked = sorted(items, key=lambda i: -ERROR_SEVERITY.get(i.error_type, 0))
    fb.error_types = sorted({i.error_type for i in items},
                            key=lambda t: -ERROR_SEVERITY.get(t, 0))
    fb.error_type = ranked[0].error_type

    # next_action: ordered by severity, at most two entries (giving more is the same as giving none)
    tops = ranked[:2]
    fb.next_action = " ".join(
        _next_action(i, ref_plan=ref_plan, my_tools=my_plan,
                     subject=subj_by_point.get(i.point)) for i in tops
    )

    # misconceptions: spell out the wrong assumption "I thought orchestrating it this way would
    # cover everything"
    unmeasured = [i for i in items if i.error_type == "unmeasured"]
    if unmeasured:
        tools = ", ".join(my_plan) or "(no tools called)"
        dims = ", ".join(i.dimension or i.point for i in unmeasured)
        fb.misconceptions.append(
            f"I assumed that calling {tools} would cover {dims} — in reality those dimensions are "
            f"not in those tools' output. "
            f"(They were already called and still could not be retrieved, so this is not a matter "
            f"of forgetting to call them: these two tools simply do not produce that dimension.)")
    wrong = [i for i in items if i.error_type == "wrong_value"]
    if wrong:
        # ⚠️ Extension of convention ⑧: never write (got≠ref) — ref is the referee's reference
        # value, and writing it out is equivalent to handing over the answer.
        fb.misconceptions.append(
            "I assumed the number I retrieved is the number under the referee's metric definition — "
            "in reality the filter conditions / scope disagree: "
            + ", ".join(f"{i.dimension or i.point} (the value disagrees with the referee's metric "
                        f"definition)" for i in wrong))
    form = [i for i in items if i.error_type == "value_form"]
    if form:
        fb.misconceptions.append(
            "I assumed the answer has this form — in reality the referee wanted a different one: "
            + ", ".join(f"{i.dimension or i.point}({i.got_type}≠{i.ref_type})" for i in form))

    n_by_type: dict[str, int] = {}
    for i in items:
        n_by_type[i.error_type] = n_by_type.get(i.error_type, 0) + 1
    # ⚠️ Convention ⑧: coverage is a "VLML verdict number" and must not enter feedback (see the
    # not_accepted branch). Here we only give a count such as "number of scoring points covered";
    # exactly which dimensions were missed lives in missing_points.
    fb.feedback = (
        f"Verdict {verdict}: "
        + "; ".join(f"{ERROR_TYPES.get(t, t)} ×{n}" for t, n in
                   sorted(n_by_type.items(), key=lambda kv: -kv[1]))
        + f". Covered {len(covered)} scoring points (no percentage reported)."
    )

    fb.step_feedback = _step_feedback(my_plan, ref_plan, items)
    fb.related_topics = sorted({i.dimension or i.point for i in items})
    return fb


def _step_feedback(my_plan: list[str], ref_plan: list[str],
                   items: list[MissingItem]) -> list[dict[str, Any]]:
    """Step-by-step feedback: what role each tool actually played at its step.

    The study companion's `step_feedback` says per step "was this step right". In MVE a "step" is a
    tool call, so what can be said is: after calling it, which dimensions came in, which still did
    not, and which tools were never called at all.
    """
    out: list[dict[str, Any]] = []
    covered_dims = set()
    for t in my_plan:
        out.append({"tool": t, "used": True, "note": ""})
    for t in ref_plan:
        if t not in my_plan:
            out.append({"tool": t, "used": False,
                        "note": "the referee called it and I did not — the missing dimensions "
                                "most likely come from here"})

    # Tag "called but nothing came out" onto a specific tool: only the last one (the earlier ones
    # cannot be pinned down)
    dims_missing = {i.dimension or i.point for i in items
                    if i.error_type in ("unmeasured", "missing_dimension")}
    if out and dims_missing:
        last_used = [o for o in out if o["used"]]
        if last_used:
            last_used[-1]["note"] = (
                f"it was called, but {', '.join(sorted(dims_missing))} are still not retrieved — "
                f"these dimensions are not produced by it, so the tool has to change")
    _ = covered_dims
    return out


def blueprint_for(fb: Feedback) -> dict[str, Any] | None:
    """Compress the feedback bundle into a blueprint the skill library can carry
    (defect → strategy → action).

    Isomorphic to the blueprints produced by `bypass_learn.py`, only the `source` differs:
    bypass learning distils the referee's approach (`bypass`), while this is what was crystallised
    after getting it right ourselves (`practice`).
    """
    if not fb.error_type and not fb.missing_detail:
        # Correct: what gets crystallised is "this orchestration is effective"
        return {
            "defect": "", "strategy": "combine_tools" if len(fb.my_plan) > 1 else "drill_down",
            "trajectory": fb.my_plan,
            "dimensions": [d for d in fb.related_topics],
            "source": "practice",
        }
    defect = ERROR_TO_DEFECT.get(fb.error_type, "missing_dimension")
    return {
        "defect": defect,
        "strategy": _strategy_for(defect, ref_plan=fb.reference_plan, my_tools=fb.my_plan),
        "trajectory": fb.reference_plan or fb.my_plan,
        "dimensions": [d.get("dimension") for d in fb.missing_detail],
        "source": "practice",
    }


def render_critique(fb: Feedback) -> str:
    """Render the feedback bundle as the critique text handed to the model (fed into the next
    round's prompt).

    Follows the forked critic's shape: **first-person self-reflection**. But no longer a single
    template — it now carries the error_type attribution and next_action's concrete move.
    """
    if not fb.error_type:
        return (f"My fact set covers all {len(fb.covered_points)} of the referee's scoring points; "
                f"the orchestration is effective. {fb.next_action}")
    head = (f"I thought orchestrating it this way would cover every scoring point, but after the "
            f"referee's comparison the verdict was {fb.verdict}.")
    body = fb.feedback
    mis = (" ".join(f"Wrong assumption: {m}" for m in fb.misconceptions)) if fb.misconceptions else ""
    act = f"Next step: {fb.next_action}"
    return " ".join(x for x in (head, body, mis, act) if x)


# --------------------------------------------------------------------------
# Offline recomputation: rebuild the feedback from one run_log row (used to verify "is the structure
# right")
# --------------------------------------------------------------------------
def dim_map_for(topic_id: str) -> dict[str, str]:
    """Scoring point original text → dimension.

    Offline recomputation must supply this mapping: the `missing` entries stored in run_log are the
    **original scoring point text** (e.g. "eco round win rate (a percentage value 0-100, such as
    42.9)"), which is unusable as a dimension name; the real dimension names live in the rubric
    (`eco_win_rate`).
    """
    try:
        from tasks import TASKS
    except Exception:
        return {}
    task = TASKS.get(str(topic_id or ""))
    if task is None:
        return {}
    return {p.point: p.dimension for p in getattr(task, "rubric", [])}


def from_row(row: dict[str, Any]) -> Feedback:
    """Rebuild the feedback bundle from one run_log row — no model re-run, structure recomputed
    only."""
    topic = str(row.get("topic_id") or "")
    return build_feedback(
        verdict=str(row.get("verdict") or ""),
        score=int(row.get("score") or 0),
        coverage=float(row.get("coverage") or 0.0),
        covered=[str(c) for c in (row.get("covered") or [])],
        missing=[str(m) for m in (row.get("missing") or [])],
        rejected_low_base=[str(p) for p in (row.get("rejected_low_base") or [])],
        dim_by_point=dim_map_for(topic),
        ref_plan=[str(t) for t in (row.get("ref_plan") or [])],
        my_plan=[str(t) for t in (row.get("trajectory") or [])],
        confidence=float(row.get("evaluator_confidence") or 0.0),
    )


def _main() -> int:
    ap = argparse.ArgumentParser(description="Grading feedback structure: offline recomputation + "
                                             "replay")
    ap.add_argument("--topic", default="", help="only look at this question; all of them if omitted")
    ap.add_argument("--limit", type=int, default=5, help="replay the most recent N entries")
    ap.add_argument("--json", action="store_true", help="print the raw JSON")
    args = ap.parse_args()

    import run_log
    rows = run_log.load_all()
    if args.topic:
        rows = [r for r in rows if str(r.get("topic_id") or "") == args.topic]
    rows = rows[-args.limit:]
    if not rows:
        print("No log entries to replay")
        return 1

    for r in rows:
        fb = from_row(r)
        if args.json:
            print(json.dumps(fb.to_dict(), ensure_ascii=False))
            continue
        print(f"[{r.get('ts')}] {r.get('topic_id')} · {fb.verdict} · "
              f"coverage {fb.coverage:.0%}")
        print(f"  Criteria     : {fb.feedback}")
        if fb.error_type:
            print(f"  Error type   : {fb.error_type}  ({ERROR_TYPES.get(fb.error_type, '')})")
        print(f"  Next step    : {fb.next_action}")
        for m in fb.misconceptions:
            print(f"  Wrong assumption: {m}")
        print(f"  My orchestration: {' → '.join(fb.my_plan) or '(none)'}")
        print(f"  Referee orchestration: {' → '.join(fb.reference_plan) or '(none)'}")
        for s in fb.step_feedback:
            flag = "✓called" if s["used"] else "✗not called"
            print(f"  Step         : {flag} {s['tool']}"
                  + (f" — {s['note']}" if s["note"] else ""))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
