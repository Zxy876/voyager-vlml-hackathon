#!/usr/bin/env python3
"""Voyager cockpit: lets the MVE dashboard start / stop **real learning in MC** with one click.

Why a separate file (same argument as mve/pilot.py): the dashboard is an HTTP service, and process handles
hang off requests, so they're lost the moment the dashboard restarts — a user clicking "start" yet unable
to find out whether it's running is worse than not having the button. So the cockpit itself is an
**independent subprocess**, state is written to a file, and the dashboard only reads the file.

   Dashboard ──Popen──> voyager_pilot.py _run ──Popen──> run_voyager_learn.py (venv)
    │                   ↑                              │
    └── reads voyager_pilot_state.json ─────────────────┘

Stop semantics (mirroring the MVE dashboard's "stop" button):
  SIGTERM to the cockpit process → forwarded to run_voyager_learn.py (it has a SIGTERM handler that, after
  the current task ends, calls voyager.request_stop() to gracefully exit the learn loop) → the cockpit
  writes the final state after the child process exits. Skills are persisted per task, so a hard kill
  can't lose half a skill.

Two kinds of clearing (matching MVE's clear-library / clear-memory, both implemented by
voyager_learn_reset, backup first then delete):
  Clear library  — delete ckpt/skill/ (the learned JS skill library)
  Clear memory — delete ckpt/curriculum/ + ckpt/events/ (curriculum progress / QA cache / event history)

Usage (called by the dashboard, not run directly):
    python mve/voyager_pilot.py status | stop
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
STATE = ROOT / "voyager_pilot_state.json"
LOG = ROOT / "voyager_pilot.log"

# ---- Voyager install location (/root/voyager on the VPS; locally overridable via env vars) ----
from voyager_paths import (  # noqa: E402
    VOYAGER_DIR, VOYAGER_PY, SECRET_FILE, CKPT_DIR, DEFAULT_MAX_ITER, PROGRESS,
)

_JOB_LABEL = "real MC learning (auto-curriculum)"
# The child process (run_voyager_learn.py's learn loop) can take a long time on a single task;
# on stop, give a 120s grace period for the current task to wrap up, hard-kill only past that.
STOP_GRACE = 120

_CHILDREN: list[subprocess.Popen] = []


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write(state: dict[str, Any]) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _alive(pid: int) -> bool:
    """Is the process still alive (first reap zombies with waitpid, then probe with kill)."""
    try:
        wpid, _status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        return False
    return True


def _secret_key() -> str:
    """Read DEEPSEEK_API_KEY / ZHIPU_API_KEY from the secret file (supports both `KEY=val` and bare-key formats)."""
    if not SECRET_FILE.exists():
        return ""
    txt = SECRET_FILE.read_text(encoding="utf-8", errors="replace").strip()
    if not txt:
        return ""
    if "=" in txt.splitlines()[0]:
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith("ZHIPU_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return txt


def _secret_env() -> dict[str, str]:
    """Read all `KEY=val` lines from the secret file (ignoring comments/blank lines) and inject them into the child process environment.

    This way ZHIPU_API_KEY / ZHIPU_BASE_URL / ZHIPU_MODEL (or DEEPSEEK_*) written in the secret file
    are all forwarded as-is to run_voyager_learn, and neko_model decides which LLM backend to use by name.
    """
    out: dict[str, str] = {}
    if not SECRET_FILE.exists():
        return out
    for line in SECRET_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        out[k] = v
    return out


def _progress() -> dict[str, Any]:
    """Read the progress (completed/failed/iteration) written by run_voyager_learn.py."""
    if not PROGRESS.exists():
        return {}
    try:
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read() -> dict[str, Any]:
    """Current state. If the process is gone, automatically judge it stopped — don't trust the running bit in the file."""
    base: dict[str, Any] = {
        "running": False, "pid": None, "started_at": "", "stopped_at": "",
        "iterations": 0, "max_iterations": DEFAULT_MAX_ITER,
        "last_exit": None, "why": "never started", "completed_count": 0,
        "failed_count": 0, "completed": [], "failed": [], "mc_port": 25565,
    }
    if not STATE.exists():
        return base
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {**base, "why": "state file corrupted"}
    pid = st.get("pid")
    if st.get("running") and pid and not _alive(int(pid)):
        st["running"] = False
        st["why"] = "process exited (crashed or force-killed)"
    if not st.get("running") and not st.get("why"):
        st["why"] = "stopped"
    # Merge in live progress (even if the process has exited, surface the final progress)
    prog = _progress()
    if prog:
        st["completed_count"] = prog.get("completed_count", 0)
        st["failed_count"] = prog.get("failed_count", 0)
        st["completed"] = prog.get("completed", [])
        st["failed"] = prog.get("failed", [])
        if prog.get("iteration") is not None:
            st["iterations"] = prog["iteration"]
    return {**base, **st}


def tail(lines: int = 80) -> str:
    if not LOG.exists():
        return ""
    content = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _log(text: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(text)
        f.flush()


def start(*, mc_port: int = 25565, max_iterations: int = DEFAULT_MAX_ITER) -> dict[str, Any]:
    cur = read()
    if cur.get("running"):
        return {"ok": False, "error": f"already running (pid {cur.get('pid')})"}

    cmd = [
        str(VOYAGER_PY), str(VOYAGER_DIR / "run_voyager_learn.py"),
        "--mc-port", str(mc_port), "--ckpt-dir", CKPT_DIR,
        "--max-iterations", str(max_iterations),
    ]
    # At startup, inject the LLM credentials (ZHIPU_* / DEEPSEEK_*) from the secret file into the child process environment
    env = dict(os.environ)
    env.update(_secret_env())
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        cmd, cwd=str(VOYAGER_DIR), env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    st = {
        "running": True, "pid": proc.pid, "started_at": _now(),
        "stopped_at": "", "iterations": 0, "max_iterations": max_iterations,
        "mc_port": mc_port, "last_exit": None, "why": "learning",
        "completed_count": 0, "failed_count": 0, "completed": [], "failed": [],
        "stop_requested": False,
    }
    _write(st)
    sec = _secret_env()
    has_llm = any(k in sec for k in ("ZHIPU_API_KEY", "DEEPSEEK_API_KEY"))
    _log(f"\n===== starting Voyager learning {_now()} · mc_port {mc_port} · "
         f"limit {max_iterations} rounds · pid {proc.pid} · LLM credentials {'present' if has_llm else 'absent'} =====\n")
    if not has_llm:
        _log("[cockpit] warning: /root/.voyager_secret has no ZHIPU_API_KEY/DEEPSEEK_API_KEY, "
             "LLM calls will fail.\n")
    return {"ok": True, **st}


def stop() -> dict[str, Any]:
    st = read()
    pid = st.get("pid")
    if not st.get("running") or not pid:
        return {"ok": True, "stopped": True, "note": "wasn't running in the first place"}
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, ValueError, PermissionError, OSError) as e:
        return {"ok": True, "stopped": True, "note": f"process already gone ({e})"}

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
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur.update({"running": False, "stopped_at": _now(),
                        "why": "stopped (force-killed)", "child_pid": None})
            _write(cur)
        except Exception:
            pass
    _log(f"\n===== stopped {_now()} · {st.get('completed_count', 0)} skills learned =====\n")
    return {"ok": True, "stopped": True, "completed_count": st.get("completed_count", 0)}


def clear_log() -> None:
    if LOG.exists():
        LOG.unlink()


# ---------------------------------------------------------------------------
# The cockpit subprocess itself
# ---------------------------------------------------------------------------

def _install_signal_handlers() -> None:
    """On receiving SIGTERM, forward it to the running run_voyager_learn (which itself gracefully exits
    the learn loop), and mark the stop. Don't sys.exit directly: let the main loop write the final state
    after the child exits, for otherwise, exiting here would leave the child (started with
    start_new_session, not in this group) orphaned and still running."""

    def handler(signum, _frame):
        for p in _CHILDREN:
            try:
                p.terminate()  # SIGTERM → run_voyager_learn's handler calls request_stop()
            except Exception:
                pass
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur.update({"running": False, "stop_requested": True,
                        "stopped_at": _now(), "why": "stopped (requested)",
                        "child_pid": None})
            _write(cur)
        except Exception:
            pass

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _run(mc_port: int, max_iterations: int) -> int:
    _install_signal_handlers()
    logf = LOG.open("a", encoding="utf-8")
    try:
        code = _loop(mc_port, max_iterations, logf)
    finally:
        try:
            logf.close()
        except Exception:
            pass
    return code


def _loop(mc_port: int, max_iterations: int, logf) -> int:
    pid = os.getpid()

    def mark(text: str) -> None:
        logf.write(text)
        logf.flush()

    _write({
        "running": True, "pid": pid, "started_at": _now(), "stopped_at": "",
        "iterations": 0, "max_iterations": max_iterations, "mc_port": mc_port,
        "last_exit": None, "why": "learning", "completed_count": 0,
        "failed_count": 0, "completed": [], "failed": [], "stop_requested": False,
    })

    env = dict(os.environ)
    env.update(_secret_env())
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [
        str(VOYAGER_PY), str(VOYAGER_DIR / "run_voyager_learn.py"),
        "--mc-port", str(mc_port), "--ckpt-dir", CKPT_DIR,
        "--max-iterations", str(max_iterations),
    ]
    what = f"real MC learning (auto-curriculum) · mc_port {mc_port} · limit {max_iterations} rounds · {_now()}"
    mark(f"\n----- {what} -----\n")

    fd = os.open(str(LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(VOYAGER_DIR), env=env, stdin=subprocess.DEVNULL,
            stdout=fd, stderr=fd, start_new_session=True,
        )
        _CHILDREN.append(proc)
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur["child_pid"] = proc.pid
            _write(cur)
        except Exception:
            pass
        # Normally wait for the child to exit (learn loop finishes or exceeds max_iterations).
        # If a stop signal arrives, the handler has already forwarded SIGTERM to the child, and the child
        # exits at the task boundary; give STOP_GRACE grace here, hard-killing only past it, to avoid deadlock.
        try:
            code = proc.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            mark(f"\n[cockpit] child did not exit within {STOP_GRACE}s, force-killed\n")
            code = 124
        _CHILDREN.remove(proc)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    prog = _progress()
    try:
        cur = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.update({
        "running": False, "stopped_at": _now(), "last_exit": code,
        "child_pid": None, "stop_requested": False,
        "completed_count": (prog.get("completed_count", 0)
                           if prog else cur.get("completed_count", 0)),
        "failed_count": (prog.get("failed_count", 0)
                         if prog else cur.get("failed_count", 0)),
        "why": "stopped" if code == 0 else f"stopped (exit code {code})",
    })
    _write(cur)
    mark(f"\n===== cockpit finished {_now()} · exit code {code} · "
         f"{cur.get('completed_count', 0)} skills learned =====\n")
    return code


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "_run":
        mp = 25565
        mi = DEFAULT_MAX_ITER
        if "--mc-port" in argv:
            mp = int(argv[argv.index("--mc-port") + 1])
        if "--max-iterations" in argv:
            mi = int(argv[argv.index("--max-iterations") + 1])
        sys.exit(_run(mp, mi))

    if argv and argv[0] == "status":
        st = read()
        print(json.dumps({**st, "tail": tail(20)}, ensure_ascii=False, indent=2))
        sys.exit(0)

    if argv and argv[0] == "stop":
        print(json.dumps(stop(), ensure_ascii=False))
        sys.exit(0)

    print(__doc__)
    print("usage: python mve/voyager_pilot.py status | stop")
