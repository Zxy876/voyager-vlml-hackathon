#!/usr/bin/env python3
"""Question generator + practice scope (mirrors the study companion's practice_scope).

The study companion's two mechanisms, implemented isomorphically here:

1. PracticeScope (practice_scope.py:12): mode = explicit_scope | explicit_topic
   —— on the dashboard you can either pick a scope (rotate through multiple questions) or pin one question.

2. ordered_scope_topics (practice_scope.py:248-268): how the next question is picked
   sort key = (attempted, depth, difficulty, id)
   —— unattempted first, attempted last, same tier sorted by difficulty ascending.
   Plus retry_wrong_questions from filter_question_params_to_scope:
   wrong questions come first.

This file does not import vlml_env (to avoid pulling up the whole VLML when the dashboard starts).
"""

from __future__ import annotations

import re
from typing import Any

from loop_core import AnswerSpec, RubricPoint, Task  # noqa: E402

# ⚠️ No longer hardcoded: after the database swap (GRID → vlr.gg → rib.gg), 2843069 / Cloud9
# **do not exist at all** in the new database —— on the rib database, 0 of the seed questions'
# 17 rubric points actually yielded data.
# The anchor follows the database, queried live from the database (see anchor.py). The old database still picked these two values.
try:
    import anchor as _anchor
    SERIES = _anchor.series()
    C9 = _anchor.team()
except Exception:                                        # pragma: no cover
    SERIES = "2843069"
    C9 = "Cloud9"


def _fb_sql(where: str) -> str:
    """Deterministic SQL for first-blood metrics: returns (conversion %, denominator)."""
    return (
        "SELECT ROUND(AVG(fb_team_won)*100, 1) AS conv, COUNT(*) AS n "
        f"FROM agg_first_blood_stats WHERE {where}"
    )


def _streak_sql(map_name: str, team: str = C9) -> str:
    """Deterministic SQL for the longest losing streak (gaps-and-islands).

    Two pitfalls hit in practice, the order cannot be swapped:
    1. The filter must go **after** the numbering —— number the full sequence first, then filter the rounds the target team lost,
       otherwise the whole stretch is treated as one block (measured 13 instead of 8).
    2. The starting round must be the start of "the longest block", not a direct MIN ——
       otherwise you get the group-wide minimum (measured R1 instead of R7).
    """
    return (
        "SELECT cnt AS max_streak, start_round FROM ("
        "  SELECT COUNT(*) AS cnt, MIN(round_number) AS start_round FROM ("
        "    SELECT round_number, losing_team_name,"
        "           ROW_NUMBER() OVER (ORDER BY round_number) AS rn,"
        "           ROW_NUMBER() OVER (PARTITION BY losing_team_name ORDER BY round_number) AS grp"
        f"    FROM rounds WHERE series_id='{SERIES}' AND map_name='{map_name}'"
        "  ) t"
        f"  WHERE losing_team_name='{team}'"
        "  GROUP BY (rn - grp)"
        ") x ORDER BY cnt DESC, start_round ASC LIMIT 1"
    )


# ---------------------------------------------------------------------------
# Question registry
# ---------------------------------------------------------------------------

TASK = Task(
    topic_id="fb_conversion_analysis",
    question="What went wrong with first-blood conversion for Cloud9 in the series they lost?",
    difficulty=3,
    min_base=20,
    requires_tools=["match_summary_report", "query_sql"],
    rubric=[
        RubricPoint(
            point="Overall first-blood count",
            subject={"series": SERIES, "team": C9},
            dimension="opening_duels.fb",
            weight=25,
            min_base=1,
            answer_spec=AnswerSpec(
                sql="SELECT COUNT(*) AS n, (SELECT COUNT(*) FROM agg_first_blood_stats) AS total "
                    f"FROM agg_first_blood_stats WHERE fb_team='{C9}'",
                value_column=0, base_column=1, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="Overall first-blood conversion rate",
            subject={"series": SERIES, "team": C9},
            dimension="conversion.fb_conv",
            weight=25,
            min_base=20,
            answer_spec=AnswerSpec(
                sql=_fb_sql(f"fb_team='{C9}'"),
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="First-blood conversion rate on Corrode",
            subject={"series": SERIES, "map": "Corrode", "team": C9},
            dimension="map_fb_conv",
            weight=30,
            min_base=3,
            answer_spec=AnswerSpec(
                sql=_fb_sql(f"map_name='Corrode' AND fb_team='{C9}'"),
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="First-blood conversion rate on Haven",
            subject={"series": SERIES, "map": "Haven", "team": C9},
            dimension="map_fb_conv",
            weight=20,
            min_base=3,
            answer_spec=AnswerSpec(
                sql=_fb_sql(f"map_name='Haven' AND fb_team='{C9}'"),
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
    ],
)


TASK_HARD = Task(
    # The convention must be written into the question text: in practice all 18 rounds were 0%, the model
    # computed max_losing_streak=14/13 while the referee says 8,
    # streak_start_round computed 1/6 while the referee says 7 —— it called the tools and called them
    # correctly; what was wrong is **how a losing streak is counted**. But the referee's convention hides
    # inside _streak_sql (gaps-and-islands), with not a word of it in the question text,
    # so the model can only guess. A question it can't guess right scores 0% no matter how many rounds run.
    topic_id="corrode_collapse",
    question="In this series, on which single map did Cloud9's longest continuous stretch of lost rounds happen?"
             "How many rounds were in that stretch, and at which round number did it start?"
             "(Losing-streak convention: **counted within the same map only** in round-number order;"
             "winning a single round breaks the streak; no reset at halftime,"
             "and it is not 'the total number of rounds lost across the whole match'.)"
             "Also give Cloud9's first-blood conversion rate on that map.",
    difficulty=4,
    min_base=1,
    requires_tools=["query_sql"],
    rubric=[
        RubricPoint(
            point="First-blood conversion rate on the map with the worst losses (the map named in subject)",
            subject={"series": SERIES, "map": "Corrode", "team": C9},
            dimension="map_fb_conv",
            weight=20,
            min_base=3,
            answer_spec=AnswerSpec(
                sql=_fb_sql(f"map_name='Corrode' AND fb_team='{C9}'"),
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="Longest losing-streak round count (consecutive by round number within the same map, broken by winning one round, no halftime reset)",
            subject={"series": SERIES, "map": "Corrode", "team": C9},
            dimension="max_losing_streak",
            weight=40,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=_streak_sql("Corrode"),
                value_column=0, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="Losing-streak starting round number (the first round number of the longest block, not the minimum round number of the whole match)",
            subject={"series": SERIES, "map": "Corrode", "team": C9},
            dimension="streak_start_round",
            weight=40,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=_streak_sql("Corrode"),
                value_column=1, numeric_tolerance=0,
            ),
        ),
    ],
)


# Third question: in between the two —— only tests "drill down by map", no window functions needed
TASK_MID = Task(
    topic_id="map_rounds_split",
    question="How many rounds were played on each of the three maps? What is Cloud9's win rate on each map?",
    difficulty=2,
    min_base=1,
    requires_tools=["query_sql"],
    rubric=[
        RubricPoint(
            point="Corrode round count",
            subject={"series": SERIES, "map": "Corrode"},
            dimension="map_rounds",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=f"SELECT COUNT(*) FROM rounds WHERE series_id='{SERIES}' AND map_name='Corrode'",
                value_column=0, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="Haven round count",
            subject={"series": SERIES, "map": "Haven"},
            dimension="map_rounds",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=f"SELECT COUNT(*) FROM rounds WHERE series_id='{SERIES}' AND map_name='Haven'",
                value_column=0, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="Lotus round count",
            subject={"series": SERIES, "map": "Lotus"},
            dimension="map_rounds",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=f"SELECT COUNT(*) FROM rounds WHERE series_id='{SERIES}' AND map_name='Lotus'",
                value_column=0, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="Cloud9's win rate on Corrode",
            subject={"series": SERIES, "map": "Corrode", "team": C9},
            dimension="map_win_rate",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                sql="SELECT ROUND(AVG(CASE WHEN winning_team_name='%s' THEN 1.0 ELSE 0.0 END)*100, 1), COUNT(*) "
                    f"FROM rounds WHERE series_id='{SERIES}' AND map_name='Corrode'" % C9,
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="Cloud9's win rate on Haven",
            subject={"series": SERIES, "map": "Haven", "team": C9},
            dimension="map_win_rate",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                sql="SELECT ROUND(AVG(CASE WHEN winning_team_name='%s' THEN 1.0 ELSE 0.0 END)*100, 1), COUNT(*) "
                    f"FROM rounds WHERE series_id='{SERIES}' AND map_name='Haven'" % C9,
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
    ],
)


# Fourth question: the only one that can detect "did it use pattern_detection_report"
#
# Design notes: pistol / eco metrics can also be computed with query_sql from agg_team_round_stats,
# so they **cannot distinguish** which tool Voyager actually used. What only pattern_detection_report
# provides is `scope.confidence` (confidence label, thresholds 100/50/20) —— so it is made the
# highest-weighted rubric point: getting it = the tool was really used, missing it = it went around.
TASK_PATTERN = Task(
    topic_id="pistol_eco_pattern",
    question="How did Cloud9 perform in pistol rounds and eco rounds?"
             "Also, given the current sample size, what is the confidence in this conclusion?",
    difficulty=3,
    min_base=1,
    requires_tools=["pattern_detection_report"],
    rubric=[
        RubricPoint(
            point="Pistol-round win rate (percentage value 0-100, e.g. 50.0)",
            subject={"series": SERIES, "team": C9},
            dimension="pistol_win_rate",
            weight=25,
            min_base=1,
            answer_spec=AnswerSpec(
                tool="pattern_detection_report",
                tool_args={"team_name": C9},
                value_path="key_metrics.economy.pistol.num",
                base_path="key_metrics.economy.pistol.denom",
                percent=True, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="Eco-round win rate (percentage value 0-100, e.g. 42.9)",
            subject={"series": SERIES, "team": C9},
            dimension="eco_win_rate",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                tool="pattern_detection_report",
                tool_args={"team_name": C9},
                value_path="key_metrics.economy.eco.num",
                base_path="key_metrics.economy.eco.denom",
                percent=True, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="Sample round count supporting this conclusion (integer)",
            subject={"series": SERIES, "team": C9},
            dimension="pattern_rounds",
            weight=25,
            min_base=1,
            answer_spec=AnswerSpec(
                tool="pattern_detection_report",
                tool_args={"team_name": C9},
                value_path="scope.rounds",
                numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="Confidence label (fill in exactly one of moderate / strong / weak / insufficient, taken as-is from the tool's scope.confidence)",
            subject={"series": SERIES, "team": C9},
            dimension="pattern_confidence",
            weight=30,
            min_base=1,
            answer_spec=AnswerSpec(
                tool="pattern_detection_report",
                tool_args={"team_name": C9},
                value_path="scope.confidence",
                numeric_tolerance=0,
            ),
        ),
    ],
)


# First question: difficulty 1 —— the start of the difficulty ladder
#
# Why a difficulty-1 question must be registered: in practice, over 45 rounds, corrode_collapse (difficulty 4)
# failed 12 times with coverage at 0%, and the planner's wrong_retry has infinite priority, so other
# questions never got a turn —— the difficulty ladder was broken at the very bottom, and Voyager kept
# circling questions with "no successful experience". This question needs just one single-table
# aggregation to be fully correct; its role is to let Voyager get its **first correct**,
# so the mastery series goes from None to having a value, and later progression has a starting point.
#
# Note: human import **cannot create new questions** (imports are only intentions pointing at registered
# questions; the control fact stores a topic_id), so the difficulty ladder must be filled in from the
# question-registry side.
TASK_EASY = Task(
    topic_id="series_totals",
    question="How many rounds did Cloud9 play in total in this match? How many of those rounds did they win?",
    difficulty=1,
    min_base=1,
    requires_tools=["query_sql"],
    rubric=[
        RubricPoint(
            point="Total rounds across the match (an integer)",
            subject={"series": SERIES},
            dimension="total_rounds",
            weight=50,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=f"SELECT COUNT(*) FROM rounds WHERE series_id='{SERIES}'",
                value_column=0, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="Rounds Cloud9 won (an integer)",
            subject={"series": SERIES, "team": C9},
            dimension="rounds_won",
            weight=50,
            min_base=1,
            answer_spec=AnswerSpec(
                sql="SELECT SUM(CASE WHEN winning_team_name='%s' THEN 1 ELSE 0 END) "
                    f"FROM rounds WHERE series_id='{SERIES}'" % C9,
                value_column=0, numeric_tolerance=0,
            ),
        ),
    ],
)


# --------------------------------------------------------------------------
# "A brand-new question never asked before" —— used to validate the seed layer
# --------------------------------------------------------------------------
# The only reason it exists: the knowledge graph's dimension nodes all derive from TASKS[].rubric, so only
# questions that have been asked **enter the graph**. This question deliberately **does not rebuild the graph**
# (its dimensions kast_pct / adr_value are not in knowledge_graph.json), so the old path
# render_for_prompt(topic_id) returns an empty string, and only the seed layer (matching 212
# question-independent facts) can give it a knowledge graph.
# It is the discriminating question for "is the knowledge graph really usable by every question": without it,
# no new question can get a knowledge graph, while old questions are already saturated (100%), so nothing could be measured.
TASK_NEW = Task(
    topic_id="kast_adr_check",
    question="What are Cloud9's KAST and K/D in this match?"
             "(Convention: **team overall**, taken from match_analysis_report's team section,"
             "not opponent, and not a specific player.)",
    difficulty=2,
    min_base=1,
    requires_tools=["match_analysis_report"],
    rubric=[
        RubricPoint(
            point="KAST (percentage 0-100, e.g. 100.0)",
            subject={"series": SERIES, "team": C9},
            dimension="kast_pct",
            weight=50,
            min_base=1,
            # percent convention follows the pistol question: **must point at .num**, paired with base_path
            # pointing at .denom. The first version I wrote as `...kast` (missing .num), and the referee got
            # the whole {num,denom} dictionary, so got=100.0 vs ref={...} were never comparable ——
            # both A/B groups scored 0%, but the question was wrong, not the model (the model's 100.0 was correct).
            answer_spec=AnswerSpec(
                tool="match_analysis_report",
                tool_args={"series_id": SERIES},
                value_path="key_metrics.team.consistency.kast.num",
                base_path="key_metrics.team.consistency.kast.denom",
                percent=True, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="K/D (ratio, e.g. 1.24)",
            subject={"series": SERIES, "team": C9},
            dimension="kd_ratio",
            weight=50,
            min_base=1,
            # Scalar metric (not a num/denom pair): point directly at it, no percent.
            answer_spec=AnswerSpec(
                tool="match_analysis_report",
                tool_args={"series_id": SERIES},
                value_path="key_metrics.team.consistency.kd",
                percent=False, numeric_tolerance=0.05,
            ),
        ),
    ],
)


TASKS: dict[str, Task] = {
    TASK_EASY.topic_id: TASK_EASY,
    TASK_MID.topic_id: TASK_MID,
    TASK.topic_id: TASK,
    TASK_HARD.topic_id: TASK_HARD,
    TASK_PATTERN.topic_id: TASK_PATTERN,
    TASK_NEW.topic_id: TASK_NEW,
}

# **Seed questions** = these hardcoded ones in tasks.py. The study companion's isomorphic counterpart is
# `static/knowledge_seeds/*.json`: knowledge points are **declared by the author**, existing before questions do
# and not growing with practice/question generation. Only a seed's rubric has the right to derive dimension nodes in the knowledge graph.
SEED_TOPICS: set[str] = set(TASKS.keys())


def _merge_generated() -> int:
    """Merge questions validated by `question_gen` into TASKS.

    Why it must be merged here: the knowledge graph's dimension nodes all derive from `TASKS[].rubric`
    (tasks.py:360); without merging, auto-generated questions enter neither mastery nor the knowledge
    graph, equivalent to generating nothing.

    Default `validated_target=False` —— per the study companion: only validated questions count,
    **generated ≠ active**; they must first be runnable by the referee and have their convention confirmed.
    """
    try:
        import question_gen
    except Exception:                                        # pragma: no cover
        return 0
    added = 0
    for rec in question_gen.load_generated():
        topic = str(rec.get("topic_id") or "").strip()
        if not topic or topic in TASKS:
            continue
        # Questions rejected on re-check do not enter the question bank (see `question_gen.revalidate_store`).
        # Why this gate is needed: the validation rules were **added later**, while the questions were
        # already in the bank —— in practice `max_losing_streak_map`'s value_column pointed at map_name
        # (the true value is the map name 'Lotus'), scoring 0% for 5 straight rounds with the critique
        # unchanged. That is not "can't learn", it is a broken question; broken questions mixed into the
        # bank will flatten the learning curve.
        if rec.get("invalid"):
            continue
        try:
            task = question_gen.to_task(rec)
        except Exception:                                    # pragma: no cover
            continue
        if not task.rubric:
            continue
        TASKS[topic] = task
        added += 1
    return added


GENERATED_COUNT = _merge_generated()

# **Runtime questions** = questions produced by the question generator / human import and adopted after
# passing validation. Their key difference from seed questions: **their rubric has no right to
# derive dimension nodes in the knowledge graph**.
# Per the study companion `knowledge_tracker.py:1555`: knowledge points created at runtime
# (the one created only when `_ensure_topic` cannot resolve an existing knowledge point) do not enter
# answer records, do not participate in mastery, and only enter the pending-review candidate queue.
# MVE tightens this rule further —— not even empty shell nodes are created;
# all runtime new dimensions land on **edges** (`knowledge_graph.link_import`).
# Without this, every adopted new question adds a node to the dimension layer, equivalent to
# "questions derive the knowledge graph".
RUNTIME_TOPICS: set[str] = set(TASKS.keys()) - SEED_TOPICS


# ---------------------------------------------------------------------------
# Practice scope and next question (mirrors practice_scope.ordered_scope_topics)
# ---------------------------------------------------------------------------

def next_topic(
    *,
    attempted: dict[str, int],
    failed: dict[str, int],
    current: str | None = None,
    mode: str = "explicit_scope",
    only: list[str] | None = None,
) -> str:
    """Pick the next question.

    Sorting mirrors the study companion's ordered_scope_topics (practice_scope.py:260-267):
        (attempted?, wrong?, difficulty, id)
    Plus the study companion's filter_question_params_to_scope rule: wrong questions (retry_wrong_questions) come first.

    only —— practice scope (practice_scope.py). A human clicks a dimension on the knowledge graph to set it as the scope,
    and the question generator only produces questions within that scope; given `only`, it does **not** degrade to the full bank
    (it falls back to the full bank only when the scope truly has no questions left, which means the scope itself
    is invalidated, and practice_scope has already marked it invalidated).
    """
    if mode == "explicit_topic" and current in TASKS:
        return current

    def key(topic_id: str) -> tuple:
        task = TASKS[topic_id]
        done = topic_id in attempted
        wrong = topic_id in failed
        return (
            not wrong,       # wrong questions first (retry_wrong_questions)
            done,            # unattempted first (ordered_scope_topics' attempted key)
            task.difficulty,  # difficulty ascending
            topic_id,
        )

    pool = [t for t in (only or []) if t in TASKS] if only else None
    if pool:
        return min(pool, key=key)
    return min(TASKS, key=key)


def task_brief(task: Task) -> dict[str, Any]:
    """Fields for the question card on the dashboard (answer_spec not exposed —— that is server-side private)."""
    return {
        "topic_id": task.topic_id,
        "question": task.question,
        "difficulty": task.difficulty,
        "key_points": [p.point for p in task.rubric],
        "weights": {p.point: p.weight for p in task.rubric},
        "dims": sorted({p.dimension for p in task.rubric}),
        "requires_tools": list(task.requires_tools or []),
        "has_answer_spec": sum(1 for p in task.rubric if p.answer_spec is not None),
    }


def all_briefs() -> list[dict[str, Any]]:
    return [task_brief(t) for t in TASKS.values()]


# ---------------------------------------------------------------------------
# Which question a human-imported question routes to
# ---------------------------------------------------------------------------
#
# Why this step is necessary: the question generator's weak_topic / human_focus branches read the
# causal timeline by topic_id (`causal_timeline.counts_by_topic()` skips rows with an empty topic_id).
# A human types a sentence on the dashboard at random, and we cannot ask them to memorize topic_ids
# first —— so natural language must be routed to a question. When routing fails, honestly leave it empty
# and state on the dashboard "this one does not drive question generation",
# rather than quietly stuffing in a default value.
#
# Scoring uses "the length of the matched word" rather than "the number of matches": long words
# (first-blood conversion, confidence) indicate ownership better than short words (map, win rate),
# avoiding a question like "win rates on the three maps" being routed to the pistol question.
TOPIC_HINTS: dict[str, tuple[str, ...]] = {
    "series_totals": (
        "total_rounds",
        "total rounds", "how many rounds", "rounds in total", "rounds played",
        "rounds won", "rounds did", "in total", "across the match", "whole match",
        # "win how many" was deleted: in practice the question "what fraction can they win after winning first blood" was dirtily matched into
        # series_totals by this 4-character phrase (its length score beat fb's 2-character "first blood") —— win-rate
        # questions all contain that phrase, and it belongs to no question. The original question text routes via "how many rounds they won".
    ),
    "pistol_eco_pattern": (
        "pistol", "eco", "force buy", "confidence", "sample size", "enough sample",
        "pistol round", "eco round", "pistol rounds", "eco rounds", "forced buy",
        "half buy", "confident", "sample size enough", "enough data",
    ),
    "corrode_collapse": (
        "streak", "collapse", "losing streak", "longest", "starting round",
        "which round", "consecutive", "in a row", "collapse", "consecutive losses",
        "worst", "lost the most", "longest run", "which round did it start",
    ),
    "fb_conversion_analysis": (
        "fb_conv", "first blood", "after first blood", "wins the first blood",
        "first-blood conversion", "opening duel", "first kill", "fb conversion",
        "conversion rate", "conversion", "after winning first blood",
        "post first blood",
        # The last few (previously the Chinese ones) were added for length: when asking "what fraction can they win after winning first blood", the 2-character word
        # loses to other questions' 4-character generic phrases (see the series_totals comment).
    ),
    "map_rounds_split": (
        "map_rounds", "map_win_rate",
        "three maps", "each map", "rounds per map", "win rate", "on the map",
        "per map", "rounds on each map", "round count",
    ),
    # lotus_win_rate was added later and missed from the handwritten table —— in practice the question
    # "What is Cloud9's win rate on Lotus?" could not route (AUTO_HINTS derives map/win/rate, which are
    # **English** stems, so non-English questions could not match), so this human import's topic_id was empty, the question generator could not see it,
    # and it would also use this text to generate a new question duplicating an existing one. Added here.
    "lotus_win_rate": (
        "lotus", "lotus_win_rate",
        "lotus map", "win rate on lotus", "on lotus",
    ),
}


def _auto_hints() -> dict[str, tuple[str, ...]]:
    """**Automatically derive** topic-routing keywords from TASKS —— the fallback for the handwritten table `TOPIC_HINTS`.

    Why automation is necessary: `kast_adr_check` was added later and is not in the handwritten table ——
    in practice "What is Cloud9's KAST in this match?" could not route at all, so this control fact's
    topic_id was empty, the question generator's `latest_topics_by_kind()` skipped it,
    and the human-import intent was broken. **Supplementing the handwritten table once per new question will eventually miss again.**

    Three derivation sources (all existing server-side hard facts, no manual maintenance):
      1. English stems of `rubric.dimension`: kast_pct → kast / pct
      2. Uppercase abbreviations in `rubric.point`: KAST, ADR
      3. Ratio notation in `rubric.point`: K/D
    """
    out: dict[str, set[str]] = {}
    for tid, task in TASKS.items():
        words = out.setdefault(tid, set())
        for p in task.rubric:
            for w in re.split(r"[^A-Za-z0-9]+", str(p.dimension) or ""):
                if len(w) >= 3:
                    words.add(w.lower())
            for abbr in re.findall(r"[A-Z]{2,}", str(p.point) or ""):
                words.add(abbr.lower())
            for ratio in re.findall(r"[A-Za-z]/[A-Za-z]", str(p.point) or ""):
                words.add(ratio.lower())
    return {k: tuple(sorted(v)) for k, v in out.items()}


AUTO_HINTS: dict[str, tuple[str, ...]] = _auto_hints()


def infer_topic(question: str) -> tuple[str, str]:
    """Route a natural-language sentence to the most likely question. Returns (topic_id, matched word).

    Returns ("", "") when routing fails —— callers must handle this branch, not default to the first question.

    Two levels: first check the manually refined `TOPIC_HINTS`, then the automatically derived
    `AUTO_HINTS` (catches questions added later without a handwritten-table entry).
    """
    q = (question or "").lower()
    if not q.strip():
        return "", ""
    best_topic, best_hit, best_score = "", "", 0
    # Handwritten table takes priority (manually refined is more accurate); auto-derived words are short
    # and generic (map / win / rate), only a fallback, and require ≥3 characters —— the length gate **only
    # governs auto-derived words**; handwritten short keywords are valid and must not be filtered out by it.
    for source, weight, minlen in ((TOPIC_HINTS, 0, 0), (AUTO_HINTS, -2, 3)):
        for topic, hints in source.items():
            if topic not in TASKS:
                continue
            for h in hints:
                hl = h.lower()
                if len(h) < minlen or hl not in q:
                    continue
                # English words get a bit of extra weight: they are exact dimension names, more reliable than loose keywords
                score = len(h) + (3 if h.isascii() else 0) + weight
                if score > best_score:
                    best_topic, best_hit, best_score = topic, h, score
        if best_topic:
            break
    return best_topic, best_hit
