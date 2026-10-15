#!/usr/bin/env python3
"""UI interaction event stream.

Why this stream exists separately: the user requires the exported md to "be concrete about UI interactions (which key was pressed,
then which difficulty was chosen)". None of the existing three streams takes this view:

  run_log.jsonl       — Voyager's answer results (machine view)
  causal_timeline     — the causal sequence of control / attempt (question generator view)
  coach_log.jsonl     — human-imported questions and explanations (content view)

**None of them records "what the human pressed on the UI"**. Without it, the exported md can only say "coverage 45%",
not "at 02:01 the human pressed start, chose continuous adaptive + 3 rounds + pinned pistol_eco_pattern (difficulty 3)"
— and the latter is what is actually needed to reproduce an experiment.

Events are persisted as ui_events.jsonl and placed first when exporting md (mirroring the study companion's doc_exporter.py
`## Recent Interactions`, the first section after Overview).

Difficulty must be recorded: the topic dropdown shows topic_id, but what the human really cares about is "which difficulty was chosen",
so each event stores both topic_id and difficulty (difficulty=0 and annotated when the question generator auto-selects).
"""

from __future__ import annotations

import json
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "ui_events.jsonl"

# Event kinds (things actually pressed on the dashboard)
PILOT_START = "pilot_start"    # pressed "Start Voyager"
PILOT_STOP = "pilot_stop"      # pressed "Stop"
IMPORT = "human_import"        # human imports an intent
TAB = "tab_switch"             # switch tab
CLEAR_LOG = "clear_log"        # clear the run log
EXPORT = "export_md"           # export md
DB_PROBE = "db_probe"          # pressed "Probe": look at what is in this database before switching
DB_SWITCH = "db_switch"        # pressed "Switch": writes db_config.json (requires a dashboard restart to take effect)
RESET = "reset_all"            # pressed "Clear learning state"
PRACTICE_SCOPE_SET = "practice_scope_set"      # pressed "Practice this topic" on the knowledge graph page
PRACTICE_SCOPE_CLEAR = "practice_scope_clear"  # pressed "Clear scope"
SUBJECT_ADD = "subject_add"        # entry registry: a human fills in one entry (series/team after switching data source)
SUBJECT_SYNC = "subject_sync"      # pressed "Sync from data source": registers values probed from the database as entries
SUBJECT_DEL = "subject_del"        # delete one (human-filled) entry
MC_LEARN = "mc_learn"              # pressed "🧠 Start MC thinking learning → MVE" (MC-side learning + import)
VOYAGER_START = "voyager_start"    # pressed "▶ Start MC real learning" (run Voyager's auto curriculum inside MC)
VOYAGER_STOP = "voyager_stop"      # pressed "■ Stop" (Voyager learning)
VOYAGER_CLEAR_SKILL = "voyager_clear_skill"    # pressed "Clear library" (delete the skill library)
VOYAGER_CLEAR_MEMORY = "voyager_clear_memory"  # pressed "Clear memory" (delete curriculum progress + events)

# kind -> plain language (shown directly in the exported md)
ACTION_LABEL = {
    PILOT_START: "Pressed \"▶ Start Voyager\"",
    PILOT_STOP: "Pressed \"■ Stop\"",
    IMPORT: "Submitted a human-import intent",
    TAB: "Switched dashboard tab",
    CLEAR_LOG: "Pressed \"Clear log\"",
    EXPORT: "Exported md",
    DB_PROBE: "Pressed \"Probe\" (inspected the new database)",
    DB_SWITCH: "Pressed \"Switch database\"",
    RESET: "Pressed \"Clear learning state\"",
    PRACTICE_SCOPE_SET: "Pressed \"Practice this topic\" (set practice scope)",
    PRACTICE_SCOPE_CLEAR: "Pressed \"Clear scope\"",
    SUBJECT_ADD: "Registered an entry (human-filled)",
    SUBJECT_SYNC: "Pressed \"Sync entries from data source\"",
    SUBJECT_DEL: "Deleted an entry",
    MC_LEARN: "Pressed \"🧠 Start MC thinking learning → MVE\"",
    VOYAGER_START: "Pressed \"▶ Start MC real learning\"",
    VOYAGER_STOP: "Pressed \"■ Stop MC real learning\"",
    VOYAGER_CLEAR_SKILL: "Pressed \"Clear library (delete skill library)\"",
    VOYAGER_CLEAR_MEMORY: "Pressed \"Clear memory (delete curriculum progress + events)\"",
}


def _next_seq(rows: list[dict[str, Any]]) -> int:
    highest = 0
    for r in rows:
        try:
            highest = max(highest, int(r.get("seq") or 0))
        except (TypeError, ValueError):
            continue
    return highest + 1


def append(kind: str, *, detail: dict[str, Any] | None = None,
           result: str = "") -> dict[str, Any]:
    rows = load()
    ev = {
        "seq": _next_seq(rows),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "action": ACTION_LABEL.get(kind, kind),
        "detail": detail or {},
        "result": result,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def load() -> list[dict[str, Any]]:
    if not LOG.exists():
        return []
    out: list[dict[str, Any]] = []
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


def difficulty_of(topic_id: str) -> int:
    """topic_id -> difficulty. Returns 0 when unknown (question generator auto-select)."""
    if not topic_id:
        return 0
    try:
        from tasks import TASKS
        return int(TASKS[topic_id].difficulty)
    except Exception:
        return 0


def recent(limit: int = 50) -> list[dict[str, Any]]:
    return load()[-limit:]


def reset() -> None:
    if LOG.exists():
        LOG.unlink()
    print(f"UI interaction event stream cleared: {LOG}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        reset()
    else:
        for e in recent(30):
            print(f"#{e['seq']} {e['ts'][11:19]} {e['action']:24s} "
                  f"{json.dumps(e['detail'], ensure_ascii=False)}"
                  + (f"  -> {e['result']}" if e.get("result") else ""))
