#!/usr/bin/env python3
"""Adaptive question generator (mirrors the study companion's planner.select_practice_selection).

The study companion's priority chain (spelled out plainly in the docstring at planner.py:303):

    retry > due_review > weak_topic > blocked_diagnostic > recommended > default

Every selection carries `reason` + `explanation` — **the question generator must explain
why this question**; it must not be a black-box ranking. That is the dividing line between
"adaptive" and "static ordering": my previous next_topic() had ordering but no reasons,
which does not count as adaptive.

Also ported over is the study companion's readiness gate (planner.py:209 apply_readiness_policy):
questions without evidence obtained cannot advance — this corresponds to the midwife's fourth
round, "cannot generate new questions when generation is stuck; repeating (needs to obtain evidence
(memory-adaptive question generation for Voyager))".

One more rule that must be inherited:
    Questions imported by a human have validated_target=False, so they **do not feed mastery**,
    but they are written into the causal timeline. The question generator reads both the timeline
    (causal) and mastery (scoring) — not just the score.
"""

from __future__ import annotations

from typing import Any

import causal_timeline  # noqa: E402
import difficulty  # noqa: E402
import run_log  # noqa: E402
from tasks import TASKS  # noqa: E402

# Review interval (same idea as FSRS: even once mastered, keep coming back, otherwise it regresses)
REVIEW_AFTER_ATTEMPTS = 4

# Cooldown before retrying a wrong answer (mirroring the study companion's `retrying` state at
# store_qa.py:322: at least 30 minutes between two retries). An MVE run lasts only a few minutes,
# so a wall-clock cooldown is meaningless; the equivalent quantity is **how many other questions
# have been attempted in between**. Measured consequences of having no such gate: corrode_collapse
# wrong 12 times with 0% coverage, wrong_retry permanently winning, and every other question
# never getting its turn — the difficulty ladder breaks at the bottom, so mastery never rises.
RETRY_COOLDOWN = 2

# The **graduation** condition for a wrong answer: coverage repeating verbatim (and short of full
# marks) this many times in a row means further practice is just repetition — it must yield,
# otherwise we get "two dead questions alternating forever".
#
# Measured (53-round logs): series_totals sat at 50% for 23 consecutive attempts, corrode_collapse
# sat at 0% for 18, those two questions ate 79% of all rounds, while answerable questions
# (pistol 100%, map 100%) got only 8 rounds in total. The cooldown gate merely turned
# "hammering one question" into "alternating between two"; it gave wrong answers no way out —
# and the way out must be "graduate when there is no progress". Any change in coverage
# (even a tiny rise) puts the question back into the wrong-answer pool.
STALE_LIMIT = 3

# How many consecutive perfect scores count as "this question has been conquered". Once conquered
# it should not be re-tested over and over; it should instead be treated as the signal to
# "step up one level" — see the `cleared` comment in `_mastery_by_topic`
# (without it, a whole-library perfect-score loop is unbreakable).
CLEAR_RUN = 3


def _mastery_by_topic() -> dict[str, dict[str, Any]]:
    """Most recent mastery per question + whether it was ever answered wrong + cooldown progress."""
    rows = run_log.load_all()
    out: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(rows):
        topic = str(r.get("topic_id") or "")
        if not topic:
            continue
        rec = out.setdefault(topic, {
            "attempts": 0, "wrongs": 0, "last_coverage": 0.0,
            "last_mastery": None, "evidence_none": 0,
            "last_seen": -1, "cooling": 0, "cov_seq": [],
        })
        rec["attempts"] += 1
        if str(r.get("verdict")) in ("wrong", "dont_know"):
            rec["wrongs"] += 1
        if str(r.get("evidence_status")) == "none":
            rec["evidence_none"] += 1
        rec["last_evidence"] = str(r.get("evidence_status") or "")
        if r.get("coverage") is not None:
            rec["last_coverage"] = float(r["coverage"])
            rec["cov_seq"].append(round(float(r["coverage"]), 4))
        if r.get("mastery") is not None:
            rec["last_mastery"] = float(r["mastery"])
        rec["last_seen"] = i
    # Cooldown progress: how many **other** questions were attempted after the last attempt here
    for topic, rec in out.items():
        if rec["last_seen"] < 0:
            continue
        rec["cooling"] = sum(
            1 for r in rows[rec["last_seen"] + 1:]
            if str(r.get("topic_id") or "") != topic
        )
        # Stall length: counting backwards from the most recent attempt, how many consecutive
        # attempts have identical coverage
        seq = rec["cov_seq"]
        tail = seq[-1] if seq else None
        run = 0
        for v in reversed(seq):
            if v == tail:
                run += 1
            else:
                break
        rec["stale_run"] = run
        # Stalled: coverage identical for N consecutive attempts **and still not perfect** →
        # more practice would just be repetition.
        rec["stalled"] = run >= STALE_LIMIT and rec["last_coverage"] < 1.0
        # Conquered: N consecutive perfect scores → time to step up in difficulty (this level did
        # not exist before).
        #
        # Without it there is a subtle infinite loop: when everything in the library scores full
        # marks, `stalled` is always False (because `last_coverage < 1.0` never holds), so the
        # `all_stalled` branch never fires and the question generator **never generates harder
        # questions** — measured: kast correct 10 times in a row, pistol 6 times in a row,
        # stale_run far past the threshold yet none counted as stalled.
        # In other words: "stuck after answering wrong" is a signal, "answered right" is not a signal
        # at all. The study companion does the exact opposite — mastering a level immediately
        # advances to the next (`difficulty_policy` + a question bank laid out as
        # 82 knowledge points × 3 difficulty levels), where answering right is the trigger to
        # raise difficulty.
        rec["cleared"] = run >= CLEAR_RUN and rec["last_coverage"] >= 1.0

    # The three fields difficulty adaptation needs (the criteria in `difficulty.py` mirror the
    # study companion's difficulty_policy). Only the **most recent** snapshot is taken: mastery is a
    # projection of the sequence, and using historical totals treats "used to be good" as
    # "is good now" — which is exactly why the study companion uses recent_results rather than
    # cumulative figures.
    for topic, rec in out.items():
        recent = [r for r in rows if str(r.get("topic_id") or "") == topic]
        last = recent[-1] if recent else {}
        rec["confidence"] = float(last.get("mastery_confidence") or 0.0)
        rec["flags"] = list(last.get("flags") or [])
        rec["recent_verdicts"] = [str(r.get("verdict") or "")
                                  for r in recent[-2:]]
    return out


def _has_reusable_program(topic: str) -> bool:
    """Whether the skill library already holds a program for this question that **runs as-is**
    (has source code + has a main function name).

    This is the precondition for "removing the scaffold": removing the scaffold means
    "it already knows this, no help needed", and in this prototype the observable form of
    "knows it" is exactly **a proven program in the library that can be reused**. Removing the
    scaffold when there is no program is not raising difficulty, it is just letting the same
    old mistake recur — measured: on the same question `full` retrieves correctly
    (kast={109/109}, kd=1.24) while partial / none degrade to 1.0.
    """
    try:
        import skill_store
    except Exception:
        return False
    for s in skill_store.all_skills():
        if str(s.get("topic") or "") != topic:
            continue
        if str(s.get("source") or "") != "practice":
            continue
        if not str(s.get("code") or ""):
            continue
        if not str((s.get("blueprint") or {}).get("program_name") or ""):
            continue
        return True
    return False


def _hint_for(topic: str, rec: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    """How much scaffold this question should get (isomorphic to the study companion's
    difficulty_policy.select).

    The unanchored-exam logic was revised once (2026-10-08). Originally:
        m = min(m, true); cov = min(cov, true)
    A low unanchored score clamped everything back to full — producing a **deadlock**:
    unanchored 16% → always full → practice always carries the full knowledge graph →
    the model only learns to copy → unanchored stays at 16% → always full.
    **The partial level was never practised**, so naturally there was no middle state in the
    curve (measured on the dashboard: baseline exam 16% → retest after practice 16% →
    final exam 0%).

    It now works as **both ends pull, the middle is left to level stepping**:
      · unanchored exam already at target (≥0.8) → straight to none (stop helping once it knows
        it; this is the only moment the unanchored exam should "remove" the scaffold)
      · no reusable program in the library → full (don't take away the crutches when it hasn't
        learned to walk; kept as the fallback)
      · in between → leave it to the level smoothing in `difficulty.select()`:
        **performance at the same level decides remove/release, at most ±1 level per round**
        (study companion: never drop more than one level). Two rounds correct at full → partial;
        two rounds correct at partial → none. That way the partial level is inevitably practised,
        and the unanchored level can genuinely climb.
    """
    seed = TASKS[topic].difficulty if topic in TASKS else 2
    true = _true_level(topic)
    m = float((rec or {}).get("last_mastery") or 0.0)
    cov = (rec or {}).get("last_coverage")
    cov = None if cov is None else float(cov)
    # Unanchored exam already at target: the only case allowed to "jump straight to none"
    # (stop helping once it knows it)
    if true is not None and true >= 0.80:
        m = max(m, true)
        cov = true if cov is None else max(cov, true)
    # A poor unanchored score **no longer flattens the level** — that would be the deadlock above.
    # It only acts through the "no reusable program → full" fallback (see the end of this function).
    d = difficulty.select(
        seed,
        mastery=m,
        coverage=cov,
        attempts=(rec or {}).get("attempts") or 0,
        confidence=(rec or {}).get("confidence") or 0.0,
        flags=(rec or {}).get("flags") or (),
        recent_verdicts=(rec or {}).get("recent_verdicts") or (),
        reason=reason,
        prev_hint=_prev_hint(topic),
    )
    if true is not None:
        d["why"] += f"; unanchored exam {true:.0%}"
    # Precondition for removing the scaffold: a program that runs as-is exists in the library.
    # Without one, fall back to full.
    if d["hint"] != difficulty.HINT_FULL and not _has_reusable_program(topic):
        d["hint"] = difficulty.HINT_FULL
        d["why"] += "; but this question has no reusable program yet → keep the scaffold"
    return d


def _prev_hint(topic: str) -> str:
    """The scaffold level used in this question's **previous practice round** (read across processes
    from run_log).

    Level smoothing is relative to "the previous round" — and each unit is a fresh process, so
    without persisting to disk the decision restarts from zero every round and the smoothing
    constraint becomes meaningless.
    """
    try:
        rows = [r for r in run_log.load_all()
                if str(r.get("topic_id") or "") == topic]
    except Exception:
        return ""
    for r in reversed(rows):
        h = str(r.get("hint") or "").strip().lower()
        if h:
            return h
    return ""


def _with_hint(sel: dict[str, Any], mastery: dict[str, Any]) -> dict[str, Any]:
    """Fill in "target difficulty + scaffold level + why" for one selection."""
    rec = mastery.get(sel["topic_id"])
    d = _hint_for(sel["topic_id"], rec, sel["reason"])
    sel["difficulty_target"] = d["difficulty"]
    sel["hint"] = d["hint"]
    sel["hint_label"] = difficulty.HINT_LABEL[d["hint"]]
    sel["difficulty_why"] = d["why"]
    return sel


def _used_tools() -> set[str]:
    """The tools Voyager has actually called successfully so far (tallied from the trajectories in
    the run log)."""
    used: set[str] = set()
    for r in run_log.load_all():
        for t in (r.get("trajectory") or []):
            if isinstance(t, str) and t and not t.startswith("<"):
                used.add(t)
    return used


def _true_level(topic: str) -> float | None:
    """This question's **true level** (coverage of the most recent scaffold-free exam); None if never
    tested.

    Why this layer is mandatory: `last_coverage` inside `mastery` was produced **with the knowledge
    graph** in hand. The graph feeds the metric definitions, column meanings and structural skeleton
    into the prompt — measured, with the graph the first round already scores 100%, while without it
    the average is only 29%. So practice coverage measures **how high the scaffold is**, not the model's
    level. To "match the level", the question generator must read the unanchored exam
    (`exam.true_level`), not the practice log.

    Isomorphic position: the `mastery` parameter of the study companion's
    `difficulty_policy.select()` (difficulty_policy.py:94) — over there mastery is clean because the
    companion's questions get no knowledge graph; MVE has one extra scaffold layer (the graph), so it
    must swap in the unanchored value to be equivalent.
    """
    try:
        import exam
        return exam.true_level(topic)
    except Exception:                                        # pragma: no cover
        return None


def _global_target(mastery: dict[str, Any]) -> tuple[int, str]:
    """Global target difficulty: **the higher the true level, the harder the next question should be**.

    The study companion needs no such rule — its difficulty is computed directly when generating a
    question by (knowledge point, difficulty) (`difficulty_policy.select`). MVE's questions are
    hard-coded with one fixed difficulty each, so the gradient can only land on "which existing
    question to climb from".

    Rule (invented for MVE, no isomorphic counterpart in the study companion):
        target difficulty = 1 + number of questions whose true level is already "mastered", clamped to [1, 4]

    **The criterion must use the unanchored exam, not practice coverage** — this was the seam fixed
    in this round: practice runs with the graph and is therefore always 100%, so `cleared` equals the
    whole library on the very first round and `target` pins to 4 immediately, flattening the gradient
    before it had a chance to unfold. After switching to the unanchored exam: baseline average 29%,
    1 question mastered → target = 2, and the gradient starts climbing from the bottom.

    Nothing tested at all → target=1 (mirroring the study companion: a new knowledge point starts with
    mastery hard-pinned at 0.0, so you begin from the most basic).
    """
    try:
        import exam
        prof = exam.profile()
    except Exception:                                        # pragma: no cover
        prof = {}
    known = [t for t in TASKS if t in prof]
    if not known:
        return 1, "no baseline exam yet (nothing has been tested unanchored) → start from the most basic"
    covs = [float(prof[t]["coverage"]) for t in known]
    mastered = sum(1 for c in covs if c >= 0.80)
    avg = sum(covs) / len(covs)
    target = min(4, max(1, 1 + mastered))
    return target, (f"unanchored exam {len(known)}/{len(TASKS)} questions · average {avg:.0%} · "
                    f"{mastered} mastered → target difficulty {target}")


def _by_gradient(candidates: list[str], target: int) -> str:
    """Pick the candidate **closest to the target difficulty** (ties broken towards easier)."""
    return min(candidates,
               key=lambda t: (abs(TASKS[t].difficulty - target),
                              TASKS[t].difficulty, t))


def select_next(*, explicit_topic_id: str = "") -> dict[str, Any]:
    """Pick the next question and explain why.

    Returns {topic_id, reason, explanation, blocked, hint, difficulty_target}
    """
    mastery = _mastery_by_topic()
    target, why_target = _global_target(mastery)
    sel = _select_raw(explicit_topic_id=explicit_topic_id, mastery=mastery,
                      target=target)
    sel = _with_hint(sel, mastery)
    sel["difficulty_target"] = target
    sel["difficulty_why"] = why_target + " | " + str(sel.get("difficulty_why") or "")
    return sel


def _sql_dims() -> set[str]:
    """Dimensions that can write their own SQL (server-declared `by_sql=True`).

    Why the distinction is mandatory: dimensions reached through the tool path are constrained by
    the tool signature (`kast_pct` only has the team-level metric from `match_analysis_report`),
    so they can neither switch the subject granularity nor express a window function — using one as
    "a new question direction at difficulty 4" is guaranteed to fail.
    Measured exactly so: `_new_topic_suggestion` picked kast_pct as the focus and asked for
    "a question at player-level scope", and the model could only force a query against
    match_players_report (that's a tool name, not a table), failing on the same mistake all three
    tries.
    """
    try:
        import question_gen
        specs = question_gen._dim_specs()
    except Exception:                                        # pragma: no cover
        return set()
    return {str(d) for d, s in (specs or {}).items()
            if isinstance(s, dict) and s.get("by_sql")}


def _new_topic_suggestion(
    settled: list[tuple[str, dict[str, Any]]],
    *,
    target: int = 2,
    mode: str = "stalled",
) -> dict[str, Any]:
    """Mirrors the study companion's `weak_topic`: first decide **what to practise**, then hand that to
    the question generator to write the question.

    In the study companion this step is `get_weak_topics()` (knowledge_tracker.py:2066) returning the
    weak knowledge points, and the planner sets `selection_reason=weak_topic` accordingly; **the
    planner itself calls no LLM** — generating the question text belongs to the entry layer
    (`entry_tutor_question_entries.py:1289 _generate_question_payload_impl`). Isomorphic in MVE: this
    function only produces "what to write a question about + difficulty + why", and not a single
    word is handed to the model.

    Why this layer must be added: when the study companion has "no advanceable question" it goes to
    `weak_topic` to **generate** a new question; the original MVE simply hit `all_stalled`, marked
    itself blocked and got stuck — which is precisely the root cause of the "stuck at 50% for 10
    consecutive rounds with nothing advanceable" symptom.

    **Difficulty must hook into the gradient** (it used not to, the biggest seam in this loop):
      stalled → new question difficulty = max(global target, weak question difficulty) — fill the
      weak spot, do not go easier
      cleared → new question difficulty = min(4, global target + 1) — this is **stepping up one
                level**; otherwise when everything scores full marks it only ever generates
                same-level questions, so the bank grows but difficulty does not.
    Previously this always copied the stalled question's seed difficulty via
    `int(task.difficulty)`, so the gradient's `target` broke off right here — leaving "adaptive
    gradient" to act only on "which old question to pick", never on "what new question to write".
    """
    if not settled:
        return {}
    topic, m = min(settled, key=lambda kv: (kv[1]["last_coverage"],
                                            -kv[1]["stale_run"]))
    task = TASKS.get(topic)
    seed = int(getattr(task, "difficulty", 2) or 2)
    if mode == "cleared":
        # Everything conquered → step up one level (clamped to 4: that is the bank ceiling,
        # beyond it there is no metric definition left to write a question about)
        difficulty = min(4, max(1, int(target or 2) + 1))
    else:
        # Stalled → at least not below the global target, and not below the weak question's own difficulty
        difficulty = min(4, max(1, max(int(target or 2), seed)))
    dims = [str(p.dimension) for p in (task.rubric if task else [])]
    # focus must be a dimension that can **write its own SQL**: tool dimensions cannot switch the
    # subject granularity, nor express the structure difficulty 3+ demands (JOIN / window functions).
    # This used to take dims[0] directly, and so picked kast_pct (by_tool, no player-level metric)
    # to write a difficulty-4 question — a direction that is unsolvable against the data, so all
    # three generation attempts failed.
    sql_dims = _sql_dims()
    focus = next((d for d in dims if d in sql_dims), "")
    if not focus:
        # This question is all tool dimensions → pick some other SQL dimension from the bank as
        # the direction, otherwise "write a question at difficulty {difficulty}" has nothing to
        # land on.
        focus = next(iter(sorted(sql_dims)), "") or (dims[0] if dims else "")
    # Which subject keys existing questions already used — a new question must switch to another
    # key, otherwise it trips the "duplicates an existing question" gate
    used: set[str] = set()
    for t in TASKS.values():
        for p in t.rubric:
            used.update((p.subject or {}).keys())
    # `want` is a subject key consumed downstream by question_gen — keep these key names as-is.
    if "player" not in used:
        want, what = "player", "player-level"
    elif "map" not in used:
        want, what = "map", "single-map"
    else:
        want, what = "", "a different angle"
    return {
        "about": f"Write a new question that uses the {focus} metric definition with {what} scope"
                 + (f" (include the {want} key in subject)" if want else " (use a different subject)"),
        "focus_dimension": focus,
        "subject_key": want,
        "difficulty": difficulty,
        "why": ((f"{topic} scored full marks {m['stale_run']} times in a row (the whole library is"
                 f" conquered) → step up one level and write a question at difficulty {difficulty}"
                 f" (global target {target})") if mode == "cleared" else
                (f"{topic} has sat at {m['last_coverage']:.0%} for {m['stale_run']} consecutive"
                 f" attempts; the metric it tests ({focus or 'unknown'}) is the current weak spot"
                 f" → write one at difficulty {difficulty} (global target {target})")),
    }


def _select_raw(*, explicit_topic_id: str, mastery: dict[str, Any],
                target: int) -> dict[str, Any]:

    # 0) A human pinned one question (the study companion's explicit_topic mode)
    if explicit_topic_id and explicit_topic_id in TASKS:
        return {
            "topic_id": explicit_topic_id,
            "reason": "explicit_topic",
            "explanation": "pinned on the dashboard; the question generator stays out of it",
            "blocked": False,
        }

    # 1) wrong_retry: questions with "task not finished" take priority — this is **Voyager's ruler,
    #    the failed ledger** (mirroring the original failed_tasks; done = orchestration ran + data
    #    retrieved + nothing blew up, coverage points do not count).
    #    Isomorphic to the study companion's retry_wrong_question: error = task not finished, not a
    #    missed VLML point. But there is a cooldown gate: something just retried and no other
    #    question has been done in between yields first — otherwise we grind the same hard question
    #    forever (measured: corrode_collapse wrong 12 times, every other question never gets its turn).
    #    Stalled questions don't count as "retryable": if results are identical for STALE_LIMIT
    #    consecutive attempts, re-queuing it is just repetition. In the study companion the
    #    counterpart is "a wrong answer is cleared only once redone correctly"; here we add
    #    "graduate when there is no progress", otherwise the wrong-answer pool only grows and all
    #    compute is dumped on dead questions (measured: 79%).
    undone = run_log.undone_counts()
    wrongs = [
        (t, m) for t, m in mastery.items()
        if t in TASKS and undone.get(t, 0) > 0
        and m["cooling"] >= RETRY_COOLDOWN and not m["stalled"]
    ]
    if wrongs:
        # Most unfinished attempts first (Voyager's ruler; not ordered by coverage)
        topic, m = min(wrongs, key=lambda kv: -undone[kv[0]])
        return {
            "topic_id": topic,
            "reason": "wrong_retry",
            "explanation": f"this question's task was left unfinished {undone[topic]} times"
                           f" (finished = ran through + retrieved data + nothing blew up;"
                           f" coverage points don't count), so close that out before advancing"
                           f" (cooldown {m['cooling']}/{RETRY_COOLDOWN} already served)",
            "blocked": False,
        }

    # 1.5) transfer_weak: knowledge points that failed the transfer exam go first — a
    #     **criterion-layer signal**.
    #
    #     Per the user's decision, adaptive learning grounds in the Voyager criterion layer (it is
    #     learning, so "what to practise next" reads where it has got to), not in the study
    #     companion's mastery (that is an independent evaluation ruler). One of the criterion
    #     layer's hard signals is the transfer exam: a transfer (independent retest of unpractised
    #     questions) coverage below the mastery line = skill accumulation failed to transfer to this
    #     question = it did not learn it.
    #     `exam_weak` reads the baseline exam; `transfer_weak` reads the post-learning transfer exam —
    #     only the latter failing shows "it learned, but did not learn how to use it".
    #     It also uses "ascending, take the lowest" (weakest first) plus exhausted graduation, to
    #     avoid grinding.
    try:
        import exam as exam_mod
        tweak = [(t, c) for t, c in exam_mod.transfer_weakest()
                 if t in TASKS and not exam_mod.exhausted(t)]
    except Exception:                                        # pragma: no cover
        tweak = []
    if tweak:
        topic, lv = tweak[0]
        return {
            "topic_id": topic,
            "reason": "transfer_weak",
            "explanation": f"the transfer exam (independent retest of unpractised questions) is only "
                           f"{lv:.0%}, below the mastery line — skill accumulation did not transfer "
                           f"to this question, so close that out first"
                           + (f"; other transfer-weak ones: "
                              f"{', '.join(f'{t}({c:.0%})' for t, c in tweak[1:3])}"
                              if len(tweak) > 1 else ""),
            "blocked": False,
        }

    stalled = [(t, m) for t, m in mastery.items() if t in TASKS and m["stalled"]]
    # Conquered questions (consecutive perfect scores): they no longer need practice, but they
    # **are the signal to raise difficulty**.
    cleared = [(t, m) for t, m in mastery.items() if t in TASKS and m["cleared"]]

    # 2) due_review: practised enough but still not perfect → review (FSRS-style)
    due = [
        (t, m) for t, m in mastery.items()
        if t in TASKS and m["attempts"] >= REVIEW_AFTER_ATTEMPTS
        and m["wrongs"] == 0 and m["last_coverage"] < 1.0
        and not m["stalled"]
    ]
    if due:
        topic, m = min(due, key=lambda kv: kv[1]["last_coverage"])
        return {
            "topic_id": topic,
            "reason": "due_review",
            "explanation": f"practised {m['attempts']} times but coverage is stuck at "
                           f"{m['last_coverage']:.0%}, so it's time to review rather than open something new",
            "blocked": False,
        }

    # ---- 2.5) exam_weak: mirrors the study companion's weak_topic — pick the question with the
    #     **lowest true level** ----
    #
    #     This branch is where "clear the bank with a baseline exam → match the level → push
    #     level-appropriate questions" lands; newly added in this round.
    #     Why a new branch is mandatory: every branch above judges on the **practice log**, but
    #     practice runs with the knowledge graph — measured, with the graph the first round is
    #     already 100%. So wrong_retry (`wrongs>0`) never fires and due_review (`<1.0`) never fires
    #     either, and the question generator slides all the way down to recommended / default,
    #     which only sort by "has it been practised + is it close to the target difficulty" —
    #     **completely unrelated to whether the model can answer it**. That is the seam behind
    #     "the adaptive logic hooked up the gradient, but the gradient never hooked up the level".
    #
    #     Criteria copied from the study companion's `get_weak_topics()` (knowledge_tracker.py:2073-2084):
    #       · only accept true level < 0.60 (the companion's weak line)
    #       · **ascending, take the lowest** — weakest first, not "closest to the threshold"
    #       · practised but the unanchored score didn't rise (`exam.exhausted`) → graduate and
    #         yield, don't dump compute on a dead question
    try:
        import exam
        weak = [(t, c) for t, c in exam.weakest() if t in TASKS
                and not exam.exhausted(t)]
    except Exception:                                        # pragma: no cover
        weak = []
    if weak:
        topic, lv = weak[0]
        return {
            "topic_id": topic,
            "reason": "exam_weak",
            "explanation": f"the unanchored exam (knowledge graph removed) is only {lv:.0%}, the "
                           f"weakest question in the whole bank — following the companion's "
                           f"'weakest first' rule we push it now (not the one closest to passing)"
                           + (f"; the ones yielding to it: {', '.join(f'{t}({c:.0%})' for t, c in weak[1:3])}"
                              if len(weak) > 1 else ""),
            "blocked": False,
        }

    # ---- 2.6) unplaced: questions still **never tested unanchored** → level unknown, measure it first ----
    #     Isomorphic to the companion's "unpractised ones first, ordered ascending by depth → difficulty"
    #     (planner.py:174-205): without priors you must not guess, so start from the shallowest and
    #     easiest. The typical case is right after clearing the bank and before the baseline exam —
    #     then this branch is the only basis available.
    try:
        import exam
        pending = [t for t in exam.unplaced() if t in TASKS]
    except Exception:                                        # pragma: no cover
        pending = []
    if pending:
        topic = min(pending, key=lambda t: (int(getattr(TASKS[t], "difficulty", 2) or 2),
                                            t))
        return {
            "topic_id": topic,
            "reason": "unplaced",
            "explanation": f"this question has never been tested unanchored, so its true level is "
                           f"unknown ({len(pending)} questions in the bank are still untested) — "
                           f"following the companion's 'unpractised first' rule we start here; its "
                           f"difficulty {TASKS[topic].difficulty} is the lowest of this batch. "
                           f"To test the whole bank in one go: python mve/exam.py --all",
            "blocked": False,
        }

    # 3) human_focus: a human recently imported a question about some topic (present as a timeline
    #    control fact) — this is exactly where "human import → influences adaptive question
    #    generation" lands.
    #
    #    Uses latest_topics_by_kind rather than counts_by_topic: the meaning of a human import lies
    #    in **the present intent**, so it must be the most recent one, not the one imported most
    #    often historically.
    #
    #    The two cases are worded separately:
    #      a) Voyager never practised it → open a question along the human-imported direction
    #      b) practised but not perfect → still return to it (human intent outranks "advance to new")
    #
    #    But (b) must also pass the cooldown gate — otherwise wrong_retry just yielded and here we
    #    push the same 0% hard question straight back, merely moving the deadlock to another branch
    #    (measured exactly that). In the study companion "was imported by a human" is likewise never
    #    a reason to bypass the retry cooldown.
    for topic in causal_timeline.latest_topics_by_kind(causal_timeline.CONTROL):
        if topic not in TASKS:
            continue
        m = mastery.get(topic)
        if m is None:
            return {
                "topic_id": topic,
                "reason": "human_focus",
                "explanation": f"a human recently imported a question about "
                               f"\"{TASKS[topic].question[:28]}…\" (it is in the causal timeline), "
                               f"but Voyager has not formally practised it yet — open a question "
                               f"along the human-imported direction",
                "blocked": False,
            }
        if m["last_coverage"] < 1.0 and m["cooling"] >= RETRY_COOLDOWN:
            return {
                "topic_id": topic,
                "reason": "human_focus",
                "explanation": f"the human's recent import landed on this question, and it only "
                               f"reached {m['last_coverage']:.0%} — follow the human's intent and "
                               f"fill it up rather than opening something new "
                               f"(cooldown {m['cooling']}/{RETRY_COOLDOWN} already served)",
                "blocked": False,
            }
        # Still cooling down (just tried, not yet spaced out) or already perfect → look at the
        # human's next-earlier import
        continue

    # 3.5) tool_coverage: a tool that has **never once been used** → write a question that must
    #    use it. Coverage cannot stay a structural argument (a tool counts as covered once it enters
    #    TOOL_REGISTRY); it has to have actually been called. This branch is new — the study
    #    companion has no direct counterpart. It embodies that very same principle as
    #    "weak_topic fills weak spots first", only with tools in place of knowledge points.
    used = _used_tools()

    def _never_used(topic_id: str) -> bool:
        req = set(TASKS[topic_id].requires_tools or [])
        return bool(req) and not (req & used)

    # Stalled questions must **not** enter through this branch: measured, it selected series_totals
    # back — already stuck at 50% for 23 consecutive attempts — so the deadlock merely moved from
    # wrong_retry to tool_coverage (the same mistake was made earlier in the human_focus branch; see
    # the comment at :146-148 above).
    fresh = [t for t in TASKS if _never_used(t) and t not in mastery
             and not (mastery.get(t) or {}).get("stalled")]
    if not fresh:
        # Practised is fine too: as long as the tool has never been used, coverage is not yet achieved
        fresh = [t for t in TASKS if _never_used(t)
                 and not (mastery.get(t) or {}).get("stalled")]
    if fresh:
        topic = _by_gradient(fresh, target)
        gap = sorted(set(TASKS[topic].requires_tools) - used)
        return {
            "topic_id": topic,
            "reason": "tool_coverage",
            "explanation": f"tool {', '.join(gap)} has never once been used so far — write a "
                           f"question that cannot be answered without it (only this tool can supply "
                           f"this question's key scoring point)",
            "blocked": False,
        }

    # 4) recommended: advance along the difficulty gradient with the least-practised question
    #    (this is how the difficulty ladder is walked: once wrong_retry yields to the cooldown,
    #    first fill in the untouched question with the lowest difficulty)
    untouched = [t for t in TASKS if t not in mastery]
    if untouched:
        topic = _by_gradient(untouched, target)
        return {
            "topic_id": topic,
            "reason": "recommended",
            "explanation": f"this one has not been practised yet, and its difficulty "
                           f"{TASKS[topic].difficulty} is closest to the target difficulty {target} "
                           f"— climb from here",
            "blocked": False,
        }

    # 5) default: everything has been practised → pick the lowest coverage.
    #    But **only ones that haven't stalled**: letting a stalled question back in here means
    #    "the graduation rule was added for nothing" — the deadlock just moves from wrong_retry to
    #    default (which measurement confirms).
    pushable = [
        (t, m) for t, m in mastery.items()
        if t in TASKS and m["last_coverage"] < 1.0 and not m["stalled"]
    ]
    if pushable:
        topic, m = min(pushable, key=lambda kv: (kv[1]["last_coverage"],
                       abs(TASKS[kv[0]].difficulty - target)))
        return {
            "topic_id": topic,
            "reason": "default",
            "explanation": f"everything has been practised, so pick the lowest coverage that "
                           f"hasn't stalled ({m['last_coverage']:.0%})",
            "blocked": False,
        }

    # 5.5) No advanceable question left — say so honestly rather than pretending to be adaptive.
    #     The two kinds of "can't advance" have opposite natures, so wording and generation must
    #     be kept apart:
    #
    #       all_stalled  = stuck (not perfect, more practice is repetition) → fill the weak spot,
    #                      difficulty follows the weak question
    #       all_cleared  = conquered (all perfect) → **time to raise difficulty**, one level above
    #                      the target
    #
    #     The latter simply did not exist before: the `stalled` criterion requires
    #     `last_coverage < 1.0`, so when the whole bank scores full marks the question generator
    #     only falls through to the cold-start fallback and picks among a few perfect questions,
    #     never generating anything harder — "answered right" is not a signal at all. The study
    #     companion does the exact opposite: mastering a level advances immediately (a bank laid
    #     out as 82 knowledge points × 3 difficulty tiers).
    if stalled:
        topic, m = min(stalled, key=lambda kv: (kv[1]["last_coverage"], -kv[1]["stale_run"]))
        return {
            "topic_id": topic,
            "reason": "all_stalled",
            "explanation": f"no advanceable question left: this one has sat at "
                           f"{m['last_coverage']:.0%} for {m['stale_run']} consecutive attempts "
                           f"(the other non-perfect questions are stalled too) — re-queuing it is "
                           f"just repetition",
            "blocked": True,
            # The study companion goes to weak_topic here to **generate** a new question
            # (entry_tutor_answer_entries.py:174: reason != due_review → action=generate_question).
            # MVE completes this link: the planner only says "what to practise", generation is done
            # by the entry layer.
            "suggestion": _new_topic_suggestion(stalled, target=target,
                                                mode="stalled"),
            "action": "generate_question",
        }

    # 5.6) All conquered: this is the signal to **raise difficulty**, not "nothing to do".
    #     This branch did not exist before, so the bank stayed frozen at 6 questions and the
    #     difficulty never rose. The criterion is tightened: it takes **the whole bank at full
    #     marks** to count as "time to raise difficulty". When only one or two are conquered and
    #     the rest still sit at low coverage, the right move is to go back and fill the gaps, not
    #     to skip a level.
    unfinished = [t for t, m in mastery.items()
                  if t in TASKS and m["last_coverage"] < 1.0]
    if cleared and not unfinished:
        # The one conquered most times — it best represents "this metric is learned, we can move up"
        topic, m = max(cleared, key=lambda kv: kv[1]["stale_run"])
        return {
            "topic_id": topic,
            "reason": "all_cleared",
            "explanation": (
                f"this question scored full marks {m['stale_run']} times in a row "
                f"({len(cleared)} in the whole bank conquered, target difficulty {target}) — "
                f"testing it again measures nothing, so step up a level"),
            "blocked": True,
            "suggestion": _new_topic_suggestion(cleared, target=target,
                                                mode="cleared"),
            "action": "generate_question",
        }

    # 6) blocked_diagnostic: fallback (the midwife's fourth round: if evidence can't be obtained,
    #    don't generate a new question). Its position must be last — in the study companion's
    #    apply_readiness_policy (planner.py:235) it appears only when retry/due/weak are **all**
    #    empty, it is not a high-priority branch. I originally put it before recommended, which
    #    built yet another deadlock (I had just fixed two). The criterion is "the **most recent**
    #    attempt obtained no evidence", not a historical total — corrode_collapse went 'none' only
    #    once in 13 rounds, and a cumulative criterion would lock it here forever.
    no_evidence = [
        (t, m) for t, m in mastery.items()
        if t in TASKS and m.get("last_evidence") == "none"
    ]
    if no_evidence:
        topic, m = min(no_evidence, key=lambda kv: -kv[1]["evidence_none"])
        return {
            "topic_id": topic,
            "reason": "blocked_diagnostic",
            "explanation": f"nothing else is advanceable, and this question obtained no evidence "
                           f"on its most recent attempt ({m['evidence_none']} times in history) — "
                           f"repeat it until evidence arrives",
            "blocked": True,
        }

    topic = _by_gradient(list(TASKS), target)
    return {
        "topic_id": topic,
        "reason": "cold_start",
        "explanation": f"no practice record at all yet, starting from the one closest to target "
                       f"difficulty {target}",
        "blocked": False,
    }


# Display labels only — nothing keys off these strings (run_mve.py / dashboard.py / learn.py /
# export_md.py only print them).
REASON_LABEL = {
    "explicit_topic": "pinned",
    "wrong_retry": "retry wrong answer",
    "transfer_weak": "weak on transfer exam",
    "due_review": "due for review",
    "human_focus": "human-imported direction",
    "weak_topic": "human-imported direction",
    "tool_coverage": "fill a tool coverage gap",
    "blocked_diagnostic": "no evidence · repeat",
    "all_stalled": "all stalled · generate a new question",
    "all_cleared": "all conquered · step up and generate a new question",
    "exam_weak": "weakest unanchored · match the level",
    "unplaced": "not yet baselined · measure the level first",
    "recommended": "advance to a new question",
    "default": "fill the weakest gap",
    "cold_start": "cold start",
}


def next_brief() -> dict[str, Any]:
    """The recommendation card for the dashboard."""
    sel = select_next()
    task = TASKS.get(sel["topic_id"])
    return {
        **sel,
        "reason_label": REASON_LABEL.get(sel["reason"], sel["reason"]),
        "question": task.question if task else "",
        "difficulty": task.difficulty if task else 0,
        "difficulty_target": sel.get("difficulty_target"),
        "hint": sel.get("hint", ""),
        "hint_label": sel.get("hint_label", ""),
        "difficulty_why": sel.get("difficulty_why", ""),
        "cmd": f"python mve/run_mve.py --llm --topic {sel['topic_id']}",
    }
