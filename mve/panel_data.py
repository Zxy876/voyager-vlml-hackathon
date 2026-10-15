#!/usr/bin/env python3
"""Dashboard data layer: aggregates run_log.jsonl into the views the dashboard displays.

All contracts mirror the study companion's ui_api.py:
- No evidence = unassessed + None, **not 0%** (ui_api.py:41-47)
- Five-band status weak/progress/good/mastered + unassessed (ui_api.py:62-73)
- flags badges false_mastery / low_confidence (knowledge_tracker.py:298-299, :274)
- Mastery change displayed as "Mastery {before} -> {after}" (i18n zh-CN: ui.practice.mastery_delta_fmt)

VLML has no frontend (no html/tsx/jsx in the repo), so the dashboard form comes from the study companion and the data from MVE.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import run_log  # noqa: E402
import causal_timeline  # noqa: E402
import skill_store  # noqa: E402
import practice_scope  # noqa: E402

ROOT = Path(__file__).resolve().parent


def _task_library() -> list[dict[str, Any]]:
    """Question list (does not import vlml_env, so dashboard startup does not pull in VLML)."""
    try:
        import tasks
        return tasks.all_briefs()
    except Exception as e:
        return [{"topic_id": "?", "question": f"Failed to load the question registry: {e}",
                 "difficulty": 0, "key_points": [], "weights": {},
                 "dims": [], "has_answer_spec": 0}]


def _mc_thinking_view() -> dict[str, Any]:
    """MC thinking-learning import status: whether the 4 domain-agnostic planning thoughts (topic=mc-thinking) have entered the shared library.

    The dashboard's "Skill library" section shows "MC thinking imported: N entries" based on this, making "thinking accumulation imported" visible and provable.
    """
    items = [s for s in skill_store.all_skills() if s.get("topic") == "mc-thinking"]
    skill_md = ROOT / "thought_store" / "SKILL.md"
    return {
        "count": len(items),
        "items": [
            {"name": s.get("name", ""), "text": (s.get("text", "") or "")[:90],
             "hits": s.get("hits", 0), "ok": s.get("ok", 0),
             "versions": s.get("versions", 1)}
            for s in items
        ],
        "skill_md": str(skill_md) if skill_md.exists() else "",
    }


def _referee_view(topic_id: str) -> dict[str, Any]:
    """The referee's (VLML0) standard-answer view — two-layer isolation (course-corrected 2026-10-12).

      · `facts` / `narrative` = the objective referee (DB baseline), used by both Voyager comparison and dashboard scoring;
      · `judge` = the VLML perspective for humans (narrative + LLM estimate), **for reference only, not part of scoring**.

    Only show what already exists (cache hit); when absent mark pending, and do not run live —
    the dashboard is a read view, and rendering should not trigger an LLM orchestration.
    """
    if not topic_id:
        return {"state": "pending", "facts": []}
    try:
        import vlml0_referee
        ans = vlml0_referee.cached(topic_id)
        judge = vlml0_referee.cached_judge(topic_id)
    except Exception as e:
        return {"state": "unavailable", "reason": f"{type(e).__name__}: {e}"[:160], "facts": []}
    if ans is None:
        return {"state": "pending", "facts": []}

    def _fmt(f: dict) -> dict:
        return {
            "subject": "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items()),
            "dimension": f.get("dimension"),
            "value": f.get("value"),
            "base": f.get("base"),
            "source": (f.get("source") or {}).get("tool", ""),
        }

    judge_view: dict[str, Any] = {}
    if judge:
        judge_view = {
            "narrative": judge.get("narrative", ""),
            "llm_facts": [_fmt(f) for f in (judge.get("llm_facts") or [])],
        }
    return {
        "state": "ready" if ans.ok else "empty",
        "facts": [_fmt(f) for f in ans.facts],
        "trajectory": ans.trajectory,
        "narrative": ans.narrative,
        "judge": judge_view,
    }

# Five-band UI status (mirroring the thresholds in ui_api.py:62-73)
def _ui_status(mastery: float | None, flags: list[str]) -> str:
    if mastery is None:
        return "unassessed"
    if "false_mastery" in flags or mastery < 0.40:
        return "weak"
    if mastery < 0.60:
        return "progress"
    if mastery < 0.80:
        return "good"
    return "mastered"


STATUS_LABEL = {
    "unassessed": "Unassessed",
    "weak": "Weak",
    "progress": "In progress",
    "good": "Proficient",
    "mastered": "Mastered",
}

FLAG_LABEL = {
    "false_mastery": "False mastery",
    # V1's binary flag (attempts<3). V2 has switched to continuous discounting and no longer produces it;
    # the label is kept only so **old logs** still display; new snapshots will not carry it.
    "low_confidence": "Insufficient sample size (legacy V1)",
    # Study companion mastery_v2.py:285 — undigested wrong answers cap mastery at 0.79
    "unresolved_wrong_cap": "Has undigested wrong answers",
    # New in V2 (mastery_v2.py:241, :274)
    "no_evidence": "No evidence",
    "zero_confidence_evidence": "Zero evidence weight",
}


def _retention_view() -> dict[str, dict[str, Any]]:
    """Retention (forgetting): how much of each question remains right now.

    V2's projection only answers "to what degree is it mastered now"; this layer answers "how much remains after a while".
    """
    try:
        import mastery_retention
        rows = mastery_retention.STORE.load()
        return {t: mastery_retention.STORE.current(t) for t in rows}
    except Exception:
        return {}


def _pct(v: float | None) -> str | None:
    return None if v is None else f"{v * 100:.0f}%"


def _exam_view() -> dict[str, Any]:
    """**True-level curve** — scaffold-removed exam (exam_log.jsonl).

    Why the dashboard must draw this separately: the old `coverage_track` (retired) read run_log,
    which is coverage from practice **with the knowledge graph** — observed 100% in the first round with the graph,
    forming a flat line pinned to the top from the very first row. It measures the height of the scaffold, not the model's level.
    The numbers from re-examining without the graph (`exam.py`) are what the learning curve should plot.

    Plotted **per question row**, not connected end to end: the 3rd data point may be a question it already knew,
    the 4th one originally at 0%; connecting them into a line makes the shape determined by question selection order, not by learning.
    """
    try:
        import exam
    except Exception:
        return {"has_data": False}
    rows = exam.load()
    if not rows:
        return {"has_data": False,
                "hint": "No bare-exam records yet: python mve/exam.py --all (whole-library placement)",
                # Teaching graph (study companion question generation / graph port): show the topic graph skeleton even with no data
                "teaching": _teaching_payload()}

    by_topic: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_topic.setdefault(str(r.get("topic_id") or ""), []).append(r)

    tracks: list[dict[str, Any]] = []
    for t, seq in by_topic.items():
        # Baseline **anchored to placement**, never "the first record" — after adding the final exam (kind=final),
        # a question's record order no longer guarantees the placement comes first (make-up placements, --force reruns
        # all shuffle it). Once the implicit assumption "first record == placement" breaks, the baseline becomes
        # "the level after practice", and the learning curve vanishes into thin air.
        pl = [r for r in seq if str(r.get("kind") or "") == "placement"]
        anchor = pl[0] if pl else seq[0]
        base = float(anchor.get("coverage") or 0.0)
        rest = [r for r in seq if r is not anchor]
        rest_cov = [float(r.get("coverage") or 0.0) for r in rest]
        finals = [float(r.get("coverage") or 0.0) for r in seq
                  if str(r.get("kind") or "") == "final"]
        tracks.append({
            "topic_id": t,
            "baseline": base,
            "baseline_pct": _pct(base),
            "has_placement": bool(pl),
            "level": exam.level_of(base),
            "exam_count": len(seq),
            "after": [{"cov": c, "pct": _pct(c),
                       "kind": str(r.get("kind") or "")}
                      for c, r in zip(rest_cov, rest)],
            "delta_pp": round((rest_cov[-1] - base) * 100) if rest_cov else None,
            # Latest level gets its own column: `profile()` keeps only the last record, so a post-practice re-exam at 100%
            # displaces the placement. The baseline column alone makes people think "practiced for nothing",
            # and the latest column alone makes people think "it already knew" — both columns must be given together.
            "latest": rest_cov[-1] if rest_cov else base,
            "latest_pct": _pct(rest_cov[-1] if rest_cov else base),
            "latest_level": exam.level_of(rest_cov[-1] if rest_cov else base),
            # Final exam gets its own column: re-examine the whole library with the skill library, answering "how much remains after learning so far"
            "final": finals[-1] if finals else None,
            "final_pct": _pct(finals[-1]) if finals else None,
            # Practiced but bare exam still does not rise → graduated and make way (the study companion has no such line; MVE's own)
            "exhausted": bool(exam.exhausted(t)),
        })
    tracks.sort(key=lambda x: (x["baseline"], x["topic_id"]))

    deltas = [x["delta_pp"] for x in tracks if x["delta_pp"] is not None]
    covs = [float(x["baseline"]) for x in tracks]
    lates = [float(x["latest"]) for x in tracks]
    transfer = [r for r in rows if str(r.get("kind") or "") == "transfer"]
    tseq = [float(r.get("coverage") or 0.0) for r in transfer]
    return {
        "has_data": True,
        "tracks": tracks,
        "avg_baseline": round(sum(covs) / len(covs) * 100) if covs else 0,
        "avg_latest": round(sum(lates) / len(lates) * 100) if lates else 0,
        "avg_delta_pp_all": (round((sum(lates) / len(lates) - sum(covs) / len(covs)) * 100)
                             if covs else 0),
        "retested": len(deltas),
        "rose": sum(1 for d in deltas if d > 0),
        "avg_delta_pp": round(sum(deltas) / len(deltas)) if deltas else None,
        "mastered": sum(1 for c in covs if c >= 0.80),
        "mastered_now": sum(1 for c in lates if c >= 0.80),
        # Final exam: re-examine the whole library with the current skill library. Its difference from "post-practice re-exam" is coverage —
        # the post-practice re-exam only tests the question just practiced; the final exam is the whole library, so the averages are comparable.
        "final": _final_view(tracks, covs),
        # Transfer control: do unpracticed questions rise — telling "memorized this question" from "truly learned"
        "transfer": {
            "count": len(tseq),
            "seq": [_pct(c) for c in tseq],
            "rose": bool(len(tseq) >= 2 and tseq[-1] > tseq[0] + 0.001),
            # Transfer success rate: the share truly "solved independently" in unpracticed questions' independent exams.
            # Exam records have no success boolean; coverage≥1.0 is its achieved form
            # (all rubric points covered with correct values). This is the hardest signal in the criterion area.
            "ok_count": sum(1 for c in tseq if c >= 1.0),
        } if tseq else None,
    }


def _final_view(tracks: list[dict[str, Any]],
                baselines: list[float]) -> dict[str, Any] | None:
    """Final exam summary: the level after re-examining the whole library with the skill library and without the graph.

    Why it must be a separate column: the `latest` column is "each question's last exam", and post-practice re-exams at 100% are included,
    but it covers only the **practiced** questions — using it as "the current level" leaves out unpracticed questions.
    The final exam is the **whole library**, so its average is comparable with the placement average (same denominator).
    """
    vals = [float(t["final"]) for t in tracks if t.get("final") is not None]
    if not vals:
        return None
    b = sum(baselines) / len(baselines) if baselines else 0.0
    f = sum(vals) / len(vals)
    return {
        "count": len(vals),
        "avg": round(f * 100),
        "avg_baseline": round(b * 100),
        "delta_pp": round((f - b) * 100),
        "mastered": sum(1 for v in vals if v >= 0.80),
        "covered_all": len(vals) == len(tracks),
    }


def _human_imports(limit: int = 10) -> list[dict[str, Any]]:
    """History of human imports (reads the file directly, does not import coach — that pulls in the whole VLML).

    The midwife's exact words from round two: in the study companion's dashboard "learners can import question text and content in exchange for the system's
    memory and explanation of it"; ours is isomorphic, except the provider of memory and explanation is swapped to VLML.
    """
    log = ROOT / "coach_log.jsonl"
    if not log.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-limit:]


def _ability_universe_size() -> int:
    """The size of the full capability set (the **denominator** of mastery): graph nodes + capability items registered on the **consumption side**.

    ⚠️ Why the practice line's `runtime_*` entries are excluded: `runtime_topics.json` is **the same file shared** by the practice line
    (`voyager_vlml/loop.py:_save_runtime_topic`) and the consumption line
    (`voyager_vlml/consume.py:_runtime_topic_id`),
    and of the 16 live entries, **12 are `source="practice"`**. Questions registered by the practice line are
    "what the curriculum plans to practice", not "what humans asked", and counting them in the denominator only dilutes mastery — moreover the consumption side
    will most likely never answer those questions, equivalent to 12 unearnable points lying in the denominator.

    Same convention as `consume._ability_universe()` (a set there, a size here).
    """
    n = 0
    try:
        import topic_graph
        n = len(topic_graph.TopicGraph().topics)
    except Exception:
        n = 0
    try:
        p = ROOT / "runtime_topics.json"
        if p.exists():
            reg = json.loads(p.read_text(encoding="utf-8"))
            n += sum(1 for k, v in (reg or {}).items()
                     if str(k).startswith("consume_")
                     or str((v or {}).get("source") or "") == "consume")
    except Exception:
        pass
    return n


def _consume_curve(limit: int = 40) -> list[dict[str, Any]]:
    """**Consumption progress curve** — decided by the user on 2026-10-13: "measuring Voyager's progress curve and mastery
    can all just be measured on the consumption side".

    Why consumption and not practice:
      Practice (run_log) is not scored — VLML's ruler cannot enter Voyager's feedback loop, so that line
      only has "was data retrieved or not", and the curve it plots is "is the pipeline working", not "is the level high".
      Consumption (a human asks → Voyager answers with accumulated capability) is the **delivery**: the human really wants an answer,
      and measuring coverage by having the VLML0 referee run independently at that point is the real progress curve.

    The curve plots the **cumulative mastered count** (`mastered_total`) — mastery is not a score discounted from coverage,
    but "answered → this question enters the capability library → mastered" (defined by the user on 2026-10-13).
    So progress = the capability set growing, with the y axis being "how many questions it can do".

    ⚠️ **Learning does not count** (decided by the user on 2026-10-14, mirroring the original Voyager):
      The original `voyager/agents/curriculum.py:135` learning curve = `len(self.completed_tasks)`,
      `:339` appends **only on success**, and the original has no concept of "exams" at all —
      learning curve = **the size of the capability set**; a practice success only advances curriculum planning and does not count as capability growth.
      MVE's practice line (`curriculum.py:296` / `run_mve.py:463`) also writes
      `MasteryStore`'s `completed`, but that is a **dedup bit** ("planned already, do not plan again"),
      not mastery — so this curve **only counts questions answered on the consumption side**, never reading MasteryStore.

    ⚠️ `mastered_total` is **recomputed here**, not trusting the number computed at record time:
      once the convention changes, old numbers hard-written in history do not update themselves — that old server record is
      `58 / 45` (129%, numerator bigger than denominator), and plotting it directly pins the curve's first point to the top and past 100%.
      The recompute rule is the original's line: `len(completed_tasks)` — answered +1, failures not counted.

    Coverage is recorded on a separate line (that is the quality score of how completely this answer covered, not mastery).
    Old human imports (pure VLML explanations) lack these fields and are skipped entirely —
    None must not be filled in as 0, which would plot "not measured" as "measured 0".
    """
    out: list[dict[str, Any]] = []
    # The y axis uses the cumulative mastered count (original convention); `universe` is only a reference display for "how many questions total"
    universe = _ability_universe_size()
    book: set[str] = set()
    for i, rec in enumerate(
        [r for r in _human_imports(limit=limit)
         if r.get("mode") == "voyager_consume"],
        start=1,
    ):
        # Cumulative: only **answered** enters the capability set (failures not counted, mirroring the original :339)
        if rec.get("mastered") and str(rec.get("topic_id") or "").strip():
            book.add(str(rec.get("topic_id")).strip())
        cov = rec.get("coverage")
        out.append({
            "i": i,
            "ts": rec.get("ts", ""),
            "question": str(rec.get("question") or "")[:80],
            "topic_id": str(rec.get("topic_id") or ""),
            "coverage": float(cov) if cov is not None else None,
            "score": rec.get("score"),
            "verdict": str(rec.get("verdict") or ""),
            # Mastery (capability set) — **recomputed** here, not trusting the old-convention number hard-written in records
            "mastered": bool(rec.get("mastered")),
            "mastered_total": len(book),
            "ability_total": max(universe, len(book)),
            "skill_action": str(rec.get("skill_action") or ""),
            "reused": bool(rec.get("reused")),
            # —— Comparison conclusions of method vs approach (the basis of scoring; people must see "on what grounds this score was given") ——
            "judge": str(rec.get("judge") or ""),
            "method_reason": str(rec.get("method_reason") or ""),
            "method_diff": str(rec.get("method_diff") or ""),
            "same_metric": bool(rec.get("same_metric")),
            "voyager_sound": bool(rec.get("voyager_sound")),
            "judged": bool(rec.get("judged")),
            "referee_rows": int(rec.get("referee_rows") or 0),
            "coverage_referee": (float(rec["coverage_referee"])
                                 if rec.get("coverage_referee") is not None else None),
            "verdict_referee": str(rec.get("verdict_referee") or ""),
            "rows": int(rec.get("rows") or 0),
        })
    return out


def _db_view() -> dict[str, Any]:
    """Which database is currently pointed at (does not import vlml_env, to avoid dashboard startup pulling in the whole VLML)."""
    try:
        import db_switch
        return {"path": db_switch.current_path(), "is_default": db_switch.is_default()}
    except Exception as e:
        return {"path": "", "is_default": True,
                "error": f"{type(e).__name__}: {e}"[:160]}


def _causal_view(limit: int = 25) -> dict[str, Any]:
    """Action causal timeline (control / attempt / referee / narrative on the same monotonic sequence).

    The dashboard must show it because: human imports **do not enter mastery**, and if the dashboard only had a mastery view,
    people would feel their imports vanished without a trace. This line is the only evidence that "the import really happened".
    """
    try:
        rows = causal_timeline.for_panel(limit)
        counts = causal_timeline.counts_by_topic()
    except Exception as e:
        return {"rows": [], "counts": {}, "reason": f"{type(e).__name__}: {e}"[:160]}
    return {
        "rows": rows,
        "counts": {
            t: {k: v for k, v in c.items() if v}
            for t, c in counts.items()
        },
        "reason": "",
    }


def _pilot_view() -> dict[str, Any]:
    """Cockpit status (the dashboard only reads; it holds no process handle).

    Why not hang the process on the dashboard: a dashboard restart loses the handle, and after pressing "start"
    the human cannot tell whether it is running, which is worse than not offering the button. So status is written to a file, and this only reads the file.
    """
    try:
        import pilot
        st = pilot.read()
        return {**st, "tail": pilot.tail(80)}
    except Exception as e:
        return {"running": False, "tail": "", "why": f"Cockpit unavailable: {type(e).__name__}: {e}"[:160]}


def _voyager_view() -> dict[str, Any]:
    """Voyager real-learning cockpit status (the dashboard only reads; it holds no process handle).

    Isomorphic to _pilot_view: status is written to voyager_pilot_state.json, and progress is
    written to voyager_progress.json per task by run_voyager_learn.py. This only reads files,
    and additionally attaches the **existence** of the skill library / curriculum progress / events directory — so people see at a glance "what is in the library now".
    """
    try:
        import voyager_pilot
        from voyager_paths import SKILL_DIR, CURRICULUM_DIR, EVENTS_DIR
        st = voyager_pilot.read()
        return {
            **st,
            "tail": voyager_pilot.tail(80),
            "skill_dir_exists": SKILL_DIR.exists(),
            "curriculum_dir_exists": CURRICULUM_DIR.exists(),
            "events_dir_exists": EVENTS_DIR.exists(),
        }
    except Exception as e:
        return {"running": False, "tail": "", "why": f"Voyager cockpit unavailable: {type(e).__name__}: {e}"[:160]}


def _next_recommend() -> dict[str, Any]:
    """The question generator's current recommendation (with reason). After a human import, this should change accordingly."""
    try:
        import planner
        return planner.next_brief()
    except Exception as e:
        return {"topic_id": "", "reason": "unavailable",
                "reason_label": "Unavailable", "explanation": f"{type(e).__name__}: {e}"[:160],
                "question": "", "difficulty": 0}


def probe_env() -> dict[str, Any]:
    """Probe whether VLML is available.

    The midwife's requirement from round three: when MCP is unreachable the dashboard must say so clearly, not silently display empty.
    No retries here; only report status + reason, and let the human decide the next step.
    """
    try:
        import vlml_env  # noqa: F401  (chdirs + patches)
        from vlml_env import get_database_info
        import asyncio

        info = asyncio.run(get_database_info())
        if isinstance(info, dict) and info.get("error"):
            return {"ok": False, "state": "degraded",
                    "reason": str(info["error"])[:200]}
        # Return structure verified: available_tables(list) / statistics(dict) / usage_tips(list)
        tables = list(info.get("available_tables") or [])
        stats = dict(info.get("statistics") or {})
        return {
            "ok": True,
            "state": "ready",
            "tables": len(tables),
            "events": stats.get("total_events") or stats.get("total_base_events"),
            "series": len(info.get("recent_series") or []),
            "usage_tips": len(info.get("usage_tips") or []),
            "reason": "",
        }
    except Exception as e:  # Any failure must be displayable on the dashboard
        return {"ok": False, "state": "unavailable",
                "reason": f"{type(e).__name__}: {e}"[:200]}


def build_state() -> dict[str, Any]:
    rows = run_log.load_all()
    memory = skill_store.load()
    # Whether bloat/hallucination prevention is self-evident comes down to these two numbers
    skill_stats = skill_store.stats()
    # Sort: **most effective (high ok) first**, then most injected, with never-used ones last.
    #
    # Unsorted, this list is in insertion order — before the reuse link, all 25 entries showed 0/0 with no difference;
    # after connecting (observed hits 9 / ok 5), the 2 entries truly reused and helpful would drown at
    # the very bottom of 31 entries, and people could not see at all "which lessons are really in use".
    # Semantics of hits / ok (skill_store.py:313 / :327):
    #   hits = how many times injected into the prompt via retrieval; ok = how many times judged successful this round after injection.
    #   "hits rise, ok does not" = injected but did not help, which is the signal to sink it to the bottom.
    _detail = [
        {
            "key": s.get("key", ""),
            "topic": s.get("topic", ""),
            "name": s.get("name", ""),
            "text": s.get("text", ""),
            "hits": int(s.get("hits") or 0),
            "ok": int(s.get("ok") or 0),
            "versions": s.get("versions", 1),
            "rate": (
                round(int(s.get("ok") or 0) / int(s.get("hits") or 1), 3)
                if int(s.get("hits") or 0) else None
            ),
        }
        for s in skill_store.all_skills()
    ]
    _detail.sort(key=lambda x: (-x["ok"], -x["hits"]))
    skill_detail = _detail

    # Human import path: these two blocks must display even when "nothing has run yet" — they are exactly where people start
    imports = _human_imports()
    recommend = _next_recommend()
    causal = _causal_view()
    pilot_view = _pilot_view()
    db = _db_view()
    # Practice scope (set by pressing "Practice this topic" on the knowledge graph page). The dashboard only reads, never infers.
    scope = practice_scope.get_scope()

    if not rows:
        return {
            "has_data": False,
            "env": probe_env(),
            "db": db,
            "pilot": pilot_view,
            "voyager": _voyager_view(),
            "practice_scope": scope,
            "imports": imports,
            # Consumption progress curve: given as usual even when run_log is empty (e.g. right after clearing the library) —
            # progress is measured on the consumption line, so practice never having run does not affect it
            "consume_curve": _consume_curve(),
            "recommend": recommend,
            "causal": causal,
            "tasks": _task_library(),
            "skills": memory,
            "skill_stats": skill_stats,
            "skill_detail": skill_detail,
            "mc_thinking": _mc_thinking_view(),
            # Placement/exams go through exam_log.jsonl and are **never written** to run_log.jsonl (deliberately separated:
            # run_log is practice coverage with the graph, constant 100%, plotting as a fake flat line).
            # So "run_log empty" ≠ "no data at all" — after a placement runs it must still be shown.
            # Observed consequence of omitting this: after 7 placement questions ran live, the overview page still showed
            # "No run records yet", and the true-level curve never appeared at all.
            "exam_curve": _exam_view(),
            # The navigation's "trajectory N rounds" reads this; without it, it is "undefined rounds"
            "total_rounds": 0,
            "topic_id": "",
            "hint": ("No practice records yet. Placement/exams do not enter run_log (they go through exam_log) —"
                     "the display below appears as usual once bare-exam data exists."),
        }

    # ---- Global progress sequence (continuously numbered across runs) ----
    series: list[dict[str, Any]] = []
    for i, r in enumerate(rows, start=1):
        flags = list(r.get("flags") or [])
        m = r.get("mastery")
        series.append({
            "i": i,
            "ts": r.get("ts", ""),
            "mode": r.get("mode", ""),
            # topic_id must be included: the exported md's "mastery (by question)" and round details both rely on it
            # for grouping/display. Previously only "task" was mapped (the --topic passed on the command line, often empty),
            # so the question column in exports was entirely empty.
            "topic_id": str(r.get("topic_id") or r.get("task") or ""),
            "task": r.get("task", ""),
            "round": r.get("round"),
            "coverage": r.get("coverage"),
            "score": r.get("score"),
            "verdict": r.get("verdict", ""),
            # VLML observation: all rubric points achieved (exam ruler). Old logs lack this column → False
            "success": bool(r.get("success")),
            # Criterion-layer boolean: task done (Voyager's ruler: orchestration ran + data retrieved + no crash,
            # coverage points not counted). Old logs lack this column → False.
            "done": bool(r.get("done")),
            "evidence": r.get("evidence_status", ""),
            "mastery": m,
            "mastery_pct": _pct(m),
            "ui_status": _ui_status(m, flags),
            "status_label": STATUS_LABEL[_ui_status(m, flags)],
            "level": r.get("level"),
            "flags": [{"key": f, "label": FLAG_LABEL.get(f, f)} for f in flags],
            # New in V2: evidence count / recency (freshness after time decay) / retention
            "evidence_count": r.get("mastery_evidence_count"),
            "mastery_recency": r.get("mastery_recency"),
            "retention": _retention_view().get(str(r.get("topic_id") or "")),
            "facts": r.get("facts_count"),
            "judge": r.get("judge", ""),
            # Narrative section (comparable always False, not part of comparison)
            "narrative": r.get("narrative", ""),
            "insights": r.get("insights") or [],
            "caveats": r.get("caveats") or [],
            "narrative_comparable": bool(r.get("narrative_comparable", False)),
            "narrative_based_on": r.get("narrative_based_on", 0),
            "no_tool_calls": bool(r.get("no_tool_calls")),
            "unjudgeable": r.get("unjudgeable") or [],
            "referee_facts": r.get("referee_facts"),
            "trajectory": r.get("trajectory") or [],
            "covered": r.get("covered") or [],
            "missing": r.get("missing") or [],
            "rejected": r.get("rejected_low_base") or [],
            "skill_count": r.get("skill_count"),
            # Hallucination prevention: facts dropped by provenance validation this round
            "hallucinations": r.get("hallucinations") or [],
            "hallucination_count": int(r.get("hallucination_count") or 0),
        })

    hallucination_total = sum(s["hallucination_count"] for s in series)

    # ---- Reading: **scaffold-removed exam takes priority** ----
    #
    # This section used to look only at run_log's coverage trajectory, and what it judged was wrong — observed:
    # after the server ran 7 placement questions (19% average) + 8 post-practice re-exams (all 100%),
    # the exported "reading" said "perfect score in the first round: this task is too easy for the model to measure a learning curve".
    # The 8 100% figures behind that were all practiced **with the knowledge graph** (observed: perfect score in the first round with the graph),
    # measuring the height of the scaffold, not the model's level. The reality is 19% → 100%, a rise of 81pp.
    #
    # So the reading order is changed: when scaffold-removed exam data exists, it takes precedence, and the run_log line is demoted to a footnote.
    cov = [s["coverage"] for s in series if s["coverage"] is not None]
    mas = [s["mastery"] for s in series if s["mastery"] is not None]
    exam_curve = _exam_view()

    if exam_curve.get("has_data") and exam_curve.get("retested"):
        b = exam_curve["avg_baseline"]
        l = exam_curve["avg_latest"]
        n = exam_curve["retested"]
        up = exam_curve["rose"]
        d = exam_curve["avg_delta_pp"] or 0
        # The plus sign in "+84pp" gets escaped by export_md's escape_markdown into "\+84pp",
        # so the two words "rose"/"fell" replace the symbol.
        word = "rose" if d > 0 else "fell"
        if d > 0:
            reading = "learned"
            reading_text = (
                f"Scaffold-removed exam: placement {b}% → post-practice re-exam {l}% ({up} of {n} re-examined questions rose,"
                f"average {word} {abs(d)}pp) — the true level has risen."
            )
        elif d < 0:
            reading = "regressed"
            reading_text = (f"Scaffold-removed exam: placement {b}% → post-practice re-exam {l}% (average {word} {abs(d)}pp)"
                            f" — regressed instead; check whether the skill library introduced wrong lessons.")
        else:
            reading = "flat"
            reading_text = (f"Scaffold-removed exam: placement {b}% → post-practice re-exam {l}% (flat) — "
                            f"practiced without rising; learning did not occur or the task is saturated.")
    elif exam_curve.get("has_data"):
        reading = "insufficient"
        reading_text = (f"Placement done at {exam_curve['avg_baseline']}% ({len(exam_curve['tracks'])} questions),"
                        f"not yet re-examined — run the \"learning units\" to get a Δ.")
    elif len(cov) >= 2:
        first_full = cov[0] >= 1.0
        if first_full:
            reading = "too_easy"
            reading_text = ("Perfect score in the first practice round: note this was run **with the knowledge graph**, measuring the scaffold, not the level."
                            "To judge whether learning happened, run placement + post-practice re-exam (scaffold-removed exam).")
        elif cov[-1] > cov[0]:
            reading = "learned"
            reading_text = f"Coverage {cov[0]:.0%} → {cov[-1]:.0%}: more complete after filling the gaps."
        elif cov[-1] < cov[0]:
            reading = "regressed"
            reading_text = f"Coverage {cov[0]:.0%} → {cov[-1]:.0%}: regressed instead; check whether the skill library introduced wrong lessons."
        else:
            reading = "flat"
            reading_text = f"Coverage stable at {cov[-1]:.0%}: no change; learning did not occur or the task is saturated."
    else:
        reading = "insufficient"
        reading_text = "Insufficient sample size (fewer than 2 attempts); progress cannot be read."

    # Rising mastery cannot be used as evidence: in V2, confidence rises with the **sum of evidence weights**,
    # and weights are pulled down by time decay and evaluator confidence, but **grinding rounds can still push it up**.
    # The criterion must use the criterion layer's success (self-verification boolean), not coverage,
    # and certainly not mastery — mastery is only an independent assessment, never a learning criterion.
    mas_warning = (
        "mastery's confidence = 1-exp(-sum of evidence weights/4), which repeated answering still pushes up;"
        "it is only an independent assessment. For the criterion, look at success (all rubric points achieved) in the criterion area."
        if len(mas) >= 2 else None
    )

    # ⚠️ The "does the orchestration trajectory get more complex" path (traj_sizes / tool_sets / new_tools) is **retired**:
    #   the run_log.trajectory it reads is only written by the frozen old backend mve/run_mve.py,
    #   while the new backend voyager_vlml/loop.py does not persist the tool sequence into run_log — across 36 live records it
    #   is always empty, always computing to [0,0,...] and []. The dashboard's "orchestration trajectory" table is retired accordingly.
    #   "which tools this round called, success/failure, rows retrieved" is instead provided by the
    #   loop heartbeat in mve/loop_state.json (tool_calls / tool_ok / tool_fail / rows), which is genuinely produced.
    last = series[-1]
    prev_mastery = series[-2]["mastery"] if len(series) >= 2 else None

    attempted = run_log.attempted_counts()
    failed = run_log.failed_counts()
    topic_id = str(rows[-1].get("topic_id") or "")

    return {
        "has_data": True,
        "env": probe_env(),
        "db": db,
        "pilot": pilot_view,
        "voyager": _voyager_view(),
        "practice_scope": scope,
        "question": rows[-1].get("question", ""),
        "topic_id": topic_id,
        # Question generator: mirrors the study companion's practice_scope selection ordering
        "referee": _referee_view(topic_id),
        "tasks": _task_library(),
        # Human import path: what was imported + which question the question generator therefore changed to recommending
        "imports": imports,
        # Consumption progress curve: progress and mastery are **measured on the consumption line** (practice is not scored)
        "consume_curve": _consume_curve(),
        "recommend": recommend,
        "causal": causal,
        "attempted": attempted,
        "failed": failed,
        "total_rounds": len(series),
        "series": series,
        "reading": reading,
        "reading_text": reading_text,
        "mastery_warning": mas_warning,
        # ── Retired trajectory keys (indexed by backend, 2026-10-10) ──────────────────────
        # The following top-level keys were originally for the dashboard to draw lines, and now have **no consumers at all**
        # (the dashboard computes live from series on the client, and no longer wants server-fed trajectories):
        #     runs / coverage_track / done_track / success_track /
        #     mastery_track / trajectory_sizes / new_tools
        # Of these, coverage_track / mastery_track are constant empty arrays on the new backend (the practice line is not scored),
        # and trajectory_sizes / new_tools are constant 0 / [] (the new backend does not persist trajectory).
        # Keeping them just hangs a row of "0"s and empty arrays on the dashboard, looking like "it did not learn", when in fact nobody produces them.
        # Compute live from series when needed; do not stuff them into state again.
        "last_done": bool(series[-1]["done"]),
        # VLML observation: whether the most recent round achieved all rubric points (exam ruler)
        "last_success": bool(series[-1]["success"]),
        "skills": memory,
        # Bloat prevention: write count vs actual entries (bloat=1 means every write became a new entry, i.e. not blocked)
        "skill_stats": skill_stats,
        "skill_detail": skill_detail,
        "mc_thinking": _mc_thinking_view(),
        # Hallucination prevention: fabricated numbers caught by provenance validation
        "hallucination_total": hallucination_total,
        # Note: the new backend (practice line) **does not write mastery**, so this is usually None.
        # Previously, with no mastery value it returned "Mastery None", hanging a line of noise on the exported md.
        "mastery_delta": (
            f"Mastery {_pct(prev_mastery)} -> {_pct(last['mastery'])}"
            if prev_mastery is not None and last["mastery"] is not None
            else (f"Mastery {_pct(last['mastery'])}" if last["mastery"] is not None else None)
        ),
        # True-level curve (scaffold-removed exam) — the column the learning curve should look at
        # (the reading above already computed it once; reuse it here, do not read exam_log again)
        "exam_curve": exam_curve,
        # Teaching graph (study companion question generation / graph port): prerequisite gating + weak-first + mastery overlay.
        # Coexists with the scoring side's /api/graph: one governs "what to learn next", the other "dimension↔tool".
        # Lazy loading + exception fallback: topic_graph reads JSON seeds, and a failure does not drag down the whole dashboard.
        "teaching": _teaching_payload(),
    }


def _teaching_payload() -> dict:
    """Teaching graph payload (lazy loading). On failure returns an empty shell, and the adaptive tab degrades to "topic graph"."""
    try:
        import topic_graph
        return topic_graph.get_graph().to_graph_payload()
    except Exception as e:  # The teaching graph failing must never drag down the whole dashboard
        return {"schema_version": 3, "difficulty_target": 1, "nodes": [],
                "edges": [], "layers": {}, "total": 0,
                "error": f"{type(e).__name__}: {e}"[:200]}


def as_json() -> str:
    return json.dumps(build_state(), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    print(as_json())
