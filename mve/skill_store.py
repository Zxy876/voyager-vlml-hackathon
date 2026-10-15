#!/usr/bin/env python3
"""Voyager skill library persistence — mirroring the original repo's SkillManager for "bloat prevention".

Three hard rules from the original repo's voyager/agents/skill.py that keep the skill library from exploding:

  1. Skills are not list.append, but `dict[program_name]` — same-named skills are **overwritten**, not added:
       if program_name in self.skills:
           print(f"Skill {program_name} already exists. Rewriting!")
           self.vectordb._collection.delete(ids=[program_name])
     (SkillManager.add_new_skill)

  2. Retrieval takes top_k only:
       k = min(self.vectordb._collection.count(), retrieval_top_k)   # retrieval_top_k=5
       docs_and_scores = self.vectordb.similarity_search_with_score(query, k=k)
     No matter how big the library grows, only 5 entries ever enter the prompt — this is the key to "the library grows but the prompt does not".

  3. Useless skills are simply not stored:
       if info["task"].startswith("Deposit useless items into the chest at"):
           return

This module copies those three verbatim:
  1. add() deduplicates by (topic, name), and on a hit does Rewriting (overwrite + archive old version + versions+1)
  2. retrieve() returns top_k entries only (vector cosine first, degrading to lexical when embedding is unavailable;
     same-topic priority + effectiveness weighting unchanged)
  3. add() drops low-value lessons (too short / nearly duplicate of an existing skill / merely restating the question)

Whether bloat was truly blocked must be observable — stats() gives the ratio of writes (cumulative write attempts)
to skills (actual entries): v1 was 15 writes → 15 entries (1.0, not blocked at all).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

STORE = Path(__file__).resolve().parent / "skill_store.json"

# Mirroring the original repo's retrieval_top_k=5: cap on lesson entries entering the prompt
RETRIEVAL_TOP_K = 5

# Name/content similarity thresholds (no vector DB; approximated with lexical Jaccard over mixed CN/EN text)
DUP_THRESHOLD = 0.62    # above → treated as the same skill → overwrite (Rewriting)
SKIP_THRESHOLD = 0.90   # above → content barely changed → skip even overwriting, discard directly
MIN_LESSON_LEN = 12     # Mirroring the "Deposit useless items" special case: too-short lessons have no reuse value
MAX_HISTORY = 3         # Max old versions kept (archived, not entering the prompt)

# Vector mode (embedding via Nebius Qwen3-Embedding-8B, same approach as the original but the whole stack stays inside
# Token Factory and does not pull in Chroma). The cosine distribution differs from Jaccard, so cross-topic thresholds must be set separately.
# 0.40 is the 2026-10-14 empirical calibration: 8 probes (Qwen3-Embedding-8B, 4096 dims) —
#   related (same axis rephrased / cross-language) 0.66-0.70; same-domain different question 0.45-0.47;
#   completely unrelated (braised pork / weather / poetry) 0.26-0.34 → boundary band 0.34-0.45, taking the middle, recall-leaning.
# Small sample (8 probes) makes this a calibration starting point, not a law; changing the embedding model requires re-measuring.
VEC_CROSS_MIN_SIM = 0.40    # Cross-topic threshold (vector mode): below it counts as unrelated

# Two thresholds for injecting cross-topic lessons into the prompt (without them, the previous question's lessons skew it)
CROSS_TOPIC_MIN_SIM = 0.12   # Lower bound on lexical relatedness to this question's text
CROSS_TOPIC_MAX = 2          # Max cross-topic entries


# ---------------- File I/O ----------------

def _empty() -> dict[str, Any]:
    return {
        "version": 2,
        "skills": {},
        "writes": 0,      # Cumulative attempted writes
        "added": 0,       # Actually created new
        "rewrites": 0,    # Overwritten (Rewriting)
        "skipped": 0,     # Discarded as useless/duplicate
    }


def _load_raw() -> dict[str, Any]:
    if not STORE.exists():
        return _empty()
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    # v1 is {"memory": [str, ...]} — migrate to v2, or the old library keeps treating entries as new
    if isinstance(data, dict) and isinstance(data.get("memory"), list):
        return _migrate_v1(data["memory"])
    if not isinstance(data, dict) or not isinstance(data.get("skills"), dict):
        return _empty()
    for k, v in _empty().items():
        data.setdefault(k, v)
    return data


def _migrate_v1(memory: list[str]) -> dict[str, Any]:
    """v1's list[str] → v2's dict. Old entries have no name; take the first 12 characters as the name."""
    out = _empty()
    now = time.time()
    for i, text in enumerate(memory):
        if not isinstance(text, str) or not text.strip():
            continue
        topic = ""
        body = text.strip()
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", body, re.S)
        if m:
            topic, body = m.group(1), m.group(2)
        name = re.sub(r"\s+", "", body)[:12] or f"skill{i}"
        out["skills"][_key(topic, name)] = {
            "topic": topic, "name": name, "text": body.strip(),
            "hits": 0, "ok": 0, "created": now, "updated": now,
            "versions": 1, "history": [],
        }
    out["writes"] = len(out["skills"])
    out["added"] = len(out["skills"])
    return out


def _save(data: dict[str, Any]) -> None:
    STORE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------- Name normalization and similarity ----------------

def norm_name(name: str) -> str:
    """Skill-name normalization: strip whitespace and punctuation, used as the primary key like program_name."""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(name or "")).lower()


def _key(topic: str, name: str) -> str:
    return f"{topic}::{norm_name(name)}"


def _tokens(s: str) -> set[str]:
    """Mixed CN/EN tokenization: English/digits by word, Chinese by 2-gram."""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(s or "").lower())
    toks: set[str] = set()
    for w in s.split():
        if re.fullmatch(r"[a-z0-9_]+", w):
            toks.add(w)
            continue
        if len(w) == 1:
            toks.add(w)
        for i in range(len(w) - 1):
            toks.add(w[i:i + 2])
    return toks


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------- Vector mode (mirroring the original SkillManager's vector retrieval, lightweight implementation) ----------------
# The original (voyager_original/voyager/agents/skill.py:43-47,117-130) uses Chroma +
# OpenAIEmbeddings for similarity retrieval. Here the idea is isomorphic but without heavy dependencies:
# embedding goes through voyager_vlml.llm.embed (Qwen3-Embedding-8B on Nebius Token Factory),
# vectors stored in JSON alongside skills, and cosine computed at retrieval time.
# When embedding is unavailable, everything degrades to lexical Jaccard — retrieval availability does not depend on Nebius being reachable.

def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def _skill_emb(s: dict[str, Any]) -> list[float]:
    v = s.get("emb")
    return [float(x) for x in v] if isinstance(v, list) and v else []


def _embed_one(text: str) -> list[float]:
    """Compute one text's vector; any failure returns [] (the caller degrades to lexical)."""
    try:
        from voyager_vlml import llm
        vs = llm.embed([str(text or "")[:2000]])
        return vs[0] if vs else []
    except Exception:
        return []


def _vec_or_lexical(qv: list[float], sv: list[float], q: str, text: str) -> tuple[float, bool]:
    """Use cosine with vectors, degrade to Jaccard without. Returns (sim, whether vector mode)."""
    if qv and sv:
        return _cos(qv, sv), True
    return similarity(q, text), False


# ---------------- Writing (dedup overwrite) ----------------

def add(
    topic: str,
    name: str,
    text: str,
    *,
    source: str = "practice",
    blueprint: dict[str, Any] | None = None,
    code: str = "",
) -> tuple[str, str]:
    """Write one skill. Returns (action, key), where action ∈ added / rewritten / skipped.

    Mirroring SkillManager.add_new_skill: same name → Rewriting, not append.

    `code` is the **solution source code** — the original stores `program_code` (full JS source),
    and `retrieve_skills` likewise returns `skills[name]["code"]`,
    with the same source spliced into the prompt (`programs` property).
    Previously there was only `text` here (one natural-language lesson), which the model read but did not change behavior for:
    observed injecting 2 lessons, trajectory still single-tool, coverage dropping to 30%.
    The real gap is right here — **the original learns code, MVE learned talk**.

    `source` distinguishes which channel a skill came through:
      - `practice` — the question-solving channel (solidify one's own approach when judged correct)
      - `bypass`   — the bypass channel (observing the referee's standard solution, see `bypass_learn.py`)
    Why the distinction matters: **when judged wrong we do not write our own approach** (it has been proven wrong),
    but what the bypass channel learns is the referee's approach, already verified correct — the two channels have different credibility,
    and a post-mortem must be able to tell whether a skill is "something I figured out" or "something I learned".
    """
    text = str(text or "").strip()
    # Mirroring the "Deposit useless items" special case: contentless lessons are not stored
    if len(text) < MIN_LESSON_LEN or text in {"none", "None", "(none)"}:
        data = _load_raw()
        data["writes"] += 1
        data["skipped"] += 1
        _save(data)
        return "skipped", ""

    data = _load_raw()
    data["writes"] += 1
    key = _key(topic, name)
    now = time.time()

    # Same-name primary key hit → overwrite directly
    hit = data["skills"].get(key)
    best_key, best_sim = key, 1.0 if hit else 0.0
    if not hit:
        # The LLM may give a different name each round — mirroring curriculum's qa_cache:
        # similarity_search_with_score < threshold counts as a cache hit, and must not be treated as a new entry
        probe = f"{name} {text}"
        for k, s in data["skills"].items():
            if s.get("topic") != topic:
                continue
            sim = similarity(probe, f"{s.get('name','')} {s.get('text','')}")
            if sim > best_sim:
                best_key, best_sim = k, sim

    if best_sim >= DUP_THRESHOLD and best_key in data["skills"]:
        s = data["skills"][best_key]
        # Content barely changed → skip even the overwrite (this is exactly the kind of write that bloated v1 by a dozen entries)
        #
        # **But if the source changed, it must be updated, even if the text is unchanged**. Observed pitfall: after the percent-metric
        # source changed from a "single .num path" to "num/denom two paths", the lesson sentence
        # was word-for-word identical → took the skipped branch → the library kept the old source → the model still dug
        # to numerator 3 and submitted it (3 ≠ 50.0). Identical text does not mean identical content.
        stale_code = bool(code) and code != str(s.get("code") or "")
        if similarity(text, s.get("text", "")) >= SKIP_THRESHOLD and not stale_code:
            data["skipped"] += 1
            _save(data)
            return "skipped", best_key
        hist = list(s.get("history") or [])
        hist.append(s.get("text", ""))
        s["history"] = hist[-MAX_HISTORY:]
        s["text"] = text
        s["name"] = str(name or s.get("name", ""))
        s["updated"] = now
        s["versions"] = int(s.get("versions") or 1) + 1
        s["source"] = str(source or s.get("source") or "practice")
        if blueprint:
            s["blueprint"] = blueprint
        if code:
            s["code"] = code
        emb = _embed_one(f"{s['name']} {text}")
        if emb:
            s["emb"] = emb
        data["rewrites"] += 1
        _save(data)
        return "rewritten", best_key

    emb = _embed_one(f"{name} {text}")
    data["skills"][key] = {
        "topic": topic, "name": str(name or ""), "text": text,
        "hits": 0, "ok": 0, "created": now, "updated": now,
        "versions": 1, "history": [],
        "source": str(source or "practice"),
        "blueprint": dict(blueprint or {}),
        "code": str(code or ""),
        **({"emb": emb} if emb else {}),
    }
    data["added"] += 1
    _save(data)
    return "added", key


# ---------------- Retrieval (top_k) ----------------

def retrieve(
    topic: str, query: str, top_k: int = RETRIEVAL_TOP_K
) -> list[dict[str, Any]]:
    """Mirroring SkillManager.retrieve_skills: only top_k entries enter the prompt.

    Dual-mode similarity (2026-10-14 vectorization rework):
      - **Vector mode** (default): both question and skill text go through Nebius embedding for cosine —
        mirroring the original's vector-retrieval approach in skill.py:117-130, bridging the
        semantic gap that lexical Jaccard cannot cross ("win rate after first blood" vs recipe text "first blood conversion analysis" barely overlap lexically).
      - **Lexical mode** (fallback): when embedding is unavailable / old skills have no vectors, fall back to
        "same-topic priority + Jaccard + effectiveness" scoring.
    Cross-topic thresholds are separated by mode (VEC_CROSS_MIN_SIM / CROSS_TOPIC_MIN_SIM) —
    cosine and Jaccard distributions differ, so sharing one threshold inevitably over-kills or under-passes.

    Two hard thresholds (observed: without them, cross-topic lessons skew the result):
      1. **Cross-topic lessons must be genuinely relevant** to be injected. When the library is small, top_k is not filled,
         so the previous question's lessons get force-stuffed into this question's prompt ("drill down query_sql by map dimension"
         applied to the pattern question, and Voyager goes off the rails). The original repo naturally
         blocks irrelevant skills via vector similarity; here an explicit relevance lower bound must be added.
      2. At most 2 cross-topic entries. What should dominate the prompt is this question's lessons.
    """
    data = _load_raw()
    qv = _embed_one(query)
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for key, s in data["skills"].items():
        same_topic = s.get("topic") == topic
        text = f"{s.get('name','')} {s.get('text','')}"
        sim, is_vec = _vec_or_lexical(qv, _skill_emb(s), query, text)
        floor = VEC_CROSS_MIN_SIM if is_vec else CROSS_TOPIC_MIN_SIM
        if not same_topic and sim < floor:
            continue                      # Threshold 1: irrelevant cross-topic lessons do not enter the prompt
        score = 3.0 if same_topic else 0.0
        score += 2.5 * sim
        hits = int(s.get("hits") or 0)
        score += 0.25 * min(hits, 6)
        score += 0.6 * (int(s.get("ok") or 0) / max(hits, 1))
        scored.append((score, key, s))
    scored.sort(key=lambda x: -x[0])

    out: list[dict[str, Any]] = []
    cross = 0
    for score, key, s in scored:
        if s.get("topic") != topic:
            if cross >= CROSS_TOPIC_MAX:  # Threshold 2
                continue
            cross += 1
        if len(out) >= max(1, top_k):
            break
        item = dict(s)
        item["key"] = key
        item["score"] = round(score, 3)
        out.append(item)
    return out


def mark_used(keys: list[str]) -> None:
    """Retrieved and injected into the prompt → hits +1."""
    if not keys:
        return
    data = _load_raw()
    touched = False
    for k in keys:
        if k in data["skills"]:
            data["skills"][k]["hits"] = int(data["skills"][k].get("hits") or 0) + 1
            touched = True
    if touched:
        _save(data)


def mark_result(keys: list[str], ok: bool) -> None:
    """Judged correct this round after injection → ok +1. Low-effectiveness skills naturally sink in retrieval."""
    if not keys or not ok:
        return
    data = _load_raw()
    touched = False
    for k in keys:
        if k in data["skills"]:
            data["skills"][k]["ok"] = int(data["skills"][k].get("ok") or 0) + 1
            touched = True
    if touched:
        _save(data)


# ---------------- Observability and compatibility ----------------

def stats() -> dict[str, Any]:
    """Whether bloat was truly blocked: see bloat = writes / skills."""
    data = _load_raw()
    n = len(data["skills"])
    writes = int(data.get("writes") or 0)
    return {
        "skills": n,
        "writes": writes,
        "added": int(data.get("added") or 0),
        "rewrites": int(data.get("rewrites") or 0),
        "skipped": int(data.get("skipped") or 0),
        "bloat": round(writes / n, 2) if n else 0.0,
        "top_k": RETRIEVAL_TOP_K,
    }


def all_skills() -> list[dict[str, Any]]:
    data = _load_raw()
    return [dict(s, key=k) for k, s in data["skills"].items()]


def seed_thinking() -> list[dict[str, Any]]:
    """General planning thinking distilled by MC (topic=mc-thinking): domain-agnostic
    'what to do first / what after' lessons used to seed the prompt's memory block.
    Returns an empty list when none have been distilled yet (callers skip the block)."""
    return [s for s in all_skills() if s.get("topic") == "mc-thinking"]


def load() -> list[str]:
    """Backward-compatible for old callers: returns all skill texts (with [topic] prefix). Display only."""
    return [f"[{s.get('topic','')}] {s.get('text','')}" for s in all_skills()]


def save(memory: list[str]) -> None:
    """Backward-compatible for old callers. Now a direct write with append semantics, bypassing dedup — for migration only."""
    for text in memory:
        topic, body = "", str(text)
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", body, re.S)
        if m:
            topic, body = m.group(1), m.group(2)
        add(topic, re.sub(r"\s+", "", body)[:12], body)


def reset() -> None:
    if STORE.exists():
        STORE.unlink()
    print(f"Skill library cleared: {STORE}")
