# MVE — Nebius × NVIDIA Global AI Hackathon Submission

> **Track: Personal AI** — *"Build an always-on, private assistant that works
> for you while keeping your data under your control. Give it persistent
> memory, reusable skills, access to the tools and information you choose,
> and the ability to carry out tasks across your daily workflows."*
>
> MVE (Minimum Viable Egocentric) is exactly that, for **data work**: an agent
> that answers your questions against your own database, and — the part most
> demos skip — **learns a reusable skill for every question it answers**, with
> success verified against the database itself, not against another model's
> opinion.

## In one sentence

**Voyager's learning loop × database ground truth**: the agent writes its own
questions, writes its own SQL, and only counts a question as "learned" when it
actually retrieved data from the database; every learned recipe goes into a
skill library; the next time you ask, it reuses the recipe to answer you —
**the memory lives in the skill library, not in the model weights** (which is
precisely this track's "persistent memory, reusable skills").

## Why Personal AI, not another chatbot

| Personal AI track requirement | How MVE implements it |
|---|---|
| persistent memory | `mve/skill_store.json`: every skill = a verified SQL recipe + a one-line method description + hit/success counters. Survives restarts, reused across sessions |
| reusable skills | the retrieval key is the **task text** (isomorphic to the original Voyager `retrieve_skills(query=context)` — no topic_id routing layer); same-name skills get rewritten, not duplicated |
| access to the tools you choose | 10 MCP data tools (`query_sql` + `get_database_info` + 8 report tools); the agent itself decides which one to call |
| carry out tasks across workflows | two loops: the **practice loop** (self-quiz → write SQL → retrieve → consolidate skill) and the **consumption loop** (you ask one sentence → reuse skills → referee independently re-checks) |
| your data under your control | all data lives in a local DuckDB file; the LLM only ever generates SQL and **never receives the data**; switching data sources = one line in `db_config.json` |

## The real problem it solves (Potential Impact)

LLMs can *answer as if* they retrieved data without retrieving any: on an
empty table, an aggregate SQL still returns one row `(NULL, 0)`, and a
row-count criterion `rows >= 1` counts that as "data retrieved" — the bad
recipe gets frozen into a "skill", and its later reuse produces a **0% /
sample size 0** fake answer that looks perfectly complete (we hit and fixed
exactly this on 2026-10-14; full record in README chapter 21). MVE's critic
does not look at the model's attitude, only at the database:

> **Data retrieved ⇔ at least one tool call succeeded AND the result contains
> at least one real value.** All-NULL, or all-NULL-except-zeros, both count as
> "not retrieved".

Under this criterion, "learned" is a verifiable event, not a self-reported one.

## Learning from the original Voyager — and what we measured

The project is a faithful re-implementation of Voyager's learning loop against a
real database, so we went back to the original source
(`voyager_original/`, MineDojo/Voyager) and ported three engineering mechanisms
— while explicitly *not* porting its LLM-based success criterion (ours is
stricter: database ground truth, not model opinion):

| # | Mechanism | Original (line-referenced) | MVE implementation |
|---|---|---|---|
| 1 | 4-attempt retry budget per task | `action_agent_task_max_retries=4` (`voyager.py:30`), `done = iter >= 4 or success` (`:277-280`) | `solve(max_iters=4)` across practice, consumption and CLI; from attempt 3 the prompt mandates a *simplify-and-rewrite* (drop CTEs/nesting) |
| 2 | Explain before code | `action_template.txt`'s Explain→Plan→Code structure | Two-part output `Explain: … / SQL: …`; extraction is line-anchored on `SQL:` (Explain text can contain the word "selected") |
| 3 | Specific, name-calling critique | `critic.txt`'s few-shots teach the model to name exactly what's missing | `critic._suggest_names`: parses DuckDB Binder/Catalog errors for invented table/column names and suggests the closest real names from the live schema (difflib) — the deterministic isomorphic of few-shot specificity |

Two correctness bugs surfaced only through **10 consecutive runs of the same
question** (G2 Esports' round win rate after first blood on Split; expected
100%, n=7 — verified against the vendored rib.gg database):

1. **Empty tables had to leave the selectable list.** A prompt warning
   "0-row tables return nothing" was not enough — the model kept selecting
   `agg_first_blood_stats` (a table whose *name* is purpose-built for first
   blood) across 3 correction rounds. `schema_block` now moves 0-row tables
   into a separate forbidden list, so picking a dead table is structurally
   impossible. Same fix round: `max_cols` 14→20, because `first_kill` (column
   16 of `ext_player_round_stats`) was being truncated out of the schema.
2. **agreement=0 ≠ "the answer is unreliable".** When the two sides return
   different *shapes* (wide columns vs dimension/value rows), the comparison
   model mis-scored agreement as 0 and the narrator wrote "conclusion
   unreliable" — mistaking "failed to cross-check" for "wrong answer". Now
   only `voyager_sound=false` licenses the phrase "has flaws".

**Measured results (10 consecutive runs, Nemotron on Nebius Token Factory):**

| Metric | Result |
|---|---|
| Voyager side retrieved real (non-vacuous) data | **10/10** |
| Answer numerically correct (100% / n=7) | **10/10** |
| Referee's independent query succeeded | 7/10 |
| When the referee failed | Coverage left **empty** (never a faked 0) |
| Practice loop under Nemotron | 3/3 rounds succeeded in 25s; skills grow from zero |

**Skill retrieval is now vector-based, the way the original does it** — but
without the heavy dependency: embeddings run on Nebius itself
(`Qwen/Qwen3-Embedding-8B`, present in the live `/v1/models` catalogue), vectors
are stored beside each skill in JSON, and cosine similarity replaces lexical
matching (with automatic fallback to lexical when the embedding endpoint is
unavailable). The cross-topic threshold was **calibrated, not guessed**: 8
probes gave 0.66–0.70 for related rephrasings (including cross-language),
0.45–0.47 for same-domain different questions, 0.26–0.34 for unrelated text →
`VEC_CROSS_MIN_SIM = 0.40` (the midpoint of the measured 0.34–0.45 gap).

**An honest open question**: we have *not* yet measured "how many skills until
it can answer" as a number, and today's data says why — all 10 correct answers
came from **on-the-spot SQL writing**, not skill reuse (the library had no
first-blood recipe). The skill library's value shows up on questions where
on-the-spot writing fails but reuse saves the round. The experiment is designed
(clear library → human import → run k rounds → measure hit rate / reuse success
/ degradation per k); the number gets reported when it exists, not before.

## Significant updates during the submission period

The core prototype began on 2026-09-30 (inside the submission window that
opened 2026-08-26), evolving along the main development line at
<https://github.com/Zxy876/voyager-vlml-mve>. Everything below was built
between 2026-10-14 and 2026-10-30, during the submission period:

1. **LLM backend moved to Nebius Token Factory.** Every LLM call in the repo
   now funnels through a single egress, `voyager_vlml/llm.py`
   (OpenAI-compatible HTTP, zero SDK dependencies). With `NEBIUS_API_KEY`
   set, calls go to Token Factory + NVIDIA Nemotron; without a key, every
   call prints a loud warning — so a demo can never silently fall back to a
   non-Nebius backend (that would be a rule-violating submission).
2. **"Vacuum success" fix (criterion layer).** `critic.is_vacuous` /
   `has_real_data` tighten "data retrieved" from "tool ok AND rows >= 1" to
   "at least one real value". All five gates on the consumption loop
   (degraded criterion / facts / referee / method comparison / narrative /
   consolidation) and the referee observation repacking now only count real
   values. 22 unit tests (`test_vacuum.py`).
3. **Skill-store vacuum audit (`audit_skills.py`).** Every skill is re-run
   against the live database: 19 audited, 6 found vacuous, 4 replaced with
   verified SQL, 2 fixed for pattern/definition bugs. Run this script after
   every data-source switch.
4. **Same-task re-verification.** The recipe that used to answer 0% / n=0
   (falsely green) now answers **100% / n=7** — G2 Esports' round win rate
   after winning first blood on Split, rib.gg public match data — with an
   independent referee re-query and coverage 1.0.
5. **Three mechanisms ported from the original Voyager** (line-referenced in
   code comments): 4-attempt retry budget (`voyager.py:30`),
   Explain-before-SQL output (`action_template.txt`), and a deterministic
   name-calling critique that parses DuckDB errors and suggests real schema
   names — plus two correctness fixes those ports exposed (empty tables
   structurally excluded from the schema; `agreement=0` no longer narrated
   as "unreliable"). Measured on 10 consecutive runs: 10/10 real data.
6. **Vector-based skill retrieval** (Qwen3-Embedding-8B on Nebius, cosine
   similarity, threshold calibrated on 8 probes) — the original's Chroma
   approach without the heavyweight dependency.

## Setup — running on Nebius

```bash
# 0) dependencies (duckdb + httpx; everything else is stdlib; Python >= 3.10)
pip install -r requirements.txt

# 1) LLM backend = Nebius Token Factory + NVIDIA Nemotron
cp .env.example mve/.env      # fill in NEBIUS_API_KEY (create at studio.nebius.ai)
python scripts/check_llm.py   # smoke test: resolve endpoint + list models + real chat + JSON round-trip

# 2) the database is vendored in this repo (vlml/data/vlml_rib.duckdb,
#    built from rib.gg public match records). To rebuild from source instead:
python mve/vlml_source/ingest.py --event 2977 --max 8 --db vlml/data/vlml_rib.duckdb
python mve/knowledge_graph.py --build    # build the knowledge graph (optional; load() has a fingerprint guard)

# 3) run it
python mve/dashboard.py                  # dashboard at http://127.0.0.1:8777
python -m voyager_vlml.loop --rounds 5   # practice loop: self-quiz + SQL; the skill library starts growing
# consumption loop: in the dashboard's "Human Import" panel, ask one sentence, e.g.:
#   On the 'Split' map, after G2 Esports wins the first blood, what fraction
#   of rounds do they win on average? Include the sample size.
# Expected (verified against the vendored rib database): 100%, sample size 7;
# the referee independently re-queries; coverage 1.0
```

**Live demo (online instance on the Nebius backend)**:
`<DEMO_URL — filled in before submission>` — the dashboard has nine panels;
suggested tour: data-source check → Start Voyager → running → trajectory →
skills & causal timeline (watch the skill library grow from 0) → Human Import
same-task re-test.

## Tech stack & Nebius/NVIDIA usage

- **Single LLM egress** `voyager_vlml/llm.py`: OpenAI-compatible HTTP POST to
  `https://api.tokenfactory.nebius.com/v1/chat/completions`, Bearer
  `NEBIUS_API_KEY`, model `nvidia/nemotron-3-super-120b-a12b` (default;
  swap to `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` for cheaper/faster — note
  the exact capitalization, verified against `GET /v1/models`).
- **What the LLM does here (and only this)**: writes questions, writes SQL,
  generates critique guidance after vacuum detection, and narrates final
  answers. **The success/failure criterion never passes through the LLM** —
  it is decided by DuckDB ground truth (see above).
- **Where Token Factory accelerates the workflow**: the practice loop's
  up-to-3-iteration correction rounds and the consumption loop's independent
  referee re-query are all short-context, high-frequency, low-latency calls —
  the nano/super tier split maps exactly onto "fast everyday calls vs.
  serious reasoning".

## Honest limitations (worth knowing for judges)

1. The demo dataset is public Valorant match data (rib.gg). The engine is
   data-source agnostic (swap the DB in `db_config.json` to switch domains),
   but the **verified skills in this repo are specific to this dataset**.
2. Model capability ceiling, reported honestly: both Nemotron and GLM can
   spend 3 correction rounds still failing to fix a SQL column name (Binder
   Error persists). The system then **records the failure as a failure** —
   the criterion guarantees "failures are reported as failures"; it does not
   guarantee the model always writes correct SQL.
3. The dashboard is prototype-grade completeness, not product polish; code
   comments and README chapter 20 (debugging archive) record the full
   pitfall history.

## Repository map (for judges)

| Path | Contents |
|---|---|
| `voyager_vlml/loop.py` | practice loop: milestone topic → self-quiz → write SQL → criterion → consolidate skill |
| `voyager_vlml/consume.py` | consumption loop: one question → reuse skills → referee re-check → narrative answer |
| `voyager_vlml/agents/critic.py` | criterion layer: vacuum detection + SQL-language critique (fed back into iteration) |
| `voyager_vlml/llm.py` | **single LLM egress** (Nebius Token Factory first) |
| `mve/skill_store.py` | skill library (isomorphic to the original SkillManager: same-name rewriting) |
| `mve/dashboard.py` | dashboard (nine panels; the online demo entry) |
| `vlml/` | the VLML data layer (21 tables + 46 insight SQL + 10 MCP tools), vendored with its LICENSE |
| `test_vacuum.py` | criterion-layer unit tests (22 cases) |
| `audit_skills.py` | skill-store vacuum audit (run after every data-source switch) |
| `README.md` | full documentation (ch. 21 = original Voyager skill↔milestone provenance + this fix's full record; an English guide for judges sits at the top) |
