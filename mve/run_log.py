#!/usr/bin/env python3
"""Run log: appends one JSONL line per round.

The dashboard's progress curve reads this file. Why it is written separately:
- The main loop only runs; it does not care about the persistence format
- The dashboard only reads; it does not depend on the main loop still running

One record = one round's complete observable state.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

LOG = Path(__file__).resolve().parent / "run_log.jsonl"


def append(record: dict[str, Any]) -> None:
    record.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_all() -> list[dict[str, Any]]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def attempted_counts() -> dict[str, int]:
    """How many times each topic was attempted (mirrors the study companion's attempted key in ordered_scope_topics)."""
    out: dict[str, int] = {}
    for r in load_all():
        t = str(r.get("topic_id") or "")
        if t:
            out[t] = out.get(t, 0) + 1
    return out


def failed_counts() -> dict[str, int]:
    """How many times each topic was wrong (wrong/dont_know) — the study companion's retry_wrong_questions picks these."""
    out: dict[str, int] = {}
    for r in load_all():
        t = str(r.get("topic_id") or "")
        if t and str(r.get("verdict") or "") in ("wrong", "dont_know"):
            out[t] = out.get(t, 0) + 1
    return out


def undone_counts() -> dict[str, int]:
    """Count of "task not done" per topic — **the failed ledger of Voyager's ruler** (mirrors the original failed_tasks).

    done = orchestration ran + data retrieved + no crash (loop_core.task_done), **coverage points not counted**.
    The question generator (planner) uses this to decide "should this unfinished task still be given" — curriculum planning
    is based on Voyager's own learning quality (completed / failed ledger), not VLML coverage points.
    """
    out: dict[str, int] = {}
    for r in load_all():
        t = str(r.get("topic_id") or "")
        if t and not r.get("done"):
            out[t] = out.get(t, 0) + 1
    return out


def unresolved_wrongs(topic_id: str) -> int:
    """Number of wrong answers for this topic that are **not yet digested** (mirrors the study companion's unresolved_wrong_count in mastery_v2).

    Semantics: wrong answers accumulate, a correct answer resets to zero. The study companion uses it for two things
    (adaptive_learning/mastery_v2.py:283-288):
        if unresolved_wrong_count > 0:
            mastery = min(mastery, policy.unresolved_wrong_mastery_cap)  # =0.79
            flags.append("unresolved_wrong_cap")
        mastered = resolved_wrong_count == 0 and mastery >= threshold
    That is: **undigested wrong answers bar a "mastered" verdict** — this is what "being wrong is a signal, not a failure"
    looks like on the scoring side: you are not punished for being wrong, but un-cleared wrong answers block graduation.
    """
    n = 0
    for r in load_all():
        if str(r.get("topic_id") or "") != topic_id:
            continue
        v = str(r.get("verdict") or "")
        if v == "correct":
            n = 0
        elif v in ("wrong", "partial", "dont_know"):
            n += 1
    return n


def evidence_rows(topic_id: str) -> list[Any]:
    """This topic's **entire** history of evidence, for the V2 mastery model to rebuild across processes.

    V2's weighting and time decay require seeing the full sequence: feeding only the current round
    leaves the weight sum at a single entry, and confidence degenerates into a constant.
    Skip rounds with evidence_status == "none" — consistent with the main loop's rule that rounds
    with no evidence are not counted in the mastery sequence.
    """
    import mastery_model  # Local import: avoids a circular dependency with upper-level modules

    out: list[Any] = []
    for r in load_all():
        if str(r.get("topic_id") or "") != topic_id:
            continue
        if str(r.get("evidence_status") or "") == "none":
            continue
        try:
            out.append(mastery_model.evidence_from_row(r))
        except Exception:
            continue
    return out


def recent_scores(topic_id: str, limit: int = 9) -> list[float]:
    """This topic's historical score sequence (for the main loop to continue the mastery sequence across processes).

    Only collect rounds with evidence_status != "none" — consistent with the main loop's rule that rounds
    without evidence are not counted in the mastery sequence. Scores use the same verdict
    mapping as run_mve's history (correct=1.0 / partial=0.6 / others=0.0), not score/100:
    the sequence must use one consistent scale, or variance / consistency are meaningless.
    """
    out: list[float] = []
    for r in load_all():
        if str(r.get("topic_id") or "") != topic_id:
            continue
        if str(r.get("evidence_status") or "") == "none":
            continue
        v = str(r.get("verdict") or "")
        out.append({"correct": 1.0, "partial": 0.6}.get(v, 0.0))
    return out[-limit:]


def last_missing(topic_id: str) -> list[str]:
    """The missing rubric points from this topic's most recent wrong answer — used to restore failed_dims across runs."""
    out: list[str] = []
    for r in load_all():
        if str(r.get("topic_id") or "") != topic_id:
            continue
        if str(r.get("verdict") or "") == "correct":
            out = []
        else:
            out = [str(x) for x in (r.get("missing") or [])]
    return out


def reset() -> None:
    if LOG.exists():
        LOG.unlink()
    print(f"Run log cleared: {LOG}")
