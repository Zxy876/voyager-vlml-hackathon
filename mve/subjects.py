#!/usr/bin/env python3
"""**Fillable value registry**: which values can be filled in when changing series / teams.

⚠️ One course correction on 2026-10-08 — this is **not** a "subject registry"
--------------------------------------------------------
The user's exact words: "the topics/tool sets under a subject are all the same anyway; there is no need to fill in a subject and then organize tool sets yourself. Look at what keys the study companion actually maps to series id and team."

Going to the study companion for evidence, the answer is `entry_tutor_question_entries.py:1043-1051`:

    store.list_topics(5000, scope.subject, scope.stage,
                      chapter=scope.chapter, unit=scope.unit,
                      course_family=scope.course_family)

→ subject / stage / chapter / unit / course_family are **all `list_topics`
filter parameters**, not hierarchical ownership on the topics themselves. The topic store is **one shared copy**;
changing subject or course family only decides "which ones get selected for practice", **the topics and tool sets themselves are never rebuilt**.

So series ids / team names are not "subjects", but:

| MVE value | Study companion counterpart | Nature |
|---|---|---|
| **Series id (series / data source version)** | `course_family` (which textbook/version) | scope filter parameter, topics shared |
| **Concrete team / player / map values** | `entity` in `_semantic_routing.py:149` (`"max 120 chars; empty when absent"`, **not in the topics table**) | runtime data-retrieval parameter, does not enter the knowledge structure |
| **Which database is connected** | no counterpart in the study companion | MVE's own `data_source` |

So this module's role is a **"fillable value registry"**: it provides candidate values for the scope (course_family / entity)
and records "under which range this entry was **this** run". It does not define the knowledge
structure — changing values neither requires nor triggers a tool-set rebuild.

Isomorphism reference (evidence from study companion source)
------------------------
* `study_companion/store.py:1504-1506`
      subject = CASE WHEN topics.source='seed' THEN topics.subject
                     ELSE excluded.subject END
  → seeded subjects are locked; runtime entries' subjects are **writable and changeable** (the subject field itself is editable,
    which is a different matter from "changing values requires rebuilding tool sets").
* `study_companion/_semantic_routing.py:23` `ALLOWED_SUBJECTS` is a hard-coded
  frozenset: the study companion's subjects are a **preset enum** that humans cannot fill in freely.
  MVE here is **deliberately looser**: data sources get swapped (GRID → vlr.gg → …),
  and a preset enum inevitably rots. So values are human-fillable, with only empty/dedup validation and no whitelist.
* `study_companion/_semantic_routing.py:149` `"entity": "string, max 120
  chars; empty when absent"` → entity is a **runtime** field, nullable, **not in the topics
  table**. Concrete team/player/map values sit in this tier.

Scope marking (not "ownership")
----------------------
Entries (human-imported edges / dimensions produced by Voyager awaiting review) carry entity literals in their SQL
(`series_id = 'xxx'` / `team_name = 'NRG'`), or the question generator explicitly passes
`subject={"team": "NRG"}`. Look these values up in the registry for entries with the same kind and value;
not found → recorded as **unmarked scope**, awaiting a human fill, never force-stuffed.
This is only "the range this entry ran under **this** time", **it does not change its position in the knowledge structure**.

Usage
----
    python mve/subjects.py                 # list fillable values + entries with unmarked scope
    python mve/subjects.py --sync          # register values probed from entity_catalog
    python mve/subjects.py --add team NRG --note "…"   # a human fills in one fillable value
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUBJECTS = HERE / "subjects.json"
DB_CONFIG = HERE / "db_config.json"

# Entry kind → label (kept consistent with knowledge_graph.SUBJECT_LABEL)
KIND_LABEL = {"series": "Series", "team": "Team", "map": "Map", "player": "Player",
              "tournament": "Tournament", "round": "Round", "game": "Game",
              "custom": "Custom"}

# Default value: currently connected to VLML's duckdb slice
DEFAULT_FAMILY = "vlml0"
# Known database file names → code (add a line here when swapping data sources; unrecognized names use the file name itself)
_FAMILY_BY_STEM = {"vlml_events": "vlml0",        # GRID slice (per-event, key expired)
                   "vlml_vlr": "vlr.gg",         # public source A (round-level + aggregate-level)
                   "vlml_rib": "rib.gg"}         # public source B (+ per-round × per-player / economy)


# --------------------------------------------------------------------------
# Store / load
# --------------------------------------------------------------------------
def _load() -> dict:
    try:
        raw = json.loads(SUBJECTS.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("subjects", [])
            raw.setdefault("attached", {})
            return raw
    except Exception:
        pass
    return {"subjects": [], "attached": {}}


def _save(data: dict) -> None:
    SUBJECTS.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")


def _norm(v: str) -> str:
    """Normalize: for comparison. Case/spaces/quotes should never create two entries."""
    return re.sub(r"[\s'\"`]+", "", str(v or "")).strip().lower()


def _sid(kind: str, value: str) -> str:
    return f"{kind}:{_norm(value)}"


# --------------------------------------------------------------------------
# Registration (human-filled / synced from the database)
# --------------------------------------------------------------------------
def register(kind: str, value: str, *, label: str = "", note: str = "",
             source: str = "human") -> dict:
    """Register an entry. If it already exists, only fill in label/note; do not create a duplicate.

    Human-filled entries with `source="human"` are **never overwritten by `--sync`** — isomorphic to the study companion's
    `topics.source='seed'` CASE: presets are locked, runtime values are editable.
    """
    kind = str(kind or "").strip().lower()
    value = str(value or "").strip()
    if not kind or not value:
        return {"ok": False, "reason": "Kind and value must both be non-empty"}
    data = _load()
    sid = _sid(kind, value)
    for s in data["subjects"]:
        if s.get("id") == sid:
            if label and not s.get("label"):
                s["label"] = label
            if note:
                s["note"] = note
            _save(data)
            return {"ok": True, "id": sid, "existed": True}
    data["subjects"].append({
        "id": sid, "kind": kind, "value": value,
        "label": label or value, "note": note, "source": source,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save(data)
    return {"ok": True, "id": sid, "existed": False}


def unregister(sid: str) -> bool:
    """Delete one **human-filled** entry (deleting a probed one just gets pulled back by --sync)."""
    data = _load()
    before = len(data["subjects"])
    data["subjects"] = [s for s in data["subjects"] if s.get("id") != sid]
    data["attached"] = {k: [x for x in v if x != sid]
                        for k, v in (data.get("attached") or {}).items()}
    data["attached"] = {k: v for k, v in data["attached"].items() if v}
    _save(data)
    return len(data["subjects"]) < before


def sync_from_catalog() -> dict:
    """Register entity values probed from `entity_catalog.json` as `source="catalog"`.

    Run this after swapping data sources: series / teams in the new database automatically appear in the fillable entry values,
    with no need for the human to type them one by one. Human-filled entries with the same name are left untouched.
    """
    try:
        cat = json.loads((HERE / "entity_catalog.json").read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "reason": f"Cannot read entity_catalog.json: {exc}"}
    ents = (cat or {}).get("entities") or {}
    if not ents:
        return {"ok": False, "reason": "No entities in entity_catalog ("
                                       f"{(cat or {}).get('error') or 'empty'})"}
    data = _load()
    human_ids = {s.get("id") for s in data["subjects"]
                 if s.get("source") == "human"}
    known = {s.get("id") for s in data["subjects"]}
    added = 0
    for kind, items in ents.items():
        for it in items or []:
            v = str((it or {}).get("value") or "").strip()
            if not v:
                continue
            sid = _sid(kind, v)
            if sid in known or sid in human_ids:
                continue
            data["subjects"].append({
                "id": sid, "kind": kind, "value": v, "label": v,
                "note": "", "source": "catalog",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            })
            known.add(sid)
            added += 1
    _save(data)
    return {"ok": True, "added": added, "total": len(data["subjects"])}


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def all_subjects() -> list[dict]:
    return list(_load().get("subjects") or [])


def by_kind(kind: str) -> list[dict]:
    k = str(kind or "").strip().lower()
    return [s for s in all_subjects() if s.get("kind") == k]


def values(kind: str) -> list[str]:
    """The **fillable values** under this kind: probed + human-filled (deduplicated, human-filled appended at the end).
    For UI dropdowns and the question generator."""
    return [str(s.get("value") or "") for s in by_kind(kind)
            if s.get("value")]


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------
# Entity literals in SQL: series_id = 'xxx' / team_name='NRG' / map_name IN (...)
_LIT = re.compile(
    r"(series_id|team_name|map_name|player_name|tournament_name)"
    r"\s*(?:=|IN)\s*\(?\s*'([^']{1,80})'", re.I)
_COL2KIND = {"series_id": "series", "team_name": "team", "map_name": "map",
             "player_name": "player", "tournament_name": "tournament"}


def entities_in_sql(sql: str) -> dict[str, str]:
    """Recognize entities (series ids / team names / …) from actually-run SQL — no guessing, literals only."""
    out: dict[str, str] = {}
    for col, val in _LIT.findall(str(sql or "")):
        kind = _COL2KIND.get(str(col).lower())
        if kind and val and kind not in out:
            out[kind] = val
    return out


def resolve(entities: dict[str, str] | None) -> list[str]:
    """Entities → entry id list. Returns empty when unrecognizable (never force-stuffed)."""
    out: list[str] = []
    for kind, val in (entities or {}).items():
        sid = _sid(kind, val)
        if any(s.get("id") == sid for s in all_subjects()) and sid not in out:
            out.append(sid)
    return out


def attach(entry_id: str, subject_ids: list[str]) -> int:
    """Attach one entry (insight / dimension / human-imported edge / dimension awaiting review) under an entry."""
    eid = str(entry_id or "").strip()
    if not eid:
        return 0
    data = _load()
    cur = [s for s in (data["attached"].get(eid) or [])]
    for sid in subject_ids or []:
        if sid and sid not in cur:
            cur.append(sid)
    data["attached"][eid] = cur
    _save(data)
    return len(cur)


def of_entry(entry_id: str) -> list[str]:
    return list((_load().get("attached") or {}).get(str(entry_id or "")) or [])


def entries_of(subject_id: str) -> list[str]:
    sid = str(subject_id or "")
    return [e for e, v in (_load().get("attached") or {}).items() if sid in v]


def unassigned(entry_ids: list[str] | None = None) -> list[str]:
    """Entries not yet owned. Pass None to return all "unowned" entries recorded in the registry."""
    att = _load().get("attached") or {}
    if entry_ids is None:
        return sorted(e for e, v in att.items() if not v)
    return [e for e in entry_ids if not (att.get(e) or [])]


# --------------------------------------------------------------------------
# Which database is connected (MVE's own key; no counterpart in the study companion)
# --------------------------------------------------------------------------
def data_source() -> str:
    """Which database is currently connected. It changes itself when the database is swapped; nothing is hand-written.

    Not called `course_family`: the study companion's course_family is "which textbook/version", a
    **filter parameter** corresponding to our **series id**; "which database is connected" is one layer further out
    (vlml0 = GRID slice / vlml_vlr = vlr.gg), and the study companion has no such thing.
    """
    path = ""
    try:
        cfg = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        path = str((cfg or {}).get("db_path") or "").strip()
    except Exception:
        pass
    if not path:
        return DEFAULT_FAMILY
    stem = Path(path).stem
    return _FAMILY_BY_STEM.get(stem, stem or DEFAULT_FAMILY)


def course_family() -> str:
    """Backward-compatible call: == `data_source()`. New code should use `data_source()`."""
    return data_source()


def _main() -> int:
    args = sys.argv[1:]
    if "--add" in args:
        i = args.index("--add")
        rest = args[i + 1:]
        note = ""
        if "--note" in rest:
            j = rest.index("--note")
            note = " ".join(rest[j + 1:])
            rest = rest[:j]
        if len(rest) < 2:
            print("Usage: --add <kind> <value> [--note description]")
            return 2
        r = register(rest[0], " ".join(rest[1:]), note=note)
        print(f"{'Already exists' if r.get('existed') else 'Registered'}: {r.get('id')}"
              if r.get("ok") else f"❌ {r.get('reason')}")
        return 0 if r.get("ok") else 1
    if "--sync" in args:
        r = sync_from_catalog()
        if not r.get("ok"):
            print(f"❌ {r.get('reason')}")
            return 1
        print(f"Registered {r['added']} new entries from entity_catalog, {r['total']} total")
        return 0
    if "--json" in args:
        print(json.dumps(_load(), ensure_ascii=False, indent=1))
        return 0

    data = _load()
    print(f"Data source (data_source): {data_source()}")
    subs = data.get("subjects") or []
    if not subs:
        print("\n(Registry is empty — run `--sync` to register from the database, or `--add series xxx`)")
    for kind in sorted({str(s.get("kind")) for s in subs}):
        items = by_kind(kind)
        print(f"\n{KIND_LABEL.get(kind, kind)} ({kind}) · {len(items)}")
        for s in items:
            tag = "human-filled" if s.get("source") == "human" else "probed"
            n = len(entries_of(s.get("id") or ""))
            print(f"  [{tag}] {s.get('value')}"
                  + (f"  ({s['note']})" if s.get("note") else "")
                  + f"  · {n} entries")
    ua = unassigned()
    if ua:
        print(f"\nUnowned entries ({len(ua)}):")
        for e in ua[:20]:
            print(f"  - {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
