#!/usr/bin/env python3
"""Narrative insight layer: turn referee-confirmed facts into human-readable explanations.

This step sits at the very end of the pipeline. It runs only after Voyager's facts have
passed the referee:

    question generator issues a question -> Voyager orchestrates data retrieval -> fact set
        -> VLML0 referee cross-checks -> corroborated facts -> [this file] narrative insight
        -> panel "explanation output" area

Why it must run after cross-checking:
    The narrative is the final deliverable for the user (coach / analyst). If we feed all of
    Voyager's facts into the narrative without first passing the referee, we would be dressing
    wrong numbers up as fluent conclusions -- which is exactly what VLML set out to eliminate
    ("no hallucination, no fabricated data"). So this file collects only **covered
    (referee-approved)** facts, never value-mismatched ones.

Why a separate layer:
    VLML itself draws this boundary (README.md:97):
        "All reports return metrics and evidence only. LLMs should generate insights."
    Metrics and evidence come from tools; insight comes from the LLM; the two do not mix.

The narrative is tagged `comparable: false` -- **not part of cross-checking**.
This is the contract fixed in round 2 of the midwife: both machine-readable and
human-readable (panel dashed box) dual tags.
"""

from __future__ import annotations

import json
from typing import Any

from llm_client import chat  # noqa: E402

# VLML's own reporting standard (insights_reference.md):
#   :130 hard rule -- you must give numerator and denominator: "75% clutch win rate (3/4)",
#       never just "75%"
#   :122-127 four confidence tiers -- n>=100 Strong / 50-99 Moderate / 20-49 Weak / <20 Insufficient
NARRATOR_SYSTEM = """You are a Valorant esports data analyst (from the coach's perspective).

Iron rules:
1. Use only the facts given below; never introduce any number that does not appear in the data.
2. Every conclusion must trace back to a fact; when citing a percentage you **must include the
   denominator**, written as "67% (16/24)", never just "67%".
3. With a small sample size you must state it (denominator < 20 marks Weak, < 50 marks Moderate).
4. No predictions, no filler beyond tactical advice.

Output a JSON:
{"narrative": "3-5 sentence coherent analysis, for the coach",
 "insights": ["one actionable insight", "at most 3"],
 "caveats": ["the data's limitations, e.g. small sample size", "may be an empty array"]}"""


def confidence_label(base: int | None, min_base: int = 20) -> str:
    """Per VLML insights_reference.md:122-127 four confidence tiers."""
    if base is None:
        return ""
    if base >= 100:
        return "Strong"
    if base >= 50:
        return "Moderate"
    if base >= min_base:
        return "Weak"
    return "Insufficient"


def _fmt_fact(f: dict[str, Any]) -> str:
    subj = "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items())
    base = f.get("base")
    conf = confidence_label(base)
    val = f.get("value")
    unit = f.get("unit") or "raw"
    suffix = "%" if unit == "percent" else ""
    base_txt = f", denominator {base}" if base is not None else " (no denominator)"
    conf_txt = f", confidence {conf}" if conf else ""
    return f"- {subj} · {f.get('dimension')} = {val}{suffix}{base_txt}{conf_txt}"


def narrate(
    task: Any,
    verified_facts: list[dict[str, Any]],
    *,
    verdict: str = "",
) -> dict[str, Any]:
    """Swap verified facts into narrative insight.

    comparable is always False -- this is a contract, not an option.
    """
    if not verified_facts:
        return {
            "narrative": "",
            "insights": [],
            "caveats": [],
            "comparable": False,
            "skipped": "No referee-confirmed facts this round; skip narration "
                       "(avoid rationalizing wrong numbers)",
        }

    fact_lines = "\n".join(_fmt_fact(f) for f in verified_facts)
    prompt = f"""Question:
{task.question if task else ''}

The following facts **have passed the original VLML referee's verification** (all values match):

{fact_lines}

This judgment: {verdict}

Please write coach-facing narrative insight based on these verified facts. Output JSON only."""

    raw = chat(
        [
            {"role": "system", "content": NARRATOR_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
    )

    start, end = raw.find("{"), raw.rfind("}")
    payload: dict[str, Any] = {}
    if start >= 0 and end > start:
        try:
            payload = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            payload = {}

    return {
        "narrative": str(payload.get("narrative") or "").strip(),
        "insights": [str(x).strip() for x in (payload.get("insights") or []) if str(x).strip()],
        "caveats": [str(x).strip() for x in (payload.get("caveats") or []) if str(x).strip()],
        # Contract: the narrative never participates in cross-checking. Machine-readable tag.
        "comparable": False,
        "based_on": len(verified_facts),
        "raw": "" if payload else raw[:400],
    }
