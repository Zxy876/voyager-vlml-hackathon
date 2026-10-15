#!/usr/bin/env python3
"""Voyager agent: retrieves data from VLML and hands back a fact set.

Two implementations:
- ScriptedVoyager: no LLM needed. Driven by the skill library — after each round
  it records the tool combination that was used, and the next round reuses the
  combinations that worked. Used to get the pipeline running first.
- LLMVoyager: needs an LLM key. Lets the model decide which tools to call
  (genuinely "learning to orchestrate").

Both produce the same structure: facts[] + narrative{comparable:false}
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
from typing import Any

from vlml_env import (  # noqa: E402  (bootstrap the environment first)
    execute_custom_sql,
    get_database_info,
    match_analysis_report,
    match_economy_report,
    match_players_report,
    match_rounds_report,
    match_summary_report,
    pattern_detection_report,
    player_profile_report,
    scouting_report,
)

import causal_timeline  # noqa: E402
import skill_store  # noqa: E402
import run_log  # noqa: E402

from loop_core import from_key_metrics, from_sql_result, make_fact  # noqa: E402
from llm_client import chat, chat_json  # noqa: E402
import feedback as feedback_mod  # noqa: E402

# Scaffold tier constants (defined in difficulty.py). The knowledge graph block must
# scale with them — the study companion puts the graph in the **optional** section of
# the question-generation prompt and drops the whole section when the token budget is
# exceeded (`llm_prompts.py:285-303`); the grading contract does not mention the graph
# at all (`prompt_templates.py:170-189`). MVE previously missed this layer: the graph
# block was only governed by the env global switch, unrelated to mastery/coverage
# → questions the model can already solve still get the answer template.
from difficulty import HINT_FULL, HINT_NONE, HINT_PARTIAL  # noqa: E402

# ⚠️ No longer hardcoded: after switching databases (GRID → vlr.gg → rib.gg),
# 2843069 / Cloud9 **do not exist at all** in the new database — measured on the rib
# database, **0 of 17** scoring points of the seed questions produced any numbers.
# The anchors follow the database and are looked up live (see anchor.py). The old
# database still yields these same two values.
try:
    import anchor as _anchor
    SERIES = _anchor.series()
    C9 = _anchor.team()
except Exception:                                        # pragma: no cover
    SERIES = "2843069"
    C9 = "Cloud9"
NRG = "NRG"


class Skill:
    """One skill: for a given question type, which tool combination yields which dimensions."""

    def __init__(self, name: str, steps: list[str], yields: list[str]):
        self.name = name
        self.steps = steps          # tool call sequence (data-retrieval combo, not scored)
        self.yields = yields        # dimension prefixes this skill can obtain
        self.used = 0
        self.hit = 0

    @property
    def success_rate(self) -> float:
        return self.hit / self.used if self.used else 0.0

    def __repr__(self) -> str:
        return f"<Skill {self.name} used={self.used} hit={self.hit} rate={self.success_rate:.2f}>"


class ScriptedVoyager:
    """Skill-library-driven Voyager. Behaviour changes as skills accumulate — this is
    the observable part of "learning"."""

    def __init__(self) -> None:
        self.skills: list[Skill] = []
        self.trajectory: list[dict[str, Any]] = []

    # ---- skill library ----
    def _ensure_skill(self, name: str, steps: list[str], yields: list[str]) -> Skill:
        for s in self.skills:
            if s.name == name:
                return s
        s = Skill(name, steps, yields)
        self.skills.append(s)
        return s

    def _pick_skill(self, need_dims: list[str]) -> Skill | None:
        """Pick a skill by "can it cover the needed dimensions"; if none can, return
        None (which triggers exploration)."""
        best = None
        best_score = 0.0
        for s in self.skills:
            cover = sum(1 for d in need_dims if any(d.startswith(y) for y in s.yields))
            if cover == 0:
                continue
            score = cover + 0.3 * s.success_rate
            if score > best_score:
                best, best_score = s, score
        return best

    # ---- execution ----
    async def run(self, task: Any) -> dict[str, Any]:
        """Run one round: pick a skill → call tools → produce a fact set."""
        need_dims = [p.dimension for p in task.rubric]
        skill = self._pick_skill(need_dims)

        # No skill covers it → explore: call only one coarse-grained report first
        # (novice behaviour)
        if skill is None:
            skill = self._ensure_skill(
                name="整体概览",  # skill name kept in Chinese: run_mve.py matches on it
                steps=["match_summary_report(series, team)"],
                yields=["opening_duels", "conversion", "impact", "consistency"],
            )

        facts: list[dict[str, Any]] = []
        self.trajectory = []

        if any(p.dimension.startswith("map_fb_conv") for p in task.rubric) and \
           not any(s.name == "分图下钻" for s in self.skills):
            # Per-map dimensions are needed but drilling is not learned yet →
            # only the overall overview, so coverage is incomplete
            pass

        # Step 1: overall overview (every skill starts here)
        rep = await match_summary_report(series_id=SERIES, team_name=C9)
        self.trajectory.append({"tool": "match_summary_report",
                                "args": {"series_id": SERIES, "team_name": C9}})
        km = (rep.get("key_metrics") or {}).get("team", {})
        facts += from_key_metrics(
            km, subject={"series": SERIES, "team": C9},
            tool="match_summary_report",
            scope={"rounds": 59},
        )
        skill.used += 1

        # Step 2: if the skill library has "分图下钻" and this round needs it,
        # drill down after finishing the overview
        drill = next((s for s in self.skills if s.name == "分图下钻"), None)
        if drill is not None:
            rows = await execute_custom_sql(
                "SELECT map_name AS map, fb_team,"
                " COUNT(*) AS fb,"
                " ROUND(AVG(fb_team_won)*100, 1) AS conv_pct"
                " FROM agg_first_blood_stats GROUP BY 1, 2"
            )
            self.trajectory.append({"tool": "execute_custom_sql", "args": {"drill": "map"}})
            facts += [
                make_fact(
                    subject={"series": SERIES, "map": m, "team": t},
                    dimension="map_fb_conv",
                    value=c, unit="percent", base=int(n),
                    scope={"rounds": 59},
                    source={"tool": "execute_custom_sql", "section": "agg_first_blood_stats"},
                )
                for m, t, n, c in (rows.get("rows") or [])
            ]
            drill.used += 1

        return {
            "facts": facts,
            "narrative": {"text": self._narrate(facts), "comparable": False},
            "skill_used": skill.name,
        }

    # ------------------------------------------------------------------
    # "Network genius" mode: do not read the rubric, decompose the original
    # question into topics, gather indirect evidence from the tools, and
    # approach the answer step by step
    # ------------------------------------------------------------------
    # Per the user's decision: give the original question but **not** the rubric.
    # At first Voyager does not even understand the question; it decomposes it
    # through its own understanding, asks VLML0/MCP indirect questions, reasons, and
    # accumulates techniques, getting better over time. It produces
    # questions_asked (the approach process) + reasoning (step-by-step inference)
    # + conclusion (synthesised conclusion) — the whole thinking infra is
    # transparent, for a human to read.
    #
    # NOTE: the Chinese alternatives below are matched against `task.question`,
    # which comes from testsets/testset_all.json and is written in Chinese.
    # These literals must stay Chinese to keep matching the question text —
    # do not translate.
    REVIEW_TOPIC_RULES: list[tuple[re.Pattern, str]] = [
        (re.compile(r"首血|转换|fb", re.I), "fb"),
        (re.compile(r"手枪|pistol", re.I), "pistol"),
        (re.compile(r"eco|经济", re.I), "eco"),
        (re.compile(r"回合", re.I), "rounds"),
        (re.compile(r"胜率", re.I), "win_rate"),
        (re.compile(r"连败|丢分|最长|连续", re.I), "streak"),
        (re.compile(r"kast|kd|击杀", re.I), "kast"),
        (re.compile(r"地图|哪张图|每张图|图名", re.I), "by_map"),
        (re.compile(r"置信|confidence|数据量", re.I), "pattern"),
    ]
    KNOWN_MAPS = ["Corrode", "Haven", "Lotus", "Sunset", "Summit",
                  "Ascent", "Bind", "Split", "Abyss", "Pearl", "Icebox"]

    def _review_topics(self, question: str) -> list[str]:
        """Decompose topics from the original question (without reading the rubric) — the
        network genius's first step: guess what the question is asking about."""
        hits = [tag for rx, tag in self.REVIEW_TOPIC_RULES if rx.search(question)]
        return hits or ["overview"]

    def _map_from_question(self, question: str) -> str | None:
        for m in self.KNOWN_MAPS:
            if m.lower() in question.lower():
                return m
        return None

    @staticmethod
    def _row(rows: dict[str, Any]) -> dict[str, Any]:
        """Rows from execute_custom_sql are tuples, zipped with columns into a dict."""
        row0 = (rows.get("rows") or [])
        if not row0:
            return {}
        return dict(zip(rows.get("columns") or [], row0[0]))

    async def run_review(self, task: Any) -> dict[str, Any]:
        """One "network genius" round: without knowing the rubric, decompose topics from
        the original question → indirect evidence gathering → synthesis.

        Difference from run(): run() retrieves data against the rubric's dimensions
        (the rote-answer path), while run_review() only takes the question and
        records every "why did I ask this, what came back, what did I infer from it"
        into questions_asked / reasoning — the approach process is fully transparent.
        """
        topics = self._review_topics(task.question)
        facts: list[dict[str, Any]] = []
        questions: list[dict[str, Any]] = []   # approach process: each indirect question
        reasoning: list[str] = []              # step-by-step inference
        self.trajectory = []

        # ① Overall overview: first ask "how is this team playing overall"
        # (the network genius builds a global impression first)
        rep = await match_summary_report(series_id=SERIES, team_name=C9)
        self.trajectory.append({"tool": "match_summary_report",
                                "args": {"series_id": SERIES, "team_name": C9}})
        km = (rep.get("key_metrics") or {}).get("team", {})
        km_facts = from_key_metrics(
            km, subject={"series": SERIES, "team": C9},
            tool="match_summary_report", scope={"rounds": 59},
        )
        questions.append({
            "intent": "Look at the overall picture first: this team's key metrics in this series",
            "tool": "match_summary_report",
            "args": {"series_id": SERIES, "team_name": C9},
            "result": {f["dimension"]: {"v": f["value"], "base": f.get("base")}
                       for f in km_facts},
        })
        facts += km_facts
        reasoning.append(
            f"The overall overview yielded {len(km_facts)} metrics; "
            f"establishing a global impression first."
        )

        pat_done = False   # pistol/eco/pattern share one pattern_detection_report query

        # ② Topic evidence: one indirect question per topic (decomposed from the
        # original question)
        for t in topics:
            if t == "overview":
                continue

            if t == "fb":
                rows = await execute_custom_sql(
                    f"SELECT COUNT(*) AS n, ROUND(AVG(fb_team_won)*100,1) AS conv_pct"
                    f" FROM agg_first_blood_stats WHERE fb_team='{C9}'")
                rec = self._row(rows)
                questions.append({
                    "intent": "First blood conversion — count the first bloods and compute the first blood conversion rate",
                    "tool": "execute_custom_sql",
                    "args": {"sql": "agg_first_blood_stats WHERE fb_team", "subject": "overall"},
                    "result": rec,
                })
                if rec.get("n") is not None:
                    facts.append(make_fact(
                        subject={"series": SERIES, "team": C9},
                        dimension="opening_duels.fb", value=rec["n"],
                        unit="count", base=int(rec["n"]),
                        source={"tool": "execute_custom_sql",
                                "section": "agg_first_blood_stats"},
                    ))
                if rec.get("conv_pct") is not None:
                    facts.append(make_fact(
                        subject={"series": SERIES, "team": C9},
                        dimension="conversion.fb_conv", value=rec["conv_pct"],
                        unit="percent", base=int(rec.get("n") or 0),
                        source={"tool": "execute_custom_sql",
                                "section": "agg_first_blood_stats"},
                    ))
                reasoning.append(
                    f"First blood confirmed: count {rec.get('n')}, "
                    f"conversion rate {rec.get('conv_pct')}%."
                )

            if t == "by_map" or t == "win_rate":
                if t == "by_map" and "win_rate" in topics:
                    # win_rate already asked for per-map win rate; by_map must not
                    # ask the same question again
                    continue
                rows = await execute_custom_sql(
                    "SELECT map_name AS map,"
                    " ROUND(AVG(CASE WHEN winning_team_name='" + C9 +
                    "' THEN 1.0 ELSE 0.0 END)*100,1) AS win_rate, COUNT(*) AS n"
                    " FROM rounds WHERE series_id='" + SERIES + "' GROUP BY 1")
                rows_list = rows.get("rows") or []
                questions.append({
                    "intent": "Break down round win rate by map — which maps are strong, which are weak",
                    "tool": "execute_custom_sql",
                    "args": {"sql": "rounds GROUP BY map_name", "subject": "by_map"},
                    "result": rows_list,
                })
                for m, w, n in rows_list:
                    if w is None:
                        continue
                    facts.append(make_fact(
                        subject={"series": SERIES, "map": m, "team": C9},
                        dimension="map_win_rate", value=w, unit="percent",
                        base=int(n),
                        source={"tool": "execute_custom_sql",
                                "section": "rounds"},
                    ))
                reasoning.append(
                    f"Per-map win rate: { {m: w for m, w, _ in rows_list} }."
                )

            if t == "rounds":
                rows = await execute_custom_sql(
                    "SELECT COUNT(*) AS n,"
                    " SUM(CASE WHEN winning_team_name='" + C9 +
                    "' THEN 1 ELSE 0 END) AS won"
                    " FROM rounds WHERE series_id='" + SERIES + "'")
                rec = self._row(rows)
                questions.append({
                    "intent": "How many rounds did this series play in total, and how many did it win",
                    "tool": "execute_custom_sql",
                    "args": {"sql": "rounds COUNT/SUM", "subject": "series"},
                    "result": rec,
                })
                if rec.get("n") is not None:
                    facts.append(make_fact(
                        subject={"series": SERIES},
                        dimension="total_rounds", value=rec["n"],
                        unit="count", base=int(rec["n"]),
                        source={"tool": "execute_custom_sql", "section": "rounds"},
                    ))
                if rec.get("won") is not None:
                    facts.append(make_fact(
                        subject={"series": SERIES, "team": C9},
                        dimension="rounds_won", value=rec["won"],
                        unit="count", base=int(rec["n"] or 0),
                        source={"tool": "execute_custom_sql", "section": "rounds"},
                    ))
                reasoning.append(
                    f"Series rounds: total {rec.get('n')}, won {rec.get('won')}."
                )

            if t == "pistol" or t == "eco" or t == "pattern":
                if pat_done:
                    continue
                pat_done = True
                try:
                    pat = await pattern_detection_report(team_name=C9)
                    pkm = ((pat.get("key_metrics") or {}).get("economy") or {})
                    pscope = pat.get("scope") or {}
                    questions.append({
                        "intent": "Pistol round / eco round performance, plus the confidence of the conclusion",
                        "tool": "pattern_detection_report",
                        "args": {"team_name": C9},
                        "result": {"pistol": pkm.get("pistol"),
                                   "eco": pkm.get("eco"),
                                   "scope": pscope},
                    })
                    for dim, key in (("pistol_win_rate", "pistol"),
                                     ("eco_win_rate", "eco")):
                        node = pkm.get(key) or {}
                        if node.get("num") is not None:
                            facts.append(make_fact(
                                subject={"series": SERIES, "team": C9},
                                dimension=dim, value=node["num"],
                                unit="percent", base=int(node.get("denom") or 0),
                                source={"tool": "pattern_detection_report",
                                        "section": f"key_metrics.economy.{key}"},
                            ))
                    if pscope.get("rounds") is not None:
                        facts.append(make_fact(
                            subject={"series": SERIES, "team": C9},
                            dimension="pattern_rounds", value=pscope["rounds"],
                            unit="count", base=int(pscope["rounds"] or 0),
                            source={"tool": "pattern_detection_report",
                                    "section": "scope"},
                        ))
                    if pscope.get("confidence"):
                        facts.append(make_fact(
                            subject={"series": SERIES, "team": C9},
                            dimension="pattern_confidence",
                            value=pscope["confidence"],
                            source={"tool": "pattern_detection_report",
                                    "section": "scope"},
                        ))
                    reasoning.append(
                        f"Economy pattern: pistol={pkm.get('pistol')} eco={pkm.get('eco')} "
                        f"sample size {pscope.get('rounds')} "
                        f"confidence {pscope.get('confidence')}.")
                except Exception as e:                            # pragma: no cover
                    reasoning.append(
                        f"pattern_detection_report unavailable: {type(e).__name__}")

            if t == "kast":
                try:
                    ana = await match_analysis_report(series_id=SERIES, team_name=C9)
                    cons = ((ana.get("key_metrics") or {}).get("team") or {}).get("consistency") or {}
                    questions.append({
                        "intent": "What are the KAST and K/D values",
                        "tool": "match_analysis_report",
                        "args": {"series_id": SERIES, "team_name": C9},
                        "result": cons,
                    })
                    kast = cons.get("kast") or {}
                    if kast.get("num") is not None:
                        facts.append(make_fact(
                            subject={"series": SERIES, "team": C9},
                            dimension="kast_pct", value=kast["num"],
                            unit="percent", base=int(kast.get("denom") or 0),
                            source={"tool": "match_analysis_report",
                                    "section": "key_metrics.team.consistency.kast"},
                        ))
                    if cons.get("kd") is not None:
                        facts.append(make_fact(
                            subject={"series": SERIES, "team": C9},
                            dimension="kd_ratio", value=cons["kd"],
                            unit="raw", base=None,
                            source={"tool": "match_analysis_report",
                                    "section": "key_metrics.team.consistency.kd"},
                        ))
                    reasoning.append(f"KAST={kast.get('num')} K/D={cons.get('kd')}.")
                except Exception as e:                            # pragma: no cover
                    reasoning.append(
                        f"match_analysis_report unavailable: {type(e).__name__}")

            if t == "streak":
                tmap = self._map_from_question(task.question) or "Sunset"
                sql = ("SELECT cnt AS max_streak, start_round FROM ("
                       "  SELECT COUNT(*) AS cnt, MIN(round_number) AS start_round FROM ("
                       "    SELECT round_number, losing_team_name,"
                       "      ROW_NUMBER() OVER (ORDER BY round_number) AS rn,"
                       "      ROW_NUMBER() OVER (PARTITION BY losing_team_name"
                       "        ORDER BY round_number) AS grp"
                       f"    FROM rounds WHERE series_id='{SERIES}' AND map_name='{tmap}'"
                       "  ) t WHERE losing_team_name='" + C9 + "'"
                       "  GROUP BY rn - grp ORDER BY cnt DESC LIMIT 1)")
                rows = await execute_custom_sql(sql)
                rec = self._row(rows)
                questions.append({
                    "intent": f"Longest consecutive run of rounds lost — count consecutive losing streaks on {tmap}",
                    "tool": "execute_custom_sql",
                    "args": {"sql": "rounds gaps-and-islands", "subject": {"map": tmap}},
                    "result": rec,
                })
                if rec.get("max_streak") is not None:
                    facts.append(make_fact(
                        subject={"series": SERIES, "map": tmap, "team": C9},
                        dimension="max_losing_streak", value=rec["max_streak"],
                        unit="count", base=int(rec["max_streak"] or 0),
                        source={"tool": "execute_custom_sql", "section": "rounds"},
                    ))
                    facts.append(make_fact(
                        subject={"series": SERIES, "map": tmap, "team": C9},
                        dimension="streak_start_round", value=rec["start_round"],
                        unit="count", base=None,
                        source={"tool": "execute_custom_sql", "section": "rounds"},
                    ))
                reasoning.append(
                    f"Longest losing streak {rec.get('max_streak')} "
                    f"(starting at round {rec.get('start_round')}, on {tmap})."
                )

        # ③ Synthesis: organise the indirect evidence into an answer to the original
        # question (the network genius's last step)
        conclusion = self._integrate_review(task, facts, topics)

        return {
            "facts": facts,
            "narrative": {"text": self._narrate(facts), "comparable": False},
            "review_topics": topics,
            "questions_asked": questions,
            "reasoning": reasoning,
            "conclusion": conclusion,
            "skill_used": "review-" + "+".join(topics),
        }

    def _integrate_review(self, task: Any, facts: list[dict[str, Any]],
                          topics: list[str]) -> str:
        """Synthesised conclusion: organise the indirectly verified facts into a passage
        that directly answers the original question."""
        if not facts:
            return (f"No usable data for '{task.question}' yet — so far only the "
                    f"overall overview is available; the key numbers have not been "
                    f"asked for.")
        lines = []
        for f in facts:
            subj = "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items())
            lines.append(f"- {subj} · {f['dimension']} = {f['value']}")
        return (f"Synthesising {len(facts)} indirect evidence results to answer "
                f"'{task.question}':\n" + "\n".join(lines))

    def _narrate(self, facts: list[dict[str, Any]]) -> str:
        """Narrative section: not compared, only shown to a human."""
        return (f"This round collected {len(facts)} facts, covering "
                f"{'/'.join(sorted({f['dimension'] for f in facts}))}")

    def learn(self, *, covered: list[str], missing: list[str], task: Any = None,
              referee: Any = None, verdict: str = "",
              rejected_low_base: list[str] | None = None,
              confidence: float = 0.0) -> None:
        """After a round ends, update the skill library: for whichever dimension is missing,
        add a skill that can cover it.

        Note: this must be judged by **dimension**, not by the scoring point's Chinese
        name — the name carries no dimension information (v1 fell into exactly this
        trap, so the skill library never learned anything).

        The scripted baseline **does not consume** the structured feedback pack
        (referee / verdict / rejected_low_base / confidence) — that machinery is for
        LLMVoyager's "write a program → get it running → store the program" loop;
        it is accepted here only to keep the signature compatible with the caller
        (measured: running auto-generated new questions crashed with a TypeError
        because of an incompatible signature).
        """
        dim_by_point = {p.point: p.dimension for p in (task.rubric if task else [])}
        miss_dims = [dim_by_point.get(m, m) for m in missing]

        if any(d.startswith("map_") for d in miss_dims) and \
           not any(s.name == "分图下钻" for s in self.skills):
            self._ensure_skill(
                name="分图下钻",  # skill name kept in Chinese: run_mve.py matches on it
                steps=["match_summary_report", "execute_custom_sql(agg_first_blood_stats by map)"],
                yields=["map_fb_conv"],
            )


# ---------------------------------------------------------------------------
# LLMVoyager: let the model decide which tools to call, and learn from its own gaps
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tool catalog: copied verbatim from VLML's MCP server (vlml/src/vlml/server.py).
# Its docstrings are reused unchanged — VLML puts "what this is + when to use it"
# on that single line and uses it to steer the LLM's orchestration. We use the very
# same text for Voyager.
# Note: the externally exposed MCP tool name is query_sql (only the Python
# implementation function is named execute_custom_sql).
# ---------------------------------------------------------------------------

TOOL_CATALOG = """
Available tools (all return structured data only — no conclusions, no advice):

1. match_summary_report(series_id, team_name?)
   Lightweight summary (metadata, team_comparison, key_metrics, benchmarks).
   **Look at this first to get the global picture.**
   ⚠️ map_name is not supported (VLML upstream bug); to drill down per map use 9. query_sql.

2. match_players_report(series_id, team_name?, map_name?)
   Player perspective (performance, kast_impact_analysis, opening_death_impact,
   highlight_rounds). Use for player analysis.

3. match_rounds_report(series_id, team_name?, map_name?, round_start?, round_end?)
   Round by round (round_timeline, round_situations, half_breakdown).
   **Very heavy; use pagination on large datasets.**

4. match_economy_report(series_id, team_name?)
   Economy and tactics (economy_context, attack_patterns). Use for economy cascade
   or predictability analysis.
   ⚠️ map_name is not supported (VLML upstream bug); to drill down per map use 9. query_sql.

5. match_analysis_report(series_id, team_name?)
   All 18 sections. Use only when you need everything at once.
   ⚠️ map_name is not supported (VLML upstream bug); to drill down per map use 9. query_sql.

6. player_profile_report(player_name, series_ids?, last_n_series?, map_name?, agent_name?)
7. scouting_report(team_name, series_ids?, last_n_series?)
   Opponent scouting. **map_name is not supported (VLML upstream bug); to drill down
   per map use 9. query_sql**.
8. pattern_detection_report(team_name?, player_name?, tournament_name?, series_ids?, min_rounds?)
9. query_sql(sql_query) — custom SQL, **SELECT only**. To drill down per map or per
   round you must use it
   (something like WHERE map_name='Haven' always works and fills the gap left by
   1/4/5/7 above, which cannot filter by map).
10. get_database_info() — get the table list and usage tips.

Common tables (for query_sql):
  agg_first_blood_stats(round_id, game_id, series_id, map_name, round_number,
                        fb_team, fd_team, fb_player, fd_player,
                        winning_team_name, fb_team_won, fd_team_won)
  rounds(round_id, series_id, game_id, round_number, map_name,
         winning_team_name, losing_team_name, end_reason)
  games(game_id, series_id, map_name, team1_name, team2_name, winning_team_name)
  base_events(series_id, game_id, round_id, event_type, actor_player_name,
              actor_team_name, target_player_name, is_kill, is_first_blood,
              is_plant, is_defuse, map_name, ...)
"""

# VLML's own usage tips, copied verbatim from get_database_info().usage_tips
# (measured output, 4 items)
VLML_USAGE_TIPS = """
Use agg_player_game_stats for fast player performance queries
Use base_events for detailed event-level analysis
All tables have team_name populated (91% coverage)
Metrics include: K/D, ADR, KAST%, first bloods, multi-kills
"""

# The block below is **not** written by VLML; I added it from measurement.
# Why: VLML's usage_tips says nothing about SQL limitations, yet measurement shows
# you hit these:
#   WITH x AS (...) SELECT ...  ->  {'error': 'Only SELECT queries are allowed'}
#   SELECT ... FROM (SELECT ...)  ->  returns normally
# Without this in the catalog, the model keeps running into the same wall, and even
# then it cannot tell whether it wrote the SQL wrong or the tool is broken.
LOCAL_SQL_NOTES = """
Measured limitations of query_sql (all hit in practice):
- A statement must start with SELECT. **WITH (CTE) is NOT supported**; writing one
  raises "Only SELECT queries are allowed".
- For multi-step computation, use a nested subquery: SELECT ... FROM (SELECT ...) t
  — that one passes.
- Example: for "run of consecutive rounds" problems such as a losing streak on a map,
  use a nested subquery + ROW_NUMBER() to segment
  (gaps-and-islands), rather than fetching the rounds back and counting them yourself.
    For the concrete skeleton, see 【Knowledge graph · relevant subgraph for this question】
    above — if it is there, follow it.

**If you are unsure of a column name, look it up first — never guess** (this is where
hallucination is most likely: a guessed column name
will always raise a Binder Error, and then you will guess the very same thing again
next round):
    SELECT column_name FROM information_schema.columns WHERE table_name='rounds'
You can look up table names too: SELECT table_name FROM information_schema.columns GROUP BY table_name
Note: the column name for "team" differs across tables (some have team_name, some have
winning_team_name); confirm the actual column name in the target table before writing SQL.

Known tool defects (VLML upstream bugs — not your mistake):
- match_summary_report / match_analysis_report / match_economy_report / scouting_report
  **crash when passed map_name** (error "Referenced table tgs not found").
  To analyse per map, use query_sql and add WHERE map_name=... yourself — that always works.

**If the question names a map, you must filter by that map (answering the wrong
question is more dangerous than erroring out)**:
- When Haven / Corrode / Lotus or any other map name appears in the question, your
  query_sql **must include**
  `WHERE map_name='<that map>'`, otherwise you get data for the whole series back and
  the conclusion will be attributed to the wrong map.
- Do not rely on team_name filtering alone — team is a whole-series dimension and
  cannot filter out maps.
- Conversely: when the question names no map, do not add map_name arbitrarily, or you
  will lose the data for the other maps.
"""

PLAN_SCHEMA = """Output a single JSON object and nothing else:
{"thought": "One sentence saying how you plan to query",
 "calls": [{"tool": "tool name", "args": {"param_name": "param_value"}}]}
Cap on the number of calls = the number of scoring points for this question (one query
per scoring point is enough); do not output any explanatory text."""


# ---------------------------------------------------------------------------
# Tool dispatch table: TOOL_CATALOG lists 10 tools, so all 10 must really be callable
# here.
# A mismatch between catalog and capability is one source of false positives — the
# model plans against the catalog while the execution side reports "unknown tool".
# query_sql is the external MCP name, execute_custom_sql is the Python implementation
# name; both map to the same function.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Known defect: VLML's match_summary_report / match_analysis_report crash when passed
# map_name
#
#   Measured: BinderException: Referenced table "tgs" not found! (Candidate: "prs")
#   Root cause: tools/reports/match_analysis.py:172 splices map_filter into
#         "AND tgs.map_name = ?", but the same filter is reused in
#         tools/sql/team_economy_eco.sql — that SQL's subquery only has prs/r/g
#         aliases, no tgs. Triggered only when map_name is passed.
#   This is a VLML upstream bug, unrelated to the synthetic data (without map_name
#   everything works).
#
# Handling: do not modify the repository source; **raise an explicit error** at the
# dispatch layer and steer the model to query_sql instead.
#   Do not silently degrade to "ignore map_name" — that would return whole-series data
#   masquerading as per-map data,
#   which is far more dangerous than erroring out (facts would carry the wrong subject).
# ---------------------------------------------------------------------------
VLML_MAP_NAME_BUG = (
    "This tool crashes when passed map_name (VLML upstream bug: team_economy_eco.sql "
    "references a non-existent alias tgs). "
    "To analyse per map, use query_sql and filter map_name yourself."
)


async def _guarded_report(fn, args: dict[str, Any]):
    if args.get("map_name"):
        return {"error": VLML_MAP_NAME_BUG}
    return await fn(**args)


def _wrap_guarded(fn):
    """Wrap a report tool that has the map_name bug with a guard layer.

    When map_name is passed, return a clean steering error ('use query_sql instead')
    rather than leaking VLML's upstream
    BinderException (Referenced table tgs not found) to the caller — that error
    prevents coach/voyager's _execute from getting obs, and the whole round then
    degrades to skipped.
    """
    async def _wrapped(**kwargs):
        return await _guarded_report(fn, kwargs)
    try:
        _wrapped.__name__ = getattr(fn, "__name__", "guarded")
    except Exception:                                       # pragma: no cover
        pass
    return _wrapped


TOOL_REGISTRY: dict[str, Any] = {
    "match_summary_report": match_summary_report,
    "match_players_report": match_players_report,
    "match_rounds_report": match_rounds_report,
    "match_economy_report": match_economy_report,
    "match_analysis_report": match_analysis_report,
    "player_profile_report": player_profile_report,
    "scouting_report": scouting_report,
    "pattern_detection_report": pattern_detection_report,
    "query_sql": execute_custom_sql,
    "execute_custom_sql": execute_custom_sql,
    "get_database_info": get_database_info,
}

# These report tools share the same map_filter splicing logic, so they all hit the
# same bug
MAP_NAME_BUGGY_TOOLS = {
    "match_summary_report", "match_analysis_report",
    "match_economy_report", "scouting_report",
}

# Swap the 4 report tools that have the map_name bug for guarded versions.
# Previously _guarded_report / MAP_NAME_BUGGY_TOOLS existed but were never wired into
# the registry — dead code,
# so passing map_name went straight into VLML's upstream tgs BinderException. Wired
# up here.
for _name in MAP_NAME_BUGGY_TOOLS:
    if _name in TOOL_REGISTRY:
        TOOL_REGISTRY[_name] = _wrap_guarded(TOOL_REGISTRY[_name])


_DB_SELF_DESC: str = ""


async def db_self_description() -> str:
    """Following VLML's own approach: hand "what is available" to the caller
    **before** any SQL is written.

    Source: get_database_info() in vlml/src/vlml/tools/db_query_tools.py:272-334:
    it does not validate the input, it **self-describes** — returning
      - available_tables: 9 tables, each with a one-line human-readable description
      - usage_tips: 4 usage tips
      - sample_recent_stats: real samples (so the caller sees what the values look like)
      - recent_series
    This is VLML's real answer to "non-standard input": the caller knows the format
    before taking action,
    instead of writing something, getting it bounced, and then guessing.

    My earlier approach was the reverse — relying on feeding back errors
    (schema_probe to look up column names, the parameter whitelist saying "only these
    are accepted"). That is **after-the-fact remediation**, and a round can only try
    once.
    Both are needed: this handles the before, and those gates handle the after.
    """
    global _DB_SELF_DESC
    if _DB_SELF_DESC:
        return _DB_SELF_DESC
    try:
        info = await get_database_info()
    except Exception as e:
        return f"(Database self-description unavailable: {type(e).__name__}: {e})"
    if not isinstance(info, dict) or info.get("error"):
        return (f"(Database self-description unavailable: "
                f"{str((info or {}).get('error'))[:150]})")

    tables = "\n".join(f"  - {t}" for t in (info.get("available_tables") or []))
    tips = "\n".join(f"  - {t}" for t in (info.get("usage_tips") or []))
    series = ", ".join(str(x) for x in (info.get("recent_series") or [])[:5])
    sample = (info.get("sample_recent_stats") or [])[:1]
    sample_txt = ""
    if sample:
        sample_txt = "\nSample (look carefully at what the values look like):\n  " + json.dumps(
            sample[0], ensure_ascii=False, default=str)[:300]

    _DB_SELF_DESC = (
        "【Database self-description · given by VLML itself; read this before writing SQL】\n"
        f"Available tables:\n{tables}\n\n"
        f"VLML's usage tips:\n{tips}\n\n"
        f"Existing series: {series or '(none)'}"
        f"{sample_txt}\n\n"
        "If you are unsure of a column name, look it up first — never guess:\n"
        "  SELECT column_name FROM information_schema.columns WHERE table_name='<table name>'\n"
        "  SELECT table_name FROM information_schema.columns GROUP BY table_name"
    )
    return _DB_SELF_DESC


def _coerce_value(v: Any) -> Any:
    """Normalise a model-supplied value into a comparable form.

    Measurement shows the model writes percentages as "50%" and confidence as
    "Moderate" — the content is right,
    but `"50%"` will not convert to float, so at comparison time it becomes a
    "value not comparable", which is wasted work.
    Formatting differences should not affect scoring, so normalise them here;
    **whether the number is right is still decided by the referee**.
    """
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        t = v.strip()
        if t.endswith("%"):                      # "50%" → 50.0
            try:
                return float(t[:-1].strip())
            except ValueError:
                return t
        try:
            return float(t)                      # "59" → 59.0
        except ValueError:
            return t.lower() if t.isascii() else t   # "Moderate" → "moderate"
    return v


def _blueprint_hint(skill: dict[str, Any]) -> str:
    """Also carry the skill blueprint's "standard orchestration sequence" into the
    prompt — not just a single sentence.

    The problem measured in practice: given only a one-sentence natural-language
    lesson, the model **reads it but does not change behaviour** —
    2 lessons injected, the trajectory is still a single tool, and coverage even
    dropped to 30%. The reason is that the lesson says
    "split by which dimension", not "which tools to call".

    The study companion's ability module delivers the whole blueprint
    (defect → strategy → concrete action),
    not a one-sentence summary (`adaptive_learning/cognitive_delivery.py`). Same
    here:
    hand over the standard orchestration sequence learned on the bypass as well.
    """
    bp = skill.get("blueprint") or {}
    traj = [str(t) for t in (bp.get("trajectory") or [])
            if not str(t).startswith("<")]
    if not traj:
        return ""
    hint = f" | Standard orchestration: {' → '.join(traj)}"
    if bp.get("strategy"):
        hint += f" (strategy: {bp['strategy']})"
    return hint


def _code_block(skill: dict[str, Any], limit: int = 900) -> str:
    """The skill's **solution source code** — the original version splices
    `entry['code']` (the `programs` property) into the prompt, not the function
    name and not a one-sentence description
    (`voyager/agents/skill.py:55`).

    Why the source code is mandatory: given only advice like "first call query_sql
    to get the overall picture", the model reads it but does not change behaviour
    (measured: 2 lessons injected, the trajectory is still a single tool, coverage
    drops to 30%). What it lacks is not "direction",
    it is "what this SQL actually looks like" — which table, how to layer it, which
    column to take. Give it the source
    and only then does it know what to write.
    """
    code = str(skill.get("code") or "").strip()
    if not code:
        return ""
    src = code if len(code) <= limit else code[:limit].rstrip() + " …"
    indented = "\n".join("      " + ln for ln in src.splitlines())
    return "\n    Solution source code:\n" + indented


# ---------------------------------------------------------------------------
# Knowledge graph = the equivalent of control_primitives
# ---------------------------------------------------------------------------
# Why could the original Voyager write runnable JS? Not because the model knows
# Mineflayer, but because
# `render_system_message()` splices the **source code of control_primitives** into
# the system
# message — the model can see the implementation of every skill function, not just
# the function name
# (voyager/agents/action.py:render_system_message).
#
# MVE's isomorphic counterpart: inject "how is this metric computed / which table to
# read / which columns to use / where others commonly go wrong"
# as a **relevant subgraph for this question**. Previously this block did not exist
# at all — the graph only fed dashboard
# visualisations, forensic hints and grading feedback, so Voyager could not see a
# single line of it while planning and could only guess column names
# (measured: on round 2 of corrode it wrote fb_conv and round_number as columns in
# the SELECT).
#
# Division of labour with "asking for help": the graph is **always-on** context
# (like control_primitives always being in the system
# message), does not count toward used_hint and applies no mastery discount;
# asking for help (a VLML0 explanation) is the study-companion sense of
# "borrowing help" and takes the 0.85 factor. The two must not be conflated.
# ---------------------------------------------------------------------------

_GRAPH: Any = None          # None=not loaded / False=unavailable / KnowledgeGraph=loaded

# Whether to hand over SQL structural skeletons (gaps-and-islands and the like) —
# on by default; an environment variable can turn it off for A/B
GRAPH_WITH_RECIPE = os.environ.get("MVE_GRAPH_RECIPE", "1") != "0"

# Master switch for graph injection: MVE_GRAPH=0 falls back to the baseline with "no
# control_primitives".
# The only reason it exists is A/B — to find out whether the graph actually helps,
# you must be able to turn it off and rerun the same question,
# otherwise "it got better after adding the graph" can never be falsified (it might
# have come from some other change).
GRAPH_ENABLED = os.environ.get("MVE_GRAPH", "1") != "0"
# Can be turned off temporarily (for exams). Why not an environment variable:
# environment variables are **process-level**,
# while "one practice round → one exam round" has to switch back and forth inside
# the same process. An exam = withdraw the scaffold,
# and see whether the model can still get it right using the skill library it has
# accumulated on its own — that is what a learning curve is supposed to measure.
GRAPH_STATE: dict[str, bool] = {"on": GRAPH_ENABLED}

# Module-level mirror of the current scaffold tier (written by
# LLMVoyager.__init__).
# The reason it exists is the same as for `GRAPH_STATE`: graph assertions are
# module-level functions and cannot see self.hint_level,
# but they must scale with the tier — see the study companion's evidence in
# `graph_context`.
HINT_STATE: dict[str, str] = {"level": ""}


def _assist_on() -> bool:
    """Whether the graph's **assistance** (assertions / auto-fixes) should be
    given right now.

    Criterion: the graph is on + assertions are on + **the scaffold has not been
    withdrawn to none**.
    Study-companion isomorphic: in the question-generation prompt the graph is an
    optional section that can be dropped entirely; the grading contract
    does not contain the graph at all. So "still intercepting errors on questions
    the model can already do" is something the study companion never does.
    """
    return bool(GRAPH_STATE["on"] and ASSERT_ENABLED
                and str(HINT_STATE.get("level") or "").strip().lower() != HINT_NONE)

# Separate switches for the two layers in the graph — the only reason they exist is
# A/B.
# The insight layer ("which part of the tool's output to take") and the data layering
# (which layer the table is in, what feeds it) are new this round,
# so they must be separately switchable in order to measure whether the model
# actually uses them; otherwise you can only go on "it feels useful".
GRAPH_SECTION = os.environ.get("MVE_GRAPH_SECTION", "1") != "0"
GRAPH_STAGE = os.environ.get("MVE_GRAPH_STAGE", "1") != "0"
GRAPH_INSIGHT = os.environ.get("MVE_GRAPH_INSIGHT", "1") != "0"

# Seed-layer fallback switch: when the old path (fetching dimensions by topic_id)
# comes up empty, use the **question text**
# to match against 212 facts that are independent of the questions. Turning it off
# lets you A/B whether "the graph really works for every question".
GRAPH_SEED_FALLBACK = os.environ.get("MVE_GRAPH_SEED", "1") != "0"

# Master switch for code-based evidence retrieval: when MVE_ACTION_CODE=1 Voyager
# produces **code** instead of plan JSON,
# and the values are extracted by the interpreter along the path — no LLM
# transcription.
# The reason it exists: measurement shows the root cause of "grabbing the wrong key"
# lies in the transcription step. The source states the path
# `key_metrics.team.consistency.kd` perfectly clearly, yet the model still hands in
# kast's value 1.0
# (kast=109/109) — it was **eyeballing** through the huge JSON returned by the tool.
# Hand the path to the interpreter to execute and it cannot misread.
#
# 2026-10-06: default changed to 1 (was 0). Controlled experiment (same skill
# library / same time / same question):
#     code path 100%   — kd_ratio handed in 1.24
#     default path 50%  — kd_ratio handed in 1.0 (kast's num/denom mapped onto a
#                         scalar)
# The skill library already states the correct path; the two paths have **identical
# input**, and the only difference is "execute vs read".
# That is precisely the essence of the original Voyager: skills are exec'd, not
# read.
# Turning it on by default does not affect SQL-style questions — `_is_tool_path_task`
# routes by answer-spec source,
# and answer_spec.sql still takes the original path (that one has the graph skeleton
# and layering assertions; corrode once dropped to 0% when everything went through
# code).
ACTION_CODE = os.environ.get("MVE_ACTION_CODE", "1") != "0"

# Turn off **assertions only** (hard validation) while keeping the prompt hints —
# A/B must be able to separate the two:
# once assertions take effect they block the failure, which completely masks the
# marginal effect of the hint layer
# (measured: pistol is 100% whether or not section is on, because the parameter
# assertions already cover it).
ASSERT_ENABLED = os.environ.get("MVE_ASSERT", "1") != "0"


def _load_graph() -> Any:
    global _GRAPH
    if _GRAPH is None:
        try:
            import knowledge_graph
            g = knowledge_graph.KnowledgeGraph.load()
            _GRAPH = g if g.nodes else False
        except Exception:
            _GRAPH = False
    return _GRAPH or None


def _apply_auto_fixes(plan: dict[str, Any], task: Any,
                      errors: list[str]) -> None:
    """After validation passes, directly fix any SQL that can be repaired
    mechanically."""
    for c in plan.get("calls") or []:
        if not isinstance(c, dict):
            continue
        if TOOL_REGISTRY.get(str(c.get("tool"))) is not execute_custom_sql:
            continue
        args = dict(c.get("args") or {})
        key = "sql_query" if "sql_query" in args else "sql"
        sql = str(args.get(key) or "")
        fixed, note = auto_fix_layering(sql, task)
        if note and fixed != sql:
            args[key] = fixed
            c["args"] = args
            errors.append(f"Auto-fixed SQL: {note}")


def _check_tool_params(tool: str, args: dict[str, Any]) -> str:
    """Intercept "at least one of these must be passed" parameter constraints
    before the call is issued.

    Why this is needed: measured on the pistol_eco_pattern question, the model
    called the right tool per the graph —
    `pattern_detection_report` — but passed not a single parameter; the tool returned
    scope=null,
    all four scoring points came back empty, coverage 0%. It "called the right tool"
    yet retrieved nothing.
    Writing the hint (param_hint) into the prompt is not enough; following the
    precedent of the layering assertions, make it hard validation.
    """
    if not _assist_on():
        return ""
    g = _load_graph()
    if g is None:
        return ""
    node = g.nodes.get(f"tool:{tool}")
    if node is None:
        return ""
    need = list(node.detail.get("needs_any_of") or [])
    if not need:
        return ""
    if any(str(k) in (args or {}) and args.get(k) not in (None, "") for k in need):
        return ""
    if need:
        return (f"{tool} must be passed at least one of {' or '.join(need)} to "
                f"return a valid result — passing none returns empty "
                f"(measured scope=null), so this dimension cannot be retrieved"
                )

    # Parameter **type**: cases that do not raise a syntax error but are entirely
    # wrong semantically
    # (passing series_ids='2843069' gets split character by character → rounds=0)
    for key, want in (node.detail.get("param_types") or {}).items():
        if key not in (args or {}):
            continue
        val = args.get(key)
        if want == "list" and isinstance(val, str):
            return (f"{tool}'s {key} must be passed as an **array** [\"{val}\"], "
                    f"not a string — "
                    f"a string gets split character by character and the result "
                    f"becomes an empty set (measured rounds=0)")
    return ""


def _inner_where(sql: str) -> str:
    """The innermost WHERE clause (from the first WHERE to the next closing
    parenthesis).

    In segmented computation (gaps-and-islands), "which layer does the filtering"
    is itself part of the metric definition:
    the inner layer numbers **all rounds** first, and only the outer layer filters
    by team. Moving the team filter into the inner layer
    leaves only the rounds that team lost, so the numbering comes out naturally
    consecutive and the whole run is treated as one block
    (measured: it computed 13 while the true value was 8).
    """
    i = (sql or "").upper().find(" WHERE ")
    if i < 0:
        return ""
    rest = sql[i:]
    j = rest.find(")")
    return rest[:j] if j > 0 else rest


def _check_group_order(sql: str, task: Any) -> tuple[list[str], str]:
    """Row order must be deterministic after GROUP BY — otherwise "take the values from
    row N" assigns them to the wrong group.

    Measured (`map_rounds_split`): the model wrote a `GROUP BY map_name` returning
    three rows,
    then took the values by row index and handed in Corrode/Haven/Lotus =
    21/24/14, while the true values are 14/21/24
    — **every number is right, they are just matched to the wrong map**. This kind of
    error raises nothing and returns nothing empty;
    only comparison by the referee can catch it, and by then a whole round has been
    wasted.

    The most robust form is **one SQL per group** (the referee's own answer_spec is
    written that way:
    `WHERE map_name='Corrode'`), so on rejection we hand out that skeleton with the
    same metric definition.
    """
    if not _assist_on():
        return [], ""
    s = (sql or "").upper()
    if "GROUP BY" not in s or "ORDER BY" in s:
        return [], ""
    recipe = ""
    for p in (getattr(task, "rubric", None) or []):
        subj = getattr(p, "subject", None) or {}
        spec = getattr(p, "answer_spec", None)
        if subj.get("map") and getattr(spec, "sql", ""):
            try:
                from knowledge_graph import _sql_recipe
                recipe = _sql_recipe(str(spec.sql))[:400]
            except Exception:
                recipe = ""
            break
    return ([
        "You used GROUP BY but **no ORDER BY** — the order of the result rows is "
        "undetermined, so taking values by row N assigns the numbers to the wrong "
        "group (measured: the Corrode/Haven/Lotus round counts were entirely "
        "misaligned: submitted 21/24/14, true values 14/21/24 — the numbers are all "
        "right, they are just matched to the wrong map). "
        "Switch to **one SQL per group** (WHERE map_name='?'), which is the most "
        "robust; if you insist on GROUP BY you must explicitly ORDER BY the "
        "grouping key.",
    ], recipe)


def _check_sql_layering(sql: str, task: Any) -> tuple[list[str], str]:
    """Following process_ai_message's AST assertion: reject when invalid instead of
    just "a friendly reminder".

    The original uses babel to assert the structure of the JS code (must be async,
    the parameter must be named bot);
    here we isomorphically assert the **layered structure** of the SQL, with criteria
    taken from the knowledge graph's recipe.

    Returns (all problems, correct skeleton). **Returning all problems together**
    was forced by measurement:
    when only one is stated at a time, the model fixes the WHERE and forgets the
    GROUP BY, then goes back to WHERE the next round,
    spinning through three retries → the whole round is wasted (this is where the
    0%-coverage fluctuations come from).
    """
    if not _assist_on():
        return [], ""
    if not sql or "PARTITION BY" not in sql.upper():
        return [], ""
    g = _load_graph()
    if g is None:
        return [], ""
    problems: list[str] = []
    recipe = ""
    try:
        for dim in g.dims_of(str(getattr(task, "topic_id", "") or "")):
            rule = g.layering_rule(dim)
            if not rule:
                continue
            recipe = recipe or str(rule.get("note") or "")
            inner = _inner_where(sql)
            for col in rule.get("outer_only") or []:
                if re.search(r"\b" + re.escape(col) + r"\b", inner or ""):
                    problems.append(
                        f"{col} is written in the **innermost** WHERE — it filters "
                        f"out the team before numbering, so the remaining round "
                        f"numbers are necessarily consecutive and the whole run is "
                        f"treated as one block; "
                        f"{col} belongs in the outer layer after `) t WHERE`, "
                        f"with the inner layer keeping only series_id + map_name")
            # The grouping must not be lost: measured — after the WHERE was
            # intercepted it helpfully deleted the GROUP BY as well,
            # so all the lost rounds merged into one run again and the value was
            # still wrong.
            ids = rule.get("group_by_ids") or []
            if ids:
                m = re.search(r"GROUP\s+BY\s+(.+?)(?:\)\s*\w+\s+ORDER|\)\s*\w+$|"
                              r"\bORDER\b|$)", sql, re.IGNORECASE | re.DOTALL)
                gsql = (m.group(1) if m else "")
                if not gsql or not all(
                        re.search(r"\b" + re.escape(i) + r"\b", gsql) for i in ids):
                    problems.append(
                        f"Missing grouping `GROUP BY ({rule.get('group_by_expr') or ', '.join(ids)})`"
                        f" — without grouping by (number difference) you only get one "
                        f"whole run, "
                        f"which is exactly why you computed 'how many rounds were "
                        f"lost across the entire match'")
            if problems:
                break
    except Exception:
        return [], ""
    return problems, recipe


def auto_fix_layering(sql: str, task: Any) -> tuple[str, str]:
    """If it can be fixed mechanically, **fix it directly** — do not count on the model
    to fix it itself.

    Why this must live in code: the rejection message is perfectly clear ("move
    losing_team_name to
    the outer layer after `) t WHERE`"), yet the model resubmitted the identical
    statement three times in a row — it believes its own form
    is correct. Retries are unreliable, so never hand it a transformation you can
    apply automatically.

    Only one transformation is performed: **when the outer layer already has the
    condition, delete the redundant copy in the inner layer**.
    Semantics are unchanged (not a single filter condition is lost, it is only moved
    between layers), so there is no risk of "breaking the SQL".
    """
    if not _assist_on():
        return sql, ""
    if not sql or "PARTITION BY" not in sql.upper():
        return sql, ""
    g = _load_graph()
    if g is None:
        return sql, ""
    try:
        for dim in g.dims_of(str(getattr(task, "topic_id", "") or "")):
            rule = g.layering_rule(dim)
            if not rule:
                continue
            fixed = sql
            notes: list[str] = []
            for col in rule.get("outer_only") or []:
                i = fixed.upper().find(" WHERE ")
                if i < 0:
                    continue
                j = fixed.find(")", i)
                if j < 0:
                    continue
                inner, tail = fixed[i:j], fixed[j:]
                if not re.search(r"\b" + re.escape(col) + r"\b", inner):
                    continue
                # The outer layer must already have the same condition, otherwise deleting it
                # would drop the filter — do not fix
                if not re.search(r"\b" + re.escape(col) + r"\b", tail):
                    continue
                new_inner = re.sub(
                    r"\s+AND\s+" + re.escape(col)
                    + r"\s*(?:=|<>|!=|IS|IN|LIKE)\s*(?:'[^']*'|\"[^\"]*\"|[\w.()]+)",
                    "", inner, count=1, flags=re.IGNORECASE)
                if new_inner == inner:
                    continue
                fixed = fixed[:i] + new_inner + tail
                notes.append(
                    f"moved the inner {col} filter to the outer layer "
                    f"(segmentation must number first, then filter)")
            if notes:
                return fixed, "; ".join(notes)
    except Exception:
        return sql, ""
    return sql, ""


def graph_context(task: Any, *, with_recipe: bool | None = None,
                  mode: str = "plan", hint: str = "") -> str:
    """The relevant subgraph for this question, rendered as text for the model;
    returns an empty string when the graph is unavailable (does not block the main
    flow).

    Two paths, tried in order:
    1. `render_for_prompt(topic_id)` — for questions that have appeared before, going
       through the rubric-derived dimensions,
       with the most complete information (metric definition, confusions, skeleton,
       parameter traps).
    2. `render_for_query(question text)` — **any question**, using the seed layer
       (212 facts independent of the questions) matched by the question text. This
       is exactly how the study companion makes the graph work for every question
       (`build_knowledge_guidance_payload` → `match_topics(query)`):
       its 82 knowledge-point seeds exist before the questions, and the questions
       only determine the focus of the match.
       Without this fallback, MVE's graph degenerates into "notes on the few
       questions that happened to appear" —
       the graph block for a new question is empty.

    `hint` = the scaffold tier (full / partial / none in `difficulty.py`).
    **The graph block must be governed by it** — this is the layer MVE previously
    missed, and also the study companion's single most critical
    constraint (see the evidence below):

      · `llm_prompts.py:270-275`: in the question-generation prompt the graph content
        belongs to the **optional**
        section; `llm_prompts.py:285-303` **drops the whole section** when the token
        budget is exceeded, and what remains in the `required` section is only
        blockers / question type / difficulty —
        i.e. **the study companion would rather give no graph than give more graph
        than is needed**.
      · `prompt_templates.py:170-189`: not one of the sixteen requirements in the
        grading contract
        **mentions the graph**; `tutor_llm_agent_answer_evaluate.py:16-38`
        passes only question / answer / expected_answer.
      · `llm_prompts.py:455-487`: the place where the graph is genuinely a
        first-class citizen is the **explanation**
        (assembled into a named section "Knowledge graph guidance:"), not problem
        solving.

    Consequence of missing this layer (measured): the graph block used to be governed
    only by the env global switch, unrelated to mastery or
    coverage — so a question practised to 100% coverage and 0.9 mastery still handed
    Voyager "how to compute it / which column to take / the full SQL skeleton", and
    the practice score necessarily pegged at the top.
    Tiers:
        full    → give everything (structural layer + metric-definition layer): still
                  learning, prop it up first
        partial → give only the structural layer (tables / columns / upstream / tool
                  traps): half known, prop up half
        none    → give nothing: once you know it, stop looking (the study companion
                  drops the optional section entirely)
    """
    if not GRAPH_STATE["on"]:
        return ""
    _h = str(hint or "").strip().lower()
    if _h == HINT_NONE:
        return ""                      # study companion: the optional section is
                                       # dropped entirely
    if with_recipe is None:
        with_recipe = (_h != HINT_PARTIAL)
    g = _load_graph()
    if g is None:
        return ""
    topic_id = str(getattr(task, "topic_id", "") or "")
    if not topic_id:
        return ""
    try:
        s = g.render_for_prompt(
            topic_id,
            with_recipe=bool(with_recipe) and GRAPH_WITH_RECIPE,
            # The metric-definition layer (how to compute it / which column to take /
            # the skeleton) = **the answer template**:
            # only given at the full tier. partial / no tier give the structural
            # layer only.
            with_formula=(_h == HINT_FULL),
            with_section=GRAPH_SECTION,
            with_stage=GRAPH_STAGE,
            with_insight=GRAPH_INSIGHT,
        )
    except Exception as exc:  # pragma: no cover
        # Must not swallow silently: an AttributeError was once swallowed here, and
        # the symptom was the graph "mysteriously disappearing"
        # (no graph block in the prompt, but not a single error), which took a long
        # time to diagnose.
        print(f"[graph] render_for_prompt failed, no graph for this question: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        s = ""
    if s or not GRAPH_SEED_FALLBACK:
        return s
    # Questions that have appeared before can also land here: the topic_id exists but
    # the graph has no dimensions for it (a newly added question,
    # or a rubric whose dimension names changed). In that case use the question text
    # to go through the seed layer.
    question = str(getattr(task, "question", "") or "")
    if not question:
        return ""
    try:
        return g.render_for_query(question, mode=mode, limit=3)
    except Exception as exc:  # pragma: no cover
        print(f"[graph] render_for_query failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return ""


def _tools_used_before(topic_id: str) -> set[str]:
    """The tools this question **really called** in **previous rounds** (across
    processes).

    Why this must be cross-process: loop mode starts a fresh run_mve (a new Voyager
    instance) for each question,
    so looking only at `self.trajectory` / `self.attempted` shows just this process's
    rounds.
    Measured consequence: series_totals ran 24 rounds stuck at 50%, the feedback named
    query_sql every round,
    yet the model only called match_summary_report every round → "never tried" is
    always true →
    the "try it yourself first" discipline meant it **never asked for help even
    once** (across 53 rounds of logs, series_totals
    has not a single help-request record).
    """
    if not topic_id:
        return set()
    out: set[str] = set()
    try:
        for r in run_log.load_all():
            if str(r.get("topic_id") or "") != topic_id:
                continue
            for t in (r.get("trajectory") or []):
                # "<deterministic-sql>" is an internal marker, not a real tool name, so it cannot
                # count as "tried"
                if isinstance(t, str) and t and not t.startswith("<"):
                    out.add(t)
    except Exception:
        return set()
    return out


class LLMVoyager:
    """The real Voyager: the model orchestrates the tools itself, reflects itself,
    and stores the lessons in the skill library.

    "Learning to orchestrate" shows up in code as: the lessons in self.memory enter
    the next round's prompt,
    and thereby change its call plan. An empty memory → it most likely only does the
    overall overview;
    with lessons → it should learn to drill down.
    """

    def __init__(self, model: str = "", api_key: str = "", blind: bool = False,
                 hint_level: str = "") -> None:
        # The skill library accumulates across runs — without accumulation there is
        # no progress curve to see
        self.memory: list[str] = skill_store.load()
        self.trajectory: list[dict[str, Any]] = []
        self.last_thought = ""
        self.blind = blind                 # True = no dimension/subject hints while
                                           # planning
        # Scaffold tier (computed by the question generator from mastery, see
        # difficulty.py):
        #   full    gives ★ the declared answer-spec path + critic feeds back the real
        #           candidate paths
        #   partial gives only the focused structural document, no ★, no candidates
        #   none    no focusing, no ★, no candidates (fully open)
        # Empty string = unspecified → treated as full (preserves old behaviour, no
        # fallback)
        self.hint_level = str(hint_level or "")
        # Module-level mirror: lets the **graph assertions** scale with the tier too
        # (those assertions are module-level functions and
        # cannot see self). Assertions are a scaffold as well — they intercept errors
        # like "tool called without parameters"
        # for Voyager, which amounts to an open-book exam. The study companion's
        # grading contract has no graph (`prompt_templates.py:
        # 170-189`, not one of the sixteen requirements mentions it), so withdrawing
        # the scaffold should withdraw them too.
        HINT_STATE["level"] = self.hint_level
        self.failed_dims: list[str] = []   # following fork glm_curator._context():
                                           # failed dimensions feed the next question
        # The named **scoring points** (finer than dimensions): one dimension may
        # carry several scoring points,
        # and naming only the dimension makes the model think it already submitted it.
        self.failed_points: list[str] = []
        self.last_critique = ""
        self.errors: list[str] = []        # tool execution / plan parsing errors; must
                                           # reach the dashboard

        # ---- anti-hallucination: following ActionAgent.render_human_message ----
        # The original repo splices "last round's code / Execution error / Critique"
        # back in as an observation,
        # explicitly writing "No code in the first round" / "No error" / "None" when
        # an item is missing.
        # Do not leave the model room to imagine "what exactly did I do last round" —
        # that room is the entry point for hallucination.
        self.last_round: dict[str, Any] = {
            "calls": [],     # the tool calls actually issued last round
            "errors": [],    # last round's execution errors
            "critique": "",  # last round's self-reflection
        }
        # ---- anti-hallucination: fact grounding ----
        # A value the model extracted must really have appeared in the tool's return,
        # otherwise it is made up.
        self.hallucinations: list[str] = []
        # Skill keys injected into the prompt this round (for skill-effectiveness
        # statistics)
        self._injected_keys: list[str] = []
        # Table structure probed after a SQL error — the "what do you have now" section of
        # the next round's prompt
        self.schema_hints: list[str] = []
        # Every call that was **attempted** (including failures and their raw SQL);
        # used only for feedback, it does not count as evidence
        self.attempted: list[dict[str, Any]] = []
        # VLML self-description (table list + usage tips + samples); shown to the
        # model before it writes SQL
        self.db_info: str = ""

        # ---- asking for help (bypass explanation) ----
        # used_hint: whether VLML0's explanation was used when answering this round.
        # The study companion's isomorphic item is "was this answer helped along by
        # a hint" (mastery_v2.py:109) — only **asking for help** counts; injecting
        # skill-library lessons
        # does not (that is a normal part of solving). Mapping it to "skills were
        # injected" was wrong.
        self.used_hint: bool = False
        self.help_pack: dict[str, Any] | None = None
        self.last_help_decision: dict[str, Any] | None = None
        self.last_feedback: dict[str, Any] | None = None

    # ---- observation feedback: following voyager/agents/action.py:render_human_message
    #       ----
    # ------------------------------------------------------------------
    # Help decision: after being graded wrong, Voyager itself decides whether to go
    # to the bypass (the VLML0 explanation interface)
    # ------------------------------------------------------------------
    def decide_help(self, *, task: Any = None,
                    fb: dict[str, Any] | None = None) -> dict[str, Any]:
        """Decide whether to ask for help after being graded wrong.
        **Not every wrong answer triggers help.**

        The feedback already carries "which tool produces this dimension" (a
        declaration from the knowledge graph),
        so solve it yourself first if you can — call it as suggested, and only say
        something if it still cannot be retrieved.
        Only the three cases below are worth spending an explanation on:
          (a) you already called that tool as hinted and still cannot retrieve it;
          (b) the feedback did not say clearly what to call (the graph has no such
              edge);
          (c) the same gap keeps recurring and the direction you tried yourself has
              been proven ineffective.

        This is the study companion's isomorphic item for "hint first, explanation
        second": the hint (the tool name the graph states) comes first,
        the explanation (the standard solution) comes second; you must not start
        with the explanation.
        """
        fb = fb or getattr(self, "last_feedback", None) or {}
        if not fb or not fb.get("error_type"):
            return {"need_help": False,
                    "reason": "Not graded wrong; no need to ask for help"}

        my_plan = [str(t.get("tool")) for t in self.trajectory if t.get("tool")]
        tried = set(my_plan) | {str(t.get("tool")) for t in self.attempted
                                if t.get("tool")}
        # Tools named in the feedback (from the graph) — check whether they were
        # already tried
        suggested: set[str] = set()
        for m in fb.get("missing_detail") or []:
            suggested |= set(m.get("producers") or [])
        gap = sorted(suggested - tried)

        # "Named but not tried" must be split into two categories; conflating them
        # amounts to blocking help forever:
        #   still_worth   — never tried (neither this process nor in history) →
        #                   worth trying yourself
        #   ineffective   — tried in history but it did not work out that time either
        #                   → trying again will most likely be the same
        # Measured: series_totals ran 24 rounds stuck at 50%; the feedback named
        # query_sql every round,
        # yet the model only called match_summary_report every round → gap is never
        # empty →
        # the "try it yourself first" discipline meant it never asked for help even
        # once.
        #       (History comes from run_log: in loop mode each question is a new
        # process, so looking only at this process's
        # trajectory can never reveal "I tried that before".)
        hist = _tools_used_before(str(getattr(task, "topic_id", "") or ""))
        ineffective = sorted(set(gap) & hist)
        still_worth = sorted(set(gap) - hist)

        # The hard rule comes before the model's judgement: while there are tools
        # that have "never been tried", never ask for help.
        # Measured pitfall: leaving this to the model's free rein, it says "I have
        # already tried all the named tools"
        # (it has tried none), then either asks for help wrongly or gives a
        # self-contradictory reason.
        # "Try it yourself first" is a discipline, not an option — if there is an
        # untried tool, you must try it first.
        if still_worth:
            self.last_help_decision = {
                "need_help": False,
                "reason": (f"There are still named tools that have never been tried "
                           f"({', '.join(still_worth)}); try them yourself first"),
                "untried_suggested": still_worth,
                "ineffective_before": ineffective,
                "by": "rule",
            }
            return self.last_help_decision

        # Reaching here means: the named tools have all been tried this round / in
        # this process,
        # or the "never tried" ones have already been proven ineffective in history —
        # only now is it worth spending one explanation.

        prompt = f"""You have just finished this question. The referee ruled as follows:

Criterion: {fb.get('feedback', '')}
Next step: {fb.get('next_action', '')}
Tools I actually called this time: {', '.join(sorted(tried)) or '(none)'}
Tools named in the feedback that **I have not tried yet**: {', '.join(gap) or '(none)'}
Of those, ones tried in history but which did not work out that time either (trying
again will most likely be the same): {', '.join(ineffective) or '(none)'}
Missing dimensions: {', '.join(fb.get('missing_points') or []) or '(none)'}

Now decide: **whether to ask for help** (call VLML0's explanation interface and look
at how the standard solution is orchestrated).

Decision criteria (this is the key — do not ask for help the moment you are graded
wrong):
- If there are still tools that were "named and that I have never tried" →
  **do not ask for help**; call one as suggested first.
  Anything you can solve yourself you must solve yourself, and each request for help
  is recorded against mastery (scored as "borrowed help", i.e. at a 0.85 factor).
- Only ask for help in these cases:
  (a) I have tried all the named tools and still cannot retrieve the value;
  (b) the feedback never said clearly what to call (no usable tool name);
  (c) the ones I had not tried have historically been proven useless (this question
      has stalled at the same coverage for several rounds);
  (d) the gap in the same dimension has recurred, and the direction I tried myself
      has been proven ineffective.

Output JSON: {{"need_help": true or false, "reason": "one-sentence reason"}}"""

        try:
            out = chat_json(
                [
                    {"role": "system",
                     "content": "You are an analysis agent that tries by itself first "
                                "and only asks for help when that truly fails."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
        except Exception as e:
            return {"need_help": False,
                    "reason": f"Decision call failed: {type(e).__name__}"}

        need = bool(out.get("need_help"))
        reason = str(out.get("reason") or "").strip()
        self.last_help_decision = {"need_help": need, "reason": reason,
                                   "untried_suggested": gap, "by": "model"}
        return self.last_help_decision

    def accept_help(self, pack: dict[str, Any]) -> None:
        """Store the explanation obtained in the learning step — the next round's
        prompt will carry the **how-to-do-it** part.

        The explanation has four sections (following the study companion's
        _solution_structure.py:173):
            question analysis / solution process / answer / transfer
        The "answer" is seen only in **the learning round**; what is fed back into the
        next round's prompt is three procedural things
        (orchestration + which dimensions each tool is responsible for + the solution
        process and metric definition); the values are not fed back.

        Why the answer is given: the goal is to learn, not to pass (in the study
        companion's explanation the answer is visible anyway).
        Why it is withheld when redoing: the values still have to be fetched by the
        model itself with tools, otherwise mastery would be fake.
        """
        self.help_pack = pack
        self.used_hint = True
        study = pack.get("study") or {}
        self._log_skill("help_received", pack.get("topic_id", ""),
                        str(study.get("process") or pack.get("explain")
                            or study.get("transfer") or "")[:160])

    def _render_observation(self) -> str:
        """Assemble last round's calls (including the raw SQL) / errors / self-
        reflection into an observation.

        The original repo's form (action.py:201-236):
            Code from the last round: ...   (first round writes
                                           "No code in the first round")
            Execution error: ...            (no error writes "No error")
            Critique: ...                   (none writes "None")
        Two key points — missing either one makes it spin in place:
          (a) what is fed back is the **full text of the code**, not the function name
              — unable to see what it wrote, it can only rewrite it,
              and measurement shows the same team_name error three rounds in a row;
          (b) **when something is missing you must explicitly write None** — an empty
              string is taken by the model as "last round went fine",
              and then it keeps inventing under that assumption.
        """
        calls = self.last_round.get("calls") or []
        errs = self.last_round.get("errors") or []
        # Critique must take the **current** self.last_critique, not the snapshot in
        # last_round:
        # last_round is snapshotted at the end of the previous run(), and at that
        # moment learn() has not run yet, so critique
        # is always the empty string — which means `_render_observation` always
        # rendered "None".
        # Measured evidence (the prompt dumped by MVE_DEBUG_PROMPT=1):
        #   "Your self-reflection Critique last round: None"
        # That is precisely the direct cause of fb_conversion_analysis running 23
        # rounds stuck at 80% and never once retrieving
        # the Haven first-blood conversion rate — the recipe was written into the
        # feedback but never reached the next round's eyes.
        crit = (getattr(self, "last_critique", "") or "").strip() \
            or (self.last_round.get("critique") or "").strip()

        if calls:
            call_lines = []
            for i, c in enumerate(calls, 1):
                arg = (c.get("args") or {})
                sql = str(arg.get("sql_query") or arg.get("sql") or "")
                shown = f" {sql}" if sql else f" {json.dumps(arg, ensure_ascii=False)[:120]}"
                tail = f"→ failed: {c['error'][:200]}" if c.get("error") else "→ succeeded"
                call_lines.append(f"  {i}. {c.get('tool')}{shown}\n     {tail}")
            calls_txt = "\n" + "\n".join(call_lines)
        else:
            calls_txt = " None (this is the first round)"

        lines = [
            "Calls you actually issued last round (failures included, with the raw "
            "SQL): " + calls_txt,
            "Execution errors last round: "
            + ("; ".join(str(e)[:220] for e in errs) if errs else "None"),
            "Your self-reflection Critique last round: "
            + (crit if crit else "None"),
            "Gap dimensions per the referee's ruling last round: "
            + (", ".join(self.failed_dims) if self.failed_dims else "None"),
            # The dimension name is not enough — map_fb_conv carries two scoring
            # points in this question, on Corrode and Haven;
            # saying only "map_fb_conv is missing" makes the model think it already
            # submitted it (it did submit Corrode).
            # You must **name the scoring points** and state that each needs its own
            # query.
            "Scoring points not covered last round (each needs its own query — do "
            "not merge two into one): "
            + (", ".join(self.failed_points) if getattr(self, "failed_points", None)
               else "None"),
            # Saying only "you were wrong" is useless; you have to say "what you do
            # have" — otherwise it can only guess again
            "For the table you got wrong last round, the columns actually available: "
            + ("; ".join(self.last_round.get("schema_hints") or [])[:900]
               if self.last_round.get("schema_hints") else "None"),
        ]
        # The standard solution you learned (given by the learning step and explained
        # thoroughly; but the **values are not fed back** —
        # the values still have to be fetched by calling tools yourself, they cannot
        # be copied in, and grounding validation will block them).
        pack = getattr(self, "help_pack", None) or {}
        if pack:
            mapping = "; ".join(
                f"{t} → {', '.join(d)}" for t, d in
                (pack.get("dims_by_tool") or {}).items())
            study = pack.get("study") or {}
            lines.append(
                "【The standard solution I learned】(the learning step explained it "
                "thoroughly; only the method is carried over here, fetch the values "
                "yourself)"
                + f"\n  Orchestration: {' → '.join(pack.get('plan') or []) or '(none)'}"
                + f"\n  Dimensions each tool is responsible for: {mapping or '(none)'}"
                + (f"\n  Question analysis: {study['analysis'][:220]}" if study.get("analysis") else "")
                + (f"\n  Solution process and metric definition: {study['process'][:400]}" if study.get("process") else "")
                + (f"\n  Transfer: {study['transfer'][:220]}" if study.get("transfer") else "")
                + "\n  (You saw the answer in the learning record last round; this "
                  "round you must call the tools yourself to fetch the values — "
                  "writing the numbers in directly will be blocked by grounding "
                  "validation.)")
        return "\n".join(lines)

    # Note: there used to be a `_feedback_block()` here, but it was never called
    # anywhere —
    # "feeding the score back" is actually carried by two lines inside
    # `_render_observation`
    # ("Gap dimensions per the referee's ruling last round" + "Scoring points not
    # covered last round").
    # Dead code is more dangerous than no code: it looks like the feedback path is
    # connected when it is not. Deleted.

    # ---- planning ----
    def _memory_block(self, task: Any) -> str:
        """Skill library injection: following SkillManager.retrieve_skills, take only
        retrieval_top_k entries.

        The original repo (skill.py:76-96):
            k = min(self.vectordb._collection.count(), retrieval_top_k)   # =5
            docs_and_scores = self.vectordb.similarity_search_with_score(query, k=k)
        No matter how large the skill library grows, only 5 entries ever enter the
        prompt — "the library grows but the prompt does not" rests on this one line.

        Previously everything was stuffed in: running the same question three rounds
        in a row accumulated a dozen nearly identical lessons,
        and they crowded the prompt together, drowning out the one that was actually
        useful.
        """
        topic = getattr(task, "topic_id", "") if task else ""
        query = getattr(task, "question", "") if task else ""
        top = skill_store.retrieve(topic, query, top_k=skill_store.RETRIEVAL_TOP_K)
        if not top:
            self._injected_keys = []
            return "(no lessons yet)"
        self._injected_keys = [s["key"] for s in top]
        skill_store.mark_used(self._injected_keys)

        mine = [s for s in top if s.get("topic") == topic]
        others = [s for s in top if s.get("topic") != topic]
        out = ""
        if mine:
            out += "Lessons for this question:\n" + "\n".join(
                f"- {s['text']}" + (f" (reused {s['hits']} times)" if s.get("hits") else "")
                + _blueprint_hint(s)
                + _code_block(s)
                for s in mine
            )
        if others:
            out += ("\n\nLessons from other questions (reference only; check whether "
                    "they apply here):\n"
                    + "\n".join(f"- {s['text']}" + _blueprint_hint(s)
                                 + _code_block(s) for s in others))
        # General planning thinking distilled by MC (topic=mc-thinking): seed context,
        # so the model always sees "what to do first, what after" while planning or
        # coding (otherwise the cross-topic threshold filters it out)
        seed = skill_store.seed_thinking()
        if seed:
            out += ("\n\nGeneral planning thinking from MC (domain-agnostic · what to "
                    "do first and what after · follow it preferentially):\n"
                    + "\n".join(f"- {s.get('text', '')}" for s in seed))
        return out or "(no lessons yet)"

    # ---- plan validation: following voyager/agents/action.py:process_ai_message ----
    # The original repo runs AST assertions on the code returned by the model (must be
    # an async function, the parameter must be named bot),
    # and retries 3 times if invalid. Isomorphic here: the tool name must be in the
    # whitelist, the args must be a dict,
    # and the SQL must be SELECT. What a model invents most is "a tool name that does
    # not exist" —
    # measured: it even wrote the internal marker <deterministic-sql> in as a real
    # tool.
    def _allowed_params(self, tool: str) -> set[str] | None:
        """The parameter names the tool really accepts (None = if the signature
        cannot be found, do not validate)."""
        fn = TOOL_REGISTRY.get(tool)
        if fn is None:
            return None
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return None
        allowed = {
            p.name for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          inspect.Parameter.KEYWORD_ONLY)
        }
        # On the MCP side the parameter is sql_query, on the Python side it is named sql
        # — accept both
        if tool in ("query_sql", "execute_custom_sql"):
            allowed |= {"sql_query", "sql"}
        return allowed

    def _validate_plan(self, plan: Any, task: Any = None) -> str:
        if not isinstance(plan, dict):
            return "What came back is not a JSON object"
        calls = plan.get("calls")
        if not isinstance(calls, list) or not calls:
            return "calls is empty or is not an array"
        # The cap follows the **number of scoring points**, not a hardcoded 3.
        # Measured pitfall: with a hardcoded 3, `fb_conversion_analysis` (4 scoring
        # points,
        # one query per map) and `map_rounds_split` (5) **necessarily lose calls** —
        # the model writes 4 and the gate blocks it, all 3 retries are wasted on
        # "too many calls", and it finally degrades to letting 3 through,
        # so the simplest form of all — one query per map — becomes unusable.
        # The cap is "one query per scoring point", not "three chances".
        cap = max(3, len(getattr(task, "rubric", None) or []))
        if len(calls) > cap:
            return (f"calls exceeds {cap} ({len(calls)}) — at most {cap} calls are "
                    f"allowed this round")
        for i, c in enumerate(calls):
            if not isinstance(c, dict):
                return f"calls[{i}] is not an object"
            tool = str(c.get("tool") or "").strip()
            if not tool:
                return f"calls[{i}] is missing tool"
            if tool.startswith("<"):
                return (f"calls[{i}] tool name {tool} is an internal marker, not a "
                        f"real tool")
            if tool not in TOOL_REGISTRY:
                # Saying only "it is not in the list" just makes it guess another
                # name. You must also spell out
                # **what it mistook for a tool name** (measured: the graph gave it a
                # table name and it filled that table name
                # into calls[].tool, only stopping after three collisions).
                why = ""
                g = _load_graph()
                if g is not None:
                    if f"table:{tool}" in g.nodes:
                        why = (f" — {tool} is a **table name**; use query_sql and "
                               f"FROM it in the SQL, it cannot be a tool name")
                    elif f"dim:{tool}" in g.nodes:
                        why = (f" — {tool} is the **dimension name** you are supposed "
                               f"to fill in, not a tool")
                    elif f"tool:{tool}" in g.nodes:
                        why = (f" — {tool} is in the graph but not in this process's "
                               f"tool registry")
                return f"calls[{i}] tool name {tool} is not in the tool catalog{why}"
            args = c.get("args")
            if args is not None and not isinstance(args, dict):
                return f"calls[{i}] args must be an object"
            # Parameter name whitelist: models love inventing parameters (measured:
            # it once wrote dimensions), and they also love writing
            # team_name as team. A TypeError at execution time is already a round
            # too late —
            # reject it here and tell it "which parameters are accepted" at the same
            # time,
            # otherwise it can only guess again.
            if isinstance(args, dict) and args:
                allowed = self._allowed_params(tool)
                if allowed:
                    bad = [k for k in args if k not in allowed]
                    if bad:
                        return (f"calls[{i}] {tool} does not accept the parameter(s) "
                                f"{', '.join(bad)}; "
                                f"it only accepts: {', '.join(sorted(allowed))}")
            # The "at least one of these must be passed" parameter constraint
            # (criteria come from the graph, not hardcoded).
            # Note it must sit **outside** the `if` that tests args for emptiness —
            # passing not a single parameter is exactly
            # the case that should be blocked; inside that branch it would never be
            # reached.
            missing_param = _check_tool_params(
                tool, args if isinstance(args, dict) else {})
            if missing_param:
                return f"calls[{i}] {missing_param}"
            if TOOL_REGISTRY.get(tool) is execute_custom_sql:
                sql = str((args or {}).get("sql_query")
                          or (args or {}).get("sql") or "").strip()
                if not sql:
                    return f"calls[{i}] query_sql is missing sql_query"
                if not sql.lower().lstrip().startswith("select"):
                    return f"calls[{i}] query_sql only allows SELECT"
                # **Structural** assertions on the metric definition (following
                # process_ai_message's AST assertions):
                # in segmented computation, which layer the filter sits in is itself
                # the metric definition, so a wrong layer must be blocked here,
                # rather than waiting for the referee to say "the value is wrong" —
                # by then a whole round has been wasted.
                # State **all** the problems together, with the correct skeleton:
                # stating one at a time makes it oscillate
                # between two errors, and once the three retries are used the whole
                # round is wasted.
                bad, recipe = _check_sql_layering(sql, task)
                if not bad:
                    # If the layering is fine, then check "grouping order" — the two
                    # are independent; do not let them block each other
                    bad, recipe = _check_group_order(sql, task)
                if bad:
                    msg = f"calls[{i}] metric-definition error: " + "; ".join(bad)
                    if recipe:
                        msg += (f". Correct skeleton (follow it, replacing '?' with a "
                                f"real value): {recipe}")
                    return msg
        return ""

    def _plan(self, task: Any, retries: int = 3, call_limit: int = 3) -> dict[str, Any]:
        mem = self._memory_block(task)
        # Knowledge graph subgraph — where the original version splices control_primitives in
        # render_system_message.
        # Placed after the tool catalog: first know "which tools exist", then know
        # "how this metric is computed with them".
        # The graph block follows the **scaffold tier** (the study companion: the graph
        # is in the optional section and can be dropped entirely).
        # Previously no hint was passed here, so no matter how far practice had
        # progressed it was given at the env default value —
        # a question with 100% coverage still received "how to compute it + which
        # column to take + the full skeleton".
        gctx = graph_context(task, hint=str(getattr(self, "hint_level", "") or ""))
        gctx_block = f"\n{gctx}\n" if gctx else ""
        if self.blind:
            # Blind test: only the natural-language scoring points are given;
            # dimension / subject are not leaked.
            # This measures "can the model think of which dimension to break the
            # problem down to on its own", rather than copying a hint.
            points = "\n".join(f"- {p.point}" for p in task.rubric)
        else:
            points = "\n".join(
                f"- {p.point}｜the dimension must be written as `{p.dimension}`｜"
                f"the subject must be written as "
                f"{json.dumps(p.subject, ensure_ascii=False)}｜weight {p.weight}"
                for p in task.rubric
            )
        prompt = f"""You are a data analysis agent. The user's question is:

{task.question}

Scoring points (each must be covered by data, otherwise points are deducted):
{points}

{TOOL_CATALOG}{gctx_block}
VLML's own usage tips:
{self.db_info}

【Tools first · query_sql is only the escape hatch】
VLML's 10 report tools already encapsulate the metric definitions. Whatever a report
tool can retrieve, use a report tool;
do not hand-write SQL to compute it by brute force.

Three classes of error that measurement shows over and over — you must avoid them
(they are all the same mistake:
writing "the semantics you want to express" straight in as an identifier in the
database):
  1. Treating a dimension name as a column name — pistol_win_rate,
     pattern_rounds, pattern_confidence
     are **the dimensions you are supposed to fill in**, not columns in a table.
  2. Treating a tool name as a table name — pattern_detection_report is a tool; it
     cannot be written after FROM.
  3. Guessing column names — some tables have team_name and some do not; look it up
     in information_schema.columns before writing.

{LOCAL_SQL_NOTES}

Lessons you accumulated in the past (only the ones most relevant to this question):
{mem}
{self._render_observation()}
【Call count this round】You must produce exactly {call_limit} calls — one query per
scoring point.
One short means one scoring point can never obtain a fact (the execution layer only
runs the first {call_limit} calls; the extras are discarded).
{PLAN_SCHEMA}"""

        # Debug forensics: when MVE_DEBUG_PROMPT=1 the full prompt is dumped to /tmp.
        # "Did the graph actually make it into the prompt" can only be confirmed by
        # reading the text, not guessed — measurement has slipped up right here:
        # the code injected it, but the model's behaviour did not change; it must be
        # seen to be believed.
        if os.environ.get("MVE_DEBUG_PROMPT"):
            try:
                with open("/tmp/mve_plan_prompt.txt", "w", encoding="utf-8") as f:
                    f.write(prompt)
            except OSError:
                pass

        # Following process_ai_message's retry=3: if parsing is invalid, come back
        # once more carrying the error,
        # rather than silently degrading into "no tools called → no evidence".
        last_err = ""
        last_plan: dict[str, Any] = {}
        for attempt in range(1, retries + 1):
            try:
                plan = chat_json([
                    {"role": "system",
                     "content": "You are a rigorous data analysis agent. Output JSON "
                                "only."},
                    {"role": "user", "content": prompt},
                ])
            except Exception as e:
                last_err = f"Call failed: {type(e).__name__}: {e}"
                self.errors.append(
                    f"Planning failed (attempt {attempt}): {last_err}"[:200])
                continue
            err = self._validate_plan(plan, task)
            last_plan = plan if isinstance(plan, dict) else {}
            if not err:
                self.last_thought = str(plan.get("thought", "")) if isinstance(plan, dict) else ""
                _apply_auto_fixes(plan, task, self.errors)
                return plan
            last_err = err
            self.errors.append(
                f"Plan validation failed (attempt {attempt}/{retries}): {err}")
            # Feed the validation result back and let the model fix it — saying only
            # "try again" just makes it repeat the same mistake
            prompt += (
                f"\n\n【The previous version was rejected】{err}."
                f"Tool names must be taken verbatim from the tool catalog above; "
                f"do not invent them. Please output again."
            )

        # Retries exhausted and still invalid: fix what can be fixed rather than
        # wasting the whole round.
        # Wasting the round = not a single tool called = judged "no evidence"
        # (dont_know 0%).
        # That punishes formatting rather than capability — and it wipes out the
        # progress made in the previous round,
        # which is one of the sources of coverage swinging wildly. So here we always
        # let it through in degraded form:
        # drop only the calls whose tool names are invalid, run the rest as usual (SQL
        # with a wrong metric definition will retrieve wrong values,
        # but at least some evidence remains, and the next round's critique can point
        # out "the value does not match").
        calls = last_plan.get("calls")
        if isinstance(calls, list):
            kept = [
                c for c in calls[:call_limit]
                if isinstance(c, dict) and str(c.get("tool", "")) in TOOL_REGISTRY
            ]
            if kept:
                last_plan["calls"] = kept
                _apply_auto_fixes(last_plan, task, self.errors)
                self.errors.append(
                    f"Plan validation did not converge after {retries} retries; "
                    f"letting {len(kept)} valid calls through in degraded form "
                    f"(dropped {len(calls[:call_limit]) - len(kept)}): {last_err}"[:220]
                )
                self.last_thought = str(last_plan.get("thought", ""))
                return last_plan

        self.errors.append(
            f"Planning still invalid after {retries} retries; giving up this round: "
            f"{last_err}")
        self.last_thought = ""
        return {}

    # ---- execution ----
    # ---- table structure probing: the "Inventory" after a SQL error ----
    async def _schema_probe(self, sql: str, error: str = "") -> str:
        """On a SQL error, look up which columns the tables involved in the failing
        statement **actually have**, and **what to use instead**.

        Why this step is mandatory: measurement showed three rounds stuck on the same
        error — the model kept writing team_name,
        while the rounds table has no such column at all. Feeding back only
        "Referenced column team_name not found" is useless: it knows it was wrong, but
        it does not know **what is right**,
        so it can only guess again, and most likely make the same mistake.

        Isomorphic to the original repo's ActionAgent.render_human_message: it gives
        not just "Execution error",
        but also the Inventory / Nearby blocks (what you have available).
        Here, "what you have available" is the column names that genuinely exist in
        information_schema.

        Listing the column names alone is still not enough — measurement shows it
        wrote team_name anyway even when given the column names,
        because what it wants is the semantics ("group by team"), not a list of column
        names. So the **alternative** must also be spelled out:
        when team_name is missing, tell it that winning_team_name / losing_team_name
        exist.
        """
        tables = re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*)", sql or "", re.I)
        tables = [t.lower() for t in tables if t.lower() != "select"]
        if not tables:
            return ""
        tbls = sorted(set(tables))[:3]
        in_list = ",".join(f"'{t}'" for t in tbls)
        q = (f"SELECT table_name, column_name FROM information_schema.columns "
             f"WHERE table_name IN ({in_list}) ORDER BY table_name, ordinal_position")
        try:
            res = await execute_custom_sql(q)
        except Exception:
            return ""
        rows = (res or {}).get("rows") if isinstance(res, dict) else None
        if not rows:
            # No columns found = this table does not exist at all (measurement: the
            # model wrote a **tool name** into
            # FROM as a table name: FROM pattern_detection_report). Following VLML's
            # get_database_info approach,
            # what is needed here is the **list of what is available**, rather than
            # just saying "you were wrong".
            return await self._table_menu(tables)
        by: dict[str, list[str]] = {}
        for t, c in rows:
            by.setdefault(str(t), []).append(str(c))

        out = "; ".join(f"{t} has columns {', '.join(cs[:24])}" for t, cs in by.items())

        # Spell out "which column is missing → which column to use instead". Even
        # after the list of column names is given, the model still gets it wrong,
        # because what it wants is semantics (group by team), not a list of column
        # names.
        m = re.search(r'Referenced column "([^"]+)" not found', error or "")
        if m:
            missing = m.group(1)
            core = [p for p in missing.split("_") if p not in
                    {"name", "id", "key", "code", "type"} and len(p) > 2]
            alts: list[str] = []
            for cs in by.values():
                for c in cs:
                    if c == missing:
                        continue
                    if any(p in c for p in core) and c not in alts:
                        alts.append(c)
            if alts:
                out += (f"\nNote: these tables have **no** {missing} column; "
                        f"to express the same thing, use {', '.join(alts[:8])}. "
                        f"Do not write {missing} again.")
        return out

    async def _table_menu(self, tried: list[str]) -> str:
        """Following VLML get_database_info: give the list of available tables rather
        than just saying "you were wrong"."""
        try:
            res = await execute_custom_sql(
                "SELECT table_name FROM information_schema.columns GROUP BY table_name"
            )
        except Exception:
            return ""
        names = sorted({str(r[0]) for r in (res.get("rows") or []) if r})
        if not names:
            return ""
        return (
            f"The database has **no** {', '.join(tried) or 'table by that name'} — "
            f"a tool name is not a table name and cannot be written after FROM. "
            f"The only available tables are: {', '.join(names)}"
        )

    def _call_limit(self, task: Any) -> int:
        """At most how many calls to run this round — it must be determined by the
        **number of scoring points** and must not be hardcoded.

        Direct consequences of hardcoding 3 (measured evidence, 88 rounds of logs):
            map_rounds_split      5 scoring points → at most 3 covered → stuck at 60%
            fb_conversion_analysis 4 scoring points → at most 3 covered → stuck at 80%
        Whereas a tool-path question can bring back several dimensions in one call, so
        3 is enough → 100% across the board.
        So "SQL-style questions are never learned" was never the model failing to
        learn; it was the **execution layer truncating** —
        however accurate the feedback, the fourth query never gets a chance to go out.

        The cap of 8 is only a guard against blowups (running dozens of SQL statements
        at once would overwhelm the referee); it is not a capability ceiling.
        """
        n = len(getattr(task, "rubric", None) or [])
        return max(3, min(8, n)) if n else 3

    async def _execute(self, plan: dict[str, Any],
                       limit: int = 3) -> list[dict[str, Any]]:
        obs: list[dict[str, Any]] = []
        for call in (plan.get("calls") or [])[:limit]:
            tool = str(call.get("tool", ""))
            args = dict(call.get("args") or {})
            fn = TOOL_REGISTRY.get(tool)
            if fn is None:
                obs.append({"tool": tool, "error": f"Unknown tool {tool}"})
                self._note_attempt(tool, args, f"Unknown tool {tool}")
                continue
            try:
                # On the MCP side query_sql's parameter is sql_query; the model may
                # also write it as sql. Accept both.
                if fn is execute_custom_sql:
                    sql = str(args.pop("sql_query", "") or args.pop("sql", "") or
                              args.pop("sql_query", ""))
                    if not sql.lower().lstrip().startswith("select"):
                        obs.append({"tool": tool, "error": "Only SELECT is allowed"})
                        self.errors.append(f"{tool}: Only SELECT is allowed")
                        self._note_attempt(tool, {"sql_query": sql},
                                           "Only SELECT is allowed")
                        continue
                    res = await fn(sql)
                    args = {"sql_query": sql}
                    if isinstance(res, dict) and res.get("error"):
                        # The SQL blew up → while we are here, look up this table's real
                        # columns and feed them to the next round
                        hint = await self._schema_probe(sql, str(res.get("error", "")))
                        if hint:
                            self.schema_hints.append(hint)
                elif tool in MAP_NAME_BUGGY_TOOLS:
                    res = await _guarded_report(fn, args)
                else:
                    res = await fn(**args)
                if isinstance(res, dict) and res.get("error"):
                    self.errors.append(f"{tool}: {res['error']}"[:200])
                    obs.append({"tool": tool, "args": args, "error": res["error"]})
                    self._note_attempt(tool, args, str(res["error"]))
                    continue
                obs.append({"tool": tool, "args": args, "result": res})
                # The diary must carry the **raw return**: the fork's
                # _emit_trajectory records
                # `response` (drift_voyager.py:329-345), and the critic reads it too.
                # Previously only tool+args were recorded and result was dropped —
                # so afterwards it was impossible to answer
                # "what exactly did the tool return at the time"; all you could see
                # were the facts the LLM had transcribed.
                # The diary must carry the **raw return**: the fork's
                # _emit_trajectory records
                # `response` (drift_voyager.py:329-345), and the critic reads it too.
                # Previously only tool+args were recorded and result was dropped —
                # so afterwards it was impossible to answer
                # "what exactly did the tool return at the time"; only the
                # LLM-transcribed facts remained.
                # Store the raw object (untruncated): trajectory only travels in
                # memory, and when persisted
                # run_mve takes only the tool name (:311), so the log will not blow
                # up.
                self.trajectory.append({"tool": tool, "args": args, "result": res})
                self._note_attempt(tool, args, "")
            except TypeError as e:  # wrong parameter name/count — report it so the
                                     # model can fix it next round
                obs.append({"tool": tool, "error": f"Argument error {e}"})
                self.errors.append(f"{tool}: Argument error {e}")
                self._note_attempt(tool, args, f"Argument error {e}")
            except Exception as e:  # a crashing tool must not take down the round
                obs.append({"tool": tool, "error": f"{type(e).__name__}: {e}"})
                self.errors.append(f"{tool}: {type(e).__name__}: {e}")
                self._note_attempt(tool, args, f"{type(e).__name__}: {e}")
        if not obs:
            # The plan contained no calls at all — this must be stated; it must not
            # silently become "no evidence"
            self.errors.append(
                "No tool calls were produced this round (plan.calls was empty or "
                "failed to parse)")
        return obs

    def _note_attempt(self, tool: str, args: dict[str, Any], error: str) -> None:
        """Record every call that was **attempted** (including failures and its raw
        SQL).

        trajectory only records successful calls — so last round's feedback contained
        just the tool name and no SQL.
        The model cannot see what it wrote last round, so it regenerates it and then
        repeats the same mistake
        (measured: three rounds of the same team_name error).
        The original repo feeds back the **full text** of "Code from the last round",
        not the function name.
        """
        self.attempted.append({
            "tool": tool,
            "args": {k: v for k, v in (args or {}).items() if k in
                     {"sql_query", "sql", "series_id", "team_name", "team", "map_name"}},
            "error": (error or "")[:300],
        })

    # ---- fact extraction ----
    def _extract(self, task: Any, obs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        points = "\n".join(
            f"- {p.point}｜dimension=`{p.dimension}`｜"
            f"subject={json.dumps(p.subject, ensure_ascii=False)}"
            for p in task.rubric
        )
        payload = json.dumps(
            [{"tool": o["tool"], "args": o.get("args"),
              "result": o.get("result"), "error": o.get("error")} for o in obs],
            ensure_ascii=False,
            default=str,   # VLML's returns contain datetime, which fails to serialise
                           # if left unhandled
        )[:12000]
        prompt = f"""Below is the real data returned by the tools:

{payload}

Scoring points:
{points}

Please organise the data into an array of facts. Each fact looks like:
{{"subject": {{"series":"...","team":"...","map":"..."}},
  "dimension": "the dimension name; it must match the dimension in the scoring point",
  "value": the number,
  "unit": "percent|count|ratio|raw",
  "base": the denominator (an integer; if the data is {{"num":x,"denom":y}} fill in y;
  if there is none, null)}}

Output JSON only: {{"facts":[...], "narrative":"one-sentence conclusion"}}
Do not invent numbers that are not in the data.

**If it could not be retrieved, do not give that fact at all**; do not fill in 0 or
null as a placeholder — a placeholder will be treated as the real answer
and compared against the referee's value (measured: the model filled in 0, it compared
as "value mismatch 0≠59", and the points were simply lost).

Note: in the tool returns, **besides key_metrics, look at the top-level fields too**
(scope / summary / report_type etc.).
Metadata such as sample size and confidence is written in scope — measurement shows
the model only digs through key_metrics,
so when it cannot get scope.rounds it fills in a 0 and calls it a day."""
        try:
            out = chat_json([
                {"role": "system",
                 "content": "You are a rigorous data organiser. Output JSON only and "
                            "never invent numbers."},
                {"role": "user", "content": prompt},
            ])
        except Exception as e:
            self.errors.append(f"Extraction failed: {type(e).__name__}: {e}"[:200])
            out = {}
        facts = []
        for f in (out.get("facts") or []):
            if not isinstance(f, dict):
                continue
            if "subject" not in f or "dimension" not in f:
                continue
            facts.append(make_fact(
                subject=f.get("subject") or {},
                dimension=str(f.get("dimension")),
                value=_coerce_value(f.get("value")),
                unit=str(f.get("unit") or "raw"),
                base=f.get("base"),
                source={"tool": "llm_extract", "args": {}},
            ))
        self._narrative_text = str(out.get("narrative", ""))
        return facts

    # ---- fact grounding: the last gate against hallucination ----
    def _ground_facts(
        self, facts: list[dict[str, Any]], obs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Drop values that "never appeared in the tool returns at all".

        This is the hardest move in the anti-hallucination set, and it corresponds to
        the validation side of the original repo's critic
        (critic.py:39-42: if onError appears in the event it is judged a failure
        outright; the model is not allowed to talk its way around it).
        Writing "do not invent numbers" in the prompt is a soft constraint —
        measurement shows that when every tool fails the model
        can still write out a whole set of plausible-looking numbers, precisely
        because a prompt cannot stop it.

        Criterion: the value must be findable in the tool-return text (approximately
        equal),
        or computable from two numbers in the returns (e.g. 13/21→61.9, in which case
        base is in the data too).
        """
        if not facts:
            return facts
        blob = json.dumps(
            [o.get("result") for o in obs if o.get("result") is not None],
            ensure_ascii=False, default=str,
        )
        nums: list[float] = []
        for m in re.finditer(r"-?\d+(?:\.\d+)?", blob):
            try:
                nums.append(float(m.group()))
            except ValueError:
                continue
        if not nums:
            # No number came back at all — then every numeric fact is untrustworthy
            return [f for f in facts if not isinstance(f.get("value"), (int, float))]

        def near(x: float) -> bool:
            return any(abs(x - n) <= max(0.05, abs(n) * 0.005) for n in nums)

        # Fallback check: the model often computes the correct ratio but leaves base
        # empty (measured: eco 6/14→42.86
        # was dropped as invented, which is a **false positive**). So we must allow
        # "any two returned numbers divided by each other" —
        # this gate exists to block numbers with **no source at all**; exact
        # verification is the referee's job, not this gate's.
        pool = sorted(set(nums))[:200]

        def derivable(x: float) -> bool:
            for a in pool:
                for b in pool:
                    if b == 0:
                        continue
                    if abs(a / b * 100 - x) <= 0.15:   # percentage: 6/14→42.86
                        return True
                    if abs(a / b - x) <= 0.005:        # ratio: 13/21→0.619
                        return True
            return False

        kept: list[dict[str, Any]] = []
        for f in facts:
            v = f.get("value")
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                kept.append(f)          # non-numeric (e.g. a text enum) is not subject
                                        # to numeric grounding
                continue
            v = float(v)
            if near(v) or near(round(v, 4)):
                kept.append(f)
                continue
            base = f.get("base")
            if isinstance(base, (int, float)) and base:
                # Allow derivation from the denominator: for percent, v*base/100; for
                # ratio, v*base
                if near(v * float(base)) or near(v * float(base) / 100.0):
                    kept.append(f)
                    continue
            # Do not rush to condemn when base is missing — first check whether it
            # can be obtained by dividing two numbers from the returns
            if derivable(v):
                kept.append(f)
                continue
            self.hallucinations.append(
                f"{f.get('dimension')}={v}: this number is not in the tool returns; "
                f"dropped"
            )
        return kept

    # ---- main entry point (same signature as ScriptedVoyager) ----
    @staticmethod
    def _is_tool_path_task(task: Any) -> bool:
        """Is this question a "tool-path" question (values are obtained by digging along
        a path rather than by writing SQL yourself).

        Why the split: measurement shows the code path is a root cure for **tool-type**
        questions (the wrong-key disease is gone),
        but a regression for **SQL-type** ones (corrode: original path 100%, code path
        0% — the model gives up writing
        gaps-and-islands, switches to match_rounds_report instead, and hands in 0 when
        it cannot retrieve anything).

        This is not a tuning problem, it is the **isomorphism boundary**: the original
        Voyager's JS is "calling an API on the
        bot", which corresponds to calling an MCP tool here; whereas SQL is a
        **parameter string** of `query_sql`,
        corresponding to the original `bot.chat("...")` string parameter — the
        original never learns a chat string as a program, and the skill library has
        no "chat string" category of skill either.
        So: **tool-path questions go through code, SQL questions go through the
        original plan path** (that path has the graph
        skeleton and the `_check_sql_layering` layering assertions, which are exactly
        the guardrails SQL questions need).
        """
        specs = [getattr(p, "answer_spec", None)
                 for p in (getattr(task, "rubric", None) or [])]
        specs = [s for s in specs if s is not None]
        if not specs:
            return False
        return all(bool(getattr(s, "tool", "")) and not getattr(s, "sql", "")
                   for s in specs)

    # ---- replay the programs you accumulated yourself (the "skill is exec'd" on the
    #       plan path) ----
    async def _replay_saved(self, task: Any) -> tuple[list[dict], dict] | None:
        """Replay the call sequence stored in the skill library; **adopt it only if all
        dimensions were retrieved**.

        Why the plan path must have this too (it did not before, and that was the root
        cause of the learning curve never rising):
        6 of the 8 questions are SQL-type and take the plan path, and that path
        **never produces
        `program_code`** — so "it worked" contributes nothing to the skill library,
        and when the knowledge graph is withdrawn and you retest, you
        have to rewrite the SQL from scratch and then make the same
        metric-definition mistake again (measured: corrode_collapse
        finished practice at 100% and still scored 0% on the retest, and what was
        missing was still the same losing_team_name placed in the wrong layer).

        The original Voyager's rule is "write it yourself → get it running → store the
        program → run it directly next time",
        with **whether it ran** as the criterion, not what this thing looks like. The
        comment at `voyager.py:1631-1634` previously excluded SQL questions on the
        grounds that "the original never learns a chat string as a program" —
        that was mistaking surface form for the isomorphism boundary: the original's
        `bot.chat()` sends a natural-language instruction,
        whereas MVE's SQL is **structured retrieval code**, the same kind of thing as
        "digging along a path"
        (both are "how to get the data out"), just written differently. The price of
        excluding it is:
        sixty percent of questions can never be learned.

        Only the `practice` channel is replayed (source code you got running
        yourself), and **if the dimensions are not all present it is not adopted** —
        reusing a partially correct program amounts to freezing a known defect into
        default behaviour.
        """
        try:
            import skill_store
        except Exception:                                    # pragma: no cover
            return None
        tid = str(getattr(task, "topic_id", "") or "")
        dims = [str(getattr(p, "dimension", "") or "")
                for p in (getattr(task, "rubric", None) or [])]
        # The current question's entities (values passed at the call site) — when
        # reusing the same orchestration across questions, they override the call
        # arguments.
        # Same convention as `_cur_subject` on the code path `_run_with_code`:
        # the skill function takes parameters and the values are passed at the call
        # site (isomorphic to the original); it does not reuse the entities hardcoded
        # when the skill was written.
        cur: dict[str, Any] = {}
        for _p in getattr(task, "rubric", []) or []:
            for _k, _v in ((getattr(_p, "subject", None) or {}) or {}).items():
                if isinstance(_v, (list, tuple)):
                    _v = _v[0] if _v else None
                if _v not in (None, ""):
                    cur.setdefault(str(_k), _v)
        # Use retrieve()'s ordering for the candidate set (same-topic first + lexical
        # relevance + effectiveness weighting):
        # scanning the whole library would also pull in irrelevant cross-topic skills
        # for replay, which is expensive and easily misleading.
        # retrieve already implements two thresholds: "same topic first" and "at most 2
        # cross-topic, with a relevance floor".
        try:
            candidates = skill_store.retrieve(
                tid, str(getattr(task, "question", "") or ""),
                top_k=skill_store.RETRIEVAL_TOP_K)
        except Exception:                                    # pragma: no cover
            candidates = []
        for s in candidates:
            if str(s.get("source") or "") != "practice":
                continue
            calls = list((s.get("blueprint") or {}).get("calls") or [])
            if not calls:
                continue
            # Cross-topic reuse: override the call arguments with the current question's
            # entities — only overriding keys **already present** in the call,
            # never adding new ones out of thin air (otherwise execute_custom_sql
            # would receive a superfluous series=… argument).
            # This is exactly the transfer form of "the same orchestration, different
            # series/team/map",
            # and also the original's "stop learning → transfer the skill library to a
            # new task" landing on the plan path.
            if str(s.get("topic") or "") != tid and cur:
                calls = [dict(c, args=dict((c.get("args") or {}))) for c in calls]
                for c in calls:
                    for k, v in cur.items():
                        if k in c["args"]:
                            c["args"][k] = v
            obs = await self._execute({"calls": calls}, limit=len(calls))
            if not [o for o in obs if o.get("result") is not None]:
                continue
            facts = self._extract(task, obs)
            facts = self._ground_facts(facts, obs)
            got = {str(f.get("dimension")) for f in facts}
            if dims and not all(d in got for d in dims):
                continue                    # not fully retrieved → does not count; let
                                            # the model rewrite it
            return facts, s
        return None

    async def _run_with_code(self, task: Any) -> dict[str, Any]:
        """Code-based evidence retrieval: the model writes code → the interpreter
        executes it → **the values are produced by the code**.

        Following the original `action.py`'s complete chain (`render_system_message` →
        the model emits
        ```js → `process_ai_message`'s AST assertions → interpreter execution →
        observation fed back verbatim`),
        the MVE side is implemented by `action_code.py`. The fundamental difference
        from the plan-JSON path:

            plan JSON path: the model calls tools → **the model reads the returns and
                            transcribes them into facts** → judgement reads the facts
            code path     : the model writes code → **the interpreter extracts values
                            along the path** → judgement reads the raw values

        The study companion's very same principle
        (`deterministic_evaluators.py:158-167`): expected and
        tolerance are read only from the server-side private answer_spec, **never
        from the learner's answer**.
        Transcribing means making the model a reader all over again — which is
        precisely the step the study companion explicitly excludes.
        """
        import action_code

        dims = sorted({str(p.dimension) for p in getattr(task, "rubric", [])
                       if getattr(p, "dimension", "")})
        # The current question's entities (series / team / map) — injected when
        # reusing a skill,
        # rather than reusing the set hardcoded when the skill was written (original:
        # values are passed at the call site).
        self._cur_subject = {}
        for _p in getattr(task, "rubric", []) or []:
            for _k, _v in ((getattr(_p, "subject", None) or {}) or {}).items():
                if isinstance(_v, (list, tuple)):
                    _v = _v[0] if _v else None
                if _v not in (None, ""):
                    self._cur_subject.setdefault(str(_k), _v)
        # The solution source code learned in the skill library must go into the system
        # message (following the original's `+ skills`) —
        # otherwise not a single line of what was learned on the bypass is visible
        # while writing code.
        skills_text = ""
        top: list[dict[str, Any]] = []
        try:
            top = skill_store.retrieve(getattr(task, "topic_id", ""),
                                       getattr(task, "question", ""),
                                       top_k=skill_store.RETRIEVAL_TOP_K)
            # The injection count and mark_used must be done here: previously only the
            # plan path
            # (`_memory_block`) ever set `_injected_keys`, so on the code path it was
            # always empty,
            # and the skills' hits / ok / effectiveness weighting were all dead — the
            # dashboard's "injected 0/5 this round"
            # did not mean nothing was injected, it meant nothing was recorded.
            self._injected_keys = [s["key"] for s in top]
            skill_store.mark_used(self._injected_keys)
            blocks = [f"- {s.get('text','')}{_code_block(s)}" for s in top]
            if blocks:
                # You must state "these paths have been verified correct, copy them
                # preferentially": without that, the model will
                # pick some other similar-looking path from the tool's structure
                # documentation (measured: it picked
                # `team_comparison.Cloud9...` while the true value is in
                # `key_metrics.team...`).
                skills_text = ("\n\nSolutions you have learned (**these paths have "
                               "been verified correct by the referee; copy them "
                               "preferentially and do not pick another one from the "
                               "return structures above**):\n"
                               + "\n".join(blocks))
            # MC seed thinking: injected into the coding stage together as fallback
            # context for "what to do first and what after"
            seed = skill_store.seed_thinking()
            if seed:
                skills_text += ("\n\nGeneral planning thinking from MC (domain-agnostic "
                               "· what to do first and what after · "
                               "follow it preferentially):\n"
                               + "\n".join(f"- {s.get('text', '')}" for s in seed))
        except Exception:
            skills_text = ""
            self._injected_keys = []

        # ---- First run the programs in the library; if one runs, adopt it directly
        #      (the original "skill is exec'd" itself) ----
        #
        # Previously skills were only **spliced into the prompt for a human to
        # read**, never executed — which is precisely
        # the root cause of "the skill library has content but coverage does not
        # rise": the model rewrites it every round, and every rewrite is
        # a fresh opportunity to misread a key. The original Voyager simply exec's
        # `program_code` after taking it out.
        # Only the `practice` channel is reused (source code you got running
        # yourself); the bypass channel stores pseudocode
        # comments (`bypass_learn._solution_code`), which have no main function and
        # cannot run.
        reused = await self._reuse_skill(top, dims)
        if reused is not None:
            out, skill = reused
            self.trajectory = [{"tool": str(t), "error": ""}
                               for t in (out.get("tools") or [])]
            self.attempted = [dict(c) for c in (out.get("calls") or [])]
            facts = self._facts_from_values(task, out.get("values") or {},
                                            out.get("tools") or [])
            self.last_round = {"calls": self.attempted, "errors": [],
                               "critique": "", "schema_hints": [],
                               "code": str(out.get("program_code") or "")}
            return {
                "facts": facts,
                "narrative": {"text": "", "comparable": False},
                "skill_used": f"reuse({skill.get('name','')})",
                "reused_skill": skill.get("name", ""),
                "program_code": str(out.get("program_code") or ""),
                "program_name": str((skill.get("blueprint") or {})
                                    .get("program_name") or ""),
                "trajectory": self.trajectory, "errors": [],
                "hallucinations": [], "attempted": self.attempted,
            }

        prev = getattr(self, "last_round", None) or {}
        out = await action_code.collect(
            task, dims=dims,
            code=str(prev.get("code") or ""),
            error="; ".join(str(e) for e in (prev.get("errors") or []))[:400],
            critique=str(prev.get("critique") or ""),
            missing=list(getattr(self, "failed_dims", None) or []),
            skills_text=skills_text,
            hint=str(getattr(self, "hint_level", "") or ""),
        )

        if out.get("error"):
            self.errors.append(str(out["error"]))
            self.last_round = {"calls": [], "errors": self.errors,
                               "critique": "", "schema_hints": [],
                               "code": str(out.get("program_code") or "")}
            return {"facts": [],
                    "narrative": {"text": "", "comparable": False},
                    "skill_used": "action_code(failed)",
                    "trajectory": [], "errors": self.errors,
                    "hallucinations": [], "attempted": []}

        # The shape of trajectory must be **identical** to the plan-JSON path's
        # (elements are dicts carrying "tool"):
        # the main loop reads it as `t["tool"]`. If the code path stuffed plain strings
        # in here,
        # run_mve.py:179's `t.get("error")` would raise AttributeError and take down
        # the whole round.
        self.trajectory = [{"tool": str(t), "error": ""}
                           for t in (out.get("tools") or [])]
        self.attempted = [dict(c) for c in (out.get("calls") or [])]
        facts = self._facts_from_values(task, out.get("values") or {},
                                        out.get("tools") or [])
        self.last_round = {
            "calls": self.attempted, "errors": list(self.errors),
            "critique": self.last_critique, "schema_hints": [],
            "code": str(out.get("program_code") or ""),
        }
        return {
            "facts": facts,
            "narrative": {"text": "", "comparable": False},
            "skill_used": f"action_code({out.get('program_name') or ''})",
            # This source code you wrote yourself must be carried out: once it runs,
            # the main loop stores it in the skill library —
            # the original is "write it yourself → get it running → store
            # program_code", not storing someone else's answer.
            "program_code": str(out.get("program_code") or ""),
            "program_name": str(out.get("program_name") or ""),
            # Externally we still report the list of tool names (for the dashboard and
            # logs); only the internal self.trajectory has the dict shape
            "trajectory": self.trajectory,
            "errors": list(self.errors),
            "hallucinations": [],
            "attempted": self.attempted,
        }

    async def _reuse_skill(self, top: list[dict[str, Any]],
                           dims: list[str]) -> tuple[dict[str, Any], dict] | None:
        """Try to directly run a program from the skill library; if it runs and the
        dimensions are complete, return (result, skill).

        This is the isomorphic counterpart of the original
        `SkillManager.retrieve_skills` → `exec` section.
        The criterion is "are the dimensions complete", not "was there an exception":
        when a program runs to completion but takes the wrong path it
        raises no exception (dig returns None when it cannot find the value), so
        looking only at exceptions would accept the wrong ones as right.

        The injected entity values come from **the current question**
        (`self._cur_subject`), not from the set hardcoded when the skill was
        written — isomorphic to the original: the skill function takes parameters and
        the values are passed at the call site.
        """
        import action_code
        for s in top or []:
            if str(s.get("source") or "") != "practice":
                continue
            code = str(s.get("code") or "")
            name = str((s.get("blueprint") or {}).get("program_name") or "")
            if not code or not name:
                continue
            # Stale skills are not reused: values are hardcoded inside them, and those values
            # no longer exist in the current database.
            # Without blocking them it would "run successfully" (0 rows is not an
            # error) and merely hand in an empty result —
            # measured: 16 reuses across 26 exams, and coverage still fell back to
            # zero; this batch of skills did it.
            dead = action_code.stale_literals(code)
            if dead:
                continue
            try:
                out = await action_code.execute(code, name, subject=self._cur_subject)
            except Exception:
                continue
            vals = out.get("values") or {}
            if dims and all(not action_code.is_empty(vals.get(d)) for d in dims):
                out["program_code"] = code
                out["program_name"] = name
                return out, s
        return None

    def _facts_from_values(self, task: Any, values: dict[str, Any],
                           tools: list[str]) -> list[dict[str, Any]]:
        """Raw values obtained by code → facts. **No LLM processing whatsoever.**

        The `{num, denom}` shape is converted into a percentage according to
        answer_spec's percent convention,
        with the conversion rule coming from the server-side recipe (the same
        convention the referee uses), not computed on the fly by the model.
        """
        facts: list[dict[str, Any]] = []
        for p in getattr(task, "rubric", None) or []:
            dim = str(getattr(p, "dimension", "") or "")
            if dim not in values:
                continue
            raw = values[dim]
            if raw is None:
                continue                     # not retrieved means not retrieved; no
                                            # fallback
            spec = getattr(p, "answer_spec", None)
            percent = bool(getattr(spec, "percent", False)) if spec else False
            value: Any = raw
            base: int | None = None
            if isinstance(raw, dict):
                num = raw.get("num")
                denom = raw.get("denom")
                if percent and isinstance(num, (int, float)) \
                        and isinstance(denom, (int, float)) and denom:
                    value = round(float(num) / float(denom) * 100, 1)
                    base = int(denom)
                else:
                    value = num if num is not None else raw
                    if isinstance(denom, (int, float)):
                        base = int(denom)
            unit = "percent" if percent else ("ratio" if isinstance(value, float)
                                              else "raw")
            facts.append(make_fact(
                subject=dict(getattr(p, "subject", None) or {}),
                dimension=dim,
                value=value, unit=unit, base=base,
                source={"tool": "action_code",
                        "program": True,
                        "tools": list(tools)},
            ))
        return facts

    async def run(self, task: Any) -> dict[str, Any]:
        self.trajectory = []
        self.errors = []
        self.hallucinations = []
        self.schema_hints = []
        self.attempted = []

        if ACTION_CODE and self._is_tool_path_task(task):
            return await self._run_with_code(task)

        # Following VLML: get "what is available" before taking action, rather than writing
        # something, getting it bounced, and then guessing
        if not self.db_info:
            self.db_info = await db_self_description()

        # Restore gaps across runs: the dimensions graded wrong in the previous run
        # must keep driving the redo this round.
        # Gaps are recorded in run_log (persisted), not in the skill library — the
        # skill library only holds approaches the referee accepted.
        if not self.failed_dims:
            dim_by_point = {p.point: p.dimension for p in task.rubric}
            last_missing = [str(m) for m in
                            run_log.last_missing(getattr(task, "topic_id", ""))]
            self.failed_dims = sorted({
                dim_by_point.get(m.split("(")[0].strip(), m)
                for m in last_missing
            })
            # Scoring-point level is restored alongside: at dimension level, restoring
            # across runs cannot express "which entry under this dimension was not
            # submitted".
            # ⚠️ missing entries in run_log carry a `(value mismatch: got≠ref)`
            # judgement suffix (containing the referee's
            # reference value), and that suffix must be stripped when restoring —
            # failed_points gets spliced into the next round's prompt.
            self.failed_points = sorted({feedback_mod.point_name(m)
                                         for m in last_missing})

        # ---- First run the programs in the library; if one runs, adopt it directly
        #      (the original "skill is exec'd" itself) ----
        #
        # Previously skills were only **spliced into the prompt for a human to
        # read**, never executed — which is precisely
        # the root cause of "the skill library has content but coverage does not
        # rise": the model rewrites it every round, and every rewrite is
        # a fresh opportunity to misread a key. The original Voyager simply exec's
        # `program_code` after taking it out.
        # The plan path (SQL questions) has no program_code; its equivalent is the
        # stored
        # **call sequence** — see `_replay_saved`.
        replayed = await self._replay_saved(task)
        if replayed is not None:
            facts, skill = replayed
            return {
                "facts": facts,
                "narrative": {"text": "", "comparable": False},
                "skill_used": f"replay({skill.get('name', '')})",
                "reused_skill": skill.get("name", ""),
                "thought": "Replayed the call sequence from the skill library that I "
                           "got running myself (the metric definition has already "
                           "been verified by the referee)",
                "errors": list(self.errors),
                "hallucinations": list(self.hallucinations),
            }

        plan = self._plan(task, call_limit=self._call_limit(task))
        obs = await self._execute(plan, limit=self._call_limit(task))
        ok_obs = [o for o in obs if o.get("result") is not None]

        # Following critic.py:39-42 — onError is judged a failure outright.
        # When every tool fails we do not enter extraction: asking the model to
        # "organise the facts" from a string of error text
        # only makes it turn the errors into numbers. Measured: an empty trajectory
        # still produced 5 facts.
        if not ok_obs:
            facts: list[dict[str, Any]] = []
            self.errors.append(
                "No tool call succeeded this round — judged a failure outright by the "
                "critic's onError rule, and fact extraction is skipped (otherwise "
                "the model invents numbers from the error messages)"
            )
        else:
            facts = self._extract(task, obs)
            facts = self._ground_facts(facts, obs)

        # Store this round's observations — the next round's prompt needs them
        # (following render_human_message)
        self.last_round = {
            # Use attempted rather than trajectory: failed calls and their raw SQL
            # must be fed back too
            "calls": [dict(a) for a in self.attempted],
            "errors": list(self.errors),
            "critique": self.last_critique,
            "schema_hints": list(self.schema_hints),
        }
        self.schema_hints = []
        return {
            "facts": facts,
            "narrative": {"text": getattr(self, "_narrative_text", ""), "comparable": False},
            "skill_used": f"llm(skills={len(skill_store.all_skills())})",
            "thought": self.last_thought,
            "errors": list(self.errors),
            "hallucinations": list(self.hallucinations),
        }

    # ---- learning: feeding the score back, following the fork's critic shape ----
    # _critic() in voyager-fork/voyager_disk/drift_voyager.py:412-469:
    #   returns (success, critique, signal), where critique is a **first-person
    #   self-reflection**
    #   ("I expected… but the world returned… I thought…"), success → _synthesize_skill,
    #   failure → (intent, critique) goes into IntentDiffAnalyzer and lands as a
    #   GapSpec.
    # _context() in fork/voyager_disk/glm_curator.py:235-243 in turn writes
    # "tasks that have already failed (gap type)" into the next question's prompt —
    # that is "feeding the score back to drive the next question".
    def learn(
        self,
        *,
        covered: list[str],
        missing: list[str],
        task: Any = None,
        referee: Any = None,
        verdict: str = "",
        rejected_low_base: list[str] | None = None,
        confidence: float = 0.0,
    ) -> str:
        """Return this round's critique (it is written into the next round's prompt
        and also goes to the dashboard).

        The structured feedback pack lives in `self.last_feedback` (a dict), with
        fields aligned to the study companion's
        `answer_evaluate` return — the critique is merely its rendered text view.
        """
        dim_by_point = {p.point: p.dimension for p in (task.rubric if task else [])}
        topic = getattr(task, "topic_id", "?") if task else "?"

        # My orchestration: only **successful** calls count. Failed calls go into
        # errors/attempted
        # and must not count as "an orchestration I know", otherwise the feedback will
        # think some tool has already been called.
        my_plan = [str(t.get("tool")) for t in self.trajectory if t.get("tool")]
        # The referee's orchestration: internal markers (<deterministic-sql>) are not
        # real tools, filter them out —
        # measurement shows Voyager writes them into the skill library as real tool
        # names and then keeps calling a tool that does not exist.
        ref_plan = [str(t) for t in (getattr(referee, "trajectory", None) or [])
                    if not str(t).startswith("<")]

        fb = feedback_mod.build_feedback(
            verdict=verdict,
            score=int(round(100 * (len(covered) / max(1, len(covered) + len(missing))))),
            coverage=(len(covered) / max(1, len(covered) + len(missing))),
            covered=covered, missing=missing,
            rejected_low_base=list(rejected_low_base or []),
            dim_by_point=dim_by_point,
            # subject lets the feedback for SQL-path dimensions say clearly that
            # "WHERE must carry map=Haven"
            subj_by_point={str(p.point): dict(p.subject or {})
                           for p in (task.rubric if task else [])},
            ref_plan=ref_plan, my_plan=my_plan,
            confidence=float(confidence or 0.0),
        )
        self.last_feedback = fb.to_dict()
        miss_dims = sorted({d.get("dimension") or d.get("point")
                            for d in fb.missing_detail})

        if not missing:
            # Following curriculum.clean_up_tasks: on success, push out the failure
            # records of the same question.
            # In v1 it was append, so "lessons from failure" and "experience from
            # success" ended up fighting each other in the library at the same time.
            #
            # The lesson is no longer a template ("first get the overall report then
            # drill down with query_sql" — that sentence is hardcoded,
            # so writing it into the skill library is the same as writing nothing):
            # it is now **this concrete orchestration + the dimensions it covers**.
            # The name must be a **stable primary key**: the graph's question-type
            # topic_id itself
            # (`run_mve._stable_skill_name` is also `{topic_id}`).
            # Previously this was called `{topic}解法` ("解法" = solution) — that literal
            # suffix is part of the historical skill-store key format still present in
            # skill_store.json, so it is quoted verbatim here and not translated. It is
            # not the same
            # key as the `{topic_id}` the main loop uses to store source code — so one
            # result landed as two entries: one **pure text with no source code**, one
            # with source code,
            # and on injection the model might read the one without source code
            # (exactly the residue of "skills are pseudocode comments that are only
            # read", and it also violates "skills store reusable code, not one or
            # two lines of idea").
            # Both channels now write the same key: this entry first leaves a textual
            # lesson, and once it runs, `_store_skill_for` fills in **the source code
            # you wrote yourself** on that same entry (add with the same key →
            # Rewriting overwrites and writes code). In the end each graph node
            # corresponds to exactly one skill with source code.
            action, key = skill_store.add(
                topic, topic, fb.lesson,
                source="practice", blueprint=feedback_mod.blueprint_for(fb),
            )
            self._log_skill(action, key)
            self.failed_dims = [d for d in getattr(self, "failed_dims", []) if d not in miss_dims]
            # Fully covered: clear the gaps (same semantics as failed_dims).
            # Note this must not be written as `[p for p in ... if p not in miss_pts]`
            # — in this branch
            # missing is always empty, and that filter would keep **all** the old
            # gaps, so the next round would keep reporting them.
            self.failed_points = []
            self._sync_memory()
            # The skill is effective → record a mark; retrieval will weight it, and
            # ineffective skills sink naturally
            skill_store.mark_result(self._injected_keys, True)
            self.last_critique = feedback_mod.render_critique(fb)
            return self.last_critique

        # Following Voyager's main loop voyager.py:351 — `if info["success"]:
        # add_new_skill(info)`:
        # **a failure is not written to the skill library**. Being graded wrong must
        # be digested, but not by freezing "what I did this time" —
        # what was done this time has already been proven wrong by the referee, and
        # writing it in is planting a landmine in the library.
        # Measured counterexample: after pistol_eco_pattern accumulated the wrong
        # lesson "analyse along the eco_win_rate dimension using
        # match_summary_report", the following rounds were led astray by it and
        # coverage actually stopped rising.
        #
        # So what does a wrong grade get digested by? Following Voyager's rollout: the
        # critique is fed back when **redoing within this question**
        # (last_round → _render_observation), redoing at most until it is right;
        # across runs it relies on the gap dimensions persisted in run_log (restored
        # in __init__),
        # which the question generator also reads to pick follow-up questions. The gaps
        # stay; the approach does not.
        #
        # Previously this called the LLM once to have the model "summarise the lesson",
        # then **did not write to the library** (only logged it) —
        # a purely wasted call. It is not called now: the structured feedback is
        # computed directly from the verdict,
        # which is more accurate than the model's retelling ("not retrieved" and
        # "retrieved wrongly" come from the verdict, they are not guesses).
        self._log_skill("not_saved_failure", f"{topic}::{fb.error_type}",
                        f"Judged {verdict} ({fb.error_type}); the approach was not "
                        f"frozen (gaps: {', '.join(miss_dims) or 'none'})")

        # Following the fork: a failure must leave behind a "gap type" for the next
        # question's curriculum to read
        self.failed_dims = sorted(set(getattr(self, "failed_dims", [])) | set(miss_dims))
        # Scoring-point level: dimension level can only say "which dimension is
        # missing", it cannot say "which entry under this dimension was not
        # submitted".
        # ⚠️ You must store the scoring point's original text (point_name) with **the
        # judgement suffix stripped** — the
        # `(value mismatch: got≠ref)` in the suffix carries the referee's reference
        # value, and failed_points gets spliced into the next round's
        # prompt (_render_observation); not stripping it means handing the model the
        # answer key outright.
        self.failed_points = sorted(
            set(getattr(self, "failed_points", []))
            | {feedback_mod.point_name(str(m)) for m in missing})
        self._sync_memory()

        # Wrong assumptions get their own entry for the causal timeline: dimension-level
        # failed_dims can only say "which dimension is missing",
        # it cannot say "what I wrongly assumed" — and the latter is what the question
        # generator should use to produce the next question.
        if fb.misconceptions:
            causal_timeline.append(
                causal_timeline.REFEREE,
                topic_id=topic,
                summary=f"Wrong assumption ({fb.error_type}): {fb.misconceptions[0]}",
                detail={"error_type": fb.error_type, "error_types": fb.error_types,
                        "misconceptions": fb.misconceptions,
                        "missing_detail": fb.missing_detail,
                        "my_plan": my_plan, "ref_plan": ref_plan,
                        "validated_target": False},
            )

        self.last_critique = feedback_mod.render_critique(fb)
        return self.last_critique

    def _log_skill(self, action: str, key: str, note: str = "") -> None:
        """The outcome of a skill write must be visible — otherwise "did it actually block
    the bloat" can only be guessed at."""
        label = {
            "added": "New skill added",
            "rewritten": "Same-named skill overwritten",
            "skipped": "Dropped (duplicate / no value)",
            "not_saved_failure": "Graded wrong · approach not frozen",
        }.get(action, action)
        self.skill_events = getattr(self, "skill_events", [])
        self.skill_events.append({
            "action": action, "key": key, "label": label, "note": note,
        })

    def _sync_memory(self) -> None:
        """self.memory is only a text view for display; the real storage is skill_store's
        dict."""
        self.memory = skill_store.load()
        return self.last_critique
