#!/usr/bin/env python3
"""
LazyLauncher - Tray daemon
Reads config from ~/.config/lazylauncher/.lazylauncher-config.json and builds
an AppIndicator menu with all registered scripts.
Also spawns one extra indicator per script that has pinned_icon=true.
"""

import json
import os
import shlex
import subprocess
import sys
import signal
import threading
import time
from pathlib import Path

from common import (
    CONFIG_DIR, CONFIG_FILE, ICON_DIR, LOG_DIR,
    RUN_STATE_FILE, ERROR_STATE_FILE,
    _safe_write, load_config, save_config,
    get_error_states, get_running_ids,
    _get_pid_start_time, _is_pid_alive, _mark_stopped,
    find_script_pid, rotate_log, log_path,
    normalize_env_vars,
)

import gi

# Try Ayatana first (Ubuntu 22.04+), fall back to legacy AppIndicator3
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, GdkPixbuf

DEFAULT_ICON_PATH = str(Path(__file__).parent / "icons" / "logo.svg")
# Prefer themed icon name (installed to hicolor by install.sh) for crisp rendering;
# fall back to local file path for uninstalled/dev usage.
_HICOLOR_ICON = Path.home() / ".local/share/icons/hicolor/scalable/apps/lazylauncher.svg"
DEFAULT_ICON  = "lazylauncher" if _HICOLOR_ICON.exists() else DEFAULT_ICON_PATH
USER_SHELL    = os.environ.get("SHELL", "/bin/bash")

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


def _parse_env_vars(raw_env) -> dict:
    """Build the runtime environment, dropping dangerous keys.

    ``raw_env`` may be the new list-of-dicts format or a legacy
    space-separated ``KEY=VALUE`` string; both are normalized.
    """
    env = os.environ.copy()
    for item in normalize_env_vars(raw_env):
        if item["key"].upper() not in _BLOCKED_ENV_KEYS:
            env[item["key"]] = item["value"]
    return env


def _handle_duplicate_run(script, script_id, label):
    """Handle the case where a script is already running. Returns True to proceed, False to abort."""
    pid = find_script_pid(script_id)
    ports = find_ports_for_pid(pid) if pid else []

    port_info = ""
    if ports:
        port_list = ", ".join(str(p) for p in ports)
        port_info = f"\nListening on port(s): {port_list}"

    dialog = Gtk.MessageDialog(
        flags=Gtk.DialogFlags.MODAL,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.NONE,
        text=f"'{label}' is already running.{port_info}",
    )
    if pid:
        dialog.format_secondary_text(f"PID: {pid}")
    dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("Run Another", Gtk.ResponseType.YES)
    kill_btn = dialog.add_button("Kill & Restart", Gtk.ResponseType.ACCEPT)
    kill_btn.get_style_context().add_class("destructive-action")

    resp = dialog.run()
    dialog.destroy()

    if resp == Gtk.ResponseType.ACCEPT:
        if ports:
            for port in ports:
                kill_port(port)
        if pid:
            _kill_safe(os.killpg, os.getpgid(pid), signal.SIGTERM)
            _kill_safe(os.kill, pid, signal.SIGTERM)
        _mark_stopped(script_id)
        time.sleep(0.5)
    elif resp != Gtk.ResponseType.YES:
        return False
    return True


def _check_port_kill(script):
    """Check if port needs killing, with confirmation for privileged ports. Returns False to abort."""
    port_str = script.get("port", "").strip()
    if not port_str or not port_str.isdigit():
        return True
    port = int(port_str)
    if not _is_port_in_use(port):
        return True
    if port < 1024 or port in _WELL_KNOWN_PORTS:
        dialog = Gtk.MessageDialog(
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Kill process on privileged port :{port}?",
        )
        dialog.format_secondary_text(
            f"Port {port} is a {'privileged' if port < 1024 else 'well-known service'} port. "
            "Killing it may disrupt system services."
        )
        resp = dialog.run()
        dialog.destroy()
        if resp != Gtk.ResponseType.YES:
            return False
    kill_port(port)
    time.sleep(0.3)
    return True


def _run_silent(cmd, cwd, env, script_id, label):
    """Run a script silently in the background with log capture."""
    log_file = None
    if script_id:
        lp = log_path(script_id)
        rotate_log(lp)
        log_file = open(lp, "a")
        log_file.write(f"\n{'='*60}\n[{__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}] Running: {cmd}\n{'='*60}\n")
        log_file.flush()
    proc = subprocess.Popen(
        [USER_SHELL, "-ilc", cmd], cwd=cwd, env=env, start_new_session=True,
        stdout=log_file, stderr=log_file,
    )
    _mark_running(script_id, proc.pid)
    threading.Thread(
        target=_notify_on_done, args=(proc, label, script_id, log_file), daemon=True
    ).start()


def _run_in_terminal(cmd, cwd, env, script_id, label):
    """Run a script in a terminal emulator with log tee."""
    log_header = ""
    tee_suffix = ""
    if script_id:
        lp = log_path(script_id)
        rotate_log(lp)
        log_str = shlex.quote(str(lp))
        log_header = f"echo '\\n============================================================\\n['\\''$(date \"+%Y-%m-%d %H:%M:%S\")'\\''] Running: {shlex.quote(cmd)}\\n============================================================' >> {log_str}; "
        tee_suffix = f" 2>&1 | tee -a {log_str}"

    q_cwd = shlex.quote(cwd)
    q_label = label
    _prompt_msg = "Press Enter..."
    _gnome_body = f"{log_header}cd {q_cwd} && {cmd}{tee_suffix}; echo; echo '--- finished ---'; read -p '{_prompt_msg}'"
    _term_body = f"{log_header}cd {q_cwd} && {cmd}{tee_suffix}; echo; read -p '{_prompt_msg}'"
    _term_body_quoted = f"{USER_SHELL} -ilc {shlex.quote(_term_body)}"

    terminals = [
        ["gnome-terminal", "--title", q_label, "--", USER_SHELL, "-ilc", _gnome_body],
        ["xfce4-terminal", "--title", q_label, "-e", _term_body_quoted],
        ["xterm", "-title", q_label, "-e", _term_body_quoted],
        ["konsole", "--title", q_label, "-e", _term_body_quoted],
    ]

    for term in terminals:
        try:
            proc = subprocess.Popen(term, env=env, start_new_session=True)
            _mark_running(script_id, proc.pid)
            return
        except FileNotFoundError:
            continue

    # Fallback: run silently in background
    proc = subprocess.Popen([USER_SHELL, "-ilc", cmd], cwd=cwd, env=env, start_new_session=True)
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
        dialog = Gtk.MessageDialog(
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Run '{label}'?",
        )
        dialog.format_secondary_text("This script requires confirmation before running.")
        resp = dialog.run()
        dialog.destroy()
        if resp != Gtk.ResponseType.OK:
            return

    cwd = str(Path(cwd).expanduser())
    env = _parse_env_vars(script.get("env_vars", ""))

    if silent:
        _run_silent(cmd, cwd, env, script_id, label)
    else:
        _run_in_terminal(cmd, cwd, env, script_id, label)


def _auto_detect_port(script_id: str, pid: int):
    """Wait for the process to open a port and save it to the config if none is set."""
    for _ in range(15):
        time.sleep(1)
        if not _is_pid_alive(pid):
            return
        ports = find_ports_for_pid(pid)
        if ports:
            cfg = load_config()
            for s in cfg.get("scripts", []):
                if s.get("id") == script_id and not s.get("port", "").strip():
                    s["port"] = str(ports[0])
                    _safe_write(CONFIG_FILE, json.dumps(cfg, indent=2))
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


def find_ports_for_pid(pid: int) -> list[int]:
    """Find TCP ports a process (and its children) is listening on."""
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=3
        )
    except Exception:
        return []

    ports = _extract_ports_from_ss(result.stdout, str(pid))

    # Also check child processes
    if not ports:
        try:
            children = subprocess.run(
                ["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=3
            )
            for child_pid in children.stdout.strip().splitlines():
                if child_pid.strip().isdigit():
                    ports.extend(_extract_ports_from_ss(result.stdout, child_pid.strip()))
        except Exception:
            pass
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
    try:
        state = {}
        if RUN_STATE_FILE.exists():
            with open(RUN_STATE_FILE) as f:
                state = json.load(f)
        state[script_id] = {"pid": pid, "start_time": _get_pid_start_time(pid)}
        _safe_write(RUN_STATE_FILE, json.dumps(state))
    except Exception:
        pass
    # Auto-detect port if not configured
    cfg = load_config()
    for s in cfg.get("scripts", []):
        if s.get("id") == script_id and not s.get("port", "").strip():
            threading.Thread(
                target=_auto_detect_port, args=(script_id, pid), daemon=True
            ).start()
            break


def _scan_running_commands() -> set:
    """Scan /proc for commands matching configured scripts."""
    try:
        with open(CONFIG_FILE) as f:
            scripts = json.load(f).get("scripts", [])
        commands = [
            (s.get("id", ""), s.get("command", "").strip(), s.get("working_dir", "").strip())
            for s in scripts
        ]
    except Exception:
        return set()

    running_procs = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmdline = (p / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="ignore").strip()
            if not cmdline:
                continue
            try:
                cwd = str((p / "cwd").resolve())
            except OSError:
                cwd = ""
            running_procs.append((cmdline, cwd))
        except OSError:
            continue

    return _match_commands(running_procs, commands)


_BOUNDARY_CHARS = frozenset({" ", "'", '"', ";", "&", "|", "\n"})
_PREFIX_CHARS = frozenset({" ", "'", '"', "/", "="})


def _is_substring_match(haystack: str, needle: str) -> bool:
    """Check if needle appears as a standalone token in haystack."""
    idx = haystack.find(needle)
    if idx == -1:
        return False
    before_ok = idx == 0 or haystack[idx - 1] in _PREFIX_CHARS
    end = idx + len(needle)
    after_ok = end == len(haystack) or haystack[end] in _BOUNDARY_CHARS
    return before_ok and after_ok


def _command_appears_in(cmd, running_procs, working_dir) -> bool:
    """Check if a command string appears in any running process cmdline."""
    for rc, proc_cwd in running_procs:
        if not _is_substring_match(rc, cmd):
            continue
        if working_dir:
            expected = str(Path(working_dir).expanduser().resolve())
            if proc_cwd and proc_cwd != expected:
                continue
        return True
    return False


def _match_commands(running_procs, commands) -> set:
    """Match running processes against configured script commands."""
    detected = set()
    for sid, cmd, working_dir in commands:
        if not cmd or not sid:
            continue
        if _command_appears_in(cmd, running_procs, working_dir):
            detected.add(sid)
    return detected


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


_manager_proc = None

def open_manager():
    """Launch the manager GUI, reusing existing instance if already open."""
    global _manager_proc
    if _manager_proc is not None and _manager_proc.poll() is None:
        try:
            subprocess.Popen(["wmctrl", "-a", "LazyLauncher"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass
        return
    entry = Path(__file__).parent / "lazylauncher.py"
    _manager_proc = subprocess.Popen([sys.executable, str(entry), "manage"],
                                     start_new_session=True)


def _open_log_file(path: str):
    """Open a log file in the default text editor or terminal pager."""
    try:
        subprocess.Popen(["xdg-open", path], start_new_session=True)
    except FileNotFoundError:
        for term in ["gnome-terminal", "xfce4-terminal", "xterm"]:
            try:
                subprocess.Popen([term, "--", "less", "+G", path], start_new_session=True)
                return
            except FileNotFoundError:
                continue


def resolve_icon(script: dict) -> str:
    """Return the icon to use for a script. Per-script custom icons were
    removed, so this is always the default app icon."""
    return DEFAULT_ICON


# ── main indicator ──────────────────────────────────────────────────────────────

class LazyLauncherTray:
    def __init__(self):
        self.indicator = AppIndicator3.Indicator.new(
            "lazylauncher",
            DEFAULT_ICON,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("LazyLauncher")

        self.pinned_indicators: list = []
        self._last_running: set = set()
        self._build_menu()

        self._config_mtime: float = CONFIG_FILE.stat().st_mtime if CONFIG_FILE.exists() else 0
        GLib.timeout_add_seconds(3, self._poll)

    def _make_running_item(self, script, sid):
        """Build a menu item for a running script with port info."""
        item = Gtk.MenuItem()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
                str(Path(__file__).parent / "icons" / "run-green.png"), 16, 16
            )
            box.pack_start(Gtk.Image.new_from_pixbuf(pixbuf), False, False, 0)
        except Exception:
            box.pack_start(Gtk.Image.new_from_icon_name("media-playback-start", Gtk.IconSize.MENU), False, False, 0)
        pid = find_script_pid(sid)
        ports = find_ports_for_pid(pid) if pid else []
        port_suffix = f"  :{', :'.join(str(p) for p in ports)}" if ports else ""
        lbl = Gtk.Label(label=f"{script.get('name', 'Unnamed')}{port_suffix}", xalign=0)
        box.pack_start(lbl, True, True, 0)
        item.add(box)
        return item

    def _build_menu(self):
        config  = load_config()
        scripts = config.get("scripts", [])
        running = get_running_ids()

        menu = Gtk.Menu()

        if scripts:
            for script in scripts:
                if not script.get("enabled", True):
                    continue
                sid = script.get("id", "")
                if sid in running:
                    item = self._make_running_item(script, sid)
                else:
                    item = Gtk.MenuItem(label=script.get("name", "Unnamed"))
                item.set_tooltip_text(script.get("command", ""))
                item.connect("activate", lambda _w, s=script: run_script(s))
                menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())
        else:
            placeholder = Gtk.MenuItem(label="(no scripts — add some)")
            placeholder.set_sensitive(False)
            menu.append(placeholder)
            menu.append(Gtk.SeparatorMenuItem())

        manage_item = Gtk.MenuItem(label="⚙  Manage Scripts…")
        manage_item.connect("activate", lambda _: open_manager())
        menu.append(manage_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _: self._quit())
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)
        self._rebuild_pinned(scripts)

    def _rebuild_pinned(self, scripts: list):
        for old in self.pinned_indicators:
            old.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
        self.pinned_indicators.clear()
        for script in scripts:
            if script.get("pinned_icon", False) and script.get("enabled", True):
                self._create_pinned(script)

    def _create_pinned(self, script: dict):
        ind_id = f"lazylauncher-pinned-{script.get('id', script['name'])}"
        icon = resolve_icon(script)
        ind = AppIndicator3.Indicator.new(
            ind_id, icon,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        ind.set_title(script.get("name", ""))

        menu = Gtk.Menu()
        run_item = Gtk.MenuItem(label=f"▶  Run: {script.get('name', '')}")
        run_item.set_tooltip_text(script.get("command", ""))
        run_item.connect("activate", lambda _w, s=script: run_script(s))
        menu.append(run_item)

        sid = script.get("id", "")
        if sid:
            running = get_running_ids()
            status = " (running)" if sid in running else ""
            log_item = Gtk.MenuItem(label=f"📋  View Logs{status}")
            lp = log_path(sid)
            if lp.exists() and lp.stat().st_size > 0:
                log_item.connect("activate", lambda _w, p=str(lp): _open_log_file(p))
            else:
                log_item.set_sensitive(False)
            menu.append(log_item)

        menu.append(Gtk.SeparatorMenuItem())
        manage_item = Gtk.MenuItem(label="⚙  Manage Scripts…")
        manage_item.connect("activate", lambda _: open_manager())
        menu.append(manage_item)

        menu.show_all()
        ind.set_menu(menu)
        self.pinned_indicators.append(ind)

    def _poll(self) -> bool:
        if CONFIG_FILE.exists():
            mtime = CONFIG_FILE.stat().st_mtime
            if mtime != self._config_mtime:
                os.execv(sys.executable, [sys.executable] + sys.argv)
        running = get_running_ids()
        if running != self._last_running:
            self._last_running = running
            self._build_menu()
        return True

    def _quit(self, _widget=None):
        for ind in self.pinned_indicators:
            ind.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
        Gtk.main_quit()


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())
    signal.signal(signal.SIGINT,  lambda *_: Gtk.main_quit())

    LazyLauncherTray()
    Gtk.main()


if __name__ == "__main__":
    main()
