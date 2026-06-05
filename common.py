#!/usr/bin/env python3
"""
LazyLauncher - Shared constants and helpers.
Used by both tray.py and manager.py.
"""

import json
import os
import time
from pathlib import Path

CONFIG_DIR       = Path.home() / ".config" / "lazylauncher"
CONFIG_FILE      = CONFIG_DIR / "config.json"
ICON_DIR         = CONFIG_DIR / "icons"
LOG_DIR          = CONFIG_DIR / "logs"
RUN_STATE_FILE   = CONFIG_DIR / "run_state.json"
ERROR_STATE_FILE = CONFIG_DIR / "error_state.json"

MAX_LOG_SIZE   = 1024 * 1024        # 1 MB per log file
MAX_LOG_AGE    = 30 * 24 * 60 * 60  # 30 days in seconds


def _safe_write(path: Path, data):
    """Write data to a file atomically using tmp + rename, with restricted permissions."""
    tmp = path.with_suffix(".tmp")
    if isinstance(data, str):
        tmp.write_text(data)
    else:
        tmp.write_bytes(data)
    os.replace(str(tmp), str(path))
    os.chmod(str(path), 0o600)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"scripts": [], "groups": []}


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _safe_write(CONFIG_FILE, json.dumps(cfg, indent=2))


def get_error_states() -> dict:
    """Return dict of script_id -> {exit_code, timestamp} for failed scripts."""
    try:
        if ERROR_STATE_FILE.exists():
            with open(ERROR_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def clear_error_state(script_id: str):
    """Clear the error state for a script."""
    if not script_id:
        return
    try:
        if not ERROR_STATE_FILE.exists():
            return
        with open(ERROR_STATE_FILE) as f:
            state = json.load(f)
        state.pop(script_id, None)
        _safe_write(ERROR_STATE_FILE, json.dumps(state))
    except Exception:
        pass


def _get_pid_start_time(pid: int) -> str:
    """Get process start time from /proc to detect PID reuse."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.split(")")[1].split()[19]
    except Exception:
        return ""


def _is_pid_alive(pid: int, start_time: str = "") -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if start_time:
        return _get_pid_start_time(pid) == start_time
    return True


def _mark_stopped(script_id: str):
    if not script_id:
        return
    try:
        if not RUN_STATE_FILE.exists():
            return
        with open(RUN_STATE_FILE) as f:
            state = json.load(f)
        state.pop(script_id, None)
        _safe_write(RUN_STATE_FILE, json.dumps(state))
    except Exception:
        pass


def get_running_ids() -> set:
    """Get set of script IDs whose process is still alive."""
    tracked = set()
    if RUN_STATE_FILE.exists():
        try:
            with open(RUN_STATE_FILE) as f:
                state = json.load(f)
            alive = {}
            for sid, entry in state.items():
                if isinstance(entry, dict):
                    pid = entry.get("pid", 0)
                    start_time = entry.get("start_time", "")
                else:
                    pid = entry
                    start_time = ""
                if _is_pid_alive(pid, start_time):
                    alive[sid] = entry
                    tracked.add(sid)
            if len(alive) != len(state):
                _safe_write(RUN_STATE_FILE, json.dumps(alive))
        except Exception:
            pass
    return tracked


def find_script_pid(script_id: str) -> int:
    """Find PID of a running script by tracked state."""
    if RUN_STATE_FILE.exists():
        try:
            with open(RUN_STATE_FILE) as f:
                state = json.load(f)
            entry = state.get(script_id)
            if entry is None:
                return 0
            if isinstance(entry, dict):
                pid = entry.get("pid", 0)
                start_time = entry.get("start_time", "")
            else:
                pid = entry
                start_time = ""
            if pid and _is_pid_alive(pid, start_time):
                return pid
        except Exception:
            pass
    return 0


def rotate_log(path: Path):
    """Rotate log file: delete if older than MAX_LOG_AGE, truncate if larger than MAX_LOG_SIZE."""
    try:
        if not path.exists():
            return
        # Time-based rotation: delete logs older than 30 days
        if time.time() - path.stat().st_mtime > MAX_LOG_AGE:
            path.unlink()
            return
        # Size-based rotation: keep the last half
        if path.stat().st_size > MAX_LOG_SIZE:
            data = path.read_bytes()
            path.write_bytes(data[len(data) // 2:])
    except OSError:
        pass


def log_path(script_id: str) -> Path:
    """Return the log file path for a script, ensuring the directory exists."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{script_id}.log"
