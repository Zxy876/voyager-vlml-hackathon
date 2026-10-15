#!/usr/bin/env python3
"""**Scaffold-removal exam** —— the real data source behind the learning curve.

Why it has to be its own file
--------------------------
During practice the knowledge graph is handed over and the model hits 100% in one round (measured:
a cleared library plus `--hint=none` still scored full marks on the first attempt). That is not
"it learned", that is **the scaffold holding it up**. Joining those 100%s into a line draws a flat
line pinned to the ceiling from the very first row —— pretty, but false.

The endpoint of scaffolding is anyway "it can still do it after the scaffold is removed". So:

    practice round: graph ON  (gives conventions, column meanings, skeleton, typical failure modes)
                    —— this is teaching
    exam round:     graph OFF —— only the question stem + the tool catalogue + **the skill library
                    it accumulated itself**

Only the exam round's coverage is an observable measure of "how much was learned". Only if it can
climb does it deserve to be called a learning curve.

The study companion has **no** counterpart implementation (its mastery is produced by practicing and
does not separate "right because it was helped" from "right on its own" —— it merely applies a 0.85
factor on `used_hint`). Here "removing the scaffold" is a **real re-exam**, not a prorated estimate.

Artifacts
----
`exam_log.jsonl`: one line per exam.
    {"at":..., "topic_id":..., "coverage":..., "verdict":...,
     "skills": <skill library size at the time>, "practice_rounds": <cumulative practice rounds>}

Recording `skills` alongside is what lets us tell two kinds of climb apart:
  · the skill library grew AND the exam score grew → it really learned (skills were reused)
  · the skill library did not grow but the exam score did → the model was just lucky / knew the
    stem by heart, which does not count

How to run it
----
    python mve/exam.py --topic max_losing_streak_map      # exam once
    python mve/exam.py --all                              # exam the whole bank (baseline exam)
    python mve/exam.py --profile                          # print the bare-exam profile (true level)
    python mve/exam.py --curve                            # print the existing exam curve

**Bare-exam coverage = this question's true level**, and that is the number the question generator
(planner) reads —— it replaces what used to be "practice coverage", and practice is done with the
graph in hand (measured: always 100%, which is the scaffold holding it up, not the level).
See `planner._true_level()`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

EXAM_LOG = HERE / "exam_log.jsonl"

import vlml_env  # noqa: F401,E402  must bootstrap the environment first
import voyager as voyager_mod  # noqa: E402
from loop_core import evaluate_vs_referee  # noqa: E402
import run_log  # noqa: E402
import skill_store  # noqa: E402
import tasks as tasks_mod  # noqa: E402
import vlml0_referee  # noqa: E402


def _practice_rounds() -> int:
    """Cumulative practice rounds (excluding exams) —— i.e. where on the x axis an exam point sits."""
    try:
        return len([l for l in run_log.LOG.read_text(
            encoding="utf-8", errors="replace").splitlines() if l.strip()])
    except Exception:
        return 0


def append(rec: dict[str, Any]) -> None:
    with EXAM_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load() -> list[dict[str, Any]]:
    if not EXAM_LOG.exists():
        return []
    return [json.loads(l) for l in EXAM_LOG.read_text(
        encoding="utf-8", errors="replace").splitlines() if l.strip()]


# ---- Bare-exam profile (true level) ----
#
# Why a separate notion of "level" instead of reusing coverage from the practice log: practice is
# done **with the knowledge graph** in hand, and the graph feeds conventions, column meanings and
# the structural skeleton straight into the prompt —— measured: with the graph the first round
# already scores 100%, without it the average is only 29%. So practice coverage measures "the height
# of the scaffold", not "the model's level". What the question generator must match is the level,
# so it has to read this.
#
# Level labels copied verbatim from the study companion `knowledge_tracker.py:240-250`, no word
# changed:
#   <0.20 untouched / <0.40 weak / <0.60 in progress / <0.80 proficient / ≥0.80 mastered
LEVELS = ((0.20, "untouched"), (0.40, "weak"), (0.60, "in progress"),
          (0.80, "proficient"), (1.01, "mastered"))

# The weak line used by the study companion's `get_weak_topics()` (knowledge_tracker.py:2073: `mastery < 0.60`)
WEAK_LIMIT = 0.60
# The study companion's "mastered" line (both difficulty_policy and mastery_v2 use 0.80)
MASTERED_LIMIT = 0.80


def level_of(cov: float) -> str:
    for edge, name in LEVELS:
        if cov < edge:
            return name
    return "mastered"


def profile() -> dict[str, dict[str, Any]]:
    """The result of each question's **most recent** scaffold-free exam (later records override
    earlier ones).

    Questions with no record do not appear here —— that is "untouched", not "a zero score".
    The two are handled differently: untouched needs a baseline exam first, a zero score needs practice.
    """
    out: dict[str, dict[str, Any]] = {}
    for r in load():
        t = str(r.get("topic_id") or "")
        if not t:
            continue
        rec = dict(r)
        rec["coverage"] = float(rec.get("coverage") or 0.0)
        rec["level"] = level_of(rec["coverage"])
        out[t] = rec
    return out


def profile_split() -> dict[str, dict[str, Any]]:
    """Each question's **baseline exam** and **latest level** returned separately —— do not keep only
    the last number.

    `profile()` keeps only the most recent record, which is semantically fine ("what is the level
    right now"), but using it alone to read the profile goes wrong: a re-exam after practice scores
    100%, which **overwrites** the baseline's 19%, so the profile looks like "it knew all along" and
    the learning curve vanishes into thin air.

    This is exactly what happened in practice: after running a 7-question baseline exam on the
    server (average 19%) plus 8 post-practice re-exams (all 100%), `exam.py --profile` printed
    "all 100% · average 100%", while the per-question baseline average on the dashboard was still
    19% —— the same exam_log, two views, two conclusions. The baseline has to be kept: the curve is
    "baseline → now", and with only "now" there is no curve.

    The baseline is **anchored to the first record with kind=="placement"**, not to "the first
    record": now that the final exam (kind=="final") exists, one question's records may contain both
    a baseline and a final exam, and the baseline is not necessarily first (practiced first and
    baselined later, or the final exam re-run with --force). "The first record == the baseline" is an
    implicit assumption; once it breaks, the baseline silently becomes "the level after practising".
    """
    out: dict[str, dict[str, Any]] = {}
    for r in load():
        t = str(r.get("topic_id") or "")
        if not t:
            continue
        cov = float(r.get("coverage") or 0.0)
        is_placement = str(r.get("kind") or "") == "placement"
        rec = out.get(t)
        if rec is None:
            rec = out[t] = {"topic_id": t, "baseline": cov, "latest": cov,
                            "exams": 0, "kind": "", "verdict": "",
                            "has_placement": False}
        # The baseline only counts the baseline exam; before any placement record exists the first record
        # stands in, and is swapped out as soon as the baseline exam arrives
        if is_placement and not rec["has_placement"]:
            rec["baseline"] = cov
            rec["has_placement"] = True
        elif not rec["has_placement"] and rec["exams"] == 0:
            rec["baseline"] = cov
        rec["exams"] += 1
        rec["latest"] = cov
        rec["kind"] = str(r.get("kind") or "")
        rec["verdict"] = str(r.get("verdict") or "")
    for r in out.values():
        r["baseline_level"] = level_of(r["baseline"])
        r["latest_level"] = level_of(r["latest"])
        r["delta_pp"] = round((r["latest"] - r["baseline"]) * 100)
    return out


def true_level(topic_id: str) -> float | None:
    """This question's true level; **returns None when never bare-examined** (not 0.0).

    None and 0.0 must stay separate: 0.0 means "examined, scored nothing", None means "not examined
    yet". The study companion's counterpart is "a new knowledge point's mastery is hard-coded 0.0"
    (`adaptive_learning/mastery_v2.py:240-256`) —— it has no "has it been examined" dimension,
    because every knowledge point starts at 0.0 the moment it is registered. MVE has this extra
    dimension because an exam really has to run the model once, which is expensive, so it cannot
    examine every question up front.
    """
    p = profile().get(topic_id)
    return None if p is None else float(p["coverage"])


def unplaced() -> list[str]:
    """Questions in the bank that **have never been bare-examined** —— the baseline exam (`--all`) is
    exactly meant to examine them once."""
    prof = profile()
    return sorted(t for t in tasks_mod.TASKS if t not in prof)


def weakest(limit: float = WEAK_LIMIT) -> list[tuple[str, float]]:
    """Mirrors the study companion's `get_weak_topics()` (knowledge_tracker.py:2073-2084).

    Two rules, both copied rather than adapted:
      1) only accept `true level < 0.60` (the study companion uses `mastery < 0.60` or false_mastery)
      2) **sort ascending and take the lowest** —— weakest first, not "closest to the threshold"
         first. The latter looks more efficient (one step pushes it over the line), but that is
         working for the score: the weakest never gets a turn and the gradient breaks from the
         bottom. The study companion picks the former.
    """
    prof = profile()
    rows = [(t, float(p["coverage"])) for t, p in prof.items()
            if t in tasks_mod.TASKS and float(p["coverage"]) < limit]
    rows.sort(key=lambda kv: (kv[1], kv[0]))
    return rows


def transfer_weakest(limit: float = MASTERED_LIMIT) -> list[tuple[str, float]]:
    """Knowledge points that failed the transfer exam —— a **criterion-layer signal** (how far Voyager
    has got).

    transfer (an independent exam of an unpracticed question) asks "after practising a different
    question, can this **unpracticed** question be solved on its own". Coverage below the mastered
    line = the accumulated skills did not transfer to it = it is exactly the weak point that most
    needs filling right now. The difference from `weakest()`:
      · weakest reads placement / the latest bare exam —— the baseline level before and after learning
      · transfer_weakest reads kind=="transfer" —— the **post-learning transfer exam**; only its
        failures show that "it learned, but did not learn how to use it".

    Sorted ascending, lowest first (same as weakest: weakest first, never "closest to passing").
    """
    rows = [r for r in load() if str(r.get("kind") or "") == "transfer"]
    by: dict[str, float] = {}
    for r in rows:
        t = str(r.get("topic_id") or "")
        if t:
            by[t] = float(r.get("coverage") or 0.0)   # later records override earlier = most recent
    out = [(t, c) for t, c in by.items() if c < limit]
    out.sort(key=lambda kv: (kv[1], kv[0]))
    return out


def exhausted(topic_id: str) -> bool:
    """This question **stopped improving despite practice** → it should graduate and make way, rather
    than dumping all the compute onto a dead question.

    Criterion: examined ≥ 2 times and the most recent result is not higher than the previous one.
    The isomorphic counterpart is MVE's own `STALE_LIMIT` outside the study companion's "a wrong
    question is only retired once it is answered correctly" rule (planner.py:46) —— there it is
    "no progress in practice → graduate", here it is "no progress **in the bare exam** → graduate".
    Using the bare exam is stricter: practice runs with a scaffold, so whether it climbs proves
    nothing.
    """
    rows = [r for r in load() if str(r.get("topic_id") or "") == topic_id]
    if len(rows) < 2:
        return False
    return float(rows[-1].get("coverage") or 0) <= float(rows[-2].get("coverage") or 0)


def print_profile() -> None:
    split = profile_split()
    if not split:
        print("No bare-exam records yet. Run: python mve/exam.py --all (whole-bank baseline exam)")
        return
    print("=" * 82)
    print("  Bare-exam profile (true level with the knowledge graph removed · baseline vs latest)")
    print("=" * 82)
    print(f"  {'question':30} {'baseline':>8} → {'latest':>6}  {'delta':>7}  {'latest level':<14} exams")
    print("  " + "-" * 78)
    for t in sorted(split, key=lambda k: (split[k]["baseline"], k)):
        r = split[t]
        d = r["delta_pp"]
        dmark = "—" if d == 0 else (f"+{d}pp" if d > 0 else f"{d}pp")
        print(f"  {t:30} {r['baseline'] * 100:>5.0f}% → {r['latest'] * 100:>5.0f}%  "
              f"{dmark:>7}  {r['latest_level']:<14} {r['exams']}")
    missing = unplaced()
    if missing:
        print(f"  (never bare-examined: {', '.join(missing)})")
    base = [r["baseline"] for r in split.values()]
    late = [r["latest"] for r in split.values()]
    print("  " + "-" * 78)
    print(f"  baseline average {sum(base) / len(base) * 100:.0f}%  →  "
          f"latest average {sum(late) / len(late) * 100:.0f}%   "
          f"(delta {(sum(late) / len(late) - sum(base) / len(base)) * 100:+.0f} pp)")
    print(f"  mastered (≥80%) at baseline: {sum(1 for c in base if c >= MASTERED_LIMIT)} → "
          f"now: {sum(1 for c in late if c >= MASTERED_LIMIT)}")
    print("  Note: 'latest' uses each question's most recent exam. A post-practice re-exam at 100%")
    print("        overwrites the baseline, so both columns must be read together —— with only the")
    print("        latest column there is no learning curve.")


def _skills_count() -> int:
    try:
        return int(skill_store.stats().get("skills") or 0)
    except Exception:
        return 0


def placement_blocked(*, force: bool = False) -> str:
    """The baseline exam's precondition: **the skill library must be empty**. Returns an empty string
    when the baseline exam may run, otherwise the reason.

    The baseline exam measures "the true level starting from zero". Once the skill library holds
    programs, the exam round replays them —— what comes out is "the level after practising", not a
    bare exam. And `profile()` takes each question's **most recent** record, so such a fake baseline
    would **overwrite** the real one and quietly ruin the profile.

    Hit twice in practice:
      1) 2 leftover programs in the skill library on the first baseline exam → max_losing_streak_map
         baselined at 100%; only after clearing the library did it baseline at 0% (those 8 rows are
         void).
      2) Clicking the baseline on the dashboard hit it again → corrode_collapse's baseline was
         overwritten from 0% up to 100%.

    So this gate has to be **hard**: no "I'll just remember" soft warning.
    """
    if force:
        return ""
    n = _skills_count()
    if n:
        return (f"the skill library already holds {n} programs —— what you would measure now is "
                f"'the level after practising', not a bare exam; clear the library first "
                f"(python mve/reset_all.py, or the dashboard's Clear library button)")
    return ""


def final_blocked() -> str:
    """The final exam's precondition is the opposite: **the skill library must not be empty**.
    Returns an empty string when the exam may run.

    The baseline exam (`placement`) measures "the true level starting from zero", which is why it
    demands an empty skill library; the final exam (`final`) measures "what is left after learning
    to now, with the graph removed" —— it **needs** the skill library in hand, because replaying the
    programs it accumulated itself is the whole point of this exam.

    With an empty skill library the final exam equals the baseline numerically, but it is persisted
    as kind="final", which muddles "is this column the baseline or the final". So this gate has to be
    hard as well.
    """
    n = _skills_count()
    if not n:
        return ("the skill library is empty —— the final exam measures 'what is left after learning "
                "to now', and with nothing learned there is nothing to examine; run learning units "
                "first (python mve/learn.py --units 8), or run the baseline exam "
                "(python mve/exam.py --all)")
    return ""


async def exam(topic_id: str, *, verbose: bool = True,
               kind: str = "practice_exam",
               extra: dict[str, Any] | None = None,
               force: bool = False) -> dict[str, Any]:
    """Examine one question: remove the graph, leaving only the stem + the tools + its own skill library.

    `kind` distinguishes four kinds of exam, so afterwards you can tell how the curve climbed:
      placement     —— the baseline exam (the first whole-bank pass after clearing the library, when
                       the skill library is empty)
      practice_exam —— the re-exam right after practising this question (the official data point
                       on the curve)
      transfer      —— the re-exam of a **question that was never practised**. Its ups and downs
                       answer: "did the curve climb because this question was memorised, or because
                       it really learned and can transfer"
      final         —— the final exam. **With the existing skill library**, the whole bank is
                       re-examined with the graph removed, answering "having learned to now, what is
                       left once the scaffold is removed". The difference from the baseline exam is
                       not the code path but the **precondition**: the baseline needs an empty
                       library, the final exam needs a populated one.
    """
    task = tasks_mod.TASKS.get(topic_id)
    if task is None:
        return {"error": f"no such question: {topic_id}"}

    if kind == "placement":
        why = placement_blocked(force=force)
        if why:
            if verbose:
                print(f"  ✗ baseline exam aborted: {why}")
            return {"error": why, "blocked": True}
    elif kind == "final":
        why = final_blocked()
        if why:
            if verbose:
                print(f"  ✗ final exam aborted: {why}")
            return {"error": why, "blocked": True}

    prev = voyager_mod.GRAPH_STATE["on"]
    voyager_mod.GRAPH_STATE["on"] = False        # ← remove the scaffold
    try:
        ref = await vlml0_referee.answer(task)
        v = voyager_mod.LLMVoyager(blind=False, hint_level="none")
        result = await v.run(task)
        facts = result.get("facts") or []
        tool_calls = sum(1 for t in v.trajectory if not str(t.get("error") or ""))
        ev = evaluate_vs_referee(facts, task.rubric, ref.facts,
                                 tool_calls=tool_calls)
        rec = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "topic_id": topic_id,
            "coverage": round(float(ev.coverage), 4),
            "verdict": str(ev.verdict),
            "score": int(ev.score),
            "missing": list(ev.missing_points)[:3],
            "skills": int(skill_store.stats().get("skills") or 0),
            "practice_rounds": _practice_rounds(),
            "reused_skill": bool(result.get("reused_skill")),
        }
        append(rec)
        if verbose:
            mark = "✅" if rec["coverage"] >= 1.0 else (
                "◐" if rec["coverage"] > 0 else "✗")
            print(f"  {mark} exam {topic_id:26} coverage {rec['coverage'] * 100:5.0f}%  "
                  f"verdict {rec['verdict']:9} skills {rec['skills']}"
                  + (" (reused a program)" if rec["reused_skill"] else ""))
            if rec["missing"]:
                print(f"      missing: {rec['missing']}")
        return rec
    finally:
        voyager_mod.GRAPH_STATE["on"] = prev     # always restore, never pollute practice rounds


def curve() -> None:
    rows = load()
    if not rows:
        print("No exam records yet. Run: python mve/exam.py --topic <question>")
        return
    KIND_MARK = {"placement": "base", "practice_exam": "re-exam",
                 "transfer": "transfer", "final": "final"}
    print("=" * 78)
    print("  Scaffold-removal exam curve (practice has the graph, the exam removes it)")
    print("=" * 78)
    print(f"  {'#':>2}  {'kind':<9} {'practice rounds':>16}  {'skills':>7}  {'coverage':>9}  question")
    print("  " + "-" * 72)
    for i, r in enumerate(rows, 1):
        print(f"  {i:>2}  {KIND_MARK.get(str(r.get('kind') or ''), '·'):<9} "
              f"{r.get('practice_rounds', 0):>16}  "
              f"{r.get('skills', 0):>7}  {float(r.get('coverage') or 0) * 100:>6.0f}%  "
              f"{r.get('topic_id')}"
              + ("  ⟲ reused a program" if r.get("reused_skill") else ""))
    cov = [float(r.get("coverage") or 0) for r in rows]
    print("  " + "-" * 72)
    print(f"  trajectory: {[f'{c * 100:.0f}%' for c in cov]}")
    if len(cov) >= 2:
        print(f"  first {cov[0] * 100:.0f}% → last {cov[-1] * 100:.0f}%  "
              f"(delta {(cov[-1] - cov[0]) * 100:+.0f} pp)")


def progress() -> None:
    """The **learning progress** view: treat the baseline exam as the reference line and only look at
    whether the official data points after it climbed.

    Why `curve()` cannot be used directly: curve joins every exam end to end into one line, while
    the data points here are **different questions** —— the third point may be pistol (known all
    along), the fourth may be corrode (0% at the start). Connect them and the shape of the line is
    decided by the topic selection order, not by learning.
    So the correct way to draw it is **one row per question**, each row being that question's own
    baseline → re-exam → re-exam.
    """
    rows = load()
    if not rows:
        return
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_topic.setdefault(str(r.get("topic_id") or ""), []).append(r)
    split = profile_split()          # the baseline is anchored to placement, not to "the first record"
    MARK = {"placement": "base", "practice_exam": "re-exam",
            "transfer": "transfer", "final": "final"}
    print("=" * 82)
    print("  Learning progress (one row per question: baseline → post-practice re-exam → final exam)")
    print("=" * 82)
    print(f"  {'question':28} {'baseline':>8}  →  later exams")
    print("  " + "-" * 76)
    deltas: list[float] = []
    finals: list[float] = []
    bases: list[float] = []
    for t in sorted(by_topic, key=lambda k: split[k]["baseline"]):
        seq = by_topic[t]
        base = split[t]["baseline"]
        bases.append(base)
        # the baseline entry itself no longer goes into the arrow (it is the number to its left)
        rest = [r for r in seq
                if not (str(r.get("kind") or "") == "placement"
                        and float(r.get("coverage") or 0) == base)] or seq[1:]
        arrow = " → ".join(
            f"{float(r.get('coverage') or 0) * 100:.0f}%"
            f"·{MARK.get(str(r.get('kind') or ''), '·')}" for r in rest
        ) if rest else "(never re-examined)"
        mark = ""
        if rest:
            last_v = float(rest[-1].get("coverage") or 0)
            d = last_v - base
            deltas.append(d)
            mark = ("  ✅ +" if d > 0.001 else ("  ─  flat" if abs(d) <= 0.001
                                               else "  ⚠ ")) + \
                   (f"{d * 100:.0f}pp" if abs(d) > 0.001 else "")
        fs = [float(r.get("coverage") or 0) for r in seq
              if str(r.get("kind") or "") == "final"]
        if fs:
            finals.append(fs[-1])
        print(f"  {t:28} {base * 100:>5.0f}%  →  {arrow}{mark}")
    print("  " + "-" * 76)
    if deltas:
        up = sum(1 for d in deltas if d > 0.001)
        print(f"  of the {len(deltas)} re-examined questions, {up} climbed · "
              f"average delta {sum(deltas) / len(deltas) * 100:+.0f} pp")
    if finals:
        print(f"  final exam on {len(finals)} questions · average {sum(finals) / len(finals) * 100:.0f}%"
              + (f"  (baseline average {sum(bases) / len(bases) * 100:.0f}% → "
                 f"delta {(sum(finals) / len(finals) - sum(bases) / len(bases)) * 100:+.0f}pp)"
                 if bases else ""))
    # Transfer check: did the unpracticed questions climb too —— that is "it learned" rather than
    # "it memorised"
    tr = [r for r in rows if str(r.get("kind") or "") == "transfer"]
    if tr:
        seq = [float(r.get("coverage") or 0) for r in tr]
        verdict = ("climbed → transfer happened" if seq[-1] > seq[0] + 0.001 else
                   "did not climb → what rose is memory, not transfer")
        shown = " → ".join(f"{c * 100:.0f}%" for c in seq)
        print(f"  Transfer check ({len(tr)} unpracticed-question exams): {shown}  {verdict}")


def _main() -> int:
    ap = argparse.ArgumentParser(description="Scaffold-removal exam: the real data source behind the learning curve")
    ap.add_argument("--topic", default="", help="which question to examine")
    ap.add_argument("--all", action="store_true",
                    help="baseline exam over the whole bank (examines everything not yet examined)")
    ap.add_argument("--final", action="store_true",
                    help="final exam over the whole bank: re-examine everything with the existing "
                         "skill library and the graph removed (does not overwrite the baseline)")
    ap.add_argument("--profile", action="store_true", help="print the bare-exam profile (true level)")
    ap.add_argument("--curve", action="store_true", help="print the exam log")
    ap.add_argument("--progress", action="store_true", help="print learning progress (one row per question)")
    ap.add_argument("--force", action="store_true",
                    help="force a re-exam (regardless of whether it was baselined or whether the "
                         "skill library is empty)")
    ap.add_argument("--clear", action="store_true", help="clear all exam records")
    args = ap.parse_args()

    if args.clear:
        if EXAM_LOG.exists():
            EXAM_LOG.unlink()
        print("Exam records cleared")
        return 0
    if args.curve:
        curve()
        return 0
    if args.profile:
        print_profile()
        return 0
    if args.progress:
        progress()
        return 0

    if args.final:
        # The final exam **examines every question**, whether or not it has been examined before ——
        # what it wants is "the whole-bank level right now", not "whatever was never examined".
        # That is exactly where it differs from --all: --all only fills in what was never baselined
        # and requires an empty skill library, while --final re-examines everything and requires a
        # non-empty one.
        #
        # It is persisted as kind="final" and does not overwrite the baseline (profile_split()
        # anchors the baseline to placement), so the baseline column survives the final exam ——
        # the curve does not vanish into thin air.
        why = final_blocked()
        if why:
            print(f"final exam aborted: {why}")
            return 1
        ids = sorted(tasks_mod.TASKS)
        print(f"Final exam: {len(ids)} questions, all re-examined with the existing skill library "
              f"and the graph removed\n")
        for t in ids:
            rec = asyncio.run(exam(t, kind="final"))
            if rec.get("error"):
                print(f"\nfinal exam aborted: {rec['error']}")
                return 1
        print()
        progress()
        return 0

    if args.all:
        # The baseline exam only examines questions **never bare-examined**. Once everything has been
        # baselined it does **not** re-examine ——
        # `profile()` takes each question's most recent record, so re-examining would overwrite
        # "the baseline starting from zero" with "the level after practising" and quietly ruin the
        # profile (hit in practice, see placement_blocked).
        ids = unplaced()
        if not ids and not args.force:
            print("Every question in the bank already has a bare-exam record —— no re-exam.")
            print("  (a re-exam would overwrite the from-zero baseline with 'the level after practising')")
            print("  to force a re-exam: python mve/exam.py --all --force")
            print("  to see the current profile: python mve/exam.py --profile")
            return 0
        if not ids:
            ids = sorted(tasks_mod.TASKS)
        print(f"Baseline exam: {len(ids)} questions, all examined with the graph removed\n")
        for t in ids:
            rec = asyncio.run(exam(t, kind="placement", force=args.force))
            if rec.get("error"):
                print(f"\nbaseline exam aborted: {rec['error']}")
                return 1
        print()
        print_profile()
        return 0
    if not args.topic:
        ap.print_help()
        return 1
    asyncio.run(exam(args.topic))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
