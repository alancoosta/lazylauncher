#!/usr/bin/env python3
"""config_actions.py — config import/export dialogs.

Thin GTK glue over :mod:`config_io`: the file-chooser plumbing plus toast
feedback, lifted out of ManagerWindow so the window just composes widgets and
wires signals. The data logic (parse + merge) stays GTK-free in config_io.
"""
import subprocess
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .common import CONFIG_FILE, config_lock, load_config, save_config
from . import config_io


class ConfigActions:
    """Import / export / open the config file via GTK dialogs."""

    def __init__(self, window, on_reload, on_toast):
        self._win = window
        self._on_reload = on_reload   # rebuild the sidebar/lists after a change
        self._on_toast = on_toast     # flash a short message in the header

    def import_config(self, _widget=None):
        dialog = Gtk.FileChooserDialog(
            title="Import Scripts",
            parent=self._win,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        f = Gtk.FileFilter()
        f.set_name("JSON files")
        f.add_pattern("*.json")
        dialog.add_filter(f)
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            dialog.destroy()
            self._do_import(path)
        else:
            dialog.destroy()

    def _do_import(self, path):
        try:
            imported = config_io.read_config_file(path)
            if not imported.get("scripts"):
                self._on_toast("No scripts found in file")
                return
            with config_lock():
                cfg = load_config()
                cfg, added = config_io.merge_imported(cfg, imported)
                save_config(cfg)
            self._on_reload()
            self._on_toast(f"Imported {added} script(s)")
        except Exception as e:
            self._on_toast(f"Import failed: {e}")

    def export_config(self, _widget=None):
        dialog = Gtk.FileChooserDialog(
            title="Export Scripts",
            parent=self._win,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_current_name(".lazylauncher-config.json")
        f = Gtk.FileFilter()
        f.set_name("JSON files")
        f.add_pattern("*.json")
        dialog.add_filter(f)
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            dialog.destroy()
            try:
                config_io.export_config_to(path)
                self._on_toast(f"Exported to {Path(path).name}")
            except Exception as e:
                self._on_toast(f"Export failed: {e}")
        else:
            dialog.destroy()

    def open_config_file(self, _widget=None):
        """Open the config file in the system's default application."""
        try:
            subprocess.Popen(["xdg-open", str(CONFIG_FILE)])
            self._on_toast(f"Opening {CONFIG_FILE.name}")
        except Exception as e:
            self._on_toast(f"Could not open config: {e}")
