"""voyager_vlml 的 LLM 调用（黑客松支线：Nebius Token Factory 优先，env 可切回）。

后端优先级（密钥在就走哪条）：
  1. NEBIUS_API_KEY                                    → Nebius Token Factory（NVIDIA 开源模型）
  2. MVE_QUIZ_BASE_URL / MVE_QUIZ_API_KEY / MVE_QUIZ_MODEL → 猫娘桥（OpenAI 兼容）
  3. ZHIPU_API_KEY / ZHIPU_BASE_URL / ZHIPU_MODEL      → MVE 自有智谱

⚠️ 黑客松规则要求提交必须跑在 Nebius Token Factory / AI Cloud 且用至少一个
NVIDIA 开源模型 —— 没配 NEBIUS_API_KEY 时这里会**每次调用都大声警告**，
防止演示悄悄回退到智谱（那等于提交不符合规则的项目，比跑不起来更糟）。
走 HTTP 不走 SDK（宿主内禁止 fork，HTTP 无此问题）。
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


def _warn_no_nebius() -> None:
    """回退到非 Nebius 后端前警告一次（防止演示悄悄违规）。"""
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
    """密钥兜底：从 mve/.env 读（照 mve/llm_client.load_env 的做法）。

    pilot 起的子进程不一定带上密钥，新后端要能自己拿到。
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
    # 1) Nebius Token Factory（OpenAI 兼容；NVIDIA 开源模型）
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
    # 2) 猫娘桥（OpenAI 兼容）
    base = os.getenv("MVE_QUIZ_BASE_URL", "").strip()
    if base:
        return (
            base.rstrip("/") + "/chat/completions",
            os.getenv("MVE_QUIZ_API_KEY", ""),
            os.getenv("MVE_QUIZ_MODEL", "glm-4-flash"),
        )
    # 3) MVE 自有智谱
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
    """发一轮对话，返回模型文本。"""
    import httpx

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
    """要模型回 JSON；宽容提取（```json 围栏 / 裸对象）。"""
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
    """Embedding 出口 —— 照原版 SkillManager 的向量检索思路（skill.py:43-47），
    但 embedding 也走 Nebius（Qwen3-Embedding-8B，实见于 /v1/models 清单），
    全栈不出 Token Factory；不引 Chroma 重依赖，向量由调用方自存自算余弦。

    一批文本一次调用；任何失败返回 None（调用方降级到词面相似度，
    检索可用性不依赖 embedding 通不通）。
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
