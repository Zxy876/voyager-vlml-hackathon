# MVE: An Agent That Learns From Your Own Database — and Proves It

**One-line pitch:** MVE (Minimum Viable Egocentric) answers questions against *your own* database and learns a reusable, verified skill for every question it answers — with success decided by database ground truth, never by another model's opinion.

---

## The inspiration

Every AI demo shows what a model can answer. Almost none shows **how the agent learned to answer it** — or whether that learning is real.

Our starting point was Voyager (Wang et al., 2023): the first LLM agent to learn skills automatically, in Minecraft. Voyager proposes its own curriculum, writes executable code, verifies it in the environment, and stores every success as a retrievable skill. Its deepest lesson: **the skill library is experience, not weights.** The model itself never changes; it gets better at completing tasks by carrying more verified experience.

We asked one question: *what if the environment isn't a game, but your own data?*

If an agent's "programs" are SQL queries over your database, then learning means accumulating **verified query recipes** — and success can be checked against ground truth instead of another model's opinion. That is MVE. Concretely: an agent that answers questions against a DuckDB database of real match data, learns a skill (question text + SQL + embedding) for every answer that checks out, and reuses those skills when a similar question arrives.

A second inspiration came from how humans actually learn. An adaptive-learning companion we built earlier keeps **practice** (scaffolded, hints allowed) strictly separate from **exams** (scaffolding removed). MVE keeps two separate logs for exactly the same reason: a practice run with knowledge-graph scaffolding tells you the scaffold works; only a naked exam tells you the real level. Collapsing the two is how systems lie to themselves — and most learning demos collapse them.

## What we learned

**1. Port mechanics, not vibes — verify with line numbers first.** Before changing anything, we read the original Voyager source. Its skills and milestones turned out to be *the same event recorded twice*, handshaking through task text (`retrieve_skills(query=self.context)`), with no ID-routing layer at all. That single finding killed our initial plan to build a topic-ID router and replaced it with a lightweight vector library — the same handshake, the same retrieval key, one less layer to maintain.

**2. When your success criterion is enumerable, "got data" must mean "got a real value."** This was the deepest bug we found, and it is a bug class, not a typo. A pure aggregate query on an empty table returns exactly one row:

$$\text{SELECT COUNT(*), \dots FROM } \varnothing \;\Rightarrow\; \text{1 row } (NULL, 0)$$

So a criterion of "the tool ran AND `rows ≥ min_rows`" is satisfied by a query that learned nothing — and the bad recipe gets固化 into the skill library, where the consumption path will faithfully reuse it. The original Voyager doesn't have this failure mode (its critic is an LLM judging "did the task get done"), but MVE's criterion is database ground truth — so the hole was ours to find and ours to fix. We rewrote the criterion as

$$\text{success} = \text{ok\_calls} \;\wedge\; \text{rows} \geq \text{min\_rows} \;\wedge\; \text{real}$$

where `real` demands at least one non-null, non-zero value — and `bool` must be checked *before* `int`, because in Python `bool` is a subclass of `int` and `True ≥ 1` passes silently.

**3. Retrieval thresholds are measurements, not settings.** We calibrated the skill-reuse threshold with 8 probes against the embedding model: relevant question/skill pairs scored **0.66–0.70**, same-domain-different-question **0.45–0.47**, fully unrelated **0.26–0.34**. The boundary band gave a defensible cut:

$$s(q,k) = \frac{\mathbf{q}\cdot\mathbf{k}}{\|\mathbf{q}\|\,\|\mathbf{k}\|}, \qquad \text{reuse iff } s(q,k) \geq 0.40$$

with a Jaccard fallback whenever the embedding endpoint is unreachable.

## How we built it

**Three loops, mapped one-to-one from Voyager onto a data environment:**

- **Curriculum** proposes a question from a four-layer knowledge graph (tool-tier → entity domain → topic domain → dimension node), seeded from VLML's own SQL catalog — 46 "insight" nodes declaring which tables each tool reads.
- **Action** writes SQL in the original's Explain → Plan → Code discipline, emitted as two labeled blocks, with up to 4 iterations (the original's `task_max_retries=4`) and a mandate to simplify from iteration 3 — because we watched a model burn three rounds failing to repair a broken CTE when a flat JOIN was correct all along.
- **Critic** is ground truth: execute against DuckDB, apply the criterion above. Success → the recipe is stored as a skill (question + SQL + embedding) in a lightweight vector library: Nebius `Qwen/Qwen3-Embedding-8B`, JSON-persisted vectors, cosine similarity.

**The consumption loop** — the part most demos skip — takes one sentence, retrieves the top skills by embedding, reuses when similarity clears the threshold, and writes from scratch when it doesn't. An independent **referee** then re-queries the same question from scratch; the two results are compared shape-agnostically, and the narration is allowed exactly three states: *mutually corroborated* / *same quantity, different value* / *could not corroborate — which is not the same as wrong*. Mid-project we caught ourselves wiring this loop to a fixed seed question for convenience, and rejected it: the whole point is *reuse what you've learned when a new problem shows up*, exactly like the original.

**Data:** real Valorant series from rib.gg, vendored into the repo (13 MB DuckDB, 9 tables). rib.gg gave us two axes the fallback source couldn't: per-round `first_kill` (first blood) and per-round buy tiers (economy) — precisely the axes the knowledge graph needed and lacked.

**Nebius:** a single LLM egress (`voyager_vlml/llm.py`, OpenAI-compatible), satisfying the hackathon's "runs on Nebius" requirement with runtime calls to Token Factory. Nemotron writes questions, SQL, and post-vacuum critique guidance; Qwen3-Embedding-8B powers retrieval. **The success/failure criterion never passes through the LLM.**

**Three ports from the original, each with source evidence:** the 4-retry budget, the Explain-first two-phase output, and a *name-calling* critic — parse the DuckDB error, run difflib against the live schema, and tell the model the real column name (`winner_team` → `winning_team_name`). We hit that exact error on deployment day; the port paid for itself immediately.

## The challenges

**The vacuum-success incident.** The full causal chain, as it actually happened: the graph had no `ext_*` axes → the first-blood recipe was never learned → retrieval missed → the agent wrote SQL from scratch → it chose a 0-row aggregate table → got the one `(NULL, 0)` row → the row-count criterion passed → the wrong answer was *stored as a new skill* → the consumption path reused it and answered **0%, sample size 0** when the truth was **100%, n = 7**. The fix touched the critic (`is_vacuous` / `has_real_data`), five gates along the consumption path, and a re-packing step that had been laundering a vacuum call into a "successful observation". One test file, 22 assertions, all green.

**Structural traps beat prompt warnings.** The model kept selecting `agg_first_blood_stats` (0 rows) no matter how the prompt warned it. We stopped warning and made it structural: empty tables moved out of the optional list into an explicit forbidden list. The same audit found `max_cols: 14` silently truncating the schema — `first_kill` is column 16, so the model *could not see* the first-blood column at all. Raised to 20.

**A length-scored matcher lied to us.** The topic matcher scored hints by string length, so "赢下多少" (4 chars) dirty-hit `series_totals` and suppressed "首血" (2 chars) — the second collision of the same class after an earlier one. Fixed by deleting generic phrases and adding longer, specific hints.

**A shape mismatch was misread as a wrong answer.** The referee returned dimension/value rows; the learning loop returned wide columns; the agreement score computed 0, and our narration escalated "could not corroborate" into "the conclusion is unreliable." Fixed on both sides: a shape-agnostic comparison prompt, and a three-state narration where only a hard defect earns the word "unreliable."

**The limit we refuse to paper over.** Our 10-run benchmark: the agent produced real data **10/10** and numerically correct answers **10/10**, with the independent referee at **7/10** (its failures honestly left empty, never filled in). But every one of those answers was written *on the spot* — the library held no first-blood recipe to retrieve. So the natural next question — *how many skills until the agent answers by retrieval?* — has no number yet. We documented the experiment design (clear the library → import one question → run k rounds → measure hit / reuse / degrade) and left the number open rather than invent it.

## What's live

- **Repo:** https://github.com/Zxy876/voyager-vlml-hackathon — self-contained; a fresh clone reproduces the flagship result (**100%, sample size 7** for the first-blood question) with nothing but `pip install duckdb httpx`.
- **Live demo (Nebius backend):** https://43.161.203.50:8443/mve-hackathon/ — nine panels. Suggested tour: data-source check → Start Voyager → watch the trajectory → skills & causal timeline (the library grows from 0) → Human Import a same-task re-test.

The bet: **Personal AI shouldn't mean a chatbot with your name in the system prompt.** It should mean an agent that accumulates verified experience against *your* data — and says "I don't know yet" in the same units it uses to say "here's the answer."
