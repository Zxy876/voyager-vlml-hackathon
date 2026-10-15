#!/usr/bin/env python3
"""Mastery model V2 — ported from the study-companion plugin's `adaptive_learning/mastery_v2.py`.

Why the old one was replaced: the old version mirrored the plugin's **V1** (`knowledge_tracker.py:252-286`),
and V1 had two hard flaws, both of which the plugin itself already fixed in V2:

  1. `confidence = 1-exp(-attempts/5)` — **counts answer attempts only**,
     so grinding attempts pushes mastery up (Voyager runs dozens of rounds in a night, far faster than a human).
     V2 computes it from the **sum of evidence weights** instead (mastery_v2.py:278).
  2. `recency = 1.0` — **hard-coded** in V1 (knowledge_tracker.py:266),
     i.e. "a perfect score three months ago is equivalent to one just now". V2 introduces `time_decay`,
     with a half-life of 60 days (mastery_v2.py:43, :416-424).

Three things V2 has that V1 lacks:
  3. **Each piece of evidence has a weight** = evaluator confidence × answer reliability × time decay (:335).
     So "judged by an LLM" is less trustworthy than "computed by SQL", and "answered too frugally" gets discounted.
  4. **Weighted average** replaces the simple average; accuracy / variance / quality are all computed by weight.
  5. **Reconstructible**: `as_of` passed explicitly, versioned policy, attempt_id deduplication —
     the same batch of facts must yield the same number whether accumulated incrementally or recomputed in one go (:208-221).

The plugin itself marks V2 as **shadow** (mastery_v2.py:1-7): V1 is still live in production,
with V2 running in parallel for reconciliation. We promote it to live directly — because MVE has
no "production users" to stay compatible with.

Two MVE-specific adaptations (no counterpart in the plugin, mapped isomorphically):
  - `response_time_ms` (human answer time) → **`tool_calls`** (how many tools Voyager called successfully).
    Same semantics: both mean "how much this answer invested", and too fast / too frugal means unreliable.
  - `evaluator_confidence` (evaluator trustworthiness) → **the share of deterministic referee facts**.
    The plugin only has "default 0.75"; MVE's referee facts have clear sources
    (`vlml0_referee.deterministic` / `vlml0_referee::tool` / `vlml0_referee.llm`),
    mapped to [0.6, 0.95] by the share of deterministic facts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

MODEL_VERSION = "mve-mastery-v2-1"

MASTERY_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
MASTERY_PROGRESSING = "progressing"
MASTERY_MASTERED = "mastered"


@dataclass(frozen=True)
class MasteryPolicy:
    """All coefficients live on a single immutable object — mirroring the plugin's mastery_v2.py:21-95.

    The point of centralizing them here (the plugin's original text): historical reconstruction is
    auditable, and callers cannot quietly change model behavior.
    """

    model_version: str = MODEL_VERSION
    correct_default_score: float = 1.0
    partial_default_score: float = 0.5
    wrong_default_score: float = 0.0
    difficulty_modifier_min: float = 0.9
    difficulty_modifier_max: float = 1.1
    hint_used_modifier: float = 0.85
    hint_not_used_modifier: float = 1.0
    hint_unknown_modifier: float = 1.0
    evaluator_confidence_default: float = 0.75
    response_time_min_reliability: float = 0.6
    response_time_suspicious_ms: int = 1_000
    response_time_full_reliability_ms: int = 5_000
    # MVE isomorphic term: tool-call depth (the plugin uses response_time_ms; we have no "answer time")
    depth_min_reliability: float = 0.6
    depth_full_calls: int = 2
    time_decay_half_life_days: float = 60.0
    # ---- MVE: decay by **session** (the plugin uses days, see the comment below) ----
    # The plugin's time unit is days (:43), because its learner "comes back to review every other day".
    # Voyager is not: it can run dozens of rounds in one night, so day-based recency is always 1.000 —
    # equivalent to no decay at all.
    # But conversely, round 1 and round 40 within the same continuous experiment shouldn't decay each
    # other either (that's one practice session).
    # So cut by **session**: one continuous stretch of operation = one session, and only a long enough
    # gap counts as the next.
    time_decay_basis: str = "session"        # "session" | "day"
    time_decay_half_life_sessions: float = 3.0
    session_gap_hours: float = 2.0
    consistency_floor: float = 0.7
    consistency_span: float = 0.3
    confidence_evidence_scale: float = 4.0
    confidence_floor: float = 0.5
    confidence_span: float = 0.5
    mastered_threshold: float = 0.8
    unresolved_wrong_mastery_cap: float = 0.79
    rounding_digits: int = 6
    # Three-state threshold (the plugin's practice_outcome.py:14)
    mastery_min_attempts: int = 3

    def __post_init__(self) -> None:
        unit_fields = (
            self.correct_default_score, self.partial_default_score,
            self.wrong_default_score, self.hint_used_modifier,
            self.hint_not_used_modifier, self.hint_unknown_modifier,
            self.evaluator_confidence_default, self.response_time_min_reliability,
            self.depth_min_reliability, self.consistency_floor,
            self.consistency_span, self.confidence_floor, self.confidence_span,
            self.mastered_threshold, self.unresolved_wrong_mastery_cap,
        )
        if not self.model_version.strip():
            raise ValueError("model_version is required")
        if any(not math.isfinite(v) or not 0.0 <= v <= 1.0 for v in unit_fields):
            raise ValueError("mastery policy probabilities must be finite values in [0, 1]")
        if not 0.0 <= self.difficulty_modifier_min <= self.difficulty_modifier_max:
            raise ValueError("difficulty modifiers must be ordered and non-negative")
        if not math.isfinite(self.time_decay_half_life_days) or self.time_decay_half_life_days <= 0:
            raise ValueError("time_decay_half_life_days must be positive")
        if not math.isfinite(self.confidence_evidence_scale) or self.confidence_evidence_scale <= 0:
            raise ValueError("confidence_evidence_scale must be positive")
        if self.consistency_floor + self.consistency_span > 1.0:
            raise ValueError("consistency factor must not exceed 1")
        if self.confidence_floor + self.confidence_span > 1.0:
            raise ValueError("confidence factor must not exceed 1")
        # The plugin added an assertion for this (mastery_v2.py:92): the cap must be below the graduation
        # threshold, otherwise the rule "no graduating with undigested wrong questions" is void.
        if self.unresolved_wrong_mastery_cap >= self.mastered_threshold:
            raise ValueError("unresolved wrong mastery cap must be below the mastered threshold")


DEFAULT_POLICY = MasteryPolicy()


@dataclass(frozen=True)
class MasteryEvidence:
    """One immutable assessment fact (mirroring the plugin's mastery_v2.py:101-118)."""

    attempt_id: str
    verdict: str
    score: float | int | None
    difficulty: float | int | None
    used_hint: bool | None
    response_time_ms: int | None
    evaluator_confidence: float | None
    submitted_at: datetime | str
    # MVE-specific: how many tools this answer called successfully (the plugin expresses the same thing via response_time_ms)
    tool_calls: int | None = None

    def __post_init__(self) -> None:
        if not str(self.attempt_id or "").strip():
            raise ValueError("attempt_id is required")
        if self.used_hint is not None and not isinstance(self.used_hint, bool):
            raise TypeError("used_hint must be bool or None")


@dataclass(frozen=True)
class MasterySnapshot:
    topic_id: str
    mastery: float
    accuracy: float
    recency: float
    consistency: float
    confidence: float
    evidence_count: int
    unresolved_wrong_count: int
    level: str            # five tiers (MVE keeps the plugin V1's tier names; the dashboard uses them)
    status: str           # three states (practice_outcome.py:7-9)
    flags: tuple[str, ...] = ()
    mastery_model_version: str = MODEL_VERSION
    computed_at: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "topic_id": self.topic_id,
            "mastery": self.mastery,
            "accuracy": self.accuracy,
            "recency": self.recency,
            "consistency": self.consistency,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "unresolved_wrong_count": self.unresolved_wrong_count,
            "level": self.level,
            "status": self.status,
            "flags": list(self.flags),
            "mastery_model_version": self.mastery_model_version,
            "computed_at": self.computed_at,
        }


@dataclass(frozen=True)
class _Prepared:
    submitted_at: datetime
    normalized_score: float
    attempt_quality: float
    evidence_weight: float
    time_decay: float
    session_index: int = 0


def assign_sessions(times: Iterable[datetime], gap_hours: float) -> list[int]:
    """Split a series of timestamps into sessions: a gap > gap_hours between neighbors starts a new session.

    Depends only on timestamps → **reconstructible** (no extra "session start/end" state needed).
    This is also why sessions aren't toggled manually: manual toggling leaves state behind, and V2's
    whole value is "the same batch of facts can recompute the same number at any time".
    """
    ordered = sorted(t for t in times if isinstance(t, datetime))
    out: list[int] = []
    idx = 0
    prev: datetime | None = None
    for t in ordered:
        if prev is not None and (t - prev).total_seconds() > gap_hours * 3600.0:
            idx += 1
        out.append(idx)
        prev = t
    return out


def calculate_mastery(
    topic_id: str,
    evidence: Iterable[MasteryEvidence],
    *,
    unresolved_wrong_count: int = 0,
    has_active_wrong_question: bool = False,
    as_of: datetime | str | None = None,
    policy: MasteryPolicy = DEFAULT_POLICY,
) -> MasterySnapshot:
    """Project mastery from all of a question's facts (mirroring the plugin's mastery_v2.py:208-303).

    `as_of` is passed explicitly instead of reading the clock — the plugin's original text: only then
    is reconstruction deterministic.
    """
    resolved_topic = str(topic_id or "").strip()
    if not resolved_topic:
        raise ValueError("topic_id is required")
    if isinstance(unresolved_wrong_count, bool):
        raise TypeError("unresolved_wrong_count must be an integer")
    wrong_count = max(0, int(unresolved_wrong_count))
    projection_time = _coerce_datetime(as_of) if as_of is not None else datetime.now(timezone.utc)

    unique = _deduplicate(evidence)
    ordered = sorted(unique, key=lambda e: (_coerce_datetime(e.submitted_at, default=projection_time),
                                            e.attempt_id))
    # Session splitting: evidence within one continuous stretch of operation doesn't decay each other;
    # only a long enough gap moves to the next session.
    sessions = assign_sessions(
        [_coerce_datetime(e.submitted_at, default=projection_time) for e in ordered],
        policy.session_gap_hours,
    )
    current_session = sessions[-1] if sessions else 0
    if ordered:
        last_ts = _coerce_datetime(ordered[-1].submitted_at, default=projection_time)
        if (projection_time - last_ts).total_seconds() > policy.session_gap_hours * 3600.0:
            current_session += 1

    prepared = [
        _prepare(item, as_of=projection_time, policy=policy,
                 session_age=current_session - sessions[i])
        for i, item in enumerate(ordered)
    ]
    evidence_count = len(prepared)
    flags: list[str] = []

    if not prepared:
        flags.append("no_evidence")
        return MasterySnapshot(
            topic_id=resolved_topic, mastery=0.0, accuracy=0.0, recency=0.0,
            consistency=0.0, confidence=0.0, evidence_count=0,
            unresolved_wrong_count=wrong_count, level=_level(0.0),
            status=MASTERY_INSUFFICIENT_EVIDENCE, flags=tuple(flags),
            mastery_model_version=policy.model_version,
            computed_at=_format_datetime(projection_time),
        )

    total_weight = sum(p.evidence_weight for p in prepared)
    if total_weight > 0.0:
        accuracy = sum(p.normalized_score * p.evidence_weight for p in prepared) / total_weight
        weighted_quality = sum(p.attempt_quality * p.evidence_weight for p in prepared) / total_weight
        variance = sum(
            p.evidence_weight * (p.normalized_score - accuracy) ** 2 for p in prepared
        ) / total_weight
    else:
        accuracy = 0.0
        weighted_quality = 0.0
        variance = 0.25
        flags.append("zero_confidence_evidence")

    consistency = _clamp(1.0 - 2.0 * math.sqrt(max(0.0, variance)))
    recency = sum(p.time_decay for p in prepared) / evidence_count
    confidence = 1.0 - math.exp(-total_weight / policy.confidence_evidence_scale)
    consistency_factor = policy.consistency_floor + policy.consistency_span * consistency
    confidence_factor = policy.confidence_floor + policy.confidence_span * confidence
    mastery = _clamp(weighted_quality * consistency_factor * confidence_factor)

    if wrong_count > 0:
        mastery = min(mastery, policy.unresolved_wrong_mastery_cap)
        flags.append("unresolved_wrong_cap")

    rounded = _round_unit(mastery, policy)
    # The three states mirror the plugin's practice_outcome.py:41-45.
    # Note the plugin's pitfall here: the V2 snapshot has no `attempts` field, yet practice_outcome
    # reads exactly that → attempts=0 < 3 → V2 would always be judged insufficient_evidence.
    # We fill that slot with evidence_count (same semantics: how many pieces of evidence).
    if evidence_count < policy.mastery_min_attempts:
        status = MASTERY_INSUFFICIENT_EVIDENCE
    elif rounded >= policy.mastered_threshold and not (
        wrong_count > 0 or has_active_wrong_question
    ):
        status = MASTERY_MASTERED
    else:
        status = MASTERY_PROGRESSING

    return MasterySnapshot(
        topic_id=resolved_topic,
        mastery=rounded,
        accuracy=_round_unit(accuracy, policy),
        recency=_round_unit(recency, policy),
        consistency=_round_unit(consistency, policy),
        confidence=_round_unit(confidence, policy),
        evidence_count=evidence_count,
        unresolved_wrong_count=wrong_count,
        level=_level(rounded),
        status=status,
        flags=tuple(flags),
        mastery_model_version=policy.model_version,
        computed_at=_format_datetime(projection_time),
    )


def _level(mastery: float) -> str:
    """Five tiers, mirroring the plugin's knowledge_tracker.py:240-250."""
    v = _clamp(float(mastery or 0.0))
    if v < 0.20:
        return "not started"
    if v < 0.40:
        return "weak"
    if v < 0.60:
        return "in progress"
    if v < 0.80:
        return "proficient"
    return "mastered"


def _deduplicate(evidence: Iterable[MasteryEvidence]) -> tuple[MasteryEvidence, ...]:
    """Duplicate submissions with the same attempt_id: ignore if the facts agree, error on conflict (plugin :306-315)."""
    unique: dict[str, MasteryEvidence] = {}
    for item in evidence:
        prev = unique.get(item.attempt_id)
        if prev is not None and prev != item:
            raise ValueError(f"conflicting facts for attempt_id: {item.attempt_id}")
        unique[item.attempt_id] = item
    return tuple(unique.values())


def _prepare(
    item: MasteryEvidence,
    *,
    as_of: datetime,
    policy: MasteryPolicy,
    session_age: int = 0,
) -> _Prepared:
    submitted_at = _coerce_datetime(item.submitted_at, default=as_of)
    normalized_score = _normalized_score(item.verdict, item.score, policy)
    difficulty_modifier = _difficulty_modifier(item.difficulty, policy)
    hint_modifier = _hint_modifier(item.used_hint, policy)
    attempt_quality = _clamp(normalized_score * difficulty_modifier * hint_modifier)
    evaluator_confidence = _finite_unit(
        item.evaluator_confidence, default=policy.evaluator_confidence_default
    )
    # The plugin is response_time_reliability × (no depth term);
    # MVE uses tool-call depth for the same thing: submitting after a single aggregate report call → unreliable.
    depth_reliability = _depth_reliability(item.tool_calls, policy)
    time_decay = _decay(submitted_at, as_of=as_of, policy=policy, session_age=session_age)
    evidence_weight = _clamp(evaluator_confidence * depth_reliability * time_decay)
    return _Prepared(
        submitted_at=submitted_at,
        normalized_score=normalized_score,
        attempt_quality=attempt_quality,
        evidence_weight=evidence_weight,
        time_decay=time_decay,
        session_index=session_age,
    )


def _normalized_score(verdict: str, score: float | int | None, policy: MasteryPolicy) -> float:
    """Mirroring the plugin's mastery_v2.py:346-368.

    The key difference from V1: **if there's a concrete score, use the score** (V1 only recognizes
    the verdict's three tiers). MVE's score is coverage (0-100), so a 45% partial and a 75%
    partial don't both get the same 0.6 — this directly eliminates V1's information loss.
    """
    normalized_verdict = str(verdict or "").strip().lower()
    fallback = {
        "correct": policy.correct_default_score,
        "partial": policy.partial_default_score,
        "wrong": policy.wrong_default_score,
        "dont_know": policy.wrong_default_score,
    }.get(normalized_verdict, policy.wrong_default_score)
    if normalized_verdict in {"wrong", "dont_know"}:
        return policy.wrong_default_score
    if isinstance(score, bool) or score is None:
        return fallback
    try:
        numeric = float(score)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(numeric):
        return fallback
    return _clamp(numeric / 100.0)


def _difficulty_modifier(difficulty: float | int | None, policy: MasteryPolicy) -> float:
    """Plugin's mastery_v2.py:371-388; question difficulty uses the server-side 1..5 tiers."""
    if isinstance(difficulty, bool) or difficulty is None:
        normalized = 0.5
    else:
        try:
            numeric = float(difficulty)
        except (TypeError, ValueError, OverflowError):
            numeric = 3.0
        if not math.isfinite(numeric):
            numeric = 3.0
        normalized = _clamp((numeric - 1.0) / 4.0)
    return policy.difficulty_modifier_min + (
        policy.difficulty_modifier_max - policy.difficulty_modifier_min
    ) * normalized


def _hint_modifier(used_hint: bool | None, policy: MasteryPolicy) -> float:
    if used_hint is None:
        return policy.hint_unknown_modifier
    return policy.hint_used_modifier if used_hint else policy.hint_not_used_modifier


def _response_time_reliability(response_time_ms: int | None, policy: MasteryPolicy) -> float:
    """Mirroring the plugin's mastery_v2.py:399-413 (MVE doesn't pass answer time for now; kept to align the structure)."""
    if isinstance(response_time_ms, bool) or response_time_ms is None or response_time_ms < 0:
        return 1.0
    if response_time_ms <= policy.response_time_suspicious_ms:
        return policy.response_time_min_reliability
    if response_time_ms >= policy.response_time_full_reliability_ms:
        return 1.0
    span = policy.response_time_full_reliability_ms - policy.response_time_suspicious_ms
    progress = (response_time_ms - policy.response_time_suspicious_ms) / span
    return policy.response_time_min_reliability + (
        1.0 - policy.response_time_min_reliability
    ) * progress


def _depth_reliability(tool_calls: int | None, policy: MasteryPolicy) -> float:
    """The isomorphic version of `_response_time_reliability`: swapping "answer time" for "tool-call depth".

    Only 1 successful tool call (e.g. reporting four numbers off a single aggregate report) → unreliable,
    taking the same lower bound 0.6 as the plugin does for "submitting in 1 second".
    """
    if isinstance(tool_calls, bool) or tool_calls is None or tool_calls < 0:
        return 1.0
    if tool_calls <= 1:
        return policy.depth_min_reliability
    return 1.0


def _decay(
    submitted_at: datetime,
    *,
    as_of: datetime,
    policy: MasteryPolicy,
    session_age: int = 0,
) -> float:
    """Time-decay dispatch: MVE defaults to **sessions**, the plugin's convention is days (switchable back).

    By session: `2^(-session age / half-life in sessions)`. Within the same session = 1 (no decay),
    3 sessions apart = 0.5. How long a gap counts as one session is set by `session_gap_hours`.

    Use `2.0 ** x` rather than `math.exp2`: the latter was only added in Python 3.11,
    and observed on a 3.10 interpreter it raises `AttributeError: module 'math' has no
    attribute 'exp2'` outright, crashing the whole round at update_mastery — the mastery model
    shouldn't depend on the interpreter version.
    """
    if policy.time_decay_basis == "day":
        return _time_decay(submitted_at, as_of=as_of, policy=policy)
    return _clamp(2.0 ** (-max(0, session_age)
                          / policy.time_decay_half_life_sessions))


def _time_decay(submitted_at: datetime, *, as_of: datetime, policy: MasteryPolicy) -> float:
    """Plugin's mastery_v2.py:416-424: half-life of 60 days (a human's review rhythm).

    Basically inert for MVE — Voyager runs dozens of rounds in a night, so 60 days ≈ zero decay,
    which is why the default is `time_decay_basis = "session"`.
    """
    age_days = max(0.0, (as_of - submitted_at).total_seconds()) / 86_400.0
    return _clamp(math.exp(-math.log(2.0) * age_days / policy.time_decay_half_life_days))


def _finite_unit(value: float | int | None, *, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return _clamp(default)
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return _clamp(default)
    return _clamp(numeric) if math.isfinite(numeric) else _clamp(default)


def _clamp(value: float) -> float:
    return 0.0 if not math.isfinite(value) else min(1.0, max(0.0, value))


def _round_unit(value: float, policy: MasteryPolicy) -> float:
    return round(_clamp(value), policy.rounding_digits)


def _coerce_datetime(value: datetime | str, *, default: datetime | None = None) -> datetime:
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            if default is None:
                raise ValueError("datetime value is required")
            return default
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            if default is None:
                raise
            return default
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def evidence_from_row(row: dict, *, default_difficulty: float = 3.0) -> MasteryEvidence:
    """Reconstruct one piece of evidence from a run_log row — for cross-process reconstruction.

    Evidence must be **fully** reconstructible from the log: otherwise, with every run a new process,
    weights/time decay would only ever see the current round.
    """
    ts = str(row.get("ts") or "")
    round_no = row.get("round")
    return MasteryEvidence(
        attempt_id=f"{ts}#{round_no}",
        verdict=str(row.get("verdict") or ""),
        score=row.get("score"),
        difficulty=row.get("difficulty", default_difficulty),
        used_hint=None,
        response_time_ms=None,
        evaluator_confidence=row.get("evaluator_confidence"),
        submitted_at=ts,
        tool_calls=row.get("tool_calls"),
    )


# ---------------------------------------------------------------------------
# Dimension-level mastery — the isomorphic counterpart of the plugin's readiness
# ---------------------------------------------------------------------------
# The plugin's `readiness_in_scope` (knowledge_tracker.py:435-503) is a
# **joint "knowledge graph + mastery" query**:
#     mastery vs required_mastery → ready / blockers
# There, mastery hangs off the **knowledge graph node** (topic), so granularities align naturally —
# which is why the plugin's graph can answer "can this knowledge point be tested right now, and at
# what difficulty".
#
# MVE's granularity mismatch: mastery is computed per **topic** (`calculate_mastery`),
# while the knowledge graph nodes are **dim:*** — so the graph can't do plugin-style readiness
# queries, and the question generator can only read the **static seed difficulty** on nodes,
# not "where Voyager is at now". This is one of the root causes of the learning curve's 0→100 step
# function: the gradient is missing the mastery dimension.
#
# Here topic-level mastery is **projected onto dimensions**:
#     dimension d's mastery = mean score of all **exam** questions that involve d
# ⚠️ Only exam_log counts. run_log is practice run with the knowledge graph, where coverage is always
#    100% (a flat fake line hugging the top); computing mastery from it treats the scaffold as
#    ability (a core project design; see the dual-log section of the README).
# ---------------------------------------------------------------------------
_DIM_MASTERY_CACHE: dict[str, dict[str, Any]] = {}


def dimension_mastery(*, reload: bool = False) -> dict[str, dict[str, Any]]:
    """{dimension: {mastery, n, last, topics}} — exam-based, not practice."""
    if _DIM_MASTERY_CACHE and not reload:
        return _DIM_MASTERY_CACHE
    import json as _json
    from pathlib import Path as _P
    dims_of: dict[str, set[str]] = {}
    try:
        from tasks import TASKS
        for tid, t in TASKS.items():
            for p in (getattr(t, "rubric", None) or []):
                d = str(getattr(p, "dimension", "") or "").strip()
                if d:
                    dims_of.setdefault(str(tid), set()).add(d)
    except Exception:
        dims_of = {}
    rows: list[dict[str, Any]] = []
    p = _P(__file__).resolve().parent / "exam_log.jsonl"
    if p.exists():
        rows = [_json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                if l.strip()]
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        tid = str(r.get("topic_id") or "")
        try:
            sc = float(r.get("score") or 0) / 100.0
        except (TypeError, ValueError):
            continue
        for d in dims_of.get(tid, ()):
            rec = out.setdefault(d, {"n": 0, "sum": 0.0, "last": sc,
                                     "topics": set()})
            rec["n"] += 1
            rec["sum"] += sc
            rec["last"] = sc
            rec["topics"].add(tid)
    for d, rec in out.items():
        rec["mastery"] = round(rec["sum"] / max(1, int(rec["n"])), 4)
        rec["topics"] = sorted(rec["topics"])
    _DIM_MASTERY_CACHE.clear()
    _DIM_MASTERY_CACHE.update(out)
    return out
