#!/usr/bin/env python3
"""Isomorphic test-question generation tool (per the meeting minutes: test sets are
isomorphically generated from already-working projects, replacing only the series id, member names, etc.).

Principles (from the minutes):
  "All test questions are isomorphically generated from already-working projects, replacing only
   data such as the series id and member names. The question logic is rigorous, with no
   unreasonable generalization requirements."

Approach (deterministic, no model):
  1. Take the source question (a working TASKS question, or one specified via --topic) — the logic skeleton = rubric points + answer_spec
  2. Look up the target entities in the current database: the new series (most rounds, and ≠ the source series),
     the team with the most rounds in that series, the series' map list
  3. Isomorphic replacement: subject entity values, question text, entity values inside answer_spec.sql quotes, tool_args
     —— only swap "data", not "logic"
  4. Run the VLML0 referee check: a question "holds up" only if every rubric point's dimension
     yields a value in the ground-truth facts (min_cover threshold); ones that yield nothing are
     dropped —— this guarantees the test set contains no fake questions that "can't find data
     after a database swap" (the lesson of the rib database seed questions: 17 rubric points, 0 yielding data)

Usage:
  python mve/gen_testset.py --topic fb_conversion_analysis          # generate one
  python mve/gen_testset.py --all                                    # generate for all questions
  python mve/gen_testset.py --topic fb_conversion_analysis --emit-code   # print code ready to paste into the bank
  python mve/gen_testset.py --topic fb_conversion_analysis --json testsets/x.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from loop_core import AnswerSpec, RubricPoint, Task  # noqa: E402

HERE = Path(__file__).resolve().parent

# SQL keyword set —— guardrail for bare-word replacement (in-quote replacement takes priority;
# bare words are only used for non-keywords)
_SQL_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "LIMIT", "AND", "OR",
    "NOT", "IN", "AS", "ON", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER",
    "CASE", "WHEN", "THEN", "ELSE", "END", "COUNT", "SUM", "AVG", "MIN", "MAX",
    "ROUND", "CAST", "VARCHAR", "UNION", "ALL", "DISTINCT", "OVER", "PARTITION",
    "ROW_NUMBER", "IS", "NULL", "ASC", "DESC",
}


def _db_path() -> Path:
    """Current database path (same source as anchor._db_path)."""
    try:
        import anchor as _anchor
        return Path(_anchor._db_path())
    except Exception:
        return HERE.parent / "vlml" / "data" / "vlml_events.duckdb"


def _connect():
    import duckdb
    return duckdb.connect(str(_db_path()), read_only=True)


def _series_candidates(con, exclude: str) -> list[str]:
    """All series in the database, sorted by round count descending (most complete data first), excluding the source series."""
    try:
        rows = con.execute(
            "SELECT CAST(series_id AS VARCHAR) AS sid, COUNT(*) AS n "
            "FROM rounds GROUP BY 1 ORDER BY n DESC").fetchall()
    except Exception:
        rows = []
    return [str(r[0]) for r in rows if str(r[0]) != exclude]


def _team_of(con, sid: str) -> str | None:
    """The team with the most rounds in this series (same convention as anchor.pick)."""
    try:
        r = con.execute(
            "SELECT t FROM (SELECT winning_team_name AS t FROM rounds "
            "WHERE CAST(series_id AS VARCHAR)=? UNION ALL "
            "SELECT losing_team_name FROM rounds "
            "WHERE CAST(series_id AS VARCHAR)=?) "
            "WHERE t IS NOT NULL AND t<>'' GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1",
            [sid, sid]).fetchone()
        return str(r[0]) if r else None
    except Exception:
        return None


def _maps_of(con, sid: str) -> list[str]:
    """Map list in this series (round count descending)."""
    try:
        rows = con.execute(
            "SELECT map_name FROM rounds WHERE CAST(series_id AS VARCHAR)=? "
            "AND map_name IS NOT NULL AND map_name<>'' "
            "GROUP BY 1 ORDER BY COUNT(*) DESC", [sid]).fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Entity extraction and mapping
# ---------------------------------------------------------------------------

def _collect_entities(task: Task) -> dict[str, list[str]]:
    """Collect entity values from rubric subjects: {entity key: [values...]}."""
    out: dict[str, list[str]] = {}
    for p in task.rubric:
        for k, v in (p.subject or {}).items():
            if v is None:
                continue
            s = str(v)
            if s and s not in out.setdefault(k, []):
                out[k].append(s)
    return out


def _build_map(task: Task, con, new_series: str) -> dict[str, dict[str, str]]:
    """Build {entity key: {old value: new value}}.

    - series: always swap to the new series (the source series is already excluded from candidates)
    - team: swap to the team with the most rounds in the new series
    - map: prefer same-name maps (isomorphic data usually has consistent map names); otherwise pair them
      by order of appearance (preserving the source question's "multi-map comparison" structure ——
      different old maps → different new maps)
    """
    entities = _collect_entities(task)
    m: dict[str, dict[str, str]] = {}

    old_series = entities.get("series") or []
    m["series"] = {s: new_series for s in old_series}

    new_team = _team_of(con, new_series)
    old_teams = entities.get("team") or []
    if new_team:
        m["team"] = {t: new_team for t in old_teams}

    old_maps = entities.get("map") or []
    if old_maps:
        new_maps = _maps_of(con, new_series)
        used: set[str] = set()
        mm: dict[str, str] = {}
        for i, om in enumerate(old_maps):
            if om in new_maps and om not in used:
                mm[om] = om
                used.add(om)
            else:
                cand = [nm for nm in new_maps if nm not in used]
                if cand:
                    mm[om] = cand[0]
                    used.add(cand[0])
                else:                       # the new series has too few maps, keep the old map name
                    mm[om] = om
        m["map"] = mm

    # Other subject keys (player, etc.): look them up if a matching entity exists; otherwise keep the original value
    for k, vals in entities.items():
        if k in ("series", "team", "map"):
            continue
        for v in vals:
            m.setdefault(k, {})[v] = v
    return m


def _replace_text(text: str, mapping: dict[str, dict[str, str]]) -> str:
    """Text replacement: old→new for all entity keys, longest first (avoid substring damage)."""
    pairs: list[tuple[str, str]] = []
    for k, kv in mapping.items():
        for old, new in kv.items():
            if old != new:
                pairs.append((str(old), str(new)))
    pairs.sort(key=lambda p: -len(p[0]))
    out = text
    for old, new in pairs:
        out = out.replace(old, new)
    return out


def _replace_sql(sql: str, mapping: dict[str, dict[str, str]]) -> str:
    """Entity replacement inside SQL.

    Replace exact values inside quotes first (`'{old}'` / `"{old}"`), then fall back to bare-word
    replacement (only when the value is not a SQL keyword —— the validation stage still blocks
    fake questions that yield no data).
    """
    pairs: list[tuple[str, str]] = []
    for k, kv in mapping.items():
        for old, new in kv.items():
            if old != new:
                pairs.append((str(old), str(new)))
    pairs.sort(key=lambda p: -len(p[0]))

    out = sql
    # 1) exact in-quote replacement
    for old, new in pairs:
        out = out.replace(f"'{old}'", f"'{new}'")
        out = out.replace(f'"{old}"', f'"{new}"')
    # 2) bare-word fallback (guardrail: skip SQL keywords; leave single-char/short words alone)
    for old, new in pairs:
        if len(old) < 3 or old.upper() in _SQL_KEYWORDS:
            continue
        out = out.replace(old, new)
    return out


def _clone_task(task: Task, new_topic_id: str, mapping: dict[str, dict[str, str]]) -> Task:
    """Clone an isomorphic question per the entity mapping: swap data, not logic."""
    rubric = []
    for p in task.rubric:
        spec = p.answer_spec
        new_spec = None
        if spec is not None:
            new_spec = AnswerSpec(
                sql=_replace_sql(str(spec.sql or ""), mapping) if spec.sql else "",
                value_column=spec.value_column,
                base_column=spec.base_column,
                numeric_tolerance=spec.numeric_tolerance,
                closed_world=spec.closed_world,
                tool=str(spec.tool or ""),
                tool_args={k: _replace_text(str(v), mapping)
                           for k, v in (spec.tool_args or {}).items()},
                value_path=str(spec.value_path or ""),
                base_path=str(spec.base_path or ""),
                percent=spec.percent,
            )
        rubric.append(RubricPoint(
            point=_replace_text(str(p.point), mapping),
            subject={k: _replace_text(str(v), mapping) for k, v in (p.subject or {}).items()},
            dimension=str(p.dimension),
            weight=p.weight,
            min_base=p.min_base,
            expected_value=p.expected_value,
            tolerance=p.tolerance,
            answer_spec=new_spec,
        ))
    return Task(
        topic_id=new_topic_id,
        question=_replace_text(task.question, mapping),
        rubric=rubric,
        difficulty=task.difficulty,
        validated_target=task.validated_target,
        min_base=task.min_base,
        requires_tools=list(task.requires_tools or []),
    )


# ---------------------------------------------------------------------------
# Referee validation (criterion for a test question holding up)
# ---------------------------------------------------------------------------

async def _verify(task: Task, min_cover: float) -> dict[str, Any]:
    """Run the VLML0 referee, checking that every rubric point yields a value.

    Coverage criterion: the fraction of rubric points whose dimension appears in the facts ≥ min_cover.
    (Dimension = convention; the fact's subject/dimension is the direct evidence for "does this question have a solution".)
    """
    import vlml0_referee
    ref = await vlml0_referee.answer(task, force=True)
    facts = ref.facts or []
    dims = {str(f.get("dimension") or "") for f in facts}
    want = [p.dimension for p in task.rubric]
    hit = [d for d in want if d in dims]
    cover = len(hit) / len(want) if want else 0.0
    return {
        "ok": cover >= min_cover and bool(facts),
        "facts": len(facts),
        "trajectory": len(ref.trajectory or []),
        "covered_dims": hit,
        "missing_dims": [d for d in want if d not in dims],
        "cover": round(cover, 3),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit_code(task: Task, src: str, series: str, mapping: dict[str, dict[str, str]]) -> str:
    """Print a ready-to-paste Task constructor (paste into the question bank or test set to use)."""
    lines = [
        f"# Isomorphic test question: source {src} → series {series}",
        f"# Entity mapping: {json.dumps(mapping, ensure_ascii=False)}",
        f"Task(",
        f"    topic_id={task.topic_id!r},",
        f"    question={task.question!r},",
        f"    difficulty={task.difficulty},",
        f"    validated_target={task.validated_target},",
        f"    min_base={task.min_base},",
        f"    requires_tools={list(task.requires_tools or [])!r},",
        f"    rubric=[",
    ]
    for p in task.rubric:
        spec = p.answer_spec
        spec_lines = ["        AnswerSpec("]
        if spec and spec.sql:
            spec_lines.append(f"            sql={spec.sql!r},")
        if spec:
            spec_lines.append(f"            value_column={spec.value_column},")
            if spec.base_column is not None:
                spec_lines.append(f"            base_column={spec.base_column},")
            if spec.numeric_tolerance:
                spec_lines.append(f"            numeric_tolerance={spec.numeric_tolerance},")
            if spec.tool:
                spec_lines.append(f"            tool={spec.tool!r},")
                spec_lines.append(f"            tool_args={spec.tool_args!r},")
            if spec.value_path:
                spec_lines.append(f"            value_path={spec.value_path!r},")
            if spec.base_path:
                spec_lines.append(f"            base_path={spec.base_path!r},")
            if spec.percent:
                spec_lines.append("            percent=True,")
        spec_lines.append("        ),")
        lines.append(
            f"        RubricPoint(\n"
            f"            point={p.point!r},\n"
            f"            subject={p.subject!r},\n"
            f"            dimension={p.dimension!r},\n"
            f"            weight={p.weight},\n"
            f"            min_base={p.min_base},\n"
            f"            answer_spec=" + "\n".join(spec_lines) + "\n        ),"
        )
    lines.append("    ],\n)")
    return "\n".join(lines)


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Isomorphic test-question generation tool")
    ap.add_argument("--topic", default="", help="source question topic_id (empty = use all current TASKS questions)")
    ap.add_argument("--all", action="store_true", help="generate test questions for all registered questions")
    ap.add_argument("--series", default="", help="specify the target series (default: auto-pick the one with most rounds)")
    ap.add_argument("--min-cover", type=float, default=0.8,
                    help="threshold for a test question holding up: fraction of rubric points yielding data (default 0.8)")
    ap.add_argument("--json", default="", help="output JSON path (default testsets/<src>__<series>.json)")
    ap.add_argument("--emit-code", action="store_true", help="print ready-to-paste Task code")
    ap.add_argument("--keep-failed", action="store_true", help="also write fake questions that yield no data into the JSON")
    args = ap.parse_args(argv)

    import tasks as tasks_mod
    import anchor as _anchor

    con = _connect()
    try:
        if args.all:
            sources = [t for t in tasks_mod.TASKS]
        elif args.topic:
            sources = [args.topic]
        else:
            print("need --topic <topic_id> or --all")
            return 2

        out_dir = HERE / "testsets"
        out_dir.mkdir(exist_ok=True)
        results = []

        for src in sources:
            if src not in tasks_mod.TASKS:
                print(f"⚠ {src} is not in the question bank, skipping")
                continue
            task = tasks_mod.TASKS[src]

            # ── pick the target series: specified takes priority, otherwise the one with the most rounds ──
            old_series = ""
            for p in task.rubric:
                if (p.subject or {}).get("series"):
                    old_series = str(p.subject["series"])
                    break
            if args.series:
                new_series = args.series
            else:
                cands = _series_candidates(con, exclude=old_series)
                if not cands:
                    print(f"⚠ no series in the database other than {old_series}; cannot make an isomorphic question for {src}")
                    continue
                new_series = cands[0]

            mapping = _build_map(task, con, new_series)
            new_id = f"{src}__{new_series}"
            new_task = _clone_task(task, new_id, mapping)

            v = await _verify(new_task, args.min_cover)
            rec = {
                "source_topic_id": src,
                "topic_id": new_id,
                "target_series": new_series,
                "entity_map": mapping,
                "question": new_task.question,
                "validated": v,
                "task": new_task.as_dict(),
            }
            results.append(rec)

            if v["ok"]:
                print(f"✅ {src} → {new_id}（series={new_series}）"
                      f" referee yielded {v['facts']} facts · dimension coverage {v['cover']:.0%} "
                      f"· {v['trajectory']} trajectory steps")
                if args.emit_code:
                    print()
                    print(_emit_code(new_task, src, new_series, mapping))
            else:
                print(f"❌ {src} → {new_id} yielded {v['facts']} facts, "
                      f"coverage {v['cover']:.0%} < {args.min_cover:.0%} —— dropped "
                      f"(missing dimensions: {v['missing_dims']})")
                if not args.keep_failed:
                    results.pop()

        good = [r for r in results if r["validated"]["ok"]]
        print()
        print(f"{len(good)}/{len(results)} test questions held up (threshold min_cover={args.min_cover})")
        if results:
            # Relative paths always resolve under out_dir (avoid write failures from background-task cwd drift)
            if args.json:
                p = Path(args.json)
                out_path = p if p.is_absolute() else out_dir / p.name
            else:
                out_path = out_dir / "testset.json"
            out_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"written to {out_path}")
        return 0
    finally:
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main(sys.argv[1:])))
    except KeyboardInterrupt:
        sys.exit(130)
