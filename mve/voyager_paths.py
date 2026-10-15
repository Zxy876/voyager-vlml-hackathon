#!/usr/bin/env python3
"""Shared configuration for Voyager control-related files (used by voyager_pilot / voyager_learn_reset).

On the VPS, Voyager is installed at /root/voyager, the venv is at /root/voyager-venv,
and the secret is at /root/.voyager_secret.
For local use or a different machine, override via environment variables — no code changes needed.
"""
from __future__ import annotations

import os
from pathlib import Path

# Voyager source / runtime directory (run_voyager_learn.py lives here)
VOYAGER_DIR = Path(os.environ.get("VOYAGER_DIR") or "/root/voyager")
# Python used to run Voyager (a venv with all dependencies)
VOYAGER_PY = os.environ.get("VOYAGER_PY") or "/root/voyager-venv/bin/python"
# File holding DEEPSEEK_API_KEY (read by both run_voyager.sh and voyager_pilot)
SECRET_FILE = Path(os.environ.get("VOYAGER_SECRET") or "/root/.voyager_secret")
# Checkpoint directory (relative to VOYAGER_DIR): skill library / curriculum progress / event history all live here
CKPT_DIR = os.environ.get("VOYAGER_CKPT_DIR") or "ckpt"
DEFAULT_MAX_ITER = int(os.environ.get("VOYAGER_MAX_ITER") or "160")
# Progress file written by run_voyager_learn.py per task
PROGRESS = VOYAGER_DIR / "voyager_progress.json"

# Real paths of the three reset targets
SKILL_DIR = VOYAGER_DIR / CKPT_DIR / "skill"          # reset library: learned JS skill library
CURRICULUM_DIR = VOYAGER_DIR / CKPT_DIR / "curriculum"  # reset memory: curriculum progress / QA cache
EVENTS_DIR = VOYAGER_DIR / CKPT_DIR / "events"          # reset memory: event history
