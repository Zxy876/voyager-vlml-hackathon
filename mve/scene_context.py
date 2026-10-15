#!/usr/bin/env python3
"""MVE's equivalent of Minecraft's "progressive-disclosure initial scene injection" (Task 2, landed 2026-10-12).

Minecraft's "initial scene injection" (curriculum.py:142 render_observation reads
biome / nearby_blocks / inventory / chests; warm_up :95-109 progressive disclosure)
translated into MVE's counterparts:

  MC element              MVE counterpart                             source
  ──────────────────────────────────────────────────────────────────────
  biome (the world)       data source identity (vlml_vlr / vlml_rib /   db_config
                          default database)
  nearby_blocks (in view) the currently visible "tables + the 10 VLML   information_schema
                          tools" (base tier is always given; the
                          ext_* tier only once progress is sufficient)
  inventory (backpack)    the fillable entity values (anchor
                          series/team/map + catalogue options)          anchor / entity_catalog /
                          + the already mastered knowledge points        mastery
  chests (loot)           the skill-library graph (skills/recipes      skill_store / mastery
                          already learned)

The key point: this is progressive disclosure of the **scene layer**, **independent of**
voyager.py graph_context's "scaffold layer" (full/partial/none). The scaffold layer governs
"how much answer skeleton to give"; the scene layer governs "which data/tools are allowed
to be seen". The two layers are orthogonal: a beginner may have the scene fully open
(seeing the whole database) but the scaffold fully given (the answer carried all the way);
a veteran may get only the base tables in the scene but have the scaffold fully removed
(drilling into ext on their own).

Why this must be done (the MC lesson)
-------------------------------------
MC's warm_up means "open up more world state as progress advances" —— handing over the full
map from the start leaves the agent with no entry point, and also makes it impossible to
measure whether it can use the basic tools at all. MVE previously stuffed every table into
the prompt at once, so the model skipped the base layer and wrote bare ext queries
outright → necessarily scoring 0 on databases missing those tables. Progressive disclosure
in the scene layer makes "do not touch ext before mastering the base layer" a
**structural constraint** rather than a matter of prompting.

Usage
-----
    from scene_context import scene_injection
    text = scene_injection(topic_id, progress_tier=0)   # for the model's prompt
    # or inspect the two tiers of the current database from the command line:
    python mve/scene_context.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DB_CONFIG = HERE / "db_config.json"

# The 10 VLML tools = the cross-domain baseline (user-specified: skills/actions are locked
# to the semantic layer, no bare SQL).
# Swapping databases only affects "does this tier's question have data"; the tool set is
# constant → skills stay transferable.
SEMANTIC_TOOLS: list[str] = [
    "match_analysis_report", "match_summary_report", "match_players_report",
    "match_rounds_report", "match_economy_report", "player_profile_report",
    "scouting_report", "pattern_detection_report",
    "query_sql", "get_database_info",
]

# Progress tier: 0 = beginner (base tables only), 1 = base layer unlocked (ext tables opened up)
TIER_BASE = 0
TIER_EXT = 1
# How many milestones must be unlocked to reach the ext tier. Using the **milestone count**
# rather than mastery is because mastery is VLML's **scoring result** —— scoring only reaches
# the dashboard and cannot serve as a gate on disclosure/progression
# (user's original words: "scoring results are not read by any progression condition").
TIER_EXT_UNLOCKS = 3

# VLML's known tables (static fallback): they still yield a meaningful "scene" when duckdb
# is unavailable, so question generation need not query the database live every time (which
# also avoids slow queries). When a live query succeeds, the live result wins.
KNOWN_BASE_TABLES = [
    "series", "games", "rounds", "base_events",
    "agg_team_series_stats", "agg_player_series_stats", "ext_player_game_stats",
]
KNOWN_EXT_TABLES = [
    "ext_player_round_stats", "ext_round_economy",
    "ext_game_economy", "ext_player_side_stats",
]


def data_source_identity() -> str:
    """The biome counterpart: which data source is in use (decided by the database file name)."""
    try:
        obj = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        p = str((obj or {}).get("db_path") or "")
    except Exception:
        p = ""
    # An empty db_path means the **default database** is in use (db_switch.DEFAULT_DB,
    # currently rib.gg) —— the label must follow the default database and must no longer be
    # hardcoded as "vlml_events(default)" (since 2026-10-13: after the default switched to
    # rib, that label pointed at a database nobody used).
    name = Path(p).stem if p else Path(_default_db()).stem
    if "vlr" in name:
        return f"{name} (vlr.gg source: round-level + aggregate level, no ext layer)"
    if "rib" in name:
        return f"{name} (rib.gg source: includes first-blood/economy ext layer)"
    return f"{name} (default database)" if not p else f"{name} (GRID single-match slice)"


def _default_db() -> str:
    """The default database path (defined solely in db_switch, currently rib.gg)."""
    from db_switch import DEFAULT_DB
    return str(DEFAULT_DB)


def _db_path() -> str:
    """The current database path. **Relative paths resolve against the repository root**
    (the same rule as vlml_env._resolve_db_path).

    ⚠️ Why not return the configured value verbatim: vlml_env switches cwd to `vlml/`, so a
       relative path gets concatenated twice there. Both sides must agree, otherwise the
       dashboard says "switched to the rib database" while it is still connected to the
       default one —— that is exactly how it went wrong on the server on 2026-10-13.
    """
    try:
        obj = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        p = str((obj or {}).get("db_path") or "")
    except Exception:
        p = ""
    if not p:
        return _default_db()
    return str(((HERE.parent / Path(p).expanduser()).resolve())
               if not Path(p).expanduser().is_absolute()
               else Path(p).expanduser())


def available_tables(progress_tier: int = TIER_BASE) -> dict[str, list[str]]:
    """The nearby_blocks counterpart: the currently visible tables (layered base / ext).

    With progress_tier < 1 only the base tables are returned; with >= 1 the ext_* tables
    are returned as well. This is the core of "progressive disclosure" —— a beginner who
    cannot see the ext layer has no way to write a bare ext query.

    Prefer querying the database live (when duckdb is present); otherwise fall back to the
    static list of known tables (whether the ext layer is included is decided by the data
    source identity), so every environment has a meaningful scene description.
    """
    out: dict[str, list[str]] = {"base": [], "ext": [], "source": "static"}
    live: list[str] = []
    try:
        import duckdb
        from pathlib import Path as _P
        p = _db_path()
        if _P(p).exists():
            con = duckdb.connect(p, read_only=True)
            try:
                live = [str(r[0]) for r in con.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='main' ORDER BY 1").fetchall()]
            finally:
                con.close()
    except Exception:
        live = []
    if live:
        out["source"] = "live"
        for t in live:
            (out["ext"] if t.startswith("ext_") else out["base"]).append(t)
    else:
        # Static fallback: the vlr/default database has no ext layer, the rib database does
        name = Path(_db_path()).stem
        has_ext = "rib" in name
        out["base"] = list(KNOWN_BASE_TABLES)
        out["ext"] = list(KNOWN_EXT_TABLES) if has_ext else []
    if progress_tier < TIER_EXT:
        out["ext"] = []  # beginner tier: hide the ext layer
    return out


def entity_fillables() -> dict[str, list[str]]:
    """The inventory counterpart: the fillable entity values —— the anchor (the current
    series/team/map) + the catalogue's optional values."""
    inv: dict[str, list[str]] = {"anchor": [], "options": {}}
    try:
        import anchor
        a = anchor.get()
        inv["anchor"] = [f"series={a.get('series','')}", f"team={a.get('team','')}",
                         f"map={a.get('map','')}"]
    except Exception:
        pass
    try:
        import entity_catalog
        for kind in ("series", "team", "map", "player"):
            vals = entity_catalog.options(kind)
            if vals:
                inv["options"][kind] = vals[:12]  # dropdown preview, first 12 only
    except Exception:
        pass
    return inv


def _skill_count() -> int:
    """How many recipes the skill library currently holds (**carries no scoring whatsoever**)."""
    try:
        import skill_store
        return len(skill_store.load())
    except Exception:
        return 0


def panel_only() -> dict[str, Any]:
    """The half that is **for humans only and never enters the model's context**.

    The line the user drew is very clear:
      Goes into the runtime context —— unlocked milestones / skills learned / task failures /
                                       task skips (with reasons)
                                       + environment feedback (did the tool work, how many
                                         rows came back, what changed in the graph)
      Dashboard only               —— ① which values actually exist in the database for the
                                       current entities
                                       ② the verdict from running VLML once (score / coverage)
    Why ① must not enter the context: it is a list of candidate answers, and it would shift
    its focus from "getting the data retrieval to work" to "enumerating values to make up
    an answer".
    Why ② must not enter the context: that is the ruler, not feedback. Once the ruler enters
    the feedback loop it starts currying favour with the ruler (tuning SQL to the coverage)
    instead of completing the task —— user's original words: "it would affect the focus
    with which voyager completes the task".

    The dashboard reads its numbers from here; do not go digging in MasteryStore /
    entity_catalog on its own.
    """
    out: dict[str, Any] = {
        "entity_values": {}, "mastery": {}, "skill_count": _skill_count(),
    }
    try:
        import entity_catalog
        for kind in ("series", "team", "map", "player"):
            vals = entity_catalog.options(kind)
            if vals:
                out["entity_values"][kind] = vals
    except Exception:
        pass
    try:
        from topic_graph import MasteryStore
        ms = MasteryStore()
        out["mastery"] = {
            tid: {"mastery": r.get("mastery"), "completed": ms.is_completed(tid),
                  "stale": ms.is_stale(tid), "attempts": r.get("attempts", 0)}
            for tid, r in ms.all().items()
        }
    except Exception:
        pass
    return out


def mastered_skills() -> dict[str, Any]:
    """The chests counterpart: the skill-library graph —— the mastered knowledge points
    (= the "recipes" already learned).

    ⚠️ **Dashboard only**: what this reads is mastery (≥0.8), which is a "scoring result";
    by convention it only reaches the dashboard (`panel_only()`), never `scene_injection()`.
    """
    out: dict[str, Any] = {"mastered_topics": [], "skill_count": 0}
    try:
        from topic_graph import MasteryStore
        ms = MasteryStore()
        for tid, row in ms.all().items():
            m = row.get("mastery")
            if m is not None and float(m) >= 0.8:
                out["mastered_topics"].append(tid)
    except Exception:
        pass
    try:
        import skill_store
        out["skill_count"] = len(skill_store.load())
    except Exception:
        pass
    return out


def unlocked_milestones() -> list[str]:
    """The milestone counterpart: **completed** knowledge points (tasks that ran through)
    = the unlocked graph nodes. Goes into the context.

    Isomorphic to the original curriculum.render_observation, which writes completed_tasks
    into the human message —— MVE's "completed" is topic_graph's completed flag (the task ran
    through); marking it in the context tells the agent "which milestones are already down,
    no need to go around them again". This is a boolean completed, **not** a mastery
    threshold —— scoring/mastery only reaches the dashboard and drives no logic here.
    """
    try:
        from topic_graph import MasteryStore
        ms = MasteryStore()
        return [tid for tid, r in ms.all().items() if ms.is_completed(tid)]
    except Exception:
        return []


def milestone_tree() -> dict[str, Any]:
    """The MC node milestone tree (enters the context).

    Isomorphic to the original Voyager's "milestone = a completed task" plus MC's
    "you must fell the tree first" tech tree:
      · seed    —— the trunk's starting point (no prerequisite), corresponding to MC's
                  Mine 1 wood log; in MVE it is a base-grain knowledge point, and the
                  spawn injection randomly grants one minimal unit (table, column) as the
                  real "first log of wood".
      · trunk   —— the longest prerequisite path (the design's original wording:
                  "the nodes on the path through the most nodes are the milestones").
      · milestone —— the set of nodes on the trunk.
      · keystone —— the node with the highest betweenness ("the node the most paths pass
                  through" = the thickest point of the trunk).
    The returned structure carries each milestone's completed status, for the panel/context
    to display progress.
    """
    out: dict[str, Any] = {
        "seed": None, "trunk": [], "milestones": [], "keystone": None,
        "completed_n": 0, "total_n": 0, "progress": "0/0",
    }
    try:
        from topic_graph import MasteryStore, TopicGraph
        ms = MasteryStore()
        g = TopicGraph()
        info = g.milestone_info()
        trunk = info["trunk"]
        milestones = set(info["milestones"])
        done = [t for t in trunk if ms.is_completed(t)]
        out["seed"] = info["seed"]
        out["trunk"] = trunk
        out["milestones"] = sorted(milestones)
        out["keystone"] = info["keystone"]
        out["keystone_score"] = info.get("keystone_score")
        out["completed_n"] = len(done)
        out["total_n"] = len(trunk)
        out["progress"] = f"{len(done)}/{len(trunk)}"
        out["completed_list"] = done
    except Exception:
        pass
    return out


def skip_history(limit: int = 8) -> list[dict[str, Any]]:
    """The skipped-task (with reason) counterpart: write stale-skip / give-up into the context
    (design ⑨: skipped tasks with reasons enter the runtime context). Reads the node state
    unrelated to the loop log (MasteryStore's skips).
    """
    out: list[dict[str, Any]] = []
    try:
        from topic_graph import MasteryStore
        ms = MasteryStore()
        for tid, r in ms.all().items():
            for s in (r.get("skips") or [])[-limit:]:
                out.append({"topic": tid, "reason": s.get("reason", "")})
    except Exception:
        return []
    return out[-limit:]


def learned_skills(limit: int = 5) -> list[dict[str, Any]]:
    """The learned-skill counterpart: the data-retrieval recipes (SQL source) already
    solidified in the skill library. Enters the context, available for reuse.

    Isomorphic to the original SkillManager —— the loot in chests can be called directly
    next time. Only the top ones are listed (sorted by ok/hits effectiveness) to avoid
    bloating the prompt. Design ⑦ requires a skill to store "reusable code (SQL)", not a
    sentence or two of intuition, so what is given here is a code snippet, not natural language.
    """
    try:
        import skill_store
        rows = skill_store.all_skills()
        rows.sort(key=lambda s: -(int(s.get("ok") or 0)
                                  / max(int(s.get("hits") or 1), 1)))
        return [{"topic": s.get("topic", ""), "name": s.get("name", ""),
                 "code": str(s.get("code") or s.get("text") or "")}
                for s in rows[:limit]]
    except Exception:
        return []


def failure_history(limit: int = 8) -> list[dict[str, Any]]:
    """The task-failure / skip (with reason) counterpart: write failed tasks into the
    context (isomorphic to the original curriculum.render_observation's failed_tasks).

    Design ⑨ is explicit: task failures and skipped tasks (with the reason…) go into the
    runtime context. What is read here is the loop log run_log (the one the loop writes),
    not the dashboard's state file —— the dashboard only reads it and never writes back
    to the context. Note: only "which dimensions were missed (an enumeration)" and the
    verdict are given, **never the coverage number** (convention ⑧).

    ⚠️ **Deduplication is mandatory** (measured 2026-10-13): if the same knowledge point
      fails 7 times in a row, this would otherwise emit 7 identical lines of
      `vlml_first_blood（wrong）：wrong`. That is not "history", it is noise —— it burns
      the context budget for nothing, and "with reason" becomes a misnomer (`wrong` is
      not a reason). Deduplicate on the (knowledge point, verdict, reason) triple, keeping
      the **most recent** one and recording how many times it happened —— "how many times
      it failed" is more useful than the same line seven times over.
    """
    out: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    try:
        import run_log
        for r in run_log.load_all():
            v = str(r.get("verdict") or "")
            if v in ("wrong", "dont_know") or r.get("done") is False:
                tid = r.get("topic_id") or r.get("task") or ""
                miss = [str(m) for m in (r.get("missing") or [])]
                reason = ", ".join(m.split("(")[0] for m in miss[:3]) or v
                key = (tid, v, reason)
                if key in seen:
                    seen[key]["times"] = int(seen[key].get("times") or 1) + 1
                    continue
                seen[key] = {"topic": tid, "verdict": v, "reason": reason, "times": 1}
        out = list(seen.values())
    except Exception:
        return []
    return out[-limit:]


def random_unit() -> dict[str, Any]:
    """The minimal unit = (table, column), picked at random, with the table name + the
    column's meaning and unit.

    Design ⑩: the initial injection must "randomly grant one of the minimal units", just
    like MC's spawn environment is not hardcoded and transfers to a new world. First get
    the data retrieval for this minimal unit (one column of one table) working, then push
    towards the corresponding milestone (the thickest point of the trunk, the node the most
    paths pass through). The column's meaning/unit comes from knowledge_graph's table
    node column_desc; if unavailable, only the column name is given.
    """
    try:
        tabs = available_tables(TIER_BASE).get("base") or list(KNOWN_BASE_TABLES)
        if not tabs:
            return {}
        tbl = random.choice(tabs)
        cols: dict[str, str] = {}
        try:
            import knowledge_graph
            g = knowledge_graph.KnowledgeGraph.load()
            node = g.nodes.get(f"table:{tbl}") if g is not None else None
            if node:
                cols = dict((node.detail or {}).get("column_desc") or {})
        except Exception:
            cols = {}
        col = random.choice(list(cols.keys())) if cols else ""
        raw = cols.get(col, "") if cols else ""
        # knowledge_graph's column_desc values may be dicts (with desc/type/flag), normalise.
        if isinstance(raw, dict):
            raw = raw.get("desc") or raw.get("type") or ""
        return {"table": tbl, "column": col, "desc": str(raw)}
    except Exception:
        return {}


def scene_injection(topic_id: str = "", *, progress_tier: int | None = None) -> str:
    """Generate the "scene injection" text block for the model's prompt (progressive
    disclosure, tightened or loosened according to progress_tier).

    When progress_tier is not passed it is decided automatically by the **number of
    unlocked milestones** (≥ TIER_EXT_UNLOCKS → ext tier), so that "do not touch ext
    before the base layer is down" is structural rather than a matter of prompting.

    ⚠️ This used to read "does any knowledge point have mastery ≥0.8" —— that treats
       VLML's **scoring result** as a disclosure condition, contrary to the design
       (user's original words: "scoring results are not read by any progression
       condition"). Changed to counting milestones: a milestone is boolean (did this
       question run through / not), containing no scoring at all.
    """
    if progress_tier is None:
        try:
            progress_tier = TIER_EXT if len(unlocked_milestones()) >= TIER_EXT_UNLOCKS \
                else TIER_BASE
        except Exception:
            progress_tier = TIER_BASE

    ds = data_source_identity()
    tabs = available_tables(progress_tier)
    inv = entity_fillables()
    # ⚠️ Deliberately **does not read mastery**: what chests should hold is "the data-retrieval
    #    recipes already learned" (the SQL in skill_store), not "knowledge points scoring ≥0.8".
    #    Using mastery as the gate is sneakily reading VLML's scoring result into the context ——
    #    while the design says scoring only reaches the dashboard and is read by no
    #    progression/disclosure condition (user's original words: "scoring results are not
    #    read by any progression condition").
    skills = {"skill_count": _skill_count()}
    src_note = (" (static known schema)" if tabs.get("source") != "live" else " (queried live)")

    lines: list[str] = []
    lines.append("【Initial scene (the data world)】")
    lines.append(f"· Data source (biome): {ds}")
    lines.append(
        f"· Available tools (semantic layer, always available — do not write any data-retrieval "
        f"method other than bare SQL):"
        + ", ".join(SEMANTIC_TOOLS))
    base_block = ", ".join(tabs["base"]) if tabs["base"] else "(no readable base table)"
    lines.append(f"· Visible tables (nearby_blocks · base tier{src_note}): {base_block}")
    if progress_tier >= TIER_EXT:
        ext_block = ", ".join(tabs["ext"]) if tabs["ext"] else "(this database has no ext layer)"
        lines.append(f"· Visible tables (nearby_blocks · ext tier, unlocked): {ext_block}")
    else:
        # Beginner tier: tell the model explicitly that the ext tier is put away, don't touch it
        lines.append("· Visible tables (ext tier): **not unlocked at this tier**——do not write "
                     "ext_* queries before the base tier is mastered")
    if inv["anchor"]:
        lines.append("· Current entities (inventory · anchors, question generation and answers "
                     "centre on these, do not invent them):"
                     + "; ".join(inv["anchor"]))
    # ⚠️ **Do not give** `inv["options"]` ("which values series/team/map actually have in
    #    the database"). User's original words: "which values the current entities actually
    #    have in the database … is not allowed into the feedback, because it would affect the
    #    focus with which voyager completes the task". The reasoning is very practical: it is a
    #    **list of candidate answers**, equivalent to handing over the options of a
    #    multiple-choice question —— it will "enumerate values to make up an answer" instead
    #    of "getting this question's data retrieval to work". To see which values exist, go to
    #    the dashboard (panel_only()).
    if skills.get("skill_count"):
        lines.append(
            f"· Learned skills (chests): the skill library holds {skills['skill_count']} "
            f"recipes in total; the directly reusable SQL approaches are listed below")

    # —— New isomorphic session fields (design ⑨⑩): the "learning state" fields that enter
    # the runtime context —— these are orthogonal to the biome/nearby_blocks/inventory/chests
    # above: those are "what the world looks like", the ones below are "where I have got to /
    # where I fell over". VLML's verdict and the coverage number are **not** here (convention ⑧,
    # they only reach the dashboard run_log, never the context).
    # The MC node milestone tree (the design: arrange VLML's tables into a tree, the thickest
    # part of the trunk = the milestones):
    mt = milestone_tree()
    if mt.get("trunk"):
        lines.append(
            f"· MC milestone tree (trunk = the longest prerequisite path, the nodes on the path "
            f"with the most nodes are the milestones):"
            f"progress {mt['progress']}")
        lines.append(f"    Seed (must be taken first, corresponding to MC's Mine 1 wood log): "
                     f"{mt['seed']}")
        lines.append(
            f"    Trunk chain: {' → '.join(mt['trunk'])}")
        if mt.get("keystone"):
            lines.append(
                f"    Thickest point of the trunk (keystone, the most paths pass through): "
                f"{mt['keystone']}"
                f" (betweenness {mt.get('keystone_score')})")
        if mt.get("completed_list"):
            lines.append(f"    Unlocked milestones: {', '.join(mt['completed_list'])}")
    ms_topics = unlocked_milestones()
    if ms_topics:
        lines.append(f"· Unlocked milestones (graph nodes · completed): {', '.join(ms_topics)}")
    learned = learned_skills()
    if learned:
        lines.append("· Learned skills (chests · reusable data-retrieval recipes, copy their "
                     "SQL approach):")
        for s in learned:
            code = (s["code"] or "").strip().replace("\n", " ")
            snippet = (code[:160] + "…") if len(code) > 160 else code
            lines.append(f"    - [{s['topic']}] {s['name']}: {snippet}")
    fails = failure_history()
    if fails:
        lines.append("· Past failures (with reasons, avoid repeating them):")
        for f in fails:
            times = int(f.get("times") or 1)
            lines.append(
                f"    - {f['topic']} ({f['verdict']}): {f['reason']}"
                + (f" ×{times}" if times > 1 else ""))
    skips = skip_history()
    if skips:
        lines.append("· Past skips (with reasons, avoid going round in circles again):")
        for s in skips:
            lines.append(f"    - {s['topic']}: {s['reason']}")
    unit = random_unit()
    if unit.get("table"):
        lines.append(
            f"· This run's random spawn minimal unit (table,column): {unit['table']}"
            f".{unit['column'] or '?'} ({unit['desc'] or 'column meaning/unit not registered'})")
        lines.append("    —— First get the data retrieval for this minimal unit working, "
                     "then push towards the corresponding milestone.")

    if topic_id:
        lines.append(f"· Knowledge point in focus this run: {topic_id}")
    return "\n".join(lines)


def _main() -> int:
    print("=== MVE initial scene injection (two tiers of progressive disclosure) ===\n")
    for tier in (TIER_BASE, TIER_EXT):
        label = "beginner tier (base layer)" if tier == TIER_BASE else "unlocked tier (incl. ext layer)"
        print(f"--- progress tier {tier}: {label} ---")
        print(scene_injection(progress_tier=tier))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
