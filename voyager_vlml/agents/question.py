"""VLML-isomorphic question generator: poses questions per **milestone node** (so the loop truly generates its own questions).

Design notes:
  · The question only describes "what data to retrieve", **without a reference answer** (the model writes its own SQL to fetch from the DB).
    The old backend also produced reference_sql here for the VLML referee to score against — that is for scoring only,
    and belongs in the dashboard; this file produces no reference answer.
  · It reuses **grain-locking**: the grain tag in parentheses in the topic name (rounds / games / series /
    base_events…) is the main retrieval table, and the question must stay at that grain to avoid cross-grain leakage
    (in practice, grain_game was once turned into "how many rounds").
"""

from __future__ import annotations

import re
from typing import Any

from voyager_vlml.llm import chat_json

_SYSTEM = (
    "You are a sparring coach for Valorant esports data analysis. Given the 【Topic】 and the actual schema "
    "of the current database, pose **one** practice question that requires SQL to retrieve data from the "
    "database to answer.\n"
    "Return only JSON: {\"question\": \"the question text (natural language, state the grain clearly: "
    "which map / which team / which series)\"}\n"
    "Requirements: the question must be answerable with a single SELECT; do not invent tables or columns "
    "that do not exist in the DB; do not give the answer."
)


def _gran(name: str) -> str:
    m = re.search(r"[（(]([^（）()]+)[）)]", name or "")
    return m.group(1).strip() if m else ""


class VLMLQuestionAgent:
    def __init__(self, env: Any = None) -> None:
        self.env = env  # used to fetch the actual schema (omit the schema block if not passed)

    async def _schema_block(self) -> str:
        """Fetch tables and columns live from the DB (env.schema_block, following the companion's
        entity_catalog "live-query, don't hardcode" principle).

        ⚠️ Must be async: the loop itself runs inside an event loop, so calling asyncio.run here would
        raise "coroutine was never awaited" (hit this in practice).
        """
        if self.env is None or not hasattr(self.env, "schema_block"):
            return ""
        try:
            return await self.env.schema_block() or ""
        except Exception:
            return ""

    async def generate(self, topic: dict[str, Any]) -> str:
        """Pose a question for one milestone node; return the question text."""
        name = str(topic.get("name") or topic.get("id") or "")
        skills = "；".join(str(s) for s in (topic.get("skills") or [])[:3])
        ex = ""
        examples = topic.get("examples") or []
        if examples and isinstance(examples[0], dict):
            ex = str(examples[0].get("prompt") or "")

        gran = _gran(name)
        gran_line = (
            f"\n【Grain lock】This question's grain is `{gran}`; the question must stay at that grain "
            f"(main retrieval table `{gran}`), "
            f"and must not borrow another grain (e.g., if the grain is not rounds, do not ask 'how many rounds')."
            if gran
            else ""
        )
        schema = await self._schema_block()
        user = (
            f"【Topic】{name}\n"
            f"Skill requirements: {skills or '(none)'}\n"
            f"Example phrasing: {ex or '(none)'}"
            f"{gran_line}\n"
            + (f"\n【Current DB schema (actual)】\n{schema}\n" if schema else "")
            + "\nReturn only JSON."
        )
        out = chat_json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            temperature=0.6,
        )
        return str(out.get("question") or "").strip()
