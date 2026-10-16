#!/usr/bin/env python3
"""MVE dashboard: view Voyager's progress curve and start or stop it with one click.

Usage:
    python mve/dashboard.py            # Then open http://127.0.0.1:8777
    python mve/dashboard.py --port 9000

Design source (important):
- The VLML repository contains no frontend files (no html/tsx/jsx/vue). Its "dashboard" is the LLM chat UI,
  so there is no UI to reuse. This dashboard is therefore based on the study companion's static/ frontend:
    · Layout: page-shell → hero (title and status on the left / summary-pills and controls on the right)
              → workspace-nav (six tabs) → workspace-stage (**only one dashboard is shown at a time**)
    · Visuals: white paper-like cards with soft shadows and 8px corners; selected items use the brand-color
      background with a 3px bottom indicator
    · Color scale: directly reuse the study companion's five mastery levels (weak/progress/good/mastered/new)
  Data comes from MVE's own run_log.jsonl / causal_timeline.jsonl / pilot_state.json.

Why tabs are used: the previous version stacked 12 sections in one long page, making it too dense and
forcing users to find "what to check now" themselves. The study companion shows **one workspace at a time**.

The dashboard makes no inferences. It only shows what actually happened.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import panel_data  # noqa: E402
import pilot  # noqa: E402

ROOT = Path(__file__).resolve().parent


# --- Knowledge-graph self-check: avoid "not built" after git pull / DB swap ---
import threading as _threading

_graph_build_lock = _threading.RLock()


def _do_build_graph():
    """Rebuild and persist the graph while holding _graph_build_lock (no own lock)."""
    import knowledge_graph as kg
    # build()'s `from tasks import TASKS` needs the repo root; the dashboard only
    # injects mve/ onto sys.path.
    _repo_root = str(ROOT.parent)
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    g = kg.build()
    g.save()
    return g


def _ensure_knowledge_graph(verbose: bool = True) -> bool:
    """Startup self-check: rebuild the graph if it is missing or stale.

    Staleness = schema_version + task fingerprint + modelling-doc fingerprint
    (reuses knowledge_graph._stale) + the default duckdb being newer than the graph
    (DB swap). So the gitignored json dropped by `git pull`, and a stale graph after
    swapping the DB, both rebuild automatically - no manual `--build` needed.
    A failed rebuild never blocks panel startup.
    """
    try:
        import knowledge_graph as kg
    except Exception as e:
        if verbose:
            print(f"[graph self-check] cannot import knowledge_graph: {type(e).__name__}: {e}")
        return False

    gp = kg.GRAPH_PATH
    need, reason = True, "unknown"
    if gp.exists() and gp.stat().st_size > 0:
        try:
            raw = json.loads(gp.read_text(encoding="utf-8", errors="replace"))
            need = kg._stale(raw)
            reason = "schema/task/model-doc fingerprint stale" if need else ""
            if not need:
                # DB-swap detection: default duckdb newer than graph -> stale
                db = ROOT.parent / "vlml" / "data" / "vlml_rib.duckdb"
                if db.exists() and db.stat().st_mtime > gp.stat().st_mtime:
                    need, reason = True, f"database updated ({db.name})"
        except Exception as e:
            need, reason = True, f"graph read failed: {type(e).__name__}"

    if not need:
        if verbose:
            print(f"[graph self-check] graph already up to date ({gp.name})")
        return True

    if verbose:
        print(f"[graph self-check] {reason}, auto-rebuilding graph…")
    try:
        with _graph_build_lock:
            g = _do_build_graph()
        s = g.to_dict()["summary"]
        if verbose:
            print(f"[graph self-check] rebuild done: dimensions {s['dimensions']} · "
                  f"tools {s['tools']} · tables {s['tables']} · edges {s['edges']}")
        return True
    except Exception as e:
        if verbose:
            print(f"[graph self-check] rebuild failed (panel still starts, /api/graph "
                  f"falls back to prompt): {type(e).__name__}: {e}")
        return gp.exists()


def _rebuild_graph_if_missing() -> bool:
    """Hot-path fallback: rebuild only if the graph is genuinely missing/empty
    (e.g. deleted at runtime by a baseline exam's library wipe)."""
    try:
        import knowledge_graph as kg
    except Exception:
        return False
    gp = kg.GRAPH_PATH
    try:
        if gp.exists() and gp.stat().st_size > 0 and kg.KnowledgeGraph.load().nodes:
            return True
    except Exception:
        pass
    with _graph_build_lock:
        try:
            if gp.exists() and gp.stat().st_size > 0 and kg.KnowledgeGraph.load().nodes:
                return True
        except Exception:
            pass
        try:
            _do_build_graph()
            return True
        except Exception as e:
            print(f"[graph self-check] fallback rebuild failed: {type(e).__name__}: {e}")
            return False


def _graph_json() -> str:
    """Knowledge graph: read the stored JSON without importing vlml_env (keeps the dashboard lightweight and fast).

    Follow the study companion's `graph_read_model.py` rule: return structure only, with no inference at this layer.
    If no knowledge graph has been built, return an empty marker so the frontend can prompt users to run `--build` first.
    """
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception as e:
        return json.dumps({"empty": True, "error": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False)
    if not g.nodes:
        # Startup self-check normally already rebuilt it; this is the fallback
        # (e.g. deleted at runtime by a baseline exam's library wipe).
        if _rebuild_graph_if_missing():
            try:
                g = knowledge_graph.KnowledgeGraph.load()
            except Exception:
                pass
    if not g.nodes:
        return json.dumps({"empty": True, "error": "Knowledge graph not built. Run python mve/knowledge_graph.py --build first"},
                          ensure_ascii=False)
    out = g.to_dict()
    # Pending review: new dimension names introduced by runtime questions do **not create nodes**
    # (following the study companion: knowledge points created at runtime do not enter the authoritative layer).
    # Return them separately so the frontend can show a notice at the top of the knowledge graph page.
    try:
        import knowledge_graph
        out["pending_dims"] = knowledge_graph.load_pending_dimensions()
    except Exception:
        out["pending_dims"] = []
    # Subject registry: user-defined ownership containers (enter events/teams after switching data sources,
    # then assign generated entries to them)
    try:
        import subjects
        subs = subjects.all_subjects()
        out["subjects"] = subs
        out["subject_counts"] = {s["id"]: len(subjects.entries_of(s["id"]))
                                 for s in subs}
        out["subject_unassigned"] = subjects.unassigned()
        out["data_source"] = subjects.data_source()
    except Exception:
        out["subjects"] = []
        out["subject_unassigned"] = []
    return json.dumps(out, ensure_ascii=False)

def _scope_json() -> str:
    """Current practice scope (read-only). Mirrors the study companion's study_get_practice_scope."""
    try:
        import practice_scope
        return json.dumps({"ok": True, "scope": practice_scope.get_scope()},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False)


# Bind to loopback by default: the dashboard has no authentication, so binding to 0.0.0.0 would expose
# process start/stop endpoints to the public internet. For remote access on a server, explicitly pass
# --host and configure the security group to allow only your IP.
PORT = int(os.environ.get("MVE_PANEL_PORT") or 8777)
HOST = os.environ.get("MVE_PANEL_HOST") or "127.0.0.1"
# Used when mounted under a reverse-proxy subpath (for example, https://host:8443/mve/):
# the server strips the prefix, and every frontend fetch must include it to avoid hitting the proxy root.
PREFIX = os.environ.get("MVE_PANEL_PREFIX") or ""

for _i, _a in enumerate(sys.argv[1:]):
    if _a == "--port" and _i + 2 <= len(sys.argv):
        PORT = int(sys.argv[_i + 2])
    if _a == "--host" and _i + 2 <= len(sys.argv):
        HOST = sys.argv[_i + 2]
    if _a == "--prefix" and _i + 2 <= len(sys.argv):
        PREFIX = "/" + sys.argv[_i + 2].strip("/")
def _strip_prefix(path: str) -> str:
    """Strip the reverse-proxy subpath prefix (/mve/api/state → /api/state).

    When a prefix is configured, requests without it map to an empty path (and then 404), preventing
    direct access that bypasses the proxy prefix.
    """
    if PREFIX:
        if path == PREFIX:
            return "/"
        return path[len(PREFIX):] if path.startswith(PREFIX + "/") else ""
    return path


def _html() -> str:
    """Inject the prefix: frontend fetch('/api/...') calls must include it or they hit the proxy root."""
    if not PREFIX:
        return HTML
    return (HTML.replace("<head>", '<head>\n'
                         f'<script>window.MVE_PREFIX="{PREFIX}";</script>', 1)
                .replace("fetch('", "fetch(window.MVE_PREFIX+'"))


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MVE Dashboard · Voyager Progress Curve</title>
<style>
:root{
  color-scheme: light;
  font-family:"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  --bg:#f3f7f1;
  --paper:rgba(253,255,250,.94);
  --paper-strong:rgba(255,255,255,.98);
  --ink:#1f2924;
  --muted:#607168;
  --line:rgba(31,41,36,.13);
  --brand:#2f7d57;
  --brand-strong:#17563d;
  --accent:#d58b2b;
  --accent-strong:#8a5317;
  --warning:#b7791f;
  --warning-bg:rgba(183,121,31,.10);
  /* Five mastery colors from the study companion */
  --mastery-new:#cbd5d0;
  --mastery-weak:#e89a90;
  --mastery-progress:#e2b85a;
  --mastery-good:#9bd9b8;
  --mastery-mastered:#82d99e;
  --shadow:0 10px 24px rgba(31,52,40,.07);
  --radius:8px;
  --radius-sm:6px;
  background:var(--bg);
  color:var(--ink);
}
*{box-sizing:border-box}
body{
  margin:0;min-height:100vh;
  background:
    linear-gradient(90deg,rgba(47,125,87,.045) 1px,transparent 1px),
    linear-gradient(rgba(47,125,87,.04) 1px,transparent 1px),
    linear-gradient(180deg,#f8fbf6 0%,#eef5f2 100%);
  background-size:36px 36px,36px 36px,auto;
}
h1,h2,p{margin:0}
button,textarea,select{font:inherit}

/* ---------- Layout ---------- */
.page-shell{width:min(1240px,calc(100vw - 24px));min-width:0;margin:0 auto;padding:28px 0 36px}
@media(max-width:980px){.page-shell{width:calc(100vw - 24px);padding:20px 0 28px}}
.main-view{display:grid;gap:18px}

/* ---------- hero ---------- */
.hero{
  position:relative;display:grid;gap:24px;align-items:center;
  grid-template-columns:minmax(300px,.9fr) minmax(520px,1.3fr);
  min-height:166px;padding:24px 24px 24px 28px;
  border:1px solid rgba(47,125,87,.20);
  border-left:6px solid rgba(47,125,87,.72);
  border-radius:var(--radius);
  background:linear-gradient(135deg,rgba(255,255,255,.96),rgba(244,249,241,.86)),var(--paper);
  box-shadow:var(--shadow);overflow:hidden;
}
@media(max-width:980px){.hero{grid-template-columns:1fr;min-height:0}}
.hero__copy,.hero__controls{position:relative;z-index:1}
.hero__copy{display:grid;gap:10px}
.hero__eyebrow{color:var(--brand-strong);font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}
.hero-title{display:inline-flex;align-items:center;gap:8px;font-size:26px;line-height:1.15}
.hero-title__cat{display:inline-grid;place-items:center;width:32px;height:32px;
  border:1px solid rgba(47,125,87,.20);border-radius:var(--radius);
  background:rgba(47,125,87,.10);font-size:18px;line-height:1}
.hero__status{color:var(--muted);font-size:14px}
.hero__controls{display:grid;gap:14px;justify-items:end}
.summary-pills{display:flex;gap:8px;align-items:center;justify-content:flex-end;
  width:100%;flex-wrap:wrap}
.summary-pill{display:grid;gap:3px;min-width:132px;max-width:230px;padding:9px 12px;
  border:1px solid rgba(47,125,87,.16);border-left:3px solid rgba(47,125,87,.48);
  border-radius:var(--radius-sm);background:rgba(253,255,250,.86);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.92)}
.summary-pill span{color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase}
.summary-pill strong{min-width:0;color:var(--ink);font-size:14px;line-height:1.35;overflow-wrap:anywhere}

/* ---------- Cockpit (Start / Stop) ---------- */
.runctl{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:2px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 16px;border-radius:var(--radius-sm);
  border:1px solid transparent;cursor:pointer;font-size:13.5px;font-weight:600;
  transition:transform 150ms ease,background 150ms ease,border-color 150ms ease}
.btn:hover{transform:translateY(-1px)}
.btn:disabled{opacity:.55;cursor:not-allowed;transform:none}
.btn-primary{background:var(--brand);color:#fff;box-shadow:0 1px 2px rgba(31,52,40,.12)}
.btn-primary:hover{background:var(--brand-strong)}
.btn-danger{background:#c0392b;color:#fff}
.btn-danger:hover{background:#96281a}
.btn-secondary{background:#fff;color:#374151;border-color:var(--line)}
.btn-secondary:hover{border-color:rgba(47,125,87,.42);background:rgba(237,248,240,.94)}
.runctl select{padding:8px 10px;border:1px solid var(--line);border-radius:var(--radius-sm);
  background:#fff;color:var(--ink);font-size:13px}
.runctl .lbl{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase}
.runstate{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mastery-new);display:inline-block}
.dot.on{background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.18);animation:pulse 1.6s ease-in-out infinite}
.dot.off{background:#9ca3af}
.dot.warn{background:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,.18);animation:pulse 1.6s ease-in-out infinite}
/* Practice scope bar: set the scope on the knowledge graph page; display and clear it here */
.scopebar{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:8px;
  padding:7px 10px;border:1px dashed var(--line);border-radius:var(--radius-sm);background:rgba(255,255,255,.55)}
.scopebar .lbl{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase}
.scopebar .hint{font-size:12px;color:var(--muted)}
.scope-act{display:inline-flex;gap:6px;align-items:center}
.tbl tr.now td{background:#eef8f3}
@keyframes pulse{50%{box-shadow:0 0 0 6px rgba(34,197,94,.06)}}

/* ---------- Workspace Tabs ---------- */
.workspace-nav{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;min-width:0;
  padding:10px;border:1px solid rgba(31,41,36,.12);border-radius:var(--radius);
  background:rgba(255,255,255,.88);box-shadow:var(--shadow)}
@media(max-width:980px){.workspace-nav{grid-template-columns:repeat(3,minmax(0,1fr))}}
.workspace-tab{position:relative;display:grid;grid-template-columns:auto minmax(0,1fr);
  grid-template-rows:auto auto;gap:5px 9px;align-items:center;min-width:0;min-height:68px;
  padding:11px 12px;color:var(--ink);text-align:left;overflow:hidden;
  border:1px solid rgba(31,41,36,.11);border-radius:var(--radius-sm);
  background:rgba(250,252,249,.94);box-shadow:0 1px 2px rgba(31,52,40,.04);
  cursor:pointer;transition:transform 150ms ease,border-color 150ms ease,background 150ms ease}
.workspace-tab::after{content:"";position:absolute;inset:auto 10px 0;height:3px;border-radius:999px 999px 0 0;
  background:var(--brand);opacity:0;transform:scaleX(.55);transition:opacity 150ms ease,transform 150ms ease}
.workspace-tab:hover{border-color:rgba(47,125,87,.30);background:#fff;transform:translateY(-2px)}
.workspace-tab[aria-selected="true"]{color:var(--brand-strong);
  border-color:rgba(47,125,87,.38);background:rgba(237,248,240,.92)}
.workspace-tab[aria-selected="true"]::after{opacity:1;transform:scaleX(1)}
.workspace-tab__icon{grid-row:1/-1;display:grid;place-items:center;width:34px;height:34px;
  color:var(--brand-strong);border:1px solid rgba(47,125,87,.20);border-radius:10px;
  background:rgba(237,248,240,.92);font-size:17px}
.workspace-tab__label{min-width:0;font-size:13.5px;font-weight:700;line-height:1.25}
.tab-en{display:block;font-size:10px;font-weight:400;opacity:.62;letter-spacing:.02em}
.workspace-tab__status{min-width:0;font-size:11.5px;color:var(--muted);line-height:1.35;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ---------- Dashboard ---------- */
.workspace-stage{display:grid;gap:16px}
.panel{border:1px solid var(--line);border-radius:var(--radius);background:var(--paper-strong);
  box-shadow:var(--shadow);padding:18px 20px}
.panel__head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:12px;
  padding-bottom:10px;border-bottom:1px solid var(--line)}
.panel__head h2{font-size:16px;line-height:1.3}
.panel__head .hint{font-size:12px;color:var(--muted)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:980px){.grid2{grid-template-columns:1fr}}

h3{margin:16px 0 8px;font-size:13px;color:var(--muted);font-weight:700;
   text-transform:uppercase;letter-spacing:.04em}
h3:first-child{margin-top:0}
.kpis{display:flex;gap:26px;flex-wrap:wrap}
.kpi .label{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase}
.kpi .value{font-size:26px;font-weight:600;line-height:1.2}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
      background:rgba(47,125,87,.12);color:var(--brand-strong);margin-right:6px}
.chip.warn{background:var(--warning-bg);color:#784910}
.chip.bad{background:rgba(192,57,43,.12);color:#8a2015}
.chip.ok{background:rgba(130,217,158,.28);color:#14532d}
.chip.grey{background:rgba(31,41,36,.07);color:#4b5563}
.chip.m-weak{background:var(--mastery-weak);color:#7a2318}
.chip.m-progress{background:var(--mastery-progress);color:#6b4308}
.chip.m-good{background:var(--mastery-good);color:#14532d}
.chip.m-mastered{background:var(--mastery-mastered);color:#0f4a2a}
.chip.m-new{background:var(--mastery-new);color:#3f4a44}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:700;font-size:12px}
td.num{font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.note{border:1px solid #fde68a;border-radius:var(--radius-sm);padding:10px 12px;
      font-size:12.5px;color:#78350f;background:#fffbeb;margin-top:10px}
.note.info{border-color:rgba(47,125,87,.28);background:rgba(237,248,240,.92);color:#17563d}
.note.bad{border-color:rgba(220,38,38,.3);background:#fef2f2;color:#7f1d1d}
ul{margin:6px 0 0;padding-left:20px}
li{margin:3px 0}
.tool{display:inline-block;background:rgba(31,41,36,.05);border:1px solid var(--line);
      border-radius:5px;padding:1px 7px;margin:2px 3px 2px 0;font-size:12px}
.tool.new{background:rgba(130,217,158,.28);border-color:#86efac;color:#14532d}
.legend{font-size:12px;color:var(--muted);margin-top:6px}
.nocmp{border:1px dashed #9ca3af;border-radius:var(--radius-sm);padding:12px 14px;
       margin-top:10px;background:#fafafa}
.nocmp-tag{display:inline-block;font-size:11px;color:#6b7280;border:1px dashed #9ca3af;
           border-radius:4px;padding:1px 7px;margin-bottom:7px}
.nocmp-body{font-size:13.5px;line-height:1.75}
.nocmp-sub{font-size:12px;color:var(--muted);margin-top:7px}
.nocmp-sub ul{margin:4px 0 0}
.tasks{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px}
@media(max-width:980px){.tasks{grid-template-columns:1fr}}
.taskcard{border:1px solid var(--line);border-radius:var(--radius-sm);padding:11px 13px;background:#fcfcfd}
.taskcard.now{border-color:rgba(47,125,87,.42);background:rgba(237,248,240,.72)}
.taskcard .th{font-size:13px;margin-bottom:6px;line-height:1.45}
.taskcard .tm{font-size:12px;color:var(--muted);margin-top:4px}
.recmd{border:1px solid var(--line);border-radius:var(--radius-sm);padding:11px 13px;background:#fff;margin-top:10px}
.recmd.now{border-color:rgba(47,125,87,.42);background:rgba(237,248,240,.72)}
.recmd .rq{font-size:13.5px;line-height:1.5;margin:5px 0}
.recmd .rw{font-size:12.5px;color:#374151;margin-top:5px}
.changed{display:inline-block;font-size:11px;background:var(--warning-bg);color:#784910;
         border-radius:4px;padding:1px 7px;margin-left:6px}
.timeline{font-size:12.5px}
.timeline .tl{display:flex;gap:9px;padding:5px 0;border-bottom:1px solid rgba(31,41,36,.07)}
.timeline .tl:last-child{border-bottom:0}
.timeline .seq{color:#9ca3af;font-variant-numeric:tabular-nums;min-width:28px}
.timeline .kd{min-width:78px}
.timeline .ts{color:var(--muted);margin-left:auto;font-size:11.5px;white-space:nowrap}
.imp{border:1px solid rgba(47,125,87,.28);background:rgba(237,248,240,.62);
     border-radius:var(--radius);padding:14px 16px}
.imp textarea{width:100%;min-height:64px;resize:vertical;padding:9px 11px;
  border:1px solid var(--line);border-radius:var(--radius-sm);font:13.5px/1.6 inherit;
  color:var(--ink);background:#fff;outline:none}
.imp textarea:focus{border-color:var(--brand)}
.imp select{padding:7px 10px;border:1px solid var(--line);border-radius:var(--radius-sm);
  font:13px inherit;background:#fff;color:var(--ink);max-width:360px}
.imp-row{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;margin-top:10px}
.hit{font-size:12px;color:var(--muted);margin-top:8px;min-height:18px}
.hit b{color:var(--brand-strong)}
.hit.no b{color:#8a2015}
.fact{border-left:3px solid rgba(47,125,87,.38);padding-left:10px;margin:8px 0;font-size:13px}
.fact .fv{font-weight:600}
.spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(47,125,87,.24);
      border-top-color:var(--brand);border-radius:50%;animation:sp .7s linear infinite;
      vertical-align:-1px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
.console{background:#12211a;color:#d7e6dc;border-radius:var(--radius-sm);padding:12px 14px;
  font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;
  max-height:460px;overflow:auto;margin-top:10px}
.console .dim{color:#7f9a8c}
/* Continuous switch: matches the original Voyager's while True loop. The learning loop does not stop
   until the user clicks Stop or the limit is reached. The dashboard previously hard-coded rounds:3,
   so each run exited after only three questions and could never cover all 29 knowledge graph nodes.
   This switch makes the number of questions an explicit user choice. */
.runctl-ck{display:inline-flex;align-items:center;gap:5px;font-size:12px;
  color:#5c6f66;cursor:pointer;user-select:none}
.runctl-ck input{accent-color:var(--brand);cursor:pointer;vertical-align:-1px}
.runctl-ck.off{opacity:.55;cursor:not-allowed}
</style>
</head>
<body>
<div class="page-shell">
  <div class="main-view">
    <header class="hero">
      <div class="hero__copy">
        <p class="hero__eyebrow">MVE · Voyager × VLML</p>
        <h1 class="hero-title"><span class="hero-title__cat">🛰</span>Voyager Progress Curve</h1>
        <p class="hero__status" id="statusLine">Loading…</p>
        <div class="runctl">
          <span class="lbl">Cockpit</span>
          <button class="btn btn-primary" id="runStart">▶ Start Voyager</button>
          <button class="btn btn-danger" id="runStop" disabled>■ Stop</button>
          <button class="btn btn-secondary" id="btnExport" title="Export the current learning process as Markdown">⬇ Export MD</button>
          <button class="btn btn-secondary" id="mcLearn" title="Run MC strategy learning first (extract domain-agnostic planning patterns), store the results, then automatically start the standard practice loop (the full MVE flow)">🧠 Start MC Strategy Learning → MVE</button>
          <label class="runctl-ck" id="ckWrap"
                 title="Matches the original Voyager (the main loop at voyager.py:326 is while True): the learning loop does not stop until you click Stop or reach the limit. Checked = continuous; unchecked = run one round for review.">
            <input type="checkbox" id="ckContinuous" checked>
            <span>Continuous (stops after the graph is complete or Stop is clicked)</span>
          </label>
          <span class="runstate"><i class="dot off" id="runDot"></i><span id="runState">Not Running</span></span>
        </div>
        <!-- Practice scope: study companion onboarding.md:82. Users select a knowledge point on the knowledge
             graph to set the scope, and the question generator stays within it. This removes the need for
             controls such as "select difficulty / select question," leaving only Start / Stop in the cockpit. -->
        <div class="scopebar" id="scopeBar">
          <span class="lbl">Practice Scope</span>
          <span id="scopeChip"><span class="chip grey">Not Set · Question Generator Chooses</span></span>
          <span class="hint" id="scopeHint">Open Knowledge Graph and click Practice This Knowledge Point on a dimension</span>
        </div>
      </div>
      <div class="hero__controls">
        <div class="summary-pills" id="pills"></div>
      </div>
    </header>

    <nav class="workspace-nav" id="nav"></nav>
    <div class="workspace-stage" id="stage"><div class="empty">Loading…</div></div>
  </div>
</div>

<script>
const ESC = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const TABS = [
  {id:'overview', icon:'📈', label:'Overview', en:'Overview',
   // Once the learning curve is connected to the consumption capability side, this tab directly reports
   // the cumulative number of solvable questions (the original metric). With no consumption records,
   // it lists the page contents instead of showing "mastery xx%" (the ungraded practice loop keeps it empty).
   status:s=>{const cc=s.consume_curve||[];
     return cc.length?('Can Solve '+(cc[cc.length-1].mastered_total||0)+' Questions'):'Criteria / Learning Curve';}},
  {id:'practice', icon:'🎯', label:'Practice', en:'Practice', status:s=>s.topic_id||'—'},
  {id:'import',   icon:'✍️', label:'Human Import', en:'Human Import', status:s=>(s.imports||[]).length+' Imports'},
  {id:'trace',    icon:'🧭', label:'Trajectory', en:'Trajectory', status:s=>s.total_rounds+' Rounds'},
  {id:'skill',    icon:'🧠', label:'Skills · Causal', en:'Skills & Causal', status:s=>(s.skill_stats&&s.skill_stats.skills||0)+' Skills'},
  {id:'graph',    icon:'🕸', label:'Knowledge Graph', en:'Knowledge Graph',
   status:s=>s.practice_scope&&s.practice_scope.active?('Scope: '+s.practice_scope.label):'Dimension → Tool'},
  {id:'voyager', icon:'🤖', label:'MC Real Learning', en:'MC Real Learning',
   status:s=>(s.voyager&&s.voyager.running)?'Learning':((s.voyager&&s.voyager.completed_count)?('Learned '+(s.voyager.completed_count)+' Skills'):'Idle')},
  {id:'adaptive', icon:'🧩', label:'Adaptive Practice', en:'Adaptive Practice',
   status:s=>(s.teaching&&s.teaching.target)?('Target Difficulty '+s.teaching.target):'Knowledge Point Graph'},
  {id:'console',  icon:'🖥', label:'Run Log', en:'Run Log',
   status:s=>{const p=s.pilot||{}; return p.running?'Running':(p.winding_down?'Finishing':'Idle');}},
];
let TAB = localStorage.getItem('mve_tab') || 'overview';

/* ---------------- Utilities ---------------- */
function chips(items, cls){
  if(!items||!items.length) return '<span class="chip grey">None</span>';
  return items.map(i=>`<span class="chip ${cls||''}">${ESC(typeof i==='string'?i:i.label)}</span>`).join('');
}
function lineChart(pts, opts){
  const W=560,H=190,P={l:38,r:14,t:14,b:26};
  if(!pts.length) return '<div class="empty">No Data</div>';
  const iw=W-P.l-P.r, ih=H-P.t-P.b;
  // Y-axis ticks default to percentages over 0..1; with `opts.max`, they show **absolute values**.
  // The learning curve follows original Voyager (curriculum.py:135) and plots `len(completed_tasks)`:
  // the cumulative **number** of solvable questions, not a proportion.
  const maxV = Number(opts.max) || 1;
  const fmtV = opts.fmt || (v=>Math.round(v*100)+'%');
  const X=i=>P.l+(pts.length===1?iw/2:iw*i/(pts.length-1));
  const Y=v=>P.t+ih*(1-Math.max(0,Math.min(1,v/maxV)));
  const grid=[0,.25,.5,.75,1].map(v=>
    `<line x1="${P.l}" y1="${Y(v*maxV)}" x2="${W-P.r}" y2="${Y(v*maxV)}" stroke="rgba(31,41,36,.08)"/>
     <text x="${P.l-8}" y="${Y(v*maxV)+4}" font-size="10" fill="#9ca3af" text-anchor="end">${fmtV(v*maxV)}</text>`).join('');
  const path=pts.map((p,i)=>`${i?'L':'M'}${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join(' ');
  const dots=pts.map((p,i)=>
    `<circle cx="${X(i).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="3.2" fill="${opts.color}">
     <title>Run ${p.x} · ${p.label}</title></circle>`).join('');
  const xlab=pts.map((p,i)=>
    `<text x="${X(i).toFixed(1)}" y="${H-8}" font-size="10" fill="#9ca3af" text-anchor="middle">${p.x}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px">
    ${grid}
    <path d="${path}" fill="none" stroke="${opts.color}" stroke-width="2.2"
          stroke-dasharray="${opts.dash||''}" stroke-linejoin="round"/>
    ${dots}${xlab}</svg>`;
}
/* Criterion area: Boolean trajectory (green=met, red=not met).
   The criterion layer uses done (Voyager's metric: task complete = executed + data retrieved + no failure,
   without counting coverage points). VLML success (all scoring points met) is a blue-gray observation line
   and does not drive learning. */
function stepChart(pts, opts){
  const W=560,H=120,P={l:38,r:14,t:14,b:26};
  if(!pts.length) return '<div class="empty">No Data</div>';
  const iw=W-P.l-P.r, ih=H-P.t-P.b;
  const X=i=>P.l+(pts.length===1?iw/2:iw*i/(pts.length-1));
  const Y=v=>P.t+(v?0:ih);              // true at the top, false at the bottom
  const cells=pts.map((p,i)=>{
    const x0=X(i), x1=i<pts.length-1?X(i+1):X(i)+10;
    return `<rect x="${x0.toFixed(1)}" y="${Y(p.v).toFixed(1)}" width="${(x1-x0).toFixed(1)}" height="${ih}" fill="${p.v?opts.yes:opts.no}" opacity="0.9">
      <title>Run ${p.x} · ${p.v?'Met':'Not Met'}</title></rect>`;
  }).join('');
  const xlab=pts.map((p,i)=>
    `<text x="${X(i).toFixed(1)}" y="${H-8}" font-size="10" fill="#9ca3af" text-anchor="middle">${p.x}</text>`).join('');
  const labels=`<text x="${P.l+4}" y="${P.t+10}" font-size="10" fill="${opts.yes}">✓ Met</text>
               <text x="${P.l+4}" y="${P.t+ih-4}" font-size="10" fill="${opts.no}">✗ Not Met</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px">${cells}${labels}${xlab}</svg>`;
}

/* ---------------- hero ---------------- */
function renderHero(s){
  const pills=[];
  if(s.has_data){
    const tr=(s.exam_curve||{}).transfer;
    const lastOk = s.last_success ? '<span class="chip ok">success</span>'
                                  : '<span class="chip bad">Not Met</span>';
    // Criterion KPI: self-verification result for the current question (not coverage or mastery)
    pills.push(['Current Criterion', lastOk]);
    pills.push(['Transfer Success Rate', tr ? (tr.ok_count+' / '+tr.count) : '—']);
    pills.push(['Total Rounds', s.total_rounds]);
    pills.push(['Skill Library', (s.skill_stats&&s.skill_stats.skills||0)+' Skills / Bloat '+(s.skill_stats&&s.skill_stats.bloat||1)]);
    // The "Mastery Change," "Status," and "Blocked Fabrications" blocks were removed
    // (2026-10-13 backend-index cleanup). All 36 production run_log records come from the new backend
    // (judge=db_evidence), which produces none of mastery / status_label / flags / hallucination_count.
    // Those fields were written only by the frozen legacy backend mve/run_mve.py.
    // Keeping them would permanently show "Mastery None" and empty flags, falsely implying no learning.
  }
  pills.push(['Data Source', `<span class="chip ${s.env.state==='ready'?'ok':'bad'}">${s.env.state==='ready'?'VLML Ready':'VLML Unavailable'}</span>`]);
  document.getElementById('pills').innerHTML =
    pills.map(([l,v])=>`<div class="summary-pill"><span>${ESC(l)}</span><strong>${v}</strong></div>`).join('');

  const p=s.pilot||{};
  const lp=p.loop||{};
  const dot=document.getElementById('runDot');
  // "Stopped" while the loop is still finishing: pilot exits after receiving the stop signal, while loop.py
  // finishes the current question before exiting (60–90 seconds). Users must see data changing during this period.
  const winding = !!p.winding_down;
  dot.className='dot '+((p.running||winding)?(winding?'warn':'on'):'off');
  let runTxt;
  if(winding){
    runTxt = `Finishing · Stops After Round ${lp.round||'?'} · ${ESC(p.why||'')}`;
  }else if(p.running){
    const kind = (p.rounds===0||lp.continuous) ? 'Continuous (Matches Original while True)'
                                               : ('Limit '+(p.rounds||1)+' Rounds');
    runTxt = `Running · ${kind} · ${lp.round||p.iterations||0} Rounds Completed`;
  }else{
    runTxt = p.why||'Not Running';
  }
  document.getElementById('runState').innerHTML = runTxt;
  document.getElementById('runStart').disabled = !!(p.running||winding);
  document.getElementById('runStop').disabled  = !(p.running||winding);

  // Continuous switch: disable it when a practice scope is active. A scope pins the generator to one question,
  // so continuous mode would rerun the same question forever (progress stays at 0/1). Only one round is allowed.
  const ck=document.getElementById('ckContinuous'), ckw=document.getElementById('ckWrap');
  if(ck){
    const scoped=!!(window.__scope&&window.__scope.active);
    ck.disabled = p.running||winding||scoped;
    if(scoped){ ck.checked=false; }
    ckw.className = 'runctl-ck'+(ck.disabled?' off':'');
    ckw.title = scoped
      ? 'Practice scope is set: the generator is pinned to one question, so continuous mode would repeat it forever (progress stays at 0/1). Clear the scope to enable continuous mode.'
      : 'Matches original Voyager: the main loop is while True and stops only when you click Stop or reach the limit.';
  }

  document.getElementById('statusLine').textContent = s.has_data
    ? (s.reading_text||'')
    : (s.hint||'No Run Records Yet');

  renderScopeBar(s);
}

/* Practice scope bar: when active, show "scope → question"; otherwise prompt users to set it on the graph page.
   Scope determines question generation, so the cockpit needs no difficulty/question controls, only Start / Stop. */
function renderScopeBar(s){
  window.__scope = s.practice_scope || {active:false};
  const sc=window.__scope;
  const chip=document.getElementById('scopeChip');
  const hint=document.getElementById('scopeHint');
  if(!chip) return;
  if(sc.active){
    chip.innerHTML = `<span class="chip ok">${ESC(sc.label)}</span>`
      + `<span class="chip grey">${ESC((sc.topics||[]).join('、'))}</span>`
      + `<span class="chip grey">r${sc.scope_revision||0}</span>`
      + ` <button class="btn btn-secondary scope-act" data-scope-clear="1"
           style="padding:4px 9px;font-size:12px">Clear Scope</button>`;
    hint.textContent = sc.point ? ('Scope Criterion: '+sc.point) : '';
  }else if(sc.invalidated){
    chip.innerHTML = `<span class="chip warn">Scope Invalidated</span>`
      + `<span class="chip grey">${ESC(sc.label||'')}</span>`;
    hint.textContent = ESC(sc.reason||'Dimension is no longer in the knowledge graph');
  }else{
    chip.innerHTML = `<span class="chip grey">Not Set · Question Generator Chooses</span>`;
    hint.textContent = 'Open Knowledge Graph and click Practice This Knowledge Point on a dimension';
  }
}

function renderNav(s){
  document.getElementById('nav').innerHTML = TABS.map(t=>`
    <button class="workspace-tab" role="tab" data-tab="${t.id}" aria-selected="${TAB===t.id}">
      <span class="workspace-tab__icon">${t.icon}</span>
      <span class="workspace-tab__label">${t.label}<span class="tab-en">${t.en||''}</span></span>
      <span class="workspace-tab__status">${ESC(t.status(s))}</span>
    </button>`).join('');
}

/* ---------------- Sections ---------------- */
/* Actual capability curve: unscaffolded exam (the column that belongs on the learning curve) */
function examCard(s){
  const e=s.exam_curve||{};
  const p=s.pilot||{};
  const busy=!!p.running;
  const runningJob=busy?(p.job_label||p.job||'Task'):'';
  const ctl=`<div class="runctl">
      <button class="btn btn-primary" id="btnPlacement" ${busy?'disabled':''}>Baseline Exam (Entire Library, No Graph)</button>
      <button class="btn btn-secondary" id="btnFinal" ${busy?'disabled':''}>Final Exam (With Skill Library)</button>
      <span class="lbl">Units</span>
      <select id="learnUnits">${[2,4,6,8].map(n=>`<option value="${n}" ${n===4?'selected':''}>${n}</option>`).join('')}</select>
      <span class="lbl">Transfer Check Every</span>
      <select id="learnTransfer">${[0,2,3,4].map(n=>`<option value="${n}" ${n===3?'selected':''}>${n===0?'None':n+' Units'}</option>`).join('')}</select>
      <button class="btn btn-secondary" id="btnLearn" ${busy?'disabled':''}>Run Learning Units</button>
      <button class="btn btn-danger" id="btnExamStop" ${busy?'':'disabled'}>■ Stop</button>
    </div>
    ${busy?`<div class="note info">Running: <b>${ESC(runningJob)}</b>
      ${p.iterations?`(Run ${p.iterations})`:''} — See progress on the Run Log page.
      The eight-question baseline exam takes about seven minutes. Refreshing the page will not interrupt it.</div>`:''}`;
  if(!e.has_data) return `<div class="panel"><div class="panel__head"><h2>Actual Capability Curve · Unscaffolded Exam</h2>
      <span class="hint">The practice loop uses the knowledge graph, so it measures the scaffold, not capability</span></div>
    <div class="empty">${ESC(e.hint||'No unscaffolded exam records yet')}</div>
    <div class="note">Click Baseline Exam to test every question in the library <b>without the knowledge graph</b>
      (about seven minutes) and measure true zero-baseline capability. The skill library must be empty:
      testing with it measures capability after practice, not the starting point.</div>
    ${ctl}</div>`;
  const rowsHtml=(e.tracks||[]).map(t=>{
    const after=t.after.length
      ? t.after.map(a=>`<span class="chip ${a.cov>=1?'ok':(a.cov>0?'warn':'bad')}">${ESC(a.pct)}</span>`).join(' ')
      : '<span class="hint">Not Retested</span>';
    const d=t.delta_pp===null?'':(t.delta_pp>0
      ? `<span class="chip ok">+${t.delta_pp}pp</span>`
      : (t.delta_pp<0?`<span class="chip bad">${t.delta_pp}pp</span>`:'<span class="chip grey">No Change</span>'));
    return `<tr><td class="mono">${ESC(t.topic_id)}${t.exhausted?' <span class="chip grey">Graduated · Yielded Slot</span>':''}</td>
      <td>${ESC(t.baseline_pct)} <span class="hint">${ESC(t.level)}</span></td>
      <td>${after}</td>
      <td>${t.final_pct?`<span class="chip ${t.final>=1?'ok':(t.final>0?'warn':'bad')}">${ESC(t.final_pct)}</span>`:'<span class="hint">—</span>'}</td>
      <td>${d}</td></tr>`;
  }).join('');
  const tr=e.transfer;
  const trHtml=tr?`<div class="note"><b>Transfer Check (${tr.count} exams on unpracticed questions):</b>${ESC((tr.seq||[]).join(' → '))} —
    ${tr.rose?'Improved, indicating actual learning rather than memorizing this question.':'<b>No Improvement</b>: the system memorized this question\'s solution rather than gaining transferable capability.'}</div>`:'';
  const fin=e.final;
  const finHtml=fin?`<div class="note"><b>Final Exam (${fin.count} questions${fin.covered_all?' · Entire Library':' · Partial'}):</b>
    Baseline average ${fin.avg_baseline}% → final average ${fin.avg}%
    (${fin.delta_pp>0?'+':''}${fin.delta_pp}pp, ${fin.mastered} questions at ≥80%)
    — This is an unscaffolded retest <b>with the skill library</b>. It uses the same denominator as the baseline and is directly comparable.</div>`:'';
  return `<div class="panel"><div class="panel__head"><h2>Actual Capability Curve · Unscaffolded Exam</h2>
      <span class="hint">Practice uses the graph / exams remove it — one row per question, no artificial connections</span></div>
    <div class="kpis">
      <div class="kpi"><div class="label">Baseline Average</div><div class="value">${e.avg_baseline}%</div></div>
      <div class="kpi"><div class="label">Latest Average</div><div class="value">${e.avg_latest}%</div></div>
      <div class="kpi"><div class="label">Mastered ≥80% (Baseline → Now)</div><div class="value">${e.mastered} → ${e.mastered_now}</div></div>
      <div class="kpi"><div class="label">Retested / Improved</div><div class="value">${e.retested} / ${e.rose}</div></div>
      <div class="kpi"><div class="label">Average Gain</div><div class="value">${e.avg_delta_pp===null?'—':(e.avg_delta_pp>0?'+':'')+e.avg_delta_pp+'pp'}</div></div>
    </div>
    <table><thead><tr><th>Question</th><th>Baseline (Unscaffolded)</th><th>Post-Practice Retest</th><th>Final Exam</th><th>Δ</th></tr></thead>
      <tbody>${rowsHtml}</tbody></table>
    ${finHtml}
    ${trHtml}
    ${ctl}
    <div class="legend">Baseline = first full-library unscaffolded exam after clearing the library (fixed baseline);
      Retest = remove the graph and retest after practicing that question;
      Final exam = retest the entire library <b>with the current skill library</b>, directly comparable to baseline.
      The question generator selects the weakest question first based on the baseline column.</div></div>`;
}

function pOverview(s){
  // Keep two empty states distinct: an empty run_log does not mean there is no data.
  // Baseline exams use exam_log.jsonl and intentionally do not enter run_log; completed exams must still appear.
  // Otherwise the overview would report no run records immediately after a completed baseline exam.
  if(!s.has_data){
    const ex=s.exam_curve||{};
    if(!ex.has_data) return `<div class="panel"><div class="empty">${ESC(s.hint||'No Run Records')}</div>
    <div class="note info">The dashboard reads run_log.jsonl (practice) only.
      <b>Use the Learning Curve below for progress and mastery</b>. The practice loop is intentionally ungraded;
      the learning curve is connected to the <b>consumption capability side</b> (credit is recorded only after a user asks and receives an answer).
      Click ▶ Start Voyager above, or first run <span class="mono">python mve/run_mve.py --llm</span> in a terminal.</div></div>
    <div class="panel">${consumeCurve(s)}</div>
    ${envCard(s)}`;
    return `<div class="note info"><b>Practice records (run_log) are empty</b> — this is expected:
      baseline exams do not enter run_log. <b>The learning curve is below (consumption capability side)</b>.
      Next, click Run Learning Units to extend both the practice and retest curves.</div>
    <div class="panel">${consumeCurve(s)}</div>
    ${examCard(s)}
    ${envCard(s)}`;
  }
  // The `cov` (coverage) and `mas` (mastery) lines were removed. All production run_log records come from
  // the new backend (judge=db_evidence), where coverage/score are always None and mastery is never written.
  // Plotting them creates two permanent zero lines and falsely suggests that coverage declined.
  // Criterion layer: task completion (Voyager metric: executed + data retrieved + no failure; no coverage points)
  const suc=s.series.filter(p=>p.done!==null && p.done!==undefined)
                     .map(p=>({x:p.i,v:!!p.done}));
  // VLML observation: all scoring points met (exam metric; does not drive learning)
  const vlm=s.series.filter(p=>p.success!==null && p.success!==undefined)
                    .map(p=>({x:p.i,v:!!p.success}));
  const rc=s.recommend||{};
  const whyHtml=rc.topic_id?`<div class="note info"><b>Question Rationale (Criterion Layer):</b>
    ${ESC(rc.reason_label||rc.reason)} — ${ESC(rc.explanation)}</div>`:'';
  return `<div class="panel">
    <div class="panel__head"><h2>Current State · Criteria</h2>
      <span class="hint">Use criterion-layer done (Voyager metric: task complete), not coverage or mastery</span></div>
    <div class="kpis">
      <div class="kpi"><div class="label">Current Criterion</div>
        <div class="value">${s.last_done?'<span class="chip ok">done · Task Complete (executed + data retrieved + no failure)</span>':'<span class="chip bad">Incomplete · execution/data retrieval failed</span>'}</div></div>
      <div class="kpi"><div class="label">VLML Observation</div>
        <div class="value">${s.last_success?'<span class="chip ok">success · All Scoring Points Met</span>':'<span class="chip dim">Not Fully Met</span>'}</div></div>
      <div class="kpi"><div class="label">Total Rounds</div><div class="value">${s.total_rounds}</div></div>
    </div>
    <div class="note ${s.reading==='learned'?'info':''}"><b>Interpretation:</b>${ESC(s.reading_text)}</div>
    ${whyHtml}
  </div>
  <div class="panel">${consumeCurve(s)}</div>
  <div class="panel"><div class="panel__head"><h2>Criteria · Task Completion (Voyager Metric)</h2>
      <span class="hint">Storing skills, stopping repeated questions, and advancing depend only on this Boolean line; coverage points do not count</span></div>
      ${stepChart(suc,{yes:'#2f7d57',no:'#e5484d'})}
      <div class="legend">Green = task complete (orchestration executed + data retrieved + no failure); red = incomplete.
        VLML success (all scoring points met) is observation only ↓</div>
      ${stepChart(vlm,{yes:'#3b82f6',no:'#c7ccd4'})}
      <div class="legend">Blue = VLML success observation (exam metric; does not drive learning).</div>
      <div class="note"><b>The practice loop is intentionally ungraded</b>: coverage, score, and mastery remain empty in practice.
        They are <b>metrics</b>; putting them into the feedback loop teaches the system to please the referee rather than retrieve data.
        <b>The learning curve therefore uses the consumption capability side</b> (the line above): credit is recorded only when
        a user asks and receives an answer; successful practice <b>does not count</b>. This follows original Voyager:
        <span class="mono">curriculum.py:339</span> appends only on success, and
        <span class="mono">:135</span> defines the curve as <span class="mono">len(completed_tasks)</span>.
        The unscaffolded retest curve appears at the bottom.</div>
    </div>
  ${examCard(s)}
  ${envCard(s)}`;
}

function pPractice(s){
  const r=s.recommend||{};
  const changed = window.__changedFrom && r.topic_id && r.topic_id!==window.__changedFrom;
  const recmd = r.topic_id ? `<div class="recmd ${changed?'now':''}">
      <div><span class="chip ok">Current Recommendation</span>
        <span class="chip grey">${ESC(r.reason_label||r.reason)}</span>
        <span class="chip grey">Difficulty ${r.difficulty}${r.difficulty_target?` → Target ${r.difficulty_target}`:''}</span>
        ${r.hint?`<span class="chip grey">Scaffold ${ESC(r.hint)}</span>`:''}
        ${changed?`<span class="changed">Import Changed Recommendation (Previously ${ESC(window.__changedFrom)})</span>`:''}</div>
      <div class="rq">${ESC(r.question)}</div>
      <div class="rw"><b>Why This Question:</b>${ESC(r.explanation)}</div>
      ${r.difficulty_why?`<div class="rw" style="margin-top:4px"><b>Difficulty Progression:</b>${ESC(r.difficulty_why)}</div>`:''}
      ${r.hint_label?`<div class="rw" style="margin-top:4px"><b>Scaffold Level:</b>${ESC(r.hint)} — ${ESC(r.hint_label)}</div>`:''}
      <div class="tm mono" style="margin-top:6px;font-size:12px;color:var(--muted)">${ESC(r.cmd||'')}</div>
    </div>` : '';

  const taskCards=(s.tasks||[]).map(t=>{
    const done=(s.attempted||{})[t.topic_id]||0;
    const wrong=(s.failed||{})[t.topic_id]||0;
    const isNow=t.topic_id===s.topic_id;
    const tag = isNow ? '<span class="chip ok">Current</span>'
      : wrong>0 ? `<span class="chip bad">Errors ${wrong}</span>`
      : done>0 ? `<span class="chip grey">Completed ${done}</span>`
      : '<span class="chip">Not Attempted</span>';
    return `<div class="taskcard ${isNow?'now':''}">
      <div class="th">${ESC(t.question)} ${tag}</div>
      <div class="tm"><span class="chip grey">Difficulty ${t.difficulty}</span>
        <span class="chip grey">${t.key_points.length} Scoring Points</span>
        <span class="chip grey">${t.has_answer_spec} Deterministic Recipes</span></div>
      <div class="tm">Required Tools: ${(t.requires_tools||[]).map(x=>`<span class="tool">${ESC(x)}</span>`).join('')||'—'}</div>
      <div class="tm">Run: <span class="mono">python mve/run_mve.py --llm --topic ${ESC(t.topic_id)}</span></div>
    </div>`;
  }).join('');

  const ref=s.referee||{state:'pending',facts:[]};
  const refState={ready:['ok','Ready'],pending:['warn','Pending'],empty:['bad','No Output'],unavailable:['bad','Unavailable']}[ref.state]||['grey',ref.state];
  const refRows=ref.facts.length?ref.facts.map(f=>
    `<tr><td class="mono">${ESC(f.subject)}</td><td class="mono">${ESC(f.dimension)}</td>
     <td class="num"><b>${ESC(f.value)}</b></td><td class="num">${f.base===null?'—':f.base}</td>
     <td class="mono">${ESC(f.source)}</td></tr>`).join('')
    :`<tr><td colspan="5" class="empty">The referee has not run this question. One main-loop run will generate and cache it.</td></tr>`;

  return `<div class="panel">
      <div class="panel__head"><h2>Question Generator · Next Question</h2>
        <span class="hint">Study companion priority: retry errors &gt; due reviews &gt; human imports &gt; tool coverage gaps &gt; new questions</span></div>
      ${recmd||'<div class="empty">Question Generator Unavailable</div>'}
    </div>
    <div class="panel">
      <div class="panel__head"><h2>Current Question</h2></div>
      <div class="mono" style="font-size:13px">${ESC(s.question||'—')}</div>
      <div class="legend">topic_id: ${ESC(s.topic_id||'—')}</div>
    </div>
    <div class="panel">
      <div class="panel__head"><h2>VLML0 Referee · Standard Answer</h2>
        <span class="hint">Generated independently by original VLML, not handwritten</span>
        <span class="chip ${refState[0]}">${ESC(refState[1])}</span></div>
      ${ref.trajectory&&ref.trajectory.length?`<div style="margin-bottom:8px"><span class="chip grey">Orchestration: ${ref.trajectory.map(t=>ESC(t)).join(' → ')}</span></div>`:''}
      <table><thead><tr><th>subject</th><th>dimension</th><th>Standard Value</th><th>base</th><th>Source</th></tr></thead>
        <tbody>${refRows}</tbody></table>
      <div class="note info">Voyager never sees the values in this column, only its own coverage.
        It stops as soon as the answer passes and does not force all rounds.</div>
    </div>
    <div class="panel">
      <div class="panel__head"><h2>Practice Scope</h2><span class="hint">Ordered using study companion ordered_scope_topics</span></div>
      <div class="tasks">${taskCards}</div>
    </div>`;
}

function pImport(s){
  return `<div class="panel">
    <div class="panel__head"><h2>Human Import Intent</h2>
      <span class="hint">User asks → Voyager answers · VLML analysis remains visible</span></div>
    ${coachRunningBar(s)}
    <div class="imp">
      <div style="font-size:12.5px;color:var(--muted);margin-bottom:8px">
        Enter a question. <b>Voyager answers using its accumulated capabilities</b>: prior experience recalled by mem0,
        data-retrieval patterns from the skill library, and tools/tables visible in the current data world. The VLML
        analysis below still discloses which tools ran and which facts were retrieved. The result is written as a
        <b>control fact</b> to the causal timeline, which the question generator uses to select the next question.
      </div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:8px">
        <b>Inclusion criterion</b> (not "whether the graph contains a dimension with the same name"): determine whether
        the tables read by this VLML retrieval are covered by the graph's <b>in-table tool set</b> (46 SQL insights).
        Covered → include by <b>creating an edge, not a node</b> (same as the study companion: do not create a knowledge
        point for unmatched material); not covered → report it accurately, exclude it, and do not generate questions from it.
      </div>
      <textarea id="q" placeholder="Example: How well does Cloud9 perform in pistol rounds? Is the conclusion reliable?"></textarea>
      <div class="imp-row">
        <select id="topic"><option value="">Auto Match (Keyword Routing)</option></select>
        <button class="btn btn-primary" id="go">Ask Voyager</button>
        <button class="btn btn-secondary" id="clr">Clear</button>
      </div>
      <div class="hit" id="hit">Topic routing results appear here.</div>
      <div id="result"></div>
    </div>
    ${consumeCurve(s)}
    <h3>Import History (${(s.imports||[]).length})</h3>
    <div class="legend">Dashed box = comparable: false. It is excluded from comparison and mastery, and enters only the causal timeline.</div>
    <div id="implist"></div>
  </div>`;
}

/* Consumption progress curve: progress and mastery are measured on the consumption pipeline (set 2026-10-13).
   Why not practice: practice is ungraded (VLML metrics cannot enter Voyager's feedback loop), and run_log coverage
   is always None. Consumption is delivery: when a user requests an answer, the VLML0 referee independently measures
   coverage, providing a real progress signal. */
function consumeCurve(s){
  const cc=s.consume_curve||[];
  if(!cc.length) return `<h3>Learning Curve · Consumption Capability</h3>
    <div class="empty">No consumption records yet. Ask a question above and Voyager will answer using accumulated capabilities.
    A successfully answered question enters its capability library; mastery and progress accumulate here.</div>`;
  // Main curve = **cumulative mastered count** as the capability set grows.
  // The Y-axis shows the **absolute number** of solvable questions, not a percentage, following original Voyager:
  // `curriculum.py:135 return len(self.completed_tasks)`.
  // Percentages introduce denominator ambiguity (should practiced questions be included?); absolute counts do not.
  // Failures do not count, so the curve never decreases and directly represents learning.
  const tot=cc[cc.length-1].ability_total||0;
  const masMax=Math.max(1,...cc.map(c=>c.mastered_total||0));
  const masPts=cc.map((c,i)=>({
    x:i+1,
    y: c.mastered_total||0,
    label:`${c.mastered_total||0} Mastered · ${c.question}`}));
  // Secondary curve = coverage (answer completeness for this attempt; a quality score, not mastery)
  const covPts=cc.filter(c=>c.coverage!==null&&c.coverage!==undefined)
                 .map((c,i)=>({x:i+1, y:c.coverage, label:`Agreement ${Math.round(c.coverage*100)}% · ${c.question}`}));
  const last=cc[cc.length-1];
  const reused=cc.filter(c=>c.reused).length;
  // Comparison detail for each consumption: the score comes from comparing the two approaches and must be inspectable.
  // A percentage without its basis does not let users judge whether the score is trustworthy.
  const cmpRows=cc.map((c,i)=>`<tr>
      <td class="num">${c.i}</td>
      <td>${ESC(c.question)}</td>
      <td class="num">${ESC(c.rows||0)}</td>
      <td class="num">${ESC(c.referee_rows||0)}</td>
      <td class="num">${c.coverage===null||c.coverage===undefined?'—'
        :(c.judged?Math.round(c.coverage*100)+'%':'Not Judged')}</td>
      <td>${c.coverage===null||c.coverage===undefined
            ? '<span class="chip grey">Not Judged</span>'
            : (c.same_metric?'<span class="chip ok">Yes</span>':'<span class="chip warn">No</span>')}</td>
      <td>${c.coverage===null||c.coverage===undefined
            ? '<span class="chip grey">—</span>'
            : (c.voyager_sound?'<span class="chip ok">Sound</span>':'<span class="chip bad">Material Flaw</span>')}</td>
      <td style="font-size:12px">${ESC(c.method_reason||c.verdict||'')}${c.method_diff?' ('+ESC(c.method_diff)+')':''}</td>
    </tr>`).join('');
  return `<h3>Learning Curve · Consumption Capability (${cc.length} Consumptions)</h3>
    <div class="legend"><b>Mastery = growth of the capability set</b>: a user asks → Voyager answers →
    the question enters its capability library. The Y-axis is the <b>cumulative number of solvable questions</b>,
    following original Voyager <span class="mono">curriculum.py:135 len(completed_tasks)</span>.
    <b>Practice does not count</b>: the practice loop is intentionally ungraded. Successful runs only advance
    curriculum planning ("this question has been planned; do not plan it again") and do not expand capability.
    Original Voyager also appends only on success (<span class="mono">:339</span>); failures do not count.
    Agreement is separate: it compares the Voyager and independent VLML approaches for quality reference only.</div>
    ${lineChart(masPts,{color:'#059669',max:masMax,fmt:v=>Math.round(v)+' Questions'})}
    <div class="legend" style="margin-top:4px">Cumulative Mastery ${last.mastered_total||0} / ${tot||'?'} Questions
      ${tot?`(${Math.round(100*(last.mastered_total||0)/tot)}%)`:''}</div>
    ${covPts.length?`<div class="legend" style="margin-top:8px">Agreement (Voyager vs. VLML approach comparison; display only)</div>${lineChart(covPts,{color:'#2563eb',dash:'4 3'})}`:''}
    <div class="legend" style="margin-top:6px">
      Latest: ${last.mastered?'<span class="chip ok">Mastered (Added to Capability Library)</span>':'<span class="chip bad">No Answer · Not Added</span>'}
      ${last.coverage===null||last.coverage===undefined?'':`<span class="chip grey">Agreement ${Math.round(last.coverage*100)}% · ${ESC(last.verdict||'')}</span>`}
      <span class="chip grey">Reused Learned Recipes ${reused}/${cc.length} Times</span>
      ${last.skill_action?`<span class="chip grey">${ESC(last.skill_action)}</span>`:''}
    </div>
    <h3 style="margin-top:10px">Approach Comparison Details</h3>
    <div class="legend">Voyager retrieves one result and VLML independently retrieves another. The LLM judges the two SQL queries
      and outputs side by side; neither answer must be reduced to a scalar fact, so tabular answers can also be evaluated.</div>
    <table><thead><tr><th>#</th><th>Question</th><th>Voyager Rows</th><th>VLML Rows</th>
      <th>Agreement</th><th>Same Metric</th><th>Voyager Approach</th><th>Criterion / Difference</th></tr></thead>
      <tbody>${cmpRows}</tbody></table>`;
}

/* Human-readable criterion labels. This column previously had only VLML0 / rubric. The new backend's `db_evidence`
   fell into the else branch as "rubric," which is the wrong criterion name: users would think the round was scored
   by a rubric when it actually means "whether data was truly retrieved from the DB." */
const JUDGE_LABEL={db_evidence:'DB Evidence', referee:'VLML0 Referee', rubric:'rubric',
                   method_compare:'Approach Comparison', none:'Not Judged'};

function pTrace(s){
  if(!s.has_data) return '<div class="panel"><div class="empty">No trajectory data yet.</div></div>';

  // Conditionally render the entire orchestration trajectory block. Only the frozen legacy backend writes
  // tool sequences (trajectory); the new backend does not store them in run_log. They are empty in all 36
  // production records. The old page therefore showed a table full of "Not Called / None," falsely suggesting
  // that no tools ran when the field simply has no producer.
  const withTraj=s.series.filter(p=>(p.trajectory||[]).length);
  const trajPanel = withTraj.length ? `<div class="panel">
      <div class="panel__head"><h2>Orchestration Trajectory</h2><span class="hint">Direct evidence of learned orchestration</span></div>
      <table><thead><tr><th>#</th><th>Tool Sequence This Round</th><th>Covered Scoring Points</th><th>Missing</th><th>Failed Base Gate</th></tr></thead>
        <tbody>${s.series.map(p=>`
          <tr><td class="num">${p.i}</td>
            <td>${(p.trajectory||[]).length?(p.trajectory||[]).map(t=>`<span class="tool">${ESC(t)}</span>`).join(''):'<span class="chip grey">Not Called</span>'}</td>
            <td>${(p.covered||[]).length?chips(p.covered,'ok'):'<span class="chip grey">None</span>'}</td>
            <td>${(p.missing||[]).length?chips(p.missing,'bad'):'<span class="chip grey">None</span>'}</td>
            <td>${(p.rejected||[]).length?chips(p.rejected,'warn'):'<span class="chip grey">None</span>'}</td></tr>`).join('')}</tbody></table>
    </div>`
    : `<div class="panel"><div class="panel__head"><h2>Orchestration Trajectory</h2></div>
        <div class="empty">The new backend <b>does not store tool sequences in run_log</b>. This page belongs to the frozen
        legacy backend (mve/run_mve.py). To see tool calls, successes, and retrieved rows for this round, use the
        <b>Loop Heartbeat</b> at the top of Run Log; the new backend writes it accurately each round.</div></div>`;

  // Round details: remove Facts / flags / Hallucinations Blocked (always empty; legacy backend only).
  // Combine Coverage / Score and explain that the practice loop is intentionally ungraded.
  const rows=s.series.map(p=>`
    <tr><td class="num">${p.i}</td><td class="mono">${ESC(p.mode)}</td><td class="num">${p.round}</td>
      <td>${p.done?'<span class="chip ok">✓</span>':'<span class="chip bad">✗</span>'}</td>
      <td>${p.success?'<span class="chip ok">✓</span>':'<span class="chip bad">✗</span>'}</td>
      <td><b>${ESC(p.verdict)}</b></td>
      <td class="num">${p.coverage===null?'<span class="hint">Practice Ungraded</span>':(p.coverage*100).toFixed(0)+'%'}</td>
      <td class="mono">${ESC(p.evidence)}</td>
      <td>${ESC(JUDGE_LABEL[p.judge]||p.judge||'—')}</td>
      <td>${p.no_tool_calls?'<span class="chip bad">No Tool Calls</span>':'<span class="chip grey">—</span>'}</td>
      <td class="mono" style="font-size:12px">${ESC(p.topic_id||'')}</td></tr>`).join('');

  // Narrative insights are also conditional: the consumption pipeline produces narratives; practice does not.
  const withStory=s.series.filter(p=>p.narrative);
  const storyPanel = withStory.length ? `<div class="panel">
      <div class="panel__head"><h2>Narrative Insights</h2>
        <span class="hint">VLML README:97 — tools produce metrics and evidence; the LLM produces insights</span></div>
      ${withStory.slice(-2).map(p=>`
        <div class="nocmp">
          <div class="nocmp-tag">Excluded from Comparison · comparable: false</div>
          <div class="nocmp-body">${ESC(p.narrative)}</div>
          ${(p.insights||[]).length?`<div class="nocmp-sub"><b>Insights:</b><ul>${p.insights.map(i=>`<li>${ESC(i)}</li>`).join('')}</ul></div>`:''}
          ${(p.caveats||[]).length?`<div class="nocmp-sub"><b>Data Limitations:</b>${p.caveats.map(c=>ESC(c)).join('; ')}</div>`:''}
          <div class="nocmp-sub">Answer ${p.i} · Based on ${p.narrative_based_on||0} facts <b>verified by the referee</b></div>
        </div>`).join('')}
    </div>`
    : `<div class="panel"><div class="panel__head"><h2>Narrative Insights</h2></div>
        <div class="empty">The <b>consumption pipeline</b> produces narratives (user asks → compare approaches → LLM insight).
        The practice loop only retrieves data and produces no narratives. Ask a question on Human Import to populate this section.</div></div>`;

  return `${trajPanel}
    <div class="panel">
      <div class="panel__head"><h2>Round Details</h2>
        <span class="hint">done = whether data was actually retrieved from the DB; grading occurs only in consumption and exams</span></div>
      <table><thead><tr><th>#</th><th>Mode</th><th>Round</th><th>done</th><th>success</th><th>verdict</th>
        <th>Grade</th><th>Evidence State</th><th>Criterion</th><th>Tools</th><th>Knowledge Point</th></tr></thead>
        <tbody>${rows}</tbody></table>
    </div>
    ${storyPanel}`;
}

function pSkill(s){
  const st=s.skill_stats||{};
  const cards=(s.skill_detail||[]).map(x=>`
    <div class="taskcard" style="margin-top:8px">
      <div class="th">${ESC(x.text)}</div>
      <div class="tm"><span class="chip grey">${ESC(x.topic||'—')}</span>
        <span class="chip grey">Hits ${x.hits}</span>
        <span class="chip ${x.ok>0?'ok':'grey'}">Effective ${x.ok}</span>
        ${x.hits>0
          ? `<span class="chip ${x.ok>0?'ok':'bad'}">Effectiveness ${Math.round(100*x.ok/x.hits)}%</span>`
          : '<span class="chip dim">Not Used Yet</span>'}
        ${x.versions>1?`<span class="chip warn">${x.versions} Versions</span>`:''}</div>
    </div>`).join('');
  const c=s.causal||{};
  const kc={control:'ok',attempt:'',referee:'grey',narrative:'grey'};
  const mc=s.mc_thinking||{};
  const mcBlock = mc.count ? `
    <div class="note info" style="margin-top:10px">
      🧠 <b>MC Strategy Learning Stored: ${mc.count}</b>
      <span class="hint">(topic=mc-thinking; retrievable during the standard MVE practice loop)</span>
      <ul style="margin:6px 0 0 18px;padding:0">${(mc.items||[]).map(x=>`<li><b>${ESC(x.name)}</b> · Hits ${x.hits} · Effective ${x.ok}${x.versions>1?` · ${x.versions} Versions`:''}</li>`).join('')}</ul>
      ${mc.skill_md?`<div class="hint">SKILL.md: ${ESC(mc.skill_md)} (injected directly in the OpenClaw exam environment)</div>`:''}
    </div>` : '';
  const tl=(c.rows||[]).length?`<div class="timeline">${(c.rows||[]).slice().reverse().map(r=>`
      <div class="tl"><span class="seq">#${r.seq}</span>
        <span class="kd"><span class="chip ${kc[r.kind]||'grey'}">${ESC(r.kind_label)}</span></span>
        <span>${ESC(r.summary)}</span>
        <span class="ts">${ESC(String(r.ts||'').replace('T',' ').slice(5,19))}</span></div>`).join('')}</div>`
    :'<div class="empty">Timeline is empty.</div>';
  return `<div class="panel">
      <div class="panel__head"><h2>Skill Library</h2>
        <span class="hint">Following original voyager.py:195/199: <b>retrieve first</b>, then write SQL; sorted by effectiveness</span></div>
      <div class="legend"><b>Hits</b> = times retrieved and injected into prompts · <b>Effective</b> = successful rounds after injection ·
        <b>Hits increase while Effective does not</b> = it was injected but did not help, so it naturally drops in retrieval ranking.
        High effectiveness means the experience is being reused successfully.
      </div>
      <div class="legend">Entries <b>${st.skills||0}</b> · Total Writes <b>${st.writes||0}</b>
        (Rewrites ${st.rewrites||0} · Discarded ${st.skipped||0}) ·
        Bloat <span class="chip ${(st.bloat||1)>1?'ok':'warn'}">${st.bloat||1}</span>
        · Up to <b>${st.top_k||5}</b> entries injected into each prompt</div>
      ${cards||'<div class="empty">Empty. Voyager has not accumulated any experience.</div>'}
      ${mcBlock}
    </div>
    <div class="panel">
      <div class="panel__head"><h2>Action Causal Timeline</h2>
        <span class="hint">control (human import) and attempt (Voyager action) share one monotonic sequence; ordered only, not graded</span></div>
      ${tl}
    </div>`;
}

/* ---------------- Voyager Real Learning (Live MC) ----------------
   Controls for actual learning in MC, using the same mental model as the MVE cockpit:
   Start / Stop / Clear Library / Clear Memory. Stop exits gracefully at a task boundary (no hard kill; skills are stored).
   Clear Library deletes ckpt/skill/; Clear Memory deletes ckpt/curriculum/ + ckpt/events/. */
function pVoyager(s){
  const v=s.voyager||{};
  const running=!!v.running;
  const st = v.stop_requested ? 'Stopping (Finishing Current Task)'
           : (running ? 'Learning' : ('Idle · '+(v.why||'')));
  const inv=`<div class="kpis">
    <div class="kpi"><div class="label">Skill Library</div><div class="value">${v.skill_dir_exists?'Available':'Empty'}${v.completed_count?(' · Learned '+v.completed_count):''}</div></div>
    <div class="kpi"><div class="label">Curriculum Progress</div><div class="value">${v.curriculum_dir_exists?'Available':'Empty'}</div></div>
    <div class="kpi"><div class="label">Event History</div><div class="value">${v.events_dir_exists?'Available':'Empty'}</div></div>
    <div class="kpi"><div class="label">Failed Tasks</div><div class="value">${v.failed_count||0}</div></div>
    <div class="kpi"><div class="label">Current Iteration</div><div class="value">${v.iterations||0} / ${v.max_iterations||'—'}</div></div>
  </div>`;
  const ctl=`<div class="runctl">
      <span class="lbl">MC Real Learning</span>
      <button class="btn btn-primary" id="voyStart" ${running?'disabled':''}>▶ Start Learning</button>
      <button class="btn btn-danger" id="voyStop" ${running?'':'disabled'}>■ Stop</button>
      <button class="btn btn-secondary" id="voyClearLog">Clear Log</button>
    </div>
    <div class="runctl" style="margin-top:8px">
      <button class="btn btn-danger" id="voyClearSkill" title="Delete ckpt/skill/: Voyager's learned JS skill library">🗑 Clear Library (Delete Skills)</button>
      <button class="btn btn-danger" id="voyClearMemory" title="Delete ckpt/curriculum/ + ckpt/events/: curriculum progress and event history">🗑 Clear Memory (Delete Progress + Events)</button>
    </div>
    <div class="note ${running?'info':''}">${
      running
        ? ('Voyager automatic curriculum running in MC (pid '+v.pid+') · '+v.completed_count+' Skills Learned · '+v.failed_count+' Failed')
        : ('Status: '+st)
    }</div>`;
  const log = ESC((v.tail||'(No Log)').replace(/\u001b\[[0-9;]*m/g,''));
  return `<div class="panel">
    <div class="panel__head"><h2>MC Real Learning · Voyager Automatic Curriculum</h2>
      <span class="hint">Voyager connects to an external MC server, proposes tasks → bot executes → Critic verifies success → JS skills stored</span>
      <span class="chip ${running?'ok':'grey'}">${running?'Learning':'Idle'}</span></div>
    <div class="legend">Operates like the MVE dashboard: <b>Stop</b> (graceful task-boundary exit, no hard kill),
      <b>Clear Library</b> (forget skills), and <b>Clear Memory</b> (forget progress). Skills are stored per task;
      stopping does not discard learned skills.</div>
    ${inv}
    ${ctl}
    <div class="console">${log}</div>
    <div class="legend">Logs come from run_voyager_learn.py (venv). Before starting: 1) VPS memory upgraded;
      2) MC server running on 25565; 3) /root/.voyager_secret contains DEEPSEEK_API_KEY.
      After Stop, the model exits after finishing the current task and is not killed midway.</div>
  </div>`;
}

/* Loop heartbeat bar: reads a **separate state file** (mve/loop_state.json, written only by
   voyager_vlml/loop.py). They are separate because dashboard state records user actions (who clicked
   Start/Stop and the pid), while loop state records loop progress. Sharing one file would cause each
   loop rewrite to erase a recent stop_requested, while dashboard pid updates would erase fresh evidence failures.

   Its most important job is to visibly separate "it did not learn" from "the pipeline never ran."
   The remedies are opposite; merging both into "failed" sends users to fix the wrong component. */
function loopHealthBar(s){
  const p=s.pilot||{}, lp=p.loop||{};
  if(!lp || !lp.round) return '';      // No loop runs yet; do not distract

  const KIND={
    linkage:{c:'bad', t:'Pipeline Failed',
      d:'No tool call succeeded. This is not a learning failure; the environment did not start. '
        +'Check whether the database file exists, all 10 VLML tools are installed (see Data Source & Database above), '
        +'and which database mve/db_config.json points to. Fix configuration/dependencies, not capability.'},
    sql_error:{c:'warn', t:'Invalid SQL',
      d:'The tools ran, but the generated SQL is wrong (Parser / Binder errors, wrong column names, or comparing '
        +'Boolean columns as strings). This is a capability problem and the pipeline is healthy. '
        +'Inspect the specific SQL error in the log below; do not troubleshoot the database or tools.'},
    empty:{c:'warn', t:'Retrieved 0 Rows',
      d:'The SQL ran but returned no data. This is a capability problem (filters too strict or no matching data), '
        +'and the pipeline is healthy. Inspect the WHERE clause in the log below.'},
    unproven:{c:'warn', t:'Criterion Not Met',
      d:'Data was retrieved, but the critic found that the observations were not valid evidence (usually the wrong entity or an empty shell table).'},
  };
  const k=KIND[lp.no_evidence_kind]||null;
  const streak=+(lp.no_evidence_streak||0);
  const chips=[
    `Round ${ESC(lp.round)}`,
    lp.continuous?'Continuous (Matches Original while True)':`Round Limit`,
    `Progress ${ESC(lp.progress||0)} Milestones`,
    `Tools ${ESC(lp.tool_ok||0)} Succeeded / ${ESC(lp.tool_fail||0)} Failed`,
    `Retrieved ${ESC(lp.rows||0)} Rows`,
  ].map(t=>`<span class="chip grey">${t}</span>`).join('');
  const warn = (streak>=3 && k)
    ? `<div class="note ${k.c==='bad'?'bad':'info'}" style="margin-top:8px">
         <b>⚠️ No Evidence for ${streak} Consecutive Rounds — ${ESC(k.t)}</b>
         <div style="font-size:12px;color:var(--muted);margin-top:4px">${k.d}</div>
         <div style="font-size:12px;color:var(--muted);margin-top:4px">
           Note: this is observation only; <b>the loop does not stop because of it</b> (only Stop changes cadence).
           ${lp.rounds_since_progress?`${ESC(lp.rounds_since_progress)} rounds since the last milestone unlock.`:''}
         </div></div>`
    : '';
  const err = lp.last_error
    ? `<div class="legend mono" style="word-break:break-all;margin-top:6px">
         Latest Tool Error: ${ESC(String(lp.last_error).slice(0,300))}</div>`
    : '';
  return `<div class="panel" style="margin-bottom:10px">
    <div class="panel__head"><h2>Loop Heartbeat</h2>
      <span class="hint">mve/loop_state.json — written by the loop, read-only here, separate from cockpit state</span>
      <span class="chip ${lp.running?'ok':'grey'}">${lp.running?'Loop Running':'Stopped'}</span>
      ${lp.updated_at?`<span class="chip grey">${ESC(String(lp.updated_at).slice(11,19))}</span>`:''}</div>
    <div>${chips}</div>
    ${lp.topic?`<div class="legend">Current Question: <span class="mono">${ESC(lp.topic)}</span>`
      +`${lp.generated?' (Generated by the system)':''}${lp.reason?` · ${ESC(lp.reason)}`:''}</div>`:''}
    ${warn}${err}
  </div>`;
}

function pConsole(s){
  const p=s.pilot||{};
  const log=ESC(p.tail||'(No Log)');
  const cont=(p.rounds===0)||((p.loop||{}).continuous);
  return `${loopHealthBar(s)}
  <div class="panel">
    <div class="panel__head"><h2>Cockpit</h2>
      <span class="hint">The dashboard starts/stops and reads logs only; process state: mve/pilot_state.json, loop state: mve/loop_state.json</span>
      <span class="chip ${p.running?'ok':'grey'}">${p.running?'Running':'Not Running'}</span>
      ${p.job_label?`<span class="chip ${p.running?'ok':'grey'}">${ESC(p.job_label)}</span>`:''}
      <span class="chip ${cont?'ok':'grey'}">${cont?'Continuous (Graph Complete / Stop)':'Single Round'}</span>
      ${p.topic?`<span class="chip grey">${ESC(p.topic)}</span>`:''}
      ${p.iterations?`<span class="chip grey">${p.iterations} Runs</span>`:''}
      ${p.started_at?`<span class="chip grey">Started ${ESC(String(p.started_at).slice(11,19))}</span>`:''}</div>
    ${p.why&&!p.running?`<div class="legend">${ESC(p.why)}</div>`:''}
    <div class="note info">The learning loop <b>runs continuously</b>, matching original voyager.py:326 while True:
      after one question, the curriculum selects the next from the graph until you click <b>Stop</b>.
      Stop is <b>graceful</b>: it finishes the current question and stores skills before exiting instead of killing the process.
      The milestone curriculum owns question selection (seed milestones → trunk progression); use a single-round limit only for a quick check.</div>
    <div class="console">${log}</div>
    <div style="margin-top:10px">
      <button class="btn btn-secondary" id="clearLog">Clear Log</button>
      <button class="btn btn-secondary" id="refreshNow">Refresh Now</button>
    </div>
  </div>`;
}

function envCard(s){
  const env=s.env||{}, db=s.db||{};
  const map={ready:['ok','Ready'],degraded:['warn','Degraded'],unavailable:['bad','Unavailable']};
  const [c,label]=map[env.state]||['grey',env.state];
  const tables=(env.tables!==undefined&&env.tables!==null)?env.tables:'—';
  return `<div class="panel">
    <div class="panel__head"><h2>Data Source & Database</h2>
      <span class="hint">The VLML database is a .duckdb file; switch databases by changing this file</span>
      <span class="chip ${c}">${ESC(label)}</span></div>
    <div style="margin-bottom:8px">
      ${env.ok?`<span class="chip grey">${tables} Tables</span>`:''}
      ${(env.events!==undefined&&env.events!==null)?`<span class="chip grey">${env.events} Events</span>`:''}
      ${env.usage_tips?`<span class="chip grey">${env.usage_tips} Orchestration Tips</span>`:''}
      ${db.is_default?'<span class="chip grey">Default Database</span>':'<span class="chip warn">Custom Database Active</span>'}
    </div>
    <div class="legend mono" style="word-break:break-all">Current Database: ${ESC(db.path||'—')}</div>
    ${env.reason?`<div class="note">${ESC(env.reason)}</div>`:''}

    <h3>Load a New Database</h3>
    <div class="imp-row" style="margin-top:4px">
      <input id="dbPath" class="mono" placeholder="/absolute/path/to/another.duckdb"
             style="flex:1;min-width:280px;padding:8px 10px;border:1px solid var(--line);
                    border-radius:var(--radius-sm);background:#fff;color:var(--ink)">
      <button class="btn btn-secondary" id="dbProbe">Validate</button>
      <button class="btn btn-primary" id="dbSwitch" disabled>Switch & Restart Process</button>
      <label class="legend" style="display:flex;align-items:center;gap:5px;margin:0">
        <input type="checkbox" id="dbReset"> Clear State After Switch
      </label>
    </div>
    <div class="hit" id="dbHit">Click Validate first to report table and event counts and check whether the series required by existing questions still exists.</div>

    <div class="note info">
      The dashboard process <b>must restart</b> after switching databases. VLML tools hard-code
      <span class="mono">EventDatabase(read_only=True)</span> without db_path. MVE patches the default database
      in <span class="mono">vlml_env.py</span>, and installs that patch only once at process startup.
      <br><br>
      Known limitation: <span class="mono">SERIES / team names</span> in the question registry are hard-coded.
      Reconfigure these constants after switching databases or all questions will return no data.
      Validation checks this condition.
    </div>
  </div>`;
}

/* ---------------- Knowledge Graph ----------------
   Following study companion knowledge_map.tsx: present structure read-only, with no inference.
   Use a **layered** layout (table → tool → dimension, left to right), not force-directed.
   The graph is small with fixed natural layers; layering is more readable and does not jump on refresh. */
let GRAPH=null;
async function loadGraph(){
  if(GRAPH) return GRAPH;
  try{ const r=await fetch('/api/graph?t='+Date.now()); GRAPH=await r.json(); }
  catch(e){ GRAPH={empty:true,error:String(e)}; }
  return GRAPH;
}
const REL={produced_by:['#2f7d5f','Produces','solid'],derived_from:['#8a94a6','Derived from Table','dashed'],
           confusable:['#c2703a','Confusable','dashed'],co_occurs:['#b9c2ce','Co-occurs in Question','dashed'],
           procedure_step:['#6d5bd0','Orchestration Order','solid']};
function renderGraph(g){
  if(g.empty) return `<div class="empty">${ESC(g.error||'Knowledge graph is empty')}</div>`;
  const nodes=g.nodes||[], edges=g.edges||[];
  const byId={}; nodes.forEach(n=>byId[n.id]=n);
  // Draw only nodes with edges. Isolated tools (VLML has nine, but most are unused by questions)
  // would only add clutter; connected nodes carry information.
  const linked=new Set();
  edges.forEach(e=>{linked.add(e.from);linked.add(e.to);});
  const cols={table:[],tool:[],dimension:[]};
  nodes.filter(n=>linked.has(n.id)).forEach(n=>{(cols[n.kind]||(cols[n.kind]=[])).push(n);});
  ['table','tool','dimension'].forEach(k=>cols[k].sort((a,b)=>a.label.localeCompare(b.label)));
  if(!cols.dimension.length) return `<div class="empty">No connected dimensions in the knowledge graph</div>`;

  // Pending review bar: new runtime dimensions **do not take node slots** (following the study companion:
  // runtime knowledge points stay outside the authoritative layer and are registered for review only).
  // The dimension layer is derived only from seed-question rubrics.
  const pendHtml = (g.pending_dims||[]).length ? `<div class="note info" style="margin-bottom:8px">
    <b>${(g.pending_dims||[]).length} Pending Candidates</b> (new runtime dimensions;
    <b>no nodes created</b>, stored on edges only — dimensions come from seed questions and do not grow with generation):
    ${(g.pending_dims||[]).map(p=>`<span class="chip grey">${ESC(p.dimension)}`
      + (p.nearest?` <span style="opacity:.7">≈ ${ESC(p.nearest)}</span>`:'')
      + `</span>`).join(' ')}
  </div>` : '';

  const COLW=262, ROW=30, PADX=54, PADY=26, NW=168, NH=22;
  const order=['table','tool','dimension'];
  const pos={};
  const maxN=Math.max(...order.map(k=>cols[k].length));
  const H=PADY*2+maxN*ROW, W=PADX*2+COLW*2+NW;
  order.forEach((k,i)=>{
    const items=cols[k], total=items.length*ROW;
    const top=PADY+(H-PADY*2-total)/2;
    items.forEach((n,j)=>{ pos[n.id]={x:PADX+i*COLW, y:top+j*ROW+ROW/2}; });
  });
  const colTitle=[['table','Tables (VLML Model)'],['tool','Tools (MCP)'],['dimension','Dimensions']]
    .map(([k,t],i)=>`<text x="${PADX+i*COLW+NW/2}" y="${PADY-9}" font-size="11" font-weight="700"
        fill="#6b7686" text-anchor="middle">${t}</text>`).join('');

  const es=edges.filter(e=>pos[e.from]&&pos[e.to]).map(e=>{
    const a=pos[e.from], b=pos[e.to], [c,,dash]=REL[e.relation]||['#b9c2ce','','dashed'];
    const x1=a.x+NW, y1=a.y, x2=b.x, y2=b.y;
    const same=(a.x===b.x);
    const mx=(x1+x2)/2;
    // Route same-column edges (between dimensions) around the outside so they do not overlap nodes
    const d=same?`M${x1},${y1} C${x1+46},${y1} ${x2+46},${y2} ${x2},${y2}`
                :`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
    return `<path d="${d}" fill="none" stroke="${c}" stroke-width="${e.relation==='produced_by'?1.9:1.2}"
      ${dash==='dashed'?'stroke-dasharray="4 3"':''} opacity=".72">
      <title>${ESC(byId[e.from]&&byId[e.from].label||e.from)} → ${ESC(byId[e.to]&&byId[e.to].label||e.to)}
${ESC(REL[e.relation]&&REL[e.relation][1]||e.relation)}${e.origin==='observed'?' (Observed)':''}</title></path>`;
  }).join('');

  // Dimension nodes are clickable and set the practice scope (same as Practice This Knowledge Point)
  const ns=Object.entries(pos).map(([id,p])=>{
    const n=byId[id]||{};
    const fill={dimension:'#e8f2ee',tool:'#eaf0fb',table:'#f2f0e8'}[n.kind]||'#eee';
    const stroke={dimension:'#2f7d5f',tool:'#48679c',table:'#a8976a'}[n.kind]||'#999';
    const deg=edges.filter(e=>e.from===id||e.to===id).length;
    const clickable = n.kind==='dimension'
      ? ` data-scope-set="${ESC(n.label||id)}" style="cursor:pointer"` : '';
    const isScope = n.kind==='dimension' && window.__scope && window.__scope.active
      && window.__scope.label===n.label;
    return `<g${clickable}><rect x="${p.x}" y="${p.y-NH/2}" width="${NW}" height="${NH}" rx="5"
      fill="${isScope?'#d9efe3':fill}" stroke="${stroke}" stroke-width="${isScope?2.4:1.2}"/>
      <text x="${p.x+8}" y="${p.y+4}" font-size="11" fill="#22303a">${ESC(n.label||id)}</text>
      ${isScope?'<text x="'+(p.x+NW-6)+'" y="'+(p.y+4)+'" font-size="10" fill="#2f7d5f" '
        +'text-anchor="end">Practicing</text>':''}
      <title>${ESC(n.label||id)}
Type: ${ESC(n.kind)}
Edges: ${deg}${n.detail&&n.detail.topic_id?'\nQuestion: '+ESC(n.detail.topic_id):''}${n.detail&&n.detail.required_by?'\nRequired by: '+ESC((n.detail.required_by).join(', ')):''}${n.kind==='dimension'?'\nClick to set practice scope':''}</title></g>`;
  }).join('');

  // Detail list below: following study companion relation_groups — graph for structure, list for content
  const dims=cols.dimension.map(n=>n.label);
  const rows=dims.map(d=>{
    const prod=[...new Set(edges.filter(e=>e.to==='dim:'+d&&e.relation==='produced_by')
      .map(e=>(byId[e.from]||{}).label||e.from))];
    const tbl=[...new Set(edges.filter(e=>e.to==='dim:'+d&&e.relation==='derived_from')
      .map(e=>(byId[e.from]||{}).label||e.from))];
    const conf=[...new Set(edges.filter(e=>e.relation==='confusable'&&(e.to==='dim:'+d||e.from==='dim:'+d))
      .map(e=>(e.to==='dim:'+d?(byId[e.from]||{}):(byId[e.to]||{})).label||''))].filter(Boolean);
    const src=prod.length?`<span class="chip ok">${prod.map(ESC).join(', ')}</span>`
                         :(tbl.length?`<span class="chip grey">Table ${tbl.map(ESC).join(', ')}</span>`
                                     :`<span class="chip warn">No Source</span>`);
    const isNow = window.__scope && window.__scope.active && window.__scope.label===d;
    // Study companion's four layers: stage (tool-set progression) / subject (entity domain) /
    // chapter (topic domain) → knowledge point
    const dt=(byId['dim:'+d]||{}).detail||{};
    const sv=(dt.subject_values||[]);
    const layer=`<td>${dt.stage_label?ESC(dt.stage_label):'—'}</td>`
      + `<td>${dt.subject_label?`<span class="chip grey" title="${ESC(sv.join(', ')||'No Values')}">${ESC(dt.subject_label)}`
          + (sv.length?` · ${sv.length} Values`:'')+`</span>`:'—'}</td>`
      + `<td>${dt.chapter?ESC(dt.chapter):'—'}</td>`;
    return `<tr${isNow?' class="now"':''}><td class="mono">${ESC(d)}</td>${layer}<td>${src}</td>
      <td>${conf.length?conf.map(c=>`<span class="chip warn">${ESC(c)}</span>`).join(''):'<span class="chip grey">—</span>'}</td>
      <td><button class="btn ${isNow?'':'btn-secondary'}" data-scope-set="${ESC(d)}"
        style="padding:4px 10px;font-size:12px">${isNow?'✓ Practicing':'Practice This Knowledge Point'}</button></td></tr>`;
  }).join('');

  const sm=g.summary||{};
  return scopeBarHtml() + pendHtml + `<div class="panel__head"><h2>Dimension → Tool → Table</h2>
      <span class="hint">${sm.dimensions||0} Dimensions · ${sm.tools||0} Tools · ${sm.tables||0} Tables · ${sm.edges||0} Edges</span></div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;background:#fff;border-radius:8px">
      ${colTitle}${es}${ns}
    </svg>
    <div class="note info" style="margin-top:10px">The error-feedback mapping "which tool produces this dimension"
      comes from this graph. It is declared by <span class="mono">answer_spec.tool</span>, not guessed by the model.
      Isolated nodes (no edges) are hidden: VLML has nine tools, and only tools actually used appear.
      <br><b>Click any dimension</b> (or the table button) to set the practice scope; the question generator then stays within it.</div>
    <table class="tbl" style="margin-top:12px">
      <thead><tr><th>Knowledge Point (Dimension)</th><th>Stage · Tool-Set Progression</th><th>Subject · Entity Domain</th>
        <th>Chapter · Topic Domain</th><th>Produced By</th><th>Confusable</th><th>Practice</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <div class="note" style="margin-top:8px;font-size:12px;color:var(--muted)">
      Layers follow the study companion: <b>Stage → Subject → Chapter → Knowledge Point</b>.
      <b>Stage</b> = minimum tool set required for the knowledge point (raw event table → derived aggregation
      → insight SQL → reporting tool); <b>Subject</b> = event / team / player / map (derived from real columns,
      not handwritten); <b>Chapter</b> = topic domain under the entity domain (first blood / economy / pistol round / losing streak…).</div>
    ${subjectHtml(g)}`;
}

/* Value list (**not** a "subject registry")
   ------------------------------------------------------------------
   Study companion `entry_tutor_question_entries.py:1043-1051`:
       store.list_topics(5000, scope.subject, scope.stage,
                         chapter=scope.chapter, unit=scope.unit,
                         course_family=scope.course_family)
   These keys are all list_topics filters; there is one shared knowledge-point library.
   Switching events or teams therefore changes only a filter (and tests transfer: can the same knowledge point
   be computed for another team?). Knowledge points and tool sets are not rebuilt or reorganized.
   Event ID ≈ study companion course_family (version/source); concrete team/player/map value ≈ runtime `entity`
   (optional and excluded from the topics table). */
function subjectHtml(g){
  const subs=g.subjects||[], sCnt=g.subject_counts||{}, sUn=g.subject_unassigned||[];
  const KIND={series:'Event',team:'Team',map:'Map',player:'Player',
              tournament:'Event Overview',custom:'Custom'};
  const rows=subs.map(s=>`<tr>
      <td>${ESC(KIND[s.kind]||s.kind)}</td>
      <td class="mono">${ESC(s.value)}</td>
      <td>${s.source==='human'
            ?'<span class="chip ok">User Added</span>'
            :'<span class="chip grey">Discovered</span>'}</td>
      <td>${s.note?ESC(s.note):'<span style="opacity:.5">—</span>'}</td>
      <td>${(sCnt[s.id]||0)||'<span style="opacity:.5">0</span>'}</td>
      <td>${s.source==='human'?`<button class="btn btn-secondary" data-subject-del="${ESC(s.id)}"
            style="padding:3px 8px;font-size:11px">Delete</button>`:''}</td></tr>`).join('');
  const un=sUn.length?`<div class="note warn" style="margin-top:8px">
      <b>${sUn.length} Unlabeled Scopes</b> (observed in runs, but the value is missing from this list;
      add it above to assign them immediately):
      ${sUn.slice(0,12).map(x=>`<span class="chip grey">${ESC(x)}</span>`).join(' ')}</div>`:'';
  return `<div style="margin-top:16px">
    <div class="panel__head"><h2>Available Values (Analysis Scope)</h2>
      <span class="hint">Database <span class="chip grey">${ESC(g.data_source||'—')}</span>
        · ${subs.length} Values · ${sUn.length} Unlabeled Scopes</span></div>
    <div class="note info" style="margin-bottom:10px">
      These are <b>filter values</b>, not a subject hierarchy. Switching events or teams changes only the data-retrieval scope
      (also a transfer test: can the same knowledge point be computed for another team?).
      <b>Knowledge points and tool sets are shared; they are not rebuilt or reorganized.</b>
      <br>Study companion equivalent: <span class="mono">list_topics(subject, stage, chapter, unit,
      course_family)</span> arguments are all <b>filters</b>; concrete team/player values correspond to runtime
      <span class="mono">entity</span> (optional and outside the knowledge structure).
      <br>Discovered values come from the current database; user-added values remain after switching databases.</div>
    <table class="tbl"><thead><tr><th>Type</th><th>Value</th><th>Source</th>
      <th>Notes</th><th>Entries</th><th></th></tr></thead>
      <tbody>${rows||'<tr><td colspan="6" style="opacity:.6">(Empty — Sync from Data Source or add a value)</td></tr>'}</tbody></table>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
      <select id="subjKind">${['series','team','map','player','custom']
        .map(k=>`<option value="${k}">${KIND[k]}</option>`).join('')}</select>
      <input id="subjValue" placeholder="Value, e.g. NRG / 706349" style="width:200px">
      <input id="subjNote" placeholder="Notes (Optional)" style="width:200px">
      <button class="btn btn-primary" data-subject-add="1" style="padding:5px 12px">Add Value</button>
      <button class="btn btn-secondary" data-subject-sync="1" style="padding:5px 12px">Sync from Current Database</button>
    </div>${un}</div>`;
}

/* Practice scope bar at the top of the graph page, following study companion i18n:
   ui.knowledge.practice_topic = "Practice This Knowledge Point"
   ui.practice.next_step.choose_scope = "Choose Practice Scope" */
function scopeBarHtml(){
  const sc=window.__scope||{active:false};
  if(sc.active){
    return `<div class="scopebar" style="margin:0 0 14px">
      <span class="lbl">Practice Scope</span>
      <span class="chip ok">${ESC(sc.label)}</span>
      <span class="chip grey">${ESC((sc.topics||[]).join(', '))}</span>
      <button class="btn btn-primary" data-scope-practice="1"
        style="padding:6px 14px;font-size:13px">▶ Practice This Scope</button>
      <button class="btn btn-secondary" data-scope-clear="1"
        style="padding:6px 12px;font-size:13px">Clear Scope</button>
      <span class="hint">${ESC(sc.point||'')}</span>
    </div>`;
  }
  if(sc.invalidated){
    return `<div class="scopebar" style="margin:0 0 14px">
      <span class="lbl">Practice Scope</span>
      <span class="chip warn">Invalidated</span><span class="chip grey">${ESC(sc.label||'')}</span>
      <span class="hint">${ESC(sc.reason||'')} — Select another dimension</span>
    </div>`;
  }
  return `<div class="scopebar" style="margin:0 0 14px">
    <span class="lbl">Practice Scope</span>
    <span class="chip grey">Not Set · Question Generator Chooses</span>
    <span class="hint">Click any graph dimension or Practice This Knowledge Point below to restrict the cockpit to that scope</span>
  </div>`;
}
function pGraph(s){
  const id='graphBody';
  loadGraph().then(g=>{ const el=document.getElementById(id); if(el) el.innerHTML=renderGraph(g); });
  return `<div class="panel" id="${id}"><div class="empty">Loading Knowledge Graph…</div></div>`;
}

/* ---------------- Practice Scope Actions ----------------
   Event delegation: the graph renders asynchronously (loadGraph().then writes innerHTML).
   Attaching onclick in render() would target empty nodes, so listen on document. */
async function setScope(dim){
  try{
    const r=await fetch('/api/practice-scope',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'set', dim:dim})});
    const d=await r.json();
    uiEvent('practice_scope_set',
      {\u7ef4\u5ea6: dim, \u5173\u8054\u9898: (d.scope&&d.scope.topics||[]).join(', ')||'(None)',
       revision: d.scope&&d.scope.scope_revision},
      d.ok ? ('Practice scope set → '+(d.scope&&d.scope.topics||[]).join(', '))
           : ('Failed: '+(d.error||d.code||'Unknown')));
    if(!d.ok) alert('Cannot set scope: '+(d.error||'Unknown'));
  }catch(e){ alert('Set scope failed: '+e); }
  finally{ await load(); }
}
async function clearScope(){
  try{
    const r=await fetch('/api/practice-scope',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'clear'})});
    const d=await r.json();
    uiEvent('practice_scope_clear',{\u539f\u8303\u56f4: d.cleared||'—'},
      d.ok?'Cleared · Question generator restored':'Failed');
  }catch(e){ alert('Clear failed: '+e); }
  finally{ await load(); }
}
async function addSubject(){
  const kind=(document.getElementById('subjKind')||{}).value||'';
  const value=((document.getElementById('subjValue')||{}).value||'').trim();
  const note=((document.getElementById('subjNote')||{}).value||'').trim();
  if(!value){ alert('Enter a subject value first (for example, NRG or an event ID)'); return; }
  try{
    const r=await fetch('/api/subject/add',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({kind:kind,value:value,note:note})});
    const d=await r.json();
    uiEvent('subject_add',{\u5b66\u79d1:kind+'='+value,\u8865\u5f52\u5c5e\u6761\u76ee:d.moved||0},
      d.ok?('Registered'+(d.existed?' (Already Exists)':'')
           +((d.moved)?'; assigned '+d.moved+' existing entries':'')
           +(!d.ok?('Failed: '+(d.error||'')):''))
      :('Failed: '+(d.error||'Unknown')));
    if(!d.ok) alert('Registration failed: '+(d.error||'Unknown'));
  }catch(e){ alert('Registration failed: '+e); }
  finally{ await load(); }
}
async function syncSubjects(){
  try{
    const r=await fetch('/api/subject/sync',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({})});
    const d=await r.json();
    uiEvent('subject_sync',{\u65b0\u767b\u8bb0:d.added||0},
      d.ok?('Added '+d.added+', '+d.total+' total'):('Failed: '+(d.reason||'')));
    if(!d.ok) alert('Sync failed: '+(d.reason||'Unknown'));
  }catch(e){ alert('Sync failed: '+e); }
  finally{ await load(); }
}
async function delSubject(id){
  try{
    const r=await fetch('/api/subject/del',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:id})});
    const d=await r.json();
    uiEvent('subject_del',{\u5b66\u79d1:id}, d.ok?'Deleted':'Not Found');
  }catch(e){ alert('Delete failed: '+e); }
  finally{ await load(); }
}
document.addEventListener('click',e=>{
  const s=e.target.closest('[data-scope-set]');
  if(s){ setScope(s.dataset.scopeSet); return; }
  if(e.target.closest('[data-scope-clear]')){ clearScope(); return; }
  if(e.target.closest('[data-scope-practice]')){ pilotStart(); return; }
  if(e.target.closest('[data-subject-add]')){ addSubject(); return; }
  if(e.target.closest('[data-subject-sync]')){ syncSubjects(); return; }
  const sd=e.target.closest('[data-subject-del]');
  if(sd){ delSubject(sd.dataset.subjectDel); return; }
});

/* ---------------- Adaptive Practice (Study Companion Question Generator / Graph Port) ---------------- */
function pAdaptive(s){
  const tg=s.teaching||{};
  const nodes=tg.nodes||[];
  const mastered=nodes.filter(n=>n.mastery!==null&&n.mastery>=0.8).length;
  const blocked=nodes.filter(n=>n.blocked).length;
  // Card content comes from client state window.__oq / window.__oqGradeHTML and is not written to server state;
  // otherwise, polling would rebuild the stage every four seconds and erase it.
  const oq=window.__oq||null;
  const cardDisp=oq?'block':'none';
  const topic=oq?(oq.topic_name||oq.topic||'Knowledge Point'):'Knowledge Point';
  const meta=oq?(`Difficulty ${oq.difficulty||'?'} · Type ${oq.question_type||'?'} · `
                +((oq.selection&&oq.selection.reason)||'')):'';
  const ques=oq?(oq.question||'(No Question)'):'';
  const hint=oq?(ESC(oq.hint||'(No Special Guidance)')+'<br><b>Suggested Steps:</b><br>'
                +(oq.solution_steps||[]).map(ESC).join('<br>')):'';
  const sel=oq?((oq.selection&&oq.selection.explanation)||''):'';
  const gradeHTML=window.__oqGradeHTML||'';
  const gradeDisp=gradeHTML?'block':'none';
  return `<div class="panel">
  <div class="panel__head"><h2>🧩 Adaptive Practice · Teaching Graph</h2>
    <span class="hint">Prerequisite gating + weakest-first + question-type rotation (ported from the study companion)</span></div>
  <div class="row" style="gap:10px;flex-wrap:wrap">
    <span class="chip ${mastered?'ok':''}">Mastered ${mastered}/${nodes.length}</span>
    <span class="chip ${blocked?'warn':''}">Prerequisites Not Met ${blocked}</span>
    <span class="chip">Target Difficulty ${tg.difficulty_target||1}</span>
    <span class="chip grey">Knowledge Points ${tg.total||nodes.length}</span>
  </div>
  <div class="note info">Questions use <b>real entities from the current database</b> (series/team/map follows the database).
    Repeated practice on one knowledge point <b>rotates question types</b> and explicitly differs from the previous question.</div>
  <div class="row" style="margin-top:8px">
    <button class="btn btn-primary" id="oqNext">▶ Next Question</button>
    <span class="hint" id="oqSel">${ESC(sel)||'Selection rationale appears here'}</span>
  </div>
  <div id="oqCard" class="panel" style="margin-top:12px;display:${cardDisp}">
    <div class="panel__head"><h3 id="oqTopic">${ESC(topic)}</h3>
      <span class="hint" id="oqMeta">${ESC(meta)}</span></div>
    <div id="oqQues" class="q-text">${ESC(ques)}</div>
    <details style="margin:6px 0"><summary class="hint">📌 Guidance for the Model (Pitfalls / Table Retrieval Steps)</summary>
      <div id="oqHint" class="note">${hint}</div></details>
    <textarea id="oqAns" class="ta" rows="3" placeholder="Enter an answer (free text; SQL / tool output accepted)"></textarea>
    <div class="row" style="margin-top:6px">
      <button class="btn btn-primary" id="oqSubmit">Submit for Grading</button>
      <button class="btn btn-secondary" id="oqReveal">Show Reference Answer</button>
    </div>
    <div id="oqGrade" class="note" style="margin-top:8px;display:${gradeDisp}">${gradeHTML}</div>
  </div>
</div>`;
}
let _oqLastQuestion='';
async function oqLoadGraph(){
  try{ const r=await fetch('/api/teaching-graph?t='+Date.now()); return await r.json(); }
  catch(e){ return null; }
}
async function oqNext(){
  const btn=document.getElementById('oqNext'); btn.disabled=true; btn.textContent='Generating…';
  try{
    const r=await fetch('/api/open-quiz/next',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({last_question:_oqLastQuestion})});
    const d=await r.json();
    if(!d.topic_id){ alert('Question generation failed: '+(d.error||'Unknown')); return; }
    _oqLastQuestion=d.question||'';
    window.__oq=d;
    window.__oqGradeHTML='';   // Clear the previous grade for a new question
    const _a=document.getElementById('oqAns'); if(_a) _a.value='';  // Clear prior answer so polling restoration does not refill it
    await load();              // Re-render adaptive page from window.__oq so polling cannot erase the card
  }catch(e){ alert('Request failed: '+e); }
  finally{ btn.disabled=false; btn.textContent='Next Question'; }
}
async function oqGrade(){
  const d=window.__oq; if(!d){ return; }
  const ua=document.getElementById('oqAns').value.trim();
  if(!ua){ alert('Answer before submitting'); return; }
  const btn=document.getElementById('oqSubmit'); btn.disabled=true;
  try{
    const r=await fetch('/api/open-quiz/grade',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:d, user_answer:ua, topic_id:d.topic})});
    const j=await r.json();
    const g=j.grade||{};
    window.__oqGradeHTML=
      `<b>Score: ${(+g.score*100).toFixed(0)}%</b> · ${ESC(g.feedback||'')}<br>`+
      (g.hits&&g.hits.length?('Hits: '+g.hits.map(ESC).join(', ')+'<br>'):'')+
      (g.misses&&g.misses.length?('Missing / Incorrect: '+g.misses.map(ESC).join(', ')):'');
    await load();  // Refresh mastery and render grade from window.__oqGradeHTML so polling cannot erase it
  }catch(e){ alert('Grading failed: '+e); }
  finally{ btn.disabled=false; }
}
function oqReveal(){
  const d=window.__oq; if(!d) return;
  window.__oqGradeHTML=
    `<b>Reference Answer:</b>${ESC(d.answer||'(None)')}<br>`+
    `<b>Accepted Answers:</b>`+(d.accepted_answers||[]).map(ESC).join(', ');
  // Write directly to the DOM for immediate feedback and persist for the next polling re-render
  const g=document.getElementById('oqGrade');
  if(g){ g.style.display='block'; g.innerHTML=window.__oqGradeHTML; }
}

const PANELS={overview:pOverview,practice:pPractice,import:pImport,trace:pTrace,skill:pSkill,graph:pGraph,voyager:pVoyager,adaptive:pAdaptive,console:pConsole};

/* ---------------- Human Import (Attach Events Only in Import Section) ---------------- */
function fillTopics(s){
  // The cockpit no longer has a question selector (scope selects questions); only import topic routing remains
  const sel=document.getElementById('topic');
  if(!sel) return;
  // dataset.loaded prevents duplicate fills but also blocked new questions: after import generated a question,
  // it never appeared until manual refresh. Rebuild when the question count changes. Normal four-second polling
  // remains stable, and new questions appear immediately.
  const n=(s.tasks||[]).filter(t=>t.topic_id&&t.topic_id!=='?').length;
  if(sel.dataset.loaded && String(n)===sel.dataset.taskn) return;
  const keep=sel.value;
  sel.innerHTML='<option value="">Auto Match (Keyword Routing)</option>'+
    (s.tasks||[]).filter(t=>t.topic_id&&t.topic_id!=='?').map(t=>
      `<option value="${ESC(t.topic_id)}">${ESC(t.topic_id)} · Difficulty ${t.difficulty}</option>`).join('');
  sel.value=keep; sel.dataset.loaded='1'; sel.dataset.taskn=String(n);
}

function importsList(s){
  const imps=s.imports||[];
  if(!imps.length) return '<div class="empty">No human imports yet.</div>';
  return imps.slice().reverse().map(im=>`
    <div class="nocmp" style="margin-top:8px">
      <div class="nocmp-tag">Excluded from Mastery · validated_target: false</div>
      <div class="nocmp-sub" style="margin-top:0;font-size:12px">
        ${ESC(String(im.ts||'').replace('T',' '))}
        ${im.topic_id?`<span class="chip">${ESC(im.topic_id)}</span>`:'<span class="chip bad">Not Assigned to a Question</span>'}
        ${im.topic_hit?`<span class="chip grey">Matched "${ESC(im.topic_hit)}"</span>`:''}
        ${(im.trajectory||[]).length?`<span class="chip grey">Orchestration: ${(im.trajectory||[]).map(t=>ESC(t)).join(' → ')}</span>`:'<span class="chip bad">No Data Retrieved</span>'}
        ${graphLinkChips(im.graph_link)}
        <span class="chip grey">${im.facts_count} Facts</span>
      </div>
      <div class="nocmp-body" style="margin-top:6px;font-size:13px">${ESC(im.question)}</div>
      ${im.narrative?`<div class="nocmp-sub">${ESC(im.narrative)}</div>`:''}
      ${(im.insights||[]).length?`<div class="nocmp-sub"><b>Insights:</b><ul>${im.insights.map(i=>`<li>${ESC(i)}</li>`).join('')}</ul></div>`:''}
      ${(im.caveats||[]).length?`<div class="nocmp-sub"><b>Data Limitations:</b>${im.caveats.map(c=>ESC(c)).join('; ')}</div>`:''}
      ${consumeBlock(im)}
    </div>`).join('');
}

/* Consumption-specific block: answer + reused capability or solved live + mastery + **approach comparison**.
   It must appear in history, not only the submission receipt. Consumption runs in the background (1–3 minutes end to end),
   so the receipt only says accepted and results arrive through four-second polling. Without history, users never see
   Voyager's answer or the grading basis. */
function consumeBlock(im){
  if(im.mode!=='voyager_consume') return '';
  const pct=v=>(v===null||v===undefined)?'—':Math.round(v*100)+'%';
  const judged = im.coverage!==null && im.coverage!==undefined;
  return `
    <div style="margin-top:6px;border-top:1px dashed var(--line);padding-top:6px">
      <span class="chip ${im.reused?'ok':'warn'}">${im.reused?'Reused Learned Recipe':'Solved Live'}</span>
      ${im.mastered
        ? `<span class="chip ok">Mastered · ${ESC(im.mastered_total)}/${ESC(im.ability_total)} Questions</span>`
        : '<span class="chip bad">No Answer · Not Added to Capability Library</span>'}
      ${judged
        ? (im.judged
            ? `<span class="chip ${im.coverage>=0.95?'ok':(im.coverage>0?'warn':'bad')}">Agreement ${pct(im.coverage)}</span>
               <span class="chip ${im.voyager_sound?'ok':'bad'}">${im.voyager_sound?'Sound Approach':'Material Flaw'}</span>`
            : `<span class="chip grey">Different metrics; not actually judged (usually referee drift)</span>
               <span class="chip ${im.voyager_sound?'ok':'bad'}">${im.voyager_sound?'Voyager Approach Sound':'Voyager Approach Flawed'}</span>`)
        : '<span class="chip grey">Not Judged (VLML retrieved no data)</span>'}
      ${(im.coverage_referee!==null&&im.coverage_referee!==undefined)
        ? `<span class="chip grey">Hard Metric ${pct(im.coverage_referee)}</span>`:''}
      <span class="chip grey">Tools ${ESC(im.ok_tool_calls||0)} · ${ESC(im.rows||0)} Rows</span>
    </div>
    ${im.answer?`<div style="margin-top:6px"><b>Voyager Answer:</b>
      <div class="nocmp-body" style="margin-top:4px">${ESC(im.answer)}</div></div>`:''}
    ${im.method_reason?`<div style="margin-top:5px;font-size:12px;color:var(--muted)">
      <b>Approach Comparison:</b>${ESC(im.method_reason)}${im.method_diff?'; Difference: '+ESC(im.method_diff):''}
      <br>Voyager retrieved ${ESC(im.rows||0)} rows; independent VLML retrieved ${ESC(im.referee_rows||0)} rows.
      The referee only retrieves data; the LLM directly compares both SQL queries and outputs.</div>`:''}
    ${im.skipped?`<div style="margin-top:5px;font-size:12px;color:var(--muted)">${ESC(im.skipped)}</div>`:''}`;
}

/* What the human import added to the graph. Inclusion criterion: whether VLML can complete the work using
   the table-covered tool set. Inclusion creates an edge, not a node. This row keeps it visible on the dashboard
   instead of appearing once in a terminal log. */
function graphLinkChips(gl){
  if(!gl || !Object.keys(gl).length) return '';
  if(!gl.covered)
    return '<span class="chip bad">Not Added to Graph: Tool Set Not Covered</span>';
  return '<span class="chip ok">Added to Graph (Edge Only, No Node)</span>'
    + `<span class="chip grey">Coverage Layer ${ESC(gl.cover_level||'')}`
    + ` | Insights ${(gl.insights||[]).length}/${ESC(gl.insights_total||0)}`
    + ` | Tables ${(gl.tables||[]).length}</span>`;
}

/* Background task progress bar: consumption takes 1–3 minutes. Without this, users see a spinning button
   and cannot distinguish "running" from "stalled." */
function coachRunningBar(s){
  if(!s.coach_running)
    return s.coach_error
      ? `<div class="note" style="margin:8px 0"><b>Previous Consumption Failed:</b>${ESC(s.coach_error)}</div>`
      : '';
  return `<div class="note info" style="margin:8px 0">
    <b>Voyager Is Answering:</b>${ESC(s.coach_question||'')}
    <span class="chip grey">${ESC(s.coach_elapsed)} Seconds</span>
    <div style="font-size:12px;color:var(--muted);margin-top:4px">
      This chains more than ten model calls (find recipe → fill entity values → retrieve data → independent VLML retrieval →
      compare approaches → LLM narrative insights) and usually takes 1–3 minutes.
      The result appears automatically in import history below.
    </div></div>`;
}

/* Acceptance receipt: clearly state that work is running and where to find the result. */
function renderImportBusy(res){
  const box=document.getElementById('result'); if(!box) return;
  box.innerHTML=`<div class="note info" style="margin-top:10px">
    ${res.busy
      ? `<b>Previous Question Still Running:</b> ${ESC(res.error||'')}`
      : `<b>Accepted · Voyager Is Answering</b>
         <div style="margin-top:5px">${ESC(res.message||'')}</div>
         <div style="margin-top:6px;font-size:12px;color:var(--muted)">
           It is processing sequentially: find learned recipe → fill entity values → retrieve data →
           independent VLML retrieval → compare approaches → LLM narrative insights.
           This requires more than ten model calls and usually takes one or two minutes.</div>`}
  </div>`;
}

function renderImportResult(res){
  const box=document.getElementById('result'); if(!box) return;
  if(!res){ box.innerHTML=''; return; }
  if(!res.ok){ box.innerHTML=`<div class="note" style="margin-top:10px"><b>Import Failed:</b>${ESC(res.error)}</div>`; return; }
  const facts=(res.facts||[]).map(f=>
    `<div class="fact"><b>${ESC(f.subject||'')}</b> · ${ESC(f.dimension||'')} =
       <span class="fv">${ESC(f.value)}</span>${f.base!==null&&f.base!==undefined?` <span style="color:var(--muted)">(base ${ESC(f.base)})</span>`:''}</div>`).join('');
  // Voyager capability-use disclosure for mode=voyager: recalled memories, reused skills, and scene injection
  const vc=res.voyager_context||null;
  const vcChips=vc?`
      <span class="chip ok">Voyager Answer</span>
      <span class="chip grey">${vc.memory_count||0} Memories Recalled</span>
      ${(vc.skills||[]).length?`<span class="chip grey">Reused Skills: ${vc.skills.map(k=>ESC(k.name)).join(', ')}</span>`:''}
      ${vc.scene?'<span class="chip grey">Scene Injected (Tools/Tables base·ext Layers)</span>':''}`:'';
  // Consumption pipeline (voyager_vlml/consume.py) disclosure.
  // Key difference: the old path inserted only skill-library **text** into VLML orchestration prompts.
  // The consumption pipeline retrieves a stored SQL recipe, fills entity values from this question, and executes it.
  // Whether capability was reused or the answer was solved live must be obvious; claiming reuse with the wrong recipe
  // is worse than admitting a live solution.
  const isConsume = res.mode === 'voyager_consume';
  const consumeChips = isConsume ? `
      <span class="chip ${res.reused?'ok':'warn'}">${res.reused?'Reused Learned Recipe':'Solved Live (No Applicable Recipe)'}</span>
      <span class="chip grey">${res.skills_found||0} Skill Library Hits</span>
      ${(res.used_skills||[]).length?`<span class="chip grey">Used: ${res.used_skills.map(k=>ESC(k.name||k.topic||'')).join(', ')}</span>`:''}
      <span class="chip grey">Tool Calls ${res.ok_tool_calls||0}/${res.tool_calls||0} Succeeded · ${res.rows||0} Rows</span>` : '';
  const pct = v => (v===null||v===undefined) ? '—' : Math.round(v*100)+'%';
  // Coverage/mastery are calculated **only on consumption** (practice is ungraded) as user-facing quality metrics.
  // Method set 2026-10-13: do not require each side to reduce results into {subject,dimension,value} for key matching.
  // Instead, directly compare Voyager and VLML methods: each runs SQL, then the LLM checks whether both use the same
  // metric and whether either has material flaws. Agreement becomes coverage. The old fragile reduction step failed
  // for tabular answers, causing empty coverage and answers even after retrieving 85 rows.
  const cmpBlock = isConsume ? `
    <div style="margin-top:9px">
      <b>Approach Comparison (Grading Basis):</b>
      ${res.method_compare
        ? `<div style="margin-top:4px">
             <span class="chip ${res.same_metric?'ok':'warn'}">${res.same_metric?'Same Metric':'Different Metrics'}</span>
             <span class="chip ${res.voyager_sound?'ok':'bad'}">${res.voyager_sound?'Voyager Approach Sound':'Voyager Approach Flawed'}</span>
             <span class="chip grey">Agreement ${pct(res.coverage)}</span>
           </div>
           ${res.method_reason?`<div style="margin-top:4px;font-size:12px"><b>Basis:</b>${ESC(res.method_reason)}</div>`:''}
           ${res.method_diff?`<div style="margin-top:4px;font-size:12px"><b>Difference:</b>${ESC(res.method_diff)}</div>`:''}
           <div style="margin-top:4px;font-size:12px;color:var(--muted)">
             Voyager retrieved ${ESC(res.rows||0)} rows; independent VLML retrieved ${ESC(res.referee_rows||0)} rows.
             The referee retrieves without restructuring; the LLM directly compares both SQL queries and outputs.
           </div>`
        : `<div style="margin-top:4px;font-size:12px;color:var(--muted)">
             ${ESC(res.skipped||'VLML retrieved no data, so there is no second approach to compare. Coverage remains empty rather than using a false zero.')}</div>`}
      ${res.coverage_referee!==null&&res.coverage_referee!==undefined
        ? `<div style="margin-top:4px;font-size:12px"><b>Hard Metric (Question-Library Rubric):</b>
             <span class="chip ${res.coverage_referee>=0.95?'ok':'warn'}">Coverage ${pct(res.coverage_referee)}</span>
             ${ESC(res.verdict_referee||'')} — Shown beside the main metric without conversion.</div>`:''}
    </div>` : '';
  const scoreBlock = isConsume ? `
    <div style="margin-top:9px">
      <b>Coverage (Quality Score; User-Facing Only, Not Fed Back to Voyager):</b>
      ${res.coverage===null||res.coverage===undefined
        ? `<div style="margin-top:4px">Not Calculated — ${ESC(res.skipped||'No second approach to compare')}</div>`
        : `<div style="margin-top:4px">
             ${res.judged
               ? `<span class="chip ${res.coverage>=0.95?'ok':(res.coverage>0?'warn':'bad')}">Agreement ${pct(res.coverage)}</span>`
               : `<span class="chip grey">Different Metrics · Not Actually Judged</span>`}
             <span class="chip grey">Score ${ESC(res.score)}</span>
             <span class="chip grey">Verdict ${ESC(res.verdict||'')}</span>
             ${res.judge?`<span class="chip grey">Metric ${ESC(res.judge)}</span>`:''}
           </div>
           ${res.judged?'':'<div style="margin-top:4px;font-size:12px;color:var(--muted)">The approaches measure different quantities (usually referee drift), so this question was <b>not actually judged</b>. This is not a Voyager error and does not count as zero.</div>'}
           ${(res.covered_points||[]).length?`<div style="margin-top:4px;font-size:12px"><b>Covered:</b>${res.covered_points.map(c=>ESC(c)).join('; ')}</div>`:''}
           ${(res.missing_points||[]).length?`<div style="margin-top:4px;font-size:12px"><b>Missing:</b>${res.missing_points.map(c=>ESC(c)).join('; ')}</div>`:''}
           ${(res.unjudgeable||[]).length?`<div style="margin-top:4px;font-size:12px"><b>Material Flaws:</b>${res.unjudgeable.map(c=>ESC(c)).join('; ')}</div>`:''}`}
      ${cmpBlock}
    </div>` : '';
  box.innerHTML=`<div class="note info" style="margin-top:10px">
    <div style="margin-bottom:6px">
      ${res.topic_id?`<span class="chip ok">Assigned to ${ESC(res.topic_id)}</span>`:'<span class="chip bad">Not Assigned → Does Not Drive Question Generation</span>'}
      ${res.topic_hit?`<span class="chip grey">Matched "${ESC(res.topic_hit)}"</span>`:''}
      <span class="chip grey">validated_target: ${ESC(res.validated_target)}</span>
      ${(res.trajectory||[]).length?`<span class="chip grey">VLML Analysis: ${(res.trajectory||[]).map(t=>ESC(t)).join(' → ')}</span>`:'<span class="chip bad">No Data Retrieved</span>'}
      ${vcChips}
      ${consumeChips}
      ${graphLinkChips(res.graph_link)}
    </div>
    ${isConsume&&res.reuse_note?`<div style="margin-top:5px;font-size:12px"><b>Recipe Use:</b>${ESC(res.reuse_note)}</div>`:''}
    ${isConsume?`<div style="margin-top:9px"><b>Mastery (Capability Set):</b>
      ${res.mastered
        ? `<span class="chip ok">Mastered · Added to Capability Library</span>
           <span class="chip grey">${ESC(res.mastered_total)} / ${ESC(res.ability_total)} Questions</span>
           ${res.skill_action?`<span class="chip grey">${ESC(res.skill_action)}</span>`:''}
           <div style="font-size:12px;color:var(--muted);margin-top:4px">
             Mastery criterion is whether an answer was produced (tools ran + data retrieved + answer generated),
             <b>not coverage</b>. Coverage is quality reference only and does not determine whether the question was learned.</div>`
        : `<span class="chip bad">No Answer · Not Added to Capability Library</span>
           <div style="font-size:12px;color:var(--muted);margin-top:4px">${ESC(res.skipped||'')}</div>`}
    </div>`:''}
    ${scoreBlock}
    ${res.skipped?`<div><b>No Explanation Produced:</b>${ESC(res.skipped)}</div>`:''}
    ${(res.errors||[]).length?`<div style="margin-top:5px"><b>Tool Errors:</b>${(res.errors||[]).map(e=>ESC(e)).join('; ')}</div>`:''}
    ${isConsume&&res.answer?`<div style="margin-top:9px"><b>Voyager Answer:</b>
      <div class="nocmp-body" style="margin-top:4px">${ESC(res.answer)}</div></div>`:''}
    ${facts?`<div style="margin-top:9px"><b>VLML Analysis · Retrieved Facts (${(res.facts||[]).length}):</b>${facts}
      <div style="font-size:12px;color:var(--muted);margin-top:5px">
        ${isConsume
          ? 'Voyager retrieved these facts using accumulated capability (shown so you can inspect the output). <b>Grading does not key-match them</b>. Agreement above compares both SQL queries and results directly, so tabular answers can be judged without scalar reduction.'
          : 'Note: facts on this path are <b>not verified by the referee</b>, and dimension names are open-ended. They are weaker than referee-verified Voyager facts and cannot serve as a criterion.'}</div></div>`:''}
    ${(vc&&vc.memory_texts&&vc.memory_texts.length)?`<div style="margin-top:9px;font-size:12px;color:var(--muted)">
      <b>Memories Used (mem0 Recall):</b><ul style="margin:4px 0 0 16px">${vc.memory_texts.map(m=>`<li>${ESC(String(m).slice(0,120))}</li>`).join('')}</ul></div>`:''}
    ${res.narrative?`<div class="nocmp" style="margin-top:9px">
        <div class="nocmp-tag">Excluded from Comparison · comparable: false</div>
        <div class="nocmp-body">${ESC(res.narrative)}</div>
        ${(res.insights||[]).length?`<div class="nocmp-sub"><b>Insights:</b><ul>${res.insights.map(i=>`<li>${ESC(i)}</li>`).join('')}</ul></div>`:''}
        ${(res.caveats||[]).length?`<div class="nocmp-sub"><b>Data Limitations:</b>${res.caveats.map(c=>ESC(c)).join('; ')}</div>`:''}
      </div>`:''}
    <div style="margin-top:8px;font-size:12px">Written to the causal timeline as a control fact; the question generator can see it.
      ${isConsume
        ? '<b>Coverage and mastery are calculated on the consumption pipeline</b> (practice is ungraded). Both are user-facing only and are not fed back to Voyager.'
        : '<b>Excluded from mastery</b>: visible to the question generator, invisible to grading.'}</div>
  </div>`;
}

let pvTimer=null;
function preview(){
  const q=document.getElementById('q'); const hit=document.getElementById('hit');
  if(!q||!hit) return;
  const v=q.value.trim();
  if(!v){ hit.className='hit'; hit.textContent='Topic routing results appear here.'; return; }
  fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:v})})
    .then(r=>r.json()).then(d=>{
      if(d.matched){ hit.className='hit';
        hit.innerHTML=`Will be assigned to <b>${ESC(d.topic_id)}</b> (matched "${ESC(d.hit)}") · ${ESC(String(d.question).slice(0,40))}…`;
      }else{ hit.className='hit no';
        hit.innerHTML=`<b>No Matching Question</b> — It will still be recorded and explained, but topic_id is empty,
          so the question generator cannot read it and it <b>does not drive generation</b>. You can select a topic manually.`;
      }
    }).catch(()=>{});
}

let busy=false;
async function submitImport(){
  if(busy) return;
  const q=document.getElementById('q'); if(!q) return;
  const v=q.value.trim();
  if(!v){ document.getElementById('hit').innerHTML='<b>Enter a question before submitting.</b>'; return; }
  const btn=document.getElementById('go');
  busy=true; btn.disabled=true; btn.innerHTML='<span class="spin"></span>Voyager Is Answering…';
  window.__changedFrom=(window.__lastRec||{}).topic_id||'';
  const sel=document.getElementById('topic');
  const picked=sel ? sel.value : '';
  try{
    const r=await fetch('/api/coach',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:v, topic_id:picked, mode:'voyager'})});
    const res=await r.json();
    // Background execution: once accepted, do not wait synchronously. Results enter import history via four-second polling.
    // Waiting 2–5 minutes caused browser/Caddy timeouts in testing; users saw a stuck "Answering" button and assumed
    // Voyager could not answer even though it was still running.
    if(res.accepted||res.busy){
      renderImportBusy(res);
      uiEvent('human_import',{\u95ee\u9898:v.slice(0,300),\u6a21\u5f0f:'Consumption (Background)',
        \u624b\u52a8\u6307\u5b9a\u7684\u9898: picked||'Not Specified'}, res.busy?('Rejected: previous question still running'):('Accepted · Running in Background'));
      if(!res.busy){ q.value=''; q.blur(); }
      document.getElementById('hit').textContent='Topic routing results appear here.';
      await load();
      return;
    }
    renderImportResult(res);
    uiEvent('human_import',
      {\u95ee\u9898: v.slice(0,300),
       \u6a21\u5f0f: 'voyager (memory + skills + scene injection)',
       \u624b\u52a8\u6307\u5b9a\u7684\u9898: picked || 'Not Specified (Automatic Keyword Routing)'},
      res.ok
        ? ('Assigned to '+(res.topic_id||'No Question')
           +(res.topic_hit?' (Matched "'+res.topic_hit+'")':'')
           +' · '+(res.facts||[]).length+' Facts · Orchestration '+(res.trajectory||[]).join(' → ')
           +' · '+(((res.voyager_context||{}).memory_count)||0)+' Memories Recalled'
           +' · validated_target=false · Excluded from Mastery')
        : ('Failed: '+(res.error||'Unknown')));
    q.value=''; q.blur();   // Blur so polling can refresh import history; rendering is held while the input has focus
    document.getElementById('hit').textContent='Topic routing results appear here.';
    await load();
  }catch(e){ renderImportResult({ok:false,error:String(e)}); }
  finally{ busy=false; btn.disabled=false; btn.textContent='Ask Voyager'; }
}

/* ---------------- Cockpit Controls ---------------- */
/* ---------------- UI Interaction Telemetry ----------------
   Exported Markdown must include exact UI interactions (which button and selected difficulty).
   Every button click and select change is therefore stored in ui_events.jsonl and exported first. */
const TAB_LABEL = {overview:'Overview', practice:'Practice', import:'Human Import',
                   trace:'Trajectory', skill:'Skills · Causal', graph:'Knowledge Graph', voyager:'MC Real Learning', adaptive:'Adaptive Practice', console:'Run Log'};
function uiEvent(kind, detail, result){
  try{
    fetch('/api/ui-event',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({kind:kind, detail:detail||{}, result:result||''})});
  }catch(e){}
}

async function runJob(job, opts){
  opts=opts||{};
  const b=opts.btnId?document.getElementById(opts.btnId):null;
  if(b&&b.disabled) return;
  // rounds must default to **0 (continuous)**, not 3. With `opts.rounds||3`, passing 0 is falsy and becomes 3.
  // The UI then shows Continuous but the request sends 3, so each run exits after three questions and cannot cover
  // a 29-node graph. Original Voyager learning runs continuously.
  const body={job:job, mode:opts.mode||'once',
              rounds:(opts.rounds!==undefined&&opts.rounds!==null)?+opts.rounds:0,
              units:+(opts.units||1), transfer:+(opts.transfer||0),
              use_scope:opts.use_scope!==false};
  if(b){ b.disabled=true; b.innerHTML='<span class="spin"></span>'+ESC(opts.busyText||'Starting…'); }
  try{
    const r=await fetch('/api/pilot/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    const d=await r.json();
    uiEvent('pilot_start',
      {\u4efb\u52a1:d.job_label||job, \u5355\u5143:body.units, \u8fc1\u79fb\u5bf9\u7167:body.transfer||'None', \u6a21\u5f0f:body.mode},
      d.ok?('Started · pid '+d.pid+' · '+(d.job_label||job)):('Failed: '+(d.error||'Unknown')));
    if(!d.ok){ alert('Start failed: '+(d.error||'Unknown')); }
    else { TAB='console'; localStorage.setItem('mve_tab',TAB); }
    await load();
  }catch(e){
    uiEvent('pilot_start',{\u4efb\u52a1:job},'Exception: '+e);
    alert('Start failed: '+e);
  }
  finally{ if(b){ b.disabled=false; b.textContent=opts.btnText||'▶ Start Voyager'; } }
}
async function pilotStart(){
  // Difficulty/question controls are gone; practice scope selects questions (study companion onboarding.md:82).
  // rounds = 0 → continuous, matching original voyager.py:326 while True; only Stop or --max-iterations exits.
  // rounds = 1 → run one round for review.
  // With a scope, force 1: scope pins one question, and continuous mode would repeat it forever with progress at 0/1.
  // pilot.py:_loop enforces the same gate; the dashboard disables the switch first.
  const sc=window.__scope||{active:false};
  const ck=document.getElementById('ckContinuous');
  const rounds=(ck&&ck.checked&&!sc.active)?0:1;
  await runJob('practice',{mode:'loop', rounds:rounds, use_scope:true,
    btnId:'runStart', btnText:'▶ Start Voyager', busyText:'Starting…'});
}
/* MC Strategy Learning → automatic full MVE flow:
   POST /api/mc-learn first (extract + store, synchronous and fast), then start the standard practice loop
   using the same pilot/start flow as Start Voyager. One button runs MC strategy learning → store → standard MVE. */
async function mcLearnThenRun(){
  const b=document.getElementById('mcLearn');
  if(!b||b.disabled) return;
  if(window.__pilot&&window.__pilot.running){
    alert('MVE is already running (pid '+window.__pilot.pid+').\n\n'
      +'MC strategy learning is ready. Wait for it to finish or click Stop first.');
    return;
  }
  b.disabled=true; b.innerHTML='<span class="spin"></span>MC Learning…';
  try{
    const r=await fetch('/api/mc-learn',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    if(!d.ok){ alert('MC strategy learning failed: '+(d.error||'Unknown')); return; }
    uiEvent('mc_learn',
      {\u63d0\u70bc\u539f\u5219:d.principles, \u5165\u5e93:d.store.mc_thinking_count, \u5bc6\u5ea6:d.densities},
      'MC Strategy Learning Complete · Stored');
    // Automatically start the full standard MVE practice loop.
    // Same rule as Start Voyager: continuous by default (rounds 0), degrading to one round only when a practice
    // scope pins the generator to a single question.
    const _ck=document.getElementById('ckContinuous');
    const _sc=window.__scope||{active:false};
    await runJob('practice',{mode:'loop',
      rounds:(_ck&&_ck.checked&&!_sc.active)?0:1,
      use_scope:true, btnId:'runStart', btnText:'▶ Start Voyager',
      busyText:'Starting…'});
  }catch(e){ alert('MC learning failed: '+e); }
  finally{ if(b){ b.disabled=false; b.textContent='🧠 Start MC Strategy Learning → MVE'; } }
}
async function startPlacement(){
  const n=window.__skills||0;
  if(n>0){
    const ok=confirm('The skill library already contains '+n+' programs.\n\n'
      +'The baseline exam measures true zero-baseline capability. Testing with the skill library measures post-practice capability '
      +'and overwrites the original zero-baseline values.\n\n'
      +'OK: clear the library (automatic backup to mve/_backup_*) and start baseline; Cancel: do not run.');
    if(!ok) return;
    try{
      const r=await fetch('/api/reset',{method:'POST'});
      const d=await r.json();
      if(!d.ok){ alert('Clear library failed: '+(d.error||'Unknown')); return; }
      uiEvent('reset_all',{\u539f\u56e0:'Clear Before Baseline Exam', \u6e05\u5e93\u524d\u6280\u80fd\u6570:n},'Library Cleared (Backup Created)');
    }catch(e){ alert('Clear library failed: '+e); return; }
  }
  await runJob('placement',{btnId:'btnPlacement',
    btnText:'Baseline Exam (Entire Library, No Graph)', busyText:'Running Baseline…'});
}
/* Final exam has the opposite prerequisite from baseline. Baseline requires an empty library (zero baseline),
   while final requires the library (measure retained capability without scaffolds). It stores kind="final" and does
   not overwrite the baseline, so the baseline column remains after the final exam. */
async function startFinal(){
  const n=window.__skills||0;
  if(!n){
    alert('The skill library is empty. The final exam measures retained learning, so there is nothing to test yet.\n\n'
      +'Run Learning Units to build skills, or run Baseline Exam first to measure zero-baseline capability.');
    return;
  }
  await runJob('final',{btnId:'btnFinal',
    btnText:'Final Exam (With Skill Library)', busyText:'Running Final Exam…'});
}
async function pilotStop(){
  const b=document.getElementById('runStop'); if(b.disabled) return;
  b.disabled=true; b.textContent='Stopping…';
  const p=window.__pilot||{};
  try{
    await fetch('/api/pilot/stop',{method:'POST'});
    await load();
    uiEvent('pilot_stop',
      {\u505c\u6b62\u65f6\u6a21\u5f0f: p.mode||'—', \u5df2\u8dd1\u9898\u6570: p.iterations||0, \u9009\u9898: p.topic||'—'},
      'Stopped');
  }
  catch(e){ alert('Stop failed: '+e); }
  finally{ b.disabled=false; b.textContent='■ Stop'; }
}

/* ---------------- Voyager Real Learning Controls ---------------- */
async function voyagerStart(){
  const b=document.getElementById('voyStart'); if(!b||b.disabled) return;
  b.disabled=true; b.innerHTML='<span class="spin"></span>Starting…';
  try{
    const r=await fetch('/api/voyager/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    uiEvent('voyager_start',
      {mc_port:d.mc_port||25565, \u4e0a\u9650:d.max_iterations||''},
      d.ok?('Started · pid '+(d.pid||'')):('Failed: '+(d.error||'Unknown')));
    if(!d.ok){ alert('Start failed: '+(d.error||'Unknown')); }
    else { TAB='voyager'; localStorage.setItem('mve_tab',TAB); }
    await load();
  }catch(e){ uiEvent('voyager_start',{},'Exception: '+e); alert('Start failed: '+e); }
  finally{ if(b){ b.disabled=false; b.textContent='▶ Start Learning'; } }
}
async function voyagerStop(){
  const b=document.getElementById('voyStop'); if(!b||b.disabled) return;
  b.disabled=true; b.textContent='Stopping…';
  const v=window.__voyager||{};
  try{
    const r=await fetch('/api/voyager/stop',{method:'POST'});
    const d=await r.json();
    uiEvent('voyager_stop',{\u505c\u6b62\u65f6\u8f6e\u6b21:v.iterations||0, \u5df2\u5b66\u6280\u80fd:v.completed_count||0},'Stopped');
    await load();
  }catch(e){ alert('Stop failed: '+e); }
  finally{ if(b){ b.disabled=false; b.textContent='■ Stop'; } }
}
async function voyagerClear(which, label){
  const ok=confirm('Clear '+(which==='skill'?'Library':'Memory')+' = delete Voyager\'s '
    +(which==='skill'?'learned JS skill library (ckpt/skill/)':'curriculum progress + event history (ckpt/curriculum/ + ckpt/events/)')
    +'.\n\nA backup is created in voyager_backup_*.\n\nClear '+label+'?');
  if(!ok) return;
  try{
    const r=await fetch('/api/voyager/clear-'+which,{method:'POST'});
    const d=await r.json();
    uiEvent('voyager_clear_'+which,{\u6e05\u4e86:d.cleared||[], \u5907\u4efd:d.backup||'None'}, d.ok?('Cleared '+label):'Failed');
    alert(d.ok?('Cleared '+label+' (Backup: '+(d.backup||'None')+')'):('Failed: '+(d.error||'Unknown')));
    await load();
  }catch(e){ alert('Clear '+label+' failed: '+e); }
}

/* ---------------- Main Render ---------------- */
function render(s){
  window.__lastRec = s.recommend || {};
  window.__pilot   = s.pilot || {};
  window.__voyager = s.voyager || {};
  window.__skills  = (s.skill_stats||{}).skills || 0;
  // topic_id -> difficulty (telemetry must record the selected difficulty)
  window.__diff = {};
  (s.tasks||[]).forEach(t=>{ if(t.topic_id) window.__diff[t.topic_id]=t.difficulty; });
  window.__difficultyOf = id => window.__diff[id];
  renderHero(s);
  renderNav(s);
  const stage=document.getElementById('stage');
  // Do not rebuild the page while a user is operating form controls.
  // Polling calls render every four seconds; replacing stage.innerHTML resets controls to HTML defaults.
  // Affected controls observed in testing (same root cause, fixed together):
  //   learnUnits / learnTransfer — always reset to 4 and 3
  //   q — half-typed questions disappear
  //   topic — manual import routing resets to Auto Match
  //   dbPath — database path is cleared
  //   fresh explanation in #result is also erased; skip while submission is busy.
  // Polling resumes after focus leaves the control, so no data is lost.
  const af=document.activeElement;
  const FORM=['SELECT','INPUT','TEXTAREA'];
  const holdingForm = af && af.tagName && FORM.includes(af.tagName)
                      && stage.contains(af);
  if(!(TAB==='import' && busy) && !holdingForm){
    // Preserve user-edited values even during rebuild. Polling still runs while a button has focus and must not
    // reset the selected unit count, transfer interval, or draft.
    const vals={}, afId=(af&&af.id&&stage.contains(af))?af.id:null;
    stage.querySelectorAll('select[id],input[id],textarea[id]')
      .forEach(el=>{ vals[el.id]=el.value; });
    stage.innerHTML=(PANELS[TAB]||pOverview)(s);
    Object.entries(vals).forEach(([id,v])=>{
      const n=document.getElementById(id);
      if(n && v) n.value=v;
    });
    if(afId){
      const n=document.getElementById(afId);
      if(n && n.tagName && FORM.includes(n.tagName)) n.focus();
    }
    fillTopics(s);          // #topic is new after rebuild; refill after restoring values so keep works
    const il=document.getElementById('implist');
    if(il) il.innerHTML=importsList(s);

    const go=document.getElementById('go'); if(go) go.onclick=submitImport;
    const clr=document.getElementById('clr'); if(clr) clr.onclick=()=>{
      document.getElementById('q').value='';
      document.getElementById('result').innerHTML='';
      document.getElementById('hit').textContent='Topic routing results appear here.';
    };
    const qq=document.getElementById('q');
    if(qq){
      qq.addEventListener('input',()=>{clearTimeout(pvTimer);pvTimer=setTimeout(preview,350);});
      qq.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter')submitImport();});
    }
  }
  const cl=document.getElementById('clearLog'); if(cl) cl.onclick=async()=>{
    await fetch('/api/pilot/clear-log',{method:'POST'});
    uiEvent('clear_log',{},'Run Log Cleared');
    await load();
  };
  const bp=document.getElementById('btnPlacement');
  if(bp) bp.onclick=startPlacement;
  const bf=document.getElementById('btnFinal');
  if(bf) bf.onclick=startFinal;
  const bl=document.getElementById('btnLearn');
  if(bl) bl.onclick=()=>runJob('learn',{btnId:'btnLearn', btnText:'Run Learning Units',
    busyText:'Learning…',
    units:+((document.getElementById('learnUnits')||{}).value||4),
    transfer:+((document.getElementById('learnTransfer')||{}).value||0)});
  const bes=document.getElementById('btnExamStop'); if(bes) bes.onclick=pilotStop;
  const rn=document.getElementById('refreshNow'); if(rn) rn.onclick=load;
  const ex=document.getElementById('btnExport'); if(ex) ex.onclick=exportMd;
  const dp=document.getElementById('dbProbe'); if(dp) dp.onclick=dbProbe;
  const ds=document.getElementById('dbSwitch');
  if(ds){ ds.onclick=dbSwitch; ds.disabled=!dbProbedOk; }
  const vs=document.getElementById('voyStart');
  if(vs) vs.onclick=voyagerStart;
  const vsp=document.getElementById('voyStop');
  if(vsp) vsp.onclick=voyagerStop;
  const vcl=document.getElementById('voyClearLog');
  if(vcl) vcl.onclick=async()=>{ await fetch('/api/voyager/clear-log',{method:'POST'}); await load(); };
  const vcs=document.getElementById('voyClearSkill');
  if(vcs) vcs.onclick=()=>voyagerClear('skill','Library');
  const vcm=document.getElementById('voyClearMemory');
  if(vcm) vcm.onclick=()=>voyagerClear('memory','Memory');
  // Adaptive-practice tab buttons exist only on that tab; guard with if and bind on every render
  const oqn=document.getElementById('oqNext');    if(oqn) oqn.onclick=oqNext;
  const oqs=document.getElementById('oqSubmit');  if(oqs) oqs.onclick=oqGrade;
  const oqr=document.getElementById('oqReveal');  if(oqr) oqr.onclick=oqReveal;
}

/* ---------------- Database Switch: Validate, Then Switch ----------------
   Validation must happen **before** switching. Discovering an empty or wrong database afterward leaves users
   unable to tell where the previous data went. */
let dbProbedOk=false;
async function dbProbe(){
  const hit=document.getElementById('dbHit'); if(!hit) return;
  const p=document.getElementById('dbPath').value.trim();
  hit.className='hit'; hit.innerHTML='Validating…';
  try{
    const r=await fetch('/api/db/probe',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:p})});
    const d=await r.json();
    dbProbedOk=!!d.ok;
    document.getElementById('dbSwitch').disabled=!dbProbedOk;
    if(!d.ok){ hit.className='hit no'; hit.innerHTML=`<b>Cannot Use:</b>${ESC(d.error||'Unknown')}`; return; }
    hit.innerHTML=`Valid · <b>${d.tables.length}</b> Tables`
      + ((d.events!==undefined&&d.events!==null)?` · <b>${d.events}</b> Events`:'')
      + (d.series_ids&&d.series_ids.length?` · series: ${d.series_ids.map(x=>ESC(x)).join(', ')}`:'')
      + (d.warning?`<br><b style="color:#8a2015">⚠ ${ESC(d.warning)}</b>`:'');
  }catch(e){ hit.className='hit no'; hit.innerHTML='Validation failed: '+ESC(e); }
}
async function dbSwitch(){
  const b=document.getElementById('dbSwitch'); if(!b||b.disabled) return;
  const p=document.getElementById('dbPath').value.trim();
  const reset=document.getElementById('dbReset')?.checked;
  if(!confirm((p?('Switch to: '+p):'Restore Default Database')
      +(reset?', and clear all learning data (including entity lists / schema cache derived from the previous database; entities will be rediscovered)':'')
      +'\n\nThe dashboard process must restart after switching. Continue?')) return;
  b.disabled=true;
  try{
    const r=await fetch('/api/db/switch',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:p, reset:reset})});
    const d=await r.json();
    const hit=document.getElementById('dbHit');
    hit.className='hit';
    hit.innerHTML = d.ok
      ? `<b>Configuration Saved</b>: ${ESC(d.db_path)}`
        +(d.reset_detail
          ? `<br><span class="mono">${ESC(Object.entries(d.reset_detail)
              .filter(([k])=>k!=='\u5207\u6362\u5230'&&k!=='\u8bf4\u660e')
              .map(([k,v])=>k+': '+v).join(' · '))}</span>`
          : '')
        +`<br>Restart the dashboard process now:
         <span class="mono">lsof -ti:8777 | xargs kill; python mve/dashboard.py</span>`
      : `<b>Failed:</b>${ESC(d.error||'Unknown')}`;
    await load();
  }catch(e){ document.getElementById('dbHit').innerHTML='Switch failed: '+ESC(e); }
  finally{ b.disabled=false; }
}

async function exportMd(){
  const b=document.getElementById('btnExport'); if(!b||b.disabled) return;
  b.disabled=true; const old=b.textContent; b.textContent='Exporting…';
  try{
    const r=await fetch('/api/export',{method:'POST'});
    const d=await r.json();
    if(!d.ok){ alert('Export failed: '+(d.error||'Unknown')); return; }
    const blob=new Blob([d.markdown],{type:'text/markdown;charset=utf-8'});
    const a=document.createElement('a');
    a.href=URL.createObjectURL(blob); a.download=d.filename;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(a.href);
  }catch(e){ alert('Export failed: '+e); }
  finally{ b.disabled=false; b.textContent=old; }
}

async function load(){
  try{
    const r=await fetch('/api/state?t='+Date.now());
    render(await r.json());
  }catch(e){
    document.getElementById('stage').innerHTML='<div class="panel"><div class="empty">Load failed: '+ESC(e)+'</div></div>';
  }
}

document.getElementById('nav').addEventListener('click',e=>{
  const b=e.target.closest('[data-tab]'); if(!b) return;
  const from=TAB; TAB=b.dataset.tab; localStorage.setItem('mve_tab',TAB);
  uiEvent('tab_switch',{\u4ece: TAB_LABEL[from]||from, \u5207\u5230: TAB_LABEL[TAB]||TAB}, '');
  load();
});
document.getElementById('runStart').onclick=pilotStart;
document.getElementById('mcLearn').onclick=mcLearnThenRun;
document.getElementById('runStop').onclick=pilotStop;

load();
setInterval(load, 4000);
</script>
</body>
</html>
"""


# Background task state for the consumption pipeline (in process memory; cleared on dashboard restart like
# pilot_state, but too lightweight to persist).
COACH_JOB: dict = {"running": False, "question": "", "started_at": 0.0, "error": ""}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _payload(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = _strip_prefix(self.path.split("?")[0])
        if path in ("/", "/index.html"):
            body = _html().encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif path == "/api/state":
            import time as _t
            # The consumption pipeline runs in the background for 1–3 minutes; the frontend polls every four seconds.
            # Exposing this state prevents the Answering button from appearing stalled.
            st = panel_data.build_state()
            st["coach_running"] = bool(COACH_JOB["running"])
            st["coach_question"] = str(COACH_JOB["question"] or "")[:80]
            st["coach_elapsed"] = (int(_t.time() - COACH_JOB["started_at"])
                                   if COACH_JOB["running"] else 0)
            st["coach_error"] = str(COACH_JOB["error"] or "")
            body = json.dumps(st, ensure_ascii=False, default=str).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif path == "/api/graph":
            body = _graph_json().encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif path == "/api/practice-scope":
            body = _scope_json().encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif path == "/api/teaching-graph":
            # Teaching graph (29 study-companion seed knowledge points + prerequisite gating + mastery overlay).
            # It coexists with /api/graph (grading-side dimension graph): one determines what to learn next,
            # the other maps each dimension to its tool.
            try:
                import topic_graph
                body = json.dumps(topic_graph.get_graph().to_graph_payload(),
                                 ensure_ascii=False).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"[:200]})
                return
        else:
            self.send_error(404)
            return
        self._send(200, body, ctype)

    def do_POST(self) -> None:  # noqa: N802
        """Write endpoints in two groups:

        Human intent: /api/preview (topic routing only, no VLML), /api/coach (actual run)
        Cockpit: /api/pilot/start|stop|clear-log
        """
        path = _strip_prefix(self.path.split("?")[0])
        payload = self._payload()

        if path == "/api/preview":
            try:
                import tasks
                topic, hit = tasks.infer_topic(str(payload.get("question") or ""))
                task = tasks.TASKS.get(topic)
                self._json(200, {
                    "topic_id": topic, "hit": hit, "matched": bool(topic),
                    "question": task.question if task else "",
                    "difficulty": task.difficulty if task else 0,
                })
            except Exception as e:
                self._json(500, {"matched": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/coach":
            q = str(payload.get("question") or "").strip()
            if not q:
                self._json(400, {"ok": False, "error": "Question is empty"})
                return
            mode = str(payload.get("mode") or "voyager").strip().lower()
            if mode != "voyager":
                # Legacy path (pure VLML explanation) is fast (~30s); keep it synchronous.
                try:
                    import asyncio
                    import coach  # Starts the VLML environment (chdir + patch); load on demand
                    res = asyncio.run(coach.explain(q, voyager=False))
                    res["ok"] = True
                except Exception as e:
                    self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
                    return
                self._json(200, res)
                return

            # Run the consumption pipeline in the background (lesson from 2026-10-13 testing). The full chain runs
            # more than ten sequential LLM calls (same-question validation + scoring-point derivation + recipe filling +
            # SQL iteration ×3 + fact structuring + independent VLML retrieval ×3 + approach comparison + narrative)
            # and takes 2–5 minutes in testing.
            # Synchronous waiting causes Caddy gateway timeout and leaves the frontend spinning on Answering;
            # users then assume Voyager failed. Accept immediately, finish in a background thread, and write coach_log.
            # Four-second frontend polling then updates import history and the progress curve, like the cockpit pilot.
            import threading
            import time as _time
            if COACH_JOB["running"]:
                self._json(200, {
                    "ok": False, "busy": True,
                    "error": (f"Previous question is still running ({COACH_JOB['question'][:40]}…; "
                              f"{int(_time.time() - COACH_JOB['started_at'])} seconds elapsed). "
                              "Wait for the result before asking another question.")})
                return
            COACH_JOB.update({"running": True, "question": q,
                              "started_at": _time.time(), "error": ""})

            def _run_consume_bg() -> None:
                try:
                    import asyncio
                    import sys as _sys
                    from pathlib import Path as _Path
                    _repo_root = str(_Path(__file__).resolve().parents[1])
                    if _repo_root not in _sys.path:
                        _sys.path.insert(0, _repo_root)
                    import voyager_vlml.consume as _consume
                    # consume is async and must run through asyncio.run. Direct invocation returns an unexecuted
                    # coroutine (observed warning: "coroutine 'consume' was never awaited"), appearing as accepted
                    # and completed progress with no actual work.
                    asyncio.run(_consume.consume(
                        q, topic_hint=str(payload.get("topic_id") or "")))
                    # consume writes coach_log through _log; frontend polling picks it up naturally
                except Exception as e:  # Surface failures instead of swallowing them
                    COACH_JOB["error"] = f"{type(e).__name__}: {e}"[:300]
                    try:
                        from coach import _log_import
                        _log_import({
                            "question": q, "mode": "voyager_consume",
                            "topic_id": "", "topic_hit": "", "source": "human_import",
                            "validated_target": False, "trajectory": [], "errors":
                            [COACH_JOB["error"]], "facts": [], "facts_count": 0,
                            "narrative": "", "insights": [], "caveats": [],
                            "skipped": f"Consumption pipeline failed: {COACH_JOB['error']}",
                        })
                    except Exception:
                        pass
                finally:
                    COACH_JOB["running"] = False

            threading.Thread(target=_run_consume_bg, daemon=True).start()
            self._json(200, {"ok": True, "accepted": True, "running": True,
                             "message": "Accepted. Voyager is answering (more than ten model calls; "
                                        "about 1–3 minutes). The result will appear automatically in "
                                        "import history below; no need to wait on this screen."})
            return

        if path == "/api/db/probe":
            try:
                import db_switch
                import ui_events
                p = str(payload.get("path") or "")
                pr = db_switch.probe(p)
                # Record validation: switching databases is the only action that may make all data unavailable.
                # On failure, users need to review what validation observed.
                ui_events.append(
                    ui_events.DB_PROBE,
                    detail={"\u76ee\u6807\u5e93": pr.get("path") or "(Default Database)",
                            "\u4f53\u68c0": (f"{len(pr.get('tables') or [])} Tables"
                                     f" · {pr.get('events')} Events")
                                    if pr.get("ok") else (pr.get("error") or "Failed"),
                            "series \u68c0\u67e5": pr.get("warning") or "Existing question series remain in the database"},
                    result="Can Switch" if pr.get("ok") else "Cannot Switch",
                )
                self._json(200, pr)
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/db/switch":
            try:
                import db_switch
                import ui_events
                p = str(payload.get("path") or "")
                res = db_switch.write(p)
                detail = {"\u5207\u6362\u5230": res["db_path"]}
                if res["is_default"]:
                    detail["\u8bf4\u660e"] = "Restored default database"
                # write() now calls anchor.refresh(): reuse old anchors if present in the new database; otherwise
                # select the match/team/map with the most rounds. Report the outcome.
                if res.get("anchor_note"):
                    detail["\u951a\u70b9"] = res["anchor_note"]
                if payload.get("reset"):
                    try:
                        import subprocess
                        # With --db-switch, clearing state must also remove entity_catalog / tool_schemas derived
                        # from the previous database. Old team/map/series_id values make the question generator
                        # select questions that cannot be queried in the new database.
                        _r = subprocess.run(
                            [sys.executable, str(ROOT / "reset_all.py"), "--db-switch"],
                            capture_output=True, timeout=180)
                        # Check the return code. reset_all rejects clearing state while the loop is running (code 1).
                        # Previously the dashboard always reported success even when nothing was deleted.
                        detail["\u987a\u5e26"] = ("State Cleared (Including Previous-Database Cache)" if _r.returncode == 0
                                          else "Clear Rejected (Task Running; Stop It First)")
                        if _r.returncode == 0:
                            # After clearing, repopulate values by querying the **new database**. entity_catalog.py
                            # reads db_config.json, which now points to the new database.
                            _r2 = subprocess.run(
                                [sys.executable, str(ROOT / "entity_catalog.py")],
                                capture_output=True, timeout=180)
                            if _r2.returncode == 0:
                                detail["\u5b9e\u4f53\u6e05\u5355"] = "Rebuilt from New Database"
                            else:
                                detail["\u5b9e\u4f53\u6e05\u5355"] = ("Rebuild Failed; Run Manually: "
                                                      "python mve/entity_catalog.py")
                            # Previously only entity_catalog was rebuilt; tool_schemas was omitted. --db-switch also
                            # deletes tool_schemas.json, while load_schemas() returns {} without rebuilding, and async
                            # probe_schemas() had no CLI. The model then had no tool schema and guessed field paths
                            # (observed: all four dimensions resolved to None). Use action_code.py --rebuild-schemas here
                            # so both caches are restored together.
                            _r3 = subprocess.run(
                                [sys.executable, str(ROOT / "action_code.py"),
                                 "--rebuild-schemas"],
                                capture_output=True, timeout=300)
                            if _r3.returncode == 0:
                                detail["\u5de5\u5177\u7ed3\u6784"] = (
                                    "Rebuilt from New Database (~ in CLI output marks failures "
                                    "that reused previous schemas)")
                            else:
                                detail["\u5de5\u5177\u7ed3\u6784"] = (
                                    "No Schemas Probed (probe parameters come from anchors; if anchors "
                                    "are absent from the new database, the entire probe fails). Previous "
                                    "schemas remain cached. Run: python mve/action_code.py --rebuild-schemas")
                    except Exception:
                        detail["\u987a\u5e26"] = "State Clear Failed (Database Already Switched)"
                # Verify after switching. refresh selects anchors from the database; anchor_status checks whether
                # those values truly exist. Report both: the first says what was attempted, the second confirms it.
                # Otherwise, a silent refresh failure remains invisible.
                try:
                    _st = db_switch.anchor_status()
                    if _st.get("stale"):
                        detail["\u951a\u70b9\u6838\u5bf9"] = _st.get("hint") or "Anchors Not Found in New Database"
                    elif _st.get("hint"):
                        detail["\u951a\u70b9\u6838\u5bf9"] = _st.get("hint")
                    elif _st.get("checked"):
                        detail["\u951a\u70b9\u6838\u5bf9"] = (
                            f"Verified in New Database: {_st.get('anchor', {}).get('team')} / "
                            f"{_st.get('anchor', {}).get('map')} / series "
                            f"{_st.get('anchor', {}).get('series')}")
                except Exception:
                    pass
                ui_events.append(ui_events.DB_SWITCH, detail=detail, result="ok")
                self._json(200, {**res, "need_restart": True,
                                 "reset_detail": detail})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path in ("/api/subject/add", "/api/subject/sync", "/api/subject/del"):
            # Subject registry: users enter subjects (new event/team domains after switching data sources),
            # then attach VLML/Voyager-generated entries to the subject by entity value.
            try:
                import subjects
                import knowledge_graph
                import ui_events
                if path.endswith("/sync"):
                    res = subjects.sync_from_catalog()
                    ui_events.append(ui_events.SUBJECT_SYNC,
                                     detail={"\u65b0\u767b\u8bb0": res.get("added", 0)},
                                     result="ok" if res.get("ok") else "Failed")
                    self._json(200, res)
                    return
                if path.endswith("/del"):
                    sid = str(payload.get("id") or "")
                    ok = subjects.unregister(sid)
                    ui_events.append(ui_events.SUBJECT_DEL, detail={"\u5b66\u79d1": sid},
                                     result="Deleted" if ok else "Not Found")
                    self._json(200, {"ok": ok, "id": sid})
                    return
                kind = str(payload.get("kind") or "").strip()
                value = str(payload.get("value") or "").strip()
                res = subjects.register(kind, value,
                                        note=str(payload.get("note") or ""))
                # Recalculate ownership immediately. Entries generated before the user added this subject must be
                # attached now without waiting for --build. moved counts only entries newly assigned in this run;
                # previously assigned entries are excluded so the number remains informative.
                moved = 0
                if res.get("ok"):
                    try:
                        before = set(subjects.unassigned())
                        for r in (knowledge_graph.load_imported_links()
                                  + knowledge_graph.load_pending_dimensions()):
                            knowledge_graph._attach_entry(r)
                        moved = len(before - set(subjects.unassigned()))
                    except Exception:
                        pass
                ui_events.append(ui_events.SUBJECT_ADD,
                                 detail={"\u5b66\u79d1": f"{kind}={value}",
                                         "\u8865\u5f52\u5c5e\u6761\u76ee": moved},
                                 result="Registered" if res.get("ok") else "Failed")
                self._json(200, {**res, "moved": moved})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/practice-scope":
            # set = use a graph dimension as practice scope; clear = restore question-generator selection.
            # Mirrors study_set_practice_scope: canonical validation + incrementing revision.
            try:
                import practice_scope
                import ui_events
                if str(payload.get("action") or "") == "clear":
                    res = practice_scope.clear_scope()
                    ui_events.append(ui_events.PRACTICE_SCOPE_CLEAR,
                                     detail={"\u539f\u8303\u56f4": res.get("cleared") or "—"},
                                     result="Cleared · Question Generator Restored")
                    self._json(200, {**res, "scope": practice_scope.get_scope()})
                    return
                dim = str(payload.get("dim") or "")
                scope = practice_scope.set_scope(dim)
                ui_events.append(
                    ui_events.PRACTICE_SCOPE_SET,
                    detail={"\u7ef4\u5ea6": dim,
                            "\u5173\u8054\u9898": ", ".join(scope.get("topics") or []),
                            "revision": scope.get("scope_revision")},
                    result="Scope Active → " + ", ".join(scope.get("topics") or []),
                )
                self._json(200, {"ok": True, "scope": scope})
            except Exception as e:
                code = str(getattr(e, "code", "") or "PRACTICE_SCOPE_ERROR")
                try:
                    import practice_scope
                    cur = practice_scope.get_scope()
                except Exception:
                    cur = {"active": False}
                self._json(200, {"ok": False, "code": code,
                                 "error": f"{type(e).__name__}: {e}"[:300],
                                 "scope": cur})
            return

        if path == "/api/open-quiz/next":
            # Select the next knowledge point and generate an open-ended question
            # (prerequisite gating + weakest-first + question-type rotation).
            try:
                import open_quiz
                scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else None
                explicit = str(payload.get("topic_id") or "") or None
                last = str(payload.get("last_question") or "")
                q = open_quiz.generate_next(scope=scope, explicit=explicit,
                                            last_question=last)
                body = json.dumps(q, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/open-quiz/grade":
            # Grade semantically using accepted_answers + key_points; math_exact uses fast numeric grading first.
            # Then record mastery.
            try:
                import open_quiz
                q = payload.get("question") if isinstance(payload.get("question"), dict) else {}
                ua = str(payload.get("user_answer") or "")
                topic_id = str(q.get("topic") or payload.get("topic_id") or "")
                g = open_quiz.grade_answer(q, ua)
                if topic_id:
                    open_quiz.record_result(topic_id, score=g["score"])
                    # Task 3: on an incorrect answer (score<0.6), write the misconception to mem0 as corrective memory.
                    # Future generation/answers can recall it, closing the memory loop for human consumption.
                    corr_written = open_quiz.record_correction_on_failure(
                        topic_id, user_answer=ua, grade=g)
                else:
                    corr_written = 0
                self._json(200, {"ok": True, "grade": g, "topic_id": topic_id,
                                "correction_memories": corr_written})
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/ui-event":
            try:
                import ui_events
                ev = ui_events.append(
                    str(payload.get("kind") or "unknown"),
                    detail=payload.get("detail") or {},
                    result=str(payload.get("result") or ""),
                )
                self._json(200, {"ok": True, "seq": ev["seq"]})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/export":
            try:
                import export_md
                import ui_events
                md = export_md.build_markdown()
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                ui_events.append(ui_events.EXPORT,
                                 detail={"\u5b57\u7b26\u6570": len(md)},
                                 result=f"mve_export_{stamp}.md")
                self._json(200, {"ok": True, "markdown": md,
                                 "filename": f"mve_export_{stamp}.md"})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/pilot/start":
            # Always leave topic empty: question selection belongs to the practice scope or question generator,
            # not cockpit controls. The dashboard therefore has no difficulty/question dropdown, only Start / Stop.
            self._json(200, pilot.start(
                mode=str(payload.get("mode") or "once"),
                topic="",
                # Default 0 = continuous. Using `or 3` replaces the frontend's 0 as falsy, so the Continuous switch
                # still runs only three questions and exits before completing the graph.
                rounds=int(payload.get("rounds") or 0),
                use_scope=bool(payload.get("use_scope", True)),
                # Jobs: practice / placement (baseline exam) / learn (learning units)
                job=str(payload.get("job") or "practice"),
                units=int(payload.get("units") or 1),
                transfer=int(payload.get("transfer") or 0),
            ))
            return

        if path == "/api/pilot/stop":
            self._json(200, pilot.stop())
            return

        if path == "/api/pilot/clear-log":
            pilot.clear_log()
            self._json(200, {"ok": True})
            return

        # Clear state before baseline so the skill library is empty; otherwise the exam measures post-practice capability.
        # reset_all creates a backup in mve/_backup_* before deletion, so this endpoint adds no extra confirmation.
        if path == "/api/reset":
            try:
                import reset_all
                _old = sys.argv[:]
                sys.argv = ["reset_all.py"]       # reset_all.main() reads argv
                try:
                    # Check the return code: reset_all rejects clearing while the loop/cockpit is running (code 1).
                    # Previously this return value was discarded and the dashboard reported a successful backup
                    # even when no files were deleted.
                    rc = reset_all.main()
                finally:
                    sys.argv = _old
                if rc != 0:
                    self._json(409, {"ok": False,
                                     "error": "A task is still running; clear rejected. Click Stop first, "
                                              "wait for the loop to finish (about one minute), then clear."})
                    return
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        # MC strategy learning ("start learning in MC"): extract + store. The frontend then automatically starts
        # the standard practice loop (the full MVE flow through pilot/start).
        if path == "/api/mc-learn":
            try:
                import mc_learn
                import ui_events
                res = mc_learn.run()
                ui_events.append(
                    ui_events.MC_LEARN,
                    detail={"\u63d0\u70bc\u539f\u5219": res["principles"],
                            "\u5165\u5e93\u6761\u6570": res["store"]["mc_thinking_count"],
                            "SKILL.md": res["skill_md"]},
                    result="MC Strategy Learning Complete → Stored",
                )
                self._json(200, res)
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        # ---- Voyager Real Learning (Live MC) ----
        # Start / Stop / Clear Library / Clear Memory: same model as the MVE cockpit; state is persisted and dashboard is read-only.
        if path == "/api/voyager/start":
            try:
                import voyager_pilot
                import ui_events
                res = voyager_pilot.start()
                ui_events.append(
                    ui_events.VOYAGER_START,
                    detail={"mc_port": res.get("mc_port", 25565),
                            "\u4e0a\u9650": res.get("max_iterations", "")},
                    result="Started" if res.get("ok") else ("Failed: " + res.get("error", "")),
                )
                self._json(200, res)
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/voyager/stop":
            try:
                import voyager_pilot
                import ui_events
                res = voyager_pilot.stop()
                ui_events.append(
                    ui_events.VOYAGER_STOP,
                    detail={"\u5df2\u5b66\u6280\u80fd": res.get("completed_count", 0)},
                    result="Stopped",
                )
                self._json(200, res)
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/voyager/clear-log":
            try:
                import voyager_pilot
                voyager_pilot.clear_log()
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/voyager/clear-skill":
            try:
                import voyager_learn_reset
                import ui_events
                res = voyager_learn_reset.clear_skill()
                ui_events.append(
                    ui_events.VOYAGER_CLEAR_SKILL,
                    detail={"\u6e05\u4e86": res.get("cleared", []),
                            "\u5907\u4efd": res.get("backup") or "None"},
                    result="Library Cleared" if res.get("ok") else "Failed",
                )
                self._json(200, res)
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/voyager/clear-memory":
            try:
                import voyager_learn_reset
                import ui_events
                res = voyager_learn_reset.clear_memory()
                ui_events.append(
                    ui_events.VOYAGER_CLEAR_MEMORY,
                    detail={"\u6e05\u4e86": res.get("cleared", []),
                            "\u5907\u4efd": res.get("backup") or "None"},
                    result="Memory Cleared" if res.get("ok") else "Failed",
                )
                self._json(200, res)
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        self.send_error(404)

    def log_message(self, fmt: str, *args: Any) -> None:  # Silence default access logs
        pass


def main() -> None:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    _ensure_knowledge_graph()   # startup self-check: auto-rebuild if missing/stale
    print(f"MVE dashboard started: http://{HOST}:{PORT}")
    print("Stop: Ctrl-C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
