#!/usr/bin/env python3
"""LLM gateway smoke test: confirm the current backend (should be Nebius Token Factory + an NVIDIA model) actually works.

Usage (from the repository root):
    python scripts/check_llm.py

Checks three things:
  1. Endpoint resolution: which backend and model are in use (a loud warning prints if NEBIUS_API_KEY is missing)
  2. Model listing: GET {base}/models, showing a few available NVIDIA open models
  3. A real round-trip: plain chat + chat_json (MVE's question-generation and criteria both rely on the JSON channel)

Exit code 0 = all checks pass; 1 = a check failed (errors are printed as-is, never silenced).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from voyager_vlml import llm  # noqa: E402


def main() -> int:
    url, key, model = llm._endpoint()
    base = url.rsplit("/chat/completions", 1)[0]

    print("== 1. Endpoint resolution ==")
    backend = "Nebius Token Factory" if os.getenv("NEBIUS_API_KEY") else "non-Nebius fallback backend"
    print(f"  Backend: {backend}")
    print(f"  URL    : {url}")
    print(f"  Model  : {model}")
    if not os.getenv("NEBIUS_API_KEY"):
        print("  WARNING: NEBIUS_API_KEY is not set — the hackathon rules do not allow running on this backend")
        print("           (the warning above should have printed; fill in mve/.env and re-run)")

    print("\n== 2. Model listing (first 12) ==")
    try:
        r = httpx.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        ids = [m.get("id", "?") for m in (data.get("data") or [])]
        for mid in ids[:12]:
            mark = " <- NVIDIA" if "nvidia" in mid.lower() or "nemotron" in mid.lower() else ""
            print(f"  {mid}{mark}")
        if not ids:
            print("  (empty list — the endpoint may not support /models enumeration; skipping)")
    except Exception as e:
        print(f"  listing failed (non-fatal; many endpoints forbid listing models): {type(e).__name__}: {str(e)[:100]}")

    print("\n== 3. Real round-trip ==")
    try:
        text = llm.chat(
            [{"role": "user", "content": "Explain in one sentence what a skill library is."}],
            temperature=0.2, max_tokens=120, timeout=60.0,
        )
        ok_text = bool(text.strip())
        print(f"  chat channel : {'OK' if ok_text else 'FAIL (empty response)'} {text.strip()[:80]}")
    except Exception as e:
        print(f"  chat channel : FAIL {type(e).__name__}: {str(e)[:150]}")
        ok_text = False

    try:
        js = llm.chat_json(
            [{"role": "user",
              "content": 'Return JSON only: {"skill": "SQL recipe", "reusable": true}'}],
            temperature=0.1, max_tokens=120, timeout=60.0,
        )
        ok_json = isinstance(js, dict) and bool(js)
        print(f"  JSON channel : {'OK' if ok_json else 'FAIL (no object parsed)'} {js}")
    except Exception as e:
        print(f"  JSON channel : FAIL {type(e).__name__}: {str(e)[:150]}")
        ok_json = False

    nebius_ok = bool(os.getenv("NEBIUS_API_KEY"))
    print("\n== Verdict ==")
    if ok_text and ok_json and nebius_ok:
        print("PASS: gateway works and runs on Nebius + NVIDIA open models (compliant with the rules)")
        return 0
    if ok_text and ok_json:
        print("WARN: gateway works but is NOT on Nebius — NEBIUS_API_KEY must be set before the demo")
        return 1
    print("FAIL: gateway unusable — check the key / network / model ID")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
