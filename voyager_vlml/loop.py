"""Minimal driver for the VLML-isomorphic learning loop (⑤ smoke-test use; dashboard deferred).

The closed loop (truly generates its own questions and fetches data):
  curriculum.propose_next_task()   ← milestone-tree topic selection (seed → trunk, completed excluded)
      ↓
  question.generate(topic)         ← LLM **poses a question** per milestone node (question text only, no answer)
      ↓
  action.solve(question)           ← LLM **writes SQL** → VLML MCP (DB/RIB/VLGG) executes
                                     on failure, iteratively rewrites with critique (up to 3 rounds)
      ↓
  critic.check_task_success()      ← **success/failure = whether data was really fetched from the DB**
                                     (not compared against a reference answer)
      ↓
  curriculum.update_exploration_progress()  ← on success → unlock milestone + attach SQL skill pointer
                                             otherwise record failure/skip (with reason)

⚠️ Nowhere in the whole chain is there a VLML verdict, coverage, or reference answer —— those are
   for humans only, and live only in the dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# run_log / skill_store live under `mve/` (the dashboard reads exactly those two files).
# The cockpit launches this script from the **repo root** (cwd=repo root); sys.path[0] is the script's
# own directory, not mve/ —— without this block, `import run_log` gets silently swallowed by the
# except below, and the symptom is "questions ran, skills got saved, but the dashboard curve doesn't move".
_MVE_DIR = _REPO_ROOT / "mve"
if str(_MVE_DIR) not in sys.path:
    sys.path.insert(0, str(_MVE_DIR))

from voyager_vlml.agents.action import VLMLActionAgent  # noqa: E402
from voyager_vlml.agents.critic import VLMLCriticAgent  # noqa: E402
from voyager_vlml.agents.curriculum import VLMLCurriculumAgent  # noqa: E402
from voyager_vlml.agents.question import VLMLQuestionAgent  # noqa: E402
from voyager_vlml.env.vlml_mcp import VLMLMCPEnv  # noqa: E402

# ---- stop signal (isomorphic to original voyager.py:_stop_requested / run_voyager_learn.py:request_stop) ----
# Original: dashboard "stop" → SIGTERM → request_stop() → the main loop exits **after the current task ends**,
# without a hard kill (skills are already persisted per task).
# ⚠️ The learning loop is inherently **non-stopping** (original `while True` + only an iteration cap),
#    stopping is an external action, not a termination condition inside the loop.
_STOP = {"requested": False}
_MAX_ITERATIONS = 0      # total question cap in non-stop mode (0 = unlimited; original --max-iterations defaults to 160)

# ---- the loop's own state file (kept **separate** from the dashboard's state file) ----
# User's exact words: "the loop's and the dashboard's running states are written into two different state files".
# Why they must be separate (a coupling pitfall hit in practice): the dashboard state (mve/pilot_state.json)
# is the **human's operation log** (who clicked start, who clicked stop, what pid); the loop state is the
# **loop's own heartbeat** (which round it's on, whether this round's tool calls succeeded, how many
# consecutive rounds fetched no evidence). Mixing them in one file creates two mutually polluting writes:
#   · the loop rewrites the state file every round → the stop_requested bit the human clicked may get overwritten;
#   · the dashboard rewrites the file to show pid → it erases the "no data fetched this round" the loop just wrote.
# After the split: the dashboard **only reads** the loop state, and each file has exactly one writer.
LOOP_STATE = _MVE_DIR / "loop_state.json"

# Where runtime-generated knowledge-graph nodes are persisted —— **the same file as the consumption side**
# (the consumption side registers `consume_<hash8>`, here we register `runtime_<hash8>`).
# Why not store them separately: the denominator of mastery is "graph nodes + the capability items registered
# here", and two files would eventually drift into inconsistent tallies.
RUNTIME_TOPICS = _MVE_DIR / "runtime_topics.json"


def _load_runtime_topics() -> dict[str, Any]:
    try:
        import json as _json
        if RUNTIME_TOPICS.exists():
            obj = _json.loads(RUNTIME_TOPICS.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def _save_runtime_topic(node: dict[str, Any]) -> None:
    """Persist one runtime-generated knowledge-graph node.

    ⚠️ Why it must be persisted (found after 12 rounds of real server runs on 2026-10-13):
      Previously `_generate_new_task` only stuffed `curr.graph.topics[tid]` in memory,
      so it vanished when the process exited. The result: the skill library got 14 new recipes,
      MasteryStore recorded 34 completed, but **not a single graph node was left behind** ——
      on restart `select_next` can't see these completed nodes, so it's exhausted again, generates
      new questions again, and re-verifies the same questions.
      The user's design of "task succeeds → graph node marked completed (**no re-planning**)" is thus
      voided: with no node saved, every restart must re-plan from scratch.
    """
    try:
        import json as _json
        reg = _load_runtime_topics()
        tid = str(node.get("id") or "")
        if not tid:
            return
        reg[tid] = node
        RUNTIME_TOPICS.write_text(
            _json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # pragma: no cover
        print(f"  (runtime node persistence failed: {type(e).__name__}: {e})")


def _inject_runtime_topics(curr: Any) -> int:
    """Load the persisted runtime nodes back into the curriculum's in-memory graph (no need to regenerate the same batch after restart)."""
    nodes = [v for k, v in _load_runtime_topics().items()
             if isinstance(v, dict) and str(k).startswith("runtime_")]
    n = 0
    for node in nodes:
        tid = str(node.get("id") or "")
        if tid and tid not in curr.graph.topics:
            curr.graph.topics[tid] = node
            n += 1
    return n


def _write_loop_state(**kw: Any) -> None:
    """Write the loop heartbeat to `mve/loop_state.json` (only the loop writes; the dashboard only reads)."""
    import json as _json
    import time as _time

    cur: dict[str, Any] = {}
    try:
        cur = _json.loads(LOOP_STATE.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.update(kw)
    cur["updated_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        LOOP_STATE.write_text(
            _json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # pragma: no cover
        pass


# Keyword table for error triage. **Order matters**: infrastructure-class is judged first, because it can also
# look like "does not exist" (`database does not exist` vs `column "x" does not exist`).
_INFRA_ERRORS = (
    "cannot open database", "database does not exist", "io error",
    "catalog error", "permission denied", "no module named",
    "unloaded tool", "connection", "failed to open", "file not found",
)
_SQL_ERRORS = (
    "parser error", "syntax error", "binder error", "conversion error",
    "not implemented", "transactioncontext", "invalid input",
    "referenced column", "does not exist", "ambiguous reference",
)


def _is_infra_error(err: str) -> bool:
    e = (err or "").lower()
    return any(m in e for m in _INFRA_ERRORS)


def _is_sql_error(err: str) -> bool:
    e = (err or "").lower()
    return any(m in e for m in _SQL_ERRORS)


def _classify_no_evidence(observations: list[dict[str, Any]]) -> str:
    """Why no evidence was fetched this round —— so an observer can tell "it didn't learn" from
    "the pipeline never ran at all".

    ⚠️ The user explicitly required this distinguishability (exact words: "the dashboard must use
      distinguishable information to let the observer tell 'it didn't learn' from 'the pipeline never
      ran at all'"). The four outcomes are handled completely differently:
        · `linkage`   —— not a single tool call succeeded, and the error is **infrastructure-class**
                         (can't open the DB, tool not loaded, module missing). Fixable via config/deps;
                         unrelated to "did it learn or not".
        · `sql_error` —— the tool **did run**, but the SQL it wrote is wrong (Parser/Binder error).
                         This is **its** competence issue, not a pipeline issue.
                         ⚠️ This tier was added on 2026-10-13: previously "wrong SQL syntax/column name"
                         was all classified as `linkage`, so the dashboard told people to check
                         "is the DB there, is the tool loaded" —— checking places that were perfectly fine.
        · `empty`     —— SQL ran but fetched 0 rows: the question has no data in the DB, or the filter
                         is too strict.
        · `unproven`  —— data was fetched but the criterion didn't pass (the critic judged no valid
                         evidence was obtained).
    The non-stop loop **never stops for any of these** —— this function only labels them honestly.
    """
    obs = observations or []
    if not obs:
        return "linkage"          # not even one call was issued = the environment never spun up
    ok_n = sum(1 for o in obs if o.get("ok"))
    if ok_n <= 0:
        errs = [str(o.get("error") or "") for o in obs]
        if any(_is_infra_error(e) for e in errs):
            return "linkage"
        if any(_is_sql_error(e) for e in errs):
            return "sql_error"
        return "linkage"          # tool failed but cause unknown → treat as a linkage problem in the worst case
    rows = sum(int(o.get("rows") or 0) for o in obs if o.get("ok"))
    if rows <= 0:
        return "empty"
    return "unproven"


def _install_stop_handler() -> None:
    import signal

    def _handler(signum, _frame):
        _STOP["requested"] = True
        print(f"\n(received signal {signum}: finish the current question then stop)", flush=True)

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except Exception:  # pragma: no cover
            pass


def _retrieve_skills(
    tid: str, question: str, quiet: bool = False
) -> list[dict[str, Any]]:
    """Following the original `voyager.py:195`: **before** this turn writes SQL, first retrieve learned skills.

    The original is `skills = self.skill_manager.retrieve_skills(query=self.context)`,
    done on every rollout, whether the task came from the curriculum or was invented fresh. We follow
    suit: the retrieved recipes are concatenated into the system prompt (isomorphic to `action.py:94`'s
    `+ skills`), so **only an injection counts as a hit** —— we mark_used right here.

    No recipes at all / none relevant (cross-question similarity threshold) / retrieval error → return
    empty, without disturbing the main flow: an empty skill library is the normal state of cold start
    and should not be treated as an exception.
    """
    try:
        import skill_store

        have = skill_store._load_raw().get("skills") or {}
        if not have:
            return []
        got = skill_store.retrieve(tid, question, top_k=skill_store.RETRIEVAL_TOP_K)
        keys = [str(s.get("key") or "") for s in got]
        if keys:
            skill_store.mark_used(keys)
            if not quiet:
                print(
                    f"  injected {len(keys)} learned recipe(s): "
                    + ", ".join(k.split("::")[0] for k in keys[:3])
                    + (" …" if len(keys) > 3 else "")
                )
        return got
    except Exception as e:  # pragma: no cover
        print(f"  (skill library retrieval failed: {type(e).__name__}: {e})")
        return []


async def _generate_new_task(curr: Any, env: Any, act: Any = None,
                             max_tries: int = 3, max_iters: int = 4
                             ) -> tuple[str, str, list[dict[str, Any]], str,
                                        list[str]]:
    """Generate a new task, **and first verify it can really fetch data from the DB**.

    Returns (topic_id, question, observations fetched during verification, SQL used for verification).
    If nothing can be fetched, try another and regenerate; if none can be fetched, return empty strings
    —— the caller honestly reports "no doable question was generated this round".

    ⚠️ Why verification is mandatory first (from logs the user pasted on 2026-10-13): 3 consecutive
      rounds, all generated_new_task, 2 of them failed. The failure cause was **not** that the model
      can't write SQL, but that the question itself is unanswerable in the DB:
        · "which weapon is used most in each round" → JOIN `weapon_types` (no such table in the DB)
        · "average score rate per map" → uses `team_score_before / enemy_score_before` (columns don't exist)
      The old question bank had seven verification gates (`question_gen`), but **not a single one for
      runtime-generated questions** —— generated and run immediately, a failure is recorded → attempts+1
      → stale after 3 → fewer selectable nodes in the curriculum → generate new questions again. A vicious
      cycle that must be cut off at the source.

    ⚠️ Why id is hashed from the question (the second bug dug out of the same log):
      The old approach was `runtime_{n}` incrementing, but n was only counted in the **in-memory**
      `graph.topics` —— on restart it went back to `runtime_1`. So "ask about weapons" and
      "ask about average damage", two completely different questions, shared the same id, and their
      failure tallies piled onto each other (in practice `runtime_1` attempts=5, fails=5); a new question
      is born carrying the previous question's tally and goes stale instantly once it hits 3.
      Changed to `runtime_<md5(question)[:8]>`: same question → same id, different question → different id
      (same approach as the consumption side's `consume_<hash8>`).
    """
    import hashlib

    from voyager_vlml.agents.action import VLMLActionAgent
    from voyager_vlml.agents.critic import VLMLCriticAgent
    from voyager_vlml.llm import chat_json

    act = act or VLMLActionAgent(env=env)
    crit = VLMLCriticAgent()

    schema = ""
    try:
        schema = await env.schema_block()
    except Exception:
        schema = ""

    tried: list[str] = []
    for _ in range(max(1, int(max_tries))):
        out = chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a curriculum designer for Valorant esports data analysis. The current "
                        "database's topics have all been conquered or are temporarily unconquerable, so "
                        "**propose a new** topic that can still be answered with SQL from the database.\n"
                        "⚠️ Only propose topics for which the database **actually has data**: you may only "
                        "reference table and column names that appear in the schema below; do not invent "
                        "table/column names from memory (in practice we invented `weapon_types`, "
                        "`team_score_before` — they looked reasonable but don't exist in the DB).\n"
                        "Return only JSON: {\"name\": \"topic name (include the grain, e.g. round grain (rounds))\", "
                        " \"question\": \"a concrete question text\", \"skills\": [\"one or two data-retrieval points\"]}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Current DB schema:\n"
                        f"{schema}\n\n"
                        "Don't repeat existing topics.\n"
                        + ("The following question texts were just tried and fetched no data from the DB; "
                           "**do not** propose similar ones:\n- "
                           + "\n- ".join(tried) + "\n" if tried else "")
                        + "Return only JSON."
                    ),
                },
            ],
            temperature=0.7,
        )
        name = str(out.get("name") or "").strip() or "runtime new task"
        question = str(out.get("question") or "").strip()
        skills = [str(s) for s in (out.get("skills") or [])][:2]
        if not question:
            continue

        tid = "runtime_" + hashlib.md5(question.encode("utf-8")).hexdigest()[:8]
        if tid in curr.graph.topics:
            # The same question was generated before → reuse its node; don't re-verify or re-create it
            # (no SQL was written so there's no injection; give empty keys, don't falsely report a reuse)
            return tid, question, [], "", []

        # —— Verification runs as the real turn (same agent, same iteration count) ——
        # ⚠️ Don't filter questions with a stricter standard: it was initially written as `max_iters=1`
        #    (judge life-or-death after a single iteration), and 3 questions in a row all got filtered out
        #    ("learnable but wrong on the first try" questions were wrongly killed).
        #    Verification must use the same standard as the real run, otherwise it's suicidal filtering.
        #    And if it succeeds we reuse this turn's result directly, without burning another LLM + DB.
        # No data fetched → the question is invalid, try another; **don't record a failure** (it hasn't
        # entered the curriculum yet)
        #
        # ⚠️ We **also** retrieve the skill library here: the original retrieves on every rollout
        #    (`voyager.py:195`), whether the task came from the curriculum or was invented fresh.
        #    After the seed curriculum is exhausted, **everything** goes through this generation path ——
        #    in practice after progress 32, 4 consecutive rounds were all generated_new_task. If we don't
        #    retrieve here, the reuse chain never turns (all 28 skill-library entries go unused).
        gen_hits = _retrieve_skills(tid, question)
        gen_keys = [str(s.get("key") or "") for s in gen_hits]
        obs, sql = await act.solve(
            question, max_iters=max_iters, hints=skills[:3], skills=gen_hits)
        ok, _why = crit.check_task_success(
            observations=obs, task={"task": question, "hints": skills})
        if ok:
            node = {
                "id": tid,
                "name": name,
                "chapter": "runtime-generated",
                "depth": 9,          # ranked after the seeds
                "difficulty": 0.5,
                "skills": skills,
                "examples": [{"prompt": question}],
                "prerequisites": [],  # no prerequisites → immediately selectable
                "source": "practice",
            }
            curr.graph.topics[tid] = node
            _save_runtime_topic(node)   # persist: without persistence, restart would rebuild the same batch
            return tid, question, list(obs or []), str(sql or ""), gen_keys
        tried.append(question)
        print(f"  (new question failed verification, trying another: {question[:50]})")

    # Several consecutive attempts failed to generate a question fetchable from the DB —— return empty
    # honestly, don't force a question to run.
    # ⚠️ This turn's injection is already recorded in hits (mark_used is done in-place in _retrieve_skills),
    #    but there is **no** corresponding mark_result —— injected but didn't help, exactly the signal
    #    that should sink it to the bottom.
    return "", "", [], [], []


async def run_one_round(
    scope: dict[str, Any] | None = None,
    topic_id: str | None = None,
    use_llm: bool = True,
    max_iters: int = 4,
    curr: Any = None,
    env: Any = None,
) -> dict[str, Any]:
    """Run one round: milestone topic selection → LLM poses question → LLM writes SQL to fetch data → DB evidence criterion → rewrite the graph."""
    env = env or VLMLMCPEnv()
    if curr is None:
        curr = VLMLCurriculumAgent(scope=scope)
        n = _inject_runtime_topics(curr)
        if n:
            print(f"  (loaded {n} runtime nodes from last time, not rebuilding the same batch)")
    act = VLMLActionAgent(env=env)
    crit = VLMLCriticAgent()

    # ---- topic selection (milestone tree) ----
    generated = False
    gen_question = ""
    pre_obs: list[dict[str, Any]] = []
    pre_sql = ""
    # which learned recipes were injected during verification (brought back by _generate_new_task; don't double-count the hit)
    gen_keys: list[str] = []
    if topic_id:
        tid, reason = topic_id, "explicit"
    else:
        nxt = curr.propose_next_task()
        tid, reason = nxt["topic_id"], nxt["reason"]
        if not tid:
            # no conquerable task left in range → generate one **and verify it first** (design: the whole round doesn't end)
            tid, gen_question, pre_obs, pre_sql, gen_keys = await _generate_new_task(
                curr, env, act=act, max_iters=max_iters)
            reason, generated = "generated_new_task", True
            if not tid:
                # Several consecutive generated questions all fail to fetch data from the DB —— report
                # honestly, don't force a question to run just to fill the slot.
                # The old approach was "generate then run"; a failure records a failure, and failures feed
                # stale, which clogs the curriculum to death (in practice 3 rounds all generated_new_task,
                # 2 failed).
                print("  (consecutive generated questions all fail to fetch data; skip this round, record no failure)")
                # ⚠️ Fields must **fully align** with the normal return. Hit on 2026-10-13 server run:
                #   after exhausting the curriculum, consecutive generated questions all fail verification
                #   → fall into this early exit → the return is missing `progress`/`critique` →
                #   run_rounds crashes on print with `r['progress']` KeyError → **the entire non-stop
                #   loop dies on the spot**. In the non-stop loop, "spinning an empty round" is an in-design
                #   normal state (design: the whole round doesn't end), and it must never become a fatal path.
                return {
                    "topic_id": "", "reason": "generated_but_unverifiable",
                    "success": False, "question": "", "sql": "",
                    "skill_key": "", "generated": True, "observations": [],
                    "critique": "", "progress": curr.progress, "reused": 0,
                }

    topic = curr.graph.topics.get(tid) or {}

    # ---- question generation (LLM; question text only, no reference answer) ----
    # ⚠️ Runtime-new questions come **with the question text at generation time**, and have already been
    #    verified to fetch data from the DB, so we don't go through the question generator (doing so might
    #    swap the verified question for another un-fetchable phrasing).
    question = gen_question
    if not question and use_llm:
        try:
            question = await VLMLQuestionAgent(env=env).generate(topic) or ""
        except Exception as e:
            print(f"  (question generation failed: {type(e).__name__}: {e})")
    if not question:
        # fallback: use the topic's own example phrasing, to keep the chain runnable
        ex = topic.get("examples") or []
        question = (
            str(ex[0].get("prompt") or "") if ex and isinstance(ex[0], dict) else ""
        ) or f"Fetch one data point from the DB for the topic \"{topic.get('name') or tid}\"."

    # ---- write SQL + fetch data (LLM + VLML MCP, with iterative correction) ----
    #
    # Following the original Voyager's order (`voyager.py:195` → `:199` → `:370`):
    #     ① retrieve_skills **first** each round  ② concatenate into system prompt  ③ run  ④ add only on success
    # Previously the practice loop only did ④ (the `skill_store.add` below); ①②③ were all missing ——
    # so the skill library became a **one-way warehouse**: 25 real SQL entries written in, none ever used,
    # hits / ok stayed 0 (mark_used is only called after retrieve).
    #
    # ⚠️ Both paths that **actually write SQL** must retrieve (the original retrieves on every rollout):
    #    · the new-question generation path: already retrieved and injected during verification in
    #      `_generate_new_task`, keys brought back via `gen_keys`;
    #    · the on-the-spot writing path: retrieve here.
    #    **Don't retrieve on both sides** —— otherwise the same injection for the same question would be
    #    counted as a hit twice.
    injected_keys: list[str] = []

    # The verification turn already fetched data → reuse directly, don't burn another LLM + DB.
    if pre_obs:
        observations, sql = pre_obs, pre_sql
        injected_keys = gen_keys
    else:
        skill_hits = _retrieve_skills(tid, question)
        injected_keys = [str(s.get("key") or "") for s in skill_hits]
        observations, sql = await act.solve(
            question, max_iters=max_iters,
            hints=list(topic.get("skills") or [])[:3],
            skills=skill_hits,
        )

    # ---- judge success/failure: whether data was really fetched from the DB ----
    success, critique = crit.check_task_success(
        observations=observations,
        task={"task": question, "hints": list(topic.get("skills") or [])[:3]},
    )

    # ---- counting: did the injected recipes help this round? ----
    # Following the original: low-effectiveness skills naturally sink in the retrieve ranking
    # (`skill_store.py:273`'s scoring term `0.6*(ok/hits)`).
    # mark_result only records success; failure doesn't add ok —— so "hits rise, ok doesn't"
    # = injected but didn't help, exactly the signal that should surface.
    if injected_keys:
        try:
            import skill_store

            skill_store.mark_result(injected_keys, bool(success))
        except Exception:  # pragma: no cover
            pass

    # ---- store skill: reusable SQL, primary key = the graph question type topic_id ----
    skill_key = ""
    if success and sql:
        try:
            import skill_store

            _, skill_key = skill_store.add(
                tid,
                tid,  # primary key = the indexed graph question type (design)
                f"self-written SQL fetch succeeded: {question[:60]}",
                code=sql,
                blueprint={"source": "practice", "question": question},
                source="practice",
            )
        except Exception as e:  # pragma: no cover
            print(f"  (skill storage failed: {type(e).__name__}: {e})")

    curr.update_exploration_progress(
        {
            "topic_id": tid,
            "success": success,
            "verdict": "ok" if success else "no_evidence",
            "reason": "fetched from DB" if success else "not fetched from DB (pipeline/grain issue)",
            "skill_key": skill_key,
        }
    )

    # Append one run_log row, with fields aligned to the dashboard's reading (dashboard is deferred, but
    # keep data continuity first).
    # ⚠️ Don't fabricate coverage / score: that's VLML's scoring job; the new backend doesn't score, so
    #    honestly write None, and mark judge as db_evidence to distinguish.
    try:
        import run_log

        ok_calls = [o for o in observations if o.get("ok")]
        run_log.append(
            {
                "mode": "curriculum",
                "topic_id": tid,
                "task": tid,
                "question": question,
                "round": 1,
                "hint": "",
                "coverage": None,
                "score": None,
                "verdict": "ok" if success else "no_evidence",
                "success": success,
                "done": bool(success),
                "evidence_status": "collected" if success else "none",
                "judge": "db_evidence",
                "no_tool_calls": not ok_calls,
                "feedback": {},
                "tool_calls": len(observations),
                "errors": [
                    str(o.get("error") or "")
                    for o in observations
                    if not o.get("ok")
                ],
                "covered": [],
                "missing": [] if success else [question],
                "narrative": "",
                "generated_task": generated,
                "sql": sql,
                "skill_key": skill_key,
                # which learned recipes were injected this round (direct evidence of reuse; readable by dashboard/export)
                "reused_skills": injected_keys,
            }
        )
    except Exception as e:  # pragma: no cover
        print(f"  (run_log write failed: {type(e).__name__}: {e})")
    return {
        "topic_id": tid,
        "reason": reason,
        "generated": generated,
        "question": question,
        "sql": sql,
        "observations": observations,
        "success": success,
        "critique": critique,
        "skill_key": skill_key,
        "progress": curr.progress,
        # how many learned recipes were injected this round (direct evidence of reuse)
        "reused": len(injected_keys),
    }


async def run_rounds(
    n: int,
    *,
    scope: dict[str, Any] | None = None,
    use_llm: bool = True,
    max_iters: int = 4,
) -> list[dict[str, Any]]:
    """Run n rounds in a row (shares env and curriculum state).

    ⚠️ **`n <= 0` = non-stop** (isomorphic to the original Voyager's `while True`):
      runs until a human clicks "stop", or until `--max-iterations` is exhausted.
      The original has exactly this semantics —— `voyager_original/voyager/voyager.py:326`'s main loop
      **has no notion of "stop after N rounds"**, only an iteration cap (`--max-iterations`,
      default 160) and a stop signal (SIGTERM → exit after the current task finishes).

      Previously n here was a hard cap, and the dashboard hardcoded `rounds:3` → one launch only ran
      3 questions then exited, so the 29-node graph was never finished; what the human saw was
      "it ends after three rounds".
      That is not Voyager's proper behavior: **the learning loop is non-stopping**.

    Stopping is a **graceful stop**: on receiving the signal, finish the current question then exit
    (following the original "exiting learn loop after current task"), without a hard kill —— the skills
    are already persisted per task.
    """
    env = VLMLMCPEnv()
    curr = VLMLCurriculumAgent(scope=scope)
    # Load the runtime-generated nodes from last time back into the in-memory graph —— they are already
    # persisted and mostly marked completed; loading them back enables "no re-planning"; without loading,
    # every restart would regenerate and re-verify them.
    _n_inj = _inject_runtime_topics(curr)
    if _n_inj:
        print(f"(loaded {_n_inj} runtime-generated graph nodes from last time)")
    out: list[dict[str, Any]] = []
    i = 0
    # Consecutive "no evidence fetched" counter. **Observation only, no termination**: the design's
    # original wording is "when it keeps skipping and skill progress isn't rising, the whole round won't
    # end; only stopping changes the loop's pace".
    # So its only job is to write state into loop_state.json, letting the dashboard distinguish for humans
    # "it didn't learn" from "the pipeline never ran" (see _classify_no_evidence).
    streak = 0
    last_progress = curr.progress
    since_progress = 0
    while True:
        if n > 0 and i >= n:
            break
        if _STOP["requested"]:
            print("\n(received stop signal: finish the current question then stop, no hard kill)")
            break
        if n <= 0 and _MAX_ITERATIONS > 0 and i >= _MAX_ITERATIONS:
            print(f"\n(--max-iterations {_MAX_ITERATIONS} exhausted, stopping)")
            break
        r = await run_one_round(
            scope=scope,
            use_llm=use_llm,
            max_iters=max_iters,
            curr=curr,
            env=env,
        )
        r["round"] = i + 1
        out.append(r)
        i += 1

        # ---- write loop heartbeat (dashboard only reads this file, doesn't write) ----
        obs = r.get("observations") or []
        ok_calls = [o for o in obs if o.get("ok")]
        rows = sum(int(o.get("rows") or 0) for o in ok_calls)
        if r.get("success"):
            streak = 0
        else:
            streak += 1
        if int(r.get("progress") or 0) > last_progress:
            last_progress = int(r.get("progress") or 0)
            since_progress = 0
        else:
            since_progress += 1
        _write_loop_state(
            running=not _STOP["requested"],
            continuous=(n <= 0),
            round=i,
            topic=str(r.get("topic_id") or ""),
            reason=str(r.get("reason") or ""),
            generated=bool(r.get("generated")),
            success=bool(r.get("success")),
            progress=last_progress,
            tool_calls=len(obs),
            tool_ok=len(ok_calls),
            tool_fail=len(obs) - len(ok_calls),
            rows=rows,
            # how many learned recipes were injected this round = evidence the reuse chain is really turning
            reused=int(r.get("reused") or 0),
            last_error=next(
                (str(o.get("error") or "") for o in reversed(obs) if not o.get("ok")),
                ""),
            no_evidence_streak=streak,
            no_evidence_kind=(_classify_no_evidence(obs) if not r.get("success") else ""),
            rounds_since_progress=since_progress,
        )

        # Always use .get(): in the non-stop loop, "this round ran nothing" is an in-design normal state,
        # and any early-exit path missing a field must not kill the whole loop (hit KeyError crashing the
        # loop in practice).
        tag = f"round {i}/{n}" if n > 0 else f"round {i} (non-stop)"
        empty_round = not str(r.get("topic_id") or "")
        print(
            f"[{tag}] {r.get('topic_id') or '(no question run this round)'} · reason={r.get('reason')}"
            f"{' (newly generated)' if r.get('generated') else ''}"
            f" · success={r.get('success')} · progress={r.get('progress')}"
            f" · reused={r.get('reused') or 0} learned recipe(s)"
            + (f" · ⚠️ {streak} consecutive rounds with no evidence"
               f" ({_classify_no_evidence(obs)})"
               if (streak >= 3 and not empty_round) else "")
        )
    _write_loop_state(running=False, round=i, progress=last_progress)
    return out


def _main() -> int:
    ap = argparse.ArgumentParser(description="VLML-isomorphic learning loop · one round (self-generated question + data fetch)")
    ap.add_argument("--topic", default="", help="pin a specific topic (falls back to curriculum selection if omitted)")
    ap.add_argument("--no-llm", action="store_true", help="no question generation, no SQL writing (skeleton demo only)")
    ap.add_argument("--iters", type=int, default=4, help="iteration-correction rounds for SQL writing (aligned with original task_max_retries=4)")
    ap.add_argument("--rounds", type=int, default=1,
                    help="how many rounds to run; **0 = non-stop** (isomorphic to original while True, exits only on stop)")
    ap.add_argument("--max-iterations", type=int, default=0,
                    help="total question cap in non-stop mode (0 = unlimited; original --max-iterations defaults to 160)")
    ap.add_argument("--unstale", nargs="?", const="__all__", default="",
                    help="clear the 'gave up switching questions' mark and reset attempts (no value = clear all, value = clear that one)")
    a = ap.parse_args()

    # Non-stop mode must install a signal handler: the dashboard's "stop" sends SIGTERM,
    # and we want "finish the current question then exit", not a hard kill halfway.
    global _MAX_ITERATIONS
    if a.rounds == 0:
        _install_stop_handler()
        _MAX_ITERATIONS = max(0, int(a.max_iterations or 0))

    if a.unstale:
        # cleanup outlet when the curriculum is clogged by stale (in practice: 8 stale nodes make all 29 nodes unselectable)
        import topic_graph
        st = topic_graph.MasteryStore()
        tid = "" if a.unstale == "__all__" else str(a.unstale)
        touched = st.unstale(tid)
        print(f"cleared stale / attempts: {len(touched)} topic(s)"
              + (f" → {touched[:15]}" if touched else ""))
        return 0

    # ⚠️ `rounds=0` must also go through run_rounds (0 = non-stop); only `rounds==1` takes the
    #    single-round path. Previously it was written as `if a.rounds > 1`, so when the dashboard passed
    #    0 wanting "non-stop" it was treated as a single round that exits after running —— exactly the
    #    entry version of the "ends after three rounds" problem.
    if a.rounds != 1:
        rs = asyncio.run(
            run_rounds(a.rounds, use_llm=not a.no_llm, max_iters=a.iters)
        )
        ok = sum(1 for r in rs if r.get("success"))
        gen = sum(1 for r in rs if r.get("generated"))
        # "generated but failed verification" must be counted separately from "ran but failed": the former
        # is a **question-generation** problem (the question is unanswerable in the DB), the latter is **its**
        # problem. Mixing them into one number makes the issue impossible to locate.
        bad = sum(1 for r in rs
                  if r.get("reason") == "generated_but_unverifiable")
        why_stop = " (received stop signal, finish current question then exit)" if _STOP["requested"] else ""
        print("=== multi-round summary ===")
        print(f"rounds {len(rs)} · success {ok} · generated new tasks {gen}"
              f" (of which verification failed {bad}) · last-round progress "
              f"{rs[-1].get('progress') if rs else '(no round run)'}{why_stop}")
        for r in rs:
            print(f"  {r['round']}. {r['topic_id']} [{r['reason']}] success={r['success']}"
                  f" skill={r.get('skill_key') or '—'}")
            print(f"     question: {r.get('question')}")
            print(f"     SQL     : {(r.get('sql') or '').replace(chr(10), ' ')[:110]}")
        return 0

    r = asyncio.run(
        run_one_round(
            topic_id=a.topic or None, use_llm=not a.no_llm, max_iters=a.iters
        )
    )
    print("=== one-round loop (self-generated question + data fetch) ===")
    print("topic    :", r.get("topic_id"), "| reason:", r.get("reason"))
    print("question :", r.get("question"))
    print("SQL      :", r.get("sql"))
    for o in r.get("observations") or []:
        print("  obs     :", {k: o[k] for k in ("ok", "tool", "rows", "error")})
    print("success  :", r.get("success"), " (criterion = whether data was really fetched from the DB)")
    print("skill    :", r.get("skill_key") or "(not stored)")
    print("progress :", r.get("progress"), "milestones unlocked")
    if r.get("critique"):
        print("--- feedback (SQL language, no answer/coverage) ---")
        print(r["critique"])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
