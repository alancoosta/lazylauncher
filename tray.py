#!/usr/bin/env python3
"""
LazyLauncher - Tray daemon
Reads config from ~/.config/lazylauncher/.lazylauncher-config.json and builds
an AppIndicator menu with all registered scripts.
Also spawns one extra indicator per script that has pinned_icon=true.
"""
import json
import subprocess
import sys
import signal
from pathlib import Path

from common import (
    CONFIG_DIR, CONFIG_FILE, ICON_DIR, LOG_DIR,
    get_running_ids, find_script_pid, load_config, log_path,
    migrate_state, get_logger, ensure_seed_config,
)
from runner import (
    run_script, _stop_script, _cleanup_stale_tempfiles, find_ports_for_pid,
    set_prompter,
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
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf


try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3


DEFAULT_ICON_PATH = str(Path(__file__).parent / "icons" / "logo.svg")


_HICOLOR_ICON = Path.home() / ".local/share/icons/hicolor/scalable/apps/lazylauncher.svg"


DEFAULT_ICON  = "lazylauncher" if _HICOLOR_ICON.exists() else DEFAULT_ICON_PATH


class _GtkPrompter:
    """GTK3 implementation of runner's prompter protocol (used by the tray)."""

    def confirm(self, title, message=""):
        d = Gtk.MessageDialog(
            flags=Gtk.DialogFlags.MODAL, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO, text=title)
        if message:
            d.format_secondary_text(message)
        resp = d.run()
        d.destroy()
        return resp == Gtk.ResponseType.YES

    def duplicate_run(self, label, pid, ports):
        port_info = ""
        if ports:
            port_info = "\nListening on port(s): " + ", ".join(str(p) for p in ports)
        d = Gtk.MessageDialog(
            flags=Gtk.DialogFlags.MODAL, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=f"'{label}' is already running.{port_info}")
        if pid:
            d.format_secondary_text(f"PID: {pid}")
        d.add_button("Cancel", Gtk.ResponseType.CANCEL)
        d.add_button("Run Another", Gtk.ResponseType.YES)
        kill_btn = d.add_button("Kill & Restart", Gtk.ResponseType.ACCEPT)
        kill_btn.get_style_context().add_class("destructive-action")
        resp = d.run()
        d.destroy()
        if resp == Gtk.ResponseType.ACCEPT:
            return "restart"
        if resp == Gtk.ResponseType.YES:
            return "another"
        return "cancel"


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
            # Render at the widget's scale factor so the icon stays crisp on HiDPI.
            sf = max(1, box.get_scale_factor())
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
                str(Path(__file__).parent / "icons" / "run-green.png"), 16 * sf, 16 * sf
            )
            surface = Gdk.cairo_surface_create_from_pixbuf(pixbuf, sf, None)
            box.pack_start(Gtk.Image.new_from_surface(surface), False, False, 0)
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
                self._config_mtime = mtime
                self._build_menu()   # surgical rebuild; no process restart, no self-restart loop
        running = get_running_ids()
        if running != self._last_running:
            self._last_running = running
            self._build_menu()
        return True

    def _shutdown(self):
        """Quit cleanly without prompting (used by signals/logout)."""
        for ind in self.pinned_indicators:
            ind.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
        Gtk.main_quit()

    def _quit(self, _widget=None):
        running = get_running_ids()
        if running:
            d = Gtk.MessageDialog(
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.NONE,
                text=f"{len(running)} script(s) still running.",
            )
            d.format_secondary_text("Stop them before quitting?")
            d.add_button("Leave running", Gtk.ResponseType.NO)
            d.add_button("Cancel", Gtk.ResponseType.CANCEL)
            stop_btn = d.add_button("Stop & quit", Gtk.ResponseType.YES)
            stop_btn.get_style_context().add_class("destructive-action")
            resp = d.run()
            d.destroy()
            if resp == Gtk.ResponseType.CANCEL:
                return
            if resp == Gtk.ResponseType.YES:
                for sid in running:
                    _stop_script(sid)
        self._shutdown()


def main():
    migrate_state()
    ensure_seed_config()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_tempfiles()
    set_prompter(_GtkPrompter())   # runner is GTK-free; give it our GTK3 dialogs

    log = get_logger()
    log.info("tray starting")

    tray = LazyLauncherTray()
    # Signals quit cleanly without a modal prompt (avoids blocking logout).
    signal.signal(signal.SIGTERM, lambda *_: tray._shutdown())
    signal.signal(signal.SIGINT,  lambda *_: tray._shutdown())

    try:
        Gtk.main()
    finally:
        log.info("tray stopped")


if __name__ == "__main__":
    main()
