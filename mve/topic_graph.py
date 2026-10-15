#!/usr/bin/env python3
"""MVE's teaching graph + scientific topic selector (ported from the study companion's store_topics / graph_read_model).

Why this layer (not knowledge_graph.py)
----------------------------------------
`knowledge_graph.py` is the **scoring-side** subgraph: its nodes are data dimensions/tools/tables, serving only
answer_spec scoring ("which tool should this dimension call").

This file is the **teaching-side** graph: its nodes are the 29 VLML
topics produced by the study companion's `build_vlml_seed.py` (the graph you added by hand), each carrying depth / difficulty / prerequisites
(with required_mastery thresholds) / related / skills / typical_misconceptions /
examples. It answers "which topic should be learned/practiced next".

Selection logic (the study companion's "scientific" core, evidence-backed)
------------------------------------
1. **Prerequisite gating**: a topic can only be pushed once all its prerequisites have mastery ≥ required_mastery
   (the study companion's `required_mastery` gate, same approach as knowledge_tracker.py:2066).
   Pushing before the threshold is met is a typical case of being "unscientific" — the learner spins on metrics with no foundation.
2. **Weak-first**: lower mastery means higher priority; never practiced (None) counts as weakest, prioritizing new ground.
3. **Difficulty gradient**: among selectable topics pick the one closest to the global target difficulty (the study companion's `_by_gradient`).
4. **scope filtering**: the practice scope (stage/subject/chapter) filters like the study companion's list_topics.

Mastery is stored in `topic_mastery.jsonl` (decoupled from TASKS' coverage — topics ≠ exam questions).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SEED_PATH = HERE / "static" / "knowledge_seeds" / "valorant.json"
MASTERY_PATH = HERE / "topic_mastery.jsonl"

DEFAULT_REQUIRED_MASTERY = 0.5

# ---------------------------------------------------------------------------
# Voyager curriculum mechanism (replaces/enhances MVE's question generator, landed 2026-10-12)
# ---------------------------------------------------------------------------
# 1) Give-up mechanism (plugs the MC 161-round infinite loop): a topic practiced MAX_ATTEMPTS_PER_TOPIC times
#    (first attempt + retries, design-specified = 3) without success → mark stale, and select_next forcibly skips to another topic.
#    Never spin forever on one topic (MC observed 161 rounds without switching = never failing cleanly).
#    ⚠️ Hard design constraint: the give-up decision only looks at "enough attempts", never reading mastery/coverage
#    (scores/mastery go to dashboard display only, and are never read by any advancement condition).
MAX_ATTEMPTS_PER_TOPIC = 3
STALE_MASTERY = 0.6   # Kept only for the dashboard's "threshold met?" display; no longer part of give-up/selection decisions

# 2) Cross-domain baseline (user-specified: skills/actions lock to VLML's 10 tools / semantic layer, no raw SQL).
#    These topics depend on ext_* tables (first blood / economy rounds); when switching to a database without ext tables (vlml_vlr)
#    they are unanswerable → the question generator deprioritizes them, and releases them once switched to a database with ext tables (vlml_rib).
EXT_TOPICS = {
    "vlml_first_blood", "vlml_fb_conversion", "vlml_post_plant", "vlml_economy",
    "vlml_trade", "vlml_opening_duel", "vlml_grain_trap", "vlml_sample_size",
}


# ---------------------------------------------------------------------------
# Mastery storage (per topic, independent of per-question coverage)
# ---------------------------------------------------------------------------
class MasteryStore:
    """One row per topic: {topic_id, mastery, attempts, correct, last_at}.

    Mastery = exponential moving average of recent accuracy (same idea as the study companion's mastery_v2, simplified).
    """

    def __init__(self, path: Path = MASTERY_PATH):
        self.path = Path(path)
        self._rows: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row.get("topic_id"), str):
                row.setdefault("completed", False)
                # —— MVE node state (isomorphic to the original curriculum's completed/failed + skill) ——
                # Each graph node aggregates: accumulated skill pointers / task failures / skips (with reasons).
                # Design: "the skill library and the graph are the same storage — voyager unlocks milestones by graph node,
                # and each node can accumulate skills". Node state is the milestone ledger; skill SQL bodies still live in
                # skill_store (addressed by topic::name); nodes store only pointers, avoiding duplicate persistence.
                row.setdefault("skills", [])
                row.setdefault("failures", [])
                row.setdefault("skips", [])
                self._rows[row["topic_id"]] = row

    def get(self, topic_id: str) -> dict[str, Any] | None:
        return self._rows.get(topic_id)

    def mastery_of(self, topic_id: str) -> float | None:
        row = self._rows.get(topic_id)
        return float(row["mastery"]) if row and row.get("mastery") is not None else None

    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._rows)

    def update(self, topic_id: str, *, score: float, commit: bool = True,
               count_attempt: bool = True) -> dict[str, Any]:
        """score ∈ [0,1]: this attempt's score. Updates the moving-average mastery.

        `count_attempt=False` (used by the **consumption path**): updates the score only, without incrementing attempts
        or setting stale.
        Why they must be separate: practice-side stale is judged by "how many times practiced" (MAX_ATTEMPTS_PER_TOPIC=3,
        give up and switch once enough). A human import is **consumption**, not practice — the same person asking the same question three times in a row
        would mark that node stale, and practice would never select it again. That mixes the two ledgers:
        consumption counts should not eat into the practice attempt budget.

        Two extra fields maintained for Voyager's curriculum:
          streak_wrong — consecutive wrong answers (a wrong streak triggers added scaffolding; see difficulty.py);
          stale       — give-up marker: enough attempts without meeting the bar → the question generator forcibly switches topics
                        (plugs the MC 161-round infinite loop: never failing cleanly = never switching).
        """
        row = self._rows.get(topic_id) or {
            "topic_id": topic_id, "mastery": None, "attempts": 0,
            "correct": 0, "last_at": 0, "streak_wrong": 0, "stale": False,
            "completed": False, "skills": [], "failures": [], "skips": [],
        }
        if count_attempt:
            row["attempts"] = int(row.get("attempts", 0)) + 1
        row["correct"] = int(row.get("correct", 0)) + (1 if score >= 0.6 else 0)
        # Wrong-streak count: reset on correct, +1 on wrong (read by difficulty.py's two-wrongs-add-scaffolding rule)
        row["streak_wrong"] = 0 if score >= 0.6 else int(row.get("streak_wrong", 0)) + 1
        prev = row.get("mastery")
        if prev is None:
            row["mastery"] = float(score)
        else:
            # Exponential moving average: new sample weight 0.4
            row["mastery"] = round(prev * 0.6 + score * 0.4, 4)
        row["last_at"] = int(time.time())
        # Give-up mechanism: MAX_ATTEMPTS_PER_TOPIC attempts → mark stale (design-specified = 3).
        # ⚠️ Hard design constraint: the give-up decision **only looks at the count**, never reading mastery/coverage —
        # scores are never read by any advancement condition (skip, next topic, and round-end decisions never read it).
        # "Happening to guess right on the last attempt" does not clear the marker: enough is enough.
        if count_attempt:
            row["stale"] = bool(row["attempts"] >= MAX_ATTEMPTS_PER_TOPIC)
        self._rows[topic_id] = row
        if commit:
            self._persist()
        return row

    def is_stale(self, topic_id: str) -> bool:
        return bool((self._rows.get(topic_id) or {}).get("stale"))

    def unstale(self, topic_id: str = "", *, reset_attempts: bool = True,
                commit: bool = True) -> list[str]:
        """Clear the "give up and switch" marker (with topic_id, only that topic; without it, all).

        Why this escape hatch is needed (observed 2026-10-13): once stale is set the topic is **permanently** excluded from the curriculum,
        and the graph has only 29 nodes total — after 8 stale + 9 unmet prerequisites,
        `select_next` returns exhausted every time → every round generates new questions, and the curriculum is effectively void.

        Worse, attempts gets **polluted by the old backend**: observed `vlml_agent_role` attempts=732.
        That is not "practiced 732 times and still not learned", but the old backend accumulating `update(count_attempt=True)`
        every round (back then consume and practice ledgers were not separated, and stale was dead code).
        So cleanup must be able to reset attempts too, otherwise stale is set again immediately the next round after clearing.

        Returns the list of cleaned topic_ids.
        """
        touched: list[str] = []
        for tid, row in self._rows.items():
            if topic_id and tid != topic_id:
                continue
            if not (row.get("stale") or (reset_attempts
                                         and int(row.get("attempts") or 0) > 0)):
                continue
            row["stale"] = False
            if reset_attempts:
                row["attempts"] = 0
            touched.append(tid)
        if commit and touched:
            self._persist()
        return touched

    def mark_completed(self, topic_id: str, *, commit: bool = True,
                       source: str = "practice") -> dict[str, Any]:
        """Set the two bits `completed` (+ `completed_source`).

        Decoupled from mastery (display-only): completed is the boolean "was it done", mastery is
        "how well it was done" (dashboard-only). select_next excludes completed nodes on this basis, without re-planning.

        ⚠️ Semantic collision (found in observation 2026-10-13; this parameter exists to pull them apart):
          `completed` originally had **one** meaning only — "this node has been planned, `select_next`
          should not plan it again", a **dedup bit**, unrelated to "how well it was done".
          Later the consumption line (`voyager_vlml/consume.py`) **borrowed** it to mean "mastered",
          so two semantics crowded onto the same boolean, with three writers:

            · `voyager_vlml/agents/curriculum.py` practice loop success → do not plan again
            · `mve/run_mve.py` (the frozen old backend) ev.success → do not plan again
            · `voyager_vlml/consume.py` human asked and it answered → I know this one now

          When the dashboard's mastery numerator counts `completed`, the practice line's 29 `runtime_*` entries
          mix in — observed on the server as 58/45 = **129%**, numerator bigger than denominator.

          `completed_source` separates the two:
            `"practice"` = marked by the practice loop (dedup bit, **not capability**)
            `"consume"`  = marked by the consumption line (human asked and it answered, **this is capability**)

          Following the original Voyager (`voyager/agents/curriculum.py:339` appends only on success,
          `:135` learning curve = `len(completed_tasks)`): **learning does not count** —
          a practice success only advances curriculum planning, and does not count as "capability grew"; capability only grows from the consumption side.

          Upgrade rule: practice → consume one-way. Practice marked it first, then consumption answered it → upgrade to
          consume; consumption marked it first, then practice ran this topic → **no downgrade** (the fact of mastery cannot be erased).
        """
        row = self._rows.get(topic_id) or {
            "topic_id": topic_id, "mastery": None, "attempts": 0,
            "correct": 0, "last_at": 0, "streak_wrong": 0, "stale": False,
            "completed": False, "skills": [], "failures": [], "skips": [],
        }
        row["completed"] = True
        if source == "consume" or not row.get("completed_source"):
            row["completed_source"] = source
        self._rows[topic_id] = row
        if commit:
            self._persist()
        return row

    # ---- Node state: skills / failures / skips accumulated per graph node (design: skill library and graph are the same
    #      storage, each node accumulating skills; task failures/skips carry reasons into context) ----
    def node_skills(self, topic_id: str) -> list[str]:
        return list((self._rows.get(topic_id) or {}).get("skills") or [])

    def add_skill(self, topic_id: str, skill_key: str, *, commit: bool = True) -> None:
        """After a skill is persisted, hang its pointer on the corresponding graph node (each node accumulates skills)."""
        if not skill_key:
            return
        row = self._rows.get(topic_id) or {
            "topic_id": topic_id, "mastery": None, "attempts": 0,
            "correct": 0, "last_at": 0, "streak_wrong": 0, "stale": False,
            "completed": False, "skills": [], "failures": [], "skips": [],
        }
        skills = list(row.get("skills") or [])
        if skill_key not in skills:
            skills.append(skill_key)
        row["skills"] = skills
        self._rows[topic_id] = row
        if commit:
            self._persist()

    def add_failure(self, topic_id: str, verdict: str, reason: str, *,
                    commit: bool = True) -> None:
        """Record a task failure (with reason), into context so observers can tell "not learned" vs "pipeline not working".

        Also **increments attempts and sets stale at 3** — this is the only landing point of the "retry ≥3 skip"
        design constraint.
        ⚠️ Observed pitfall: stale used to be set only inside `update(score=...)`, while the new backend
        (voyager_vlml) never goes through the scoring channel, only add_failure → attempts stays 0 forever
        → stale stays False forever → select_next picks the same unwinnable question every round
        (observed 3 consecutive rounds all vlml_kd, progress frozen). Failures must also be counted,
        or "give up and switch" is dead code in a backend that does not score.
        """
        row = self._rows.get(topic_id) or {
            "topic_id": topic_id, "mastery": None, "attempts": 0,
            "correct": 0, "last_at": 0, "streak_wrong": 0, "stale": False,
            "completed": False, "skills": [], "failures": [], "skips": [],
        }
        fails = list(row.get("failures") or [])
        fails.append({"verdict": str(verdict), "reason": str(reason),
                      "at": int(time.time())})
        row["failures"] = fails[-20:]
        row["attempts"] = int(row.get("attempts", 0)) + 1
        row["last_at"] = int(time.time())
        # Give-up decision only looks at the count, never reading mastery/coverage (hard design constraint)
        row["stale"] = bool(row["attempts"] >= MAX_ATTEMPTS_PER_TOPIC)
        self._rows[topic_id] = row
        if commit:
            self._persist()
        return row

    def add_skip(self, topic_id: str, reason: str, *, commit: bool = True) -> None:
        """Record a skip (with reason), into context."""
        row = self._rows.get(topic_id) or {
            "topic_id": topic_id, "mastery": None, "attempts": 0,
            "correct": 0, "last_at": 0, "streak_wrong": 0, "stale": False,
            "completed": False, "skills": [], "failures": [], "skips": [],
        }
        skips = list(row.get("skips") or [])
        skips.append({"reason": str(reason), "at": int(time.time())})
        row["skips"] = skips[-20:]
        self._rows[topic_id] = row
        if commit:
            self._persist()

    def is_completed(self, topic_id: str) -> bool:
        return bool((self._rows.get(topic_id) or {}).get("completed"))

    def streak_of(self, topic_id: str) -> int:
        return int((self._rows.get(topic_id) or {}).get("streak_wrong", 0))

    def _persist(self) -> None:
        lines = [json.dumps(r, ensure_ascii=False) for r in self._rows.values()]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# ---------------------------------------------------------------------------
# Teaching graph
# ---------------------------------------------------------------------------
class TopicGraph:
    def __init__(self, seed_path: Path = SEED_PATH, mastery: MasteryStore | None = None):
        self.seed_path = Path(seed_path)
        self.topics: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []  # {from,to,relation,required_mastery}
        self.mastery = mastery or MasteryStore()
        self.load()

    def load(self) -> None:
        if not self.seed_path.exists():
            return
        payload = json.loads(self.seed_path.read_text(encoding="utf-8"))
        for t in payload.get("topics", []):
            self.topics[t["id"]] = t
        for t in self.topics.values():
            for pre in t.get("prerequisites", []) or []:
                req = float(pre.get("required_mastery", DEFAULT_REQUIRED_MASTERY))
                self.edges.append({
                    "from": str(pre.get("id", "")), "to": t["id"],
                    "relation": "prerequisite", "required_mastery": req,
                })
            for rel in t.get("related", []) or []:
                self.edges.append({
                    "from": str(rel.get("id", "")), "to": t["id"],
                    "relation": str(rel.get("relation", "related")),
                    "required_mastery": None,
                })

    # ---- scope / filtering ----
    @staticmethod
    def _scope_val(field: str, value: object) -> str:
        v = "" if value is None else str(value)
        if field in ("stage", "subject", "course_family"):
            return v.lower().replace("-", "_").replace(" ", "_")
        return v.strip()

    def scope_matches(self, topic: dict[str, Any], scope: dict[str, Any] | None) -> bool:
        if not scope:
            return True
        for field in ("stage", "subject", "course_family", "chapter", "unit"):
            expected = self._scope_val(field, scope.get(field))
            if not expected:
                continue
            if self._scope_val(field, topic.get(field)) != expected:
                return False
        return True

    def topics_in_scope(self, scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [t for t in self.topics.values() if self.scope_matches(t, scope)]

    # ---- MC node milestone model (design: arrange VLML's tables into a tree, thickest trunk = milestone) ----
    # Isomorphic to the original Voyager's "milestone = completed tasks" + MC's "must chop trees first" tech tree:
    #   · The prerequisite DAG (prerequisite edges) is VLML's tree; trunk = the longest prerequisite path
    #     (design's exact words "nodes on the path through the most nodes are milestones").
    #   · keystone = the node with the highest betweenness ("the node through which the most paths pass" = the thickest trunk point).
    #   · seed = the trunk's starting point (no prerequisites), corresponding to MC's Mine 1 wood log — the basic unit
    #     (random (table, column)) must be completed first to unlock the entire trunk.
    def _prereq_adj(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        parents: dict[str, list[str]] = defaultdict(list)
        children: dict[str, list[str]] = defaultdict(list)
        for e in self.edges:
            if e["relation"] != "prerequisite":
                continue
            parents[e["to"]].append(e["from"])
            children[e["from"]].append(e["to"])
        return parents, children

    def _milestone_cache(self) -> dict[str, Any]:
        if hasattr(self, "_mc_cache"):
            return self._mc_cache
        parents, children = self._prereq_adj()

        # Longest prerequisite path (DP: find the longest chain down from each root)
        memo: dict[str, tuple[int, list[str]]] = {}

        def longest(node: str) -> tuple[int, list[str]]:
            if node in memo:
                return memo[node]
            best: tuple[int, list[str]] = (1, [node])
            for c in children.get(node, []):
                l, path = longest(c)
                if l + 1 > best[0]:
                    best = (l + 1, [node] + path)
            memo[node] = best
            return best

        roots = [n for n in self.topics if not parents.get(n)]
        trunk: list[str] = []
        best_len = 0
        for r in roots:
            l, path = longest(r)
            if l > best_len:
                best_len, trunk = l, path

        milestones = set(trunk)
        seed = trunk[0] if trunk else None

        # betweenness (keystone): for each node, count the (ancestor, descendant) path pairs passing through it.
        # Ancestors / descendants are walked up/down the DAG.
        def ancestors(n: str) -> set[str]:
            out: set[str] = set()
            stack = list(parents.get(n, []))
            while stack:
                p = stack.pop()
                if p not in out:
                    out.add(p)
                    stack.extend(parents.get(p, []))
            return out

        def descendants(n: str) -> set[str]:
            out: set[str] = set()
            stack = list(children.get(n, []))
            while stack:
                c = stack.pop()
                if c not in out:
                    out.add(c)
                    stack.extend(children.get(c, []))
            return out

        keystone, key_score = None, -1
        for n in self.topics:
            a, d = len(ancestors(n)), len(descendants(n))
            score = a * d
            if score > key_score:
                key_score, keystone = score, n

        self._mc_cache = {
            "trunk": trunk, "milestones": milestones, "seed": seed,
            "keystone": keystone, "keystone_score": key_score,
        }
        return self._mc_cache

    def milestone_info(self) -> dict[str, Any]:
        """For dashboard/context consumption: trunk, milestone set, seed, keystone."""
        c = self._milestone_cache()
        return {
            "trunk": list(c["trunk"]),
            "milestones": sorted(c["milestones"]),
            "seed": c["seed"],
            "keystone": c["keystone"],
            # keystone_score = betweenness (ancestor count × descendant count) = how many prerequisite paths
            # pass through it. The dashboard needs to show "why it is the thickest trunk point", so a name alone is not enough.
            "keystone_score": c.get("keystone_score"),
            "seed_unit": None,  # The seed unit (random (table, column)) is injected by scene_context
        }

    def trunk_milestones(self) -> set[str]:
        return set(self._milestone_cache()["milestones"])

    def is_milestone(self, topic_id: str) -> bool:
        return topic_id in self._milestone_cache()["milestones"]

    def is_trunk(self, topic_id: str) -> bool:
        return topic_id in set(self._milestone_cache()["trunk"])

    # ---- Prerequisite gating ----
    def prerequisites_of(self, topic_id: str) -> list[dict[str, Any]]:
        return [e for e in self.edges if e["to"] == topic_id and e["relation"] == "prerequisite"]

    def prereq_ok(self, topic: dict[str, Any]) -> bool:
        """Prerequisite gate: unlocked only when prerequisite nodes are **completed** (task ran through).

        ⚠️ Hard design constraint: the gate only looks at "whether prerequisite nodes are completed (boolean)",
        never reading mastery/coverage — scores are never read by any advancement condition.
        The ordered curriculum (match → team → player) is driven by completion, not by scores.

        ⚠️ One addition on 2026-10-13 (an observed deadlock that had to be fixed): when a prerequisite is **already given up (stale)**,
        it no longer blocks successors. stale means "enough attempts and still cannot run through, give up and switch",
        and `select_next` excludes it permanently — so successors wait on a prerequisite that will **never be
        completed**, and the whole chain goes down with it. Observed contagion scale:

            vlml_round_result stuck → first_blood / post_plant / clutch / streak all stuck
            vlml_first_blood  stuck → fb_conversion / trade / opening_duel stuck one more layer
            vlml_half_split   stuck → side_conversion stuck
            vlml_agent_role   stuck (attempts=732, old-backend pollution)

        4 stuck nodes blocked 9 successors, and **not one of the 29 nodes** could be selected → exhausted every
        round → new questions generated every round, the curriculum effectively void. "Waiting on a given-up prerequisite" is pointless —
        and the original Voyager has no prerequisite concept at all; this is a side effect of copying the study companion.
        """
        for e in self.prerequisites_of(topic["id"]):
            pid = str(e.get("from") or "")
            if not pid or self.mastery.is_completed(pid):
                continue
            if self.mastery.is_stale(pid):
                continue          # Prerequisite already given up → no longer blocks
            return False
        return True

    def blocked_by(self, topic: dict[str, Any]) -> list[str]:
        """Return prerequisite ids that are **still being practiced** but not completed (used to explain why this question is not unlocked yet).

        Same convention as `prereq_ok`: given-up (stale) prerequisites no longer count as "the reason it is blocked",
        otherwise the explanation would say "prerequisite incomplete" while the gate has actually let it through — self-contradictory.
        """
        out = []
        for e in self.prerequisites_of(topic["id"]):
            pid = str(e.get("from") or "")
            if not pid or self.mastery.is_completed(pid):
                continue
            if self.mastery.is_stale(pid):
                continue
            out.append(pid)
        return out

    # ---- Global target difficulty (mirrors the study companion's _global_target: the more mastered, the higher the target) ----
    def global_target(self) -> tuple[int, str]:
        rows = self.mastery.all()
        known = [r for r in rows.values() if r.get("mastery") is not None]
        total = len(self.topics)
        if not known:
            return 1, "No topics started yet → target difficulty 1 (build from the foundation)"
        mastered = sum(1 for r in known if float(r["mastery"]) >= 0.8)
        avg = sum(float(r["mastery"]) for r in known) / len(known)
        target = min(4, max(1, 1 + mastered))
        return target, (
            f"Practiced {len(known)}/{total} topics · average mastery {avg:.0%} · "
            f"{mastered} mastered → target difficulty {target}"
        )

    # ---- Curriculum order (design: ordered curriculum planning, match → team → player) ----
    def _chapter_rank(self, topic: dict[str, Any]) -> int:
        """Chapters' order of appearance in the seed = curriculum order (never reading mastery/coverage)."""
        if not hasattr(self, "_chapter_order"):
            order: dict[str, int] = {}
            n = 0
            for t in self.topics.values():
                ch = str(t.get("chapter") or "")
                if ch not in order:
                    order[ch] = n
                    n += 1
            self._chapter_order = order
        return self._chapter_order.get(str(topic.get("chapter") or ""), 999)

    # ---- Core: select the next question (Voyager curriculum-driven) ----
    def select_next(
        self, *, scope: dict[str, Any] | None = None, explicit: str | None = None,
        ext_available: bool | None = None,
    ) -> dict[str, Any]:
        """Returns {topic_id, reason, explanation, blocked, hint, difficulty_target,
        novelty, stale_skipped, ext_deprioritized, exhausted}.

        ⚠️ Hard design constraint (scores/mastery are never read by any advancement condition):
          · Next topic = **ordered curriculum** (chapter order × depth), **never reading mastery/coverage**.
          · Completed nodes (task ran through) are excluded directly — "no re-planning".
          · Give-up mechanism: 3 attempts without success → stale, forcibly skip and switch topics.
          · Prerequisite gate: unlocked only when prerequisite nodes are completed (driven by completion, not scores).
          · No "winnable" task in scope (all completed / all stuck) → returns exhausted=True,
            and the caller "keeps generating new tasks" (design: the round does not end).
        """
        if explicit and explicit in self.topics:
            t = self.topics[explicit]
            return {
                "topic_id": explicit, "blocked": False, "hint": "",
                "difficulty_target": self.global_target()[0],
                "reason": "explicit", "explanation": f"Topic {explicit} pinned",
                "novelty": 0.0, "stale_skipped": 0, "ext_deprioritized": False,
                "exhausted": False,
            }

        if ext_available is None:
            ext_available = ext_tables_present()

        pool = self.topics_in_scope(scope)

        # —— Candidates = in scope, not marked completed, prerequisites completed, not stale ——
        stale_ids = [t["id"] for t in pool if self.mastery.is_stale(t["id"])]
        selectable = [
            t for t in pool
            if not self.mastery.is_completed(t["id"])
            and not self.mastery.is_stale(t["id"])
            and self.prereq_ok(t)
        ]

        if not selectable:
            # No "winnable" task in scope → signal exhausted, and the caller generates new tasks.
            done_n = sum(1 for t in pool if self.mastery.is_completed(t["id"]))
            blocked_n = sum(
                1 for t in pool
                if not self.mastery.is_completed(t["id"])
                and not self.mastery.is_stale(t["id"])
                and not self.prereq_ok(t))
            return {
                "topic_id": "", "blocked": False, "hint": "",
                "difficulty_target": self.global_target()[0],
                "reason": "exhausted",
                "explanation": (
                    f"No \"winnable\" task in scope (completed {done_n} / stuck {len(stale_ids)} "
                    f"/ prerequisites unmet {blocked_n}) → the caller should generate new tasks"),
                "novelty": 0.0, "stale_skipped": len(stale_ids),
                "ext_deprioritized": False, "exhausted": True,
            }

        # —— Ordered curriculum: trunk (milestones) first × chapter order × depth (never reading mastery/coverage) ——
        # Design "thickest trunk = milestone": trunk nodes (longest prerequisite path) are chosen before branch nodes,
        # structuring "ask about the match first → then the team → then the player" as trunk-first, rather than relying on chapter order alone.
        # When ext tables are missing, ext-dependent topics are deprioritized to the end, but still after completed nodes.
        milestones = self.trunk_milestones()
        ext_blocked = {t["id"] for t in selectable
                       if (not ext_available) and t["id"] in EXT_TOPICS}

        def key(t: dict[str, Any]) -> tuple:
            trunk_pen = 0 if t["id"] in milestones else 1
            ext_pen = 1 if t["id"] in ext_blocked else 0
            return (trunk_pen, ext_pen, self._chapter_rank(t),
                    int(t.get("depth", 1)), t["id"])

        chosen = min(selectable, key=key)
        cid = chosen["id"]
        m = self.mastery.mastery_of(cid)
        mtxt = "not practiced" if m is None else f"{m:.0%}"
        blocked = self.blocked_by(chosen)
        # Novelty score is display-only (does not drive selection): never practiced = 1.0, practiced but low mastery = medium.
        novelty = 1.0 if m is None else max(0.0, 0.6 - (m or 0))
        target, why_target = self.global_target()
        return {
            "topic_id": cid,
            "reason": "curriculum_order",
            "explanation": (
                f"{cid}: ordered curriculum (chapter {chosen.get('chapter')} · depth "
                f"{chosen.get('depth')}) | current mastery {mtxt} (display only, does not drive selection)"
                f"{('; prerequisites unmet: ' + ','.join(blocked)) if blocked else ''}"
                f"| {why_target}"
            ),
            "blocked": bool(blocked),
            "hint": self._hint_for(chosen),
            "difficulty_target": target,
            "novelty": round(novelty, 2),
            "stale_skipped": len(stale_ids),
            "ext_deprioritized": bool(cid in ext_blocked),
            "exhausted": False,
        }

    def _hint_for(self, topic: dict[str, Any]) -> str:
        """Condense the topic's misconceptions/examples into a hint for the model (so it avoids known pitfalls)."""
        mis = topic.get("typical_misconceptions") or []
        ex = topic.get("examples") or []
        parts = []
        if mis:
            parts.append("Common misconceptions: " + "; ".join(mis[:3]))
        if ex:
            first = ex[0]
            if isinstance(first, dict) and first.get("answer_outline"):
                parts.append("Example answer with tables: " + " → ".join(first["answer_outline"][:3]))
        return "｜".join(parts)

    # ---- For the dashboard: four-layer structure + edges + mastery overlay ----
    def to_graph_payload(self) -> dict[str, Any]:
        target, _ = self.global_target()
        # Skill count per node (design: skill library and graph correspond one-to-one by topic_id, with the dashboard showing
        # milestones and accumulated skills on the same screen — "the same storage" landing at node granularity).
        skill_counts: dict[str, int] = {}
        try:
            import skill_store
            for s in skill_store.all_skills():
                tid = str(s.get("topic") or "")
                if tid:
                    skill_counts[tid] = skill_counts.get(tid, 0) + 1
        except Exception:
            skill_counts = {}
        nodes = []
        for t in self.topics.values():
            m = self.mastery.mastery_of(t["id"])
            tid = t["id"]
            is_ms = self.is_milestone(tid)
            is_trunk = self.is_trunk(tid)
            nodes.append({
                "id": tid, "name": t.get("name", tid),
                "stage": t.get("stage", ""), "subject": t.get("subject", ""),
                "chapter": t.get("chapter", ""), "depth": int(t.get("depth", 1)),
                "difficulty": float(t.get("difficulty", 0.5)),
                "mastery": (None if m is None else round(m, 3)),
                "completed": bool(self.mastery.is_completed(tid)),
                "blocked": bool(self.blocked_by(t)),
                "stale": bool(self.mastery.is_stale(tid)),
                "ext_layer": bool(tid in EXT_TOPICS),
                "milestone": bool(is_ms),
                "trunk": bool(is_trunk),
                "seed": bool(tid == self._milestone_cache()["seed"]),
                "skill_count": int(skill_counts.get(tid, 0)),
                "node_skills": len(self.mastery.node_skills(tid)),
                "failures": len(self.mastery._rows.get(tid, {}).get("failures") or []),
                "skips": len(self.mastery._rows.get(tid, {}).get("skips") or []),
                "misconceptions": len(t.get("typical_misconceptions") or []),
                "examples": len(t.get("examples") or []),
            })
        edges = [{"from": e["from"], "to": e["to"], "relation": e["relation"]}
                 for e in self.edges]
        layers = defaultdict(set)
        for n in nodes:
            layers[("stage", n["stage"])].add(n["id"])
            layers[("subject", n["subject"])].add(n["id"])
            layers[("chapter", n["chapter"])].add(n["id"])
        return {
            "schema_version": 4,
            "difficulty_target": target,
            "target": target,
            "nodes": nodes, "edges": edges,
            "layers": {f"{k[0]}:{k[1]}": sorted(v) for k, v in layers.items()},
            "milestone": self.milestone_info(),
            "total": len(nodes),
        }


_GRAPH: TopicGraph | None = None


def ext_tables_present(db_path: str | None = None) -> bool:
    """Does the current database have ext_* tables (the rib.gg-exclusive layer for first blood / economy rounds, etc.)?

    This is the criterion for "scene conditioning": without ext tables, ext-dependent topics (EXT_TOPICS)
    are deprioritized in the question generator, because a bare ext query necessarily finds no data on a database missing the tables. Switching to a database
    like vlml_rib with ext tables releases them — skills still only describe "which tool to call / which kind of fact to fetch", never writing
    raw SQL, so a database swap only changes "whether that layer's questions have data"; the skills themselves transfer.
    """
    try:
        import json as _json
        import duckdb
        p = db_path
        if not p:
            cfg = HERE / "db_config.json"
            try:
                obj = _json.loads(cfg.read_text(encoding="utf-8"))
                p = (obj or {}).get("db_path") or ""
            except Exception:
                p = ""
        if not p:
            return False
        from pathlib import Path as _P
        if not _P(p).exists():
            return False
        con = duckdb.connect(str(p), read_only=True)
        try:
            tabs = [r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main'").fetchall()]
            return any(str(t).startswith("ext_") for t in tabs)
        finally:
            con.close()
    except Exception:
        return False


def get_graph() -> TopicGraph:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = TopicGraph()
    return _GRAPH
