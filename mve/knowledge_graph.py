#!/usr/bin/env python3
"""MVE's knowledge graph: three layers — dimension / tool / table — plus the edges between them.

What MVE's knowledge graph is
-----------------------------
The user's question: "What is MVE's knowledge graph? What problem does it solve? The content it is
based on is really just VLML's data modelling tables."

Compared against the study companion (`knowledge_graph_index.py` / `knowledge_graph_edges.py`):

  The companion's graph = a network of relations between subject knowledge points
    Nodes = knowledge points, carrying `skills` / `question_types` /
           **`typical_misconceptions`** (`knowledge_graph_index.py:99-112`)
    Edges = 10 semantic relations (`knowledge_graph_edges.py:22-35`):
           prerequisite / procedure_step / confusable / analogy / application /
           extends / co_occurs / supports / next / nearby
    Purpose = `build_relevant_subgraph()` (`knowledge_graph_index.py:220`) returns
           "what surrounds this knowledge point", for grading, explaining and question generation

  MVE's graph = a network of relations between data dimensions and the means of retrieving them
    Nodes = dimension / tool / table
    Edges = produced_by (which tool produces this dimension)
           derived_from (which table this dimension is computed from) ← VLML's data modelling
                                        tables enter the graph here
           confusable (confusable dimensions)
           co_occurs (appear together in the same question)
           procedure_step (orchestration order)
    Purpose = **answering "which tool should this dimension call"**

What problem it solves (the only reason to build it)
----------------------------------------------------
`feedback.py` from the previous round can already state "which class of error it was":

    eco_win_rate is not in the observations (I submitted null), it is not a computation error — a
    tool that can produce this dimension has to be called first. The one I had not called is
    pattern_detection_report.

But that sentence "the one I had not called is X" is **reverse-engineered after the fact** — it takes
the difference set between the referee's successful trajectory (`ref_plan`) and mine. That has two
flaws:

1. When the referee never ran (or the cache holds no `ref_plan`), the sentence disappears — recomputing
   historical logs offline is exactly this situation (`Referee orchestration: (none)`);
2. It says "the referee called it and I did not", not "this dimension **ought to** be produced by
   it". If the referee happens to have called one irrelevant extra tool, the difference set points
   in the wrong direction.

The graph turns this into something **queryable up front**:
    eco_win_rate --produced_by--> pattern_detection_report

The source must be **declaration or measurement**, never something the model made up:
- `produced_by`: reads `RubricPoint.answer_spec.tool` (`loop_core.py:185`) — a retrieval recipe
  configured server-side, not a guess;
- `derived_from`: parses the table names out of `answer_spec.sql` and reconciles them against VLML's
  `DATA_DICTIONARY.json` (tables it cannot recognise are not written);
- `confusable`: dimensions in the same question sharing a suffix but differing in prefix
  (pistol_win_rate / eco_win_rate — measured, the model has in fact confused those two).

How to run it
-------------
    python mve/knowledge_graph.py --build          # rebuild and persist
    python mve/knowledge_graph.py --who eco_win_rate
    python mve/knowledge_graph.py --topic pistol_eco_pattern
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

GRAPH_PATH = HERE / "knowledge_graph.json"
# VLML's data modelling tables: 17 in schema + 13 in transformations (the derived-schema agg_*)
DATA_DICT = (HERE.parent / "vlml" / "database" / "DATA_DICTIONARY.json")

# Persisted format version. **Any change to a node's declared fields must bump this by1** —
# otherwise an existing knowledge_graph.json always "looks up to date" (the fingerprint only covers
# tasks.py) and the new declarations never make it into the graph. This is the easiest pitfall of
# "persistence": you wrote the code, yet the graph is still the old one.
SCHEMA_VERSION = 2      # 2 = table nodes carry the VLML modelling declaration, dimension nodes
                        # carry companion-style declarations

# --------------------------------------------------------------------------
# Edge relation types (fewer than the companion's 10: only the ones MVE actually uses)
# --------------------------------------------------------------------------
PRODUCED_BY = "produced_by"      # dimension ← tool
DERIVED_FROM = "derived_from"    # dimension/table ← table
CONFUSABLE = "confusable"        # confusable dimensions (symmetric)
CO_OCCURS = "co_occurs"          # co-occurrence in the same question (symmetric)
PROCEDURE_STEP = "procedure_step"  # orchestration order
# Newly added: VLML's four-layer structure (tool → insight → SQL → table)
READS = "reads"                  # insight ← table
COMPOSES = "composes"            # tool ← insight (this section is produced by it)
FROM_SECTION = "from_section"    # dimension ← a section of the tool
# `application` out of the companion's 10 relations (`knowledge_graph_edges.py:31`):
# knowledge point → application scenario, where `FOCUSED_RELATION_DIRECTION` fixes the focus on the
# **from** end. MVE uses it to record "this insight/tool was actually applied once on that table" —
# a human-imported question is exactly such an "application scenario", while **the question itself
# does not enter the graph** (no node is created for it).
APPLICATION = "application"

SYMMETRIC = frozenset({CONFUSABLE, CO_OCCURS})

# Stage names (data-flow layering) mirror the companion's stage; taken from vlml_schema so the two
# copies cannot drift apart.
# This module only imports the standard library and does not connect to the database, so it is safe
# to import at the top.
try:
    # This module only imports the standard library + reads files, does not connect to the
    # database, so it is safe to import at the top.
    import vlml_schema
    from vlml_schema import STAGE_LABEL, STAGE_ORDER  # noqa: F401
except Exception:                                    # pragma: no cover
    vlml_schema = None                               # type: ignore[assignment]
    STAGE_LABEL, STAGE_ORDER = {}, []

_RE_FROM = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


@dataclass
class GraphBudget:
    """The budget for a subgraph entering the prompt — mirrors the companion's `SubgraphBudget`.

    Companion: focus_topics=3, max_depth=2, max_nodes=20, max_edges=30,
          plus relation_limits throttling per relation.
    Isomorphic here: number of focus dimensions / nodes / edges / how many columns each node shows.
    The purpose is the same in both — the library may grow, the prompt must not grow with it.
    """
    max_focus: int = 8          # focus dimensions (this question's scoring points, top N by weight)
    max_nodes: int = 20
    max_edges: int = 30
    max_cols: int = 14          # how many columns to show at most per table
    max_confusable: int = 3     # how many confusable dimensions per focus dimension
    max_insights: int = 3       # how many insights to expand per section (more drowns the metric line)
    include_confusable: bool = True


# --------------------------------------------------------------------------
# Nodes and edges
# --------------------------------------------------------------------------
def _merge_detail(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Merge two details: lists take the union, dicts the union by key, scalars keep the existing
    value."""
    out = dict(old)
    for k, v in (new or {}).items():
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, list):
            cur = list(out.get(k) or [])
            for item in v:
                if item not in cur:
                    cur.append(item)
            out[k] = cur
        elif isinstance(v, dict):
            cur = dict(out.get(k) or {})
            for dk, dv in v.items():
                if isinstance(dv, list) and isinstance(cur.get(dk), list):
                    for item in dv:
                        if item not in cur[dk]:
                            cur[dk].append(item)
                elif dk not in cur:
                    cur[dk] = dv
            out[k] = cur
        elif k not in out or out.get(k) in (None, "", []):
            out[k] = v
    return out


@dataclass
class Node:
    id: str
    kind: str                      # dimension / tool / table
    label: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "detail": self.detail}


@dataclass
class Edge:
    src: str
    dst: str
    relation: str
    # The source must be traceable: declarative (configured on the rubric) or observed (measured)
    origin: str = "declared"       # declared / observed
    reason: str = ""
    confidence: float = 1.0

    def key(self) -> tuple[str, str, str]:
        a, b = (self.src, self.dst)
        if self.relation in SYMMETRIC:
            a, b = sorted((a, b))
        return (a, b, self.relation)

    def to_dict(self) -> dict[str, Any]:
        return {"from": self.src, "to": self.dst, "relation": self.relation,
                "origin": self.origin, "reason": self.reason,
                "confidence": round(self.confidence, 3)}


class KnowledgeGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self._by_key: dict[tuple[str, str, str], Edge] = {}
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}

    # ---- Construction ----
    def add_node(self, node: Node) -> None:
        """Merge-style write — the same node gets encountered repeatedly across scoring points and
    across questions.

        It used to overwrite outright, and the measured consequence was: `map_fb_conv` appears in
        both fb_conversion_analysis and corrode_collapse, and `map_rounds` has three scoring points
        in one question (Corrode/Haven/Lotus) — the latter overwrote the former entirely, leaving
        only the last scoring point's information on the graph. And dimensions shared across
        questions are precisely the ones that deserve to be recorded in full.
        """
        old = self.nodes.get(node.id)
        if old is None:
            self.nodes[node.id] = node
            return
        if not old.label and node.label:
            old.label = node.label
        old.detail = _merge_detail(old.detail, node.detail)

    def add_edge(self, edge: Edge) -> None:
        # Both ends must already be in the graph — dangling edges turn query results into noise
        if edge.src not in self.nodes or edge.dst not in self.nodes:
            return
        k = edge.key()
        if k in self._by_key:
            old = self._by_key[k]
            # Measured evidence may upgrade a declarative edge, but not the other way round
            if edge.origin == "observed" and old.origin == "declared":
                self.edges.remove(old)
                old = edge
                self._by_key[k] = old
                self.edges.append(old)
            return
        self._by_key[k] = edge
        self.edges.append(edge)
        self._out.setdefault(edge.src, []).append(edge)
        self._in.setdefault(edge.dst, []).append(edge)

    # ---- Queries ----
    def who_produces(self, dimension: str) -> list[str]:
        """Which tools produce this dimension (this is exactly the edge feedback.py was missing)."""
        out = []
        for e in self._in.get(f"dim:{dimension}", []):
            if e.relation == PRODUCED_BY and e.src.startswith("tool:"):
                out.append(e.src[len("tool:"):])
        return sorted(set(out))

    def tables_for(self, dimension: str) -> list[str]:
        """Which tables this dimension is computed from (VLML's modelling tables come in here)."""
        out = []
        for e in self._in.get(f"dim:{dimension}", []):
            if e.relation == DERIVED_FROM and e.src.startswith("table:"):
                out.append(e.src[len("table:"):])
        return sorted(set(out))

    def yields(self, tool: str) -> list[str]:
        """Which dimensions this tool can produce."""
        out = []
        for e in self._out.get(f"tool:{tool}", []):
            if e.relation == PRODUCED_BY and e.dst.startswith("dim:"):
                out.append(e.dst[len("dim:"):])
        return sorted(set(out))

    def confusable_with(self, dimension: str) -> list[str]:
        out = []
        for e in self._out.get(f"dim:{dimension}", []):
            if e.relation == CONFUSABLE and e.dst.startswith("dim:"):
                out.append(e.dst[len("dim:"):])
        for e in self._in.get(f"dim:{dimension}", []):
            if e.relation == CONFUSABLE and e.src.startswith("dim:"):
                out.append(e.src[len("dim:"):])
        return sorted(set(out))

    def dimensions(self) -> list[str]:
        return sorted(n.id[len("dim:"):] for n in self.nodes.values()
                      if n.kind == "dimension")

    # ---- Stages: mirrors the companion's KnowledgeGraphIndex.stage_to_ids ----
    # The companion indexes by stage (primary / junior_high / senior_high) —
    # "what you must already know before learning this". MVE's isomorphic counterpart is the
    # **data-flow order**:
    # what you must already have before computing something (raw → meta → agg → insight → tool →
    # dimension).
    def stage_index(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for n in self.nodes.values():
            st = str(n.detail.get("stage") or "").strip()
            if st:
                out.setdefault(st, []).append(n.id)
        for v in out.values():
            v.sort()
        return out

    def upstream_tables(self, table: str, depth: int = 2) -> list[str]:
        """This table's upstream (where its data comes from) — answering "how the 22 tables relate"."""
        out: list[str] = []
        frontier = [f"table:{table}"]
        for _ in range(max(1, depth)):
            nxt: list[str] = []
            for nid in frontier:
                for e in self._in.get(nid, []):
                    if e.relation == DERIVED_FROM and e.src.startswith("table:"):
                        if e.src not in out and e.src != f"table:{table}":
                            out.append(e.src)
                            nxt.append(e.src)
            frontier = nxt
            if not frontier:
                break
        return sorted(out)

    def tools(self) -> list[str]:
        return sorted(n.id[len("tool:"):] for n in self.nodes.values()
                      if n.kind == "tool")

    def tables(self) -> list[str]:
        return sorted(n.id[len("table:"):] for n in self.nodes.values()
                      if n.kind == "table")

    def subgraph_for(self, dimension: str) -> dict[str, Any]:
        """Mirrors the companion's `build_relevant_subgraph`: given a focus, return the structure around
    it."""
        focus = f"dim:{dimension}"
        if focus not in self.nodes:
            return {"focus": dimension, "nodes": [], "edges": [], "found": False}
        ids = {focus}
        for e in list(self._in.get(focus, [])) + list(self._out.get(focus, [])):
            ids.add(e.src)
            ids.add(e.dst)
        return {
            "focus": dimension,
            "found": True,
            "produced_by": self.who_produces(dimension),
            "derived_from": self.tables_for(dimension),
            "confusable": self.confusable_with(dimension),
            "nodes": [self.nodes[i].to_dict() for i in sorted(ids) if i in self.nodes],
            "edges": [e.to_dict() for e in self.edges
                      if e.src in ids and e.dst in ids],
        }

# ---- Context for the model: the equivalent of control_primitives ----
    # ------------------------------------------------------------------
    def layering_rule(self, dimension: str) -> dict[str, Any] | None:
        """Extract "at which WHERE layer is this column allowed to appear" from the skeleton — used
    for **hard validation**.

    Why it is needed: even with the skeleton, the fatal mistake patterns and the self-check rules
    all written into the prompt, the model still produced the very same mistake five times over
    (pulling `losing_team_name` up into the innermost WHERE). Prompts cannot beat a strong prior, so
    switch to **constraints** instead — which is what the original Voyager does too:
    `process_ai_message` uses babel to assert things about the code's AST (it must be an async
    function, the parameter must be named bot) and retries directly when invalid, rather than
    "reminding the model to pay attention". MVE's isomorphic counterpart is exactly this SQL
    layering assertion.

    The criterion comes from the graph's skeleton (`recipe`), not hard-coded — so it works for a
    different question just the same.
    """
        node = self.nodes.get(f"dim:{dimension}")
        if node is None:
            return None
        recipe = str(node.detail.get("recipe") or "")
        if "PARTITION BY" not in recipe.upper():
            return None
        # Columns used in an "outer WHERE" like the skeleton's `) t WHERE X='?'`
        outer = re.findall(r"\)\s*\w+\s+WHERE\s+([A-Za-z_][\w]*)\s*=", recipe)
        if not outer:
            return None
        rule = {"outer_only": sorted(set(outer)),
                "note": str(node.detail.get("recipe") or "")[:200]}
        # Per-segment computation must be **grouped**: the skeleton's `GROUP BY (rn - grp)`.
        # This was tripped immediately in measurement — after the WHERE position was blocked, the
        # model deleted the GROUP BY along with it while editing, so all losing rounds merged back
        # into one single segment (13 again).
        mg = re.search(r"GROUP\s+BY\s*\(([^)]*)\)", recipe, re.IGNORECASE)
        if mg:
            ids = re.findall(r"[A-Za-z_]\w*", mg.group(1) or "")
            if ids:
                rule["group_by_ids"] = sorted(set(ids))
                rule["group_by_expr"] = mg.group(1).strip()
        return rule

    def dims_of(self, topic_id: str) -> list[str]:
        """Which dimensions this question touches (descending by weight)."""
        return sorted(
            (n.id[len("dim:"):] for n in self.nodes.values()
             if n.kind == "dimension"
             and topic_id in (n.detail.get("topics")
                              or ([n.detail["topic_id"]]
                                  if n.detail.get("topic_id") else []))),
            key=lambda d: -float((self.nodes[f"dim:{d}"].detail.get("weight")
                                  or 0)),
        )
    # The original Voyager splices control_primitives' **source code** into the system message
    # (voyager/agents/action.py:render_system_message): the model sees not just "which functions
    # exist" but also **how each is implemented**. The isomorphic thing in MVE is the subgraph
    # rendering: it does not merely say "there is a query_sql", it spells out "how this metric is
    # computed, which table it reads, which columns it uses, and where others usually go wrong".
    #
    # The budget mirrors the companion's `SubgraphBudget` (around knowledge_graph_index.py):
    #   focus_topics=3, max_depth=2, max_nodes=20, max_edges=30
    # Squeezing the whole graph into the prompt dilutes attention and drags irrelevant (confusable)
    # dimensions in along with it.
    # ------------------------------------------------------------------
    def subgraph_for_topic(self, topic_id: str,
                           budget: "GraphBudget | None" = None) -> dict[str, Any]:
        """Mirrors the companion's build_relevant_subgraph: given a question, return the structure
        around it."""
        b = budget or GraphBudget()
        focus = sorted(
            (n for n in self.nodes.values()
             if n.kind == "dimension"
             and topic_id in (n.detail.get("topics")
                              or ([n.detail["topic_id"]]
                                  if n.detail.get("topic_id") else []))),
            key=lambda n: -float(n.detail.get("weight") or 0),
        )[:b.max_focus]

        if not focus:
            return {"topic": topic_id, "found": False, "nodes": [], "edges": []}

        ids: list[str] = []
        for n in focus:
            ids.append(n.id)
            # the tools that produce it + the tables it comes from (depth=1)
            ids += [f"tool:{t}" for t in self.who_produces(n.id[len("dim:"):])]
            ids += [f"table:{t}" for t in self.tables_for(n.id[len("dim:"):])]
            # confusable dimensions (depth=1, but only directly connected ones)
            if b.include_confusable:
                ids += [f"dim:{c}" for c in
                        self.confusable_with(n.id[len("dim:"):])][:b.max_confusable]

        # dedupe while preserving order + truncate
        seen, ordered = set(), []
        for i in ids:
            if i in self.nodes and i not in seen:
                seen.add(i)
                ordered.append(i)
        ordered = ordered[:b.max_nodes]

        edges = [e.to_dict() for e in self.edges
                 if e.src in seen and e.dst in seen][:b.max_edges]
        return {
            "topic": topic_id,
            "found": True,
            "focus_dims": [n.id[len("dim:"):] for n in focus],
            "nodes": [self.nodes[i].to_dict() for i in ordered],
            "edges": edges,
        }

    # ---- Seed layer: any question stem can be matched to the graph (mirrors the companion's
    #      match_topics) ----
    def facts(self) -> list[dict[str, Any]]:
        """The graph's "complete set of metrics". Unrelated to any question, determined solely by
        the VLML structure."""
        try:
            import graph_topics
        except Exception:
            return []
        out = []
        for n in self.nodes.values():
            if n.kind != "fact":
                continue
            d = dict(n.detail)
            d["id"] = n.id
            d["label"] = n.label
            d["aliases"] = [a for a in (d.get("aliases") or [])
                            if isinstance(a, str)]
            out.append(d)
        return out

    def subgraph_for_query(self, query: str, *, mode: str = "plan",
                           limit: int = 3) -> dict[str, Any]:
        """Question stem → focus fact → one-hop subgraph (per-relation throttling + direction awareness +
    dropped-edge counts).

    This is the entry point of "the graph serves every question": a question **need not** have been
    generated before, nor have a matching dimension in the graph — as long as its stem text matches
    a seed fact, a subgraph can be produced. The companion matches graphs for any new question the
    same way (`build_knowledge_guidance_payload`).
    """
        try:
            import graph_topics
        except Exception:
            return {"query": query, "found": False, "reason": "graph_topics unavailable"}
        matches = graph_topics.match_facts(self.facts(), query=query, limit=limit)
        if not matches:
            return {"query": query, "found": False,
                    "reason": "the stem matched no metrics at all (no hit at the seed layer)"}
        contexts = []
        for m in matches:
            ctx = graph_topics.focused_context(self, m["id"], query=query,
                                               mode=mode)
            ctx["score"] = m.get("score", 0)
            ctx["matched_terms"] = m.get("matched_terms") or []
            contexts.append(ctx)
        return {
            "query": query,
            "found": True,
            "mode": mode,
            "matches": matches,
            "contexts": contexts,
            "seed_facts": len(self.facts()),
        }

    def render_for_query(self, query: str, *, mode: str = "plan",
                         limit: int = 3) -> str:
        """Render "the subgraph matched from the question stem" as text for the model.

    Division of labour with `render_for_prompt`:
        - `render_for_prompt(topic_id)` — questions that have been generated, going through
          rubric-derived dimensions (the most complete information)
        - `render_for_query(stem)`       — **any question**, going through seed facts (guarantees
          there is something to give)
    The two can coexist: the latter is the fallback, and also the proof that "the graph really does
    serve every question".
    """
        sub = self.subgraph_for_query(query, mode=mode, limit=limit)
        if not sub.get("found"):
            return ""
        bucket_title = {
            "produced_by": "which tool produces it",
            "section": "which section of the return value to take",
            "composes": "which computation units compose it",
            "reads": "which tables those units read",
            "derived_from": "which tables the data comes from",
            "confusions": "what it is easily confused with",
            "review_with": "what it often appears together with",
        }
        lines = ["[Knowledge graph · matched by question stem] Below are the metric definitions "
                 "derived from the VLML structure; they take priority over your guesses. '?' is a "
                 "redacted placeholder — the real values must be looked up by you."]
        for ctx in sub["contexts"]:
            if not ctx.get("focus", {}).get("label"):
                continue
            lines.append(f"· {ctx['focus']['label']} (match score {ctx.get('score', 0)})")
            for bucket, title in bucket_title.items():
                vals = ctx.get(bucket) or []
                if vals:
                    lines.append(f"   {title}: {', '.join(vals[:6])}")
            d = (ctx.get("summary") or {}).get("diagnostics") or {}
            dropped = sum(int(v or 0) for v in d.values())
            if dropped:
                lines.append(f"   ({dropped} edges were pruned under the {ctx.get('mode')} mode: "
                             + ", ".join(f"{k}={v}" for k, v in d.items() if v)
                             + ")")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def render_for_prompt(self, topic_id: str,
                          budget: "GraphBudget | None" = None,
                          *, with_recipe: bool = True,
                          with_formula: bool = True,
                          with_section: bool = True,
                          with_stage: bool = True,
                          with_insight: bool = True) -> str:
        """Render the subgraph as text for the model — this is MVE's control_primitives.

    `with_formula` = the master switch for the metric-definition layer (how it is computed /
    which column to take / the skeleton).
    In the companion this block is **optional**: it can be withheld entirely (see the evidence in
    the comment block below).
    Giving only the structural layer = the companion's "knowing which tables exist and which
    columns they have", **without telling it how to write the answer**.
    """
        b = budget or GraphBudget()
        sub = self.subgraph_for_topic(topic_id, b)
        if not sub.get("found"):
            # There is no focus for this question in the dimension layer (dimensions of runtime
            # questions **create no nodes**, only edges)
            # → do not return an empty string; that would amount to removing the scaffold entirely.
            # The scaffold information is on the edges.
            # Measured: without this fallback, the graph prompt for `plant_success_rate_lotus` had
            # length 0.
            return _render_from_links(topic_id, self, with_formula=with_formula)

        by_id = {n["id"]: n for n in sub["nodes"]}
        lines: list[str] = []
        # Columns used by the focus dimensions → sorted first when showing table structures
        # (otherwise they get cut off by the column budget; measured, fb_team_won was squeezed out
        # by the 14-column budget even though it is exactly the column to be AVG'd)
        # Recorded per table: misattributing same-named columns (series_id) yields a Binder Error.
        priority_cols: dict[str, list[str]] = {}
        for dim in sub["focus_dims"]:
            node = by_id.get(f"dim:{dim}")
            if node is None:
                continue
            d = node["detail"]
            head = f"· Dimension {dim} (weight {d.get('weight')})"
            lines.append(head)
            by_topic = (d.get("points_by_topic") or {}).get(topic_id) or []
            pts = by_topic or d.get("points") or (
                [d["point"]] if d.get("point") else [])
            if pts:
                lines.append(f"   What to retrieve: {'; '.join(str(p) for p in pts[:3])}")
            # ---- Metric-definition layer: **the answer template**, must be removable as a whole ----
            #
            # The isomorphic position in the companion: `model_context` at
            # `knowledge_graph_guidance.py:1088-1104` does have a `procedure` bucket, but what it
            # gives is a "step description", and the whole graph block is an **optional** segment in
            # the question-generation prompt — `llm_prompts.py:285-303` drops it **entirely** when
            # over the token budget, and the grading contract (`prompt_templates.py:170-189`,
            # sixteen requirements) **never mentions the graph at all**.
            #
            # MVE previously lacked this layer: "how to compute it / which column to take / the full
            # SQL skeleton" is resident context, independent of mastery and coverage — so for a
            # question practised up to 100% coverage and 0.9 mastery, Voyager still gets the answer
            # template, and practice scores are bound to be pinned at the ceiling
            # (run_log 8/8 all 100, exam_log 26/26 all 100 — both curves are fake flat lines).
            #
            # So the metric-definition layer is not "a scaffold", it is a **criterion**: it may only
            # be given while the student does not know it yet (hint=full); once it does know it,
            # it must be withdrawn (hint=partial / none).
            if with_formula and d.get("semantics"):
                lines.append(f"   How it is computed: {d['semantics']}")
            vcs = d.get("value_columns") or []
            if with_formula and vcs:
                lines.append("   Take the column(s) "
                             + ", ".join(str(v) for v in vcs)
                             + " of the result (counting from 0)")
            if with_formula and with_recipe and d.get("recipe"):
                lines.append(f"   Structural skeleton: {d['recipe']}")
                lines.append("   ⚠ The **position** of WHERE in the skeleton is the metric "
                             "definition: number in the inner layer first, filter in the outer one. "
                             "Do not merge every filter condition into the innermost layer — once "
                             "merged, the segments break apart / get joined wrongly.")
                lines.append(f"   Self-check: {SELF_CHECK_NOTE}")
            tool = d.get("tool")
            if tool:
                tnode = by_id.get(f"tool:{tool}") or {}
                req = tnode.get("detail", {}).get("required_params") or []
                hint = tnode.get("detail", {}).get("param_hint") or ""
                lines.append(
                    f"   Retrieval tool: {tool} (take the value at {d.get('value_path')})"
                    + (f"; **required parameters**: {', '.join(req)}" if req else "")
                    + (f"; note: {hint}" if hint else ""))
            if d.get("tables"):
                # Must state outright that "a table name is not a tool name": measured, the model
                # took the graph's `from table: agg_first_blood_stats` and wrote it straight into
                # calls[].tool, failing plan validation three times and not landing a single tool
                # call in the whole round.
                lines.append(f"   Source tables (you must query these yourself with query_sql — "
                             f"**a table name is not a tool name**): "
                             f"{', '.join(d['tables'])}")
            if d.get("tool") == "query_sql" and not d.get("tables"):
                lines.append("   Retrieval tool: query_sql")
            cols = d.get("columns") or []
            if cols:
                lines.append(f"   Columns used: {', '.join(cols[:b.max_cols])}")
            for tbl, cs in (d.get("columns_by_table") or {}).items():
                cur = priority_cols.setdefault(tbl, [])
                for c in cs:
                    if c not in cur:
                        cur.append(c)
            if d.get("typical_errors"):
                lines.append("   Typical mistakes:"
                            + "; ".join(str(e) for e in d["typical_errors"][:4]))
            # Units: the companion's knowledge points carry a unit natively, MVE had none before —
            # so whether the model computed a "conversion rate" as a fraction or as a percentage
            # came down to luck (measured, it submitted both 0.55 and 55).
            if d.get("unit"):
                lines.append(f"   Unit: {d['unit']}"
                             + (" (a percentage, not a fraction)" if d["unit"] == "%" else ""))
            if d.get("skills"):
                lines.append(f"   SQL skills needed: {', '.join(d['skills'])}")

        # Table structures: only the tables the focus dimensions genuinely read (the one most
        # needed when writing SQL)
        # Carrying **which data-flow layer it sits in and what its upstream is** — this is "how the
        # 22 tables relate"; previously the graph held only table and column names, so you could not
        # tell that agg_player_game_stats is aggregated from agg_player_round_stats.
        tbl_nodes = [n for n in sub["nodes"] if n["kind"] == "table"]
        for t in tbl_nodes:
            cols = list(t["detail"].get("columns") or [])
            if not cols:
                continue
            ordered = [c for c in priority_cols.get(t["label"], []) if c in cols]
            ordered += [c for c in cols if c not in ordered]
            stage = str(t["detail"].get("stage") or "")
            up = [u[len("table:"):] for u in
                  self.upstream_tables(t["label"], depth=2)]
            lines.append(
                f"· Table {t['label']}"
                + (f"［{STAGE_LABEL.get(stage, stage)}］" if (stage and with_stage)
                   else "")
                + f"({t['detail'].get('grain') or ''}"
                + (f"; modelling grain {gdoc}" if (gdoc := t["detail"].get("grain_doc")) else "")
                + (f"; primary key {', '.join(pk)}" if (pk := t["detail"].get("pk") or []) else "")
                + f", {t['detail'].get('rows', 0)} rows)"
                + (f" upstream: {', '.join(up)}" if (up and with_stage) else "")
                + f"\n   Columns: {', '.join(ordered[:b.max_cols])}"
+ (f" …{t['detail'].get('n_columns', 0)} columns in total"
                   if t["detail"].get("n_columns", 0) > b.max_cols else ""))
            # Column **meanings**: the metric definitions written in VLML's modelling doc.
            # Previously there were only column names, so the model could only guess from common
            # sense (guessed fb_player as player_name, map as something else) and needed three
            # collisions to get it right.
            desc = t["detail"].get("column_desc") or {}
            if desc:
                named = [c for c in ordered[:b.max_cols] if c in desc]
                if named:
                    named = named[:b.max_cols]
                    lines.append("   Column meanings:"
                                 + "; ".join(
                                     f"{c}={str((desc[c] or {}).get('desc') or '')[:60]}"
                                     for c in named[:6]))
            if t["detail"].get("purpose"):
                lines.append(f"   What this table is for: {t['detail']['purpose']}")

        # Which section to take: the head of value_path is the tool's section (the insight
        # directory).
        # Without this layer the model knows to call match_analysis_report but not that the value
        # lives in key_metrics rather than team_comparison.
        for dim in sub["focus_dims"]:
            if not with_section:
                break
            node = by_id.get(f"dim:{dim}")
            if node is None:
                continue
            sec = node["detail"].get("section")
            declared = str(node["detail"].get("tool") or "")
            if not sec:
                continue
            # The FROM_SECTION edge may not be inside the subgraph (the tool on that edge is not
            # necessarily the produced_by tool), so query the whole graph directly; and **only
            # accept the tool it declares itself** — sections of different tools can share a name
            # (there are two key_metrics), and not respecting that points at the wrong tool.
            tools = sorted({
                e.src[len("tool:"):]
                for e in self._in.get(f"dim:{dim}", [])
                if e.relation == FROM_SECTION and e.src.startswith("tool:")
                and (not declared or e.src == f"tool:{declared}")
            })
            if tools:
                lines.append(f"· {dim} is taken from the `{sec}` section of "
                             f"{', '.join(tools)}"
                             f" (not any position in the returned package)")
                if with_insight:
                    lines.extend(self._insight_lines(dim, sec, tools, b))

        confusables = [e for e in sub["edges"] if e["relation"] == CONFUSABLE]
        if confusables:
            pairs = sorted({tuple(sorted((e["from"][4:], e["to"][4:])))
                            for e in confusables})
            lines.append("· Confusable dimensions:"
                         + "; ".join(f"{a} ↔ {b}" for a, b in pairs[:6]))

        if not lines:
            return ""
        return ("[Knowledge graph · subgraph relevant to this question] The metric definitions below "
                "come from the server-side recipes and take priority over your guesses; '?' is a "
                "redacted placeholder — the real values must be looked up by you.\n"
                + "\n".join(lines))

    def _insight_lines(self, dim: str, sec: str, tools: list[str],
                       b: "GraphBudget") -> list[str]:
        """The insight layer: which SQL sits behind this section, and which tables they read.

    Why this layer is needed: the `key_metrics` section is actually composed of 5 insights
    (team_round_metrics / team_economy_pistol / …), each reading a different table. Without knowing
    this layer the model can only "call the tool + rummage through the return value", and when it
    rummages wrong it submits null.
        """
        out: list[str] = []
        for tool in tools[:1]:
            node = self.nodes.get(f"tool:{tool}")
            if node is None:
                continue
            si = (node.detail.get("section_insights") or {}).get(sec) or []
            if not si:
                continue
            parts = []
            for name in si[:b.max_insights]:
                inode = self.nodes.get(f"insight:{name}")
                if inode is None:
                    parts.append(name)
                    continue
                purpose = str(inode.detail.get("purpose") or "")
                # What is stored in tables may be a bare table name or carry the "table:" prefix —
                # measured, slicing len("table:")=6 directly would cut agg_player_round_stats into
                # "ayer_round_stats", so the prefix must be checked first.
                tbls = [t[6:] if t.startswith("table:") else t
                        for t in inode.detail.get("tables") or []]
                tbls = [t for t in tbls if t]
                parts.append(f"{name}"
                             + (f" ({purpose})" if purpose else "")
                             + (f"→reads {','.join(tbls[:3])}" if tbls else ""))
            if parts:
                out.append(f"   The `{sec}` section is composed of {len(si)} insights: "
                           + "; ".join(parts))
        return out

    # ---- Persistence ----
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "summary": {
                "dimensions": len(self.dimensions()),
                "tools": len(self.tools()),
                "tables": len(self.tables()),
                "edges": len(self.edges),
                # Cache-staleness criterion: see `_stale` in `load()`.
                # Both fingerprints are mandatory: a change in the questions requires a rebuild
                # (tasks.py), and so does a change in VLML's modelling doc (the declarations come
                # from the doc, not from the questions).
                "task_fingerprint": _task_fingerprint(),
                "model_fingerprint": _model_fingerprint(),
            },
        }

    def save(self, path: Path = GRAPH_PATH) -> None:
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1),
                        encoding="utf-8")

    @classmethod
    def load(cls, path: Path = GRAPH_PATH) -> "KnowledgeGraph":
        g = cls()
        if not path.exists():
            return g
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        # Staleness guard: the graph is a **persisted cache** of `build()`. Add a new question without
        # rebuilding and the dimension never makes it into the graph — measured, that is exactly
        # what happened with `kast_adr_check`: the graph held no kast_pct / kd_ratio, the whole
        # prompt section was empty, and the model retried the same wrong branch three rounds
        # running. The cache has to know it is stale on its own.
        #
        # Three criteria, and missing any one of them leaks:
        #   task fingerprint   — questions added/changed (answer_spec in tasks.py changed)
        #   model fingerprint  — VLML's modelling doc changed (the table/dimension node declarations
        #                        come from the doc)
        #   format version     — the node declaration fields themselves changed (questions and doc
        #                        both unchanged, but **the code** changed)
        if _stale(raw):
            try:
                g = build()
                g.save(path)
                return g
            except Exception:
                pass          # if the rebuild fails, fall back to the old graph — better than nothing
        for n in raw.get("nodes") or []:
            g.add_node(Node(id=n["id"], kind=n["kind"],
                            label=n.get("label", ""), detail=n.get("detail") or {}))
        for e in raw.get("edges") or []:
            g.add_edge(Edge(src=e["from"], dst=e["to"], relation=e["relation"],
                            origin=e.get("origin", "declared"),
                            reason=e.get("reason", ""),
                            confidence=float(e.get("confidence") or 1.0)))
        return g


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------
def _task_fingerprint() -> str:
    """The "question → dimension → declaration path" fingerprint in tasks.py.

    The graph's inputs are exactly what this fingerprint covers; a changed fingerprint means
    questions were added or changed, so the cache must be rebuilt.
    """
    try:
        from tasks import TASKS
    except Exception:
        return ""
    parts = []
    for topic_id in sorted(TASKS):
        for p in (getattr(TASKS[topic_id], "rubric", None) or []):
            s = getattr(p, "answer_spec", None)
            parts.append(
                f"{topic_id}|{p.dimension}|"
                f"{getattr(s, 'tool', '') or ''}:{getattr(s, 'value_path', '') or ''}"
                f":{getattr(s, 'sql', '') or ''}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _model_fingerprint() -> str:
    """The fingerprint of VLML's four modelling documents (`vlml_schema.model_specs()`).

    The table node and dimension node declarations are parsed out of these documents — when the
    documents change, the graph must be rebuilt.
    """
    try:
        return str(vlml_schema.model_specs().get("fingerprint") or "")
    except Exception:
        return ""


def _stale(raw: dict[str, Any]) -> bool:
    """Three criteria: format version / task fingerprint / modelling-doc fingerprint."""
    if int(raw.get("schema_version") or 0) != SCHEMA_VERSION:
        return True
    summary = raw.get("summary") or {}
    fp = _task_fingerprint()
    if fp and str(summary.get("task_fingerprint") or "") != fp:
        return True
    mf = _model_fingerprint()
    # An empty model fingerprint (the doc could not be read) is not treated as stale — otherwise
    # every load rebuilds, which is slow and noisy
    if mf and str(summary.get("model_fingerprint") or "") != mf:
        return True
    return False


def _create_table_columns(text: str) -> dict[str, list[str]]:
    """Parse column names out of CREATE TABLE statements (schema/*.sql is one of the sole
    authoritative sources).

    Why it is needed: VLML's `DATA_DICTIONARY.json` holds only 13 tables and **excludes the derived
    tables** (agg_first_blood_stats is not among them). Yet all four scoring points of
    fb_conversion_analysis read that table — without this the graph cannot supply column names and
    Voyager can only guess (measured, it guessed non-existent columns like `fb_conv` /
    `round_number`).

    Takes only the first segment between "CREATE TABLE name (" and its matching ");", takes the
    leading identifier of each line, and skips comment lines and constraint lines
    (PRIMARY/FOREIGN/UNIQUE/CHECK/CONSTRAINT).
    """
    out: dict[str, list[str]] = {}
    for m in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)\s*\(",
            text, re.IGNORECASE):
        name, start = m.group(1), m.end()
        depth, i = 1, start
        while i < len(text) and depth > 0:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        body = text[start:i - 1] if depth == 0 else text[start:start + 2000]
        cols: list[str] = []
        for raw in body.split("\n"):
            line = raw.split("--")[0].strip().rstrip(",")
            if not line:
                continue
            head = line.split()[0].strip('"`[]')
            if head.upper() in {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK",
                                "CONSTRAINT", "INDEX"}:
                continue
            if not re.fullmatch(r"[A-Za-z_][\w]*", head or ""):
                continue
            if head not in cols:
                cols.append(head)
        if cols:
            out[name] = cols
    return out


def _known_tables() -> dict[str, dict[str, Any]]:
    """VLML's data modelling tables → {grain, columns, pk, source}. Table names that cannot be
    recognised do not enter the graph.

    All three sources must be scanned; scanning only one misses things:
    - `DATA_DICTIONARY.json`: 13 tables, with grain/field descriptions, but **excluding derived
      tables**;
    - `database/schema/*.sql`: 17 CREATE TABLE statements (including agg_first_blood_stats), whose
      column names are parsed by `_create_table_columns`;
    - `database/transformations/*.sql`: 13 derived transformations, where the file name is the table
      name (a numeric prefix like `01_` must be stripped) — at least this lets the table name into
      the graph.

    Measured consequence of missing them: all four scoring points of fb_conversion_analysis read
    `agg_first_blood_stats`, which is not in the data dictionary → every derived_from edge is lost,
    only one table remains in the graph, and Voyager has no column names at all to write SQL with.

    The return structure was upgraded from "table name → description string" to "table name →
    structural dict": the graph must be able to answer "which columns does this table have", and
    grain alone is not enough.
    """
    out: dict[str, dict[str, Any]] = {}
    if DATA_DICT.exists():
        try:
            rows = json.loads(DATA_DICT.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            rows = []
        for row in rows if isinstance(rows, list) else []:
            name = str(row.get("table") or "").strip()
            if not name:
                continue
            cols = row.get("columns")
            col_names = [str(c) for c in cols] if isinstance(cols, dict) else []
            out[name] = {
                "grain": str(row.get("grain") or "").strip()
                         or f"{len(col_names)} columns",
                "columns": col_names,
                "pk": [str(p) for p in (row.get("pk") or [])],
                "source": "DATA_DICTIONARY.json",
            }

    for sub in ("schema", "transformations"):
        for path in sorted((DATA_DICT.parent / sub).glob("*.sql")):
            name = re.sub(r"^\d+_", "", path.stem)
            if not name:
                continue
            parsed: dict[str, list[str]] = {}
            try:
                if sub == "schema":
                    # Measured: one of the server's schema/*.sql files is GBK-encoded (starts with
                    # 0xa3, Chinese comments), while the local copy happened to be all UTF-8, so it
                    # was never exposed. Column names are pure ASCII, so replacing the bad bytes
                    # does not affect the parse result.
                    parsed = _create_table_columns(
                        path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                parsed = {}
            cols = parsed.get(name) or []
            if name not in out:
                out[name] = {
                    "grain": (f"defined in {sub} ({path.name})" if not cols
                              else f"{len(cols)} columns · defined in {sub}"),
                    "columns": cols,
                    "pk": [],
                    "source": f"{sub}/{path.name}",
                }
            elif not out[name]["columns"] and cols:
                # The data dictionary has a grain but no columns (or empty columns) → fill them in
                # from the CREATE TABLE statement
                out[name]["columns"] = cols
                out[name]["source"] += f" + {sub}/{path.name}"
    return out


def _tables_in_sql(sql: str, known: dict[str, Any]) -> list[str]:
    """Extract table names from SQL, keeping only the tables the data dictionary recognises (so CTE
    names are not mistaken for tables)."""
    found = []
    for name in _RE_FROM.findall(sql or ""):
        if name.lower() in {"select", "where", "group", "order", "limit"}:
            continue
        if name in known:
            found.append(name)
    return sorted(set(found))


# --------------------------------------------------------------------------
# Metric semantics: turning answer_spec into "how this metric is computed / where to read it from"
# --------------------------------------------------------------------------
# Why this block is mandatory (this is the core change of this round):
#
#   TOOL_CATALOG only gave tool signatures and a few tables' column names, and never said **what
#   the metric itself is**. Measured on corrode_collapse round 2: the model duly called query_sql
#   by name, but wrote `fb_conv` (the metric to compute) into the SELECT as if it were a column
#   name, and treated `round_number` as a column of agg_first_blood_stats — it knew what to look
#   up but not how that number comes about.
#
#   How does the original Voyager solve the same class of problem? It splices control_primitives'
#   **source code** into the system message — the model sees each skill function's implementation
#   rather than just its name. MVE's isomorphic counterpart is right here: hand over each
#   dimension's "formula / value path / which columns it uses".
#
#   The answer-leak boundary: **every literal in the SQL is redacted to '?'**. Metric definitions
#   and structure are given; answers are not. (WHERE series_id='?' AND map_name='?' only says
#   "filter by these two columns" — which map and which team it must look up itself.)
# --------------------------------------------------------------------------

_LITERAL_RE = re.compile(r"'[^']*'")
_WS_RE = re.compile(r"\s+")


def _desensitize(sql: str) -> str:
    """Redact the string literals in SQL — metric definitions are given, values are not."""
    return _LITERAL_RE.sub("'?'", sql or "")


def _sql_semantics(sql: str) -> str:
    """The outermost SELECT expression (including aliases), desensitized and truncated."""
    m = re.search(r"\bselect\b(.*?)\bfrom\b", sql or "", re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    expr = _WS_RE.sub(" ", _desensitize(m.group(1))).strip().strip(",")
    return expr[:200]


def _sql_recipe(sql: str, limit: int = 700) -> str:
    """The desensitised skeleton of the whole SQL — only complex metrics (window functions /
    nesting) need it.

    `semantics` gives only the outermost SELECT, which fails to convey the structure for the
    gaps-and-islands style (max_losing_streak groups with ROW_NUMBER() OVER (PARTITION BY ...)) —
    and that is exactly where 18 rounds of 0% got stuck. Handing over the full skeleton is what
    lets the model know "a losing streak needs a window function to segment, not just a COUNT".

    Why limit is 700 rather than 400: measured, 400 truncates the skeleton's tail into
    `... ORDER BY cnt DESC, st…` — half an SQL is worse than none, because the model can only
    guess the second half itself. The skeleton is given either in full or not at all.
    """
    if not sql:
        return ""
    flat = _WS_RE.sub(" ", _desensitize(sql)).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _has_structure(sql: str) -> bool:
    """Does this SQL have any "structure" (nested subquery / window function)? If not, only
    semantics is given."""
    return bool(re.search(r"\bover\s*\(|\bfrom\s*\(|\brow_number\b|\brank\s*\(|"
                          r"\bdense_rank\b|\blag\s*\(|\blead\s*\(",
                          sql or "", re.IGNORECASE))


def _used_columns(sql: str, tables: list[str],
                  known: dict[str, Any]) -> dict[str, list[str]]:
    """Which columns this SQL genuinely uses **on each table**.

    Rather than guessing identifiers with regexes (guessed columns pick up function aliases and
    keywords), every known column name is matched with `\b`. The column lists come from
    DATA_DICTIONARY / the CREATE TABLE statements, so they are traceable.

    Returns "table → columns" rather than one flat list: two tables can have identically named
    columns (series_id), and merging them charges table A's columns to table B's header, which makes
    the model write a Binder Error.
    """
    out: dict[str, list[str]] = {}
    for t in tables:
        cols = (known.get(t) or {}).get("columns") or []
        hits = [c for c in cols
                if re.search(r"\b" + re.escape(c) + r"\b", sql or "")]
        if hits:
            out[t] = hits[:24]
    return out


# --------------------------------------------------------------------------
# Typical mistakes: mirrors the companion's `typical_misconceptions` in knowledge_seeds
# --------------------------------------------------------------------------
# How the companion writes them (`static/knowledge_seeds/history.json`):
#   "typical_misconceptions": ["conflating the commandery system with the enfeoffment system", ...]
# Characteristics: ① **hand-maintained seeds**, not model-generated; ② they state "the mistake",
#      not "the correct answer"; ③ they are read in question generation, explanation and cognitive
#      diagnosis alike (`targeted_question_contract.py:27-29`).
#
# MVE's isomorphic counterpart is right here: the pitfalls each dimension has actually produced.
# The first four come from measured logs (53-round diagnosis: max_losing_streak computed as 14/13
# when the truth was 8, start_round computed as 1 when the truth was 7); the rest come from the
# three classes of hallucination recorded in LOCAL_SQL_NOTES.
#
# This is not "the answer", it is "a list of traps" — showing it to the model is the same as the
# companion listing common mistakes on the knowledge point.
# --------------------------------------------------------------------------
# The dict keys are dimension names looked up in the graph — never translate the keys, only the
# human-readable values.
DIM_PITFALLS: dict[str, list[str]] = {
    "max_losing_streak": [
        # The fatal mistake patterns are ordered by **measured hit rate**, not by
        # "generic → specific". Rendering takes only the first N, so putting the most fatal ones
        # last is the same as not writing them — measured, when this one sat at position 4 it was
        # sliced off by [:3] and the model reproduced the very same old mistake.
        # Symptom: it computed 13 when the truth was 8. It copied the skeleton faithfully but moved
        # the team filter **forward** into the innermost WHERE — at that point only Cloud9's losing
        # rounds remained, the numbering came out contiguous, and the whole thing was treated as
        # one segment. Order is part of the metric definition: the inner layer filters by
        # series+map only and numbers first; the outer layer filters the target team's losses.
        "moving the losing_team_name filter forward into the innermost WHERE — **all** rounds must "
        "be numbered first (the inner layer filters by series_id + map_name only), and only then "
        "does the outer layer pick the target team's losing rounds; filtering early joins all "
        "losing rounds into one single segment",
        "treating \"how many rounds the team lost in total\" as the longest losing streak (it must "
        "count a **consecutive** run)",
        "accumulating across maps: round numbers are ordered over the whole series, so it must be "
        "split by map_name first",
        "resetting per half (round 12) — the metric definition does not reset",
    ],
    "streak_start_round": [
        "using MIN(round_number) for the start — that yields the smallest round number of the whole "
        "match, not the start of the **longest** run",
        "same thing: moving the team filter into the inner layer makes the run lengths wrong, and "
        "the start is wrong with them",
        "taking the start of a different (shorter) run",
    ],
    "conversion.fb_conv": [
        "treating the first-blood count as the conversion rate (conversion rate = first bloods won "
        "/ total first bloods)",
        "forgetting to multiply by 100 (the question asks for a percentage 0-100, such as 25.0)",
        "comparing fb_team_won as a boolean string instead of AVG-ing it",
    ],
    "map_fb_conv": [
        "computing the per-map conversion rate from whole-series data (a map_name filter is "
        "mandatory)",
        "using the first-blood count of the whole series as the denominator",
    ],
    "opening_duels.fb": [
        "taking the whole-database first-blood count as this team's (it must be filtered by fb_team)",
    ],
    "map_rounds": [
        "double counting when summing across the three maps (one rounds row = one round, so count "
        "groups by map_name)",
    ],
    "map_win_rate": [
        "computing a round win rate from the games table's win/loss (the win rate is **round-level** "
        "and needs the rounds table)",
        "forgetting to multiply by 100",
    ],
    "total_rounds": [
        "double counting by adding the round counts of two maps",
        "counting rounds from the games table (one games row = one map)",
    ],
    "rounds_won": [
        "counting rounds with games.winning_team_name (that is the whole-map winner, not a round "
        "count)",
    ],
    "pistol_win_rate": [
        "confusing it with eco_win_rate (two different keys under the same key_metrics.economy)",
        "hard-computing it from rounds by hand (the tool already has the aggregation internally, "
        "so take key_metrics directly)",
    ],
    "eco_win_rate": [
        "confusing it with pistol_win_rate",
    ],
    "pattern_rounds": [
        "taking the whole-series round count as the sample size (take scope.rounds from the tool's "
        "return value)",
    ],
    "pattern_confidence": [
        "inventing a confidence label of its own — it must be taken verbatim from the tool's "
        "returned scope.confidence",
        "treating confidence as a number (it is moderate / strong / weak / insufficient)",
    ],
}


# Skeletons **with inline comments**: an auto-extracted skeleton can only show a string, it cannot
# say "what belongs on which layer".
#
# Why a hand-written version is needed: measured, even with the complete skeleton plus the generic
# warning "the WHERE position is the metric definition" all handed over, the model still produced
# the very same mistake four times — moving `losing_team_name='Cloud9'` forward into the innermost
# WHERE. It read the skeleton but merged the three WHERE layers as "three equivalent filter
# conditions". A generic warning cannot beat that prior; only a comment **on the exact line it got
# wrong** stops it.
#
# Same source as DIM_PITFALLS: hand-maintained seeds, not model-generated.
DIM_RECIPE_NOTES: dict[str, str] = {
    "max_losing_streak":
        "SELECT cnt AS max_streak, start_round FROM ("
        " SELECT COUNT(*) AS cnt, MIN(round_number) AS start_round FROM ("
        "  SELECT round_number, losing_team_name,"
        " ROW_NUMBER() OVER (ORDER BY round_number) AS rn,"
        " ROW_NUMBER() OVER (PARTITION BY losing_team_name ORDER BY round_number) AS grp"
        "  FROM rounds WHERE series_id='?' AND map_name='?'"
        "  /* ↑ inner layer: only series_id + map_name allowed. Writing losing_team_name here is"
        "     wrong throughout */"
        " ) t WHERE losing_team_name='?'"
        " /* ↑ outer layer: the team filter may only go here, after the numbering */"
        " GROUP BY (rn - grp)"
        ") x ORDER BY cnt DESC, start_round ASC LIMIT 1",
    "streak_start_round":
        "same as above (the same SQL, take column 1): the inner WHERE holds only series_id + "
        "map_name, and losing_team_name must come after the outer ) t WHERE",
}

# Self-check rule: give a check **it can decide by itself**, rather than a sentence saying
# "don't get it wrong".
# Merely warning "do not merge" offers no actionable criterion; with this rule it can compare by
# itself after writing the SQL.
SELF_CHECK_NOTE = (
    "Once written, self-check: only series_id and map_name may appear in the innermost WHERE (the "
    "one right after FROM); if the team column being filtered (losing_team_name / "
    "winning_team_name) shows up there, delete it — it belongs to the outer layer. How to check: "
    "first COUNT on its own how many rounds that team lost on this map; if your max_streak comes "
    "out exactly equal to that number, you have not segmented."
)


def _required_params(tool: str) -> list[str]:
    """Which parameters this tool **requires** (those without a default and not *args/**kwargs).

    Why the graph needs to record this: measured on the pistol_eco_pattern question, the model did
    call `pattern_detection_report` following the graph, but **passed not a single parameter** — the
    tool returned an empty pattern, so the pistol-round win rate came back null, sample size 0,
    confidence insufficient, coverage 0%. The graph said "which tool to call, which path to read",
    but never said "without team_name you get nothing at all".

    The parameter list is extracted from the function signature (traceable), not hand-written.
    """
    import inspect

    try:
        import vlml_env
    except Exception:
        return []
    fn = getattr(vlml_env, tool, None)
    if fn is None and tool == "query_sql":
        fn = getattr(vlml_env, "execute_custom_sql", None)
    if fn is None:
        return []
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return []
    out = []
    for p in sig.parameters.values():
        if p.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          inspect.Parameter.KEYWORD_ONLY):
            continue
        if p.default is inspect.Parameter.empty:
            out.append(p.name)
    return out


# --------------------------------------------------------------------------
# Parameters that are "semantically necessary": the signature cannot catch these, so they are
# annotated by hand
# --------------------------------------------------------------------------
# `_required_params()` extracts **syntactically required** parameters (those without a default).
# But what really traps people are parameters that have a default, raise no error when omitted, and
# can still return an empty result:
#   pattern_detection_report(team_name=None, player_name=None, ...) all have defaults; measured,
#   the model passed not one of them → empty pattern → pistol-round win rate null, sample size 0,
#   confidence insufficient → coverage 0%, even though it "called the right tool following the
#   graph". This one can only be hand-written like DIM_PITFALLS, with its source marked as seed.
# --------------------------------------------------------------------------
TOOL_PARAM_HINTS: dict[str, str] = {
    "pattern_detection_report":
        "at least team_name (or player_name) must be passed for the scope to be valid; passing "
        "nothing → scope.rounds=0, confidence=insufficient, and all four scoring points empty. "
        "series_ids must be passed as an **array** ['2843069']; passing a string splits it into "
        "individual characters (measured → rounds=0)",
    "query_sql":
        "sql_query is required and must start with SELECT (CTEs are not supported)",
    "match_rounds_report":
        "on large datasets you must page with round_start/round_end, otherwise it is very slow or "
        "truncated",
}

# Same as above, but this is the **machine-checkable** part: at least one of these parameters must
# be passed, or the result is necessarily empty.
# With it the validation layer can block the call before it is issued (rather than waiting for the
# referee to say "you submitted null").
# Measured: pattern_detection_report() with no arguments → scope=null;
#           team_name='Cloud9' → scope={rounds:59, confidence:moderate} (the truth).
TOOL_NEEDS_ANY_OF: dict[str, list[str]] = {
    "pattern_detection_report": ["team_name", "player_name"],
}

# The trap of parameter **types**: no error syntactically, completely wrong semantically.
# Measured: passing series_ids as the string '2843069' → it is split into individual characters →
#   scope becomes {series: 7, rounds: 0, confidence: insufficient} and all four scoring points are
#   empty;
#   passing the array ['2843069'] → {series: 1, rounds: 59, confidence: moderate} (the truth).
# The model habitually writes strings when emitting JSON, so this must be blocked before the call.
TOOL_PARAM_TYPES: dict[str, dict[str, str]] = {
    "pattern_detection_report": {"series_ids": "list"},
}


def build(*, observe: bool = False, with_schema: bool = True) -> KnowledgeGraph:
    """Build the graph from the tasks' rubrics + VLML's data dictionary + VLML's source structure.

    With observe=True an extra referee run is performed to fill in the procedure_step edges with
    the **measured** orchestration order (slow, and needs network/database access); by default only
    declarative information is used.
    With with_schema=True, VLML's 21 tables / 46 insights / 7 tools all enter the graph (queried
    live from information_schema + source parsing; it automatically falls back to file parsing when
    the database is unreachable).
    """
    import vlml_env  # noqa: F401  bootstrap the environment
    from tasks import TASKS

    g = KnowledgeGraph()
    known_tables = _known_tables()

    # ---- VLML's real structure: 21 tables + 46 insights + 7 tools ----
    # Previously it was reverse-engineered from **our own rubric SQL** only, which left just 2
    # tables in the graph — amounting to "only questions that have been generated enter the graph",
    # with none of VLML's actual modelling in it.
    # Querying information_schema + parsing the source (tables/derived/insights/tools) fills in all
    # four layers.
    schema: dict[str, Any] = {}
    if with_schema:
        try:
            import vlml_schema
            schema = asyncio.run(vlml_schema.discover())
        except Exception as e:                      # fall back to file parsing if the DB is down
            print(f"(VLML structure discovery failed, falling back to file parsing: "
                  f"{type(e).__name__}: {e})")
            schema = {}
    if schema.get("tables"):
        known_tables = schema["tables"]             # live columns are more accurate than file parsing

    # --- Tool nodes: the report tools exposed by vlml_env ---
    tool_names = [
        "match_summary_report", "match_analysis_report", "match_economy_report",
        "match_players_report", "match_rounds_report", "pattern_detection_report",
        "player_profile_report", "scouting_report", "query_sql",
    ]
    for t in tool_names:
        detail: dict[str, Any] = {"required_params": _required_params(t)}
        hint = TOOL_PARAM_HINTS.get(t)
        if hint:
            detail["param_hint"] = hint
            detail["param_hint_origin"] = "seed"
        need = TOOL_NEEDS_ANY_OF.get(t)
        if need:
            detail["needs_any_of"] = list(need)
        types = TOOL_PARAM_TYPES.get(t)
        if types:
            detail["param_types"] = dict(types)
        g.add_node(Node(id=f"tool:{t}", kind="tool", label=t, detail=detail))

    # `requires_tools` is a **question-level** declaration ("this question needs these tools"), not a
    # dimension-level "produced by it". It used to create produced_by edges all the same, so
    # query_sql — being in every question's requires_tools — showed up as a producer of every
    # dimension, which ruined who_produces.
    # The right approach: record it in the tool node's detail, create no edge.
    for topic_id, task in TASKS.items():
        for tool in getattr(task, "requires_tools", []) or []:
            nid = f"tool:{tool}"
            if nid not in g.nodes:
                g.add_node(Node(id=nid, kind="tool", label=tool))
            node = g.nodes[nid]
            req = list(node.detail.get("required_by") or [])
            if topic_id not in req:
                req.append(topic_id)
            node.detail["required_by"] = sorted(req)

    # --- Dimension nodes + produced_by / derived_from / co_occurs / confusable ---
    #
    # ⚠️ The dimensions of runtime questions (those adopted from generated_tasks.json) **create no
    # nodes**.
    # Mirroring the companion (`knowledge_tracker.py:1548/1555`):
    #     topic_id = self._ensure_topic(...)          # resolve first, only create if unresolvable
    #     is_known_topic = bool(self.store.get_topic(topic_id))
    #     qa_topic_id = topic_id if is_known_topic else ""     ← newly created ones do not enter
    # the answering record
    # i.e. a knowledge point the companion creates at runtime is an **empty shell (source=runtime,
    # all declarations empty) + does not count towards mastery + enters the pending-review candidate
    # queue** (`upsert_candidate`, evidence=MENTIONED).
    # MVE is stricter: not even an empty shell is created — the authoritative source of the
    # dimension layer is the **rubrics of the seed questions**, and new names from the runtime all
    # travel as **edges** (`link_import`), taking no node slot.
    # Otherwise every adopted question adds a dimension node (measured 15 → 16), which amounts to
    # "the graph derives from the questions" — exactly the opposite of the companion.
    try:
        from tasks import RUNTIME_TOPICS
        runtime_topics = set(RUNTIME_TOPICS)
    except Exception:                                        # pragma: no cover
        runtime_topics = set()
    pending: dict[str, dict[str, Any]] = {}

    dims_by_topic: dict[str, list[str]] = {}
    for topic_id, task in TASKS.items():
        dim_here: list[str] = []
        for p in task.rubric:
            dim = str(p.dimension)
            if not dim:
                continue
            if topic_id in runtime_topics:
                resolved = _resolve_existing_dim(dim, g)
                if resolved:
                    dim = resolved                  # reuse an existing dimension (isomorphic to
                                                # _resolve_topic_id)
                else:
                    # Create no node, only record a pending-review candidate — queryable on the
                    # dashboard, and a human decides whether to promote it
                    spec0 = getattr(p, "answer_spec", None)
                    sql0 = str(getattr(spec0, "sql", "") or "")
                    pending[dim] = {
                        "dimension": dim,
                        "topic_id": topic_id,
                        "question": str(getattr(task, "question", ""))[:300],
                        "point": str(getattr(p, "point", "") or ""),
                        "nearest": _nearest_dim(dim, g),
                        "sql": sql0[:4000],
                        "tables": (_tables_in_sql(sql0, known_tables)
                                   if sql0 else []),
                        "at": datetime.now().isoformat(timespec="seconds"),
                        "source": "runtime",
                    }
                    # A pending dimension is also an "entry produced by VLML / by a Voyager run",
                    # so it must be attached to a subject in the same way
                    try:
                        import subjects
                        pending[dim]["entities"] = subjects.entities_in_sql(sql0)
                        pending[dim]["subject_ids"] = _attach_entry(pending[dim])
                    except Exception:
                        pass
                    continue
            dim_here.append(dim)

            # Dimension node: write out in one go "what this metric is, how it is computed, which
            # columns it uses"
            detail: dict[str, Any] = {
                "topic_id": topic_id,
                "topics": [topic_id],
                "point": p.point,
                "points": [p.point],
                # The same dimension is shared by several questions (map_fb_conv belongs to two
                # questions at once).
                # When rendering the subgraph, only show **this question's** scoring points,
                # otherwise another question's subject gets dragged in as well (measured: Haven's
                # scoring points surfaced inside the corrode question).
                "points_by_topic": {topic_id: [p.point]},
                "weight": p.weight,
            }
            spec = getattr(p, "answer_spec", None)
            if spec is not None:
                if spec.sql:
                    tbls = _tables_in_sql(spec.sql, known_tables)
                    detail["tables"] = tbls
                    cols_map = _used_columns(spec.sql, tbls, known_tables)
                    detail["columns_by_table"] = cols_map
                    detail["columns"] = sorted(
                        {c for v in cols_map.values() for c in v})
                    detail["semantics"] = _sql_semantics(spec.sql)
                    if _has_structure(spec.sql):
                        detail["recipe"] = _sql_recipe(spec.sql)
                        # If an annotated version exists by hand, use that — it can point out "what
                        # belongs on which layer"
                        note = DIM_RECIPE_NOTES.get(dim)
                        if note:
                            detail["recipe"] = note
                            detail["recipe_origin"] = "annotated"
                    detail["tolerance"] = spec.numeric_tolerance
                    # The same SQL is shared by several scoring points, each taking a different
                    # column (max_losing_streak takes column 0, streak_start_round takes column 1).
                    # Without saying "which column to take", the model grabs the wrong one.
                    detail["value_columns"] = [spec.value_column]
                    if spec.base_column is not None:
                        detail["base_columns"] = [spec.base_column]
                elif spec.tool:
                    # Metrics aggregated inside the tool (pistol/eco/confidence): with no SQL, the
                    # semantics live in "which path of the return structure the value is read from".
                    detail["tool"] = spec.tool
                    detail["value_path"] = spec.value_path
                    detail["semantics"] = (
                        f"aggregated internally by {spec.tool}, value read at the dotted path: "
                        f"{spec.value_path}"
                        + (" (percentage: num/denom*100)" if spec.percent else "")
                    )
            pitfalls = DIM_PITFALLS.get(dim)
            if pitfalls:
                detail["typical_errors"] = list(pitfalls)
                detail["typical_errors_origin"] = "seed"

            g.add_node(Node(id=f"dim:{dim}", kind="dimension", label=dim,
                            detail=detail))

            if spec is None:
                continue
            if spec.tool:
                # Declarative: the retrieval recipe configured server-side (loop_core.py:185)
                if f"tool:{spec.tool}" not in g.nodes:
                    g.add_node(Node(id=f"tool:{spec.tool}", kind="tool",
                                    label=spec.tool))
                g.add_edge(Edge(
                    src=f"tool:{spec.tool}", dst=f"dim:{dim}",
                    relation=PRODUCED_BY, origin="declared",
                    reason=f"the rubric of {topic_id} declares it: value read at {spec.value_path}",
                    confidence=1.0))
            elif spec.sql:
                for tbl in _tables_in_sql(spec.sql, known_tables):
                    info = known_tables.get(tbl) or {}
                    g.add_node(Node(id=f"table:{tbl}", kind="table", label=tbl,
                                    detail={"grain": info.get("grain", ""),
                                            "columns": list(info.get("columns")
                                                            or [])[:24],
                                            "pk": list(info.get("pk") or []),
                                            "source": info.get("source", "")}))
                    g.add_edge(Edge(
                        src=f"table:{tbl}", dst=f"dim:{dim}",
                        relation=DERIVED_FROM, origin="declared",
                        reason=f"the deterministic SQL of {topic_id} reads this table",
                        confidence=1.0))
        dims_by_topic[topic_id] = sorted(set(dim_here))

        # co_occurs: dimensions appearing together in the same question (symmetric)
        uniq = sorted(set(dim_here))
        for i, a in enumerate(uniq):
            for b in uniq[i + 1:]:
                g.add_edge(Edge(src=f"dim:{a}", dst=f"dim:{b}",
                                relation=CO_OCCURS, origin="declared",
                                reason=f"question {topic_id} requires covering them together",
                                confidence=0.9))
        # confusable: dimensions with the same suffix but a different prefix
        # (pistol_win_rate / eco_win_rate)
        for a in uniq:
            for b in uniq:
                if a >= b:
                    continue
                sa, sb = a.split("_"), b.split("_")
                if len(sa) < 2 or len(sb) < 2:
                    continue
                if sa[1:] == sb[1:] and sa[0] != sb[0]:
                    g.add_edge(Edge(src=f"dim:{a}", dst=f"dim:{b}",
                                    relation=CONFUSABLE, origin="declared",
                                    reason="same suffix, different prefix — easily confused",
                                    confidence=0.8))

    # --- Reverse lookup on tool nodes: which dimensions can this tool produce ---
    # who_produces is "dimension → tool"; this adds the reverse index, read straight off detail
    # when rendering.
    for e in g.edges:
        if (e.relation == PRODUCED_BY and e.src.startswith("tool:")
                and e.dst.startswith("dim:")):
            node = g.nodes.get(e.src)
            if node is None:
                continue
            cur = list(node.detail.get("yields") or [])
            dim = e.dst[len("dim:"):]
            if dim not in cur:
                cur.append(dim)
            node.detail["yields"] = sorted(cur)

    if schema:
        _add_vlml_schema(g, schema)
        _add_seed_facts(g, schema)

    # VLML's modelling declarations: **read from files**, so they work even when the database is
    # unreachable. Placed after schema because it adds declarations onto the already-created table
    # nodes (live column names + the doc's metric definitions only combine into something complete).
    model: dict[str, Any] = {}
    if vlml_schema is not None:
        try:
            model = vlml_schema.model_specs()
        except Exception as e:        # if the doc cannot be read, only the declarations are missing;
                                       # the whole graph must not collapse
            print(f"(VLML modelling declaration parsing failed: {type(e).__name__}: {e})")
    if model:
        _add_model_declarations(g, model)
    _add_dimension_declarations(g, model)

    if observe:
        asyncio.run(_observe_procedure(g))
    _merge_imported_links(g)
    # New dimension names brought in by runtime questions: registered as pending-review candidates,
    # they do not enter the dimension layer
    n_pending = _save_pending_dimensions(pending)
    if n_pending:
        print(f"({n_pending} new dimensions from runtime questions → pending-review candidates, "
              f"no nodes created: {', '.join(sorted(pending))[:120]})")
    return g


# --------------------------------------------------------------------------
# Human-imported questions → knowledge graph **edges** (not nodes)
# --------------------------------------------------------------------------
# ⚠️ The basis for graph construction in this section was **overturned and rebuilt once**; read
# this comment block before changing anything.
#
# [The wrong approach (v1, abandoned)] when the match failed, add a `dim:` **dimension node** to
#   the graph.
#   The basis was reverse-engineering from `TASKS[].rubric` (tasks.py:361, "only questions that
#   have been generated enter the graph") — and that basis is itself wrong: it means the dimension
#   layer can only ever hold directions that already have a question, so a new direction a human
#   asks about has nowhere to land and can only be kept alive by inventing nodes.
#
# [The right basis (v2, current)] **VLML's tables expanded out to the tool-set layer: the graph has
#   full coverage.**
#   Measured: 46 `insight` nodes = VLML's `tools/sql/*.sql`, each declaring `tables` (which tables
#   it reads) and `reports` (which report tools compose it) — the table × tool-set layer has no
#   gap. So the inclusion criterion is not "does a dimension of the same name exist in the graph"
#   but **"can VLML complete this work with a tool set covered by those tables"**:
#     a human asks a question → VLML really runs a retrieval → real SQL / tool / table obtained
#     → look up which insights cover those tables (and are used by this tool)
#     → a hit means "the tool set covers it" → **include it**
#
# [How it lands] Since the coverage layer needs no patching, we **create no node, we create edges**
# (the user's own words):
#     The companion's `MaterialTopicMapper` hands unmapped material back to the UI for a human to
#     pick a node, and never creates a new knowledge point for a material; MVE is the same — a
#     human's question is an **application scenario**, and scenarios hang off edges, taking no node
#     slot.
#
#   The three kinds of edges actually created (all origin=observed, since they all come from real
#   data retrieval):
#     insight:I --application--> table:T    this insight has been used once on this table
#     tool:X    --composes-----> insight:I  this tool really used this insight this time (backfill)
#     insight:A --co_occurs----> insight:B  one question requires both insights together (symmetric)
#   Degenerate case (pure query_sql reading a table directly, with no covering insight):
#     tool:X    --application--> table:T    included at the **table** level, confidence lowered to
#                             0.8
#
# Isomorphic relations:
#   Companion  MaterialTopicMapper → map to an existing knowledge point / hand unmapped material
#              back to the human, **creating no node**
#   MVE       link_import()        → judge tool-set coverage / if covered, **create an edge**, no
#              node
IMPORTED_LINKS = HERE / "imported_links.json"
IMPORTED_DIMS = HERE / "imported_dimensions.json"   # v1 leftover, used only for a one-off migration


def load_imported_links() -> list[dict[str, Any]]:
    """The **edges** persisted for human imports (merged back into the graph at build time).

    v1's `imported_dimensions.json` (the old node-inventing mechanism) is migrated here **once**
    into edge records, and then the old file is renamed and retired — old data is not lost, but it
    no longer travels the node-creating path.
    """
    try:
        raw = json.loads(IMPORTED_LINKS.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else []
    except Exception:
        items = []
    if IMPORTED_DIMS.exists():
        try:
            old = json.loads(IMPORTED_DIMS.read_text(encoding="utf-8"))
        except Exception:
            old = []
        if isinstance(old, list) and old:
            have = {(str(it.get("question") or ""), str(it.get("dimension") or ""))
                    for it in items}
            for o in old:
                if not isinstance(o, dict):
                    continue
                k = (str(o.get("question") or ""), str(o.get("dimension") or ""))
                if k in have:
                    continue
                known = _known_tables()
                sql = str(o.get("sql") or "")
                tbls = _tables_in_sql(sql, known) if sql else []
                cov = coverage_of(tables=tbls, tool=str(o.get("tool") or ""))
                items.append({
                    "question": str(o.get("question") or "")[:300],
                    "dimension": str(o.get("dimension") or ""),
                    "topic_id": "",
                    "sql": sql[:4000],
                    "tool": str(o.get("tool") or ""),
                    "tables": tbls,
                    "covered": bool(cov["covered"]),
                    "cover_level": cov["level"],
                    "insights": cov["insights"],
                    "subject": dict(o.get("subject") or {}),
                    "value": o.get("value"),
                    "linked_at": str(o.get("adopted_at") or ""),
                    "origin": "human_import(migrated_from_v1_dimension)",
                })
                have.add(k)
            try:
                IMPORTED_LINKS.write_text(
                    json.dumps(items, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                IMPORTED_DIMS.rename(HERE / "imported_dimensions.json.v1")
            except Exception:
                pass
    return items


def _insight_index(g: KnowledgeGraph) -> list[tuple[str, set[str], set[str]]]:
    """(insight node id, the tables it reads, the report tools that use it) — the basis of tool-set
    coverage."""
    out: list[tuple[str, set[str], set[str]]] = []
    for nid, node in g.nodes.items():
        if not nid.startswith("insight:"):
            continue
        det = node.detail or {}
        out.append((nid,
                    {str(t) for t in (det.get("tables") or [])},
                    {str(r) for r in (det.get("reports") or [])}))
    return out


def coverage_of(*, tables: list[str] | set[str], tool: str = "",
                g: KnowledgeGraph | None = None,
                max_hits: int = 3) -> dict[str, Any]:
    """**Inclusion criterion**: can VLML complete this work with a "tool set covered by the tables"?

    ⚠️ `max_hits` is mandatory, not an optimisation. Measured: a single question about "round win
    rate" touches two **core tables**, `rounds` + `games`, and 16 of the 46 insights read them —
    without throttling, one import lands 32 application + 16 composes + C(16,2)=120 co_occurs =
    **168 edges**, drowning the graph and blowing the companion's `GraphBudget.max_edges=30`
    budget too.

    So what is taken here is a **minimum cover set**: sorted by "how much of this retrieval's
    tables it covers", keeping only the top `max_hits` — that is the precise answer to "which tool
    set completed this work", not "which tools happened to touch these tables".

    The `level` in the return value:
      - `insight` — an insight covers the tables this retrieval read (ideal: tool-set coverage)
      - `table`   — no insight covers it, but the table itself is in the graph (usually a direct
                     query_sql lookup; the tool set is generic SQL, which still counts as coverage,
                     with lowered confidence)
      - `none`    — not even the table is in the graph → **not covered, not included**, handed
                     back honestly (the same as the companion: when mapping fails it does not
                     force anything, it hands back to the UI for a human to handle)
    """
    try:
        g = g or KnowledgeGraph.load()
    except Exception:
        return {"covered": False, "level": "none", "insights": [],
                "insights_total": 0, "tables": []}
    tset = {str(t) for t in (tables or []) if t}
    idx = _insight_index(g)
    tool = str(tool or "").strip()

    scored: list[tuple[float, int, str]] = []
    for nid, itabs, ireps in idx:
        ov = itabs & tset
        if not ov:
            continue
        # Coverage ratio = the share of this retrieval's tables it covers; extra weight when the calling
        # tool actually declares this insight
        s = len(ov) / max(1, len(tset)) + (0.5 if (tool and tool in ireps) else 0.0)
        scored.append((s, len(ov), nid))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))

    if scored:
        top = [nid for _, _, nid in scored[:max(1, max_hits)]]
        return {"covered": True, "level": "insight",
                "insights": sorted(n[len("insight:"):] for n in top),
                "insights_total": len(scored),
                "tables": sorted(tset)}
    known = {str(t) for t in g.tables()}
    if tset and tset <= known:
        return {"covered": True, "level": "table", "insights": [],
                "insights_total": 0, "tables": sorted(tset)}
    return {"covered": False, "level": "none", "insights": [],
            "insights_total": 0, "tables": sorted(tset)}


# --------------------------------------------------------------------------
# Dimensions from runtime questions → pending-review candidates (isomorphic to the companion's
# `upsert_candidate`, **creating no node**)
# --------------------------------------------------------------------------
PENDING_DIMS = HERE / "pending_dimensions.json"


def load_pending_dimensions() -> list[dict[str, Any]]:
    """Dimension names brought in by runtime questions that did not resolve to an existing
    dimension — registered only, they do not enter the dimension layer."""
    try:
        raw = json.loads(PENDING_DIMS.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def _norm_dim(d: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(d or "").lower())


def _dim_tokens(d: str) -> set[str]:
    return {t for t in re.split(r"[^a-zA-Z0-9]+", str(d or "").lower()) if len(t) >= 2}


def _resolve_existing_dim(dim: str, g: KnowledgeGraph) -> str:
    """Resolve a runtime question's dimension name to an **existing** dimension first — isomorphic to
    the companion's `_resolve_topic_id`.

    Only two tiers of hard match count: exactly the same name / the same normalised name (punctuation
    and case removed). **No fuzzy matching**: merging two different quantities costs far more than
    leaving one extra pending-review candidate — it would directly pollute mastery and question
    routing.
    """
    if f"dim:{dim}" in g.nodes:
        return dim
    norm = _norm_dim(dim)
    for d in g.dimensions():
        if _norm_dim(d) == norm:
            return d
    return ""


def _nearest_dim(dim: str, g: KnowledgeGraph) -> str:
    """Suggest the "most similar existing dimension" for a pending-review candidate — for hints only,
    never an automatic merge.

    Mirrors the companion's `_suggest_dims` (merely saying "pick another dimension" does not make
    the model pick correctly — candidates have to be offered).
    """
    toks = _dim_tokens(dim)
    if not toks:
        return ""
    best, score = "", 0.0
    for d in g.dimensions():
        t = _dim_tokens(d)
        if not t:
            continue
        s = len(toks & t) / len(toks | t)
        if s > score:
            best, score = d, s
    return best if score >= 0.34 else ""


def _render_from_links(topic_id: str, g: "KnowledgeGraph",
                       with_formula: bool = True) -> str:
    """When the dimension layer has no focus for this question, render the prompt from the **payload on
    the edges**.

    The dimensions of runtime questions create no nodes (mirroring the companion: runtime knowledge
    points do not enter the authoritative layer), so `subgraph_for_topic` returns `found=False`.
    But the scaffold information is not lost — it lives in `imported_links.json` /
    `pending_dimensions.json`: the SQL from that real retrieval can be parsed into tables, columns,
    metric definitions and a structural skeleton. Without this block the model works naked while
    solving.

    Measured: without this fallback the graph prompt for a new question has length 0 (equivalent to
    the scaffold being withdrawn), so the question can be generated but not practised.
    """
    recs: list[dict[str, Any]] = []
    try:
        recs += [r for r in load_imported_links()
                 if str(r.get("topic_id") or "").strip() == str(topic_id)]
    except Exception:
        pass
    try:
        recs += [r for r in load_pending_dimensions()
                 if str(r.get("topic_id") or "").strip() == str(topic_id)]
    except Exception:
        pass
    if not recs:
        return ""

    b = GraphBudget()
    lines: list[str] = []
    seen: set[str] = set()
    for r in recs:
        dim = str(r.get("dimension") or "").strip()
        if not dim or dim in seen:
            continue
        seen.add(dim)
        sql = str(r.get("sql") or "")
        sh = sql_shape(sql, list(r.get("tables") or []))
        lines.append(f"· Dimension {dim} (a new dimension brought in by a runtime question, "
                     "**not in the dimension layer**; its metric definition comes from the "
                     "retrieval that genuinely ran when it was admitted)")
        if r.get("point"):
            lines.append(f"   What to retrieve: {r['point']}")
        if sh["tables"]:
            lines.append("   Source tables (you must query these yourself with query_sql — "
                         "**a table name is not a tool name**): "
                         + ", ".join(sh["tables"]))
        if sh["columns"]:
            lines.append("   Columns used: " + ", ".join(sh["columns"][:b.max_cols]))
        # Same metric definition policy as `render_for_prompt`: that layer is governed by
        # `with_formula`, and a runtime question (edges only, no nodes) taking this fallback path
        # must be no exception —
        # otherwise a question whose scaffold was withdrawn gets its answer template back the
        # moment it lands on this path.
        if with_formula and sh["semantics"]:
            lines.append(f"   How it is computed: {sh['semantics']}")
        if with_formula and sh["recipe"]:
            lines.append(f"   Structural skeleton: {sh['recipe']}")
            lines.append(f"   Self-check: {SELF_CHECK_NOTE}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _save_pending_dimensions(pending: dict[str, dict[str, Any]]) -> int:
    """Persist the pending-review candidates. Only those still "runtime and unresolved" are kept;
    the rest disappear on their own."""
    if not pending:
        return 0
    try:
        old = {str(r.get("dimension") or ""): r for r in load_pending_dimensions()}
    except Exception:
        old = {}
    old.update(pending)
    try:
        PENDING_DIMS.write_text(
            json.dumps(sorted(old.values(), key=lambda r: str(r.get("dimension"))),
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        return 0
    return len(pending)


def sql_shape(sql: str, tables: list[str] | None = None) -> dict[str, Any]:
    """Parse the "question-authoring shape" out of a **genuinely executed** SQL:
    tables / columns / metric definition / skeleton / difficulty.

    Human-imported questions create no nodes, so the question generator has no `dim:` node to look
    through — what it looks through are the **edges**, and the information on those edges is exactly
    this shape. The data source is measured just the same: it parses that SQL which really ran, not
    something the model wrote.
    """
    known = _known_tables()
    txt = str(sql or "")
    tbls = list(tables or []) or _tables_in_sql(txt, known)
    cols_map = _used_columns(txt, tbls, known) if (txt and tbls) else {}
    return {
        "tables": tbls,
        "columns_by_table": cols_map,
        "columns": sorted({c for v in cols_map.values() for c in v}),
        "semantics": _sql_semantics(txt) if txt else "",
        "recipe": _sql_recipe(txt) if txt else "",
        "difficulty": _sql_difficulty(txt) if txt else 2,
        "skills": _skills_of(txt),
    }


def _entry_id(rec: dict[str, Any]) -> str:
    """The entry's stable id in the subject registry — it must be recognisable as the same entry across
    graph rebuilds and database swaps.

    The two kinds of entry get different prefixes: pending-review dimensions brought in by runtime
    questions (source="runtime") use `pending:`, while edges left by human imports use `human:` —
    when the same dimension name appears on both sides they should not be merged into one entry.
    """
    dim = str(rec.get("dimension") or "").strip()
    q = str(rec.get("question") or "").strip()
    prefix = "pending" if str(rec.get("source") or "") == "runtime" else "human"
    return f"{prefix}:{dim or q[:24] or '?'}"


def _attach_entry(rec: dict[str, Any], entry_id: str = "") -> list[str]:
    """Attach this entry to whichever subject it can be attached to.

    When it cannot be attached it is registered as **unassigned** (an empty list), waiting for a
    human to fill that subject in on the dashboard — it is **never** force-fitted into some default
    subject: that would degrade "subject" back into an enum.
    Isomorphic to the companion's `store.py:1506`: a non-seed entry's subject may be overwritten by
    a later write, so repeating attach here is idempotent and upgradable.
    """
    try:
        import subjects
        ents = dict(rec.get("entities") or {})
        if not ents:
            for k, v in subjects.entities_in_sql(str(rec.get("sql") or "")).items():
                ents.setdefault(k, v)
        sids = subjects.resolve(ents)
        subjects.attach(entry_id or _entry_id(rec), sids)
        return sids
    except Exception:
        return []


def link_import(*, question: str, dimension: str = "", sql: str = "",
                tool: str = "", tables: list[str] | None = None,
                topic_id: str = "", value: Any = None,
                subject: dict[str, Any] | None = None) -> dict[str, Any]:
    """Take a human-imported question **onto the graph's edges**, creating no node at all. Returns the
    persisted record.

    The criterion (see `coverage_of`): are the tables that VLML's real retrieval read covered by the
    graph's "tool set covered by those tables" (insights)? Covered → include → create edges. Not
    covered → return `adopted=False` honestly, **never silently inventing a node to stand in**.

    The edges' origin must be measured (origin="observed"): these tables/insights are not declared,
    they were genuinely read and genuinely used by this retrieval.
    """
    known = _known_tables()
    tbls = list(tables or []) or (_tables_in_sql(sql, known) if sql else [])
    cov = coverage_of(tables=tbls, tool=tool)
    rec: dict[str, Any] = {
        "question": str(question or "")[:300],
        "dimension": str(dimension or "").strip(),
        "topic_id": str(topic_id or "").strip(),
        "sql": str(sql or "")[:4000],
        "tool": str(tool or "").strip(),
        "tables": sorted(set(tbls)),
        "covered": bool(cov["covered"]),
        "cover_level": str(cov["level"]),
        "insights": list(cov["insights"]),
        "insights_total": int(cov.get("insights_total") or 0),
        "subject": dict(subject or {}),
        "value": value,
        "linked_at": datetime.now().isoformat(timespec="seconds"),
        "origin": "human_import",
    }
    # --- Attach to a subject (the container a human filled in) ---
    # Entities have two sources: the subject={"team": "NRG"} the question generator passes
    # explicitly, and the literals in the SQL that genuinely ran this time. The two are merged and
    # then looked up in the registry.
    ents = {str(k).lower(): str(v) for k, v in (subject or {}).items()
            if k and v}
    try:
        import subjects
        for k, v in subjects.entities_in_sql(sql).items():
            ents.setdefault(k, v)
    except Exception:
        pass
    rec["entities"] = ents
    if not rec["covered"]:
        rec["adopted"] = False
        rec["reason"] = ("tool set does not cover it: the tables this retrieval read "
                         f"{sorted(set(tbls)) or '(no tables parsed)'} "
                         "are neither within any insight's coverage nor entirely inside the graph "
                         "→ not included, and no node created")
        # ⚠️ Attachment must happen **only after** inclusion is confirmed. A question that is not
        # covered is not persisted, so attaching here first would leave a ghost entry in
        # `subjects.json` with no matching record, which could never be recognised again after a
        # graph rebuild (hit in measurement).
        rec["subject_ids"] = []
        return rec
    rec["subject_ids"] = _attach_entry(rec)

    items = [it for it in load_imported_links()
             if not (str(it.get("question") or "") == rec["question"]
                     and str(it.get("dimension") or "") == rec["dimension"])]
    items.append(rec)
    try:
        IMPORTED_LINKS.write_text(
            json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass

    # Persisting is not enough: **these edges must also exist in the current graph right away**. The
    # question generator reads the object returned by `KnowledgeGraph.load()`, not the JSON file.
    # Fingerprint unchanged → the next load does not rebuild → the patch survives; if the
    # fingerprint changes, `_merge_imported_links` inside build() re-merges the edges from the JSON,
    # so nothing is lost.
    try:
        g = KnowledgeGraph.load()
        n = _merge_imported_links(g)
        g.save()
        rec["edges_added"] = n
    except Exception:
        pass                                                 # a failed patch does not affect persistence
    rec["adopted"] = True
    return rec


def _merge_imported_links(g: KnowledgeGraph) -> int:
    """Merge the **edges** left by human imports back into the graph at build time — creating not a
    single node.

    Why persist rather than patching the graph once at import time: the graph is a product of
    `build()`, and the moment the fingerprint changes (a new question added, VLML's doc changed) it
    is rebuilt wholesale, wiping any patch applied to the graph object. Persisting as
    `imported_links.json` is the same persistence idea as
    `question_gen.generated_tasks.json`.
    """
    added = 0
    for rec in load_imported_links():
        # --- Attachment to a subject: recomputed on every merge ---
        # When a human later fills in a subject (or swaps the data source, or syncs new fillable
        # values), **old entries should attach automatically** — that is what "subject is a
        # container" rather than an enum means.
        # Same as with coverage: every input to the criterion lives in the record, so recomputing
        # is free.
        # Placed after the coverage decision for the same reason: entries that are not covered never
        # get persisted, so attaching first would only leave ghost entries with no matching record
        # (hit in measurement; see the comment in link_import).
        # **Coverage must be recomputed at merge time; the old conclusion from persist time cannot
        # be trusted.** Measured: before the graph was rebuilt the insight layer on the server was
        # empty and a human import was judged at table level; after the rebuild the insights were
        # complete, yet the record still said table level — so the edges stayed stuck at low
        # confidence. Every input to the criterion (real SQL / tool / table) is in the record, so
        # recomputing is free.
        tbls0 = [str(t) for t in (rec.get("tables") or [])]
        cov = coverage_of(tables=tbls0, tool=str(rec.get("tool") or ""), g=g)
        if not cov["covered"]:
            continue                     # not covered does not enter the graph (as the companion
                                         # does: when mapping fails it forces nothing)
        rec["subject_ids"] = _attach_entry(rec)
        dim = str(rec.get("dimension") or "").strip()
        q = str(rec.get("question") or "")[:40]
        tag = f"human import \"{q}\""
        dim_tag = f" (dimension {dim})" if dim else ""
        ins = [f"insight:{i}" for i in (cov.get("insights") or [])
               if f"insight:{i}" in g.nodes]
        tbls = [f"table:{t}" for t in (cov.get("tables") or [])
                if f"table:{t}" in g.nodes]
        tool = str(rec.get("tool") or "").strip()
        tool_id = f"tool:{tool}" if tool else ""
        tset = set(tbls)

        # 1) application: this insight has actually been applied once on this table
        #    (as in the companion's `application`, the focus is on the from end — from is the
        #    insight, so no flip is needed)
        for i in ins:
            node = g.nodes.get(i)
            itset = {f"table:{t}" for t in ((node.detail or {}).get("tables") or [])}
            for t in sorted(itset & tset):
                g.add_edge(Edge(src=i, dst=t, relation=APPLICATION,
                                origin="observed",
                                reason=f"{tag} measured: this insight was used on that table"
                                       f"{dim_tag}",
                                confidence=0.9))
            # 2) composes: this tool really used this insight this time (backfilling what the
            #    declarations left out)
            if tool_id:
                g.add_edge(Edge(src=tool_id, dst=i, relation=COMPOSES,
                                origin="observed",
                                reason=f"{tag} measured: {tool} used this insight this time",
                                confidence=0.9))

        # 3) co_occurs (symmetric): one question requires these two insights together — the
        #    companion's semantics of "co-occurring in the same question". MVE originally used it
        #    only on dimensions; here it is applied isomorphically at the insight layer.
        for a in range(len(ins)):
            for b in range(a + 1, len(ins)):
                g.add_edge(Edge(src=ins[a], dst=ins[b], relation=CO_OCCURS,
                                origin="observed",
                                reason=f"{tag} measured: these two insights were used together",
                                confidence=0.9))

        # 4) Degenerate case: no insight covers it (mostly a direct query_sql lookup) → include at
        #    table level
        if not ins and tool_id:
            for t in sorted(tset):
                g.add_edge(Edge(src=tool_id, dst=t, relation=APPLICATION,
                                origin="observed",
                                reason=(f"{tag} measured: {tool} queried this table directly "
                                        "(no insight covers it, included at table level)"
                                        + dim_tag),
                                confidence=0.8))
        added += 1
    return added


# v1's name, kept as a compatibility shim: anywhere still calling `adopt_dimension` automatically
# goes through the new logic (creating edges, no nodes). The parameters keep v1's naming; the
# behaviour is defined by `link_import`.
def adopt_dimension(*, dimension: str = "", sql: str = "", tool: str = "",
                    question: str = "", subject: dict[str, Any] | None = None,
                    value: Any = None, **_: Any) -> dict[str, Any]:
    """**Deprecated**: v1's "create a dimension node". Everything now goes through `link_import`,
    which creates edges."""
    if not str(dimension or "").strip():
        return {}
    return link_import(question=question, dimension=dimension, sql=sql,
                       tool=tool, subject=subject, value=value)


_GRAIN_LABEL = {"round": "round", "game": "map", "series": "series", "player": "player",
                "team": "team", "map": "map", "date": "day", "tournament": "tournament",
                "entity_type": "entity type", "entity": "entity", "factor": "factor"}


def _zh_grain(doc: str) -> str:
    """Translate the grain written in the modelling doc (`(round_id, team_name)`) into a readable
    row grain.

    `_grain_of_table()` only recognises the naming `agg_{subject}_{grain}_stats`, so the 7 derived
    tables whose names do not follow the convention all degrade to the words "aggregate table" —
    and agg_first_blood_stats is precisely **one row per round**; described as an "aggregate table"
    the model JOINs rounds once more and the row count doubles. The doc holds the precise grain, so
    use it.
    """
    toks = re.findall(r"[a-z_][a-z_0-9]*", (doc or "").lower())
    if not toks:
        return "", 0
    keys = [t[:-3] if t.endswith("_id") else
            (t[:-5] if t.endswith("_name") else t) for t in toks]
    # English prose like "One row per round" is not a key list — if it cannot all be recognised, do
    # not force a translation, otherwise it produces monstrosities like "one (one × row × per ×
    # round) row" (measured).
    if any(k not in _GRAIN_LABEL for k in keys):
        return "", 0
    zh = [_GRAIN_LABEL[k] for k in keys]
    return (f"one row per {zh[0]}" if len(zh) == 1
            else "one row per (" + " × ".join(zh) + ")"), len(zh)


def _add_model_declarations(g: KnowledgeGraph, specs: dict[str, Any]) -> None:
    """Write VLML's **modelling declarations** into the table nodes — isomorphic to the companion's
    knowledge_seeds.

    The companion's 457 knowledge point nodes carry 19 fields (difficulty / question_types /
    typical_misconceptions / prerequisites / unit / depth / …); the question generator reads the
    node declarations to decide question type and metric definitions, and the model only writes the
    question text. The "author declarations" at the same layer in VLML are the four modelling docs
    under `database/`:

        DATA_MODEL.md          grain + use cases + core lineage
        DERIVED_TABLES.md      grain/upstream/key columns/example queries of the 7 derived tables
        column_definitions.yaml column-level metric definitions
        DATA_DICTIONARY.json   primary keys / column types / **metric formulas** (sum/denom)

    Previously none of that entered the graph: table nodes only had "column names + row count + a
    coarse grain", `pk` was an empty array everywhere (measured: `table:rounds` had pk=[] while the
    doc plainly says round_id), and derived tables carried no more than the words "aggregate table".
    While authoring questions the model could only guess what columns mean — which is precisely the
    root of it writing `fb_player` as `player_name`.
    """
    tbl_specs = specs.get("tables") or {}
    if not tbl_specs:
        return
    declared_by = ("VLML modelling docs: DATA_MODEL.md / DERIVED_TABLES.md / "
                   "metadata/column_definitions.yaml / DATA_DICTIONARY.json")

    # First pass: create all nodes first (an upstream table may be sorted after a downstream one,
    # and edges need both ends present)
    for name, s in sorted(tbl_specs.items()):
        nid = f"table:{name}"
        if nid not in g.nodes:
            g.add_node(Node(id=nid, kind="table", label=name, detail={}))
        node = g.nodes[nid]
        grain_doc = s.get("grain") or ""
        # When the naming convention cannot derive it ("aggregate table" / "reference or dictionary
        # table"), swap in the precise grain the doc writes;
        # when the doc's grain has three or more keys (e.g. agg_team_map_stats is team × map ×
        # tournament) the convention version cannot express it either, so the doc wins again.
        zh_grain, n_keys = _zh_grain(grain_doc)
        # NOTE: the two literals below are the exact coarse-grain strings returned by
        # vlml_schema._grain_of_table() (vlml_schema.py:126-127). They are compared
        # against, so they must stay verbatim and in sync with vlml_schema.py.
        if zh_grain and (node.detail.get("grain") in ("", None, "aggregate table", "reference/dictionary table")
                         or n_keys >= 3):
            node.detail["grain"] = zh_grain
        node.detail.update({
            "purpose": s.get("purpose") or node.detail.get("purpose") or "",
            "grain_doc": grain_doc,             # the precise grain the modelling doc writes
            "pk": list(s.get("pk") or node.detail.get("pk") or []),
            "column_desc": dict(s.get("column_desc") or {}),
            "metrics": dict(s.get("metrics") or {}),
            "layer": s.get("layer") or node.detail.get("layer") or "",
            "parent": s.get("parent") or "",
            "use_cases": s.get("use_cases") or "",
            "example_sql": s.get("example_sql") or "",
            "declared_by": declared_by,
        })

    # Second pass: the upstreams declared by the modelling docs (DERIVED_TABLES' Source + core
    # parent tables).
    # Why a separate pass: `discover()`'s lineage comes from querying transformations live, so it
    # vanishes when the database is down; this one comes from the doc and is always there. origin
    # is marked "model" to show the difference.
    for name, s in sorted(tbl_specs.items()):
        for up in s.get("upstream") or []:
            if f"table:{up}" not in g.nodes:
                continue
            g.add_edge(Edge(src=f"table:{up}", dst=f"table:{name}",
                            relation=DERIVED_FROM, origin="model",
                            reason=f"declared by the modelling doc: {name} is computed from {up}",
                            confidence=1.0))


# --------------------------------------------------------------------------
# Companion-style declarations on dimension nodes
# --------------------------------------------------------------------------
# Of the companion node's 19 fields, 12 can be genuinely filled here; the other 7 (aliases /
# curriculum_tags / curriculum_version / exam_region / exam_type etc.) have no isomorphic
# counterpart in MVE — they are neither invented nor filled. A fabricated declaration is more
# dangerous than no declaration.
def _sql_difficulty(sql: str) -> int:
    """The difficulty tier (1-4) of this SQL. The criterion is countable, not the model's
    self-assessment.

    Same tier scheme as `question_gen.DIFFICULTY_RUBRIC` (1 single-table aggregation / 2 grouping /
    3 JOIN or conditional branch / 4 window function or nested subquery). With difficulty carried by
    the graph, the question generator no longer has to count keywords in each place — the
    companion's difficulty also grows on the node.
    """
    low = (sql or "").lower()
    if "over (" in low or "over(" in low or "from (select" in low:
        return 4
    if " join " in low or "case when" in low or " having " in low:
        return 3
    if "group by" in low:
        return 2
    return 1


def _unit_of(dim: str, d: dict[str, Any]) -> str:
    """The unit of this metric (isomorphic to the companion's `unit`). Derived from the naming and
    the metric definition text, and traceable.

    The order of the criteria matters: the "losing_**s**treak" inside `max_losing_streak` contains
    `_s`, so checking time first would wrongly label it "seconds" (measured, that is exactly how it
    went wrong). Check losing streak / rounds first.
    """
    text = " ".join(str(d.get(k) or "") for k in ("point", "semantics")).lower()
    # NOTE: the English literals below ("rate" / "ratio" / "round" / "second") are matched against the
    # rubric point text in tasks.py (e.g. "Overall first-blood conversion rate",
    # "Longest losing-streak round count", "Total rounds across the match") and the metric semantics
    # built from those rubric points. They were translated from the Chinese forms 率 / 比值 / 回合 / 秒
    # and must stay aligned with the wording used in tasks.py.
    if ("pct" in dim or "rate" in dim or "conv" in dim or "kast" in dim
            or "adr" in dim or "%" in text or "rate" in text):
        return "%"
    if "ratio" in dim or "share" in dim or "ratio" in text:
        return "ratio"
    if "streak" in dim or "rounds" in dim or "round" in text:
        return "rounds"
    if dim.endswith("_s") or "time" in dim or "delay" in dim or "second" in text:
        return "seconds"
    return "count"


def _skills_of(sql: str) -> list[str]:
    low = (sql or "").lower()
    out = []
    if "over (" in low or "over(" in low or "row_number" in low:
        out.append("window function")
    if "from (select" in low or "from(select" in low:
        out.append("nested subquery")
    if " join " in low:
        out.append("multi-table JOIN")
    if "case when" in low or "coalesce(" in low:
        out.append("conditional branch")
    if "group by" in low:
        out.append("grouped aggregation")
    if any(k in low for k in ("count(", "avg(", "sum(", "max(", "min(")):
        out.append("aggregate function")
    return out


LAYER_LABEL = {"core": "core table", "derived": "derived table", "agg": "aggregate table",
               "ref": "reference / dictionary table"}

# --------------------------------------------------------------------------
# The companion's four-layer organisation: **stage → subject → chapter → knowledge point**
# --------------------------------------------------------------------------
# The hierarchy measured in the companion's `static/knowledge_seeds/math.json`:
#   stage   = school stage primary/junior_high/senior_high/college  ← **the gradient**
#   subject = subject math/physics/… (11 of them, one seed file each)
#   chapter = a chapter within a subject (math alone has 37: linear functions, trigonometry…)
#   topic   = knowledge point, carrying depth / difficulty / prerequisites / skills / …
#
# The isomorphic mapping in MVE (semantics specified by the user):
#   stage   → **the tool set of the gradient**: to master this knowledge point, what is the lowest
#             level of means required
#             raw (raw event tables) → agg (derived aggregates) → insight (insight SQL) → tool
#             (report tools)
#             Isomorphic point: both are **entering a gradient**, not a kind of node.
#   subject → **tournament / team / player**: the entity domain, auto-derived from the columns used
#             for retrieval
#   chapter → **the topic domain under the entity domain**: first blood / economy / pistol round /
#             losing streak / plant …
#   topic   → dimension nodes (VLML's modelling of the data); the scaffold was ported earlier
#
# ⚠️ A semantic misalignment must be called out: previously `chapter` held LAYER_LABEL values
# (core table / derived table / aggregate table) — which is really the **gradient**, occupying the
# companion's `stage` slot. So this round puts it back in place:
# stage takes over the gradient, chapter returns to being the "topic domain".
TOOLSET_STAGE_LABEL = {
    "raw": "raw event table", "agg": "derived aggregate table", "insight": "insight SQL",
    "tool": "report tool (aggregation inside the tool)",
}
SUBJECT_LABEL = {
    "player": "Player", "team": "Team", "map": "Map", "series": "Series",
    "round": "Round", "game": "Game", "tournament": "Tournament",
}
# Topic domain: specific before general — reverse the order and "round" swallows all of "first
# blood".
DOMAIN_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("first blood duels", ("first_blood", "fb_", "opening_duel")),
    ("plant / defuse", ("plant", "defuse")),
    ("pistol rounds", ("pistol",)),          # must come before "eco rounds": more specific
    ("eco rounds", ("eco", "economy")),
    ("pattern detection", ("pattern",)),
    ("win / losing streaks", ("streak",)),
    ("player performance", ("kast", "kd_ratio", "adr", "impact", "player")),
    ("results and score", ("win", "won", "score")),
    ("maps", ("map",)),
    ("abilities / items", ("ability",)),
    ("rounds", ("round",)),
    ("series / tournaments", ("series", "tournament")),
]
# Chapter → subject fallback: dimensions aggregated inside a tool have no recognisable column
# (pistol_win_rate / eco_win_rate), but every knowledge point in the companion has a subject, so it
# must not be left empty.
CHAPTER_SUBJECT = {
    "player performance": "player", "maps": "map", "first blood duels": "team",
    "eco rounds": "team", "pistol rounds": "team", "win / losing streaks": "team",
    "results and score": "team", "plant / defuse": "team", "pattern detection": "round",
    "rounds": "round", "series / tournaments": "series", "abilities / items": "player",
}


def _data_source() -> str:
    """Which **database** is currently connected (vlml0 / vlml_vlr / some future source).

    ⚠️ The mapping was changed once on 2026-10-08: it used to be treated as the companion's
    `course_family` (course family), which was **wrong**. See the companion's
    `entry_tutor_question_entries.py:1043-1051`:

        store.list_topics(5000, scope.subject, scope.stage,
                          chapter=scope.chapter, unit=scope.unit,
                          course_family=scope.course_family)

    —— subject / stage / chapter / unit / course_family are **all `list_topics` filter
    parameters**, not hierarchical ownership on the knowledge points. The knowledge point store is
    **one shared copy**; switching course family only decides "which ones get practised", and the
    knowledge points themselves are never rebuilt.

    So:
      * series id = the companion's `course_family` (source/version, knowledge points still shared)
      * concrete team / player / map values = the companion's `entity`
        (`_semantic_routing.py:149`) — a runtime field, **not in the topics table**, so it does not
        enter the knowledge structure
      * which database is connected = **MVE's own `data_source`**, with no counterpart in the
        companion
    """
    try:
        import subjects
        return subjects.data_source()
    except Exception:
        return "vlml0"


def _entity_values(kind: str) -> list[str]:
    """The **fillable concrete values** under this subject (series id / team name / map name /
    player name).

    Reads only two **persisted caches**, it does not connect to the database here —
    the graph must stay "no database connection, dashboard opens instantly". The caches are
    refreshed by `entity_catalog.py`.

    A subject is a **human-fillable ownership container** (`subjects.py`), not something enumerated
    from the database: after connecting a new data source (swapping to another public source), a
    human first fills in a subject (say a given tournament / team domain), and the entries VLML /
    Voyager produce afterwards hang beneath it. So this is the merge of "actually queried" and
    "registered by a human" — taking only the former leaves it empty when the database is swapped,
    and with no database connected there is nothing to choose from.
    """
    if not kind:
        return []
    vals: list[str] = []
    try:
        raw = json.loads((HERE / "entity_catalog.json").read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    items = (raw.get("entities") or {}).get(kind) or []
    for it in items:
        v = str(it.get("value") or "")
        if v and v not in vals:
            vals.append(v)
    try:
        import subjects
        for v in subjects.values(kind):
            if v and v not in vals:
                vals.append(v)
    except Exception:
        pass
    return vals


def _subject_key_of(col: str) -> str:
    """Column name → subject (series/team/player/…). Mirrors `question_gen._subject_key_of`.

    Copied here rather than imported: question_gen pulls in vlml_env (connects to the database),
    while knowledge_graph must stay lightweight — "standard library imports only".
    """
    c = str(col or "").lower()
    if "player" in c:
        return "player"
    if c.startswith("map") or c.endswith("_map"):
        return "map"
    if "team" in c:
        return "team"
    if "series" in c:
        return "series"
    if "tournament" in c:
        return "tournament"
    if "round" in c:
        return "round"
    if "game" in c:
        return "game"
    return ""


def _subjects_of(d: dict[str, Any]) -> list[str]:
    """Which **subjects** (entity domains) this knowledge point belongs to — all derived from real
    columns, nothing hand-written."""
    keys: list[str] = []
    for c in list(d.get("columns") or []):
        k = _subject_key_of(c)
        if k and k not in keys:
            keys.append(k)
    # The tool path has no columns: recognise them from value_path instead
    # (key_metrics.economy.pistol → no entity domain)
    for c in str(d.get("value_path") or "").split("."):
        k = _subject_key_of(c)
        if k and k not in keys:
            keys.append(k)
    return keys


def _stage_of(d: dict[str, Any]) -> str:
    """The lowest level of **tool set** needed to master this knowledge point (the gradient).

    tool (only reachable via aggregation inside a report tool) > insight (needs insight SQL /
    multi-table JOIN / window functions) > agg (query a derived aggregate table) > raw (query the
    raw event table directly).
    """
    if d.get("tool") and not d.get("tables"):
        return "tool"
    tables = [str(t) for t in (d.get("tables") or [])]
    recipe = str(d.get("recipe") or "")
    # Insight level: multi-table JOIN + window function/subquery — precisely the shape of insight SQL
    if len(tables) >= 3 or _sql_difficulty(recipe) >= 4:
        return "insight"
    if any(t.startswith("agg_") for t in tables):
        return "agg"
    return "raw" if tables else "tool"


def _chapter_of(dim: str, d: dict[str, Any]) -> str:
    """The **topic domain** under the subject (isomorphic to the companion's chapter).

    Recognised from the dimension name + table names + metric definition; when unrecognised it
    falls back to the data layer name — better coarse than invented.
    """
    hay = " ".join([str(dim or "").lower(),
                    " ".join(str(t) for t in (d.get("tables") or [])),
                    str(d.get("semantics") or "").lower(),
                    str(d.get("value_path") or "").lower()])
    for label, kws in DOMAIN_HINTS:
        if any(k in hay for k in kws):
            return label
    primary = (d.get("tables") or [""])[0]
    return (LAYER_LABEL.get(str((d.get("layer") or "")), "")
            or (f"table {primary}" if primary else "aggregation inside a tool"))


def _table_depth(g: KnowledgeGraph, name: str, seen: set[str] | None = None) -> int:
    """How many derivation layers deep this table sits in the data flow (isomorphic to the
    companion's `depth`)."""
    seen = seen or set()
    nid = f"table:{name}"
    if nid in seen:
        return 0
    seen.add(nid)
    ups = [e.src[len("table:"):]
           for e in g._in.get(nid, []) if e.relation == DERIVED_FROM]
    ups = [u for u in ups if u != name]
    if not ups:
        return 0
    return 1 + max(_table_depth(g, u, seen) for u in ups)


def _add_dimension_declarations(g: KnowledgeGraph, specs: dict[str, Any]) -> None:
    """Fill in companion-style declaration fields on the 15 dimension nodes.

    Previously these fields **existed only as the handful that tasks.py derives on the fly** inside
    detail (tables / columns / semantics), and not one of them was a "teaching declaration" —
    no difficulty, no unit, no prerequisites, no knowledge-point-level typical mistakes. When the
    question generator wanted to pick "a point one tier harder" it had nowhere to start, so it
    counted SQL keywords itself (and miscounted).
    """
    tbl_specs = specs.get("tables") or {}
    # Difficulty must be counted from the **full SQL**, not from detail's `semantics` —
    # semantics is only the SELECT projection ("ROUND(AVG(fb_team_won)*100,1) AS conv"), with no
    # GROUP BY / JOIN inside, so a question that clearly needs grouping was judged tier 1
    # (measured with map_fb_conv: the question generator ranked it last as "the easiest").
    sql_by_dim: dict[str, list[str]] = {}
    try:
        from tasks import TASKS
        for _tid, _t in TASKS.items():
            for _p in getattr(_t, "rubric", None) or []:
                _s = getattr(_p, "answer_spec", None)
                if _s is not None and getattr(_s, "sql", ""):
                    sql_by_dim.setdefault(str(_p.dimension), []).append(
                        _desensitize(str(_s.sql)))
    except Exception:
        sql_by_dim = {}

    for dim in sorted(g.dimensions()):
        node = g.nodes.get(f"dim:{dim}")
        if node is None:
            continue
        d = node.detail
        tables = list(d.get("tables") or [])
        recipe = str(d.get("recipe") or d.get("semantics") or "")
        # Only `recipe` (a real SQL skeleton) counts. For the tool path `semantics` is
        # a sentence of prose, so counting keywords in it yields tier 1 out of thin air — a tool
        # dimension should be 0.
        sqls = sql_by_dim.get(dim) or ([d["recipe"]] if d.get("recipe") else [])

        # --- difficulty: carried by the graph, so the question generator no longer counts separately ---
        # The tool path has no SQL to count, so it is marked 0 (it does not take part in the 1-4
        # tiers) rather than pretending to be tier 1.
        d["difficulty"] = (max((_sql_difficulty(s) for s in sqls), default=0)
                           if sqls else 0)
        # --- unit / skills / question_types ---
        d["unit"] = _unit_of(dim, d)
        skills = _skills_of(" \n ".join(sqls))
        if skills:
            d["skills"] = skills
        d["question_types"] = (["sql_recipe"] if d.get("tables")
                               else ["tool_recipe"])

        # --- The companion's four layers: stage (tool-set gradient) / subject (entity domain) /
        #     chapter (topic domain) ---
        # Previously this only had chapter + depth, and chapter held the data layer name (core table
        # / derived table) — which is the **gradient**, occupying the companion's stage slot. Now it
        # is put back in place.
        d["stage"] = _stage_of(d)
        d["stage_label"] = TOOLSET_STAGE_LABEL.get(d["stage"], d["stage"])
        subs = _subjects_of(d)
        d["subjects"] = subs
        # The companion's `topic["subject"]` is single-valued (one knowledge point belongs to one
        # subject); MVE's dimensions often span entities (map_fb_conv is both a map and a team), so
        # the primary subject is taken as the **most specific** one:
        # player > map > team > series.
        d["subject"] = (next((k for k in ("player", "map", "team", "series",
                                          "round", "game", "tournament")
                              if k in subs), (subs[0] if subs else "")))
        d["chapter"] = _chapter_of(dim, d)
        if not d["subject"]:
            # Dimensions aggregated inside a tool have no recognisable column
            # (pistol_win_rate / eco_win_rate), so the subject is back-derived from the chapter —
            # better coarse than left empty (every knowledge point in the companion has a subject).
            d["subject"] = CHAPTER_SUBJECT.get(d["chapter"], "")
        d["subject_label"] = SUBJECT_LABEL.get(d["subject"], d["subject"])
        # Which **values** this column accepts (series id / team name / …): a human fills it in when
        # importing or when generating a question, and swapping the value yields a new question and
        # also a transfer (can the same knowledge point be computed for a different team?).
        # Measured: the data source has only 1 tournament / 2 teams / 3 maps / 10 players, so the
        # tournament column cannot have its value swapped — present it honestly rather than
        # pretending there is a choice.
        d["subject_values"] = _entity_values(d["subject"])
        primary = tables[0] if tables else ""
        d["depth"] = (1 + max((_table_depth(g, t) for t in tables), default=0)
                      if tables else 0)

        # --- prerequisites: prerequisite = an upstream table (isomorphic to the companion's
        #     prerequisites) ---
        ups: list[str] = []
        for t in tables:
            # `upstream_tables` returns **node ids**, not table names — forgetting to strip the
            # prefix yields "table:table:base_events" (measured exactly that).
            for u in g.upstream_tables(t, depth=2):
                u = u[len("table:"):] if u.startswith("table:") else u
                if u != t and u not in ups:
                    ups.append(u)
        # A derived table is itself "you must know its upstream before you can use it"; add a layer
        # of upstreams declared by the modelling doc too
        for t in tables:
            for u in (tbl_specs.get(t) or {}).get("upstream") or []:
                if u != t and u not in ups:
                    ups.append(u)
        d["prerequisites"] = [
            {"id": f"table:{u}", "relation": "prerequisite",
             "reason": f"the table {primary or '(tool)'} read by {dim} is computed from {u}"
                       + (f" ({(tbl_specs.get(u) or {}).get('grain') or ''})"
                          if (tbl_specs.get(u) or {}).get("grain") else "")}
            for u in ups[:4]]

        # --- related: confusable / co-occurring (the edges already exist in the graph; here they
        #     become readable on the node) ---
        rel: list[dict[str, str]] = []
        for e in g._out.get(f"dim:{dim}", []) + g._in.get(f"dim:{dim}", []):
            other = e.dst if e.src == f"dim:{dim}" else e.src
            if not other.startswith("dim:") or other == f"dim:{dim}":
                continue
            if e.relation == CONFUSABLE:
                rel.append({"id": other, "relation": "confusable"})
            elif e.relation == CO_OCCURS:
                rel.append({"id": other, "relation": "co_occurs"})
        seen_rel: set[str] = set()
        rel = [r for r in rel if not (r["id"] in seen_rel or seen_rel.add(r["id"]))]
        if rel:
            d["related"] = rel[:6]

        # --- typical_misconceptions: seed mistakes + mistakes derived from the modelling doc ---
        misc = list(d.get("typical_errors") or [])
        for t in tables:
            s = tbl_specs.get(t) or {}
            grain = str(s.get("grain") or "")
            if s.get("layer") == "derived" and "round" in grain:
                misc.append(
                    f"{t} is already one row per \"{grain}\" (primary key "
                    f"{','.join(s.get('pk') or []) or 'see the doc'}), "
                    "so do not JOIN rounds to deduplicate — that multiplies the row count")
            for m, formula in list((s.get("metrics") or {}).items())[:1]:
                misc.append(f"{t}.{m} is computed by \"{formula}\", "
                            "so the same-named column must not be AVG'd directly")
            break
        if misc:
            d["typical_misconceptions"] = list(dict.fromkeys(misc))[:5]

        # --- examples: metric definition examples (isomorphic to the companion's examples) ---
        ex: list[str] = []
        if d.get("recipe"):
            ex.append(str(d["recipe"])[:400])
        elif d.get("semantics"):
            ex.append(str(d["semantics"])[:200])
        for t in tables[:1]:
            s = tbl_specs.get(t) or {}
            if s.get("example_sql"):
                ex.append(str(s["example_sql"])[:300])
        if ex:
            d["examples"] = ex[:2]
        d["name"] = dim
        # ⚠️ This **used to write `d["subject"] = "vlml0"`**, overwriting the real subject
        # (tournament/team/player) computed above — the subject slot was taken over by "data source".
        # Now: which database is connected = `data_source` (MVE's own key), and the subject slot goes
        # back to the entity domain.
        #
        # A knowledge point is **not hard-wired to a given tournament/team**: per the companion's
        # `entry_tutor_question_entries.py:1043-1051`, subject / chapter / course_family are all
        # **list_topics filter parameters**, and the knowledge point store is one shared copy.
        # Writing a tournament id into a knowledge point means rebuilding the tool set every time
        # the tournament changes — wasted effort. In the user's own words: "the knowledge points and
        # tool sets under a subject are all the same anyway".
        d["data_source"] = _data_source()
        # Which **values** this knowledge point can be filtered by (series id / team name / …) —
        # nullable, semantically the same as the companion's runtime `entity`: it only affects this
        # retrieval, it does not change the knowledge structure.
        d["entity_values"] = _entity_values(d["subject"])
        d["declared_by"] = ("VLML modelling doc + tasks.py's answer_spec"
                            " (difficulty/unit/prerequisites derived from the doc, metric "
                            "definitions fixed by answer_spec)")


def _add_vlml_schema(g: KnowledgeGraph, schema: dict[str, Any]) -> None:
    """Write VLML's four-layer structure into the graph: tables / derivation relations / insights /
    tool sections.

    Everything comes from **live queries and source parsing** (`vlml_schema.py`); not one entry is
    hand-written.
    """
    import vlml_schema

    # --- Tables: 21, with stage / grain / columns / row count ---
    for name, info in sorted((schema.get("tables") or {}).items()):
        g.add_node(Node(
            id=f"table:{name}", kind="table", label=name,
            detail={
                "stage": info.get("stage", ""),
                "grain": info.get("grain", ""),
                "columns": list(info.get("columns") or [])[:32],
                "n_columns": len(info.get("columns") or []),
                "rows": info.get("rows", 0),
                "source": info.get("source", ""),
            }))

    # --- Derivation: agg table ← upstream table (an upstream in the data flow = the isomorphic
    #     counterpart of the companion's prerequisite) ---
    for target, ups in sorted((schema.get("lineage") or {}).items()):
        for up in ups:
            g.add_edge(Edge(
                src=f"table:{up}", dst=f"table:{target}",
                relation=DERIVED_FROM, origin="declared",
                reason=f"defined by transformations: {target} is computed from {up}",
                confidence=1.0))

    # --- Insights: 46 SQL units ---
    for name, info in sorted((schema.get("insights") or {}).items()):
        g.add_node(Node(
            id=f"insight:{name}", kind="insight", label=name,
            detail={
                "stage": vlml_schema.STAGE_INSIGHT,
                "tables": info.get("tables") or [],
                "reports": info.get("reports") or [],
                "purpose": info.get("purpose", ""),
                "file": info.get("file", ""),
            }))
        for t in info.get("tables") or []:
            g.add_edge(Edge(
                src=f"table:{t}", dst=f"insight:{name}",
                relation=READS, origin="declared",
                reason=f"the FROM/JOIN of {info.get('file', name)}",
                confidence=1.0))
        for r in info.get("reports") or []:
            if f"tool:{r}" in g.nodes:
                g.add_edge(Edge(
                    src=f"tool:{r}", dst=f"insight:{name}",
                    relation=COMPOSES, origin="declared",
                    reason=f"{r} uses it via load_sql", confidence=0.9))

    # --- Tools: fill in sections (the insight directory) ---
    for name, info in sorted((schema.get("tools") or {}).items()):
        nid = f"tool:{name}"
        if nid not in g.nodes:
            g.add_node(Node(id=nid, kind="tool", label=name))
        node = g.nodes[nid]
        node.detail["stage"] = vlml_schema.STAGE_TOOL
        node.detail["sections"] = info.get("sections") or []
        # section → which insight SQLs compose it (the insight layer reaches the prompt through this
        # table)
        si = (schema.get("section_insights") or {}).get(name) or {}
        if si:
            node.detail["section_insights"] = si
        desc = info.get("section_desc") or {}
        if desc:
            node.detail["section_desc"] = dict(list(desc.items())[:12])

    # --- Dimensions: mark which section of the tool they take (the first segment of value_path is
    #     the section) ---
    # This is the layer of "knows which tool to call, does not know which section the value comes
    # from".
    for node in list(g.nodes.values()):
        if node.kind != "dimension":
            continue
        node.detail["stage"] = vlml_schema.STAGE_DIMENSION
        path = str(node.detail.get("value_path") or "")
        head = path.split(".")[0] if path else ""
        if not head:
            continue
        node.detail["section"] = head
        # Find which tools list this section among their sections (several tools may produce the
        # same section)
        for tnode in g.nodes.values():
            if tnode.kind != "tool":
                continue
            if head in (tnode.detail.get("sections") or []):
                g.add_edge(Edge(
                    src=tnode.id, dst=node.id,
                    relation=FROM_SECTION, origin="declared",
                    reason=f"the head of value_path `{head}` is a section of {tnode.label}",
                    confidence=0.8))


def _add_seed_facts(g: KnowledgeGraph, schema: dict[str, Any]) -> None:
    """The seed layer: the complete set of metrics **independent of any question** (isomorphic to
    the companion's topics).

    Why it is mandatory: the dimension nodes in `build()` all come from `TASKS[].rubric` —
    only questions that have been generated enter the graph (measured: just 13 dimensions, covering
    5 questions). When a new question arrives the graph holds none of its nodes and
    `subgraph_for_topic` returns found=False straight away.
    The companion does not work that way: its 82 knowledge point seeds exist prior to any question,
    and a question merely uses `match_topics(query)` to match the focus, so **any** question can be
    paired with a graph.
    That is ported verbatim here: every askable fact (insight / tool section / table) is extracted
    from the four-layer structure discovered from VLML, depending on no question at all.
    """
    try:
        import graph_topics
    except Exception:                                   # pragma: no cover
        return
    facts = graph_topics.build_seed_facts(schema)
    for n in graph_topics.seed_facts_to_nodes(facts):
        g.add_node(Node(id=n["id"], kind=n["kind"], label=n["label"],
                        detail=n["detail"]))
    # Fact → its source (tool / section / insight / table), so focused_context can bucket it
    for f in facts:
        fid = str(f.get("id") or "").strip()
        if not fid or fid not in g.nodes:
            continue
        tool = str(f.get("unit") or "").strip()
        if tool and f"tool:{tool}" in g.nodes:
            g.add_edge(Edge(src=f"tool:{tool}", dst=fid,
                            relation=PRODUCED_BY, origin="declared",
                            reason="VLML source: that tool produces this fact",
                            confidence=0.9))
        for i in f.get("insights") or []:
            if f"insight:{i}" in g.nodes:
                g.add_edge(Edge(src=f"insight:{i}", dst=fid,
                                relation=COMPOSES, origin="declared",
                                reason=f"{i} is its computation unit", confidence=0.9))
        for t in f.get("tables") or []:
            if f"table:{t}" in g.nodes:
                g.add_edge(Edge(src=f"table:{t}", dst=fid,
                                relation=DERIVED_FROM, origin="declared",
                                reason=f"data read from {t}", confidence=0.9))
    g.detail_seed_facts = len(facts)    # type: ignore[attr-defined]


async def _observe_procedure(g: KnowledgeGraph) -> None:
    """Run the referee once and fill in the procedure_step edges with the **measured** orchestration
    order.

    Declarative information can answer "which tool produces which dimension", but it cannot answer
    "which is called first, which second" — that order is only knowable by actually running it
    (the referee's trajectory *is* the measured order).
    """
    import vlml0_referee
    from tasks import TASKS

    for topic_id, task in TASKS.items():
        try:
            ref = await vlml0_referee.answer(task)
        except Exception:
            continue
        traj = [str(t) for t in (getattr(ref, "trajectory", None) or [])
                if not str(t).startswith("<")]
        for i in range(len(traj) - 1):
            a, b = traj[i], traj[i + 1]
            if a == b:
                continue
            if f"tool:{a}" not in g.nodes:
                g.add_node(Node(id=f"tool:{a}", kind="tool", label=a))
            if f"tool:{b}" not in g.nodes:
                g.add_node(Node(id=f"tool:{b}", kind="tool", label=b))
            g.add_edge(Edge(src=f"tool:{a}", dst=f"tool:{b}",
                            relation=PROCEDURE_STEP, origin="observed",
                            reason=f"{topic_id}: orchestration order measured by the referee",
                            confidence=0.95))
        # Measured produced_by: the source.tool of a referee fact is the tool that truly retrieved it
        for f in (getattr(ref, "facts", None) or []):
            src = (f.get("source") or {}).get("tool") or ""
            if not src.startswith("vlml0_referee::"):
                continue
            tool = src.split("::", 1)[1]
            dim = str(f.get("dimension") or "")
            if not dim:
                continue
            if f"tool:{tool}" not in g.nodes:
                g.add_node(Node(id=f"tool:{tool}", kind="tool", label=tool))
            if f"dim:{dim}" not in g.nodes:
                g.add_node(Node(id=f"dim:{dim}", kind="dimension", label=dim))
            g.add_edge(Edge(src=f"tool:{tool}", dst=f"dim:{dim}",
                            relation=PRODUCED_BY, origin="observed",
                            reason=f"{topic_id}: measured as retrieved by the referee",
                            confidence=1.0))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _main() -> int:
    ap = argparse.ArgumentParser(description="MVE knowledge graph: dimensions / tools / tables")
    ap.add_argument("--build", action="store_true", help="rebuild the graph and persist it")
    ap.add_argument("--observe", action="store_true",
                    help="with --build, additionally run the referee to fill in measured edges "
                         "(slow)")
    ap.add_argument("--who", default="", help="look up which tool produces this dimension")
    ap.add_argument("--topic", default="",
                    help="list the dimensions this question touches and their sources")
    ap.add_argument("--render", default="",
                    help="render this question's subgraph as text for the model (the equivalent of "
                         "control_primitives)")
    ap.add_argument("--no-recipe", action="store_true",
                    help="with --render, do not give the SQL structural skeleton (only semantics "
                         "and columns)")
    ap.add_argument("--stats", action="store_true", help="show statistics only")
    args = ap.parse_args()

    if args.build:
        g = build(observe=args.observe)
        g.save()
        s = g.to_dict()["summary"]
        print(f"Graph written to {GRAPH_PATH}")
        print(f"  dimensions {s['dimensions']} · tools {s['tools']} · "
              f"tables {s['tables']} · edges {s['edges']}")
        for d in g.dimensions():
            prod = g.who_produces(d)
            tbl = g.tables_for(d)
            src = (f"tool {','.join(prod)}" if prod else
                   (f"table {','.join(tbl)}" if tbl else "(no source)"))
            print(f"    {d:<28} ← {src}")
        return 0

    g = KnowledgeGraph.load()
    if not g.nodes:
        print("The graph does not exist yet, run --build first")
        return 1

    if args.stats:
        print(json.dumps(g.to_dict()["summary"], ensure_ascii=False))
        return 0

    if args.who:
        sub = g.subgraph_for(args.who)
        if not sub.get("found"):
            print(f"The graph has no dimension {args.who}")
            return 1
        print(f"Dimension   : {args.who}")
        print(f"  Produced by tool: {', '.join(sub['produced_by']) or '(none)'}")
        print(f"  Computed from table: {', '.join(sub['derived_from']) or '(none)'}")
        print(f"  Confusable with: {', '.join(sub['confusable']) or '(none)'}")
        return 0

    if args.render:
        text = g.render_for_prompt(args.render, with_recipe=not args.no_recipe)
        if not text:
            print(f"The graph has no dimensions for the question {args.render}, run --build first")
            return 1
        print(text)
        return 0

    if args.topic:
        import vlml_env  # noqa: F401
        from tasks import TASKS
        task = TASKS.get(args.topic)
        if task is None:
            print(f"Unknown question {args.topic}")
            return 1
        print(f"Question    : {args.topic}")
        print(f"  Declared tools: {', '.join(task.requires_tools) or '(none)'}")
        for p in task.rubric:
            d = str(p.dimension)
            prod = g.who_produces(d)
            tbl = g.tables_for(d)
            src = (f"tool {','.join(prod)}" if prod else
                   (f"table {','.join(tbl)}" if tbl else "(no source)"))
            print(f"  {d:<28} ← {src}")
        return 0

    print("Usage: --build / --who <dimension> / --topic <question> / --stats")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
