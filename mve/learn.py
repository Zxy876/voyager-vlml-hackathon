#!/usr/bin/env python3
"""Adaptive learning driver: clear the store → baseline exam → practise the weakest → remove the scaffold and re-exam → the curve rises.

Why a separate file
-------------------
`run_mve.py` is "run it once", `exam.py` is "exam it once". But the line we want is
**an alternation of the two** —— the rhythm of the alternation (practise a few rounds then
exam, which question to exam, when to switch questions) belongs neither to the question
generator nor to the exam runner; it is a third thing: learning-process scheduling.

    learning unit = practise one question (**with** the knowledge graph)
                  + re-exam the same question (**without** the knowledge graph)

The curve plots the coverage of the **re-exam** column. The practice column is always
100% (the graph feeds the conventions, column meanings and structural skeleton into the
prompt), so connecting it into a line only yields a flat line hugging the ceiling from
the very first row —— pretty, but fake.

The study companion's counterpart rhythm is "pick the weakest knowledge point → generate a
question → referee → update mastery → pick again". It needs no "remove the scaffold" step,
because its questions never get the graph in the first place; MVE has one extra layer of
graph scaffold, so it needs one extra step to measure true mastery. That is the only part
of this file that is original.

Usage
-----
    python mve/learn.py --clear              clear the store (including no-scaffold exam records)
    python mve/learn.py --place              baseline exam: exam every question never examed before
    python mve/learn.py --units 8            run 8 learning units
    python mve/learn.py --progress           learning process (one line per question)
    python mve/learn.py --go 8               one-shot: clear → baseline → 8 units → progress
    python mve/learn.py --go 8 --transfer 3  every 3 units, add one question not practised this unit

`--transfer` is the guard against self-deception: **a rising curve has two explanations** ——
memorised this question, or actually learned it. The former does not rise on questions
never practised, the latter does. Without this control group, the rise you plot proves
nothing. Better to run it, honestly report "no transfer", than to cherry-pick the
prettier dataset.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

LOG_PATH = HERE / "learn.log"

import vlml_env  # noqa: F401,E402  must bootstrap the environment first

import exam  # noqa: E402
import planner  # noqa: E402
import run_log  # noqa: E402
from tasks import TASKS  # noqa: E402


def _log(text: str) -> None:
    """The full output of a practice round goes to the log, the console keeps only a summary ——
    otherwise one unit runs to hundreds of lines."""
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(text)


def _clear() -> None:
    import reset_all
    old = sys.argv[:]
    sys.argv = ["reset_all.py"]
    try:
        reset_all.main()
    finally:
        sys.argv = old


async def _practice(topic_id: str, rounds: int = 1) -> None:
    """Practise one question (with the graph). Calls run_mve.main() directly, no second implementation."""
    import run_mve
    old = sys.argv[:]
    sys.argv = ["run_mve.py", "--llm", "--topic", topic_id,
                "--rounds", str(rounds), "--no-gen"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            await run_mve.main()
    finally:
        sys.argv = old
        # The referee answer should be printed once at the start of every unit,
        # otherwise only the first unit in the log can be checked
        if hasattr(run_mve.main, "_ref_shown"):
            delattr(run_mve.main, "_ref_shown")
        _log(f"\n{'=' * 70}\n[{topic_id}] practice round (with graph)\n{'=' * 70}\n"
             + buf.getvalue())


def _practice_count(topic_id: str) -> int:
    return sum(1 for r in run_log.load_all()
               if str(r.get("topic_id") or "") == topic_id)


def _last_practice_cov(topic_id: str) -> float | None:
    rows = [r for r in run_log.load_all()
            if str(r.get("topic_id") or "") == topic_id]
    if not rows:
        return None
    v = rows[-1].get("coverage")
    return None if v is None else float(v)


def _transfer_pick(exclude: str) -> tuple[str, int]:
    """Pick a **control question**: prefer one never practised, else the least practised
    (and never the one just practised this unit).

    Its purpose is to tell "memorised this question" apart from "actually learned it",
    so it must avoid the question practised this unit.
    """
    counts = {t: _practice_count(t) for t in TASKS}
    fresh = [t for t in TASKS if t != exclude and counts.get(t, 0) == 0]
    if fresh:
        # Among the never-practised, take the lowest no-scaffold score
        # (ones with a record win; without a record, fall back to the lowest difficulty)
        prof = exam.profile()
        fresh.sort(key=lambda t: (float(prof[t]["coverage"])
                                  if t in prof else 1.0,
                                  int(getattr(TASKS[t], "difficulty", 2) or 2), t))
        return fresh[0], 0
    rest = [t for t in TASKS if t != exclude]
    rest.sort(key=lambda t: (counts.get(t, 0), t))
    return rest[0], counts.get(rest[0], 0)


async def unit(i: int, n: int, *, rounds: int = 1,
               transfer_every: int = 0) -> None:
    sel = planner.select_next()
    topic = str(sel["topic_id"])
    reason = planner.REASON_LABEL.get(str(sel["reason"]), str(sel["reason"]))
    base = exam.true_level(topic)

    print(f"\n{'─' * 74}")
    print(f"unit {i}/{n}  topic {topic}")
    print(f"  [{reason}] {sel.get('explanation', '')}")
    print(f"  difficulty {TASKS[topic].difficulty} → target {sel.get('difficulty_target')}"
          f" · scaffold {sel.get('hint')}")

    await _practice(topic, rounds)
    prac = _last_practice_cov(topic)
    rec = await exam.exam(topic, verbose=False, kind="practice_exam")
    cov = float(rec.get("coverage") or 0.0)
    d = "" if base is None else f"  Δ {cov - base:+.0%}"
    print(f"  practice {prac if prac is None else f'{prac:.0%}'} (with graph) → "
          f"re-exam, no scaffold {cov:.0%}"
          f"{'' if base is None else f' (baseline {base:.0%})'}{d}"
          + ("  ⟲ reused a procedure from the skill library" if rec.get("reused_skill") else ""))
    if rec.get("missing"):
        print(f"     still missing: {rec['missing'][:2]}")

    if transfer_every and i % transfer_every == 0:
        t, k = _transfer_pick(topic)
        trec = await exam.exam(t, verbose=False, kind="transfer",
                               extra={"practiced_before": k})
        tcov = float(trec.get("coverage") or 0.0)
        tbase = exam.true_level(t)
        print(f"  ⇄ transfer control {t} (not practised this unit"
              + (f", never practised before" if k == 0 else f", practised {k} time(s) before")
              + f"): {tcov:.0%}"
              + (f" (baseline {tbase:.0%})" if tbase is not None else ""))


async def run_units(n: int, *, rounds: int = 1, transfer_every: int = 0) -> None:
    for i in range(1, n + 1):
        await unit(i, n, rounds=rounds, transfer_every=transfer_every)
    print()


def _place() -> int:
    # The baseline exam presupposes an empty skill library (the level starting from zero) ——
    # this gate is hard, because a fake baseline would silently overwrite the real one
    # and the profile would be ruined with no trace (exam.placement_blocked).
    why = exam.placement_blocked()
    if why:
        print(f"baseline exam aborted: {why}")
        return 1
    ids = exam.unplaced()
    if not ids:
        print("Every question in the bank already has a no-scaffold record (to re-measure, "
              "--clear first — and think it through: re-measuring overwrites the zero-baseline "
              "value with the post-practice level).")
        return 0
    print(f"baseline exam: {len(ids)} questions, all with the knowledge graph removed\n")
    for t in ids:
        rec = asyncio.run(exam.exam(t, verbose=True, kind="placement"))
        if rec.get("error"):
            print(f"\nbaseline exam aborted: {rec['error']}")
            return 1
    print()
    exam.print_profile()
    return 0


def _main() -> int:
    ap = argparse.ArgumentParser(description="Adaptive learning driver: baseline → practise the weakest → remove the scaffold and re-exam")
    ap.add_argument("--clear", action="store_true", help="clear the store (including no-scaffold exam records)")
    ap.add_argument("--place", action="store_true", help="baseline exam: exam every question never examed before")
    ap.add_argument("--units", type=int, default=0, help="run how many learning units")
    ap.add_argument("--rounds", type=int, default=1, help="practise how many rounds per unit (default 1)")
    ap.add_argument("--transfer", type=int, default=0,
                    help="every N units add one exam on a question not practised this unit (transfer control)")
    ap.add_argument("--progress", action="store_true", help="print the learning process")
    ap.add_argument("--curve", action="store_true", help="print the exam timeline")
    ap.add_argument("--go", type=int, default=0,
                    help="one-shot: clear → baseline → run N units → print progress")
    args = ap.parse_args()

    if args.go:
        print("=" * 74)
        print(f"  one-shot run: clear → baseline → {args.go} learning units")
        print("=" * 74)
        _clear()
        _place()
        asyncio.run(run_units(args.go, rounds=args.rounds,
                              transfer_every=args.transfer))
        exam.progress()
        return 0

    did = False
    if args.clear:
        _clear()
        did = True
    if args.place:
        _place()
        did = True
    if args.units:
        asyncio.run(run_units(args.units, rounds=args.rounds,
                              transfer_every=args.transfer))
        exam.progress()
        did = True
    if args.progress:
        exam.progress()
        did = True
    if args.curve:
        exam.curve()
        did = True
    if not did:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
