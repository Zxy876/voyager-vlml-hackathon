#!/usr/bin/env python3
"""Action causal timeline (mirrors the study companion's root_fact_seq).

The study companion source has no term "action causal", but docs/认知引擎阶段文档.md:80 has an isomorphic implementation:

    "attempt, question, control have all been unified into the monotonic root_fact_seq,
     same-second facts and late extractions have a stable causal order"

That is: **all types of facts enter the same monotonically increasing timeline; only order is guaranteed, no scores are produced.**
  - control fact = human import (a human-posed question, a human-given explanation)
  - attempt fact = Voyager's action (orchestration, fact set, verdict)
  - referee fact = the referee's verdict
  - narrative fact = narrative output

Why this line must exist separately:
  Mastery is "scoring", and excludes topics with validated_target=False;
  but human-imported topics, though unscored, must still be visible to the question generator —
  "human-imported intent enters the timeline (causal), not mastery (scoring);
   the question generator looks at the learning trajectory, not single scores".
  Without this line, a human import amounts to nothing happening.

This file does not import vlml_env, so the dashboard can read it directly.
"""

from __future__ import annotations

import json
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any

LOG = Path(__file__).resolve().parent / "causal_timeline.jsonl"

# Fact kinds. The control/attempt distinction is the core of this design.
CONTROL = "control"        # human import: posing questions, giving explanations, changing scope
ATTEMPT = "attempt"        # Voyager's action
REFEREE = "referee"        # referee verdict
NARRATIVE = "narrative"    # narrative output

KIND_LABEL = {
    CONTROL: "Human import",
    ATTEMPT: "Voyager action",
    REFEREE: "Referee verdict",
    NARRATIVE: "Narrative output",
}


def _next_seq(rows: list[dict[str, Any]]) -> int:
    """Monotonically increasing sequence number. Facts arriving in the same second are ordered by it, not by timestamp."""
    highest = 0
    for r in rows:
        try:
            highest = max(highest, int(r.get("seq") or 0))
        except (TypeError, ValueError):
            continue
    return highest + 1


def append(kind: str, *, topic_id: str = "", summary: str = "",
           detail: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = load()
    fact = {
        "seq": _next_seq(rows),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "topic_id": topic_id,
        "summary": summary,
        "detail": detail or {},
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(fact, ensure_ascii=False) + "\n")
    return fact


def load() -> list[dict[str, Any]]:
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
    out.sort(key=lambda r: int(r.get("seq") or 0))
    return out


def for_panel(limit: int = 30) -> list[dict[str, Any]]:
    rows = load()
    return [
        {
            "seq": r.get("seq"),
            "ts": r.get("ts", ""),
            "kind": r.get("kind", ""),
            "kind_label": KIND_LABEL.get(r.get("kind", ""), r.get("kind", "")),
            "topic_id": r.get("topic_id", ""),
            "summary": r.get("summary", ""),
        }
        for r in rows[-limit:]
    ]


def counts_by_topic() -> dict[str, dict[str, int]]:
    """What the question generator needs: how many control / attempt facts each topic has."""
    out: dict[str, dict[str, int]] = {}
    for r in load():
        topic = str(r.get("topic_id") or "")
        if not topic:
            continue
        bucket = out.setdefault(topic, {"control": 0, "attempt": 0, "referee": 0})
        kind = str(r.get("kind") or "")
        if kind in bucket:
            bucket[kind] += 1
    return out


def latest_topics_by_kind(kind: str) -> list[str]:
    """Topic list sorted by "most recent first" (deduplicated, newest first).

    `counts_by_topic()` has counts but no timing; the question generator cannot tell "imported last week"
    from "imported just now" with it. The point of a human import is the intent **right now**, so it must be taken in time order.
    """
    rows = [r for r in load() if str(r.get("kind")) == kind and str(r.get("topic_id") or "")]
    rows.sort(key=lambda r: int(r.get("seq") or 0), reverse=True)
    out: list[str] = []
    for r in rows:
        t = str(r["topic_id"])
        if t not in out:
            out.append(t)
    return out


def reset() -> None:
    if LOG.exists():
        LOG.unlink()
    print(f"Action causal timeline cleared: {LOG}")
