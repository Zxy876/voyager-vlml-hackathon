#!/usr/bin/env python3
"""Cockpit: one-click start / stop of Voyager from the dashboard.

Why a separate file instead of subprocess.Popen inside dashboard.py:
  the dashboard is an HTTP server, so a process handle attached to a request is lost as soon as
  the dashboard restarts —— the human clicked "start" but cannot tell whether it is running,
  which is worse than not offering a button at all. So the cockpit is an **independent child
  process** on its own, state is written to a file, and the dashboard only reads that file.

   dashboard ──Popen──> pilot.py _run ──Popen──> voyager_vlml/loop.py
     │                   ↑                      │
     └── reads pilot_state.json ────────────────┘

Three jobs (`--job`, the three buttons on the dashboard):
  practice  —— runs voyager_vlml/loop.py: milestone topic selection → generate its own question
                → write its own SQL to retrieve data
  placement —— runs exam.py --all: **baseline exam**, exam the whole bank with the knowledge graph
                removed (~7 minutes)
  learn     —— runs learn.py --units N: a learning unit = practice one question + re-exam the very
                same question with the knowledge graph removed

Two modes (practice only):
  once —— run one question, then exit (the question generator picks the topic, or a pinned topic)
  loop —— after finishing one question **ask the question generator for the next one**, running
          until a human clicks stop. This is what "wiring up adaptive question generation by
          itself" means: who picks the next question is the question generator, not the human.
          (With no --topic, loop.py walks the milestone tree via select_next; once the scope is
            exhausted it generates new knowledge points by itself and the round never ends ——
            the only way to change the pace is to click stop)

Stop semantics: SIGTERM goes to the cockpit process, which forwards it to the running backend.
It also writes stop_requested —— so stale state is never misread as "still running" after a
dashboard restart.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
STATE = ROOT / "pilot_state.json"
LOG = ROOT / "pilot.log"


def _pick_python() -> str:
    """Pick an interpreter that **can import duckdb** to run run_mve.

    Pitfall (hit in practice): the cockpit spawns children with sys.executable, i.e. **whichever
    interpreter started the dashboard is the one used**. Start the dashboard with a python lacking
    the VLML dependencies → click start → run_mve dies with
    `ModuleNotFoundError: No module named 'duckdb'`, exit code 1, and the human only sees
    "stopped (run_mve exit code 1)" with no idea the interpreter was the wrong one.

    So probe once at startup: only an interpreter that can `import duckdb` is used to run questions.
    MVE_PY can force a specific one.
    """
    cands = [os.environ.get("MVE_PY") or "", sys.executable or ""]
    cands += sorted(str(p) for p in (Path.home() / ".workbuddy/binaries/python/envs").glob("*/bin/python"))
    cands.append("python3")
    for c in cands:
        if not c:
            continue
        try:
            r = subprocess.run([c, "-c", "import duckdb"],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return sys.executable or "python3"


PY = _pick_python()

# The three dashboard buttons map to three kinds of work. The baseline exam / learning units
# each run for minutes, so the per-question timeout (15 minutes) is not enough for them ——
# 8 learning units take more than 15 minutes.
JOB_LABEL = {"practice": "Practice (milestone course · generates its own questions and SQL)",
             "placement": "Baseline exam (whole bank, knowledge graph removed)",
             "final": "Final exam (whole bank re-examined with the skill library)",
             "learn": "Learning units (practice one question + re-exam with the graph removed)"}
JOB_TIMEOUT = {"practice": 900, "placement": 1800, "final": 1800, "learn": 5400}

# The run_mve child processes currently running (must be taken down together on stop, else orphans)
_CHILDREN: list[subprocess.Popen] = []


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write(state: dict[str, Any]) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _alive(pid: int) -> bool:
    """Whether the process is still alive.

    Pitfall: `os.kill(pid, 0)` alone reports **zombie** processes as alive —— the process is
    already dead but the parent never waited on it, so the pid is still in the table and the kill
    probe still succeeds. This bit us in practice: after clicking stop the dashboard still showed
    "running". So first reap it with waitpid(WNOHANG); being reaped means it has really exited.
    """
    try:
        wpid, _status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass  # not a child of this process (the dashboard restarted), fall back to the kill probe
    except OSError:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        return False
    return True


def read() -> dict[str, Any]:
    """Current state. Once the process is gone it is judged stopped automatically ——
    the running flag in the file is not trusted."""
    if not STATE.exists():
        return {"running": False, "mode": "", "topic": "", "rounds": 0,
                "iterations": 0, "pid": None, "started_at": "",
                "stopped_at": "", "last_exit": None, "why": "never started"}
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"running": False, "why": "state file is corrupted"}

    pid = st.get("pid")
    # the file says running but the process is gone —— it crashed or was force-killed; report honestly
    if st.get("running") and pid and not _alive(int(pid)):
        st["running"] = False
        st["why"] = "process exited (crashed or force-killed)"
    if not st.get("running") and not st.get("why"):
        st["why"] = "stopped"
    # ⚠️ The loop state lives in **another file written by another writer** (voyager_vlml/loop.py
    # writes mve/loop_state.json once per round). The dashboard only reads it, never writes it ——
    # writing back would wipe the loop's just-recorded "this round retrieved no data" and would
    # let the loop's rewrite clobber the stop_requested flag a human just clicked.
    st["loop"] = loop_read()
    # "stopped" while actually still winding down: the semantics of a graceful stop are
    # "finish the current question, then exit", so pilot exiting ≠ the loop having ended.
    # Without flagging it, a human refreshes the dashboard while data is still climbing and
    # thinks the curve moved on its own.
    if (not st.get("running")) and st["loop"].get("running") \
            and _fresh(st["loop"].get("updated_at"), 180):
        st["winding_down"] = True
        st["why"] = "winding down (exits only after finishing the current question, not killed outright)"
    return st


LOOP_STATE = ROOT / "loop_state.json"


def loop_read() -> dict[str, Any]:
    """Read the loop heartbeat (only voyager_vlml/loop.py writes it; the dashboard only reads).

    Why it exists: the learning loop is **non-stopping** (isomorphic to the original `while True`),
    so "what exactly is it doing right now" cannot be answered by an exit code —— the loop does not
    exit. The heartbeat carries how many rounds in a row produced no evidence and **why**
    (the chain never ran / retrieved 0 rows / the criterion did not pass), so the dashboard can
    show "it did not learn" separately from "the chain never worked at all".
    """
    if not LOOP_STATE.exists():
        return {}
    try:
        return json.loads(LOOP_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fresh(stamp: Any, seconds: int = 180) -> bool:
    """Whether the timestamp is still within `seconds` (i.e. whether the heartbeat is alive).

    Why not "is the process there": the winding-down loop.py is an orphan (pilot has already
    exited), so the parent-side _alive(pid) does not apply; only the heartbeat's update time does.
    """
    if not stamp:
        return False
    try:
        t = datetime.fromisoformat(str(stamp))
    except Exception:
        return False
    return (datetime.now() - t).total_seconds() <= seconds


def tail(lines: int = 60) -> str:
    if not LOG.exists():
        return ""
    content = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _log(text: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(text)
        f.flush()


def start(*, mode: str = "once", topic: str = "", rounds: int = 0,
          use_scope: bool = True, job: str = "practice",
          units: int = 1, transfer: int = 0) -> dict[str, Any]:
    """Start the cockpit (which itself spawns a child process).

    job decides **what to run** —— the three dashboard buttons map to three kinds of work:
      practice  —— runs run_mve.py (practicing questions, with the knowledge graph)
      placement —— runs exam.py --all (**baseline exam**: exam the whole bank with the graph removed)
      learn     —— runs learn.py --units N (a learning unit: practice one question + re-exam with
                   the graph removed)

    Why the baseline exam must be a background job rather than a synchronous endpoint: examining the
    whole bank of 8 questions with the graph removed takes ~7 minutes, which an HTTP request cannot
    wait for, and refreshing the page mid-way should not lose progress. The cockpit already is
    exactly this mechanism (state in a file + a tailable log), so reuse it instead of building
    another one.

    use_scope —— when no topic is explicitly pinned, pick questions by **practice scope**
    (practice_scope.py). The scope is set by clicking "Practice this knowledge point" on the
    knowledge graph page; once a scope is set, the human no longer picks difficulty / topic in the
    cockpit and the UI is just start / stop (the approach in the study companion's onboarding.md:82).

    rounds —— **0 = non-stopping** (default). Isomorphic to the original Voyager: the main loop is
      `while True` (voyager_original/voyager/voyager.py:326); only an iteration cap and a stop signal
      can make it exit — there is no "run N rounds and finish". This used to default to 3 and the
      dashboard hard-coded `rounds:3` → one start ran just 3 questions and exited, nowhere near
      finishing a 29-node graph, and what the human saw was "it ended after three rounds".
      The learning loop was never supposed to stop.
    """
    cur = read()
    if cur.get("running"):
        return {"ok": False, "error": f"already running (pid {cur.get('pid')}, "
                                     f"job {cur.get('job') or 'practice'})"}

    # ⚠️ Second gate: **the previous round is still winding down**. Stopping is graceful — after the
    #   dashboard clicks stop, pilot exits immediately, but loop.py finishes the question in hand
    #   before exiting (60-90 seconds, isomorphic to the original
    #   "exiting learn loop after current task"). During that window the dashboard already shows
    #   "stopped", so clicking start again would spawn a second loop.py, and two loops would write
    #   to run_log / skill_store at once —— the curve doubles for no reason and graph node states
    #   overwrite each other. So use the loop's own heartbeat: if it says running and was updated
    #   recently, refuse to start.
    lp = loop_read()
    if lp.get("running") and _fresh(lp.get("updated_at"), 180):
        return {"ok": False, "error": "the previous learning loop is still winding down "
                                     "(it exits only after finishing the current question); "
                                     "please start after it has flushed, so two loops never write data at once"}

    mode = "loop" if mode == "loop" else "once"
    # 0 = non-stopping; only >0 is a hard cap (kept for one-off uses like "just let me run a few questions")
    rounds = max(0, min(1000, int(rounds or 0)))
    job = str(job or "practice")
    if job not in ("practice", "placement", "final", "learn"):
        job = "practice"
    units = max(1, min(20, int(units or 1)))
    transfer = max(0, min(10, int(transfer or 0)))

    scope_label = ""
    if job == "practice" and use_scope and not topic:
        try:
            import practice_scope
            sc = practice_scope.get_scope()
            if sc.get("active"):
                scope_label = str(sc.get("label") or "")
                topic = practice_scope.resolve_topic() or ""
        except Exception:
            scope_label = ""

    cmd = [PY, str(ROOT / "pilot.py"), "_run", "--mode", mode,
           "--rounds", str(rounds), "--job", job,
           "--units", str(units), "--transfer", str(transfer)]
    if job == "practice":
        # The cockpit (practice) runs the isomorphic Voyager loop: question generation and progress
        # both live inside run_mve; pilot only launches one long process and watches the stop
        # signal, no longer one process per question.
        cmd += ["--curriculum"]
    if topic:
        cmd += ["--topic", str(topic)]

    # start_new_session: detach from the dashboard's process group so a dashboard exit does not
    # take it down
    proc = subprocess.Popen(
        cmd, cwd=str(PROJECT), start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    st = {
        "running": True, "mode": mode, "job": job, "units": units,
        "transfer": transfer, "job_label": JOB_LABEL[job],
        "topic": topic or ("(question generator picks)" if job == "practice"
                           else "(whole bank / question generator picks)"),
        "rounds": rounds, "pid": proc.pid, "started_at": _now(),
        "stopped_at": "", "iterations": 0, "last_exit": None,
        "stop_requested": False, "why": "running",
        "scope": scope_label,
    }
    _write(st)
    _log(f"\n===== START {_now()} · job {JOB_LABEL[job]} · mode {mode} · "
         f"{'non-stopping (isomorphic to the original while True, exits only on stop)' if not rounds else f'round cap {rounds}'}"
         + (f" · units {units}" if job == "learn" else "")
         + (f" · transfer check every {transfer} units" if job == "learn" and transfer else "")
         + f" · topic {st['topic']}"
         + (f" · from practice scope '{scope_label}'" if scope_label else "")
         + f" · pid {proc.pid} =====\n")
    return {"ok": True, **st}


def stop() -> dict[str, Any]:
    """Stop. Politely SIGTERM first (the cockpit takes run_mve down with it), SIGKILL after 5s.

    SIGKILL outright is not acceptable: the cockpit is at that moment blocked waiting on run_mve,
    so killing it leaves an orphan that runs to completion and keeps writing to run_log — the human
    thinks it stopped while the data keeps climbing.
    """
    st = read()
    pid = st.get("pid")
    if not st.get("running") or not pid:
        return {"ok": True, "stopped": True, "note": "it was not running anyway"}
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, ValueError, PermissionError, OSError) as e:
        return {"ok": True, "stopped": True, "note": f"the process was already gone ({e})"}

    deadline = time.time() + 5
    while time.time() < deadline:
        time.sleep(0.3)
        if not read().get("running"):
            break
    else:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass
        # After a force-kill the cockpit never got the chance to write state, so write it here ——
        # otherwise the dashboard shows "running" forever
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur.update({"running": False, "stopped_at": _now(),
                        "why": "stopped (force-killed)", "child_pid": None})
            _write(cur)
        except Exception:
            pass
    _log(f"\n===== STOP {_now()} · ran {st.get('iterations', 0)} questions =====\n")
    return {"ok": True, "stopped": True, "iterations": st.get("iterations", 0)}


def clear_log() -> None:
    """Clear the cockpit log (the dashboard's "clear screen" button)."""
    if LOG.exists():
        LOG.unlink()


# ---------------------------------------------------------------------------
# The cockpit child process proper
# ---------------------------------------------------------------------------

def _install_signal_handlers() -> None:
    """On SIGTERM, take the running run_mve down first, then exit.

    Without this handler, Python's default behaviour is to terminate the cockpit outright —— and
    it is at that moment blocked in communicate(), so run_mve becomes an orphan that keeps running
    to completion and the log keeps growing.
    """

    def handler(signum, _frame):
        # Terminate only the run_mve recorded in the ledger.
        # Do not use killpg hoping to "take the whole process group down for free": run_mve was
        # started with start_new_session, so it left our group long ago and the only real effect of
        # killpg is to send SIGTERM to ourselves again, interrupting this handler — the process dies
        # with the state only half written (observed: the dashboard showed "crashed or force-killed").
        for p in _CHILDREN:
            try:
                p.terminate()
            except Exception:
                pass
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur.update({"running": False, "stopped_at": _now(),
                        "why": "stopped", "child_pid": None})
            _write(cur)
        except Exception:
            pass
        sys.exit(143)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _run(mode: str, topic: str, rounds: int, job: str = "practice",
         units: int = 1, transfer: int = 0, curriculum: bool = False) -> int:
    _install_signal_handlers()

    # The log file handle must be **held for the whole run**: wrapping Popen in `with` closes the
    # fd the moment it returns, so the child execs with an already-invalid fd — observed directly as
    #   Fatal Python error: init_sys_streams: ... OSError: [Errno 9] Bad file descriptor
    logf = LOG.open("a", encoding="utf-8")
    try:
        code = _loop(mode, topic, rounds, logf, job, units, transfer, curriculum)
    finally:
        try:
            logf.close()
        except Exception:
            pass
    return code


def _loop(mode: str, topic: str, rounds: int, logf, job: str = "practice",
          units: int = 1, transfer: int = 0, curriculum: bool = False) -> int:
    pid = os.getpid()

    def mark(text: str) -> None:
        """Write a marker line. It goes through the same handle and flushes immediately ——
        otherwise the parent's buffer only hits disk at the end and separator lines like
        "question N" end up after the child's output, scrambling the log order."""
        logf.write(text)
        logf.flush()

    # ⚠️ Once a specific knowledge point is pinned, "non-stopping" is forbidden: loop.py would
    #   **keep running the very same question** (topic_id is passed explicitly and never enters
    #   curriculum progression), so progress stays stuck at 0/1 and what the human sees is
    #   "it is running but nothing grows". "Practice this knowledge point" on the dashboard is
    #   exactly this path. Non-stopping only makes sense when "following the curriculum", because
    #   the curriculum swaps questions by itself.
    if topic and rounds <= 0:
        rounds = 1

    _write({
        "running": True, "mode": mode, "job": job, "units": units,
        "transfer": transfer, "job_label": JOB_LABEL.get(job, job),
        "topic": topic or ("(question generator picks)" if job == "practice"
                           else "(whole bank / question generator picks)"),
        "rounds": rounds, "pid": pid, "started_at": _now(),
        "stopped_at": "", "iterations": 0, "last_exit": None,
        "stop_requested": False, "why": "running",
    })

    def asked_to_stop() -> bool:
        try:
            return bool(json.loads(STATE.read_text(encoding="utf-8")).get("stop_requested"))
        except Exception:
            return False

    code = 0
    iterations = 0
    # The baseline exam and learning units **run only once**: internally they are already a full
    # pass over the whole bank, so wrapping them in another loop layer would mean
    # "baseline exam, then baseline exam all over again".
    # practice + curriculum: run_mve loops internally and pilot only launches one long process,
    # so the outer layer spawns just once (max_iterations=1) and the timeout is raised to 6 hours.
    max_iterations = 1 if (job != "practice" or curriculum) else (200 if mode == "loop" else 1)
    # The per-question timeout has to scale with the number of rounds: one round of the new backend
    # is "question-generation LLM + SQL-writing iterations (up to 3) + MCP data retrieval"; one round
    # measured 60-90 seconds, so at rounds=10 the old 900 seconds would cut it off midway
    # (the human sees "stopped (exit code -15)" and cannot tell whether the question finished).
    #
    # ⚠️ But **non-stopping mode (rounds<=0) must not get a wait timeout**: the termination condition
    #   of a non-stopping loop is "a human clicks stop" (SIGTERM → loop.py finishes the current
    #   question and exits gracefully), not time. A 6-hour cap quietly turns "non-stopping" back
    #   into "run for 6 hours", and the timeout goes through proc.kill()—— a hard kill in the middle
    #   of a question, whose skills then never land on disk. To limit the total number of questions,
    #   use loop.py's --max-iterations (which likewise exits only after the current question).
    if curriculum and rounds <= 0:
        timeout = None
    elif curriculum:
        timeout = 6 * 3600
    else:
        timeout = max(JOB_TIMEOUT.get(job, 900), 240 * max(rounds, 1))

    while iterations < max_iterations and not asked_to_stop():
        iterations += 1
        if job == "placement":
            cmd = [PY, str(ROOT / "exam.py"), "--all"]
            what = f"baseline exam (whole bank, knowledge graph removed) · {_now()}"
        elif job == "final":
            # Final exam: re-exam the whole bank with the existing skill library. The only
            # difference from the baseline exam is the precondition (the baseline exam needs an
            # empty library, the final exam needs a populated one); it is persisted as
            # kind="final" and never overwrites the baseline.
            cmd = [PY, str(ROOT / "exam.py"), "--final"]
            what = f"final exam (whole bank re-examined with the skill library) · {_now()}"
        elif job == "learn":
            # learn's --rounds is "how many practice rounds per learning unit", a meaning unrelated
            # to "non-stopping"; passing 0 makes it run not a single round → fall back to 1
            cmd = [PY, str(ROOT / "learn.py"), "--units", str(units),
                   "--rounds", str(max(1, rounds))]
            if transfer:
                cmd += ["--transfer", str(transfer)]
            what = (f"{units} learning unit(s)"
                    + (f" (plus one unpracticed question every {transfer} units)" if transfer else "")
                    + f" · {_now()}")
        else:
            # The backend is already voyager_vlml (milestone curriculum + SQL primitives + DB
            # evidence criteria). The old entry point was `run_mve.py --llm [--curriculum]`, whose
            # feedback skeleton is built on "take Voyager's fact set and compare it against the
            # VLML referee's gold answer" (loop_core:295 evaluate_vs_referee) → verdicts / coverage /
            # scores would flow back into Voyager's context, which contradicts the design that
            # "VLML grading is for human eyes only", so it is frozen and no longer called.
            #
            # The new backend has no --curriculum switch: it **is itself** curriculum-driven
            # (seed milestones → trunk progression), so this parameter is deliberately ignored here.
            cmd = [PY, str(PROJECT / "voyager_vlml" / "loop.py"),
                   "--rounds", str(rounds)]
            if topic:
                cmd += ["--topic", topic]
            what = (f"learning loop · **non-stopping** (isomorphic to the original while True, "
                    f"exits only on stop) · {_now()}" if rounds <= 0
                    else f"question {iterations} · {_now()} · {' '.join(cmd[-4:])}")

        mark(f"\n----- {what} -----\n")
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur["iterations"] = iterations
            cur["running"] = True
            _write(cur)
        except Exception:
            pass

        try:
            # Five decisions, each with a reason learned the hard way:
            # 1) Popen rather than subprocess.run —— we need the pid, otherwise run_mve cannot be
            #    killed on stop (killing only the cockpit leaves an orphan that keeps writing
            #    to run_log)
            # 2) stdout redirected straight to the log file rather than a PIPE —— with
            #    communicate() nothing appears until the whole question finishes, so the run log on
            #    the dashboard stays blank and a human who clicked start cannot see what it is doing
            # 3) start_new_session —— detach from the process group so a dashboard exit does not
            #    take it down
            # 4) PYTHONUNBUFFERED —— without it run_mve's print uses block buffering (4KB), so
            #    nothing is flushed through a whole question and the dashboard's run log stays empty
            # 5) stdin=DEVNULL —— hit in practice: when the cockpit is started from a command line
            #    (python -c / non-interactive shell), the parent's stdin may be an already-closed fd
            #    and run_mve, inheriting it, dies immediately with
            #    Fatal Python error: init_sys_streams ... Bad file descriptor,
            #    exit code 1 without a single question run. stdout/stderr were both redirected and
            #    stdin was not handled, so it bit us.
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            # Use os.open to get a **bare fd** rather than passing a text handle: when the same
            # TextIOWrapper is shared, the second Popen was measured getting a bad fd again
            # (the same init_sys_streams / Bad file descriptor). Opening a fresh fd per iteration
            # and closing it right after is the steadiest.
            fd = os.open(str(LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(PROJECT), stdin=subprocess.DEVNULL,
                    stdout=fd, stderr=fd, env=env,
                    start_new_session=True,
                )
                _CHILDREN.append(proc)
                try:
                    cur = json.loads(STATE.read_text(encoding="utf-8"))
                    cur["child_pid"] = proc.pid
                    _write(cur)
                except Exception:
                    pass
                try:
                    code = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    mark(f"\n[cockpit] a single task exceeded {timeout // 60} minutes and was killed\n")
                    code = 124
                _CHILDREN.remove(proc)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                cur = json.loads(STATE.read_text(encoding="utf-8"))
                cur["child_pid"] = None
                _write(cur)
            except Exception:
                pass
        except Exception as e:
            mark(f"\n[cockpit] failed to start the process: {type(e).__name__}: {e}\n")
            code = 1

        if code != 0:
            # Any non-zero code stops the loop, and the code is put on the dashboard —— retrying a
            # structural error only pollutes the log, and without exposing the code the human
            # thinks it "finished" rather than "crashed"
            mark(f"\n[cockpit] '{JOB_LABEL.get(job, job)}' exit code {code}"
                 f" (non-zero → stop, no retry)\n")
            break

    label = JOB_LABEL.get(job, job)
    try:
        cur = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.update({
        "running": False, "stopped_at": _now(), "last_exit": code,
        "iterations": iterations, "child_pid": None,
        "why": "stopped" if code == 0 else f"stopped ({label} exit code {code})",
    })
    _write(cur)
    mark(f"\n===== COCKPIT FINISHED {_now()} · job {label} · "
         f"{iterations} in total · exit code {code} =====\n")
    return code


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "_run":
        m = "loop"
        if "--mode" in argv:
            m = argv[argv.index("--mode") + 1]
        r = 0          # 0 = non-stopping (isomorphic to the original while True), same default as start()
        if "--rounds" in argv:
            r = int(argv[argv.index("--rounds") + 1])
        t = ""
        if "--topic" in argv:
            t = argv[argv.index("--topic") + 1]
        curriculum = "--curriculum" in argv

        def _flag(name: str, default: str) -> str:
            return argv[argv.index(name) + 1] if name in argv else default

        job = _flag("--job", "practice")
        units = int(_flag("--units", "1"))
        transfer = int(_flag("--transfer", "0"))
        sys.exit(_run(m, t, r, job, units, transfer, curriculum))

    if argv and argv[0] == "status":
        st = read()
        print(json.dumps({**st, "tail": tail(20)}, ensure_ascii=False, indent=2))
        sys.exit(0)

    if argv and argv[0] == "stop":
        print(json.dumps(stop(), ensure_ascii=False))
        sys.exit(0)

    print(__doc__)
    print("Usage: python mve/pilot.py status | stop")
