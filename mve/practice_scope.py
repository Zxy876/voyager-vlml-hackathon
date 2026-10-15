#!/usr/bin/env python3
"""Practice scope: pin one dimension on the knowledge graph as "what to practice next".

Why it exists:
    The dashboard used to make the human pick "mode / rounds / topic" in the cockpit; those buttons essentially let the human decide for the question generator.
    The study companion's approach (onboarding.md:82) is the opposite — **the human clicks one topic on the knowledge graph,
    sets it as the practice scope, and the question generator only generates questions within that scope**. The UI then only has start / stop.

The shape mirrors the study companion's `study_set_practice_scope` (entry_practice_scope_entries.py:117-173):
  · Stores the canonical `scope_key`, **re-validated on read** — after a graph rebuild the old dimension may no longer
    exist, in which case return invalidated + reason instead of continuing to generate questions from the stale value
    (the study companion's corresponding error code is PRACTICE_SCOPE_INVALIDATED)
  · `scope_revision` increments monotonically: +1 on every scope change, used to tell "has the question generator caught up with the latest scope"
  · Validation failures never propagate: the dashboard is a read-only view; one invalidated scope should not make /api/state return a 500

The scope → topic mapping is based on the **task registry**, not on graph nodes:
    A dimension node's detail.topic_id is the first topic recorded "first-come-first-served" when the graph was built,
    while dimensions like map_fb_conv span two topics (both fb_conversion_analysis and corrode_collapse need it).
    So the real topic list is reconciled from tasks' rubric; the graph only **validates that the dimension exists**.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SCOPE_FILE = ROOT / "practice_scope.json"
GRAPH_FILE = ROOT / "knowledge_graph.json"


class PracticeScopeError(Exception):
    """Scope unavailable. code mirrors the study companion: PRACTICE_SCOPE_INVALIDATED."""

    def __init__(self, message: str, code: str = "PRACTICE_SCOPE_INVALIDATED"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Read-only sources (none import vlml_env — the dashboard opens instantly, and queries work offline)
# ---------------------------------------------------------------------------

def graph_dims() -> dict[str, dict[str, Any]]:
    """Dimensions in the graph -> {topic_id, point, weight}. Returns empty if the graph was never built."""
    if not GRAPH_FILE.exists():
        return {}
    try:
        g = json.loads(GRAPH_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for n in g.get("nodes") or []:
        if n.get("kind") != "dimension":
            continue
        d = n.get("detail") or {}
        out[str(n.get("label") or "")] = {
            "topic_id": str(d.get("topic_id") or ""),
            "point": str(d.get("point") or ""),
            "weight": d.get("weight"),
        }
    return out


def topics_for_dim(dim: str) -> list[str]:
    """Topics covering this dimension (reconciled against the task registry's rubric).

    Why not use the graph node's topic_id: see the module header — that is the "first topic", not "all topics".
    """
    try:
        import tasks
    except Exception:
        return []
    return sorted({
        t.topic_id for t in tasks.TASKS.values()
        if any(p.dimension == dim for p in t.rubric)
    })


# ---------------------------------------------------------------------------
# Store / read / clear
# ---------------------------------------------------------------------------

def _read() -> dict[str, Any]:
    if not SCOPE_FILE.exists():
        return {}
    try:
        obj = json.loads(SCOPE_FILE.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write(state: dict[str, Any]) -> None:
    SCOPE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def set_scope(dim: str) -> dict[str, Any]:
    """Set one dimension as the practice scope. The dimension must exist in the current graph (canonical validation)."""
    dim = str(dim or "").strip()
    if not dim:
        raise PracticeScopeError("Dimension is empty", code="INVALID_PRACTICE_SCOPE")

    dims = graph_dims()
    if dim not in dims:
        raise PracticeScopeError(
            f"Dimension \"{dim}\" is not in the current knowledge graph (the graph has {len(dims)} dimensions)"
            + ("; run python mve/knowledge_graph.py --build first" if not dims else ""),
        )

    topics = topics_for_dim(dim)
    if not topics:
        raise PracticeScopeError(
            f"Dimension \"{dim}\" is in the graph, but no task's rubric uses it — cannot practice",
        )

    prev = _read()
    info = dims[dim]
    state = {
        "active": True,
        "scope_key": f"dim:{dim}",
        "kind": "dimension",
        "label": dim,
        "topics": topics,
        "point": info["point"],
        "weight": info["weight"],
        "scope_revision": int(prev.get("scope_revision") or 0) + 1,
        "set_at": datetime.now().isoformat(timespec="seconds"),
        "source": "graph",
    }
    _write(state)
    return {**state, "ok": True}


def clear_scope() -> dict[str, Any]:
    """Clear the scope (study companion onboarding.md:84: once the scope is mastered, clear it and return to question-generator auto-select)."""
    prev = _read()
    state = {
        "active": False,
        "scope_key": "",
        "kind": "",
        "label": str(prev.get("label") or ""),
        "topics": [],
        "scope_revision": int(prev.get("scope_revision") or 0) + 1,
        "set_at": "",
        "source": "graph",
    }
    _write(state)
    return {**state, "ok": True, "cleared": str(prev.get("label") or "")}


def get_scope() -> dict[str, Any]:
    """Current scope. If the stored dimension is no longer in the graph → active=False + invalidated reason."""
    st = _read()
    if not st or not st.get("active"):
        return {"active": False, "label": str(st.get("label") or ""),
                "scope_revision": int(st.get("scope_revision") or 0)}

    dim = str(st.get("label") or "")
    dims = graph_dims()
    if dim not in dims:
        return {
            "active": False, "invalidated": True, "label": dim,
            "scope_key": str(st.get("scope_key") or ""),
            "reason": f"Dimension \"{dim}\" is no longer in the current knowledge graph (was the graph rebuilt?)",
            "scope_revision": int(st.get("scope_revision") or 0),
        }

    # topics recomputed every time: the task registry may have changed; do not trust the persisted copy
    topics = topics_for_dim(dim) or list(st.get("topics") or [])
    info = dims[dim]
    return {
        "active": True,
        "scope_key": f"dim:{dim}",
        "kind": "dimension",
        "label": dim,
        "topics": topics,
        "point": info["point"],
        "weight": info["weight"],
        "scope_revision": int(st.get("scope_revision") or 0),
        "set_at": str(st.get("set_at") or ""),
        "source": "graph",
    }


def resolve_topic(*, attempted: dict[str, int] | None = None,
                  failed: dict[str, int] | None = None) -> str:
    """The next topic within the scope. Returns an empty string if no scope is set (question generator auto-select).

    Sorting fully reuses tasks.next_topic (wrong answers first → never-attempted first → difficulty ascending),
    just narrowing the candidate set from "all topics" to "topics within the scope".
    """
    sc = get_scope()
    if not sc.get("active") or not sc.get("topics"):
        return ""
    try:
        import run_log
        import tasks
        attempted = attempted if attempted is not None else run_log.attempted_counts()
        failed = failed if failed is not None else run_log.failed_counts()
        return tasks.next_topic(attempted=attempted, failed=failed,
                                only=list(sc["topics"]))
    except Exception:
        return str(sc["topics"][0])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> int:
    import sys

    args = sys.argv[1:]
    if not args or args[0] in ("--show", "-s"):
        sc = get_scope()
        print(json.dumps(sc, ensure_ascii=False, indent=2))
        return 0
    if args[0] == "--set" and len(args) >= 2:
        try:
            print(json.dumps(set_scope(args[1]), ensure_ascii=False, indent=2))
        except PracticeScopeError as e:
            print(json.dumps({"ok": False, "code": e.code, "error": str(e)},
                             ensure_ascii=False))
            return 1
        return 0
    if args[0] == "--clear":
        print(json.dumps(clear_scope(), ensure_ascii=False, indent=2))
        return 0
    if args[0] == "--resolve":
        print(resolve_topic())
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
