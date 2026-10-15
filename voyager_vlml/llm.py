"""LLM calls for voyager_vlml (hackathon side-track: Nebius Token Factory preferred, env can fall back).

Backend priority (whichever key is present wins):
  1. NEBIUS_API_KEY                                    → Nebius Token Factory (NVIDIA open models)
  2. MVE_QUIZ_BASE_URL / MVE_QUIZ_API_KEY / MVE_QUIZ_MODEL → Neko bridge (OpenAI-compatible)
  3. ZHIPU_API_KEY / ZHIPU_BASE_URL / ZHIPU_MODEL      → MVE's own Zhipu

⚠️ Hackathon rules require submissions to run on Nebius Token Factory / AI Cloud using at least one
NVIDIA open model — when NEBIUS_API_KEY is unset, this module **warns loudly on every call**,
to prevent the demo from silently falling back to Zhipu (which would make the submission non-compliant,
worse than failing to run at all).
Uses HTTP, not an SDK (the host forbids fork; HTTP avoids that issue).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_WARNED_FALLBACK = False

# Force-English rule injected at the gateway so every LLM call (Chinese or English prompt)
# returns English prose, while preserving code/SQL/JSON/identifiers and any keyword/value
# options the code hands the model (skill names, question-type keys, etc.) so coupling keys
# never desync. Reversible: set MVE_FORCE_ENGLISH=0/off/false to disable.
_ENGLISH_RULE = (
    "LANGUAGE RULE: Whenever you write natural-language explanations, summaries, critiques, "
    "or any prose, write it in English. Do NOT translate and MUST output exactly as given: "
    "code, SQL, JSON, identifiers, file paths, model names, and any specific keyword / label / "
    "value / option supplied to you (for example a value chosen from a provided list)."
)


def _enforce_english(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    flag = os.getenv("MVE_FORCE_ENGLISH", "1").strip().lower()
    if flag in ("0", "false", "off", "no"):
        return messages
    if not messages:
        return messages
    out = [dict(m) for m in messages]  # copy: never mutate the caller's history list
    for i, m in enumerate(out):
        if m.get("role") == "system":
            content = m.get("content") or ""
            if _ENGLISH_RULE not in content:
                out[i] = {"role": "system", "content": content + "\n\n" + _ENGLISH_RULE}
            return out
    # no system message present -> prepend one
    return [{"role": "system", "content": _ENGLISH_RULE}] + out


def _warn_no_nebius() -> None:
    """Warn once before falling back to a non-Nebius backend (prevents the demo from silently violating the rules)."""
    global _WARNED_FALLBACK
    if _WARNED_FALLBACK:
        return
    _WARNED_FALLBACK = True
    print(
        "WARNING [llm]: NEBIUS_API_KEY is not set; LLM calls are falling back to a non-Nebius backend.\n"
        "   The hackathon rules require running on Nebius Token Factory + NVIDIA open models —\n"
        "   set NEBIUS_API_KEY in mve/.env (see .env.example).",
        file=sys.stderr,
    )


def _load_env_once() -> None:
    """Key fallback: read from mve/.env (following the approach in mve/llm_client.load_env).

    Child processes spawned by the pilot may not inherit the keys, so the new backend must be
    able to fetch them itself.
    """
    env = Path(__file__).resolve().parents[1] / "mve" / ".env"
    try:
        if not env.exists():
            return
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


def _endpoint() -> tuple[str, str, str]:
    _load_env_once()
    # 1) Nebius Token Factory (OpenAI-compatible; NVIDIA open models)
    nebius_key = os.getenv("NEBIUS_API_KEY", "").strip()
    if nebius_key:
        base = os.getenv(
            "NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1")
        return (
            base.rstrip("/") + "/chat/completions",
            nebius_key,
            os.getenv("NEBIUS_MODEL", "nvidia/nemotron-3-super-120b-a12b"),
        )
    _warn_no_nebius()
    # 2) Neko bridge (OpenAI-compatible)
    base = os.getenv("MVE_QUIZ_BASE_URL", "").strip()
    if base:
        return (
            base.rstrip("/") + "/chat/completions",
            os.getenv("MVE_QUIZ_API_KEY", ""),
            os.getenv("MVE_QUIZ_MODEL", "glm-4-flash"),
        )
    # 3) MVE's own Zhipu
    base_u = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    return (
        base_u.rstrip("/") + "/chat/completions",
        os.getenv("ZHIPU_API_KEY", ""),
        os.getenv("ZHIPU_MODEL", "glm-4-flash"),
    )


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.4,
    max_tokens: int = 1200,
    timeout: float = 90.0,
    retries: int = 3,
) -> str:
    """Send one chat turn and return the model text."""
    import httpx

    messages = _enforce_english(messages)
    url, key, model = _endpoint()
    if not key:
        raise RuntimeError("No LLM key configured (NEBIUS_API_KEY, MVE_QUIZ_API_KEY, or ZHIPU_API_KEY)")

    last = ""
    for _ in range(retries):
        try:
            r = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=timeout,
            )
            if r.status_code >= 500 or r.status_code == 429:
                last = f"HTTP {r.status_code}: {r.text[:120]}"
            else:
                r.raise_for_status()
                data = r.json()
                return (
                    (data.get("choices") or [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    or ""
                )
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5)
    raise RuntimeError(f"LLM call failed: {last}")


def chat_json(messages: list[dict[str, str]], **kw: Any) -> dict:
    """Ask the model for JSON; tolerant extraction (```json fence / bare object)."""
    text = chat(messages, **kw)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        return json.loads(raw[s : e + 1])
    except json.JSONDecodeError:
        return {}


def embed(texts: list[str]) -> list[list[float]] | None:
    """Embedding endpoint — follows the original SkillManager's vector-retrieval idea (skill.py:43-47),
    but embeddings also go through Nebius (Qwen3-Embedding-8B, confirmed present in the /v1/models list),
    keeping the whole stack inside the Token Factory; no heavy Chroma dependency — the caller stores
    the vectors and computes cosine similarity itself.

    One call per batch of texts; any failure returns None (caller degrades to surface-form similarity,
    so retrieval availability does not depend on whether embeddings work).
    """
    if not texts:
        return []
    url, key, _ = _endpoint()
    base = url.rsplit("/chat/completions", 1)[0]
    model = os.getenv("NEBIUS_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
    try:
        import httpx

        r = httpx.post(
            f"{base}/embeddings",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "input": texts},
            timeout=60.0,
        )
        r.raise_for_status()
        data = r.json().get("data") or []
        out = [[float(x) for x in (d.get("embedding") or [])] for d in data]
        if len(out) != len(texts) or any(not v for v in out):
            return None
        return out
    except Exception:
        return None
