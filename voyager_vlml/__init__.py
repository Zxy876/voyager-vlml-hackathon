"""voyager_vlml — the VLML isomorphic version of the original Voyager learning loop.

User instruction (2026-10-12): the current mve/ backend has too big a problem (its feedback skeleton is
built on "comparing against VLML's reference answers", which violates the design), so rebuild **from the
original Voyager**, following a five-step isomorphism:

  ① MC-initialization carried info → our VLML-isomorphic milestone        (agents/curriculum.py at this level)
  ② control_primitives JS code     → SQL primitives                        (control_primitives/)
  ③ install VLML MCP deps in the voyager env (DB / RIB / VLGG)            (env/)
  ④ iterative-revision prompt language → SQL                              (prompts/ + agents/critic.py)
  ⑤ dashboard deferred (this round only validates the backend loop)

Isomorphism mapping (original MC field → VLML):
  biome             → data source (rib / vlgg / DB)
  nearby_blocks     → visible tables (unlocked by progress tier)
  other_blocks      → other tables seen before
  nearby_entities   → fillable entity values (actual value range in the DB)
  inventory         → current entity anchor (series / team / map)
  chests            → learned skills (reusable SQL recipes)
  completed_tasks   → unlocked milestones (knowledge-graph node completed)
  failed_tasks      → failed / skipped (with reason)
  "Mine 1 wood log" → VLML seed milestone (trunk start, no prerequisites)
  —— added: milestone tree (seed / trunk / keystone) + random (table, column) as the minimal unit ——
"""

__all__ = ["agents"]
