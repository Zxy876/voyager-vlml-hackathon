#!/usr/bin/env python3
"""Minimal LLM client (hackathon side branch: everything goes through the voyager_vlml.llm gateway).

⚠️ 2026-10-14 side-branch change: this file used to carry its own Zhipu endpoint logic
   (module-level constants). Hackathon rules require submissions to run on
   Nebius Token Factory + NVIDIA open-source models, so it now delegates to
   `voyager_vlml/llm.py` (the single LLM egress, Nebius first) — this file is
   currently unreferenced on MVE's active path (kept for backward compatibility
   with old callers), but it must not leave a direct Zhipu connection open.
Keys are read from mve/.env, never written into code.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


def load_env() -> None:
    env = Path(__file__).resolve().parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env()


class LLMError(RuntimeError):
    """The LLM call failed for good. Callers must be able to see it; it must not silently degrade into "no evidence"."""


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: float = 90.0,
    retries: int = 3,
) -> str:
    """Chat with backoff retries (delegates to the voyager_vlml.llm gateway; backoff retries happen inside the gateway).

    Why retries are mandatory: the referee (VLML0) and Voyager run concurrently, and in practice
    they hit rate limits/timeouts, which show up as "the model's planning returns empty → not a
    single tool call → judged no evidence".
    Without retries, this intermittent failure would be misread as "Voyager didn't learn" — a false negative.
    """
    from voyager_vlml.llm import chat as _gateway_chat

    try:
        return _gateway_chat(messages, temperature=temperature,
                             max_tokens=max_tokens, timeout=timeout,
                             retries=retries)
    except RuntimeError as e:
        raise LLMError(str(e)) from e


def chat_json(messages: list[dict[str, str]], **kw) -> dict:
    """Ask the model to output a JSON object; returns {} if parsing fails (callers handle degradation)."""
    text = chat(messages, **kw)
    # Tolerate ```json ... ``` wrapping
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
