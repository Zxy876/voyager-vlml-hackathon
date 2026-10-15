#!/usr/bin/env python3
"""Export MVE's learning process into an md artifact (mirroring the study companion's doc_exporter.py).

The study companion's md structure (doc_exporter.py:134 build_markdown):
    # Title + metadata (Exported at / Style / Range)
    ## Overview            — statistics
    ## Recent Interactions — Input/Output pairs (**placed first, the human's action trajectory**)
    ## Knowledge Map
    ## Mastery
    ## Wrong Questions

Three things copied verbatim:
  1. **Recent Interactions first** — the study companion places it as the first section after Overview,
     because "what the human did" is the starting point for reproducing an experiment. The user's requirement is the same:
     "the md must be concrete about UI interactions (which key was pressed, then which difficulty was chosen)".
  2. `escape_markdown` escaping (doc_exporter.py:17's _MARKDOWN_ESCAPE_RE) —
     without escaping, `*` `_` `#` in question text break tables and headings.
  3. Length truncation protection (_MAX_MARKDOWN_CHARS = 120_000), marked [export truncated] when exceeded.

Usage:
    python mve/export_md.py                # writes to mve/exports/
    python mve/export_md.py --stdout       # print directly
    python mve/export_md.py -o /tmp/a.md   # specify a path
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

import panel_data  # noqa: E402
import ui_events  # noqa: E402

# Mirroring the study companion's doc_exporter.py:17, but narrowed in two places:
#   a) `-` and `.` removed — the study companion escapes them verbatim, turning "0-100" "50.0" into `0\-100` `50\.0`,
#      while inline hyphens and periods do not break md structure; pure noise.
#   b) Fragments inside backticks are not escaped — `pistol_eco_pattern` written as `pistol\_eco\_pattern` is unreadable.
_MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_{}\[\]()#+!|])")
_CODE_SPAN_RE = re.compile(r"`[^`]*`")
_MAX_TEXT_CHARS = 2000
_MAX_MARKDOWN_CHARS = 120_000
# Exam-type suffixes: re-exams and transfer controls look identical in the table; unmarked, transfer is unreadable
KIND_SUFFIX = {"placement": "·placement", "practice_exam": "·re-exam",
               "transfer": "·transfer", "final": "·final"}


def _escape_segment(text: str) -> str:
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", text)


def escape_markdown(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    """Mirroring the study companion: escape characters that break md structure, and cap length. Inside backticks, keep verbatim."""
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    out: list[str] = []
    idx = 0
    for m in _CODE_SPAN_RE.finditer(text):
        out.append(_escape_segment(text[idx:m.start()]))
        out.append(m.group(0))
        idx = m.end()
    out.append(_escape_segment(text[idx:]))
    return "".join(out)


def code(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    r"""Inline code: wrapped in backticks, **not escaped**.

    Fields like question names / tool names / difficulty naturally carry underscores (`pistol_eco_pattern`,
    `match_summary_report`); wrapping them in backticks and then escaping turns them into `pistol\_eco\_pattern`,
    which renders correctly but is unreadable in source. md is not parsed inside backticks, so escaping is pure redundancy.
    """
    text = "" if value is None else str(value)
    text = text.replace("`", "'").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return f"`{text}`" if text else "`—`"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def build_markdown(state: dict[str, Any] | None = None) -> str:
    s = state if state is not None else panel_data.build_state()
    now = datetime.now().isoformat(timespec="seconds")

    env = s.get("env") or {}
    pilot = s.get("pilot") or {}
    rec = s.get("recommend") or {}
    stats = s.get("skill_stats") or {}
    events = ui_events.recent(60)

    if env.get("ok"):
        events_n = env.get("events")
        env_note = f" · {env.get('tables')} tables"
        env_note += f" / {events_n} events" if events_n else ""
    elif env.get("reason"):
        env_note = f" · {env.get('reason')}"
    else:
        env_note = ""
    lines: list[str] = [
        "# MVE learning record export",
        "",
        f"- Exported at: `{now}`",
        f"- Data source: `{env.get('state', '—')}`{env_note}",
        f"- Question generator's current recommendation: `{rec.get('topic_id', '—')}`"
        + (f" (difficulty {rec.get('difficulty')}, {rec.get('reason_label', '')})"
           if rec.get("difficulty") else ""),
        f"- Cockpit: {'running' if pilot.get('running') else 'not running'}",
        "",
    ]

    # ---------------- UI interactions (explicitly requested by the user: concrete key presses and choices) ----------------
    lines.extend(["", "## UI interactions (what was pressed, what was selected)", ""])
    if events:
        for e in events:
            # Old events may have been persisted before labels existed (the action stores the kind's raw text);
            # translate by kind again at export time, so reviews always read as plain language.
            action = ui_events.ACTION_LABEL.get(e.get("kind") or "", e.get("action"))
            lines.append(f"### #{e['seq']} · {e['ts'][11:19]} · {action}")
            d = e.get("detail") or {}
            bits = []
            for k, v in d.items():
                if v in (None, "", []):
                    continue
                bits.append(f"- {k}: {code(v, 200)}")
            if bits:
                lines.extend(bits)
            else:
                lines.append("- _(no additional parameters)_")
            if e.get("result"):
                lines.append(f"- Result: {code(e['result'], 400)}")
            lines.append("")
    else:
        lines.append("_No UI interaction records yet. Start / stop / import / scope switches on the dashboard are all recorded here._")

    # ---------------- Overview ----------------
    series = s.get("series") or []
    exam_view = s.get("exam_curve") or {}
    exam_tracks = exam_view.get("tracks") or []
    lines.extend([
        "", "## Overview", "",
        f"- Cumulative rounds: {s.get('total_rounds', 0)} (practice · run_log)",
        f"- Exam count: {sum(int(t.get('exam_count') or 0) for t in exam_tracks)}"
        f" (placement/re-exam/transfer · exam_log, **not written to run_log**)",
        f"- Skill library: {stats.get('skills', 0)} entries"
        f" (cumulative writes {stats.get('writes', 0)} · overwritten {stats.get('rewrites', 0)}"
        f" · discarded {stats.get('skipped', 0)} · bloat rate {stats.get('bloat', 1)})",
        f"- Human imports: {len(s.get('imports') or [])} entries",
        f"- Action causal facts: {len((s.get('causal') or {}).get('rows') or [])} entries",
    ])
    # The next two lines are **conditionally output**: mastery / hallucination_count are only written by the frozen old backend
    # (mve/run_mve.py). The new backend voyager_vlml/loop.py's practice line deliberately does not score,
    # so these two are always None / 0 — previously printed unconditionally, the exported overview permanently hung
    # two lines of noise: "Mastery change: Mastery None" and "Cumulative fabrications blocked: 0 entries",
    # looking like "it did not learn / it keeps fabricating", when in fact nobody produces those fields.
    if s.get("mastery_delta"):
        lines.append(f"- Mastery change: {escape_markdown(s['mastery_delta'])}")
    if int(s.get("hallucination_total") or 0) > 0:
        lines.append(f"- Cumulative fabrications blocked: {s['hallucination_total']} entries")
    if s.get("reading_text"):
        lines.append(f"- Reading: {escape_markdown(s['reading_text'])}")

    # ---------------- True level: scaffold-removed exam ----------------
    #
    # This section was added: placement / re-exam / transfer all go into exam_log.jsonl, while the exporter previously
    # only read fields derived from run_log in panel_data, so **the entire exam chain was mute in exports**.
    # Observed consequence: in the 12:10 export, the UI interactions clearly recorded "11:43:36 start placement"
    # "11:48/11:56 two learning-unit runs", yet the body contained not one line of placement data,
    # and the "reading" still said "perfect score in the first round: task too easy" — all evidence of what ran was lost.
    lines.extend(["", "## True level · scaffold-removed exam (exam_log)", ""])
    if not exam_view.get("has_data"):
        lines.append("_No bare-exam records yet. Run `python mve/exam.py --all` (whole-library placement),"
                     "then `python mve/learn.py --units 8`._")
        lines.append("")
        lines.append("The 100% on the practice line (run_log) was run **with the knowledge graph**, measuring the height of the scaffold,"
                     "not the model's level. To judge \"how much was learned\" the graph must be removed and re-examined.")
    else:
        tr = exam_view.get("transfer")
        lines.extend([
            f"- Data source: `exam_log.jsonl` — **a different thing** from run_log above: "
            f"practice gives the graph (teaching), exams remove the graph (testing).",
            f"- Placement average {exam_view.get('avg_baseline')}% → "
            f"latest average {exam_view.get('avg_latest')}%"
            f" (Δ {exam_view.get('avg_delta_pp_all') or 0:+d} percentage points)",
            f"- Re-examined {exam_view.get('retested')} questions, of which {exam_view.get('rose')} rose"
            + (f", average {exam_view['avg_delta_pp']:+d}pp"
               if exam_view.get("avg_delta_pp") is not None else ""),
            f"- Mastered at placement (≥80%) {exam_view.get('mastered')} questions → "
            f"now {exam_view.get('mastered_now')} questions",
        ])
        if tr:
            verdict = ("rose → transferable" if tr.get("rose")
                       else "did not rise → what rose is \"memorized this question\", not transferable capability")
            lines.append(f"- Transfer control ({tr.get('count')} **unpracticed** questions): "
                         f"{' → '.join(tr.get('seq') or [])} — {verdict}")
        if exam_view.get("final"):
            fin = exam_view["final"]
            lines.append(f"- Final exam ({fin.get('covered_all') and 'whole library' or 'partial'} "
                         f"{fin.get('count')} questions, **with skill library**): placement average "
                         f"{fin.get('avg_baseline')}% → final average {fin.get('avg')}%"
                         f" ({fin.get('delta_pp'):+d}pp)")
        lines.append("")
        lines.append("| Question | Placement (bare exam) | Placement level | Post-practice re-exam | Final exam | Latest | Δ | Exam count |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for t in exam_tracks:
            # The re-exam cell must be typed: post-practice re-exam and transfer control look alike; mixed together, transfer is unreadable
            after = " → ".join(
                f"{a.get('pct') or '—'}{KIND_SUFFIX.get(str(a.get('kind') or ''), '')}"
                for a in (t.get("after") or [])
            ) or "(not yet re-examined)"
            d = t.get("delta_pp")
            dcell = "—" if d is None else (f"+{d}pp" if d > 0 else (f"{d}pp" if d < 0 else "flat"))
            # "Graduated and make way" is written in the question-name cell: a separate line would break the table
            lines.append(
                f"| {code(t.get('topic_id'), 60)}"
                f"{' · graduated' if t.get('exhausted') else ''} "
                f"| {t.get('baseline_pct')} "
                f"| {escape_markdown(t.get('level'))} | {after} "
                f"| {t.get('final_pct') or '—'} "
                f"| {t.get('latest_pct')} | {dcell} | {t.get('exam_count')} |"
            )

    # ---------------- Mastery ----------------
    # This section used to be called "Mastery (by question · run_log, with scaffold)", with the main column reading coverage.
    # And coverage / mastery are only written by the frozen old backend (mve/run_mve.py) —
    # the new backend voyager_vlml/loop.py's practice line **deliberately does not score** (scoring enters the feedback loop,
    # and what it learns is pleasing the referee instead of retrieving data). As a result the whole table is now "question / rounds / — / — / 0".
    # Indexed by backend, switch to the column that is **genuinely produced**: the criterion layer's done (ran + retrieved data + no crash).
    # coverage is demoted to **shown only when present**: with old-backend logs it adds two columns; without them, no empty columns are left.
    lines.extend(["", "## Per-question statistics · practice line (criterion-layer done)", ""])
    by_topic: dict[str, dict[str, Any]] = {}
    for p in series:
        t = p.get("topic_id")
        if not t:
            continue
        r = by_topic.setdefault(
            t, {"n": 0, "done": 0, "success": 0, "best": None, "last": None, "wrongs": 0})
        r["n"] += 1
        r["done"] += 1 if p.get("done") else 0
        r["success"] += 1 if p.get("success") else 0
        cov = p.get("coverage")
        if cov is not None:
            r["best"] = max(r["best"] or 0, cov)
            r["last"] = cov
        if p.get("verdict") in ("wrong", "dont_know"):
            r["wrongs"] += 1
    has_cov = bool(by_topic) and any(r["last"] is not None for r in by_topic.values())
    if by_topic:
        if has_cov:
            lines.append("| Question | Rounds | Task done | VLML fully achieved success "
                         "| Latest coverage | Best coverage | Wrong/DontKnow |")
            lines.append("|---|---|---|---|---|---|---|")
        else:
            lines.append("| Question | Rounds | Task done | VLML fully achieved success | Wrong/DontKnow |")
            lines.append("|---|---|---|---|---|")
        for t, r in sorted(by_topic.items(), key=lambda kv: -kv[1]["n"]):
            row = (f"| {code(t, 60)} | {r['n']} "
                   f"| {r['done']}/{r['n']} | {r['success']}/{r['n']} ")
            if has_cov:
                row += f"| {_pct(r['last'])} | {_pct(r['best'])} "
            lines.append(row + f"| {r['wrongs']} |")
        if not has_cov:
            lines.append("")
            lines.append("_⚠️ The practice line deliberately does not score: coverage / score / mastery are not produced on this line_"
                         "(they are the ruler; let them into the feedback loop and what it learns is pleasing the referee, not retrieving data). "
                         "For scoring, see the \"True level\" section (scaffold-removed exam) and the consumption line.")
    else:
        lines.append("_No practice records yet._")

    # ---------------- Round details ----------------
    #
    # Also "indexed by backend": the four columns coverage / score / fact count / orchestration read
    # coverage / score / facts_count / trajectory, all four written only by the frozen old backend.
    # The new backend's practice line produces none of them → these four columns are constant "— / None / None / —".
    # Changed to **draw only when the group has data**: if there is not a single one, do not leave empty columns; do not make people guess at a row of None.
    # The main columns switch to what the new backend genuinely produces: done (task done) and success (VLML fully achieved).
    lines.extend(["", "## Round details", ""])
    if series:
        has_score = any(p.get("coverage") is not None or p.get("score") is not None
                        or p.get("facts") is not None for p in series)
        has_traj = any(p.get("trajectory") for p in series)
        head = "| # | Time | Question | Round | done | success | verdict | Evidence | Judge |"
        sep = "|---|---|---|---|---|---|---|---|---|"
        if has_score:
            head += " Coverage | Score | Fact count |"
            sep += "---|---|---|"
        if has_traj:
            head += " Orchestration |"
            sep += "---|"
        lines.append(head)
        lines.append(sep)
        for p in series:
            row = (
                f"| {p['i']} | {escape_markdown((p.get('ts') or '')[11:19])} "
                f"| {code(p.get('topic_id'), 40)} | {p.get('round')} "
                f"| {'✓' if p.get('done') else '✗'} "
                f"| {'✓' if p.get('success') else '✗'} "
                f"| {p.get('verdict')} | {p.get('evidence')} | {p.get('judge')} "
            )
            if has_score:
                row += (f"| {_pct(p.get('coverage'))} | {p.get('score')} "
                        f"| {p.get('facts')} ")
            if has_traj:
                row += f"| {code(' → '.join(p.get('trajectory') or []) or '—', 120)} "
            lines.append(row + "|")
        if not has_score:
            lines.append("")
            lines.append("_⚠️ The coverage / score / fact-count columns are retired per backend indexing: "
                         "the practice line does not score, and nobody produces them on the new backend._")
    else:
        lines.append("_No round records yet._")

    # ---------------- Skill library ----------------
    lines.extend(["", "## Skill library (lessons Voyager wrote itself)", ""])
    detail = s.get("skill_detail") or []
    if detail:
        for x in detail:
            # Effectiveness rate = ok / hits. "hits N · effective 0" = injected N times, never helped once.
            _h, _o = int(x.get("hits") or 0), int(x.get("ok") or 0)
            _rate = (
                f" · effectiveness rate {round(100 * _o / _h)}%" if _h
                else " · never used yet"
            )
            lines.append(
                f"- {escape_markdown(x.get('text'))} "
                f"(question: {code(x.get('topic'), 40)} · "
                f"hits {_h} · effective {_o}{_rate}"
                + (f" · overwritten {x.get('versions')} versions" if (x.get("versions") or 1) > 1 else "")
                + ")"
            )
    else:
        lines.append("_Skill library is empty._")

    # ---------------- Human imports ----------------
    lines.extend(["", "## Human import history", ""])
    imports = s.get("imports") or []
    if imports:
        for im in imports:
            lines.append(
                f"- {escape_markdown((im.get('ts') or '').replace('T', ' '))} "
                f"· {escape_markdown(im.get('question'), 300)}"
            )
            lines.append(
                f"  - Routed to: `{im.get('topic_id') or 'not routed to any question'}`"
                + (f" (hit {code(im.get('topic_hit'), 60)})" if im.get("topic_hit") else "")
                + f" · Orchestration: {code(' → '.join(im.get('trajectory') or []) or '—', 120)}"
                + f" · {im.get('facts_count')} facts · validated_target={im.get('validated_target')}"
            )
    else:
        lines.append("_No human imports yet._")

    # ---------------- Action causal timeline ----------------
    lines.extend(["", "## Action causal timeline", ""])
    crows = (s.get("causal") or {}).get("rows") or []
    if crows:
        lines.append("| seq | Time | Kind | Question | Summary |")
        lines.append("|---|---|---|---|---|")
        for r in crows:
            lines.append(
                f"| {r.get('seq')} | {escape_markdown((r.get('ts') or '').replace('T', ' ')[5:19])} "
                f"| {r.get('kind_label')} | {code(r.get('topic_id'), 40)} "
                f"| {escape_markdown(r.get('summary'), 200)} |"
            )
    else:
        lines.append("_Timeline is empty._")

    # ---------------- How to read this export (mirroring the study companion's attitude toward mastery: it cannot serve as evidence) ----------------
    lines.extend([
        "", "## How to read this export", "",
        "- **Do not mix the two lines**: `run_log.jsonl` is practice (**run with the knowledge graph**, observed 100% in the first round, "
        "measuring the scaffold, not the level); `exam_log.jsonl` is the scaffold-removed exam (only question text + tools + one's own skill library). "
        "\"How much was learned\" can only be read from the latter.",
        "- **The baseline cannot be displaced**: the exam profile must read the \"placement / latest\" columns together. `profile()` keeps only the last record, "
        "so a post-practice re-exam's 100% overwrites the placement's 19% — with only the latest column, the learning curve is gone.",
        "- **The criterion is coverage, not mastery**: mastery's confidence term rises with the \"sum of evidence weights\" "
        "(V2 model), which repeated answering pushes up, and using it to say \"learned\" is a false positive.",
        "- **Mastery is forgetful**: there is also a retention model `baseline × 2^(-days/half-life)`, "
        "whose half-life flexes with each answer's feedback (correct lengthens it, wrong shortens it).",
        "- **Narrative is not compared**: comparable is always false; metrics come from tools, insights from the LLM (VLML README:97).",
        "- **Human imports do not enter mastery**: validated_target=false, only entering the causal timeline.",
    ])
    if s.get("mastery_warning"):
        lines.append(f"- {escape_markdown(s['mastery_warning'])}")

    md = "\n".join(lines).strip() + "\n"
    if len(md) > _MAX_MARKDOWN_CHARS:
        md = md[:_MAX_MARKDOWN_CHARS].rstrip() + "\n\n...[export truncated]\n"
    return md


def main() -> int:
    argv = sys.argv[1:]
    # Even with the wrong python (no duckdb installed) the export still works, but "data source" is written as unavailable,
    # and the human receiving it will think the database is broken. Warn once, out loud.
    try:
        import duckdb  # noqa: F401
    except Exception:
        print("Warning: the current interpreter has no duckdb, so \"data source\" in the export will show unavailable.\n"
              "      Please run with the project environment: "
              "/Users/zxydediannao/.workbuddy/binaries/python/envs/default/bin/python3 "
              "mve/export_md.py", file=sys.stderr)
    md = build_markdown()
    # A command-line export also counts as a "human pressed a key", recorded in the interaction stream too — otherwise reviews
    # will assume this period's rounds appeared out of nowhere.
    try:
        ui_events.append(ui_events.EXPORT,
                         detail={"Method": "command-line export_md.py",
                                 "Character count": len(md)},
                         result="ok")
    except Exception:
        pass
    if "--stdout" in argv:
        print(md)
        return 0
    out = ROOT / "exports"
    out.mkdir(exist_ok=True)
    if "-o" in argv:
        path = Path(argv[argv.index("-o") + 1])
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out / f"mve_export_{stamp}.md"
    path.write_text(md, encoding="utf-8")
    print(f"Exported: {path} ({len(md):,} characters)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
