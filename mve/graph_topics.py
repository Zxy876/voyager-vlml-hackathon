#!/usr/bin/env python3
"""The graph's "seed layer" — a **question-independent** full set of metrics + text matching + semantic-bucket compression.

Ported from the study companion's `knowledge_graph_guidance.py` / `knowledge_graph_index.py`.
Why port it: the study companion's graph is a **knowledge structure that exists before the questions** (82 topic seeds),
and questions merely use `match_topics(query=question text)` to match a focus, so any question works;
whereas MVE's earlier graph was a **projection of the question rubrics** — dimension nodes were derived from `TASKS[].rubric`,
so only the 5 questions ever asked had dimensions (13 observed). New questions matched nothing,
and the graph degenerated into "a memo for those 5 questions".

The study companion's three-stage pipeline, implemented isomorphically here:
    topics (full seeds)  →  match_topics(query)  →  SubgraphBudget pruning
    →  _build_focused_model_context (compressed into semantic buckets)
    fact (full metrics)  →  match_facts(query)  →  GraphBudget+relation_limits
    →  focused_context (compressed into semantic buckets)

Several constraints that must be copied verbatim (all read from the study companion source, not invented):
1. `raw_seed_included: False` (knowledge_graph_guidance.py:999) —
   never feed the model the whole 18-field raw seed; only the label list.
   MVE's isomorphic counterpart: never feed the answer_spec's raw SQL in (it is already a desensitized skeleton).
2. Only **one-hop direct edges** count, and direction matters (:1162-1169); drops must be counted
   (diagnostics: self_relation / nonincident / direction_mismatch).
3. `relation_limits` throttles per relation (knowledge_graph_index.py:121-134) —
   preventing one dense relation from starving the others.
4. `response_mode` routing (:1302-1310) — different stages take different relation subsets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Edge relation → semantic bucket (mirrors the 9 buckets in the study companion's _build_focused_model_context)
# --------------------------------------------------------------------------
PRODUCED_BY = "produced_by"      # dimension/fact ← tool
DERIVED_FROM = "derived_from"    # dimension/table ← table
READS = "reads"                  # insight ← table
COMPOSES = "composes"            # tool ← insight
FROM_SECTION = "from_section"    # dimension ← a section of the tool
CONFUSABLE = "confusable"
CO_OCCURS = "co_occurs"

BUCKET_OF = {
    PRODUCED_BY: "produced_by",
    DERIVED_FROM: "derived_from",
    READS: "reads",
    COMPOSES: "composes",
    FROM_SECTION: "section",
    CONFUSABLE: "confusions",
    CO_OCCURS: "review_with",
}

# The **canonical direction** of edges: src→dst, and which end the focus node sits on for that relation.
# Following the study companion's `_FOCUSED_RELATION_DIRECTION` idea — "prerequisite is incoming",
# and here "facts are produced by tools" is also incoming (the edge points from tool to fact).
FOCUSED_RELATION_DIRECTION = {
    PRODUCED_BY: "incoming",
    DERIVED_FROM: "incoming",
    READS: "incoming",
    COMPOSES: "incoming",
    FROM_SECTION: "incoming",
}
SYMMETRIC_RELATIONS = frozenset({CONFUSABLE, CO_OCCURS})

# Per-relation throttling (mirrors the 10 shapes of the study companion's SubgraphBudget.relation_limits)
RELATION_LIMITS: dict[str, int] = {
    PRODUCED_BY: 4,
    FROM_SECTION: 4,
    COMPOSES: 6,
    READS: 6,
    DERIVED_FROM: 5,
    CONFUSABLE: 3,
    CO_OCCURS: 3,
}

# Relation priority (mirrors the study companion's RELATION_PRIORITY: smaller is mentioned first)
RELATION_PRIORITY = {
    PRODUCED_BY: 0,
    FROM_SECTION: 1,
    COMPOSES: 2,
    READS: 3,
    DERIVED_FROM: 4,
    CONFUSABLE: 5,
    CO_OCCURS: 6,
}

PRIORITY_SCORE = {"core": 0, "useful": 1, "optional": 2}

# --------------------------------------------------------------------------
# response_mode routing (mirrors the study companion :1302-1310)
# --------------------------------------------------------------------------
# The study companion routes by "what the user is doing": problem_solving / general_explanation /
# general_discussion / unknown, each taking a different relation subset.
# MVE's isomorphic counterpart is the **current stage**:
#   plan    — have the model write a data-retrieval plan: metrics + tools + sections + confusables (full set)
#   explain — explanation/help (bypass_learn): structure and upstream only, no confusables (they induce)
#   judge   — scoring: value paths and metrics only
#   minimal — fallback
MODE_ALLOWED: dict[str, set[str]] = {
    "plan": {PRODUCED_BY, FROM_SECTION, COMPOSES, READS, DERIVED_FROM,
             CONFUSABLE, CO_OCCURS},
    "explain": {PRODUCED_BY, FROM_SECTION, COMPOSES, READS, DERIVED_FROM},
    "judge": {PRODUCED_BY, FROM_SECTION, DERIVED_FROM},
    "minimal": {PRODUCED_BY},
}
DEFAULT_MODE = "plan"

# --------------------------------------------------------------------------
# Generic terms (mirrors the study companion's GENERIC_QUERY_TERMS)
# --------------------------------------------------------------------------
# Question text is full of words like "help me compute", "how many", "analyze"; not removing them lets every fact score.
GENERIC_TERMS = {
    # English
    "what", "how", "many", "much", "the", "this", "that", "team", "match",
    "series", "game", "games", "round", "rounds", "map", "maps", "player",
    "players", "report", "analysis", "analyze", "analyse", "compute",
    "calculate", "get", "find", "show", "give", "tell", "please", "help",
    "question", "problem", "value", "number", "count", "total", "rate",
    "percent", "percentage", "average", "avg", "mean",
    "several", "which", "why", "data", "query", "stats", "situation",
    "performance", "result", "ratio",
}

# Term → English metric words.
# Source: labeled seeds; the study companion's aliases are also hand-written in seeds (topic aliases are human-given by nature),
# and the same applies here. It only acts as **aliases** and touches no values, so it does not constitute "me being the referee".
# Note: only "term → metric" mappings go here, never values.
TERM_SEED: dict[str, list[str]] = {
    "pistol round": ["pistol", "team_economy_pistol"],
    "pistol": ["pistol", "team_economy_pistol"],
    "eco round": ["eco", "team_economy_eco"],
    "economy": ["economy", "team_economy_pistol", "team_economy_eco"],
    "first blood": ["fb", "first_blood", "opening_duels", "agg_first_blood_stats"],
    "opening kill": ["fb", "first_blood", "opening_duels"],
    "opening death": ["fd", "opening_duels"],
    "conversion rate": ["conv", "conversion", "fb_conv"],
    "conversion": ["conv", "conversion"],
    "losing streak": ["streak", "max_losing_streak"],
    "winning streak": ["streak"],
    "round": ["rounds", "round"],
    "clutch": ["clutch", "clutches", "team_impact_metrics"],
    "multikill": ["multikills", "team_impact_metrics"],
    "kast": ["kast", "team_consistency_metrics"],
    "adr": ["adr", "team_consistency_metrics"],
    "rating": ["rating", "impact"],
    "second half": ["half_breakdown"],
    "overtime": ["half_breakdown"],
    "economy curve": ["economy_context"],
    "attack": ["attack_patterns"],
    "tactics": ["attack_patterns"],
    "timeline": ["round_timeline"],
    "map": ["map", "map_zones", "agg_team_map_stats"],
    "player": ["player", "player_performance", "agg_player_round_stats"],
    "team comparison": ["team_comparison"],
    "key metrics": ["key_metrics"],
    "highlights": ["highlight_rounds"],
    "round situations": ["round_situations"],
}


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _snake_parts(name: str) -> list[str]:
    """`team_economy_pistol` → ['team', 'economy', 'pistol']."""
    return [p for p in re.split(r"[^a-z0-9]+", _text(name).lower()) if p]


def _tokens(query: str, *, strip_generic: bool = True) -> list[str]:
    """Question-text tokenization: English by [a-z0-9]+, Chinese by 2-4 character N-grams, with term mapping."""
    raw = _text(query).lower()
    if not raw:
        return []
    out: set[str] = set()
    for tok in re.findall(r"[a-z0-9_]+", raw):
        for part in _snake_parts(tok):
            if len(part) >= 2:
                out.add(part)
    # Chinese: replace terms first, then take 2-4 grams of the remaining fragments
    cjk = "".join(ch if "\u4e00" <= ch <= "\u9fff" else " " for ch in raw)
    for term, mapped in TERM_SEED.items():
        if term in cjk:
            out.update(mapped)
            cjk = cjk.replace(term, " ")
    for frag in cjk.split():
        if len(frag) < 2:
            continue
        out.add(frag)
        for size in (2, 3, 4):
            for i in range(0, len(frag) - size + 1):
                out.add(frag[i:i + size])
    if strip_generic:
        out = {t for t in out if t not in GENERIC_TERMS}
    return sorted(out, key=lambda x: (-len(x), x))


def _aliases_of(fact: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("id", "label", "unit", "chapter", "section"):
        v = _text(fact.get(key)).lower()
        if v:
            out.append(v)
            out.extend(_snake_parts(v))
    out.extend(_text(a).lower() for a in (fact.get("aliases") or []))
    out.extend(_text(a).lower() for a in (fact.get("insights") or []))
    for t in (fact.get("tables") or []):
        out.extend(_snake_parts(_text(t)))
    seen, uniq = set(), []
    for a in out:
        if a and a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def _search_text(fact: dict[str, Any]) -> str:
    parts = [_text(fact.get(k)) for k in
             ("id", "label", "unit", "chapter", "section", "tool")]
    parts += [_text(a) for a in (fact.get("aliases") or [])]
    parts += [_text(a) for a in (fact.get("insights") or [])]
    parts += [_text(a) for a in (fact.get("tables") or [])]
    return " ".join(p for p in parts if p).lower()


# --------------------------------------------------------------------------
# Focus matching (mirrors the study companion's match_topics)
# --------------------------------------------------------------------------
def match_facts(facts: list[dict[str, Any]], *, topic_id: str = "",
                query: str = "", limit: int = 5) -> list[dict[str, Any]]:
    """Question text → focus metrics.

    Scoring shape mirrors the study companion's `match_topics` (knowledge_graph_guidance.py:481):
      - topic_id exact hit → score 100 (:491-500)
      - full label/alias covered by the question text → +100 (:534-544)
      - label appears in terms → +40; alias → +36; prefix → +18; contains → +10;
        haystack → +3
    Why it exists: questions **do not need to declare in advance** which metrics they test. The study companion uses this to attach a graph to any new question,
    and MVE lacked it before, so the graph only worked for questions already asked.
    """
    limit = max(1, int(limit or 5))
    by_id = {_text(f.get("id")): f for f in facts if _text(f.get("id"))}
    key = _text(topic_id)
    if key and key in by_id:
        return [{"id": key, "label": _text(by_id[key].get("label")) or key,
                 "score": 100, "match": "topic_id"}]

    text = query or topic_id
    terms = _tokens(text)
    if not terms:
        return []
    # A **full table name** in the question text ("agg_team_game_stats") is the strongest kind of evidence:
    # observed when asking "what is this table's grain", insights citing that table outscore the table itself on scattered tokens.
    compact_q = re.sub(r"[^a-z0-9]+", "", text.lower())
    scored: list[dict[str, Any]] = []
    for fact in facts:
        fid = _text(fact.get("id"))
        if not fid:
            continue
        label = _text(fact.get("label")).lower()
        aliases = _aliases_of(fact)
        haystack = _search_text(fact)
        score = 0
        matched: list[str] = []
        for tname in (fact.get("tables") or []):
            ct = re.sub(r"[^a-z0-9]+", "", _text(tname).lower())
            if ct and len(ct) >= 6 and ct in compact_q:
                score += 150
                matched.append(_text(tname))
        # Full label covered: the strongest evidence
        label_parts = _snake_parts(label) or ([label] if label else [])
        if label_parts and set(label_parts).issubset(set(terms)):
            score += 100
            matched.append(label)
        if label and label in terms:
            score += 40
            matched.append(label)
        for alias in aliases:
            if alias in terms:
                score += 36
                matched.append(alias)
        for term in terms:
            if term == fid.lower():
                score += 20
            elif term in aliases:
                score += 18
            elif any(term in a for a in aliases):
                score += 8 if len(term) >= 3 else 5
            elif label.startswith(term):
                score += 18
            elif term in label:
                score += 10
            elif term in haystack:
                score += 3
            else:
                continue
            matched.append(term)
        if score:
            scored.append({"id": fid, "label": _text(fact.get("label")) or fid,
                           "score": score, "match": "query",
                           "matched_terms": list(dict.fromkeys(matched))[:6]})
    return sorted(scored,
                  key=lambda i: (-int(i["score"]), len(_text(i["label"])),
                                 _text(i["label"])))[:limit]


# --------------------------------------------------------------------------
# Edge sorting (mirrors the study companion's _edge_sort_key)
# --------------------------------------------------------------------------
def edge_sort_key(query: str, edge: Any) -> tuple:
    relation = _text(getattr(edge, "relation", ""))
    priority = PRIORITY_SCORE.get("core", 0)
    overlap = 0
    if query:
        hay = " ".join(_text(x) for x in
                       (getattr(edge, "src", ""), getattr(edge, "dst", ""),
                        getattr(edge, "reason", ""))).lower()
        overlap = sum(1 for t in _tokens(query) if t and t in hay)
    return (RELATION_PRIORITY.get(relation, 99), priority,
            -float(getattr(edge, "confidence", 1.0) or 0.0), -overlap,
            _text(getattr(edge, "src", "")), _text(getattr(edge, "dst", "")))


# --------------------------------------------------------------------------
# Semantic bucket compression (mirrors the study companion's _build_focused_model_context)
# --------------------------------------------------------------------------
@dataclass
class FocusBudget:
    """Mirrors the study companion's SubgraphBudget; only the fields MVE actually uses are kept here."""
    max_focus: int = 3
    max_nodes: int = 20
    max_edges: int = 30
    relation_limits: dict[str, int] = field(
        default_factory=lambda: dict(RELATION_LIMITS))


def focused_context(graph: Any, selected_id: str, *, query: str = "",
                    mode: str = DEFAULT_MODE,
                    budget: FocusBudget | None = None) -> dict[str, Any]:
    """Take only the focus's **one-hop direct edges**, respecting direction, bucket by relation, counting drops.

    The study companion's exact words (:1073-1079): "The graph UI may include a multi-hop retrieved
    subgraph. Reusing its relation groups here previously made edges between
    two neighbouring topics look like direct relations of selected_id."
    That is: an edge between two neighbors in a multi-hop subgraph gets mistaken for a direct relation of the focus.
    MVE's isomorphic risk is identical (an agg table's upstream's upstream is not a fact's direct source), so here
    we strictly read only incident edges.
    """
    b = budget or FocusBudget()
    allowed = MODE_ALLOWED.get(_text(mode).lower(), MODE_ALLOWED[DEFAULT_MODE])
    diag = {"self_relation_dropped": 0, "nonincident_edge_dropped": 0,
            "direction_mismatch_dropped": 0, "mode_filtered": 0}

    ctx: dict[str, Any] = {
        "mode": _text(mode).lower() or DEFAULT_MODE,
        "query": _text(query),
        "focus": {"id": selected_id,
                  "label": _text(getattr(graph.nodes.get(selected_id), "label", ""))
                  if selected_id in getattr(graph, "nodes", {}) else ""},
    }
    for bucket in set(BUCKET_OF.values()):
        ctx[bucket] = []
    ctx["summary"] = {"raw_seed_included": False, "mode": ctx["mode"],
                      "diagnostics": diag}

    if selected_id not in getattr(graph, "nodes", {}):
        return ctx

    indexed = ([*graph._in.get(selected_id, []),
                *graph._out.get(selected_id, [])])
    buckets: dict[str, list[tuple[str, str]]] = {
        k: [] for k in set(BUCKET_OF.values())}
    seen: set[tuple[str, str, str]] = set()
    for edge in indexed:
        src, dst = _text(edge.src), _text(edge.dst)
        rel = _text(edge.relation)
        if (src, dst, rel) in seen:
            continue
        seen.add((src, dst, rel))
        if rel not in BUCKET_OF:
            continue
        if rel not in allowed:
            diag["mode_filtered"] += 1
            continue
        if src == selected_id and dst == selected_id:
            diag["self_relation_dropped"] += 1
            continue
        if selected_id not in {src, dst}:
            diag["nonincident_edge_dropped"] += 1
            continue
        other = dst if src == selected_id else src
        if not other or other == selected_id:
            diag["self_relation_dropped"] += 1
            continue
        if other not in graph.nodes:
            diag["nonincident_edge_dropped"] += 1
            continue
        incoming = (dst == selected_id)
        expect = FOCUSED_RELATION_DIRECTION.get(rel)
        if rel not in SYMMETRIC_RELATIONS:
            if expect == "incoming" and not incoming:
                diag["direction_mismatch_dropped"] += 1
                continue
            if expect == "outgoing" and incoming:
                diag["direction_mismatch_dropped"] += 1
                continue
        buckets[BUCKET_OF[rel]].append(
            (other, _text(graph.nodes[other].label) or other))

    for key, items in buckets.items():
        ordered = sorted(items, key=lambda i: (i[1].casefold(), i[0]))
        vals, seen_labels = [], set()
        for _oid, lab in ordered:
            if not lab or lab in seen_labels:
                continue
            seen_labels.add(lab)
            vals.append(lab)
            if len(vals) >= b.relation_limits.get(
                    _rel_of_bucket(key), b.max_nodes):
                break
        ctx[key] = vals
    ctx["summary"]["diagnostics"] = diag
    return ctx


def _rel_of_bucket(bucket: str) -> str:
    for rel, bk in BUCKET_OF.items():
        if bk == bucket:
            return rel
    return bucket


# --------------------------------------------------------------------------
# Seed layer: turn VLML structure into a "full metric set" (question-independent)
# --------------------------------------------------------------------------
def build_seed_facts(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """From the four-layer structure VLML discovers, extract every fact that **can be asked about**.

    The relationship to questions is zero: these facts exist whether or not questions were ever asked. The study companion's 82 topic
    seeds have the same property (existing before questions); questions merely reference them.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(fact: dict[str, Any]) -> None:
        fid = _text(fact.get("id"))
        if not fid or fid in seen:
            return
        seen.add(fid)
        out.append(fact)

    section_insights = schema.get("section_insights") or {}

    # 1) Insight-level facts: each of the 46 insights has a purpose ("Pistol round performance")
    for name, info in sorted((schema.get("insights") or {}).items()):
        purpose = _text(info.get("purpose"))
        if not purpose:
            continue
        aliases = list(_snake_parts(name))
        aliases += [p.lower() for p in _snake_parts(purpose)]
        cjk = "".join(ch if "\u4e00" <= ch <= "\u9fff" else " "
                      for ch in purpose)
        for term, mapped in TERM_SEED.items():
            if any(m in aliases for m in mapped):
                aliases.append(term)
        add({
            "id": f"fact:insight:{name}",
            "label": purpose,
            "subject": "valorant",
            "stage": "insight",
            "unit": (info.get("reports") or [""])[0],
            "chapter": "",
            "insights": [name],
            "tables": list(info.get("tables") or []),
            "aliases": sorted(set(aliases)),
            "origin": "vlml_source",
        })

    # 2) Section-level facts: tool × section (where answers actually land)
    for tool, sections in sorted((schema.get("tools") or {}).items()):
        for sec in (sections.get("sections") or []):
            ins = (section_insights.get(tool) or {}).get(sec) or []
            tbls: list[str] = []
            for i in ins:
                node = (schema.get("insights") or {}).get(i) or {}
                for t in node.get("tables") or []:
                    if t not in tbls:
                        tbls.append(t)
            aliases = list(_snake_parts(sec)) + list(_snake_parts(tool))
            for term, mapped in TERM_SEED.items():
                if any(m in aliases for m in mapped):
                    aliases.append(term)
            add({
                "id": f"fact:section:{tool}.{sec}",
                "label": f"{sec} ({tool})",
                "subject": "valorant",
                "stage": "tool",
                "unit": tool,
                "chapter": sec,
                "tool": tool,
                "section": sec,
                "insights": list(ins),
                "tables": tbls,
                "aliases": sorted(set(aliases)),
                "origin": "vlml_source",
            })

    # 3) Table-level facts: a table itself is askable ("how many rows / what grain does this table have")
    for name, info in sorted((schema.get("tables") or {}).items()):
        aliases = list(_snake_parts(name))
        for term, mapped in TERM_SEED.items():
            if any(m in aliases for m in mapped):
                aliases.append(term)
        add({
            "id": f"fact:table:{name}",
            "label": f"{name} ({info.get('grain') or 'table'})",
            "subject": "valorant",
            "stage": _text(info.get("stage")),
            "unit": "",
            "chapter": "",
            "tables": [name],
            "aliases": sorted(set(aliases)),
            "origin": "information_schema",
        })
    return out


def seed_facts_to_nodes(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert seed facts into graph nodes (kind='fact')."""
    nodes = []
    for f in facts:
        nodes.append({
            "id": _text(f.get("id")),
            "kind": "fact",
            "label": _text(f.get("label")) or _text(f.get("id")),
            "detail": dict(f),
        })
    return nodes
