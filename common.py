#!/usr/bin/env python3
"""
LazyLauncher - Shared constants and helpers.
Used by both tray.py and manager.py.
"""

import fcntl
import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

VERSION = "1.0.0"

# Config (durable, user-editable, safe to sync/export) lives under XDG_CONFIG_HOME.
CONFIG_DIR  = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "lazylauncher"
CONFIG_FILE = CONFIG_DIR / ".lazylauncher-config.json"
ICON_DIR    = CONFIG_DIR / "icons"
LOCK_FILE   = CONFIG_DIR / ".lazylauncher.lock"

# Runtime state (logs, pids, last-exit) lives under XDG_STATE_HOME so that
# exporting/syncing the config never drags machine-specific runtime along.
STATE_DIR        = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "lazylauncher"
LOG_DIR          = STATE_DIR / "logs"
RUN_STATE_FILE   = STATE_DIR / "run_state.json"
ERROR_STATE_FILE = STATE_DIR / "error_state.json"
APP_LOG_FILE     = STATE_DIR / "lazylauncher.log"

MAX_LOG_SIZE   = 1024 * 1024        # 1 MB per log file
MAX_LOG_AGE    = 30 * 24 * 60 * 60  # 30 days in seconds


def migrate_state():
    """One-time move of runtime state out of the config dir into STATE_DIR.

    Earlier versions kept logs, run_state.json and error_state.json under
    ``~/.config/lazylauncher``. Relocate them so config stays portable. Safe to
    call on every boot: it only acts when the old locations still exist.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        moves = [
            (CONFIG_DIR / "logs", LOG_DIR),
            (CONFIG_DIR / "run_state.json", RUN_STATE_FILE),
            (CONFIG_DIR / "error_state.json", ERROR_STATE_FILE),
        ]
        for old, new in moves:
            if old.exists() and not new.exists():
                os.replace(str(old), str(new))
    except OSError:
        pass


_logger = None


def get_logger() -> logging.Logger:
    """Return the app logger, writing to STATE_DIR/lazylauncher.log (rotating)."""
    global _logger
    if _logger is None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log = logging.getLogger("lazylauncher")
        log.setLevel(logging.INFO)
        if not log.handlers:
            handler = RotatingFileHandler(
                str(APP_LOG_FILE), maxBytes=MAX_LOG_SIZE, backupCount=1
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            log.addHandler(handler)
        _logger = log
    return _logger


def _safe_write(path: Path, data):
    """Write data to a file atomically using a unique tmp + rename.

    The tmp file gets a unique name (mkstemp) in the same directory as the
    target so two concurrent writers never collide on a shared ``.tmp`` path.
    """
    mode = "w" if isinstance(data, str) else "wb"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, mode) as f:
            f.write(data)
        os.replace(tmp, str(path))
        os.chmod(str(path), 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def config_lock():
    """Serialize read-modify-write of the config between tray and manager.

    Any block that loads the config, mutates it, and saves it back should run
    inside ``with config_lock():`` so concurrent writers don't clobber each
    other's updates (a lost-update race).
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def normalize_env_vars(raw) -> list:
    """Return env vars as a list of ``{"key": str, "value": str}`` dicts.

    Accepts both the current list-of-dicts format and the legacy
    space-separated ``KEY=VALUE`` string (whitespace-split; tokens without
    ``=`` are dropped, mirroring the old runtime parser). Entries with an
    empty key are skipped. Keys are stripped; values are preserved as-is so
    they may contain spaces.
    """
    result = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()
            if key:
                result.append({"key": key, "value": str(item.get("value", ""))})
    elif isinstance(raw, str):
        for token in raw.split():
            if "=" in token:
                key, _, value = token.partition("=")
                key = key.strip()
                if key:
                    result.append({"key": key, "value": value})
    return result


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


def ensure_seed_config():
    """Seed a small example config on first run so the tray isn't empty.

    Only acts when no config file exists yet (installers may also seed one).
    Ships a 'Dev environment' group with two example scripts to demonstrate
    grouping and both run modes.
    """
    if CONFIG_FILE.exists():
        return
    cfg = {
        "scripts": [
            {
                "id": "example-files", "name": "Example: List Files",
                "command": "ls -lah", "working_dir": str(Path.home()),
                "pinned_icon": False, "enabled": True,
                "description": "Lists your home directory. Replace with your own!",
                "env_vars": [], "port": "", "confirm": False,
                "silent": False, "login_shell": True, "groups": ["example-dev"],
            },
            {
                "id": "example-clock", "name": "Example: Clock (silent)",
                "command": "date && sleep 2", "working_dir": str(Path.home()),
                "pinned_icon": False, "enabled": True,
                "description": "Runs in the background and notifies when done.",
                "env_vars": [], "port": "", "confirm": False,
                "silent": True, "login_shell": True, "groups": ["example-dev"],
            },
        ],
        "groups": [
            {"id": "example-dev", "name": "Dev environment",
             "description": "Example group — edit or delete me."},
        ],
    }
    save_config(cfg)


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
    """Rotate log file: delete if older than MAX_LOG_AGE, roll over if too large.

    Size-based rotation preserves one generation: the current log is renamed to
    ``<name>.log.1`` (replacing any previous one) and a fresh log is started on
    the next write, instead of destructively cutting the file in half.
    """
    try:
        if not path.exists():
            return
        # Time-based rotation: delete logs older than 30 days
        if time.time() - path.stat().st_mtime > MAX_LOG_AGE:
            path.unlink()
            return
        # Size-based rotation: keep the previous generation as .log.1
        if path.stat().st_size > MAX_LOG_SIZE:
            prev = path.with_suffix(".log.1")
            try:
                prev.unlink()
            except OSError:
                pass
            path.rename(prev)
    except OSError:
        pass


def log_path(script_id: str) -> Path:
    """Return the log file path for a script, ensuring the directory exists."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{script_id}.log"
