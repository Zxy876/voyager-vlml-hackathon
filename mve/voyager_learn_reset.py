#!/usr/bin/env python3
"""Voyager clear-library / clear-memory (mirrors mve/reset_all.py, but targets Voyager's checkpoint directories).

Same mental model as the MVE dashboard's "clear learning state": back up to voyager_backup_<timestamp> first,
then delete — no button may destroy data directly.

Two kinds of clearing (matching MVE's clear-library / clear-memory):
  Clear library  — delete ckpt/skill/: the JS skill library Voyager's auto-curriculum has learned
           (skills.json + code/ + vectordb). Deleting it = the model "forgets" all mastered techniques.
  Clear memory — delete ckpt/curriculum/ + ckpt/events/:
           curriculum = course progress (completed/failed tasks, QA cache, exploration vector store);
           events    = event history (json written per task by EventRecorder).
           Deleting it = the model "forgets where it got to / what it tried" — next time it explores
           from scratch, but the skill library remains.

Why they're separate: clear library = forget skills; clear memory = forget progress. In the MVE dashboard,
"stop / clear library / clear memory" are three independent controls, and Voyager mirrors that —
a user may only want to clear progress and re-run the curriculum (keeping learned skills),
or start completely over (clearing skills too).

Usage:
    python mve/voyager_learn_reset.py skill              # clear library (backup first)
    python mve/voyager_learn_reset.py memory             # clear memory (backup first)
    python mve/voyager_learn_reset.py skill  --dry-run   # only show what would be cleared
    python mve/voyager_learn_reset.py memory --no-backup # delete without backing up
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

from voyager_paths import VOYAGER_DIR, SKILL_DIR, CURRICULUM_DIR, EVENTS_DIR


# Each clearing target: path + plain-language description
TARGETS = {
    "skill": [("skill", SKILL_DIR, "learned JS skill library (skills.json + code/ + vectordb)")],
    "memory": [
        ("curriculum", CURRICULUM_DIR, "course progress (completed/failed tasks + QA cache + exploration vector store)"),
        ("events", EVENTS_DIR, "event history (json written per task)"),
    ],
}


def _backup(targets: list[tuple[str, Path, str]]) -> str | None:
    existing = [(name, path, desc) for name, path, desc in targets if path.exists()]
    if not existing:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = VOYAGER_DIR / f"voyager_backup_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for _name, path, _desc in existing:
        # Preserve the relative structure under ckpt: voyager_backup_xxx/skill, .../curriculum, .../events
        rel = path.name
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for item in path.iterdir():
            shutil.copy2(item, dest / rel / item.name) if item.is_file() else \
                shutil.copytree(item, dest / rel / item.name)
    return str(dest)


def _clear_one(which: str) -> dict:
    targets = TARGETS.get(which)
    if not targets:
        return {"ok": False, "error": f"unknown target {which}"}
    existing = [(name, path, desc) for name, path, desc in targets if path.exists()]
    if not existing:
        return {"ok": True, "cleared": [], "backup": None,
                "note": "there was nothing to clear in the first place"}
    # Back up (unless --no-backup)
    backup = None
    if "--no-backup" not in sys.argv:
        backup = _backup(targets)
    for _name, path, _desc in existing:
        shutil.rmtree(path)
    cleared = [f"{name} ({desc})" for name, _p, desc in existing]
    return {"ok": True, "cleared": cleared, "backup": backup}


def clear_skill() -> dict:
    return _clear_one("skill")


def clear_memory() -> dict:
    return _clear_one("memory")


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("skill", "memory"):
        print(__doc__)
        print("usage: python mve/voyager_learn_reset.py {skill|memory} [--dry-run] [--no-backup]")
        return 2
    which = argv[0]
    dry = "--dry-run" in argv
    targets = TARGETS[which]
    existing = [(name, path, desc) for name, path, desc in targets if path.exists()]
    label = "clear library (skill library)" if which == "skill" else "clear memory (curriculum + events)"
    print("=" * 68)
    print(f"  Voyager {label}" + (" (DRY RUN, nothing will be deleted)" if dry else ""))
    print("=" * 68)
    for name, path, desc in existing:
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        print(f"  {name:12s} {size:>12,} B   {desc}")
    if not existing:
        print("\n  Nothing to clear.")
        return 0
    if dry:
        return 0
    res = _clear_one(which)
    if res.get("backup"):
        print(f"\n  Backed up to: {res['backup']}")
    for c in res.get("cleared", []):
        print(f"  Cleared: {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
