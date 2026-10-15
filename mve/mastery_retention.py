#!/usr/bin/env python3
"""**Retention** of mastery (you forget) — modeled on the study-companion plugin's `adaptive_learning/mastery_retention.py`.

V2's projection answers "how well is it mastered right now"; this layer answers "**how much is left after a while**".
The plugin's original text (:1-5): this is an experimental heuristic, not FSRS and not a trained HLR model.

Two formulas (plugin :27-54):
    current_mastery = baseline × 2^(-days / half_life)
    half_life' = half_life × (1 ± confidence × weight)   # correct lengthens, wrong shortens

One MVE adaptation (`used_hint`): the plugin uses it to judge "was this correct answer earned alone or with a hint";
without it the half-life never moves (plugin :42-43 returns as-is). Voyager doesn't take hints,
but it **does take the skill library** — injecting skill experience this round ≈ answering correctly with a hint,
which is the isomorphic term: `used_hint ≡ skill-library experience injected this round`.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "mastery_retention.jsonl"

MODEL_VERSION = "mve-retention-1"
# The plugin uses 7.0 **days** (a human's review cycle is "every other day"). MVE's time unit goes by **sessions**
# — the same unit as mastery_model.MasteryPolicy.time_decay_half_life_sessions,
# otherwise one side decays per session and the other forgets per day, and the two don't reconcile.
INITIAL_HALF_LIFE = 3.0
SESSION_GAP_HOURS = 2.0
ZERO_THRESHOLD = 0.001
ACQUIRE_THRESHOLD = 0.01


def finite_fraction(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a mastery fraction")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("expected a finite fraction in [0, 1]")
    return result


def current_mastery(baseline: float, half_life: float, elapsed_sessions: float) -> float:
    """Plugin mastery_retention.py:27-34 (with "days" swapped for "sessions")."""
    baseline = finite_fraction(baseline)
    if not math.isfinite(half_life) or half_life <= 0:
        raise ValueError("invalid half life")
    if not math.isfinite(elapsed_sessions) or elapsed_sessions < 0:
        raise ValueError("invalid elapsed sessions")
    # Use `2.0 ** x` rather than `math.exp2`: the latter was only added in Python 3.11,
    # and on a 3.10 interpreter it raises AttributeError outright. AttributeError is not
    # within the caller's (`ValueError, TypeError`) catch scope — observed: every round threw
    # at apply_attempt and the retention had **never been updated** (that line in the log:
    # "retention not updated: module 'math' has no attribute 'exp2'").
    # Keep the same fix as mastery_model._decay.
    value = baseline * 2.0 ** (-elapsed_sessions / half_life)
    return 0.0 if value < ZERO_THRESHOLD else value


def feedback_half_life(
    half_life: float,
    elapsed_sessions: float,
    verdict: str,
    confidence: float | None,
    used_hint: bool | None,
) -> float:
    """Plugin mastery_retention.py:37-54: correct (without a hint) lengthens, wrong shortens.

    `elapsed_sessions` = how many sessions have passed since the last answer. Answering again within
    the same session → 0, half-life unchanged (consecutive attempts within one practice session don't
    count as "another review").
    """
    current_mastery(1.0, half_life, elapsed_sessions)
    if confidence is None or used_hint is None:
        return half_life
    confidence = finite_fraction(confidence)
    weight = -math.expm1(-math.log(2) * elapsed_sessions / half_life)
    if verdict == "correct" and used_hint is False:
        result = half_life * (1 + confidence * weight)
    elif verdict in {"wrong", "dont_know"}:
        result = half_life * (1 - 0.5 * confidence * weight)
    else:
        result = half_life
    if not math.isfinite(result) or result <= 0:
        raise ValueError("half life exceeded numeric range")
    return result


def evidence_baseline(scores: list[float]) -> float:
    """Plugin :57-70 — a V1-style consistency estimate, used as the baseline for retention.

    Original comment: no calendar weighting here, because "time is applied exactly once, in the retention layer";
    hint discounts are handled by the evidence collector, not this function's responsibility.
    """
    values = [finite_fraction(s) for s in scores[-10:]]
    if not values:
        raise ValueError("missing mastery evidence")
    accuracy = sum(values) / len(values)
    variance = sum((s - accuracy) ** 2 for s in values) / len(values)
    consistency = max(0.0, 1 - 2 * math.sqrt(variance))
    support = 1 - math.exp(-len(values) / 5)
    return min(1.0, accuracy * (0.6 + 0.4 * consistency) * (0.55 + 0.45 * support))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce(value: Any, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return default or _now()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return default or _now()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class RetentionStore:
    """One row per question: baseline (level when learned) + half_life (how long it lasts)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or LOG)

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        out: dict[str, dict[str, Any]] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            topic = str(row.get("topic_id") or "")
            if topic:
                out[topic] = row
        return out

    def get(self, topic_id: str) -> dict[str, Any]:
        return self.load().get(str(topic_id or ""), {
            "topic_id": str(topic_id or ""),
            "baseline": 0.0,
            "half_life": INITIAL_HALF_LIFE,
            "updated_at": "",
        })

    def upsert(self, topic_id: str, *, baseline: float, half_life: float,
               updated_at: datetime | None = None,
               session_index: int | None = None) -> dict[str, Any]:
        rows = self.load()
        prev = rows.get(str(topic_id)) or {}
        row = {
            "topic_id": str(topic_id),
            "baseline": round(float(baseline), 6),
            "half_life": round(float(half_life), 6),
            "updated_at": _coerce(updated_at or _now()).isoformat().replace("+00:00", "Z"),
            # Session index: only used to count "how many sessions have passed", not part of the mastery computation itself
            "session_index": int(prev.get("session_index") or 0)
            if session_index is None else int(session_index),
        }
        rows[str(topic_id)] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            for r in rows.values():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return row

    def apply_attempt(
        self,
        topic_id: str,
        *,
        verdict: str,
        scores: list[float],
        confidence: float | None,
        used_hint: bool | None,
        as_of: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Update this question's retention parameters after a round ends."""
        now = _coerce(as_of) if as_of is not None else _now()
        prev = self.get(topic_id)
        # How many sessions have passed: judged only by "the gap since the last answer".
        # Limitation: with no activity in between there's no way to know how many segments elapsed;
        # always count 1 — a conservative estimate.
        elapsed_sessions = 0.0
        session_index = int(prev.get("session_index") or 0)
        if prev.get("updated_at"):
            gap_hours = (now - _coerce(prev["updated_at"])).total_seconds() / 3600.0
            if gap_hours > SESSION_GAP_HOURS:
                elapsed_sessions = 1.0
                session_index += 1
        try:
            baseline = evidence_baseline(scores)
        except (ValueError, TypeError):
            baseline = float(prev.get("baseline") or 0.0)
        half_life = float(prev.get("half_life") or INITIAL_HALF_LIFE)
        try:
            half_life = feedback_half_life(
                half_life, elapsed_sessions, str(verdict or ""), confidence, used_hint
            )
        except (ValueError, TypeError):
            pass
        return self.upsert(topic_id, baseline=baseline, half_life=half_life,
                           updated_at=now, session_index=session_index)

    def current(self, topic_id: str, *, as_of: datetime | str | None = None) -> dict[str, Any]:
        """How much is **left right now** for this question (including the forgotten part)."""
        now = _coerce(as_of) if as_of is not None else _now()
        row = self.get(topic_id)
        baseline = float(row.get("baseline") or 0.0)
        half_life = float(row.get("half_life") or INITIAL_HALF_LIFE)
        elapsed = 0.0
        if row.get("updated_at"):
            gap_hours = (now - _coerce(row["updated_at"])).total_seconds() / 3600.0
            elapsed = 1.0 if gap_hours > SESSION_GAP_HOURS else 0.0
        try:
            value = current_mastery(baseline, half_life, elapsed)
        except (ValueError, TypeError):
            value = 0.0
        return {
            "topic_id": str(topic_id),
            "baseline": round(baseline, 4),
            "half_life_sessions": round(half_life, 2),
            "elapsed_sessions": round(elapsed, 3),
            "retained": round(value, 4),
            "session_index": int(row.get("session_index") or 0),
            "updated_at": row.get("updated_at") or "",
        }

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()


STORE = RetentionStore()

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "show":
        for topic, row in STORE.load().items():
            cur = STORE.current(topic)
            print(f"{topic}: baseline={cur['baseline']} half-life={cur['half_life_sessions']} sessions "
                  f"elapsed={cur['elapsed_sessions']} → retained {cur['retained']}")
    else:
        print(json.dumps({"rows": list(STORE.load().values())}, ensure_ascii=False, indent=2))
