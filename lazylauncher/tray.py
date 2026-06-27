#!/usr/bin/env python3
"""
LazyLauncher - Tray daemon
Shows an AppIndicator with a static menu: "Manage Scripts…" (opens the manager)
and "Quit". Script launching lives in the manager window.
"""
import os
import subprocess
import sys
import signal
from pathlib import Path

from .common import (
    CONFIG_DIR, ICON_DIR, LOG_DIR,
    get_running_ids, migrate_state, get_logger, ensure_seed_config,
)
from .runner import (
    stop_script, _cleanup_stale_tempfiles, set_prompter,
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
from gi.repository import Gtk


DEFAULT_ICON_PATH = str(Path(__file__).parent / "icons" / "logo.svg")


_HICOLOR_ICON = Path.home() / ".local/share/icons/hicolor/scalable/apps/lazylauncher.svg"


DEFAULT_ICON  = "lazylauncher" if _HICOLOR_ICON.exists() else DEFAULT_ICON_PATH


from .ui_shared import GtkPrompter


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
    # Relaunch ourselves as `python -m lazylauncher manage`; ensure the package's
    # parent dir is importable regardless of the child's working directory.
    pkg_parent = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        filter(None, [str(pkg_parent), os.environ.get("PYTHONPATH", "")]))}
    _manager_proc = subprocess.Popen(
        [sys.executable, "-m", "lazylauncher", "manage"],
        env=env, start_new_session=True)


class LazyLauncherTray:
    def __init__(self):
        self.indicator = AppIndicator3.Indicator.new(
            "lazylauncher",
            DEFAULT_ICON,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("LazyLauncher")

        self._build_menu()

    def _build_menu(self):
        menu = Gtk.Menu()

        manage_item = Gtk.MenuItem(label="⚙  Manage Scripts…")
        manage_item.connect("activate", lambda _: open_manager())
        menu.append(manage_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _: self._quit())
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)

    def _shutdown(self):
        """Quit cleanly without prompting (used by signals/logout)."""
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
                    stop_script(sid)
        self._shutdown()


def main():
    migrate_state()
    ensure_seed_config()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_tempfiles()
    set_prompter(GtkPrompter())   # runner is GTK-free; give it our GTK3 dialogs

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
