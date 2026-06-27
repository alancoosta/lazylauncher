#!/usr/bin/env python3
"""
LazyLauncher - Shared constants and helpers.
Used by both tray.py and manager.py.
"""

import fcntl
import json
import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

VERSION = "1.0.0"

# Config (durable, user-editable, safe to sync/export) lives under XDG_CONFIG_HOME.
CONFIG_DIR  = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "lazylauncher"
CONFIG_FILE = CONFIG_DIR / ".lazylauncher-config.json"
CONFIG_BAK  = CONFIG_DIR / ".lazylauncher-config.json.bak"
ICON_DIR    = CONFIG_DIR / "icons"
LOCK_FILE   = CONFIG_DIR / ".lazylauncher.lock"

# Runtime state (logs, pids, last-exit) lives under XDG_STATE_HOME so that
# exporting/syncing the config never drags machine-specific runtime along.
STATE_DIR        = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "lazylauncher"
LOG_DIR          = STATE_DIR / "logs"
RUN_STATE_FILE   = STATE_DIR / "run_state.json"
ERROR_STATE_FILE = STATE_DIR / "error_state.json"
APP_LOG_FILE     = STATE_DIR / "lazylauncher.log"
RUN_LOCK_FILE    = STATE_DIR / ".run_state.lock"
UI_STATE_FILE    = STATE_DIR / "ui_state.json"  # last-used tab etc. (not user data)

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


def load_ui_state() -> dict:
    """Load persisted UI state (last-used tab, …). Best-effort: returns {} when
    the file is missing or corrupt so a bad write never blocks the window."""
    try:
        with open(UI_STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ui_state(**changes):
    """Merge ``changes`` into the persisted UI state. Best-effort: a failure to
    record UI preferences must never crash the app."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state = load_ui_state()
        state.update(changes)
        _safe_write(UI_STATE_FILE, json.dumps(state))
    except Exception:
        pass


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


@contextmanager
def run_state_lock():
    """Serialize read-modify-write of run_state.json across tray and manager.

    The run state is touched from several places that all do a load → mutate →
    save cycle: the tray marking scripts running/stopped, ``get_running_ids``
    pruning dead PIDs, and the manager's stop path. ``_safe_write`` makes each
    individual write atomic, but the read-modify-write *as a whole* is not, so
    concurrent writers used to clobber each other (a freshly started script
    could vanish from tracking). This lock closes that race.

    It uses a dedicated lock file (not ``config_lock``) so run-state updates and
    config writes can never deadlock against each other.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(RUN_LOCK_FILE, "w")
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

    A dict item with a truthy ``global`` field is a *live reference* to the
    global pool and carries no value of its own; its value is resolved at launch
    time by :func:`resolve_env_vars`. Two shapes are supported:

    - ``{"key": K, "global": True}`` — references the pool entry under the *same*
      key ``K`` (the original form).
    - ``{"key": K, "global": "X"}`` — an *alias*: the local key ``K`` takes the
      value of the pool entry ``X`` (``X`` may differ from ``K``).
    """
    result = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()
            if not key:
                continue
            g = item.get("global")
            if g:
                if isinstance(g, str) and g.strip():
                    result.append({"key": key, "global": g.strip()})
                else:
                    result.append({"key": key, "global": True})
            else:
                result.append({"key": key, "value": str(item.get("value", ""))})
    elif isinstance(raw, str):
        for token in raw.split():
            if "=" in token:
                key, _, value = token.partition("=")
                key = key.strip()
                if key:
                    result.append({"key": key, "value": value})
    return result


def global_env_map(cfg=None) -> dict:
    """Return the global env pool as a plain ``{key: value}`` dict.

    Reads ``config["global_env"]`` (loading the config if ``cfg`` is omitted).
    """
    if cfg is None:
        cfg = load_config()
    return {it["key"]: it.get("value", "")
            for it in normalize_env_vars(cfg.get("global_env"))}


def resolve_env_vars(items, global_map) -> list:
    """Resolve a script's ``env_vars`` against the global pool.

    ``items`` may contain own values (``{"key","value"}``) and live references
    to the pool (``{"key","global":...}``). A ``global`` of ``True`` references
    the pool entry under the same key; a string references that pool key (an
    *alias* — local key takes another global's value). Returns a fully-resolved
    list of ``{"key","value"}`` dicts. A reference whose pool key is no longer in
    the pool is dropped (the variable is simply not injected).
    """
    result = []
    for item in normalize_env_vars(items):
        g = item.get("global")
        if g:
            ref = g if isinstance(g, str) else item["key"]
            if ref in global_map:
                result.append({"key": item["key"], "value": global_map[ref]})
        else:
            result.append({"key": item["key"], "value": item.get("value", "")})
    return result


def load_config() -> dict:
    """Load the config, never silently destroying user data on corruption.

    A missing file is a normal first run. But a file that *exists* yet fails to
    parse must not be treated as "empty" — the next ``save_config`` would
    overwrite the (possibly recoverable) data. Instead we preserve the corrupt
    file aside, fall back to the last-good ``.bak``, and only then give up to a
    fresh seed.
    """
    if not CONFIG_FILE.exists():
        return {"scripts": [], "groups": [], "global_env": []}
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        # Preserve the corrupt file for manual recovery instead of clobbering it.
        # time_ns() keeps the name unique even for two corruptions in one second.
        try:
            corrupt = CONFIG_DIR / f".lazylauncher-config.corrupt-{time.time_ns()}.json"
            shutil.copy2(str(CONFIG_FILE), str(corrupt))
            get_logger().error("Config unreadable; preserved copy at %s", corrupt)
        except OSError:
            pass
        if CONFIG_BAK.exists():
            try:
                with open(CONFIG_BAK) as f:
                    data = json.load(f)
                # Repair the on-disk config from the backup so the file is valid
                # again. Without this the corrupt file stays in place and the next
                # save_config would copy it over the last-good .bak, destroying it.
                try:
                    shutil.copy2(str(CONFIG_BAK), str(CONFIG_FILE))
                except OSError:
                    pass
                return data
            except Exception:
                pass
    return {"scripts": [], "groups": [], "global_env": []}


def scripts_in_group(scripts, gid, only_enabled=True) -> list:
    """Return the scripts that belong to group ``gid``.

    Replaces the comprehension that was inlined across the manager and the
    group form. ``only_enabled`` drops disabled scripts (the usual case for
    run/stop/sort counts).
    """
    return [s for s in scripts
            if gid in s.get("groups", [])
            and (not only_enabled or s.get("enabled", True))]


def normalize_script(script: dict) -> dict:
    """Drop legacy keys no longer used (custom icon, pinned tray icon).

    Shared by the editor's save path and any other writer so schema cleanup
    lives in one place. Mutates and returns the dict.
    """
    script.pop("icon", None)
    script.pop("pinned_icon", None)
    return script


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Keep the previous good copy as a backup before overwriting, so a bad write
    # or a later corruption can be recovered from .bak.
    if CONFIG_FILE.exists():
        try:
            shutil.copy2(str(CONFIG_FILE), str(CONFIG_BAK))
        except OSError:
            pass
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
                "enabled": True,
                "description": "Lists your home directory. Replace with your own!",
                "env_vars": [], "port": "", "confirm": False,
                "silent": False, "login_shell": True, "groups": ["example-dev"],
            },
            {
                "id": "example-clock", "name": "Example: Clock (silent)",
                "command": "date && sleep 2", "working_dir": str(Path.home()),
                "enabled": True,
                "description": "Runs in the background and notifies when done.",
                "env_vars": [], "port": "", "confirm": False,
                "silent": True, "login_shell": True, "groups": ["example-dev"],
            },
        ],
        "groups": [
            {"id": "example-dev", "name": "Dev environment",
             "description": "Example group — edit or delete me."},
        ],
        "global_env": [],
    }
    save_config(cfg)


def get_error_states() -> dict:
    """Return dict of script_id -> {exit_code, timestamp} for failed scripts."""
    try:
        if ERROR_STATE_FILE.exists():
            with open(ERROR_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        get_logger().debug("get_error_states failed", exc_info=True)
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
        get_logger().debug("clear_error_state failed", exc_info=True)


def _get_pid_start_time(pid: int) -> str:
    """Get process start time from /proc to detect PID reuse."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.split(")")[1].split()[19]
    except Exception:
        return ""


def _is_pid_alive(pid: int, start_time: str = "") -> bool:
    # pid <= 0 is never a real tracked process: os.kill(0, 0) would target our
    # own process group and falsely report "alive", creating phantom entries.
    if pid <= 0:
        return False
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
        with run_state_lock():
            if not RUN_STATE_FILE.exists():
                return
            with open(RUN_STATE_FILE) as f:
                state = json.load(f)
            state.pop(script_id, None)
            _safe_write(RUN_STATE_FILE, json.dumps(state))
    except Exception:
        get_logger().debug("_mark_stopped failed", exc_info=True)


def get_running_ids() -> set:
    """Get set of script IDs whose process is still alive."""
    tracked = set()
    if RUN_STATE_FILE.exists():
        try:
            with run_state_lock():
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
            get_logger().debug("get_running_ids failed", exc_info=True)
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
            get_logger().debug("find_script_pid failed", exc_info=True)
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
