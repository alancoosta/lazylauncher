#!/usr/bin/env python3
"""ui_shared.py — small shared UI constants and factory helpers.

Extracted from manager.py so rows and forms can use them without importing the
whole window module.
"""
import uuid
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


_STOP_LABEL = "■  Stop"


_TIP_SORT_NAME_AZ = "Sort by name A→Z"


_TIP_SORT_NAME_ZA = "Sort by name Z→A"


_TIP_RUNNING_FIRST = "Running first"


_TIP_STOPPED_FIRST = "Stopped first"


def make_tab_button(label, mode, on_toggled, active=False):
    """Build a styled segmented toggle button (the Home/Editor and Scripts/Groups
    switchers share this look). An underscore in ``label`` marks a keyboard
    mnemonic (e.g. "_Home" -> Alt+H)."""
    btn = Gtk.ToggleButton(label=label, use_underline=True)
    btn.set_mode(False)
    btn.get_style_context().add_class("group-tab")
    btn.set_active(active)
    btn.connect("toggled", lambda b: on_toggled(b, mode))
    return btn


def new_script() -> dict:
    return {
        "id":          str(uuid.uuid4())[:8],
        "name":        "New Script",
        "command":     "",
        "working_dir": str(Path.home()),
        "enabled":     True,
        "description": "",
        "env_vars":    [],
        "port":        "",
        "confirm":     False,
        "silent":      True,
        "login_shell": True,
        "depends_on":  [],
        "groups":      [],
    }


def new_group(name: str) -> dict:
    return {
        "id":          str(uuid.uuid4())[:8],
        "name":        name,
        "description": "",
    }


def _is_dark_theme():
    """Detect if the current GTK theme is dark based on the window bg luminance."""
    settings = Gtk.Settings.get_default()
    if settings and settings.get_property("gtk-application-prefer-dark-theme"):
        return True
    style = Gtk.StyleContext()
    path = Gtk.WidgetPath()
    path.append_type(Gtk.Window)
    style.set_path(path)
    bg = style.get_background_color(Gtk.StateFlags.NORMAL)
    luminance = 0.299 * bg.red + 0.587 * bg.green + 0.114 * bg.blue
    return luminance < 0.5
