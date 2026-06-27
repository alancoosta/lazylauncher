#!/usr/bin/env python3
"""runner.py — script launch / process / port engine for LazyLauncher.

Starting scripts (in a terminal or silently), tracking their PIDs, probing and
killing ports, and sending completion notifications. Shared by the tray menu and
the manager UI so neither owns the other's launch logic.

GTK-free on purpose: the launch flow needs to ask the user yes/no questions
(confirm-before-run, kill-the-port, already-running), but the tray runs on GTK3
(AppIndicator) and the manager on GTK4 — and a single process can't load both.
So the dialogs are delegated to an injected *prompter* (see set_prompter); each
UI installs its own toolkit-specific implementation at startup.
"""
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .common import (
    RUN_STATE_FILE, ERROR_STATE_FILE,
    _safe_write, config_lock, run_state_lock, load_config, save_config,
    get_running_ids, find_script_pid,
    _get_pid_start_time, _is_pid_alive, _mark_stopped,
    rotate_log, log_path, resolve_env_vars,
    global_env_map, get_logger,
)


USER_SHELL    = os.environ.get("SHELL", "/bin/bash")


class _DefaultPrompter:
    """Used when no UI installed a prompter (tests, headless): always proceed."""
    def confirm(self, title, message=""):
        return True

    def duplicate_run(self, label, pid, ports):
        return "another"


_PROMPTER = _DefaultPrompter()


def set_prompter(prompter):
    """Install the UI's dialog implementation.

    ``prompter`` must provide ``confirm(title, message) -> bool`` and
    ``duplicate_run(label, pid, ports) -> "cancel" | "another" | "restart"``.
    """
    global _PROMPTER
    _PROMPTER = prompter or _DefaultPrompter()


_BLOCKED_ENV_KEYS = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
    "PYTHONPATH", "PATH", "IFS", "MALLOC_CHECK_", "LIBMOUNT_MTAB",
})


_WELL_KNOWN_PORTS = {80, 443, 53, 22, 25, 3306, 5432, 6379, 27017}


def _kill_safe(fn, *args):
    """Call fn(*args) ignoring OSError."""
    try:
        fn(*args)
    except OSError:
        pass


def _parse_env_vars(raw_env, global_map=None) -> dict:
    """Build the runtime environment, dropping dangerous keys.

    ``raw_env`` may be the new list-of-dicts format or a legacy
    space-separated ``KEY=VALUE`` string; both are normalized. Live references
    to the global pool (``{"key","global":True}``) are resolved against
    ``global_map``. The block-list is applied to the *resolved* keys, so a
    pool entry named ``PATH`` etc. is still filtered out.
    """
    env = os.environ.copy()
    for item in resolve_env_vars(raw_env, global_map or {}):
        if item["key"].upper() not in _BLOCKED_ENV_KEYS:
            env[item["key"]] = item["value"]
    return env


def _handle_duplicate_run(script, script_id, label):
    """Handle the case where a script is already running. Returns True to proceed, False to abort."""
    pid = find_script_pid(script_id)
    ports = find_ports_for_pid(pid) if pid else []

    choice = _PROMPTER.duplicate_run(label, pid, ports)

    if choice == "restart":
        if ports:
            for port in ports:
                kill_port(port)
        if pid:
            _kill_safe(os.killpg, os.getpgid(pid), signal.SIGTERM)
            _kill_safe(os.kill, pid, signal.SIGTERM)
        _mark_stopped(script_id)
        time.sleep(0.5)
    elif choice != "another":
        return False
    return True


def _process_on_port(port: int):
    """Return (name, pid) of the process listening on a TCP port, or (None, None)."""
    try:
        r = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            stripped = line.rstrip()
            if stripped.endswith(f":{port}") or f":{port} " in line:
                m = re.search(r'\(\("([^"]+)",pid=(\d+)', line)
                if m:
                    return m.group(1), int(m.group(2))
    except Exception:
        pass
    return None, None


def _check_port_kill(script):
    """Confirm before killing whatever holds the configured port. Returns False to abort."""
    port_str = script.get("port", "").strip()
    if not port_str or not port_str.isdigit():
        return True
    port = int(port_str)
    if not _is_port_in_use(port):
        return True

    name, pid = _process_on_port(port)
    privileged = port < 1024 or port in _WELL_KNOWN_PORTS
    if name:
        text = f"Kill {name} (PID {pid}) on port :{port}?"
    else:
        text = f"Kill the process on port :{port}?"
    secondary = ""
    if privileged:
        secondary = (
            f"Port {port} is a {'privileged' if port < 1024 else 'well-known service'} port. "
            "Killing it may disrupt system services."
        )
    if not _PROMPTER.confirm(text, secondary):
        return False

    kill_port(port)
    time.sleep(0.3)
    return True


def _run_silent(cmd, cwd, env, script_id, label, login_shell=True):
    """Run a script silently in the background with log capture."""
    log_file = None
    if script_id:
        lp = log_path(script_id)
        rotate_log(lp)
        log_file = open(lp, "a")
        log_file.write(f"\n{'='*60}\n[{__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}] Running: {cmd}\n{'='*60}\n")
        log_file.flush()
    shell_flag = "-ilc" if login_shell else "-c"
    proc = subprocess.Popen(
        [USER_SHELL, shell_flag, cmd], cwd=cwd, env=env, start_new_session=True,
        stdout=log_file, stderr=log_file,
    )
    _mark_running(script_id, proc.pid)
    threading.Thread(
        target=_notify_on_done, args=(proc, label, script_id, log_file), daemon=True
    ).start()


def _write_launcher(cmd, cwd, log_str, pidfile, login_shell=True):
    """Generate a temporary launcher script; return its path.

    Putting ``cmd`` on its own line eliminates the nested-quoting fragility of
    building a single shell string, and the launcher records the *real* shell
    PID (``$$``) into ``pidfile`` so the tray can track the right process
    instead of a terminal client that exits immediately.
    """
    L = ["#!/usr/bin/env bash", f"echo $$ > {shlex.quote(pidfile)}"]
    if login_shell:
        L += ['[ -f "$HOME/.profile" ] && . "$HOME/.profile"',
              '[ -f "$HOME/.bashrc" ]  && . "$HOME/.bashrc"']
    if log_str:
        L.append(
            'printf "\\n%s\\n[%s] Running\\n%s\\n" '
            '"============================================================" '
            '"$(date "+%Y-%m-%d %H:%M:%S")" '
            '"============================================================" '
            f">> {shlex.quote(log_str)}"
        )
    L.append(f"cd {shlex.quote(cwd)} || exit 1")
    if log_str:
        L += [f"{cmd} 2>&1 | tee -a {shlex.quote(log_str)}", "rc=${PIPESTATUS[0]}"]
    else:
        L += [cmd, "rc=$?"]
    L += ["echo", 'echo "--- finished (exit $rc) ---"', 'read -p "Press Enter..."']
    fd, path = tempfile.mkstemp(prefix="lazylauncher-", suffix=".sh")
    # Self-delete on normal exit so launchers don't pile up in /tmp. If the user
    # closes the terminal mid-run the rm is skipped, so the startup sweep
    # (_cleanup_stale_tempfiles) is the safety net for those.
    L.append(f"rm -f {shlex.quote(path)}")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(L) + "\n")
    os.chmod(path, 0o700)
    return path


def _cleanup_stale_tempfiles(max_age: float = 24 * 60 * 60):
    """Remove orphaned launcher scripts/pidfiles left behind by closed terminals.

    A launcher self-deletes when its terminal exits normally, but a window
    closed mid-run leaks its ``.sh`` (and ``.pid``). Sweep anything older than
    ``max_age`` on startup so /tmp never accumulates them indefinitely.
    """
    tmp = Path(tempfile.gettempdir())
    now = time.time()
    for pattern in ("lazylauncher-*.sh", "lazylauncher-*.pid"):
        for f in tmp.glob(pattern):
            try:
                if now - f.stat().st_mtime > max_age:
                    f.unlink()
            except OSError:
                pass


def _read_pidfile(pidfile, timeout=5.0):
    """Read the real PID written by the launcher (with a short poll)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            txt = Path(pidfile).read_text().strip()
            if txt.isdigit():
                return int(txt)
        except OSError:
            pass
        time.sleep(0.1)
    return 0


def _capture_terminal_pid(pidfile: str, script_id: str):
    """Poll for the launcher's real PID, record it, then drop the pidfile.

    Runs in a background thread: ``_read_pidfile`` blocks up to a few seconds
    waiting for the launcher to write ``$$``, and doing that on the GTK main
    thread would freeze the tray menu on every terminal launch.
    """
    real_pid = _read_pidfile(pidfile)
    try:
        os.unlink(pidfile)
    except OSError:
        pass
    _mark_running(script_id, real_pid)


def _run_in_terminal(cmd, cwd, env, script_id, label, login_shell=True):
    """Run a script in a terminal emulator with log tee, tracking the real PID."""
    log_str = ""
    if script_id:
        lp = log_path(script_id)
        rotate_log(lp)
        log_str = str(lp)

    # Unique, unpredictable pidfile (mkstemp, 0600) avoids both the shared-/tmp
    # clobber vector of a fixed name and collisions between concurrent runs.
    fd, pidfile = tempfile.mkstemp(prefix=f"lazylauncher-{script_id or 'x'}-", suffix=".pid")
    os.close(fd)
    launcher = _write_launcher(cmd, cwd, log_str, pidfile, login_shell)
    q = shlex.quote(launcher)

    terminals = [
        ["gnome-terminal", "--title", label, "--", USER_SHELL, launcher],
        ["foot",           "--title", label, USER_SHELL, launcher],          # native Wayland
        ["kitty",          "--title", label, USER_SHELL, launcher],
        ["wezterm", "start", "--", USER_SHELL, launcher],
        ["alacritty",      "--title", label, "-e", USER_SHELL, launcher],
        ["xfce4-terminal", "--title", label, "-e", f"{USER_SHELL} {q}"],
        ["konsole",        "--title", label, "-e", f"{USER_SHELL} {q}"],
        ["xterm",          "-title",  label, "-e", f"{USER_SHELL} {q}"],
    ]

    for term in terminals:
        try:
            subprocess.Popen(term, env=env, start_new_session=True)
        except FileNotFoundError:
            continue
        # Capture the shell PID off the main thread so the UI never freezes.
        threading.Thread(
            target=_capture_terminal_pid, args=(pidfile, script_id), daemon=True
        ).start()
        return

    # Fallback: run the launcher directly in the background (Popen pid is the shell)
    try:
        os.unlink(pidfile)
    except OSError:
        pass
    proc = subprocess.Popen([USER_SHELL, launcher], cwd=cwd, env=env, start_new_session=True)
    _mark_running(script_id, proc.pid)


def run_script(script: dict):
    """Execute a script entry in a terminal window."""
    cmd       = script.get("command", "")
    cwd       = script.get("working_dir") or str(Path.home())
    label     = script.get("name", "Script")
    silent    = script.get("silent", False)
    script_id = script.get("id", "")

    if not cmd.strip():
        return

    if script_id and script_id in get_running_ids():
        if not _handle_duplicate_run(script, script_id, label):
            return

    if not _check_port_kill(script):
        return

    _save_exit_status(script_id, 0)

    if script.get("confirm", False):
        if not _PROMPTER.confirm(
                f"Run '{label}'?",
                "This script requires confirmation before running."):
            return

    cwd = str(Path(cwd).expanduser())
    env = _parse_env_vars(script.get("env_vars", ""), global_env_map())
    login_shell = script.get("login_shell", True)

    if silent:
        _run_silent(cmd, cwd, env, script_id, label, login_shell)
    else:
        _run_in_terminal(cmd, cwd, env, script_id, label, login_shell)


def _auto_detect_port(script_id: str, pid: int):
    """Wait for the process to open a port and save it to the config if none is set."""
    for _ in range(15):
        time.sleep(1)
        if not _is_pid_alive(pid):
            return
        ports = find_ports_for_pid(pid)
        if ports:
            with config_lock():
                cfg = load_config()
                changed = False
                for s in cfg.get("scripts", []):
                    if s.get("id") == script_id and not s.get("port", "").strip():
                        s["port"] = str(ports[0])
                        changed = True
                if changed:
                    save_config(cfg)
            return


def _parse_ports_from_line(line: str, target_pid: str) -> list[int]:
    """Extract TCP ports from a single ss output line if it matches the PID."""
    if f"pid={target_pid}," not in line and f"pid={target_pid})" not in line:
        return []
    ports = []
    for part in line.split():
        if ":" in part and not part.startswith("("):
            port_str = part.rsplit(":", 1)[-1]
            if port_str.isdigit():
                port = int(port_str)
                if port not in ports:
                    ports.append(port)
    return ports


def _extract_ports_from_ss(output: str, target_pid: str) -> list[int]:
    """Extract TCP ports from ss output lines matching a PID."""
    ports = []
    for line in output.splitlines():
        ports.extend(_parse_ports_from_line(line, target_pid))
    return ports


def _descendant_pids(root: int) -> list[int]:
    """Return ``root`` plus every descendant PID (the whole process tree).

    Builds the parent->children map from a single ``/proc`` scan (no extra
    subprocess) so a server reached through intermediate processes — ``npm`` ->
    ``node``, ``docker``, a wrapper shell — is still matched. The previous
    one-level ``pgrep -P`` only saw direct children and missed grandchildren.

    PPID is field 4 of ``/proc/<pid>/stat``; the command name (field 2) may
    contain spaces and parentheses, so we split on the last ``)`` first.
    """
    children = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return [root]
    for p in entries:
        if not p.name.isdigit():
            continue
        try:
            stat = (p / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(p.name))

    seen, stack = [], [root]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.append(cur)
        stack.extend(children.get(cur, []))
    return seen


def find_ports_for_pid(pid: int) -> list[int]:
    """Find TCP ports a process (and its whole subtree) is listening on."""
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=3
        )
    except Exception:
        return []

    ports = []
    for descendant in _descendant_pids(pid):
        for port in _extract_ports_from_ss(result.stdout, str(descendant)):
            if port not in ports:
                ports.append(port)
    return ports


def _is_port_in_use(port: int) -> bool:
    """Check if a TCP port is currently in use."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_port(port: int) -> bool:
    """Kill the process listening on a given port."""
    try:
        result = subprocess.run(
            ["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=3
            )
            for pid_str in result.stdout.strip().splitlines():
                if pid_str.strip().isdigit():
                    _kill_safe(os.kill, int(pid_str.strip()), signal.SIGKILL)
            return True
        except Exception:
            return False


def _mark_running(script_id: str, pid: int):
    if not script_id:
        return
    # Refuse to track an invalid PID. A failed terminal launch used to mark
    # pid 0, which os.kill(0, 0) reports as "alive" forever — a phantom that
    # could never be cleared. Better to not track than to track a lie.
    if not pid or pid <= 0:
        get_logger().warning("Not tracking %s: no valid PID captured", script_id)
        return
    try:
        with run_state_lock():
            state = {}
            if RUN_STATE_FILE.exists():
                with open(RUN_STATE_FILE) as f:
                    state = json.load(f)
            state[script_id] = {"pid": pid, "start_time": _get_pid_start_time(pid)}
            _safe_write(RUN_STATE_FILE, json.dumps(state))
    except Exception:
        get_logger().warning(
            "Failed to record run state for %s", script_id, exc_info=True)
    # Auto-detect port if not configured
    cfg = load_config()
    for s in cfg.get("scripts", []):
        if s.get("id") == script_id and not s.get("port", "").strip():
            threading.Thread(
                target=_auto_detect_port, args=(script_id, pid), daemon=True
            ).start()
            break


def stop_script(script_id: str):
    """Terminate a tracked script by its process group, then mark it stopped.

    Escalates SIGTERM -> SIGKILL after a short grace period so a process that
    traps or ignores SIGTERM still dies. The single source of truth for stopping
    a script (used by both the manager and the tray).
    """
    pid = find_script_pid(script_id)
    if pid:
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
        if pgid is not None:
            _kill_safe(os.killpg, pgid, signal.SIGTERM)
        _kill_safe(os.kill, pid, signal.SIGTERM)
        time.sleep(0.5)
        if _is_pid_alive(pid):
            if pgid is not None:
                _kill_safe(os.killpg, pgid, signal.SIGKILL)
            _kill_safe(os.kill, pid, signal.SIGKILL)
    _mark_stopped(script_id)


def _build_notification(proc, script_id, log_file):
    """Wait for process, write log tail, return (status, icon)."""
    try:
        rc = proc.wait()
    except Exception:
        rc = -1
    if log_file:
        try:
            log_file.write(f"\n[exit {rc}]\n")
        except Exception:
            pass
        finally:
            try:
                log_file.close()
            except Exception:
                pass
    _mark_stopped(script_id)
    _save_exit_status(script_id, rc)

    if rc == 0:
        return "finished", "utilities-terminal"

    status = f"failed (exit {rc})"
    if script_id:
        try:
            lp = log_path(script_id)
            if lp.exists():
                lines = lp.read_text(errors="replace").strip().splitlines()
                tail = "\n".join(lines[-5:])
                if tail:
                    status += f"\n\n{tail}"
        except Exception:
            pass
    return status, "dialog-error"


def _notify_on_done(proc, label: str, script_id: str = "", log_file=None):
    """Wait for process to finish and send a desktop notification."""
    status, icon = _build_notification(proc, script_id, log_file)
    try:
        subprocess.Popen(
            ["notify-send", "-i", icon, "LazyLauncher", f"{label} {status}"],
            start_new_session=True,
        )
    except FileNotFoundError:
        pass


def _save_exit_status(script_id: str, rc: int):
    """Save the last exit status for a script."""
    if not script_id:
        return
    try:
        state = {}
        if ERROR_STATE_FILE.exists():
            with open(ERROR_STATE_FILE) as f:
                state = json.load(f)
        if rc in (0, 130, 143):
            state.pop(script_id, None)
        else:
            state[script_id] = {
                "exit_code": rc,
                "timestamp": __import__('datetime').datetime.now().isoformat(),
            }
        _safe_write(ERROR_STATE_FILE, json.dumps(state))
    except Exception:
        pass
