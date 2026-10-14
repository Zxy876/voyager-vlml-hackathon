#!/usr/bin/env python3
"""LLM 网关冒烟测试：确认当前后端（应为 Nebius Token Factory + NVIDIA 模型）真的能跑。

用法（在仓库根目录）：
    python scripts/check_llm.py

检查三件事：
  1. 端点解析：当前走哪条后端、什么模型（未配 NEBIUS_API_KEY 会大声警告）
  2. 模型清单：GET {base}/models 列几个可用的 NVIDIA 开源模型
  3. 实跑一次：普通对话 + chat_json（MVE 的判据/出题都靠 JSON 通道）

退出码 0 = 全过；1 = 有一步不过（错误信息照实输出，不静默）。
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

    print("== 1. 端点解析 ==")
    backend = "Nebius Token Factory" if os.getenv("NEBIUS_API_KEY") else "非 Nebius 回退后端"
    print(f"  后端  ：{backend}")
    print(f"  URL   ：{url}")
    print(f"  模型  ：{model}")
    if not os.getenv("NEBIUS_API_KEY"):
        print("  ⚠️ 未配 NEBIUS_API_KEY —— 黑客松提交不允许跑在这个后端上")
        print("     （上面的警告应已打印；填好 mve/.env 后重跑）")

    print("\n== 2. 模型清单（前 12 个）==")
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
            mark = " ← NVIDIA" if "nvidia" in mid.lower() or "nemotron" in mid.lower() else ""
            print(f"  {mid}{mark}")
        if not ids:
            print("  （清单为空——端点可能不支持 /models 列举，跳过）")
    except Exception as e:
        print(f"  列举失败（不致命，很多端点禁列模型）：{type(e).__name__}: {str(e)[:100]}")

    print("\n== 3. 实跑一次 ==")
    try:
        text = llm.chat(
            [{"role": "user", "content": "用一句话解释什么是技能库（skill library）。"}],
            temperature=0.2, max_tokens=120, timeout=60.0,
        )
        ok_text = bool(text.strip())
        print(f"  对话通道：{'✓' if ok_text else '✗ 返回空'} {text.strip()[:80]}")
    except Exception as e:
        print(f"  对话通道：✗ {type(e).__name__}: {str(e)[:150]}")
        ok_text = False

    try:
        js = llm.chat_json(
            [{"role": "user",
              "content": '只返回 JSON：{"skill": "SQL recipe", "reusable": true}'}],
            temperature=0.1, max_tokens=120, timeout=60.0,
        )
        ok_json = isinstance(js, dict) and bool(js)
        print(f"  JSON 通道：{'✓' if ok_json else '✗ 没解析出对象'} {js}")
    except Exception as e:
        print(f"  JSON 通道：✗ {type(e).__name__}: {str(e)[:150]}")
        ok_json = False

    nebius_ok = bool(os.getenv("NEBIUS_API_KEY"))
    print("\n== 判定 ==")
    if ok_text and ok_json and nebius_ok:
        print("✅ 网关可用，且跑在 Nebius + NVIDIA 开源模型上（符合参赛规则）")
        return 0
    if ok_text and ok_json:
        print("⚠️ 网关可用，但不在 Nebius 上 —— 演示前必须配好 NEBIUS_API_KEY")
        return 1
    print("❌ 网关不可用 —— 检查密钥 / 网络 / 模型 ID")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
