"""Consumption path: a human writes one question → **Voyager answers it with its accumulated skills** → disclose the VLML analysis → compute coverage / mastery.

Why this is a separate path (vs. the practice loop `loop.py`):

    Practice (loop.py)      Consumption (this file)
    ─────────────────        ────────────────────────
    Milestone topic picks   Human asks (one sentence)
    Success = data retrieved Success = answered correctly (coverage)
    **No coverage/mastery**  **Coverage/mastery computed from here on**
    Criteria stay out of the VLML ruler    Criteria = the VLML0 referee (shown to humans only)

Boundary set by the user (2026-10-13): "The learning side is already done; the consumption side has to be done too...
Computing coverage and mastery should start from here."

⚠️ Three hard constraints (this file must uphold them):

1. **Answer with accumulated skills, not improvised**.
   Retrieve SQL recipes already solidified in the skill library → **backfill the entity values from the current sentence into the recipe** → execute.
   User's exact words: "When reusing, backfill the entity values from the current question into the recipe; don't treat 'remembering the number' as success — start storing skills from the process of using a method to retrieve the number. **Backfilling things that don't exist is just faster calculation**".
   So what is backfilled here is the **calculation** (the recipe's algorithmic structure), not last time's values.
   Only when no usable recipe exists in the library do we degrade to on-the-spot SQL, and **honestly mark reused=False**
   ("this question hasn't been learned yet — it was done on the spot" — we must not pretend capability was used).

2. **VLML's analysis is still disclosed**: which tools were called, which facts were retrieved — shown to the dashboard as-is.
   This is an audit trail for humans ("how did it arrive at this answer").

3. **Coverage/mastery goes to the dashboard only, never into Voyager feedback**.
   ⚠️ This number is **never fed back** into any prompt: not into the action agent's iterative refinement, not into the critic,
   not into the curriculum's topic selection (topic selection remains the ordered curriculum and does not read scores).
   It only lands in `coach_log.jsonl` for the dashboard — echoing the practice side's `coverage=None` in `loop.py`:
   practice doesn't grade, consumption grades, and the grade is for humans to see.

How grading works (the method set by the user on 2026-10-13, replacing the old "compare facts by key"):

    User's exact words: "Organizing facts is something that should be handed to the LLM anyway; if we're on the consumption side,
    directly compare Voyager's and VLML's methods and approaches, compute mastery and the curve, feed them into the skill library,
    and then have the LLM produce narrative insights."

    ① Voyager retrieves data with its accumulated skills (possibly reusing a recipe)
    ② The referee (VLML) **independently** does the same: data retrieval only, no organizing
    ③ `_method_compare`: lay out both sides' SQL + retrieved results and let the LLM judge
       whether they use the same definition, and whether Voyager's approach has any hard defect → **agreement is coverage**
    ④ `_narrate`: the LLM produces narrative insights based on the retrieved data (+ the comparison conclusion)
    ⑤ `_absorb`: answered → this question enters the skill library → mastered

    The old method required each side to organize into {subject, dimension, value} and then compare by key,
    while the "organizing" step was the most fragile one: when the answer is a table it can't be compressed into single-value facts → both sides hand in blank sheets →
    coverage falls through + the answer is empty (measured: 85 rows retrieved but "cannot answer" displayed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MVE_DIR = _REPO_ROOT / "mve"
for _p in (str(_REPO_ROOT), str(_MVE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from voyager_vlml.agents.action import VLMLActionAgent  # noqa: E402
from voyager_vlml.agents.critic import has_real_data  # noqa: E402
from voyager_vlml.env.vlml_mcp import VLMLMCPEnv  # noqa: E402
from voyager_vlml.llm import chat, chat_json  # noqa: E402


# ---------------------------------------------------------------------------
# ① Which question this sentence routes to (to get the scoring points — coverage is computed per scoring point)
# ---------------------------------------------------------------------------

def _infer(question: str) -> tuple[str, str, Any]:
    """Topic routing → (topic_id, hit, task).

    Routes to the **legacy task bank** `TASKS` (it has rubric + answer_spec, which is what makes coverage computable).
    Note: this is **not the same set** as the practice side's knowledge graph nodes (`vlml_*` / `runtime_*`).
    The two sets coexist on purpose: the graph governs "what to learn" (milestone progression), the task bank governs "whether the answer is right" (scoring points).
    No route → task is None, coverage cannot be computed (left honestly empty, no fabrication).
    """
    try:
        from tasks import TASKS, infer_topic
    except Exception as e:  # pragma: no cover
        print(f"  (task bank unavailable: {type(e).__name__}: {e})")
        return "", "", None
    tid, hit = infer_topic(question)
    return tid, hit, TASKS.get(tid)


def _same_question(question: str, task: Any) -> tuple[bool, str]:
    """Whether this sentence and this task ask the **same question** (not "whether they share a word").

    Why this check is mandatory (hit in practice):
      A human asks "Which player has the highest K/D ratio on the map Corrode?"
      → `infer_topic` routes it to `kast_adr_check` via the token "k/d"
      → but that task asks for "the **team-wide** KAST and K/D of Cloud9 in this match"
        (rubric subject = {series, team:NRG}, asking for the team scope, not a specific player)
      → using its rubric to judge Voyager's answer (subject = {player_name, map_name})
      → keys don't match, coverage is permanently 0.

    That is not "Voyager answered wrong" — it's **the wrong ruler**. Without this check it would be graded 0 directly,
    the dashboard would show 0% coverage, and the human would read it as "its capability is weak", when the reality is
    "we measured it against the standard answer of a different question". This kind of false failure must be blocked:
    different question → don't apply its rubric, leave honestly empty, hand it to the generate-a-new-question path.
    """
    try:
        out = chat_json(
            [
                {"role": "system",
                 "content": ("Judge whether two sentences ask the **same question**. Look at scope and object: "
                             "team-wide ≠ a specific player, whole match ≠ a single map, one metric ≠ two metrics. "
                             "Return JSON only: {\"same\": true/false, \"why\": \"one-sentence reason\"}")},
                {"role": "user",
                 "content": (f"A (asked by the human): {question}\n\n"
                             f"B (question in the task bank): {getattr(task, 'question', '')}")},
            ],
            temperature=0.0,
        )
        return bool(out.get("same")), str(out.get("why") or "")
    except Exception as e:  # pragma: no cover
        return False, f"Check failed: {type(e).__name__}"


# ---------------------------------------------------------------------------
# ② Which accumulated skills this sentence should consume (find recipes)
# ---------------------------------------------------------------------------

def _pick_ability_topic(question: str, skills: list[dict[str, Any]]) -> str:
    """Pick, among the **topics actually present in the skill library**, the one most similar to this sentence.

    Why the topic-routing result can't be used directly: routing targets the legacy task bank (series_totals etc.),
    while recipes in the skill library hang off `vlml_*` / `runtime_*` — the two ID systems don't interconnect,
    so using the routing result for `skill_store.retrieve` would retrieve nothing (measured: all 3 recipes in the
    skill library are runtime_*, while infer_topic only returns legacy task bank IDs).
    So ability attribution goes by **lexical matching**, asking "which learned recipe is this sentence most like".
    """
    try:
        from skill_store import similarity
    except Exception:  # pragma: no cover
        return ""
    best, best_s = "", 0.0
    for s in skills:
        t = str(s.get("topic") or "")
        if not t:
            continue
        blob = f"{s.get('name', '')} {s.get('text', '')} {s.get('code', '')}"
        sc = similarity(question, blob)
        if sc > best_s:
            best, best_s = t, sc
    return best


def _retrieve_skills(question: str, topic_hint: str = "") -> list[dict[str, Any]]:
    """Retrieve accumulated skills (reusable SQL recipes), top_k as in the original repo's retrieval_top_k=5."""
    try:
        import skill_store
        all_s = list((skill_store._load_raw().get("skills") or {}).values())
    except Exception as e:  # pragma: no cover
        print(f"  (skill library unavailable: {type(e).__name__}: {e})")
        return []
    if not all_s:
        return []
    topic = topic_hint or _pick_ability_topic(question, all_s)
    out = skill_store.retrieve(topic, question, top_k=5)
    try:
        skill_store.mark_used([str(s.get("key") or "") for s in out])
    except Exception:  # pragma: no cover
        pass
    return out


# ---------------------------------------------------------------------------
# ③ Reuse the recipe: backfill the current sentence's entity values into the recipe's **calculation**
# ---------------------------------------------------------------------------

_REFILL_SYSTEM = """You are a Valorant esports data analyst. You have in hand a **previously learned data-retrieval recipe (SQL)**,
and now need to use it to answer a **new phrasing**.

Rules (violating any one of them does not count as successful reuse):

0. **First judge whether the quantity the recipe computes and the quantity this sentence asks about are the same quantity.**
   Not the same quantity → return {"reused": false, "sql": "", "note": "reason"} directly,
   don't force one out.
   ⚠️ Hit in practice: asked "On which map does Cloud9 collapse the hardest?" (wanting a losing streak / collapse),
   the library only had an "average K/D" recipe, and the model still plugged 'Cloud9' in to produce a K/D query,
   even claiming it "reused the recipe" — what came back was a K/D ratio, which downstream got narrated as a "collapse rate".
   **A wrong number is still visible; a wrong quantity is a directional error, far more dangerous than an error report.**
   Swapping entity values (Cloud9→NRG) counts as reuse; switching to a completely different quantity does not.

1. What is reused is the recipe's **computational method** (which tables are used, how it aggregates, how it filters),
   **not** the values it computed last time — values must be recomputed.
2. Plug the **entity values in the current sentence** (team name / map name / series / player) into the corresponding positions of the recipe.
   Literals hard-coded in the recipe must be replaced with the value this sentence refers to.
3. If the current sentence gives no entity (e.g. asking "which team is highest") → keep the recipe's grouping structure, don't invent a value.
4. Output a single SELECT only. No INSERT/UPDATE/DROP, no multiple statements.

Return one JSON only: {"sql": "your SELECT", "reused": true/false,
"note": "one sentence explaining which part of the recipe you reused and what values you plugged in (if not reused, explain why it doesn't apply)"}"""


def _refill_sql(question: str, skills: list[dict[str, Any]], schema: str) -> dict[str, Any]:
    """Have the model adapt the recipe into data-retrieval SQL for the current sentence (backfill = adapt the calculation, not copy the values)."""
    recipes = "\n\n".join(
        f"[Recipe {i+1} | topic={s.get('topic')} | {s.get('name') or ''}]\n"
        f"Description: {str(s.get('text') or '')[:200]}\n"
        f"SQL:\n{str(s.get('code') or '')[:900]}"
        for i, s in enumerate(skills[:5])
    )
    user = (
        f"Current sentence:\n{question}\n\n"
        f"Learned recipes:\n{recipes}\n\n"
        f"Tables and columns that actually exist in the current database (only names appearing here may be referenced):\n{schema[:6000]}\n\n"
        "Produce the SELECT that answers this sentence, following the rules."
    )
    out = chat_json(
        [
            {"role": "system", "content": _REFILL_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )
    sql = str(out.get("sql") or "").strip()
    return {
        "sql": sql,
        "reused": bool(out.get("reused")) and bool(sql),
        "note": str(out.get("note") or ""),
    }


# ---------------------------------------------------------------------------
# ④ Fact organization (for the coverage comparison)
# ---------------------------------------------------------------------------

def _extract_system() -> str:
    return ("You are a rigorous data organizer. Organize the real data returned by the tools into a fact array, "
            "output JSON only, and never fabricate numbers not present in the data.")


def _extract_user(question: str, obs: list[dict[str, Any]],
                  dims: list[str] | None = None,
                  subjects: list[dict[str, Any]] | None = None) -> str:
    payload = json.dumps(
        [{"tool": o.get("tool"), "args": o.get("args"),
          "result": o.get("data"), "error": o.get("error")} for o in obs],
        ensure_ascii=False, default=str,
    )[:14000]
    hint = ""
    if dims:
        # ⚠️ Give the **dimension names** only, not the values — the names are "which assignments to hand in"; the values are still for Voyager to compute.
        # Both sides' dimensions must be named identically, otherwise the fact sets can't match keys by (subject, dimension),
        # and coverage would be permanently 0 (that's not "answered wrong" — it's "can't be compared").
        hint = (
            "\n\nThe dimensions this question is expected to cover (**names only; compute the values yourself from the data**):\n"
            + "\n".join(
                f"- dimension must be written verbatim as `{d}`"
                f"{', subject written as ' + json.dumps(s, ensure_ascii=False) if s else ''}"
                for d, s in zip(dims, subjects or [None] * len(dims))
            )
            + "\n\n⚠️ Hard constraint: for the dimensions above, if the data returned by the tools **cannot compute them, write nothing at all**, "
            "leave an empty array. **Never attach a different number to a dimension name** —— hit in practice: the question asked about "
            "collapse (wanting a losing streak), what came back was actually a K/D ratio, and the model still wrote "
            "`map_fb_conv = 1.146`, which downstream narrated as a collapse rate of 1.146. "
            "A dimension name mismatched with its value = fabrication, far worse than leaving it empty. Better to write less than to mismatch."
        )
    return f"""Real data returned by the tools:

{payload}

This sentence asks:
{question}
{hint}

Organize it into a fact array, each fact:
{{"subject": {{...}}, "dimension": "...", "value": numeric,
  "unit": "percent|count|ratio|raw", "base": denominator}}

subject must at least locate who: include "team" when a team is involved, include "map" when a specific map is involved (use the real name).
dimension must show direction: write something like max_losing_streak, not round_streak ——
a reversed direction is more dangerous than an error report.

Output JSON only: {{"facts":[...]}}
Treat all NaN / null in the data as "not retrievable"; they must not be written into facts (a NaN ratio is the product of division by zero,
and using it as a real value for comparison is bound to be wrong). If not retrievable, leave an empty array; do not fabricate."""


# ---------------------------------------------------------------------------
# ⑤ Answer this sentence with the retrieved numbers
# ---------------------------------------------------------------------------

def _method_compare(question: str, v_sql: str, v_obs: list[dict[str, Any]],
                    r_sql: str, r_obs: list[dict[str, Any]]) -> dict[str, Any]:
    """**Directly compare Voyager's and VLML's methods and approaches** (the method set by the user on 2026-10-13).

    Why the old path of "each side organizes into {subject, dimension, value}, then compare by key" was dropped:
      Hit in practice —— asked "the average number of kills per role in each round", Voyager's SQL was completely correct,
      retrieved 85 rows, but the answer is a table, which can't be compressed into single-value facts → the organizer handed in a blank sheet → the answer was empty →
      the human only saw "Voyager can't answer". The data was there; the illness was "insisting on stuffing it into structured facts".

    So here we lay out **both sides' methods (SQL) and approaches (what was retrieved)** and let the LLM judge:
      are they computing the same quantity? Same definition? Does Voyager's approach have any hard defect
      (division by zero, wrong column name, wrong granularity, reversed direction)? Give a 0-1 agreement.
    LLMs are naturally good at this kind of semantic comparison; there's no need to compress the results into key-values first.

    Returns {agreement, same_metric, voyager_sound, reason, diff}
    """
    def _brief(obs: list[dict[str, Any]]) -> str:
        return json.dumps(
            [o.get("data") for o in obs if o.get("ok")],
            ensure_ascii=False, default=str)[:3500]
    def _nrow(obs: list[dict[str, Any]]) -> int:
        return sum(int(o.get("rows") or 0) for o in obs if o.get("ok"))

    payload = (
        f"This sentence: {question}\n\n"
        f"[Voyager's method] SQL:\n{v_sql or '(none)'}\n"
        f"Retrieved {_nrow(v_obs)} rows, results (truncated):\n{_brief(v_obs) or '(nothing retrieved)'}\n\n"
        f"[VLML's method] SQL:\n{r_sql or '(none)'}\n"
        f"Retrieved {_nrow(r_obs)} rows, results (truncated):\n{_brief(r_obs) or '(nothing retrieved)'}"
    )
    out = chat_json(
        [
            {"role": "system",
             "content": ("You are a data-analysis referee. Compare what **two approaches** compute and how they compute it, "
                         "judging whether they use the same definition and whether Voyager's approach has any hard defect.\n"
                         "Hard defects include: division by zero (x/0 yields NaN), wrong column name, wrong granularity (asked per round but given a whole-match total), "
                         "reversed direction (a losing streak narrated as a winning streak), wrong scope (team-wide narrated as a specific player).\n"
                         "⚠️ The two criteria are **mutually independent** —— judge in this order, don't mix them:\n"
                         "  Step 1 voyager_sound: **look only at Voyager's own SQL** for hard defects"
                         " (wrong column name, wrong granularity, any x/0, wrong direction).\n"
                         "  Step 2 same_metric: whether both sides compute the same quantity.\n"
                         "  Step 3 agreement: consistency of definitions (not a \"who's right\" score).\n"
                         "    **Different definitions on the two sides ≠ Voyager has a hard defect** —— hit in practice: "
                         "asked \"the average kills per role in each round\", Voyager computed it completely correctly, "
                         "while the referee itself over-counted deaths into a K/D, so Voyager got judged "
                         "\"approach has a hard defect\" —— the dashboard was unfairly accusing it.\n"
                         "    When the referee goes off-track: same_metric=false, voyager_sound still per step 1, "
                         "and write clearly in diff what the referee over-counted / under-counted.\n"
                         "    reason must contain either \"Voyager has no hard defect\" or \"Voyager has a hard defect: ...\", "
                         "consistent with voyager_sound (don't contradict yourself).\n"
                         "⚠️ The two sides' results **may differ in shape** (Voyager's is often a wide-column result, "
                         "the VLML referee often returns rows of dimension/value columns) —— the same number in ratio terms "
                         "must not be judged inconsistent just for looking different: first locate the corresponding values "
                         "on each side (e.g. 100% vs 1.0, 7 vs sample_size=7) before judging."
                         "Only when the same quantity computes **different values** on both sides should agreement be low.\n"
                         "⚠️ When both sides retrieve 0 rows, no matter how alike the SQL is, **there are no numbers to compare** —— "
                         "in that case agreement must be 0, same_metric=false, "
                         "and reason must state \"both sides retrieved no data; there is nothing to compare\". Don't judge two empty results "
                         "as \"same definition\" —— hit in practice: Cloud9/Corrode has no data in the current database, "
                         "each side retrieved 0 rows, the methods looked alike, and it was judged 100% agreement."
                         "That is equivalent to giving a perfect score to \"both sides retrieved no data\" —— far more dangerous than an error report.\n"
                         "Return JSON only: "
                         '{"agreement": a decimal between 0 and 1, "same_metric": true/false,'
                         ' "voyager_sound": true/false, "reason": "one-sentence basis for the judgment",'
                         ' "diff": "the concrete difference between the two approaches (empty string if none)"}')},
            {"role": "user", "content": payload},
        ],
        temperature=0.0,
    )
    try:
        ag = float(out.get("agreement") or 0.0)
    except (TypeError, ValueError):
        ag = 0.0
    reason = str(out.get("reason") or "")
    sound = bool(out.get("voyager_sound"))
    # Consistency fallback: the model can contradict itself with "reason says 'Voyager has no hard defect' but voyager_sound=false"
    # (hit in practice). The boolean directly drives the dashboard's red/green flag for "approach holds / has a hard defect",
    # so when it contradicts its own reason, **the written reason wins** —— the reason is its real judgment,
    # while the boolean was simply skewed in the different-definitions scenario.
    # The guard "has a hard defect" not in reason plays the same role as the original Chinese question-form
    # check: a reason that merely asks "whether Voyager has a hard defect" must not be read as a no-defect statement.
    if not sound and reason and ("hard defect" in reason):
        for neg in ("no hard defect", "no hard defects", "without a hard defect"):
            if neg in reason and "has a hard defect" not in reason and "have a hard defect" not in reason:
                sound = True
                break
    return {
        "agreement": max(0.0, min(1.0, ag)),
        "same_metric": bool(out.get("same_metric")),
        "voyager_sound": sound,
        "reason": reason,
        "diff": str(out.get("diff") or ""),
    }


def _narrate(question: str, obs: list[dict[str, Any]],
             compare: dict[str, Any] | None = None) -> str:
    """Produce narrative insights based on the **retrieved data** (works even when the answer is a table)."""
    payload = json.dumps(
        [o.get("data") for o in obs if o.get("ok")],
        ensure_ascii=False, default=str)[:9000]
    extra = ""
    if compare:
        # ⚠️ 2026-10-14: low agreement ≠ anyone has a hard defect. Hit in practice: the two sides' values were actually the same
        #    (100% vs 1.0, 7 vs sample_size=7), only the shapes differed, and the referee model misjudged
        #    agreement=0, so the narration side wrote "the conclusion is unreliable" per "0% agreement" ——
        #    mistranslating "could not be mutually corroborated" into "the answer is unreliable", unfairly accusing a correct answer.
        #    Now the three cases are worded separately, and **only voyager_sound=false allows the hard-defect wording**:
        ag = float(compare.get("agreement") or 0.0)
        same = bool(compare.get("same_metric"))
        sound = bool(compare.get("voyager_sound"))
        if not sound:
            verdict = f"Voyager's approach has a hard defect: {compare.get('reason') or ''}"
        elif ag >= 0.8:
            verdict = ("Two independent approaches computed the same result; the conclusions are mutually corroborated"
                       f" (agreement {ag:.0%})")
        elif same:
            verdict = (f"Both approaches compute the same quantity but produced different values"
                       f" (agreement {ag:.0%}) —— interpret this conclusion with caution")
        else:
            verdict = (f"The two approaches use different definitions, so they could not be mutually corroborated this time (agreement {ag:.0%})"
                       " —— note: **this does not mean the answer is wrong**; Voyager's own approach has no hard defect")
        extra = (
            f"\n\nSeparately, VLML independently retrieved the data once; the comparison conclusion: {verdict}; "
            f"{compare.get('reason') or ''}"
            f"{('; difference: ' + compare['diff']) if compare.get('diff') else ''}\n"
            "State this comparison conclusion as-is in the answer, using the wording given above; do not escalate it on your own to "
            "\"the conclusion is unreliable\" (that wording is only for the \"approach has a hard defect\" case)."
        )
    return chat(
        [
            {"role": "system",
             "content": ("You are a Valorant esports data analyst. Answer the person's question using the **raw data** "
                         "retrieved from the database by the tools, and give one or two insights.\n"
                         "Use only the numbers given; do not supplement anything not in the data.\n"
                         "⚠️ When the result is multi-row (possibly hundreds of rows), **do not list them row by row**: "
                         "state the dimensions and magnitudes, point out the few highest/lowest/anomalous ones, then give one insight.\n"
                         "⚠️ Keep the whole passage within 250 words —— hit in practice: given 1334 rows to narrate, "
                         "it spread out map by map and round by round, the output was cut off mid-sentence, and the human read a fragment.")},
            {"role": "user",
             "content": f"The person's question: {question}\n\nRaw data retrieved by the tools:\n{payload}{extra}"},
        ],
        temperature=0.3,
        max_tokens=900,
    )


def _answer_raw(question: str, obs: list[dict[str, Any]]) -> str:
    """Data was retrieved, but structured facts couldn't be organized — answer directly with the **raw data**.

    ⚠️ Hit in practice: asked "the average number of kills per role in each round", the SQL was completely correct, 85 rows retrieved,
    but the answer is a table (round × agent), which can't be compressed into `{subject, dimension, value}` single-value facts
    → the organizer returned empty → the answer was empty → the human saw "Voyager can't answer".
    **Retrieving data but giving no answer is the worst kind of failure**: the data was right there.
    So this is the fallback: feed the raw rows to the model to organize into an answer —— better to have one less layer of structure than to hand in a blank sheet.
    """
    payload = json.dumps(
        [o.get("data") for o in obs if o.get("ok")],
        ensure_ascii=False, default=str)[:9000]
    return chat(
        [
            {"role": "system",
             "content": ("You are a Valorant esports data analyst. Below are the raw results retrieved from the database by the tools; "
                         "use them to answer the person's question. Use only the numbers given; do not supplement anything not in the data. "
                         "If the result is multi-row, state the dimensions and the first few rows; don't just say \"there are multiple\". "
                         "Two to four sentences.")},
            {"role": "user",
             "content": f"The person's question: {question}\n\nRaw data retrieved by the tools:\n{payload}"},
        ],
        temperature=0.3,
        max_tokens=600,
    )


def _answer(question: str, facts: list[dict[str, Any]],
            obs: list[dict[str, Any]]) -> str:
    if not facts:
        return ""
    payload = json.dumps(facts, ensure_ascii=False, default=str)[:6000]
    return chat(
        [
            {"role": "system",
             "content": ("You are a Valorant esports data analyst. Answer the person's question using the **already retrieved numbers**. "
                         "Use only the numbers given; do not supplement any inference not in the data. "
                         "State clearly what the numbers are, which object they belong to, and how large the sample is. Two to four sentences.")},
            {"role": "user",
             "content": f"The person's question: {question}\n\nFacts already retrieved:\n{payload}"},
        ],
        temperature=0.3,
        max_tokens=600,
    )


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

async def consume(
    question: str,
    *,
    topic_hint: str = "",
    auto_task: bool = True,
    env: VLMLMCPEnv | None = None,
) -> dict[str, Any]:
    env = env or VLMLMCPEnv()

    # ---- ① Topic routing (find the scoring points) ----
    topic_id, hit, task = _infer(question)
    if topic_hint:
        hit = f"{hit} (specified)" if hit else "(specified)"
    if task is not None:
        # Routing is lexical and can land on a question that "shares a word but isn't the same question".
        # Wrong ruler → coverage permanently 0, which is a false failure and must not be reported as weak capability.
        same, why = _same_question(question, task)
        if not same:
            hit = f"{hit} (the routed question is not the same question as this sentence: {why})"
            task = None

    # ---- Scoring points (dimension names) must be fixed **before data retrieval**, and both sides share the same set ----
    # ⚠️ Hit in practice: the Voyager side, having no rubric, freely named it avg_kd_ratio,
    #   while the referee side used a separately derived player —— the two sides' dimensions couldn't match, coverage permanently 0.
    #   That's not "answered wrong" — it's "the two fact sets weren't aligned", and it must be blocked.
    derived: list[dict[str, Any]] = []
    if task is not None:
        dims = [str(p.dimension) for p in (task.rubric or [])]
        subs = [dict(p.subject or {}) for p in (task.rubric or [])]
    else:
        derived = _derive_dims(question)
        dims = [str(p.get("dimension")) for p in derived]
        subs = [dict(p.get("subject") or {}) for p in derived]

    # ---- ② Find accumulated skills (recipes) ----
    skills = _retrieve_skills(question, topic_hint)
    reused_list = [{"topic": s.get("topic"), "name": s.get("name"),
                    "key": s.get("key")} for s in skills]

    # ---- ③ Data retrieval: reuse the recipe first, write on the spot if none ----
    schema = ""
    try:
        schema = await env.schema_block()
    except Exception:
        schema = ""

    observations: list[dict[str, Any]] = []
    sql, reused, note = "", False, ""
    if skills:
        r = _refill_sql(question, skills, schema)
        # The model itself judged "the recipe's quantity and this sentence's quantity are not the same" → **don't execute it**,
        # write on the spot directly. Better to do it on the spot than to retrieve data with a misattributed SQL.
        if r["reused"] and r["sql"]:
            sql, reused, note = r["sql"], True, r["note"]
            observations.append(await env.run_sql(sql))
        else:
            note = r["note"] or "(the recipe computes a different quantity than this sentence asks)"
            print(f"  (recipe not reused: {note[:100]})")
    # ⚠️ The degradation criterion must include **vacuum results** (pitfall measured 2026-10-14): pure aggregation SQL on an empty table
    #   returns 1 row (NULL, 0) —— ok=True, rows=1, and the old criterion `any(o.get("ok"))` would
    #   treat it as "reuse succeeded", so a bad recipe gets used as capability, answering "0% / sample size 0".
    #   The isomorphic point in the original: the critic judges "whether the task was actually accomplished", not "whether the code ran".
    if not any(has_real_data(o) for o in observations):
        # Recipe not reused successfully (not in the library / not applicable / broke after backfill / retrieved vacuum values after backfill)
        # → write on the spot, and mark it honestly
        if sql:
            print(f"  (recipe backfill retrieved no real data; degrading to on-the-spot SQL: {note[:60]})")
        reused, sql = False, ""
        act = VLMLActionAgent(env=env)
        observations, sql = await act.solve(question, max_iters=4)

    ok_calls = [o for o in observations if o.get("ok")]
    # Real data was retrieved (ok and non-vacuum) —— grading / answering / sedimentation all recognize only this;
    # disclosure fields (trajectory / tool_calls) still use ok_calls, so humans see what actually happened.
    real_calls = [o for o in ok_calls if has_real_data(o)]
    rows = sum(int(o.get("rows") or 0) for o in ok_calls)
    real_rows = sum(int(o.get("rows") or 0) for o in real_calls)
    trajectory = [str(o.get("tool") or "") for o in ok_calls]

    # ---- ④ Organize facts (**disclosed by the dashboard as "which numbers were retrieved"; no longer a prerequisite for grading**) ----
    # ⚠️ Grading method changed 2026-10-13 (user's exact words): "Organizing facts is something that should be handed to the LLM anyway;
    #   if we're on the consumption side, directly compare Voyager's and VLML's methods and approaches, compute mastery and the curve,
    #   feed them into the skill library, and then have the LLM produce narrative insights."
    #   Old method = each side organizes into {subject, dimension, value} and then **compares by key**,
    #   while the organizing step is precisely the most fragile: when the answer is a table it can't be compressed into single-value facts → the organizer hands in a blank sheet →
    #   coverage falls through + the answer is empty (measured: 85 rows retrieved yet "can't answer").
    #   Now: facts are still organized as before (audit trail for humans), while grading looks at ⑤'s **method-and-approach comparison**.
    out = chat_json(
        [
            {"role": "system", "content": _extract_system()},
            {"role": "user",
             "content": _extract_user(question, observations, dims or None,
                                      subs or None)},
        ],
        temperature=0.1,
    ) if real_calls else {"facts": []}

    facts: list[dict[str, Any]] = []
    try:
        from loop_core import make_fact
        for f in (out.get("facts") or []):
            if not isinstance(f, dict) or "subject" not in f or "dimension" not in f:
                continue
            facts.append(make_fact(
                subject=f.get("subject") or {},
                dimension=str(f.get("dimension")),
                value=f.get("value"),
                unit=str(f.get("unit") or "raw"),
                base=f.get("base"),
                source={"tool": (trajectory or ["query_sql"])[0], "args": {}},
            ))
    except Exception as e:  # pragma: no cover
        print(f"  (fact organization failed: {type(e).__name__}: {e})")

    # ---- ⑤ Referee (VLML) independently retrieves the data once: **data retrieval only, no organizing** ----
    # The referee side used to run another pass of "organize into facts"; that pass was pure waste and a failure source
    # (see the comments in ④). Now it only brings the numbers back, and comparison is handed to `_method_compare`.
    ref_points = derived
    if not ref_points and task is not None:
        # Routed to a task with a rubric → use **its** scoring points as "which quantities to give" for the referee
        # (more accurate than re-deriving from this sentence —— that's the pre-existing standard in the task bank).
        ref_points = [
            {"point": str(getattr(p, "point", "") or getattr(p, "dimension", "")),
             "dimension": str(getattr(p, "dimension", "")),
             "subject": dict(getattr(p, "subject", None) or {})}
            for p in (task.rubric or [])
        ]
    ref_obs: list[dict[str, Any]] = []
    ref_sql: str = ""
    ref_traj: list[str] = []
    # The referee gate likewise only recognizes real data: when the Voyager side has vacuum results, there's no point comparing against it ——
    # two empty results would be judged "same definition" (see the measured pitfall in the ⑥ comments).
    if real_calls:
        ref_obs, ref_sql, ref_traj = await _referee_observations(
            question, ref_points, env=env, schema=schema)

    # ---- ⑥ Directly compare both sides' **methods and approaches** (grading comes out of here) ----
    # ⚠️ Hard prerequisite: **both sides must have really retrieved data**. Hit in practice: Cloud9/Corrode has no data in the current database,
    # Voyager retrieved 0 rows and the referee also retrieved 0 rows, and the two SQL statements looked almost identical → judged 100% agreement.
    # That is equivalent to giving a perfect score to "both sides retrieved no data" —— far more dangerous than an error report (a lush green 100% on the dashboard).
    # So if either path has 0 rows, don't grade; leave honestly empty.
    # ⚠️ One more cut on 2026-10-14: "0 rows" must also count **vacuum rows** —— pure aggregation on an empty table returns
    #   1 row (NULL, 0); the row count is 1 but it's not a number (see critic.is_vacuous). Same on the referee side:
    #   when both sides query an empty table, vacuum rows would disguise "both paths have no data" as "both paths have data".
    ref_rows = sum(int(o.get("rows") or 0) for o in ref_obs if o.get("ok"))
    ref_real_rows = sum(
        int(o.get("rows") or 0) for o in ref_obs
        if o.get("ok") and has_real_data(o))
    compare: dict[str, Any] = {}
    if ref_obs and real_rows > 0 and ref_real_rows > 0:
        try:
            compare = _method_compare(question, sql, observations, ref_sql, ref_obs)
        except Exception as e:  # pragma: no cover
            print(f"  (method comparison failed: {type(e).__name__}: {e})")
    elif ref_obs:
        print(f"  (not grading: Voyager retrieved {real_rows} real rows / VLML retrieved "
              f"{ref_real_rows} real rows; one path retrieved no real data)")

    # ---- ⑦ Answer with the retrieved numbers: the LLM produces **narrative insights** ----
    # The answer no longer depends on facts being non-empty —— if data was retrieved, it should speak; being incompressible into single-value facts doesn't count as "didn't answer".
    # ⚠️ But "retrieved data" must be **real data**: there is nothing to narrate on a vacuum result (1 row (NULL, 0)),
    #   and a narrative written on it is fabrication (measured: answered "win rate 0%, sample size 0" when the truth was 100%, n=7).
    answer, base_note = "", ""
    if real_calls and real_rows:
        try:
            answer = _narrate(question, observations, compare or None) or ""
            if answer:
                base_note = ("(narrative insights: the LLM produced them from the retrieved raw data"
                             + (", and honestly stated the comparison conclusion against VLML's approach" if compare else "") + ")")
        except Exception as e:  # pragma: no cover
            print(f"  (narrative insights failed: {type(e).__name__}: {e})")
    if not answer and facts:
        try:
            answer = _answer(question, facts, observations) or ""
        except Exception as e:  # pragma: no cover
            print(f"  (organizing the answer failed: {type(e).__name__}: {e})")
    if not answer and real_calls and real_rows:
        # Fallback: if narration doesn't come out, lay the raw data out flat. Retrieving data but giving no answer is the worst failure.
        try:
            answer = _answer_raw(question, observations) or ""
            if answer:
                base_note = "(answered directly from the raw data)"
        except Exception as e:  # pragma: no cover
            print(f"  (answering from raw data failed: {type(e).__name__}: {e})")

    # Doesn't route to an existing question → generate a new question from this sentence (+ the just-retrieved facts).
    # ⚠️ Must happen **before** "entering the skill library": once answered, it needs a home, otherwise this question was clearly answered
    # yet can't enter the skill library for lack of a topic_id —— mastery would become "only questions that route count",
    # which conflates two things (answered vs. routable) into one condition.
    # Criterion relaxed to "answered" (either facts or answer non-empty): when the answer is a table it can't be compressed into facts,
    # but that is still answered —— it must not be homeless for lack of structured facts, and thus unable to enter the skill library.
    if not topic_id and auto_task and (facts or answer):
        try:
            from coach import _task_from_text
            new_id, why = await _task_from_text(question, facts)
            if new_id:
                from tasks import TASKS
                task = TASKS.get(new_id)
                topic_id, hit = new_id, f"{hit}{why}"
        except Exception as e:  # pragma: no cover
            print(f"  (generating a new question failed: {type(e).__name__}: {e})")

    # ---- Answered → this question enters the skill library → mastered (criterion is "answered or not", not coverage) ----
    # The criterion is "answered or not" (retrieved data + gave an answer), **not** whether it was organized into structured facts ——
    # when the answer is a table it can't be compressed into facts, but that is still answered, and it enters the skill library all the same.
    # ⚠️ "Retrieved data" likewise only counts real data: "answered" on a vacuum result is an illusion, and sedimenting it into the skill library
    #   is equivalent to laundering a bad recipe into a skill (measured: fb_conversion_analysis came about exactly this way).
    absorbed = _absorb(question, sql, topic_id) if (answer and real_calls) else {}
    m_tot = int(absorbed.get("mastered_total") or 0)
    a_tot = int(absorbed.get("ability_total") or 0)
    if absorbed.get("mastered") and not topic_id:
        # Answered but doesn't route to an existing question → it gets its own home (the ability item registered by this sentence).
        # The home must be written back, otherwise this question shows "not filed under any question" on the dashboard forever, and the human reads
        # "it hasn't learned this", when the truth is "it answered it; this sentence just isn't in the legacy task bank".
        topic_id = str(absorbed.get("topic_id") or "")
        hit = f"{hit}(this sentence is not in the legacy task bank; registered as an ability item on its own)" if hit else \
              "(this sentence is not in the legacy task bank; registered as an ability item on its own)"

    base: dict[str, Any] = {
        "question": question,
        "topic_id": topic_id,
        "topic_hit": hit,
        "mode": "voyager_consume",
        "source": "human_import",
        "validated_target": False,     # human import ≠ question-generator output (companion-study determination chain)
        # —— Skill consumption: which learned recipes were used, or done on the spot ——
        "reused": bool(reused),
        "reuse_note": note,
        "used_skills": reused_list,
        "skills_found": len(skills),
        # —— VLML's analysis (still disclosed: which tools were called, which facts were retrieved)——
        "trajectory": trajectory,
        "tool_calls": len(observations),
        "ok_tool_calls": len(ok_calls),
        "rows": rows,
        # Vacuum disclosure: how many of the rows are **real numbers** (pure aggregation on an empty table returns
        # 1 row (NULL, 0) for an ok call —— rows=1 but it's not a number). real_rows < rows means vacuum was hit.
        "real_rows": real_rows,
        "vacuous": bool(ok_calls) and not real_calls,
        "sql": sql,
        "errors": [str(o.get("error") or "") for o in observations if not o.get("ok")],
        "facts": facts,
        "facts_count": len(facts),
        "answer": answer,
        "answer_note": base_note,
        # —— The referee (VLML)'s independent path, disclosed side by side with Voyager's path ——
        "referee_sql": ref_sql,
        "referee_rows": ref_rows,
        "referee_trajectory": ref_traj,
        # —— The method-and-approach comparison conclusion (the basis for grading, see ⑥)——
        "method_compare": compare or None,
        "method_reason": str(compare.get("reason") or ""),
        "method_diff": str(compare.get("diff") or ""),
        "same_metric": bool(compare.get("same_metric")),
        "voyager_sound": bool(compare.get("voyager_sound")),
        # The two sides are not computing the same quantity → this question was **not really graded** (usually the referee itself went off-track).
        # In that case coverage is "definition agreement 0", not "Voyager answered wrong" —— the dashboard must display them separately,
        # otherwise a bright red 0% would pass "not graded" off as "weak capability".
        "judged": bool(compare.get("same_metric")),
        # —— Coverage / mastery (only computed on the consumption side, see ⑥)——
        "coverage": None,
        "score": None,
        "verdict": "",
        "covered_points": [],
        "missing_points": [],
        "unjudgeable": [],
        # The hard ruler (only run when this question routes to a task with a rubric), attached alongside the main ruler for the dashboard
        "coverage_referee": None,
        "verdict_referee": "",
        # —— Mastery = growth of the ability set ("once this question is answered, it is part of his abilities")——
        "mastered": bool(absorbed.get("mastered")),
        "skill_action": str(absorbed.get("skill_action") or ""),
        "mastered_total": m_tot,
        "ability_total": a_tot,
        # Mastery = mastered / total to be mastered (not a conversion of coverage)
        "mastery": (round(m_tot / a_tot, 4) if a_tot else None),
        "judge": "",
        "skipped": "",
    }

    # ---- ⑥ Coverage / mastery ----
    # ⚠️ Computed only on this **consumption** line. Practice (loop.py) doesn't grade and writes coverage=None.
    # ⚠️ Once computed, it only lands on disk for the dashboard, and is **never fed back** into any of Voyager's prompts.
    # ⚠️ Only real data counts: when all ok calls are vacuum results (1 row (NULL, 0)), the facts have no real source,
    #   which is equivalent to "never once connected" —— coverage must not be computed on vacuum rows (measured to produce a lush green 100%).
    if not real_calls:
        base["skipped"] = ("No real data retrieved (call failed, or the results are all vacuum values: "
                           "pure aggregation on an empty table returns 1 row (NULL, 0)), the facts have no source, "
                           "coverage not counted (avoids grading fabrication)")
        _log(base)
        return base

    pts = [str(p.get("point") or p.get("dimension")) for p in (ref_points or [])]
    base["derived_points"] = pts

    if compare:
        # Main ruler: lay out both sides' methods (SQL) and approaches (what was retrieved) and let the LLM judge
        # —— no longer requiring both sides to first compress results into {subject, dimension, value} single-value facts.
        # Agreement is coverage: it measures "whether Voyager's path uses the same definition as VLML's independent path,
        # and whether it has any hard defect".
        base.update({
            "coverage": float(compare["agreement"]),
            "score": int(round(float(compare["agreement"]) * 100)),
            "verdict": ("Both sides use the same definition" if compare["same_metric"]
                        else "The two sides compute different quantities"),
            # Since approaches rather than keys are compared, "covered / missing" refers to **whether the scoring points were covered under the same definition**
            "covered_points": pts if compare["same_metric"] else [],
            "missing_points": [] if compare["same_metric"] else pts,
            "unjudgeable": (
                ([] if compare["voyager_sound"]
                 else ["Hard defect in Voyager's approach: " + (compare.get("reason") or "")])
                + ([] if compare["same_metric"]
                   else ["The two sides compute different quantities, so this question was not really graded: "
                         + (compare.get("diff") or compare.get("reason") or "")])),
            "judge": "method_compare",
        })
    elif not ref_traj:
        base["skipped"] = ("The referee (VLML) path retrieved no real data (call failed, or the results are all "
                           "vacuum values: pure aggregation on an empty table returns 1 row (NULL, 0)) → "
                           "there is no second approach to compare against, coverage cannot be computed (left honestly empty, not passing off 0 as a score)")
    elif rows <= 0 or ref_rows <= 0:
        # Both paths must really retrieve data before grading —— otherwise two empty results would be judged "same definition, 100%"
        # (measured: Cloud9/Corrode has no data in the current database, both sides 0 rows, same method, judged a perfect score).
        # ⚠️ Vacuum rows also count as "no data retrieved": the 1 row (NULL, 0) from pure aggregation on an empty table is not a number.
        base["skipped"] = (
            f"One path retrieved no data (Voyager {real_rows} real rows / VLML {ref_real_rows} real rows) → "
            "there are no numbers to compare, coverage left honestly empty. **When both sides are at 0 rows, no matter how alike the SQL is, agreement cannot be judged** —— "
            "that is equivalent to giving a perfect score to \"both sides retrieved no data\".")
    else:
        base["skipped"] = ("The referee (VLML) retrieved data, but the method-and-approach comparison reached no conclusion → "
                           "coverage left honestly empty, not passing off 0 as a score")

    # Skill-validity feedback (maintained by the skill library itself; not feedback for Voyager)
    if reused and base.get("coverage") is not None:
        try:
            import skill_store
            skill_store.mark_result(
                [str(s.get("key") or "") for s in skills[:2]],
                bool(base["coverage"] >= 0.6))
        except Exception:  # pragma: no cover
            pass

    # ---- Hard ruler: only run once extra when this question **routes to a task with a rubric** ----
    # The legacy task bank's rubric is a pre-existing standard set of scoring points, harder than those derived from this sentence.
    # It is shown to the dashboard **side by side** with the main ruler above, with no conversion between them.
    # ⚠️ We deliberately do **not** write MasteryStore.update(score=coverage) here:
    #   mastery and coverage are two different things (user, 2026-10-13) —— mastery = answered and entered the skill library
    #   (already done by _absorb above), while coverage is only a quality score for how complete this answer was.
    #   Writing coverage into mastery would again define mastery by score, and the two semantics would clash.
    if task is not None:
        try:
            import vlml0_referee
            from loop_core import evaluate_vs_referee
            ref = await vlml0_referee.answer(task)
            ev = evaluate_vs_referee(
                facts, list(task.rubric or []), list(ref.facts or []),
                tool_calls=len(ok_calls),
            )
            base.update({
                "coverage_referee": float(ev.coverage),
                "verdict_referee": str(ev.verdict),
                "judge": (f"{base.get('judge') or ''}+referee".strip("+")
                          or "referee"),
            })
        except Exception as e:  # pragma: no cover
            base["referee_skipped"] = (
                f"Hard ruler not computed: {type(e).__name__}: {str(e)[:120]}")

    _log(base)
    return base


def _derive_dims(question: str) -> list[dict[str, Any]]:
    """Extract from this sentence itself "which quantities this question should answer" (names only, no values).

    Why it is needed: coverage compares Voyager's fact set against the referee's, and the comparison key is
    (subject, dimension). When it routes to the legacy task bank, dimension is given by the rubric;
    when it doesn't route (most free-form human questions don't), there is no rubric → coverage cannot be computed,
    and "compute coverage starting from consumption" falls through.

    So here we have the LLM derive scoring points from **this sentence**. ⚠️ What is derived is only the **name**
    (which quantities should be answered), **not the value** —— values are still retrieved from the DB by each side independently:
      · Voyager side: retrieved with its accumulated skills (③ of this file)
      · Referee side: independently via VLML's own toolchain (_referee_observations)
    Both values are DB ground truth; what is compared is two independent retrieval results, not "a model-made-up reference answer".
    """
    out = chat_json(
        [
            {"role": "system",
             "content": ("Extract from one sentence \"which quantities must be given to answer it\". Give **names and objects only**, "
                         "no values. 2 to 4 of them. Return JSON only: "
                         "{\"points\":[{\"point\":\"English description\",\"dimension\":\"English dimension name\","
                         "\"subject\":{\"team\":\"...\",\"map\":\"...\"}}]}\n\n"
                         "⚠️ Three hard constraints (each hit in practice):\n"
                         "  1. dimension must be an **English snake_case numeric metric name** —— "
                         "the likes of kd_ratio / total_damage / usage_rate / win_rate / kast_pct.\n"
                         "     **Chinese is not allowed** (measured: a Chinese phrase was written as the dimension → the referee wrote SQL per it "
                         "that retrieved only player names, not ratios → not a single fact could be organized → coverage fell through).\n"
                         "     **No ranking words like highest / lowest / most** —— those are phrasings, not metrics; "
                         "when asked \"who is highest\", dimension should be the metric itself (kd_ratio).\n"
                         "  2. subject = **who is being talked about** (real values of team / map / player, "
                         "e.g. {\"map\":\"Corrode\"}); don't stuff descriptions like \"highest K/D ratio\" in, "
                         "and don't put map names into the team field (measured to have happened once).\n"
                         "  3. Each point must be **a single number computable with SQL**, "
                         "not a description.")},
            {"role": "user", "content": question},
        ],
        temperature=0.0,
    )
    # Hard validation: dimension must be a pure-ASCII numeric metric name.
    # A non-ASCII dimension name would make both sides unable to write comparable facts ——
    # better to judge "this question yields no comparable quantities" (left honestly empty) than to compare a misaligned name into a 0 score.
    pts: list[dict[str, Any]] = []
    for p in (out.get("points") or []):
        if not isinstance(p, dict):
            continue
        d = str(p.get("dimension") or "").strip()
        if not d or not d.isascii():
            continue
        s = dict(p.get("subject") or {})
        s = {k: v for k, v in s.items() if v not in (None, "", "?")}
        pts.append({"point": str(p.get("point") or d), "dimension": d, "subject": s})
    return pts[:4]


async def _referee_observations(question: str,
                                 points: list[dict[str, Any]],
                                 env: Any = None,
                                 schema: str = ""
                                 ) -> tuple[list[dict[str, Any]], str, list[str]]:
    """The referee (VLML) **independently** retrieves the numbers once —— **data retrieval only, no organizing into facts**.

    ⚠️ Grading method changed 2026-10-13 (user's exact words): "Organizing facts is something that should be handed to the LLM anyway;
        if we're on the consumption side, directly compare Voyager's and VLML's methods and approaches, compute mastery and the curve,
        feed them into the skill library, and then have the LLM produce narrative insights."

    Previously the referee side, after retrieving numbers, ran another pass of "organize into {subject, dimension, value}";
    that pass cost two extra LLM calls and was the **main failure source**: when the answer is a table it can't be compressed into
    single-value facts → the referee side handed in a blank sheet → coverage fell through directly (even though both sides retrieved the numbers).
    Now the referee only brings the numbers back, and comparison is handed to `_method_compare` to directly examine both sides'
    methods and approaches —— LLMs are naturally good at this kind of semantic comparison; there's no need to compress results into key-values first.

    Returns (observations, sql, trajectory). Returns empty if nothing is retrieved (the caller leaves it honestly empty, no fabrication).
    """
    dims = [str(p.get("dimension")) for p in (points or [])]
    subs = [dict(p.get("subject") or {}) for p in (points or [])]

    # The referee's data retrieval goes through **the same iterative-refinement mechanism** (solve: write SQL → execute → on failure, rewrite with SQL critique
    # for 3 rounds). The previous hand-written "retry on error" loop was measured to fail column-name ambiguity across all 3 rounds ——
    # the critique mechanism is verified on the practice side to be able to correct errors; don't rebuild a weakened version of it.
    want = "; ".join(
        f"dimension=`{d}`" + (f", subject={json.dumps(s, ensure_ascii=False)}" if s else "")
        for d, s in zip(dims, subs)
    )
    q2 = (f"(Referee · independently retrieving the standard answer) {question}\n"
          + (f"Quantities to give (use these dimension names verbatim; compute the values from the database): {want}\n" if want else "")
          + "⚠️ Ratios / divisions must use NULLIF(x, 0) (e.g. AVG(kills)/NULLIF(AVG(deaths),0)) —— "
            "x/0 yields NaN without error in DuckDB, and what comes back is an incomparable number; "
            "also prefer doing the division at the aggregation layer (AVG first, then divide), not MAX over row-wise ratios.\n"
            "⚠️ Column names in multi-table JOINs must carry the table-name prefix (two tables often have same-named columns, and bare names are Ambiguous).\n"
            # 2026-10-14: although empty tables are annotated in the middle of the schema, measured that glm-4-flash still
            # queried agg_team_round_summary (0 rows) across 3 iterations —— pure aggregation on an empty table returns
            # 1 row (NULL, 0), looking like data was retrieved when it wasn't. Emphasize it once more
            # in the user message (no answers given, only table-selection discipline; the referee's independence is unaffected).
            "⚠️ Tables annotated \"0 rows · empty table\" in the schema **yield only (NULL, 0) when queried**; "
            "prefer tables annotated with row counts; if a computed ratio's columns are all NULL, the wrong table was queried.")
    if env is not None:
        from voyager_vlml.agents.action import VLMLActionAgent
        act = VLMLActionAgent(env=env)
        observations, sql2 = await act.solve(q2, max_iters=4)
        ok_obs = [o for o in observations if o.get("ok")]
        # ⚠️ 2026-10-14: ok ≠ data retrieved. Pure aggregation on an empty table returns 1 row (NULL, 0),
        #   ok=True but vacuum —— when repacking, vacuum must not be laundered into a "successful observation",
        #   otherwise ⑥'s ref_real_rows criterion is bypassed and two vacuum results would again be judged "same definition".
        #   (Measured: the referee queried agg_team_round_summary, annotated "0 rows, don't query" in the schema, across all 3 iterations,
        #   and the final ok observation was exactly (None, 0).)
        #   Also: solve breaks as soon as real data is retrieved, so if a real-data observation exists it must be the last one,
        #   paired with the returned sql2; if none exists, no real data was retrieved across all 3 rounds, so return empty honestly.
        real_obs = [o for o in ok_obs if has_real_data(o)]
        if not real_obs:
            return [], sql2, []
        last = real_obs[-1]
        return ([{"tool": "query_sql", "args": {"sql_query": sql2},
                  "data": last.get("data"), "rows": last.get("rows") or 0,
                  "error": None, "ok": True}], sql2, ["query_sql"])

    # Fallback: when env is unavailable, fall back to the old VLML orchestration (tool registry + dataset context).
    # ⚠️ Hit in practice: without injecting the actually-queried schema, the referee would make the same **column-name hallucination** as Voyager ——
    # writing `base_events.player_name`, while the real columns are actor_player_name / target_player_name
    # → all three calls Binder Error → the referee side retrieved not a single number → coverage fell through directly.
    # So the tables/columns that really exist in the database must be given here (following entity_catalog's "actually query, don't hard-code").
    try:
        from coach import _execute, _plan_prompt
    except Exception as e:  # pragma: no cover
        print(f"  (referee toolchain unavailable: {type(e).__name__}: {e})")
        return [], "", []
    plan_user = _plan_prompt(q2)
    if schema:
        plan_user += ("\n\nTables and columns that actually exist in the current database (**only names appearing here may be referenced**, "
                      "especially column names; a wrong name errors out directly):\n" + schema[:6000])
    plan = chat_json(
        [
            {"role": "system", "content": "You are the original VLML analysis engine. Retrieve data only; make no subjective inferences."},
            {"role": "user", "content": plan_user},
        ],
        temperature=0.1,
    )
    obs, traj, errors = await _execute(plan.get("calls") or [])
    if not traj and errors:
        # One retry on error: feed the tool errors back as-is and have it rewrite with the correct column names
        plan2 = chat_json(
            [
                {"role": "system", "content": "You are the original VLML analysis engine. Retrieve data only; make no subjective inferences."},
                {"role": "user",
                 "content": (plan_user + "\n\nAll calls in the last round errored:\n"
                             + "\n".join(str(e)[:300] for e in errors[:3])
                             + "\n\nFix the column/table names per the errors above and re-plan.")},
            ],
            temperature=0.1,
        )
        obs, traj, _ = await _execute(plan2.get("calls") or [])
    if not traj:
        return [], "", []
    return ([{"tool": o.get("tool"), "args": o.get("args"),
              "data": o.get("result"), "error": o.get("error"),
              "ok": not o.get("error")} for o in obs],
            "", list(traj))


_RUNTIME_TOPICS_FILE = _MVE_DIR / "runtime_topics.json"


def _runtime_topics() -> dict[str, Any]:
    """On the consumption side, questions **answered but not routing to a knowledge graph node** land here (new ability-set items)."""
    try:
        if _RUNTIME_TOPICS_FILE.exists():
            return json.loads(_RUNTIME_TOPICS_FILE.read_text(encoding="utf-8"))
    except Exception:  # pragma: no cover
        pass
    return {}


def _runtime_topic_id(question: str) -> str:
    """Answered, but not routing to any existing question → create a **stable** ability id.

    Why it must exist (the user's definition): "I ask you one sentence, Voyager answers it, and from then on this question is part of
    his abilities; that is mastery!" —— **not routing ≠ not answered**.
    Without it, mastery would become "only questions that route to the legacy task bank count", which conflates two things
    (answered vs. routable) into one condition.
    Measured (2026-10-13): asked "the average number of kills per role in each round", Voyager retrieved 484 rows
    and the answer was fully produced, yet the dashboard showed "not answered, did not enter the skill library" —— stuck exactly on this condition.

    Stable = the same sentence always yields the same id (a hash of the question text), so asking a second time won't
    create a new node that inflates the denominator (mastery denominator = knowledge graph nodes + ability items registered here).
    """
    import hashlib
    h = hashlib.md5(str(question or "").strip().encode("utf-8")).hexdigest()[:8]
    tid = f"consume_{h}"
    reg = _runtime_topics()
    if tid not in reg:
        reg[tid] = {"id": tid, "name": str(question or "")[:60],
                    "question": str(question or ""), "source": "consume"}
        try:
            _RUNTIME_TOPICS_FILE.write_text(
                json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # pragma: no cover
            print(f"  (registering a new ability item failed: {type(e).__name__}: {e})")
    return tid


def _coach_log_path() -> Path:
    """The consumption line's ledger path (the same file written by `coach._log_import`)."""
    try:
        from coach import COACH_LOG
        return Path(COACH_LOG)
    except Exception:  # pragma: no cover
        return _MVE_DIR / "coach_log.jsonl"


def _consume_book() -> set[str]:
    """**The consumption side's ability ledger** —— the isomorphic counterpart of the original `curriculum.py:135`'s `completed_tasks`.

    Takes deduplicated topic_ids from `coach_log.jsonl` where `mode=voyager_consume` and `mastered` is true.

    ⚠️ Why we deliberately do **not** count completed entries in `MasteryStore` (`topic_mastery.jsonl`):
      The `completed` there is a **deduplication flag** ("this node has been planned; don't plan it again in `select_next`"),
      and both the practice loop (`curriculum.py:296`) and the old backend (`run_mve.py:463`) write to it.
      Treating it as "mastered" would mix the practice line's 29 `runtime_*` entries into the numerator —— the server measured
      `cumulative 58 / 45 questions (mastery 129%)`, with the **numerator larger than the denominator**.
      The root cause and the decoupling approach are in the docstring of `topic_graph.MasteryStore.mark_completed`.

    Following the original Voyager:
      `voyager/agents/curriculum.py:135` learning curve = `len(self.completed_tasks)`
      —— that is **the learning loop's own ledger**, not a globally shared mastery file;
      `:339` only appends on success, **failures are not counted**; the original has no "exam" at all.
    So: **not counted during learning**. The consumption line keeps this ledger itself; a practice run only advances course planning,
    and does not enter the ability set —— abilities only grow from the side where a human really asked and it was answered.
    """
    p = _coach_log_path()
    book: set[str] = set()
    if not p.exists():
        return book
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(r.get("mode") or "") != "voyager_consume":
                continue
            # mastered false = no data retrieved / no answer produced → per the original, **failures are not counted**
            if not r.get("mastered"):
                continue
            tid = str(r.get("topic_id") or "").strip()
            if tid:
                book.add(tid)
    except Exception:  # pragma: no cover
        pass
    return book


def _ability_universe() -> set[str]:
    """The full ability set (the **denominator** of mastery): knowledge graph nodes ∪ **consumption-side** registered ability items.

    ⚠️ Why the practice line's `runtime_*` entries are excluded:
      `runtime_topics.json` is **the same file** shared by the practice line (`loop.py:_save_runtime_topic`) and the consumption line
      (this file's `_runtime_topic_id`); on the server, of the 16 entries,
      **12 have `source="practice"`**. Questions registered by the practice line are "ones the course plans to practice",
      not "ones a human asked"; counting them in the denominator would only dilute mastery —— and those questions will most likely
      never be answered on the consumption side, equivalent to 12 questions lying in the denominator that can never earn points.

    ⚠️ Why the denominator includes knowledge graph nodes **not yet mastered**:
      the denominator is "how many questions this course has in total" and the numerator is "how many of them it can do" —— this way mastery is
      "how much has been conquered", not "of those answered, how many were right" (the latter would falsely approach 100%).
    """
    ids: set[str] = set()
    try:
        import topic_graph
        ids |= set(topic_graph.TopicGraph().topics.keys())
    except Exception:  # pragma: no cover
        pass
    for k, v in _runtime_topics().items():
        k = str(k)
        if k.startswith("consume_") or str((v or {}).get("source") or "") == "consume":
            ids.add(k)
    return ids


def _absorb(question: str, sql: str, topic_id: str) -> dict[str, Any]:
    """Answered → **this question becomes part of his abilities → that is mastery** (set by the user on 2026-10-13).

    Mastery is not a score converted from coverage, but **growth of the ability set**:
      "I ask you one sentence, Voyager answers it, and from then on this question is part of his abilities".

    So two things are done here:
      1. Mark this question as **mastered** (`MasteryStore.mark_completed`);
      2. Store this retrieval SQL as a **reusable recipe** in the skill library (when this question had no recipe).
         What is stored is the **computational method**, not the values computed this time —— so the same kind of phrasing can be reused next time.

    ⚠️ The criterion is **whether it was really answered** (tools connected + data retrieved + answer produced),
    **not coverage**. Whether coverage is computed, and how much, does not affect "whether this question enters the skill library" ——
    otherwise that would be stuffing VLML's grading back into the learning determination, which is exactly what the design forbids.

    ⚠️ Questions that don't route to an existing question **enter the skill library all the same** (`_runtime_topic_id` gives it a stable id),
    and must not be judged "not mastered" for not routing —— that would tie "answered" and "routable"
    together as one condition (measured to have been bitten by this).
    """
    out: dict[str, Any] = {"mastered": False, "skill_action": "",
                           "mastered_total": 0, "ability_total": 0, "topic_id": ""}
    if not sql:
        return out
    tid = topic_id or _runtime_topic_id(question)
    out["topic_id"] = tid
    try:
        import topic_graph
        # Still marked completed —— but that is its **original** meaning: "this node has been planned, don't plan it again"
        # (a question a human has asked and that was answered need not be practiced again by the course). With source="consume"
        # marking that this was marked on the consumption side, so it can be audited later; the semantic decoupling is in mark_completed's docstring.
        topic_graph.MasteryStore().mark_completed(tid, source="consume")
        out["mastered"] = True
        # The numerator and denominator are **same-source**: both computed only from this consumption-side set, no longer mixing in the practice line's ledger.
        # Previously the numerator counted all of MasteryStore's completed (58, including the practice line's 29 runtime_*) and
        # the denominator counted "graph 29 + runtime_topics 16" (including the practice line's 12 practice entries),
        # and the two sets' definitions were inconsistent → a real run printed 129% (numerator larger than denominator).
        book = _consume_book() | {tid}
        # The denominator further unions with book: a topic_id passed in from outside may be neither in the graph nor in the registry;
        # unioning it guarantees mastered ≤ ability always holds, and the percentage never exceeds 100%.
        universe = _ability_universe() | book
        out["mastered_total"] = len(book)
        out["ability_total"] = len(universe)
    except Exception as e:  # pragma: no cover
        print(f"  (marking mastery failed: {type(e).__name__}: {e})")
        return out

    try:
        import skill_store
        have = [k for k in (skill_store._load_raw().get("skills") or {})
                if str(k).startswith(f"{tid}::")]
        if have:
            skill_store.mark_result(have, True)
            out["skill_action"] = "Existing recipe —— marked as used and valid"
        else:
            _, key = skill_store.add(
                tid, tid,
                f"Data-retrieval method that answered a consumption question: {question[:60]}",
                code=sql,
                blueprint={"source": "consume", "question": question},
                source="consume",
            )
            out["skill_action"] = f"New recipe stored in the skill library: {key}" if key else "(content too short —— not stored)"
    except Exception as e:  # pragma: no cover
        print(f"  (storing the recipe failed: {type(e).__name__}: {e})")
    return out


def _log(result: dict[str, Any]) -> None:
    """Write one human-import consumption record (read by the dashboard from `coach_log.jsonl` for display).

    Same file and same field set as `coach._log_import`; the dashboard's human-import section can display it unchanged;
    the new coverage/score/verdict/mastery/reused fields are absent from old records → the dashboard must be fault-tolerant.
    """
    try:
        from coach import _log_import
        _log_import({
            **result,
            "topic_matched": bool(result.get("topic_id")),
            "coverage": result.get("coverage"),
            "score": result.get("score"),
            "verdict": result.get("verdict", ""),
            "mastery": result.get("mastery"),
            "reused": bool(result.get("reused")),
            "used_skills": result.get("used_skills") or [],
            "answer": result.get("answer", ""),
            "ok_tool_calls": result.get("ok_tool_calls", 0),
            "rows": result.get("rows", 0),
        })
    except Exception as e:  # pragma: no cover
        print(f"  (writing the human-import log failed: {type(e).__name__}: {e})")


def _main() -> int:
    ap = argparse.ArgumentParser(description="Consumption: a human asks one question, Voyager answers with accumulated skills")
    ap.add_argument("question", nargs="*", help="the question you want to ask")
    a = ap.parse_args()
    q = " ".join(a.question).strip() or "On which map does Cloud9 collapse the hardest?"
    r = asyncio.run(consume(q))
    print("=== Consumption result ===")
    print("Question :", r.get("question"))
    print("Routed   :", r.get("topic_id") or "(not routed)", "|", r.get("topic_hit"))
    print("Ability  :", "reused recipe" if r.get("reused") else "done on the spot",
          f"({r.get('skills_found')} found in the library)", r.get("reuse_note") or "")
    print("Tools    :", r.get("trajectory"), f"| {r.get('tool_calls')} calls, {r.get('rows')} rows retrieved")
    print("SQL      :", str(r.get("sql") or "").replace("\n", " ")[:160])
    print("Facts    :", r.get("facts_count"))
    for f in (r.get("facts") or [])[:6]:
        subj = "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items())
        print(f"   {subj}  -  {f.get('dimension')} = {f.get('value')} (base={f.get('base')})")
    print("--- Answer ---")
    print(r.get("answer") or "(no data retrieved, no answer produced)")
    # Mastery and coverage are **two different things**, printed separately:
    #   mastery = answered → this question enters the skill library (ability-set growth)
    #   coverage = how complete this answer was (quality score, for humans only)
    print("--- Mastery (ability set) ---")
    if r.get("mastered"):
        print(f"Mastered  -  cumulative {r.get('mastered_total')} / {r.get('ability_total')} questions"
              f" (mastery {float(r.get('mastery') or 0):.0%})")
        print("  ", r.get("skill_action") or "")
    else:
        print("Not answered, did not enter the skill library." + (r.get("skipped") or ""))
    print("--- Coverage (quality score, for humans only, not fed back into Voyager) ---")
    if r.get("coverage") is None:
        print("Not computed:", r.get("skipped") or "(no scoring points)")
    else:
        print(f"Coverage {r['coverage']:.0%}  -  score {r['score']}  -  verdict {r['verdict']}"
              f"  -  ruler={r.get('judge')}")
        if r.get("method_reason"):
            print("  Comparison basis:", r.get("method_reason"))
        if r.get("method_diff"):
            print("  Difference between the two sides:", r.get("method_diff"))
        print("  Covered:", r.get("covered_points"))
        print("  Missing:", r.get("missing_points"))
        if r.get("unjudgeable"):
            print("  Hard defects:", r.get("unjudgeable"))
        if r.get("coverage_referee") is not None:
            print(f"  Hard ruler (task-bank rubric) coverage {r['coverage_referee']:.0%}"
                  f"  -  {r.get('verdict_referee')}")
        elif r.get("referee_skipped"):
            print("  Hard ruler:", r.get("referee_skipped"))
        if r.get("judge") == "method_compare":
            print("  (Main ruler = comparison of both sides' methods and approaches: answers are not required to be compressed into single-value facts, "
                  "so even a table as an answer can be graded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
