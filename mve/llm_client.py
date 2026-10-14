#!/usr/bin/env python3
"""LLM 最小客户端（黑客松支线：统一走 voyager_vlml.llm 网关）。

⚠️ 2026-10-14 支线改动：原来自带智谱端点逻辑（模块级常量）。黑客松规则要求
   提交必须跑在 Nebius Token Factory + NVIDIA 开源模型上，这里改为委托
   `voyager_vlml/llm.py`（Nebius 优先的唯一 LLM 出口）——本文件当前在 MVE
   活动路径上无引用（保留是为兼容旧调用方），但绝不能留一个直连智谱的口子。
密钥从 mve/.env 读取，不写进代码。
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
    """LLM 调用最终失败。调用方必须能看见它，不能静默降级成"无证据"。"""


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: float = 90.0,
    retries: int = 3,
) -> str:
    """带退避重试的 chat（委托 voyager_vlml.llm 网关；退避重试在网关内）。

    为什么必须重试：裁判（VLML0）和 Voyager 是并发打的，实测会撞上限流/超时，
    表现为「模型规划返回空 → 一次工具都没调 → 判无证据」。
    不重试的话，这种间歇故障会被误读成「Voyager 没学会」——那是假阴性。
    """
    from voyager_vlml.llm import chat as _gateway_chat

    try:
        return _gateway_chat(messages, temperature=temperature,
                             max_tokens=max_tokens, timeout=timeout,
                             retries=retries)
    except RuntimeError as e:
        raise LLMError(str(e)) from e


def chat_json(messages: list[dict[str, str]], **kw) -> dict:
    """要求模型输出 JSON 对象；解析失败返回 {}（调用方负责降级）。"""
    text = chat(messages, **kw)
    # 容忍 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
