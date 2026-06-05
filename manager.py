#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LazyLauncher - Manager UI
A GTK3 window to add, edit, delete and reorder scripts.
Writes to ~/.config/lazylauncher/config.json.
The tray daemon hot-reloads that file automatically.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

from common import (
    CONFIG_DIR, CONFIG_FILE, ICON_DIR, LOG_DIR,
    RUN_STATE_FILE, ERROR_STATE_FILE,
    _safe_write, load_config, save_config,
    get_error_states, clear_error_state,
    get_running_ids, find_script_pid,
    _get_pid_start_time, _is_pid_alive, _mark_stopped,
    log_path, rotate_log,
)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, Pango, GLib

# ANSI color code → hex color (adapted for dark background)
ANSI_COLORS = {
    30: "#555555",  31: "#c0392b",  32: "#27ae60",  33: "#f39c12",
    34: "#3498db",  35: "#8e44ad",  36: "#1abc9c",  37: "#c4bdb5",
    90: "#7a746c",  91: "#e74c3c",  92: "#2ecc71",  93: "#f1c40f",
    94: "#5dade2",  95: "#af7ac5",  96: "#48c9b0",  97: "#ede8e1",
}
_ANSI_SGR_RE = re.compile(r'\x1b\[([0-9;]*)m')
_ANSI_ALL_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
_ANSI_OSC_RE = re.compile(r'\x1b\][^\x07]*\x07')

_STOP_LABEL = "■  Stop"

_TIP_SORT_NAME_AZ = "Sort by name A→Z"
_TIP_SORT_NAME_ZA = "Sort by name Z→A"
_TIP_RUNNING_FIRST = "Running first"
_TIP_STOPPED_FIRST = "Stopped first"


def _find_all_script_pids(script_id: str) -> list[int]:
    """Find ALL PIDs for a script by tracked state."""
    pid = find_script_pid(script_id)
    return [pid] if pid else []


def _kill_safe(fn, *args):
    """Call fn(*args) ignoring OSError (covers ProcessLookupError, PermissionError)."""
    try:
        fn(*args)
    except OSError:
        pass


def stop_script(script_id: str) -> bool:
    """Kill a running script and its entire process group. Returns True on success."""
    pids = _find_all_script_pids(script_id)
    if not pids:
        _mark_stopped(script_id)
        return False
    # Collect all unique PGIDs
    pgids = set()
    for pid in pids:
        try:
            pgids.add(os.getpgid(pid))
        except OSError:
            pass
    # SIGTERM all process groups + individual PIDs
    for pgid in pgids:
        _kill_safe(os.killpg, pgid, signal.SIGTERM)
    for pid in pids:
        _kill_safe(os.kill, pid, signal.SIGTERM)
    time.sleep(0.5)
    # SIGKILL any survivors
    for pgid in pgids:
        _kill_safe(os.killpg, pgid, signal.SIGKILL)
    for pid in pids:
        if _is_pid_alive(pid):
            _kill_safe(os.kill, pid, signal.SIGKILL)
    _mark_stopped(script_id)
    return True

CSS = """
* {
    font-family: 'Ubuntu', 'Cantarell', sans-serif;
}

window {
    background-color: #1e1b18;
    color: #ede8e1;
}

/* -- headerbar -- */
headerbar {
    background-color: #252219;
    border-bottom: 1px solid #35312b;
    padding: 4px 10px;
    min-height: 46px;
}
headerbar .title {
    color: #ede8e1;
    font-size: 14px;
    font-weight: 700;
}
headerbar .subtitle {
    color: #7a746c;
    font-size: 11px;
}

/* -- sidebar -- */
#sidebar {
    background-color: #252219;
    border-right: 1px solid #35312b;
    min-width: 230px;
}
#sidebar row {
    padding: 11px 16px;
    border-bottom: 1px solid #2c2923;
}
#sidebar row:selected {
    background-color: #332c24;
    border-left: 3px solid #d4836a;
}
#sidebar row:hover:not(:selected) {
    background-color: #2a2720;
}
.script-name {
    font-size: 13px;
    font-weight: 600;
    color: #ede8e1;
}
.script-cmd {
    font-size: 11px;
    color: #6e6860;
}
.script-disabled .script-name {
    color: #4a4640;
}
.badge-pinned {
    background-color: #d4836a;
    color: #ffffff;
    border-radius: 4px;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 700;
}
.badge-disabled {
    background-color: #35312b;
    color: #7a746c;
    border-radius: 4px;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 700;
}
.badge-error {
    background-color: #c0392b;
    color: #ffffff;
    border-radius: 4px;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 700;
}
.badge-running {
    background-color: #27ae60;
    color: #ffffff;
    border-radius: 4px;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 700;
}
.badge-port {
    background-color: #2980b9;
    color: #ffffff;
    border-radius: 4px;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 700;
}

/* -- toolbar + search -- */
#list-toolbar {
    background-color: #252219;
    border-bottom: 1px solid #35312b;
    padding: 6px 8px;
}
#list-toolbar entry {
    background-color: #1e1b18;
    color: #ede8e1;
    border: 1px solid #3d3830;
    border-radius: 7px;
    padding: 6px 10px;
    font-size: 12px;
}
#list-toolbar entry:focus {
    border-color: #d4836a;
}

/* -- form panel -- */
#form-panel {
    background-color: #1e1b18;
}
.form-label {
    font-size: 10px;
    font-weight: 700;
    color: #7a746c;
    letter-spacing: 0.9px;
    margin-bottom: 4px;
}
.form-entry {
    background-color: #252219;
    color: #ede8e1;
    border: 1px solid #3d3830;
    border-radius: 8px;
    padding: 9px 12px;
    font-size: 13px;
}
.form-entry:focus {
    border-color: #d4836a;
    background-color: #2a2720;
}
.form-hint {
    font-size: 11px;
    color: #4a4640;
    margin-top: 2px;
}
.section-header {
    font-size: 10px;
    font-weight: 700;
    color: #d4836a;
    letter-spacing: 1.1px;
    border-bottom: 1px solid #35312b;
    padding-bottom: 6px;
    margin-top: 20px;
    margin-bottom: 12px;
}

/* -- buttons: base (applies to all except headerbar controls) -- */
button:not(.titlebutton) {
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 12px;
    font-weight: 600;
    border: none;
    background-color: #35312b;
    color: #ede8e1;
}
headerbar button.titlebutton {
    background: transparent;
    border: none;
    box-shadow: none;
    padding: 4px;
    min-width: 24px;
    min-height: 24px;
}

/* -- colored button variants -- */
button.btn-primary {
    background-color: #d4836a;
    color: #ffffff;
    border-radius: 8px;
}
button.btn-primary:hover {
    background-color: #dc9278;
}
button.btn-danger {
    background-color: #c0392b;
    color: #ffffff;
    border-radius: 8px;
}
button.btn-danger:hover {
    background-color: #d44637;
}
button.btn-success {
    background-color: #27ae60;
    color: #ffffff;
    border-radius: 8px;
}
button.btn-success:hover {
    background-color: #2ecc71;
}
button.btn-secondary {
    background-color: #35312b;
    color: #c4bdb5;
    border: 1px solid #4a4640;
    border-radius: 8px;
}
button.btn-secondary:hover {
    background-color: #3d3830;
    color: #ede8e1;
}
button.btn-icon {
    padding: 2px;
    min-width: 20px;
    min-height: 20px;
    background-color: transparent;
    color: #7a746c;
    border: none;
    border-radius: 4px;
}
button.btn-icon:hover {
    background-color: #2a2720;
    color: #ede8e1;
}
button.btn-icon-run {
    color: #7a746c;
}
button.btn-icon-run:hover {
    color: #27ae60;
}
button.btn-icon-run.running {
    color: #27ae60;
}

/* -- emoji picker -- */
button.emoji-btn {
    font-family: "Noto Color Emoji", "Segoe UI Emoji", sans-serif;
    font-size: 24px;
    min-width: 44px;
    min-height: 44px;
    padding: 6px;
    background-color: #252219;
    border: 1px solid #35312b;
    border-radius: 8px;
    color: #ede8e1;
}
button.emoji-btn:hover {
    background-color: #35312b;
    border-color: #4a4640;
}

/* -- dialog -- */
dialog {
    background-color: #1e1b18;
}
dialog .dialog-action-area {
    padding: 8px;
}
dialog .dialog-action-area button {
    margin: 4px;
    min-width: 80px;
}

/* -- switches -- */
switch {
    background-color: #35312b;
    border-radius: 12px;
    border: 1px solid #4a4640;
}
switch:checked {
    background-color: #d4836a;
    border-color: #d4836a;
}
switch slider {
    background-color: #ede8e1;
    border-radius: 10px;
}

/* -- empty state -- */
.empty-state {
    color: #4a4640;
    font-size: 13px;
}

/* -- scrollbar -- */
scrollbar {
    background-color: #1e1b18;
}
scrollbar slider {
    background-color: #35312b;
    border-radius: 4px;
    min-width: 4px;
    min-height: 4px;
}
scrollbar slider:hover {
    background-color: #4a4640;
}

/* -- notebook tabs -- */
notebook header {
    background-color: #252219;
    border-bottom: 1px solid #35312b;
}
notebook header tabs tab {
    background-color: #252219;
    color: #7a746c;
    padding: 6px 16px;
    border: none;
}
notebook header tabs tab:checked {
    background-color: #1e1b18;
    color: #ede8e1;
    border-bottom: 2px solid #d4836a;
}
notebook header tabs tab:hover:not(:checked) {
    color: #c4bdb5;
}

/* -- log viewer -- */
.log-view {
    background-color: #151310;
    color: #c4bdb5;
    font-family: 'Ubuntu Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 12px;
    padding: 8px;
}

/* -- group tabs -- */
#group-tabs {
    background-color: #1e1b18;
    border-bottom: 1px solid #35312b;
    min-height: 32px;
}
.group-tab {
    background-color: transparent;
    color: #7a746c;
    border: none;
    border-radius: 0;
    padding: 6px 12px;
    font-size: 11px;
    font-weight: 600;
    border-bottom: 2px solid transparent;
    min-width: 40px;
}
.group-tab:checked {
    color: #ede8e1;
    border-bottom: 2px solid #d4836a;
    background-color: transparent;
}
.group-tab:hover:not(:checked) {
    color: #c4bdb5;
    background-color: #252219;
}

/* -- group checkbox -- */
checkbutton.group-check {
    font-size: 12px;
    color: #ede8e1;
    padding: 3px 0;
}

/* -- group card -- */
.group-card {
    background-color: #252219;
    border-radius: 8px;
    border: 1px solid #35312b;
}
.group-card:hover {
    border-color: #4a4640;
}
.group-card-selected {
    border-left: 3px solid #d4836a;
    background-color: #2a2720;
}
.group-card-header {
    border-bottom: 1px solid #2c2923;
}
"""


# -- data helpers ---------------------------------------------------------------


def new_script() -> dict:
    return {
        "id":          str(uuid.uuid4())[:8],
        "name":        "New Script",
        "command":     "",
        "working_dir": str(Path.home()),
        "icon":        "",
        "pinned_icon": False,
        "enabled":     True,
        "description": "",
        "env_vars":    "",
        "port":        "",
        "confirm":     False,
        "silent":      True,
        "groups":      [],
    }


def new_group(name: str) -> dict:
    return {
        "id":          str(uuid.uuid4())[:8],
        "name":        name,
        "description": "",
    }


EMOJIS = [
    "🚀", "⚡", "🔥", "✅", "❌", "⚠️", "🔧", "📦", "🏗️", "🎯",
    "🌐", "💻", "🖥️", "📱", "⌨️", "🐳", "🦀", "🐍", "☕", "🟢",
    "🔴", "🟡", "🔵", "🟣", "⚙️", "🔒", "🔑", "📂", "📁", "💾",
    "⬆️", "⬇️", "➡️", "⬅️", "↩️", "🔄", "▶️", "⏸️", "⏹️", "🛑",
    "🌟", "⭐", "🌙", "☀️", "🌈", "🍀", "🌍", "🌊", "❄️", "💎",
    "🛠️", "📡", "🔋", "🔌", "📊", "📈", "📉", "🗓️", "🔔", "🔕",
    "☁️", "☂️", "🎨", "🎵", "🎮", "🧪", "🧰", "💡", "🔭", "🤖",
]


def _render_emoji_to_png(emoji: str) -> str:
    """Render an emoji to a PNG file in ICON_DIR and return its path."""
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    hex_name = "-".join(f"{ord(c):x}" for c in emoji)
    path = ICON_DIR / f"emoji-{hex_name}.png"
    if path.exists():
        return str(path)

    big = 128
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 109
        )
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), emoji, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (big - w) / 2 - bbox[0]
    y = (big - h) / 2 - bbox[1]
    draw.text((x, y), emoji, font=font, embedded_color=True)

    img = img.resize((48, 48), Image.LANCZOS)
    img.save(str(path))
    return str(path)


# -- script row widget ----------------------------------------------------------

class ScriptRow(Gtk.ListBoxRow):
    _shared_error_states: dict = {}
    _shared_running_ids: set = set()
    _on_run = None
    _on_stop = None

    def __init__(self, script: dict):
        super().__init__()
        self.script = script
        self._build()

    def _build(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(4)
        box.set_margin_end(4)

        self._top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        name_lbl = Gtk.Label(label=self.script.get("name", "Unnamed"))
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_hexpand(True)
        name_lbl.get_style_context().add_class("script-name")
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self._top.pack_start(name_lbl, True, True, 0)

        # Action buttons
        self._action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self._top.pack_end(self._action_box, False, False, 0)

        # Badge container (right side of top row)
        self._badge_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._top.pack_end(self._badge_box, False, False, 0)

        self._update_badges()

        if not self.script.get("enabled", True):
            self.get_style_context().add_class("script-disabled")

        box.pack_start(self._top, False, False, 0)

        cmd = self.script.get("command", "")
        if cmd:
            cmd_lbl = Gtk.Label(label=cmd[:60] + ("..." if len(cmd) > 60 else ""))
            cmd_lbl.set_halign(Gtk.Align.START)
            cmd_lbl.get_style_context().add_class("script-cmd")
            cmd_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            box.pack_start(cmd_lbl, False, False, 0)

        self.add(box)
        self.show_all()

    def _update_badges(self):
        """Update badge and action button widgets in-place."""
        for child in self._badge_box.get_children():
            self._badge_box.remove(child)
        for child in self._action_box.get_children():
            self._action_box.remove(child)

        sid = self.script.get("id", "")
        is_running = sid in ScriptRow._shared_running_ids

        # Action buttons
        stop_btn = Gtk.Button()
        stop_btn.set_image(Gtk.Image.new_from_icon_name("media-playback-stop-symbolic", Gtk.IconSize.MENU))
        stop_btn.get_style_context().add_class("btn-icon")
        stop_btn.set_tooltip_text("Stop")
        stop_btn.set_sensitive(is_running)
        if ScriptRow._on_stop:
            stop_btn.connect("clicked", lambda _, s=self.script: ScriptRow._on_stop(s))

        run_btn = Gtk.Button()
        run_btn.set_image(Gtk.Image.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.MENU))
        run_btn.get_style_context().add_class("btn-icon")
        run_btn.get_style_context().add_class("btn-icon-run")
        if is_running:
            run_btn.get_style_context().add_class("running")
        run_btn.set_tooltip_text("Run")
        if ScriptRow._on_run:
            run_btn.connect("clicked", lambda _, s=self.script: ScriptRow._on_run(s))

        self._action_box.pack_start(run_btn, False, False, 0)
        self._action_box.pack_start(stop_btn, False, False, 0)

        # Badges
        port_str = self.script.get("port", "").strip()
        if port_str:
            badge = Gtk.Label(label=f":{port_str}")
            badge.get_style_context().add_class("badge-port")
            badge.set_tooltip_text(f"Port {port_str}")
            self._badge_box.pack_start(badge, False, False, 0)

        errors = ScriptRow._shared_error_states
        if sid and sid in errors:
            err = errors[sid]
            badge = Gtk.Label(label=f"ERR:{err.get('exit_code', '?')}")
            badge.get_style_context().add_class("badge-error")
            badge.set_tooltip_text(f"Last run failed with exit code {err.get('exit_code', '?')}")
            self._badge_box.pack_start(badge, False, False, 0)

        if self.script.get("pinned_icon"):
            badge = Gtk.Label(label="PIN")
            badge.get_style_context().add_class("badge-pinned")
            self._badge_box.pack_start(badge, False, False, 0)

        if not self.script.get("enabled", True):
            badge = Gtk.Label(label="OFF")
            badge.get_style_context().add_class("badge-disabled")
            self._badge_box.pack_start(badge, False, False, 0)

        self._action_box.show_all()
        self._badge_box.show_all()


# -- group row widget -----------------------------------------------------------

class GroupRow(Gtk.ListBoxRow):
    """Sidebar row for a group – mirrors ScriptRow layout with script sub-rows."""
    _on_run_group = None
    _on_stop_group = None
    _on_run_script = None
    _on_stop_script = None
    _on_select_script_settings = None
    _on_select_script_logs = None
    _on_open_terminal = None
    _shared_running_ids: set = set()
    _sort_modes: dict = {}  # gid -> sort mode

    def __init__(self, group: dict, scripts: list):
        super().__init__()
        self.group = group
        self._scripts = scripts
        self._build()

    def _build(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(4)
        box.set_margin_end(4)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        name_lbl = Gtk.Label(label=self.group.get("name", "Unnamed"))
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_hexpand(True)
        name_lbl.get_style_context().add_class("script-name")
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        top.pack_start(name_lbl, True, True, 0)

        # Action buttons
        self._action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        top.pack_end(self._action_box, False, False, 0)

        # Badge container
        self._badge_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        top.pack_end(self._badge_box, False, False, 0)

        self._update_badges()

        box.pack_start(top, False, False, 0)

        desc = self.group.get("description", "").strip()
        count = len(self._scripts)
        suffix = "s" if count != 1 else ""
        sub = desc if desc else f"{count} script{suffix}"
        sub_text = sub[:60] + ("..." if len(sub) > 60 else "")
        sub_lbl = Gtk.Label(label=sub_text)
        sub_lbl.set_halign(Gtk.Align.START)
        sub_lbl.get_style_context().add_class("script-cmd")
        sub_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        box.pack_start(sub_lbl, False, False, 0)

        if self._scripts:
            box.pack_start(self._build_sort_bar(), False, False, 0)

        # Script sub-rows — apply current sort
        gid = self.group.get("id", "")
        sort_mode = GroupRow._sort_modes.get(gid)
        if sort_mode and self._scripts:
            self._scripts = self._sort_list(self._scripts, sort_mode)

        if self._scripts:
            for script in self._scripts:
                box.pack_start(self._build_script_row(script), False, False, 0)
        else:
            empty_lbl = Gtk.Label(label="  No scripts in this group")
            empty_lbl.set_halign(Gtk.Align.START)
            empty_lbl.get_style_context().add_class("form-hint")
            empty_lbl.set_margin_start(6)
            empty_lbl.set_margin_top(4)
            empty_lbl.set_margin_bottom(4)
            box.pack_start(empty_lbl, False, False, 0)

        self.add(box)
        self.show_all()

    def _build_sort_bar(self):
        sort_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        sort_bar.set_margin_start(6)
        sort_bar.set_margin_end(2)
        sort_bar.set_margin_top(2)
        sort_bar.set_margin_bottom(2)
        for label, mode, tip in [
            ("A→Z", "name_asc", _TIP_SORT_NAME_AZ),
            ("Z→A", "name_desc", _TIP_SORT_NAME_ZA),
            ("P↑", "port_asc", "Sort by port 1→100"),
            ("P↓", "port_desc", "Sort by port 100→1"),
            ("▶↑", "running_first", _TIP_RUNNING_FIRST),
            ("■↑", "stopped_first", _TIP_STOPPED_FIRST),
        ]:
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("btn-icon")
            btn.set_tooltip_text(tip)
            btn.connect("clicked", lambda _, m=mode: self._apply_sort(m))
            sort_bar.pack_start(btn, False, False, 0)
        return sort_bar

    def _build_script_row(self, script):
        sid = script.get("id", "")
        is_running = sid in GroupRow._shared_running_ids

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row_box.set_margin_start(6)
        row_box.set_margin_end(2)
        row_box.set_margin_top(2)
        row_box.set_margin_bottom(2)

        name_evbox = Gtk.EventBox()
        sname = Gtk.Label(label=script.get("name", "Unnamed"))
        sname.set_halign(Gtk.Align.START)
        sname.get_style_context().add_class("script-cmd")
        sname.set_ellipsize(Pango.EllipsizeMode.END)
        name_evbox.add(sname)
        if GroupRow._on_select_script_settings:
            name_evbox.connect("button-press-event",
                               lambda _w, _ev, s=script: GroupRow._on_select_script_settings(s))
        row_box.pack_start(name_evbox, True, True, 0)

        s_stop = Gtk.Button()
        s_stop.set_image(Gtk.Image.new_from_icon_name("media-playback-stop-symbolic", Gtk.IconSize.MENU))
        s_stop.get_style_context().add_class("btn-icon")
        s_stop.set_tooltip_text("Stop")
        s_stop.set_sensitive(is_running)
        if GroupRow._on_stop_script:
            s_stop.connect("clicked", lambda _, s=script: GroupRow._on_stop_script(s))

        s_run = Gtk.Button()
        s_run.set_image(Gtk.Image.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.MENU))
        s_run.get_style_context().add_class("btn-icon")
        s_run.get_style_context().add_class("btn-icon-run")
        if is_running:
            s_run.get_style_context().add_class("running")
        s_run.set_tooltip_text("Run")
        if GroupRow._on_run_script:
            s_run.connect("clicked", lambda _, s=script: GroupRow._on_run_script(s))

        s_term = Gtk.Button()
        s_term.set_image(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic", Gtk.IconSize.MENU))
        s_term.get_style_context().add_class("btn-icon")
        s_term.set_tooltip_text("Open Terminal")
        if GroupRow._on_open_terminal:
            s_term.connect("clicked", lambda _, s=script: GroupRow._on_open_terminal(s))

        s_logs = Gtk.Button()
        s_logs.set_image(Gtk.Image.new_from_icon_name("text-x-generic-symbolic", Gtk.IconSize.MENU))
        s_logs.get_style_context().add_class("btn-icon")
        s_logs.set_tooltip_text("Logs")
        if GroupRow._on_select_script_logs:
            s_logs.connect("clicked", lambda _, s=script: GroupRow._on_select_script_logs(s))

        s_settings = Gtk.Button()
        s_settings.set_image(Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.MENU))
        s_settings.get_style_context().add_class("btn-icon")
        s_settings.set_tooltip_text("Settings")
        if GroupRow._on_select_script_settings:
            s_settings.connect("clicked", lambda _, s=script: GroupRow._on_select_script_settings(s))

        row_box.pack_end(s_stop, False, False, 0)
        row_box.pack_end(s_run, False, False, 0)
        row_box.pack_end(s_term, False, False, 0)
        row_box.pack_end(s_logs, False, False, 0)
        row_box.pack_end(s_settings, False, False, 0)

        port_str = script.get("port", "").strip()
        if port_str:
            port_badge = Gtk.Label(label=f":{port_str}")
            port_badge.get_style_context().add_class("badge-port")
            port_badge.set_tooltip_text(f"Port {port_str}")
            row_box.pack_end(port_badge, False, False, 0)

        return row_box

    def _update_badges(self):
        for child in self._badge_box.get_children():
            self._badge_box.remove(child)
        for child in self._action_box.get_children():
            self._action_box.remove(child)

        any_running = any(
            s.get("id", "") in GroupRow._shared_running_ids
            for s in self._scripts
        )

        run_btn = Gtk.Button()
        run_btn.set_image(Gtk.Image.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.MENU))
        run_btn.get_style_context().add_class("btn-icon")
        run_btn.get_style_context().add_class("btn-icon-run")
        if any_running:
            run_btn.get_style_context().add_class("running")
        run_btn.set_tooltip_text("Run All")
        run_btn.set_sensitive(len(self._scripts) > 0)
        if GroupRow._on_run_group:
            run_btn.connect("clicked", lambda _, g=self.group: GroupRow._on_run_group(g))

        stop_btn = Gtk.Button()
        stop_btn.set_image(Gtk.Image.new_from_icon_name("media-playback-stop-symbolic", Gtk.IconSize.MENU))
        stop_btn.get_style_context().add_class("btn-icon")
        stop_btn.set_tooltip_text("Stop All")
        stop_btn.set_sensitive(any_running)
        if GroupRow._on_stop_group:
            stop_btn.connect("clicked", lambda _, g=self.group: GroupRow._on_stop_group(g))

        self._action_box.pack_start(run_btn, False, False, 0)
        self._action_box.pack_start(stop_btn, False, False, 0)

        # Count badge
        count = len(self._scripts)
        if count:
            badge = Gtk.Label(label=str(count))
            badge.get_style_context().add_class("badge-port")
            badge.set_tooltip_text(f"{count} script{'s' if count != 1 else ''}")
            self._badge_box.pack_start(badge, False, False, 0)

        self._action_box.show_all()
        self._badge_box.show_all()

    def _apply_sort(self, mode):
        gid = self.group.get("id", "")
        GroupRow._sort_modes[gid] = mode
        # Rebuild this row in-place
        for child in self.get_children():
            self.remove(child)
        self._scripts = self._sort_list(self._scripts, mode)
        self._build()

    @staticmethod
    def _sort_list(scripts, mode):
        running = GroupRow._shared_running_ids

        def _port_key(s):
            p = s.get("port", "").strip()
            return int(p) if p.isdigit() else 0

        s = list(scripts)
        if mode == "name_asc":
            s.sort(key=lambda x: x.get("name", "").lower())
        elif mode == "name_desc":
            s.sort(key=lambda x: x.get("name", "").lower(), reverse=True)
        elif mode == "port_asc":
            s.sort(key=_port_key)
        elif mode == "port_desc":
            s.sort(key=_port_key, reverse=True)
        elif mode == "running_first":
            s.sort(key=lambda x: x.get("id", "") not in running)
        elif mode == "stopped_first":
            s.sort(key=lambda x: x.get("id", "") in running)
        return s


# -- edit form ------------------------------------------------------------------

class ScriptForm(Gtk.Box):
    """Right-hand panel - shows when a script is selected."""

    def __init__(self, on_save, on_delete, on_run, on_duplicate=None, on_stop=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_name("form-panel")
        self._on_save   = on_save
        self._on_delete = on_delete
        self._on_run    = on_run
        self._on_duplicate = on_duplicate
        self._on_stop   = on_stop
        self._script    = None
        self._loading   = False
        self._group_checkboxes = {}
        self._build()

    def _build(self):
        # Notebook with Settings and Logs tabs
        self.notebook = Gtk.Notebook()
        self.notebook.set_tab_pos(Gtk.PositionType.TOP)

        # Run / Stop buttons at the right of the tab bar
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.set_margin_end(6)
        btn_box.set_margin_top(4)
        btn_box.set_margin_bottom(4)

        self.run_btn = Gtk.Button(label="▶  Run")
        self.run_btn.get_style_context().add_class("btn-success")
        self.run_btn.connect("clicked", self._run_current)
        btn_box.pack_start(self.run_btn, False, False, 0)

        self.stop_btn = Gtk.Button(label=_STOP_LABEL)
        self.stop_btn.get_style_context().add_class("btn-danger")
        self.stop_btn.connect("clicked", lambda _: self._on_stop and self._on_stop(self._script))
        self.stop_btn.set_sensitive(False)
        btn_box.pack_start(self.stop_btn, False, False, 0)

        self.notebook.set_action_widget(btn_box, Gtk.PackType.END)
        btn_box.show_all()

        self.pack_start(self.notebook, True, True, 0)

        # ── Settings tab ──
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_margin_start(20)
        inner.set_margin_end(20)
        inner.set_margin_top(16)
        inner.set_margin_bottom(20)

        def section(text):
            lbl = Gtk.Label(label=text)
            lbl.set_halign(Gtk.Align.START)
            lbl.get_style_context().add_class("section-header")
            inner.pack_start(lbl, False, False, 0)

        def field(label_text, widget, hint=None):
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            lbl = Gtk.Label(label=label_text)
            lbl.set_halign(Gtk.Align.START)
            lbl.get_style_context().add_class("form-label")
            header.pack_start(lbl, False, False, 0)
            if hint:
                h = Gtk.Label(label=hint)
                h.set_halign(Gtk.Align.START)
                h.get_style_context().add_class("form-hint")
                header.pack_start(h, False, False, 0)
            inner.pack_start(header, False, False, 0)
            inner.pack_start(widget, False, False, 0)
            spacer = Gtk.Box(); spacer.set_size_request(-1, 10)
            inner.pack_start(spacer, False, False, 0)

        # -- Basic info --
        section("BASIC INFO")

        name_desc_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        name_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        name_lbl = Gtk.Label(label="NAME")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.get_style_context().add_class("form-label")
        self.name_entry = Gtk.Entry()
        self.name_entry.get_style_context().add_class("form-entry")
        self.name_entry.set_placeholder_text("My Awesome Script")
        self.name_entry.set_hexpand(True)
        name_vbox.pack_start(name_lbl, False, False, 0)
        name_vbox.pack_start(self.name_entry, False, False, 0)

        desc_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        desc_lbl = Gtk.Label(label="DESCRIPTION (optional)")
        desc_lbl.set_halign(Gtk.Align.START)
        desc_lbl.get_style_context().add_class("form-label")
        self.desc_entry = Gtk.Entry()
        self.desc_entry.get_style_context().add_class("form-entry")
        self.desc_entry.set_placeholder_text("Brief description")
        self.desc_entry.set_hexpand(True)
        desc_vbox.pack_start(desc_lbl, False, False, 0)
        desc_vbox.pack_start(self.desc_entry, False, False, 0)

        name_desc_box.pack_start(name_vbox, True, True, 0)
        name_desc_box.pack_start(desc_vbox, True, True, 0)
        inner.pack_start(name_desc_box, False, False, 0)
        spacer = Gtk.Box(); spacer.set_size_request(-1, 10)
        inner.pack_start(spacer, False, False, 0)

        # -- Execution --
        section("EXECUTION")

        cmd_wd_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        cmd_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        cmd_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cmd_lbl = Gtk.Label(label="COMMAND")
        cmd_lbl.set_halign(Gtk.Align.START)
        cmd_lbl.get_style_context().add_class("form-label")
        cmd_hint = Gtk.Label(label="Shell command to execute")
        cmd_hint.set_halign(Gtk.Align.START)
        cmd_hint.get_style_context().add_class("form-hint")
        cmd_header.pack_start(cmd_lbl, False, False, 0)
        cmd_header.pack_start(cmd_hint, False, False, 0)
        self.cmd_entry = Gtk.Entry()
        self.cmd_entry.get_style_context().add_class("form-entry")
        self.cmd_entry.set_placeholder_text("./deploy.sh  or  npm run dev")
        self.cmd_entry.set_hexpand(True)
        self.cmd_entry.connect("changed", lambda e: self.run_btn.set_sensitive(bool(e.get_text().strip())))
        cmd_vbox.pack_start(cmd_header, False, False, 0)
        cmd_vbox.pack_start(self.cmd_entry, False, False, 0)

        wd_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wd_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        wd_lbl = Gtk.Label(label="WORKING DIRECTORY")
        wd_lbl.set_halign(Gtk.Align.START)
        wd_lbl.get_style_context().add_class("form-label")
        wd_hint = Gtk.Label(label="Directory where the command runs")
        wd_hint.set_halign(Gtk.Align.START)
        wd_hint.get_style_context().add_class("form-hint")
        wd_header.pack_start(wd_lbl, False, False, 0)
        wd_header.pack_start(wd_hint, False, False, 0)
        wd_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.wd_entry = Gtk.Entry()
        self.wd_entry.get_style_context().add_class("form-entry")
        self.wd_entry.set_hexpand(True)
        self.wd_entry.set_placeholder_text(str(Path.home()))
        wd_btn = Gtk.Button(label="Browse…")
        wd_btn.get_style_context().add_class("btn-secondary")
        wd_btn.connect("clicked", self._browse_dir)
        wd_inner.pack_start(self.wd_entry, True, True, 0)
        wd_inner.pack_start(wd_btn, False, False, 0)
        wd_vbox.pack_start(wd_header, False, False, 0)
        wd_vbox.pack_start(wd_inner, False, False, 0)

        cmd_wd_box.pack_start(cmd_vbox, True, True, 0)
        cmd_wd_box.pack_start(wd_vbox, True, True, 0)
        inner.pack_start(cmd_wd_box, False, False, 0)
        spacer = Gtk.Box(); spacer.set_size_request(-1, 10)
        inner.pack_start(spacer, False, False, 0)

        # -- Appearance --
        section("APPEARANCE")

        icon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.icon_entry = Gtk.Entry()
        self.icon_entry.get_style_context().add_class("form-entry")
        self.icon_entry.set_hexpand(True)
        self.icon_entry.set_placeholder_text("utilities-terminal  or  /path/to/icon.png")
        emoji_btn = Gtk.Button(label="Emoji")
        emoji_btn.get_style_context().add_class("btn-secondary")
        emoji_btn.connect("clicked", self._pick_emoji)
        icon_btn = Gtk.Button(label="Browse…")
        icon_btn.get_style_context().add_class("btn-secondary")
        icon_btn.connect("clicked", self._browse_icon)
        icon_box.pack_start(self.icon_entry, True, True, 0)
        icon_box.pack_start(emoji_btn, False, False, 0)
        icon_box.pack_start(icon_btn, False, False, 0)
        field("ICON", icon_box, hint="Named system icon, emoji, or path to .png/.svg file")

        # -- Options --
        section("OPTIONS")

        options_grid = Gtk.Grid()
        options_grid.set_column_spacing(20)
        options_grid.set_row_spacing(12)
        options_grid.set_column_homogeneous(True)

        def _option_cell(label_text, hint_text, switch):
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            lbl = Gtk.Label(label=label_text)
            lbl.set_halign(Gtk.Align.START)
            lbl.get_style_context().add_class("script-name")
            hint = Gtk.Label(label=hint_text)
            hint.set_halign(Gtk.Align.START)
            hint.get_style_context().add_class("script-cmd")
            vbox.pack_start(lbl, False, False, 0)
            vbox.pack_start(hint, False, False, 0)
            switch.set_valign(Gtk.Align.CENTER)
            box.pack_start(vbox, True, True, 0)
            box.pack_end(switch, False, False, 0)
            return box

        self.pin_switch = Gtk.Switch()
        self.enabled_switch = Gtk.Switch()
        self.confirm_switch = Gtk.Switch()
        self.silent_switch = Gtk.Switch()

        options_grid.attach(
            _option_cell("Pin as tray icon", "Dedicated icon in the panel", self.pin_switch),
            0, 0, 1, 1)
        options_grid.attach(
            _option_cell("Enabled", "Show in the tray menu", self.enabled_switch),
            1, 0, 1, 1)
        options_grid.attach(
            _option_cell("Confirm before running", "Dialog before execution", self.confirm_switch),
            0, 1, 1, 1)
        options_grid.attach(
            _option_cell("Silent mode", "Background, notify when done", self.silent_switch),
            1, 1, 1, 1)

        inner.pack_start(options_grid, False, False, 0)

        # -- Environment --
        spacer = Gtk.Box(); spacer.set_size_request(-1, 12)
        inner.pack_start(spacer, False, False, 0)
        section("ENVIRONMENT")

        env_port_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        env_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        env_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        env_lbl = Gtk.Label(label="ENV VARS")
        env_lbl.set_halign(Gtk.Align.START)
        env_lbl.get_style_context().add_class("form-label")
        env_hint = Gtk.Label(label="Space-separated KEY=VALUE pairs")
        env_hint.set_halign(Gtk.Align.START)
        env_hint.get_style_context().add_class("form-hint")
        env_header.pack_start(env_lbl, False, False, 0)
        env_header.pack_start(env_hint, False, False, 0)
        self.env_entry = Gtk.Entry()
        self.env_entry.get_style_context().add_class("form-entry")
        self.env_entry.set_hexpand(True)
        self.env_entry.set_placeholder_text("NODE_ENV=production API_KEY=abc123")
        env_vbox.pack_start(env_header, False, False, 0)
        env_vbox.pack_start(self.env_entry, False, False, 0)

        port_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        port_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        port_lbl = Gtk.Label(label="PORT")
        port_lbl.set_halign(Gtk.Align.START)
        port_lbl.get_style_context().add_class("form-label")
        port_hint = Gtk.Label(label="Auto-kill if busy")
        port_hint.set_halign(Gtk.Align.START)
        port_hint.get_style_context().add_class("form-hint")
        port_header.pack_start(port_lbl, False, False, 0)
        port_header.pack_start(port_hint, False, False, 0)
        self.port_entry = Gtk.Entry()
        self.port_entry.get_style_context().add_class("form-entry")
        self.port_entry.set_placeholder_text("3000")
        self.port_entry.set_width_chars(8)
        port_vbox.pack_start(port_header, False, False, 0)
        port_vbox.pack_start(self.port_entry, False, False, 0)

        env_port_box.pack_start(env_vbox, True, True, 0)
        env_port_box.pack_start(port_vbox, False, False, 0)
        inner.pack_start(env_port_box, False, False, 0)

        # -- Groups --
        spacer = Gtk.Box(); spacer.set_size_request(-1, 12)
        inner.pack_start(spacer, False, False, 0)
        section("GROUPS")

        self._groups_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.pack_start(self._groups_box, False, False, 0)


        scroll.add(inner)
        settings_box.pack_start(scroll, True, True, 0)

        # -- bottom action bar --
        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_bar.set_name("list-toolbar")
        action_bar.set_margin_start(0)
        action_bar.set_margin_end(0)
        action_bar.set_margin_top(0)
        action_bar.set_margin_bottom(0)

        self.del_btn = Gtk.Button(label="Delete")
        self.del_btn.get_style_context().add_class("btn-danger")
        self.del_btn.connect("clicked", lambda _: self._on_delete(self._script))

        self.dup_btn = Gtk.Button(label="Duplicate")
        self.dup_btn.get_style_context().add_class("btn-secondary")
        self.dup_btn.connect("clicked", lambda _: self._on_duplicate and self._on_duplicate(self._script))

        action_bar.pack_start(self.del_btn, False, False, 0)
        action_bar.pack_start(self.dup_btn, False, False, 0)

        settings_box.pack_start(action_bar, False, False, 0)
        self.notebook.append_page(settings_box, Gtk.Label(label="Settings"))

        # ── Logs tab ──
        self.logs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Logs toolbar
        logs_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        logs_toolbar.set_name("list-toolbar")
        logs_toolbar.set_margin_start(8)
        logs_toolbar.set_margin_end(8)
        logs_toolbar.set_margin_top(6)
        logs_toolbar.set_margin_bottom(4)

        self.log_refresh_btn = Gtk.Button(label="Refresh")
        self.log_refresh_btn.get_style_context().add_class("btn-secondary")

        self.log_clear_btn = Gtk.Button(label="Clear Logs")
        self.log_clear_btn.get_style_context().add_class("btn-danger")

        self.log_dismiss_btn = Gtk.Button(label="Dismiss Error")
        self.log_dismiss_btn.get_style_context().add_class("btn-secondary")

        self.log_path_lbl = Gtk.Label(label="")
        self.log_path_lbl.get_style_context().add_class("form-hint")
        self.log_path_lbl.set_hexpand(True)
        self.log_path_lbl.set_halign(Gtk.Align.START)
        self.log_path_lbl.set_ellipsize(Pango.EllipsizeMode.START)

        logs_toolbar.pack_start(self.log_refresh_btn, False, False, 0)
        logs_toolbar.pack_start(self.log_clear_btn, False, False, 0)
        logs_toolbar.pack_start(self.log_dismiss_btn, False, False, 0)
        logs_toolbar.pack_end(self.log_path_lbl, True, True, 0)
        self.logs_box.pack_start(logs_toolbar, False, False, 0)

        # Error banner
        self.log_error_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.log_error_bar.set_margin_start(8)
        self.log_error_bar.set_margin_end(8)
        self.log_error_bar.set_margin_bottom(4)
        error_icon = Gtk.Image.new_from_icon_name("dialog-error", Gtk.IconSize.MENU)
        self.log_error_lbl = Gtk.Label()
        self.log_error_lbl.set_halign(Gtk.Align.START)
        self.log_error_lbl.get_style_context().add_class("badge-error")
        self.log_error_bar.pack_start(error_icon, False, False, 0)
        self.log_error_bar.pack_start(self.log_error_lbl, False, False, 0)
        self.logs_box.pack_start(self.log_error_bar, False, False, 0)

        # Log text view
        self._user_scrolled_up = False
        self._log_scroll_internal = False  # guard for programmatic scrolls
        self._json_folds = {}
        self._fold_counter = 0
        self._fold_states = {}
        self._last_log_clean = ""
        self.log_scroll = Gtk.ScrolledWindow()
        self.log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.log_scroll.set_hexpand(True)
        self.log_scroll.set_vexpand(True)

        self.log_text_view = Gtk.TextView()
        self.log_text_view.set_editable(False)
        self.log_text_view.set_cursor_visible(False)
        self.log_text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_text_view.set_monospace(True)
        self.log_text_view.get_style_context().add_class("log-view")

        # JSON syntax highlight tags
        buf = self.log_text_view.get_buffer()
        buf.create_tag("json-key", foreground="#d4836a", weight=Pango.Weight.BOLD)
        buf.create_tag("json-string", foreground="#27ae60")
        buf.create_tag("json-number", foreground="#e67e22")
        buf.create_tag("json-bool", foreground="#8e44ad", weight=Pango.Weight.BOLD)
        buf.create_tag("json-null", foreground="#7f8c8d", style=Pango.Style.ITALIC)
        buf.create_tag("json-brace", foreground="#3498db")

        self.log_scroll.add(self.log_text_view)
        # Track scroll position via vadjustment (catches scrollbar drag, keyboard, mouse wheel)
        vadj = self.log_scroll.get_vadjustment()
        vadj.connect("value-changed", self._on_log_vadj_changed)
        self.log_text_view.add_events(Gdk.EventMask.POINTER_MOTION_MASK)
        self.log_text_view.connect("button-press-event", self._on_log_click)
        self.log_text_view.connect("motion-notify-event", self._on_log_motion)
        self.logs_box.pack_start(self.log_scroll, True, True, 0)

        self.notebook.append_page(self.logs_box, Gtk.Label(label="Logs"))

        # Log button connections
        self.log_refresh_btn.connect("clicked", lambda _: self._reload_log())
        self.log_clear_btn.connect("clicked", self._clear_log)
        self.log_dismiss_btn.connect("clicked", self._dismiss_log_error)

        # Auto-save on any field change
        self.name_entry.connect("changed", lambda _: self._auto_save())
        self.desc_entry.connect("changed", lambda _: self._auto_save())
        self.cmd_entry.connect("changed", lambda _: self._auto_save())
        self.wd_entry.connect("changed", lambda _: self._auto_save())
        self.icon_entry.connect("changed", lambda _: self._auto_save())
        self.env_entry.connect("changed", lambda _: self._auto_save())
        self.port_entry.connect("changed", lambda _: self._auto_save())
        for sw in (self.pin_switch, self.enabled_switch,
                   self.confirm_switch, self.silent_switch):
            sw.connect("notify::active", lambda *_: self._auto_save())

    def load_script(self, script: dict):
        self._loading = True
        self._script = script
        self.name_entry.set_text(script.get("name", ""))
        self.desc_entry.set_text(script.get("description", ""))
        self.cmd_entry.set_text(script.get("command", ""))
        self.wd_entry.set_text(script.get("working_dir", str(Path.home())))
        self.icon_entry.set_text(script.get("icon", ""))
        self.pin_switch.set_active(script.get("pinned_icon", False))
        self.enabled_switch.set_active(script.get("enabled", True))
        self.env_entry.set_text(script.get("env_vars", ""))
        self.port_entry.set_text(script.get("port", ""))
        self.confirm_switch.set_active(script.get("confirm", False))
        self.silent_switch.set_active(script.get("silent", False))
        self.run_btn.set_sensitive(bool(script.get("command", "").strip()))
        self.set_sensitive(True)
        self._loading = False
        self._user_scrolled_up = False
        self._last_log_clean = ""
        self._fold_states = {}
        self._reload_log()
        self._update_error_banner()
        self._rebuild_group_checkboxes()

    def clear(self):
        self._script = None
        self.set_sensitive(False)

    def _rebuild_group_checkboxes(self):
        for child in self._groups_box.get_children():
            self._groups_box.remove(child)
        self._group_checkboxes.clear()
        cfg = load_config()
        for g in cfg.get("groups", []):
            cb = Gtk.CheckButton(label=g["name"])
            cb.get_style_context().add_class("group-check")
            cb.connect("toggled", lambda *_: self._auto_save())
            self._group_checkboxes[g["id"]] = cb
            self._groups_box.pack_start(cb, False, False, 0)
        # Restore selection from current script
        if self._script:
            script_groups = self._script.get("groups", [])
            self._loading = True
            for gid, cb in self._group_checkboxes.items():
                cb.set_active(gid in script_groups)
            self._loading = False
        self._groups_box.show_all()

    def _auto_save(self):
        if self._loading or not self._script:
            return
        self._save()

    # ── log tab helpers ──

    def _get_log_path(self):
        if not self._script:
            return None
        sid = self._script.get("id", "")
        if not sid:
            return None
        return LOG_DIR / f"{sid}.log"

    def _on_log_vadj_changed(self, adj):
        """Track scroll position — works for mouse wheel, scrollbar drag, keyboard, etc."""
        if self._log_scroll_internal:
            return
        at_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 20
        self._user_scrolled_up = not at_bottom

    def _read_log_raw(self, lp):
        """Read raw log text from file path."""
        self.log_path_lbl.set_text(str(lp))
        try:
            if lp.exists():
                return lp.read_text(errors="replace")
            return "(no logs yet)"
        except Exception as e:
            return f"Error reading log: {e}"

    def _append_tail_content(self, buf, raw, old_clean, clean):
        """Append only the new portion of log content (no flicker)."""
        pos_map = self._build_clean_to_raw_map(raw)
        raw_offset = pos_map[len(old_clean)] if len(old_clean) < len(pos_map) else len(raw)
        tail_raw = raw[raw_offset:]
        tail_clean = clean[len(old_clean):]

        self._log_scroll_internal = True
        cursor = buf.create_mark("log-cursor", buf.get_end_iter(), False)
        tail_blocks = self._find_json_blocks(tail_clean)
        if not tail_blocks:
            self._insert_ansi_text(buf, cursor, tail_raw)
        else:
            self._insert_buffered_blocks(buf, cursor, tail_raw, tail_clean, tail_blocks)
        buf.delete_mark(cursor)

        if not self._user_scrolled_up:
            GLib.idle_add(self._scroll_to_end)
        else:
            self._log_scroll_internal = False

    def _insert_buffered_blocks(self, buf, cursor, raw, clean, blocks):
        """Insert a mix of ANSI text and collapsible JSON blocks."""
        pos_map = self._build_clean_to_raw_map(raw)
        last_raw_end = 0
        last_clean_end = 0
        for cs, ce, obj in blocks:
            if cs > last_clean_end:
                re_ = pos_map[cs]
                self._insert_ansi_text(buf, cursor, raw[last_raw_end:re_])
            self._insert_collapsible_json(buf, cursor, obj, 0)
            last_clean_end = ce
            last_raw_end = pos_map[min(ce, len(pos_map) - 1)]
        if last_raw_end < len(raw):
            self._insert_ansi_text(buf, cursor, raw[last_raw_end:])

    def _scroll_to_end(self):
        buf = self.log_text_view.get_buffer()
        self.log_text_view.scroll_to_iter(buf.get_end_iter(), 0.0, True, 0.0, 1.0)
        self._log_scroll_internal = False
        return False

    def _scroll_to_line(self, line):
        buf = self.log_text_view.get_buffer()
        it = buf.get_iter_at_line(min(line, buf.get_line_count() - 1))
        self.log_text_view.scroll_to_iter(it, 0.0, True, 0.0, 0.0)
        self._log_scroll_internal = False
        return False

    def _reload_log(self):
        lp = self._get_log_path()
        if not lp:
            self.log_text_view.get_buffer().set_text("(no script selected)")
            self.log_path_lbl.set_text("")
            return
        raw = self._read_log_raw(lp)
        buf = self.log_text_view.get_buffer()
        clean = _ANSI_ALL_RE.sub('', _ANSI_OSC_RE.sub('', raw))
        if clean == self._last_log_clean:
            return
        old_clean = self._last_log_clean
        self._last_log_clean = clean

        is_append = old_clean and clean.startswith(old_clean)

        if is_append:
            self._append_tail_content(buf, raw, old_clean, clean)
            return

        # Full rebuild needed
        saved_line = -1
        if self._user_scrolled_up:
            visible_rect = self.log_text_view.get_visible_rect()
            top_iter = self.log_text_view.get_iter_at_location(visible_rect.x, visible_rect.y)
            saved_line = top_iter[1].get_line() if isinstance(top_iter, tuple) else top_iter.get_line()
        self._log_scroll_internal = True
        self._build_collapsible_buffer(buf, raw)
        if saved_line == -1:
            GLib.idle_add(self._scroll_to_end)
        else:
            GLib.idle_add(self._scroll_to_line, saved_line)

    # ── collapsible JSON buffer builder ──

    @staticmethod
    def _is_complex_json(obj):
        """Check if a JSON object is complex enough to warrant collapsible rendering."""
        if not isinstance(obj, (dict, list)):
            return False
        if isinstance(obj, dict):
            return len(obj) >= 2 or any(isinstance(v, (dict, list)) for v in obj.values())
        return len(obj) >= 2 or any(isinstance(v, (dict, list)) for v in obj)

    @staticmethod
    def _find_json_blocks(text):
        """Find JSON blocks in text — only at line boundaries with sufficient complexity."""
        decoder = json.JSONDecoder()
        blocks = []
        for m in re.finditer(r'^[ \t]*([{\[])', text, re.MULTILINE):
            i = m.start(1)
            try:
                obj, end = decoder.raw_decode(text, i)
            except ValueError:
                continue
            if ScriptForm._is_complex_json(obj):
                blocks.append((i, end, obj))
        return blocks

    @staticmethod
    def _build_clean_to_raw_map(raw):
        """Map character positions in ANSI-stripped text back to raw text positions."""
        pos_map = []
        raw_i = 0
        raw_n = len(raw)
        while raw_i < raw_n:
            m = _ANSI_OSC_RE.match(raw, raw_i)
            if m:
                raw_i = m.end()
                continue
            m = _ANSI_ALL_RE.match(raw, raw_i)
            if m:
                raw_i = m.end()
                continue
            pos_map.append(raw_i)
            raw_i += 1
        pos_map.append(raw_i)
        return pos_map

    def _clear_fold_tags(self, buf):
        """Remove fold-specific tags from previous buffer build."""
        table = buf.get_tag_table()
        for fold_id in self._json_folds:
            for prefix in ("jte-", "jtc-", "js-", "jc-"):
                tag = table.lookup(f"{prefix}{fold_id}")
                if tag:
                    table.remove(tag)
        self._json_folds = {}
        self._fold_counter = 0

    def _build_collapsible_buffer(self, buf, raw):
        """Build log buffer with ANSI colors and collapsible JSON blocks."""
        self._clear_fold_tags(buf)
        buf.set_text("")
        clean = _ANSI_ALL_RE.sub('', _ANSI_OSC_RE.sub('', raw))
        blocks = self._find_json_blocks(clean)
        cursor = buf.create_mark("log-cursor", buf.get_end_iter(), False)
        if not blocks:
            self._insert_ansi_text(buf, cursor, raw)
            buf.delete_mark(cursor)
            return
        pos_map = self._build_clean_to_raw_map(raw)
        last_raw_end = 0
        last_clean_end = 0
        for clean_start, clean_end, obj in blocks:
            if clean_start > last_clean_end:
                raw_end = pos_map[clean_start]
                self._insert_ansi_text(buf, cursor, raw[last_raw_end:raw_end])
            self._insert_collapsible_json(buf, cursor, obj, 0)
            last_clean_end = clean_end
            last_raw_end = pos_map[min(clean_end, len(pos_map) - 1)]
        if last_raw_end < len(raw):
            self._insert_ansi_text(buf, cursor, raw[last_raw_end:])
        buf.delete_mark(cursor)

    @staticmethod
    def _parse_ansi_sgr(codes_str):
        """Parse an ANSI SGR code string into (fg, bold) state changes."""
        fg, bold = None, False
        codes = [int(c) for c in codes_str.split(';') if c.isdigit()] if codes_str else [0]
        for code in codes:
            if code == 0:
                fg, bold = None, False
            elif code == 1:
                bold = True
            elif code == 22:
                bold = False
            elif code in ANSI_COLORS:
                fg = code
            elif code == 39:
                fg = None
        return fg, bold

    def _insert_ansi_text(self, buf, cursor, text):
        """Insert text with ANSI color processing at cursor mark position."""
        text = _ANSI_OSC_RE.sub('', text)
        fg = None
        bold = False
        last = 0
        for m in _ANSI_SGR_RE.finditer(text):
            before = text[last:m.start()]
            if before:
                before = _ANSI_ALL_RE.sub('', before)
                if before:
                    self._insert_ansi_chunk(buf, cursor, before, fg, bold)
            fg, bold = self._parse_ansi_sgr(m.group(1))
            last = m.end()
        remaining = _ANSI_ALL_RE.sub('', text[last:])
        if remaining:
            self._insert_ansi_chunk(buf, cursor, remaining, fg, bold)

    def _insert_ansi_chunk(self, buf, cursor, text, fg_code, bold):
        """Insert a text chunk with ANSI-derived color at cursor mark."""
        it = buf.get_iter_at_mark(cursor)
        if fg_code and fg_code in ANSI_COLORS:
            tag_name = f"ansi-{fg_code}{'-b' if bold else ''}"
            if not buf.get_tag_table().lookup(tag_name):
                kw = {"foreground": ANSI_COLORS[fg_code]}
                if bold:
                    kw["weight"] = Pango.Weight.BOLD
                buf.create_tag(tag_name, **kw)
            buf.insert_with_tags_by_name(it, text, tag_name)
        elif bold:
            if not buf.get_tag_table().lookup("ansi-bold"):
                buf.create_tag("ansi-bold", weight=Pango.Weight.BOLD)
            buf.insert_with_tags_by_name(it, text, "ansi-bold")
        else:
            buf.insert(it, text)

    @staticmethod
    def _hash_json(obj):
        try:
            return hash(json.dumps(obj, sort_keys=True, default=str))
        except (TypeError, ValueError):
            return id(obj)

    def _insert_at_cursor(self, buf, cursor, text, tag=None):
        """Insert text at cursor mark, optionally with a tag."""
        it = buf.get_iter_at_mark(cursor)
        if tag:
            buf.insert_with_tags_by_name(it, text, tag)
        else:
            buf.insert(it, text)

    def _create_fold_tags(self, buf, fold_id, collapsed):
        """Create the four visibility tags for a JSON fold."""
        buf.create_tag(f"jte-{fold_id}", foreground="#d4836a", weight=Pango.Weight.BOLD,
                       invisible=collapsed)
        buf.create_tag(f"jtc-{fold_id}", foreground="#d4836a", weight=Pango.Weight.BOLD,
                       invisible=not collapsed)
        buf.create_tag(f"js-{fold_id}", foreground="#7a746c", style=Pango.Style.ITALIC,
                       invisible=not collapsed)
        buf.create_tag(f"jc-{fold_id}", invisible=collapsed)

    def _insert_fold_toggles(self, buf, cursor, fold_id):
        """Insert expand/collapse toggle markers."""
        self._insert_at_cursor(buf, cursor, "\u25BC ", f"jte-{fold_id}")
        self._insert_at_cursor(buf, cursor, "\u25B6 ", f"jtc-{fold_id}")

    @staticmethod
    def _make_json_summary(obj, close_br):
        """Build the one-line summary shown when a JSON block is collapsed."""
        if isinstance(obj, dict):
            preview = ", ".join(list(obj.keys())[:3])
            if len(obj) > 3:
                preview += ", \u2026"
            return f" {preview} {close_br}"
        return f" {len(obj)} items {close_br}"

    def _insert_json_content(self, buf, cursor, obj, indent, is_dict):
        """Insert the expanded content of a JSON object or array."""
        pad = "  " * (indent + 1)
        if is_dict:
            items = list(obj.items())
            for i, (key, value) in enumerate(items):
                self._insert_at_cursor(buf, cursor, f"\n{pad}")
                self._insert_at_cursor(buf, cursor, json.dumps(key, ensure_ascii=False), "json-key")
                self._insert_at_cursor(buf, cursor, ": ")
                self._insert_json_value(buf, cursor, value, indent + 1)
                if i < len(items) - 1:
                    self._insert_at_cursor(buf, cursor, ",")
        else:
            for i, value in enumerate(obj):
                self._insert_at_cursor(buf, cursor, f"\n{pad}")
                self._insert_json_value(buf, cursor, value, indent + 1)
                if i < len(obj) - 1:
                    self._insert_at_cursor(buf, cursor, ",")

    def _get_fold_state(self, obj):
        """Get the initial collapsed state for a JSON object."""
        content_hash = self._hash_json(obj)
        return self._fold_states.get(content_hash, False), content_hash

    def _insert_collapsible_json(self, buf, cursor, obj, indent):
        """Insert a JSON object or array with fold toggle controls."""
        is_dict = isinstance(obj, dict)
        if not (is_dict or isinstance(obj, list)) or not obj:
            self._insert_json_primitive(buf, cursor, obj)
            return

        fold_id = self._fold_counter
        self._fold_counter += 1
        initially_collapsed, content_hash = self._get_fold_state(obj)

        self._create_fold_tags(buf, fold_id, initially_collapsed)
        self._insert_fold_toggles(buf, cursor, fold_id)

        open_br, close_br = ("{", "}") if is_dict else ("[", "]")
        self._insert_at_cursor(buf, cursor, open_br, "json-brace")

        summary = self._make_json_summary(obj, close_br)
        self._insert_at_cursor(buf, cursor, summary, f"js-{fold_id}")

        content_start = buf.create_mark(None, buf.get_iter_at_mark(cursor), True)
        self._insert_json_content(buf, cursor, obj, indent, is_dict)

        self._insert_at_cursor(buf, cursor, f"\n{'  ' * indent}")
        self._insert_at_cursor(buf, cursor, close_br, "json-brace")

        cs = buf.get_iter_at_mark(content_start)
        ce = buf.get_iter_at_mark(cursor)
        buf.apply_tag_by_name(f"jc-{fold_id}", cs, ce)
        buf.delete_mark(content_start)

        self._json_folds[fold_id] = {
            "expanded": not initially_collapsed,
            "content_hash": content_hash,
        }

    def _insert_json_value(self, buf, cursor, value, indent):
        """Insert a JSON value — collapsible if it's a non-empty object/array."""
        if isinstance(value, (dict, list)) and len(value) > 0:
            self._insert_collapsible_json(buf, cursor, value, indent)
        else:
            self._insert_json_primitive(buf, cursor, value)

    def _insert_json_primitive(self, buf, cursor, value):
        """Insert a JSON primitive with syntax highlighting."""
        it = buf.get_iter_at_mark(cursor)
        if value is None:
            buf.insert_with_tags_by_name(it, "null", "json-null")
        elif isinstance(value, bool):
            buf.insert_with_tags_by_name(it, str(value).lower(), "json-bool")
        elif isinstance(value, (int, float)):
            buf.insert_with_tags_by_name(it, json.dumps(value), "json-number")
        elif isinstance(value, str):
            buf.insert_with_tags_by_name(it, json.dumps(value, ensure_ascii=False), "json-string")
        elif isinstance(value, dict):
            buf.insert_with_tags_by_name(it, "{}", "json-brace")
        elif isinstance(value, list):
            buf.insert_with_tags_by_name(it, "[]", "json-brace")
        else:
            buf.insert(it, json.dumps(value, ensure_ascii=False, default=str))

    def _on_log_click(self, widget, event):
        """Handle clicks on JSON fold toggles."""
        if event.button != 1:
            return False
        bx, by = widget.window_to_buffer_coords(
            Gtk.TextWindowType.WIDGET, int(event.x), int(event.y))
        result = widget.get_iter_at_location(bx, by)
        it = result[1] if isinstance(result, tuple) else result
        for tag in it.get_tags():
            name = tag.get_property("name") or ""
            if name.startswith(("jte-", "jtc-")):
                fold_id = int(name[4:])
                self._toggle_json_fold(fold_id)
                return True
        return False

    def _on_log_motion(self, widget, event):
        """Show pointer cursor over fold toggles."""
        bx, by = widget.window_to_buffer_coords(
            Gtk.TextWindowType.WIDGET, int(event.x), int(event.y))
        result = widget.get_iter_at_location(bx, by)
        it = result[1] if isinstance(result, tuple) else result
        on_toggle = any(
            (tag.get_property("name") or "").startswith(("jte-", "jtc-"))
            for tag in it.get_tags()
        )
        win = widget.get_window(Gtk.TextWindowType.TEXT)
        if on_toggle:
            win.set_cursor(Gdk.Cursor.new_from_name(widget.get_display(), "pointer"))
        else:
            win.set_cursor(None)
        return False

    def _toggle_json_fold(self, fold_id):
        """Toggle a JSON fold between expanded and collapsed."""
        if fold_id not in self._json_folds:
            return
        fold = self._json_folds[fold_id]
        new_expanded = not fold["expanded"]
        fold["expanded"] = new_expanded
        buf = self.log_text_view.get_buffer()
        table = buf.get_tag_table()
        table.lookup(f"jte-{fold_id}").set_property("invisible", not new_expanded)
        table.lookup(f"jtc-{fold_id}").set_property("invisible", new_expanded)
        table.lookup(f"js-{fold_id}").set_property("invisible", new_expanded)
        table.lookup(f"jc-{fold_id}").set_property("invisible", not new_expanded)
        self._fold_states[fold["content_hash"]] = not new_expanded

    def _update_error_banner(self, errors=None):
        if not self._script:
            self.log_error_bar.hide()
            self.log_dismiss_btn.set_sensitive(False)
            return
        sid = self._script.get("id", "")
        if errors is None:
            errors = get_error_states()
        if sid and sid in errors:
            err = errors[sid]
            self.log_error_lbl.set_text(
                f"Last run failed with exit code {err.get('exit_code', '?')}  ({err.get('timestamp', '')[:19]})")
            self.log_error_bar.show_all()
            self.log_dismiss_btn.set_sensitive(True)
        else:
            self.log_error_bar.hide()
            self.log_dismiss_btn.set_sensitive(False)

    def _clear_log(self, _widget):
        lp = self._get_log_path()
        if lp and lp.exists():
            lp.write_text("")
        self._reload_log()

    def _dismiss_log_error(self, _widget):
        if self._script:
            clear_error_state(self._script.get("id", ""))
            self._update_error_banner()

    def _save(self, _widget=None):
        if not self._script:
            return
        self._script["name"]        = self.name_entry.get_text().strip() or self._script.get("name", "New Script")
        self._script["description"] = self.desc_entry.get_text().strip()
        self._script["command"]     = self.cmd_entry.get_text().strip()
        self._script["working_dir"] = self.wd_entry.get_text().strip() or str(Path.home())
        self._script["icon"]        = self.icon_entry.get_text().strip()
        self._script["pinned_icon"] = self.pin_switch.get_active()
        self._script["enabled"]     = self.enabled_switch.get_active()
        self._script["env_vars"]    = self.env_entry.get_text().strip()
        self._script["port"]        = self.port_entry.get_text().strip()
        self._script["confirm"]     = self.confirm_switch.get_active()
        self._script["silent"]      = self.silent_switch.get_active()
        self._script["groups"]      = [gid for gid, cb in self._group_checkboxes.items() if cb.get_active()]
        self._on_save(self._script)

    def _run_current(self, _widget=None):
        """Run using current form values, without saving first."""
        cmd = self.cmd_entry.get_text().strip()
        if not cmd:
            return
        cwd = self.wd_entry.get_text().strip() or str(Path.home())
        name = self.name_entry.get_text().strip() or "Script"
        temp_script = {
            "id":          self._script.get("id", "") if self._script else "",
            "name":        name,
            "command":     cmd,
            "working_dir": cwd,
            "env_vars":    self.env_entry.get_text().strip(),
            "confirm":     self.confirm_switch.get_active(),
            "silent":      self.silent_switch.get_active(),
        }
        self._on_run(temp_script)

    def _browse_dir(self, _widget):
        dialog = Gtk.FileChooserDialog(
            title="Select Working Directory",
            parent=self.get_toplevel(),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OPEN,   Gtk.ResponseType.OK)
        # Open at current value or home
        current = self.wd_entry.get_text().strip()
        start = current if current and Path(current).expanduser().exists() else str(Path.home())
        dialog.set_current_folder(str(Path(start).expanduser()))
        if dialog.run() == Gtk.ResponseType.OK:
            chosen = dialog.get_filename()
            current_name = self.name_entry.get_text().strip()
            if not current_name or current_name == "New Script":
                self.name_entry.set_text(Path(chosen).name)
            self.wd_entry.set_text(chosen)
        dialog.destroy()

    def _browse_icon(self, _widget):
        dialog = Gtk.FileChooserDialog(
            title="Select Icon File",
            parent=self.get_toplevel(),
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OPEN,   Gtk.ResponseType.OK)
        # Open at home directory
        dialog.set_current_folder(str(Path.home()))
        f = Gtk.FileFilter()
        f.set_name("Images (PNG, SVG, ICO)")
        f.add_pattern("*.png"); f.add_pattern("*.svg"); f.add_pattern("*.ico")
        dialog.add_filter(f)
        if dialog.run() == Gtk.ResponseType.OK:
            self.icon_entry.set_text(dialog.get_filename())
        dialog.destroy()

    def _pick_emoji(self, _widget):
        win = self.get_toplevel()
        parent = win if isinstance(win, Gtk.Window) else None
        dialog = Gtk.Dialog(
            title="Pick an Emoji",
            transient_for=parent,
            modal=True,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.set_default_size(420, 380)
        dialog.set_keep_above(True)
        dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)

        content = dialog.get_content_area()
        content.set_spacing(0)

        flow = Gtk.FlowBox()
        flow.set_max_children_per_line(8)
        flow.set_min_children_per_line(8)
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_row_spacing(2)
        flow.set_column_spacing(2)
        flow.set_margin_start(8)
        flow.set_margin_end(8)
        flow.set_margin_top(8)
        flow.set_margin_bottom(8)

        for emoji in EMOJIS:
            png_path = _render_emoji_to_png(emoji)
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(png_path, 32, 32)
            img = Gtk.Image.new_from_pixbuf(pixbuf)
            evbox = Gtk.EventBox()
            evbox.add(img)
            evbox.set_tooltip_text(emoji)
            evbox.connect("button-press-event", lambda _, _ev, e=emoji: self._select_emoji(e, dialog))
            flow.add(evbox)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.add(flow)

        content.pack_start(scroll, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def _select_emoji(self, emoji, dialog):
        path = _render_emoji_to_png(emoji)
        self.icon_entry.set_text(path)
        dialog.response(Gtk.ResponseType.OK)


# -- group form -----------------------------------------------------------------

class GroupForm(Gtk.Box):
    """Right-hand panel — shows when a group is selected."""

    def __init__(self, on_save, on_delete, on_run_all, on_stop_all, on_scripts_changed=None, on_duplicate=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_name("form-panel")
        self._on_save = on_save
        self._on_delete = on_delete
        self._on_run_all = on_run_all
        self._on_stop_all = on_stop_all
        self._on_scripts_changed = on_scripts_changed
        self._on_duplicate = on_duplicate
        self._group = None
        self._loading = False
        self._script_checkboxes = {}
        self._build()

    def _build(self):
        # Notebook with Settings tab
        self.notebook = Gtk.Notebook()
        self.notebook.set_tab_pos(Gtk.PositionType.TOP)

        # Run / Stop buttons at the right of the tab bar
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.set_margin_end(6)
        btn_box.set_margin_top(4)
        btn_box.set_margin_bottom(4)

        self.run_btn = Gtk.Button(label="▶  Run All")
        self.run_btn.get_style_context().add_class("btn-success")
        self.run_btn.connect("clicked", lambda _: self._on_run_all and self._on_run_all(self._group))
        btn_box.pack_start(self.run_btn, False, False, 0)

        self.stop_btn = Gtk.Button(label="■  Stop All")
        self.stop_btn.get_style_context().add_class("btn-danger")
        self.stop_btn.connect("clicked", lambda _: self._on_stop_all and self._on_stop_all(self._group))
        self.stop_btn.set_sensitive(False)
        btn_box.pack_start(self.stop_btn, False, False, 0)

        self.notebook.set_action_widget(btn_box, Gtk.PackType.END)
        btn_box.show_all()

        self.pack_start(self.notebook, True, True, 0)

        # ── Settings tab ──
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_margin_start(20)
        inner.set_margin_end(20)
        inner.set_margin_top(16)
        inner.set_margin_bottom(20)

        def section(text):
            lbl = Gtk.Label(label=text)
            lbl.set_halign(Gtk.Align.START)
            lbl.get_style_context().add_class("section-header")
            inner.pack_start(lbl, False, False, 0)

        def spacer(h=10):
            s = Gtk.Box()
            s.set_size_request(-1, h)
            inner.pack_start(s, False, False, 0)

        # -- Basic info --
        section("BASIC INFO")

        name_desc_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        name_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        name_lbl = Gtk.Label(label="NAME")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.get_style_context().add_class("form-label")
        self.name_entry = Gtk.Entry()
        self.name_entry.get_style_context().add_class("form-entry")
        self.name_entry.set_placeholder_text("Group name")
        self.name_entry.set_hexpand(True)
        name_vbox.pack_start(name_lbl, False, False, 0)
        name_vbox.pack_start(self.name_entry, False, False, 0)

        desc_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        desc_lbl = Gtk.Label(label="DESCRIPTION (optional)")
        desc_lbl.set_halign(Gtk.Align.START)
        desc_lbl.get_style_context().add_class("form-label")
        self.desc_entry = Gtk.Entry()
        self.desc_entry.get_style_context().add_class("form-entry")
        self.desc_entry.set_placeholder_text("Brief description of this group")
        self.desc_entry.set_hexpand(True)
        desc_vbox.pack_start(desc_lbl, False, False, 0)
        desc_vbox.pack_start(self.desc_entry, False, False, 0)

        name_desc_box.pack_start(name_vbox, True, True, 0)
        name_desc_box.pack_start(desc_vbox, True, True, 0)
        inner.pack_start(name_desc_box, False, False, 0)
        spacer()

        # -- Scripts --
        spacer(2)
        section("SCRIPTS")

        hint = Gtk.Label(label="Select which scripts belong to this group")
        hint.set_halign(Gtk.Align.START)
        hint.get_style_context().add_class("form-hint")
        inner.pack_start(hint, False, False, 0)
        spacer(6)

        self._scripts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.pack_start(self._scripts_box, False, False, 0)

        scroll.add(inner)
        settings_box.pack_start(scroll, True, True, 0)

        # -- bottom action bar --
        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_bar.set_name("list-toolbar")
        self.del_btn = Gtk.Button(label="Delete")
        self.del_btn.get_style_context().add_class("btn-danger")
        self.del_btn.connect("clicked", lambda _: self._on_delete(self._group))

        self.dup_btn = Gtk.Button(label="Duplicate")
        self.dup_btn.get_style_context().add_class("btn-secondary")
        self.dup_btn.connect("clicked", lambda _: self._on_duplicate and self._on_duplicate(self._group))

        action_bar.pack_start(self.del_btn, False, False, 0)
        action_bar.pack_start(self.dup_btn, False, False, 0)
        settings_box.pack_start(action_bar, False, False, 0)

        self.notebook.append_page(settings_box, Gtk.Label(label="Settings"))

        # Auto-save
        self.name_entry.connect("changed", lambda _: self._auto_save())
        self.desc_entry.connect("changed", lambda _: self._auto_save())

    def load_group(self, group):
        self._loading = True
        self._group = group
        self.name_entry.set_text(group.get("name", ""))
        self.desc_entry.set_text(group.get("description", ""))
        self.set_sensitive(True)
        self._loading = False
        self._rebuild_script_checkboxes()
        self.update_running_state()

    def clear(self):
        self._group = None
        self.set_sensitive(False)

    def _rebuild_script_checkboxes(self):
        for child in self._scripts_box.get_children():
            self._scripts_box.remove(child)
        self._script_checkboxes.clear()
        cfg = load_config()
        gid = self._group["id"] if self._group else ""
        self._loading = True
        for s in cfg.get("scripts", []):
            cb = Gtk.CheckButton(label=s.get("name", "Unnamed"))
            cb.get_style_context().add_class("group-check")
            cb.set_active(gid in s.get("groups", []))
            cb.connect("toggled", lambda _cb, sid=s["id"]: self._toggle_script(sid, _cb.get_active()))
            self._script_checkboxes[s["id"]] = cb
            self._scripts_box.pack_start(cb, False, False, 0)
        self._loading = False
        self._scripts_box.show_all()

    def _toggle_script(self, script_id, active):
        if self._loading or not self._group:
            return
        cfg = load_config()
        gid = self._group["id"]
        for s in cfg.get("scripts", []):
            if s["id"] == script_id:
                groups = s.get("groups", [])
                if active and gid not in groups:
                    groups.append(gid)
                elif not active and gid in groups:
                    groups.remove(gid)
                s["groups"] = groups
                break
        save_config(cfg)
        if self._on_scripts_changed:
            self._on_scripts_changed(self._group)

    def _auto_save(self):
        if self._loading or not self._group:
            return
        self._group["name"] = self.name_entry.get_text().strip() or self._group.get("name", "New Group")
        self._group["description"] = self.desc_entry.get_text().strip()
        self._on_save(self._group)

    def update_running_state(self, running=None):
        if not self._group:
            return
        if running is None:
            running = get_running_ids()
        cfg = load_config()
        gid = self._group["id"]
        group_scripts = [s for s in cfg.get("scripts", []) if gid in s.get("groups", []) and s.get("enabled", True)]
        any_running = any(s.get("id", "") in running for s in group_scripts)
        self.stop_btn.set_sensitive(any_running)
        self.run_btn.set_sensitive(len(group_scripts) > 0)


# -- main window ----------------------------------------------------------------

class ManagerWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="LazyLauncher")
        self.set_wmclass("lazylauncher", "lazylauncher")
        display = Gdk.Display.get_default()
        if display:
            mon = display.get_primary_monitor()
            if mon:
                geo = mon.get_geometry()
                self.move(geo.x, geo.y)
                self.set_default_size(geo.width, geo.height)
        else:
            self.set_default_size(960, 680)
        self.set_resizable(True)

        # Window icon — prefer themed icon for crisp rendering
        _hicolor = Path.home() / ".local/share/icons/hicolor/scalable/apps/lazylauncher.svg"
        if _hicolor.exists():
            self.set_icon_name("lazylauncher")
        else:
            logo_path = Path(__file__).parent / "icons" / "logo.svg"
            if logo_path.exists():
                self.set_icon_from_file(str(logo_path))

        # CSS
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Header bar
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title("LazyLauncher")
        hb.set_subtitle("")
        self.set_titlebar(hb)

        # Hamburger menu
        menu_btn = Gtk.MenuButton()
        menu_btn.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.MENU))
        menu_btn.get_style_context().add_class("btn-icon")

        menu = Gtk.Menu()
        import_item = Gtk.MenuItem(label="Import Scripts…")
        import_item.connect("activate", self._import_config)
        menu.append(import_item)
        export_item = Gtk.MenuItem(label="Export Scripts…")
        export_item.connect("activate", self._export_config)
        menu.append(export_item)
        menu.append(Gtk.SeparatorMenuItem())
        reload_item = Gtk.MenuItem(label="Reload Tray")
        reload_item.connect("activate", self._reload_tray)
        menu.append(reload_item)
        menu.show_all()
        menu_btn.set_popup(menu)
        hb.pack_start(menu_btn)

        # Main layout
        hpaned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        hpaned.set_position(400)
        self.add(hpaned)

        # -- Left: list --
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left_box.set_name("sidebar")

        # Main tabs: Scripts | Groups
        self._sidebar_mode = "all"
        self._selected_group_id = None
        self._switching_tab = False
        tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tab_bar.set_name("group-tabs")
        tab_bar.set_margin_start(6)
        tab_bar.set_margin_end(6)
        tab_bar.set_margin_top(6)

        self.tab_all_btn = Gtk.ToggleButton(label="Scripts")
        self.tab_all_btn.set_mode(False)
        self.tab_all_btn.get_style_context().add_class("group-tab")
        self.tab_all_btn.set_active(True)
        self.tab_all_btn.connect("toggled", lambda b: self._on_tab_toggled(b, "all"))

        self.tab_groups_btn = Gtk.ToggleButton(label="Groups")
        self.tab_groups_btn.set_mode(False)
        self.tab_groups_btn.get_style_context().add_class("group-tab")
        self.tab_groups_btn.connect("toggled", lambda b: self._on_tab_toggled(b, "groups"))

        tab_bar.pack_start(self.tab_all_btn, True, True, 0)
        tab_bar.pack_start(self.tab_groups_btn, True, True, 0)
        left_box.pack_start(tab_bar, False, False, 0)

        # Sidebar stack
        self.sidebar_stack = Gtk.Stack()
        self.sidebar_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.sidebar_stack.set_transition_duration(150)

        # -- All page --
        all_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        new_btn = Gtk.Button(label="+ New Script")
        new_btn.get_style_context().add_class("btn-primary")
        new_btn.set_margin_start(6)
        new_btn.set_margin_end(6)
        new_btn.set_margin_top(6)
        new_btn.connect("clicked", self._new_script)
        all_page.pack_start(new_btn, False, False, 0)

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_box.set_name("list-toolbar")
        search_box.set_margin_start(6)
        search_box.set_margin_end(6)
        search_box.set_margin_top(6)
        search_box.set_margin_bottom(6)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.get_style_context().add_class("form-entry")
        self.search_entry.set_placeholder_text("Filter scripts…")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._filter_changed)
        search_box.pack_start(self.search_entry, True, True, 0)
        all_page.pack_start(search_box, False, False, 0)

        order_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        order_bar.set_name("list-toolbar")
        order_bar.set_margin_start(6)
        order_bar.set_margin_end(6)
        order_bar.set_margin_bottom(4)
        up_btn = Gtk.Button(label="↑")
        up_btn.get_style_context().add_class("btn-icon")
        up_btn.set_tooltip_text("Move up")
        up_btn.connect("clicked", self._move_up)
        dn_btn = Gtk.Button(label="↓")
        dn_btn.get_style_context().add_class("btn-icon")
        dn_btn.set_tooltip_text("Move down")
        dn_btn.connect("clicked", self._move_down)
        order_bar.pack_start(up_btn, False, False, 0)
        order_bar.pack_start(dn_btn, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_start(4)
        sep.set_margin_end(4)
        order_bar.pack_start(sep, False, False, 0)

        sort_az_btn = Gtk.Button(label="A→Z")
        sort_az_btn.get_style_context().add_class("btn-icon")
        sort_az_btn.set_tooltip_text(_TIP_SORT_NAME_AZ)
        sort_az_btn.connect("clicked", lambda _: self._sort_scripts("name_asc"))
        sort_za_btn = Gtk.Button(label="Z→A")
        sort_za_btn.get_style_context().add_class("btn-icon")
        sort_za_btn.set_tooltip_text(_TIP_SORT_NAME_ZA)
        sort_za_btn.connect("clicked", lambda _: self._sort_scripts("name_desc"))
        order_bar.pack_start(sort_az_btn, False, False, 0)
        order_bar.pack_start(sort_za_btn, False, False, 0)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.set_margin_start(4)
        sep2.set_margin_end(4)
        order_bar.pack_start(sep2, False, False, 0)

        sort_port_asc_btn = Gtk.Button(label="P↑")
        sort_port_asc_btn.get_style_context().add_class("btn-icon")
        sort_port_asc_btn.set_tooltip_text("Sort by port 1→100")
        sort_port_asc_btn.connect("clicked", lambda _: self._sort_scripts("port_asc"))
        sort_port_desc_btn = Gtk.Button(label="P↓")
        sort_port_desc_btn.get_style_context().add_class("btn-icon")
        sort_port_desc_btn.set_tooltip_text("Sort by port 100→1")
        sort_port_desc_btn.connect("clicked", lambda _: self._sort_scripts("port_desc"))
        order_bar.pack_start(sort_port_asc_btn, False, False, 0)
        order_bar.pack_start(sort_port_desc_btn, False, False, 0)

        sep3 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep3.set_margin_start(4)
        sep3.set_margin_end(4)
        order_bar.pack_start(sep3, False, False, 0)

        sort_run_btn = Gtk.Button(label="▶↑")
        sort_run_btn.get_style_context().add_class("btn-icon")
        sort_run_btn.set_tooltip_text(_TIP_RUNNING_FIRST)
        sort_run_btn.connect("clicked", lambda _: self._sort_scripts("running_first"))
        sort_stop_btn = Gtk.Button(label="■↑")
        sort_stop_btn.get_style_context().add_class("btn-icon")
        sort_stop_btn.set_tooltip_text(_TIP_STOPPED_FIRST)
        sort_stop_btn.connect("clicked", lambda _: self._sort_scripts("stopped_first"))
        order_bar.pack_start(sort_run_btn, False, False, 0)
        order_bar.pack_start(sort_stop_btn, False, False, 0)

        all_page.pack_start(order_bar, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_filter_func(self._filter_row)
        self.listbox.connect("row-selected", self._row_selected)
        scroll.add(self.listbox)
        all_page.pack_start(scroll, True, True, 0)

        self.sidebar_stack.add_named(all_page, "all")

        # -- Groups page --
        groups_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        manage_groups_btn = Gtk.Button(label="+ New Group")
        manage_groups_btn.get_style_context().add_class("btn-primary")
        manage_groups_btn.set_margin_start(6)
        manage_groups_btn.set_margin_end(6)
        manage_groups_btn.set_margin_top(6)
        manage_groups_btn.connect("clicked", lambda _: self._new_group_and_select())
        groups_page.pack_start(manage_groups_btn, False, False, 0)

        groups_search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        groups_search_box.set_name("list-toolbar")
        groups_search_box.set_margin_start(6)
        groups_search_box.set_margin_end(6)
        groups_search_box.set_margin_top(6)
        groups_search_box.set_margin_bottom(6)
        self.groups_search_entry = Gtk.SearchEntry()
        self.groups_search_entry.get_style_context().add_class("form-entry")
        self.groups_search_entry.set_placeholder_text("Filter groups…")
        self.groups_search_entry.set_hexpand(True)
        self.groups_search_entry.connect("search-changed", lambda _: self._rebuild_groups_view())
        groups_search_box.pack_start(self.groups_search_entry, True, True, 0)
        groups_page.pack_start(groups_search_box, False, False, 0)

        groups_order_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        groups_order_bar.set_name("list-toolbar")
        groups_order_bar.set_margin_start(6)
        groups_order_bar.set_margin_end(6)
        groups_order_bar.set_margin_bottom(4)
        g_up_btn = Gtk.Button(label="↑")
        g_up_btn.get_style_context().add_class("btn-icon")
        g_up_btn.set_tooltip_text("Move group up")
        g_up_btn.connect("clicked", self._move_group_up)
        g_dn_btn = Gtk.Button(label="↓")
        g_dn_btn.get_style_context().add_class("btn-icon")
        g_dn_btn.set_tooltip_text("Move group down")
        g_dn_btn.connect("clicked", self._move_group_down)
        groups_order_bar.pack_start(g_up_btn, False, False, 0)
        groups_order_bar.pack_start(g_dn_btn, False, False, 0)

        g_sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        g_sep.set_margin_start(4)
        g_sep.set_margin_end(4)
        groups_order_bar.pack_start(g_sep, False, False, 0)

        g_sort_az = Gtk.Button(label="A→Z")
        g_sort_az.get_style_context().add_class("btn-icon")
        g_sort_az.set_tooltip_text(_TIP_SORT_NAME_AZ)
        g_sort_az.connect("clicked", lambda _: self._sort_groups("name_asc"))
        g_sort_za = Gtk.Button(label="Z→A")
        g_sort_za.get_style_context().add_class("btn-icon")
        g_sort_za.set_tooltip_text(_TIP_SORT_NAME_ZA)
        g_sort_za.connect("clicked", lambda _: self._sort_groups("name_desc"))
        groups_order_bar.pack_start(g_sort_az, False, False, 0)
        groups_order_bar.pack_start(g_sort_za, False, False, 0)

        g_sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        g_sep2.set_margin_start(4)
        g_sep2.set_margin_end(4)
        groups_order_bar.pack_start(g_sep2, False, False, 0)

        g_sort_count_asc = Gtk.Button(label="S↑")
        g_sort_count_asc.get_style_context().add_class("btn-icon")
        g_sort_count_asc.set_tooltip_text("Fewer scripts first")
        g_sort_count_asc.connect("clicked", lambda _: self._sort_groups("count_asc"))
        g_sort_count_desc = Gtk.Button(label="S↓")
        g_sort_count_desc.get_style_context().add_class("btn-icon")
        g_sort_count_desc.set_tooltip_text("More scripts first")
        g_sort_count_desc.connect("clicked", lambda _: self._sort_groups("count_desc"))
        groups_order_bar.pack_start(g_sort_count_asc, False, False, 0)
        groups_order_bar.pack_start(g_sort_count_desc, False, False, 0)

        g_sep3 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        g_sep3.set_margin_start(4)
        g_sep3.set_margin_end(4)
        groups_order_bar.pack_start(g_sep3, False, False, 0)

        g_sort_run = Gtk.Button(label="▶↑")
        g_sort_run.get_style_context().add_class("btn-icon")
        g_sort_run.set_tooltip_text(_TIP_RUNNING_FIRST)
        g_sort_run.connect("clicked", lambda _: self._sort_groups("running_first"))
        g_sort_stop = Gtk.Button(label="■↑")
        g_sort_stop.get_style_context().add_class("btn-icon")
        g_sort_stop.set_tooltip_text(_TIP_STOPPED_FIRST)
        g_sort_stop.connect("clicked", lambda _: self._sort_groups("stopped_first"))
        groups_order_bar.pack_start(g_sort_run, False, False, 0)
        groups_order_bar.pack_start(g_sort_stop, False, False, 0)

        groups_page.pack_start(groups_order_bar, False, False, 0)

        groups_scroll = Gtk.ScrolledWindow()
        groups_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        groups_scroll.set_vexpand(True)
        self.groups_listbox = Gtk.ListBox()
        self.groups_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.groups_listbox.connect("row-selected", self._group_row_selected)
        groups_scroll.add(self.groups_listbox)
        groups_page.pack_start(groups_scroll, True, True, 0)

        self.sidebar_stack.add_named(groups_page, "groups")

        left_box.pack_start(self.sidebar_stack, True, True, 0)

        hpaned.pack1(left_box, False, False)

        # -- Right: form stack (script / group) --
        self.right_stack = Gtk.Stack()
        self.right_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.right_stack.set_transition_duration(100)

        self.form = ScriptForm(
            on_save=self._save_script,
            on_delete=self._delete_script,
            on_run=self._run_script,
            on_duplicate=self._duplicate_script,
            on_stop=self._stop_script,
        )
        self.form.set_sensitive(False)
        self.right_stack.add_named(self.form, "script")

        self.group_form = GroupForm(
            on_save=self._save_group,
            on_delete=self._delete_group_from_form,
            on_run_all=self._run_group,
            on_stop_all=self._stop_group,
            on_scripts_changed=self._update_group_row,
            on_duplicate=self._duplicate_group,
        )
        self.group_form.set_sensitive(False)
        self.right_stack.add_named(self.group_form, "group")

        hpaned.pack2(self.right_stack, True, False)

        self._load_list()
        ScriptRow._on_run = self._run_script
        ScriptRow._on_stop = self._stop_single_script
        self.show_all()
        # Select first script if available
        first = self.listbox.get_row_at_index(0)
        if first:
            self.listbox.select_row(first)
        else:
            self.form.clear()

        # Auto-refresh logs tab + detect state changes for badges
        self._last_error_state = get_error_states()
        self._last_running_state = get_running_ids()
        GLib.timeout_add_seconds(2, self._refresh_logs_tab)

    def _refresh_logs_tab(self) -> bool:
        # Check if error/running states changed — only then rebuild sidebar
        new_errors = get_error_states()
        new_running = get_running_ids()
        self.form._reload_log()
        self.form._update_error_banner(errors=new_errors)
        if new_errors != self._last_error_state or new_running != self._last_running_state:
            self._last_error_state = new_errors
            self._last_running_state = new_running
            self._refresh_running_badges()
            if self._sidebar_mode == "groups":
                self._rebuild_groups_view()
            if self.right_stack.get_visible_child_name() == "group":
                self.group_form.update_running_state(new_running)
        # Always update stop button (port may be busy from external process)
        self._update_stop_button(new_running)
        return True

    # -- running badge refresh --------------------------------------------------

    def _refresh_running_badges(self) -> bool:
        """Update RUN/ERR badges in-place without rebuilding the list."""
        ScriptRow._shared_error_states = get_error_states()
        ScriptRow._shared_running_ids = get_running_ids()
        for row in self.listbox.get_children():
            row._update_badges()
        return True

    def _update_stop_button(self, running=None):
        """Update stop button state based on running status and port usage."""
        if running is None:
            running = get_running_ids()
        cur = self.form._script
        if not cur:
            return
        sid = cur.get("id", "")
        is_running = sid in running
        port_str = cur.get("port", "").strip()
        port_busy = False
        if port_str and port_str.isdigit() and not is_running:
            from tray import _is_port_in_use
            port_busy = _is_port_in_use(int(port_str))
        self.form.stop_btn.set_sensitive(is_running or port_busy)
        if is_running:
            from tray import find_ports_for_pid
            pid = find_script_pid(sid)
            ports = find_ports_for_pid(pid) if pid else []
            if ports:
                port_list = ", ".join(str(p) for p in ports)
                self.form.stop_btn.set_label(f"{_STOP_LABEL} (:{port_list})")
            else:
                self.form.stop_btn.set_label(_STOP_LABEL)
        elif port_busy:
            self.form.stop_btn.set_label(f"{_STOP_LABEL} (:{port_str} busy)")
        else:
            self.form.stop_btn.set_label(_STOP_LABEL)

    # -- list management --------------------------------------------------------

    def _on_tab_toggled(self, button, tab):
        if self._switching_tab:
            return
        # If user tries to deactivate the active tab, force it back on
        if not button.get_active():
            if self._sidebar_mode == tab:
                button.set_active(True)
            return
        self._switch_tab(tab)

    def _switch_tab(self, tab):
        self._switching_tab = True
        self._sidebar_mode = tab
        self.tab_all_btn.set_active(tab == "all")
        self.tab_groups_btn.set_active(tab == "groups")
        self._switching_tab = False
        self.sidebar_stack.set_visible_child_name(tab)
        if tab == "groups":
            self._rebuild_groups_view()
            if self._selected_group_id:
                cfg = load_config()
                group = next((g for g in cfg.get("groups", []) if g["id"] == self._selected_group_id), None)
                if group:
                    self.group_form.load_group(group)
                    self.right_stack.set_visible_child_name("group")
        elif tab == "all":
            row = self.listbox.get_selected_row()
            if row:
                self.form.load_script(row.script)
                self.right_stack.set_visible_child_name("script")

    def _rebuild_groups_view(self):
        for child in self.groups_listbox.get_children():
            self.groups_listbox.remove(child)

        cfg = load_config()
        groups = cfg.get("groups", [])
        scripts = cfg.get("scripts", [])
        running = get_running_ids()

        GroupRow._shared_running_ids = running
        GroupRow._on_run_group = self._run_group
        GroupRow._on_stop_group = self._stop_group
        GroupRow._on_run_script = self._run_script
        GroupRow._on_stop_script = self._stop_single_script
        GroupRow._on_select_script_settings = self._open_script_settings
        GroupRow._on_select_script_logs = self._open_script_logs
        GroupRow._on_open_terminal = self._open_terminal

        query = self.groups_search_entry.get_text().lower().strip()
        if query:
            groups = [g for g in groups if query in g.get("name", "").lower()
                      or query in g.get("description", "").lower()]

        if not groups:
            msg = "No groups match the filter." if query else "No groups yet.\nClick '+ New Group' to create one."
            placeholder = Gtk.Label(label=msg)
            placeholder.get_style_context().add_class("empty-state")
            placeholder.set_justify(Gtk.Justification.CENTER)
            placeholder.set_margin_top(40)
            self.groups_listbox.set_placeholder(placeholder)
            placeholder.show()
            self.groups_listbox.show_all()
            return

        select_row = None
        for group in groups:
            gid = group["id"]
            group_scripts = [s for s in scripts if gid in s.get("groups", []) and s.get("enabled", True)]
            row = GroupRow(group, group_scripts)
            self.groups_listbox.add(row)
            if gid == self._selected_group_id:
                select_row = row

        self.groups_listbox.show_all()
        if select_row:
            self.groups_listbox.select_row(select_row)

    def _update_group_row(self, group):
        """Update only the selected group row in-place without full rebuild."""
        gid = group["id"]
        cfg = load_config()
        scripts = cfg.get("scripts", [])
        running = get_running_ids()
        group_scripts = [s for s in scripts if gid in s.get("groups", []) and s.get("enabled", True)]

        GroupRow._shared_running_ids = running
        GroupRow._on_run_group = self._run_group
        GroupRow._on_stop_group = self._stop_group
        GroupRow._on_run_script = self._run_script
        GroupRow._on_stop_script = self._stop_single_script
        GroupRow._on_select_script_settings = self._open_script_settings
        GroupRow._on_select_script_logs = self._open_script_logs
        GroupRow._on_open_terminal = self._open_terminal

        for row in self.groups_listbox.get_children():
            if isinstance(row, GroupRow) and row.group.get("id") == gid:
                idx = row.get_index()
                self.groups_listbox.remove(row)
                new_row = GroupRow(group, group_scripts)
                self.groups_listbox.insert(new_row, idx)
                new_row.show_all()
                self.groups_listbox.select_row(new_row)
                break

    def _group_row_selected(self, _listbox, row):
        if row and isinstance(row, GroupRow) and row.group.get("id") != self._selected_group_id:
            self._select_group(row.group)

    def _new_group_and_select(self):
        cfg = load_config()
        group = new_group("New Group")
        cfg.setdefault("groups", []).append(group)
        save_config(cfg)
        self._rebuild_groups_view()
        self.form._rebuild_group_checkboxes()
        self._select_group(group)
        self.group_form.notebook.set_current_page(0)
        # Focus name entry so user can type immediately
        self.group_form.name_entry.grab_focus()
        self.group_form.name_entry.select_region(0, -1)

    def _select_group(self, group):
        self._selected_group_id = group["id"]
        self.group_form.load_group(group)
        self.right_stack.set_visible_child_name("group")
        self._rebuild_groups_view()

    def _save_group(self, updated):
        cfg = load_config()
        for i, g in enumerate(cfg.get("groups", [])):
            if g["id"] == updated["id"]:
                cfg["groups"][i] = updated
                break
        save_config(cfg)
        self._rebuild_groups_view()
        self.form._rebuild_group_checkboxes()

    def _duplicate_group(self, group):
        if not group:
            return
        cfg = load_config()
        dup = new_group(f"{group.get('name', 'Group')} (copy)")
        dup["description"] = group.get("description", "")
        cfg.setdefault("groups", []).append(dup)
        # Copy script associations
        for s in cfg.get("scripts", []):
            if group["id"] in s.get("groups", []):
                s.setdefault("groups", []).append(dup["id"])
        save_config(cfg)
        self._rebuild_groups_view()
        self.form._rebuild_group_checkboxes()
        self._select_group(dup)
        self.group_form.notebook.set_current_page(0)
        self.group_form.name_entry.grab_focus()
        self.group_form.name_entry.select_region(0, -1)

    def _delete_group_from_form(self, group):
        if not group:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Delete group '{group.get('name', '')}'?",
        )
        dialog.format_secondary_text("Scripts will not be deleted, only ungrouped.")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = dialog.add_button("Delete", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("btn-danger")
        if dialog.run() == Gtk.ResponseType.OK:
            cfg = load_config()
            cfg["groups"] = [g for g in cfg.get("groups", []) if g["id"] != group["id"]]
            for s in cfg.get("scripts", []):
                s["groups"] = [gid for gid in s.get("groups", []) if gid != group["id"]]
            save_config(cfg)
            self.group_form.clear()
            self.right_stack.set_visible_child_name("script")
            self._rebuild_groups_view()
            self.form._rebuild_group_checkboxes()
        dialog.destroy()

    def _select_script_from_group(self, script):
        for row in self.listbox.get_children():
            if row.script.get("id") == script.get("id"):
                self.listbox.select_row(row)
                break

    def _open_script_settings(self, script):
        """Navigate to a script's Settings tab from group view."""
        self.form.load_script(script)
        self.form.notebook.set_current_page(0)
        self.right_stack.set_visible_child_name("script")

    def _open_terminal(self, script):
        """Open a terminal in the script's working directory."""
        cwd = script.get("working_dir", str(Path.home()))
        cwd = str(Path(cwd).expanduser())
        import subprocess
        subprocess.Popen(["x-terminal-emulator"], cwd=cwd)

    def _open_script_logs(self, script):
        """Navigate to a script's Logs tab from group view."""
        self.form.load_script(script)
        self.form.notebook.set_current_page(1)
        self.right_stack.set_visible_child_name("script")

    def _run_group(self, group):
        cfg = load_config()
        gid = group["id"]
        from tray import run_script
        count = 0
        for script in cfg.get("scripts", []):
            if gid in script.get("groups", []) and script.get("enabled", True):
                run_script(script)
                count += 1
        GLib.timeout_add(500, lambda: self._refresh_running_badges() and False)
        GLib.timeout_add(600, lambda: self._rebuild_groups_view() or False)
        self._show_toast(f"Running {count} script(s) from '{group['name']}'")

    def _stop_group(self, group):
        cfg = load_config()
        gid = group["id"]
        running = get_running_ids()
        count = 0
        for script in cfg.get("scripts", []):
            sid = script.get("id", "")
            if gid in script.get("groups", []) and sid in running:
                stop_script(sid)
                port_str = script.get("port", "").strip()
                if port_str and port_str.isdigit():
                    from tray import kill_port
                    kill_port(int(port_str))
                count += 1
        self._refresh_running_badges()
        self._rebuild_groups_view()
        self._show_toast(f"Stopped {count} script(s) from '{group['name']}'")

    def _move_group_up(self, _widget):
        group = self.group_form._group
        if not group:
            return
        cfg = load_config()
        groups = cfg.get("groups", [])
        idx = next((i for i, g in enumerate(groups) if g["id"] == group["id"]), -1)
        if idx <= 0:
            return
        groups[idx - 1], groups[idx] = groups[idx], groups[idx - 1]
        save_config(cfg)
        self._rebuild_groups_view()

    def _move_group_down(self, _widget):
        group = self.group_form._group
        if not group:
            return
        cfg = load_config()
        groups = cfg.get("groups", [])
        idx = next((i for i, g in enumerate(groups) if g["id"] == group["id"]), -1)
        if idx < 0 or idx >= len(groups) - 1:
            return
        groups[idx + 1], groups[idx] = groups[idx], groups[idx + 1]
        save_config(cfg)
        self._rebuild_groups_view()

    def _sort_groups(self, mode):
        cfg = load_config()
        groups = cfg.get("groups", [])
        scripts = cfg.get("scripts", [])
        running = get_running_ids()

        def _script_count(g):
            return len([s for s in scripts if g["id"] in s.get("groups", []) and s.get("enabled", True)])

        def _any_running(g):
            return any(
                s.get("id", "") in running
                for s in scripts
                if g["id"] in s.get("groups", []) and s.get("enabled", True)
            )

        if mode == "name_asc":
            groups.sort(key=lambda g: g.get("name", "").lower())
        elif mode == "name_desc":
            groups.sort(key=lambda g: g.get("name", "").lower(), reverse=True)
        elif mode == "count_asc":
            groups.sort(key=_script_count)
        elif mode == "count_desc":
            groups.sort(key=_script_count, reverse=True)
        elif mode == "running_first":
            groups.sort(key=lambda g: not _any_running(g))
        elif mode == "stopped_first":
            groups.sort(key=lambda g: _any_running(g))

        cfg["groups"] = groups
        save_config(cfg)
        self._rebuild_groups_view()

    def _stop_single_script(self, script):
        sid = script.get("id", "")
        stop_script(sid)
        port_str = script.get("port", "").strip()
        if port_str and port_str.isdigit():
            from tray import kill_port
            kill_port(int(port_str))
        self._refresh_running_badges()
        if self._sidebar_mode == "groups":
            self._rebuild_groups_view()

    def _load_list(self):
        for row in self.listbox.get_children():
            self.listbox.remove(row)
        ScriptRow._shared_error_states = get_error_states()
        ScriptRow._shared_running_ids = get_running_ids()
        cfg = load_config()
        for script in cfg.get("scripts", []):
            self.listbox.add(ScriptRow(script))
        self.listbox.show_all()
        if self._sidebar_mode == "groups":
            self._rebuild_groups_view()
        if self.group_form._group:
            self.group_form._rebuild_script_checkboxes()

    def _filter_row(self, row: ScriptRow) -> bool:
        query = self.search_entry.get_text().lower()
        if not query:
            return True
        name = row.script.get("name", "").lower()
        cmd  = row.script.get("command", "").lower()
        return query in name or query in cmd

    def _filter_changed(self, _entry):
        self.listbox.invalidate_filter()

    def _row_selected(self, _listbox, row):
        if row:
            self.form.load_script(row.script)
            self.right_stack.set_visible_child_name("script")
        else:
            self.form.clear()

    def _selected_index(self) -> int:
        row = self.listbox.get_selected_row()
        if row is None:
            return -1
        return row.get_index()

    def _move_up(self, _widget):
        idx = self._selected_index()
        if idx <= 0:
            return
        cfg     = load_config()
        scripts = cfg["scripts"]
        scripts[idx - 1], scripts[idx] = scripts[idx], scripts[idx - 1]
        save_config(cfg)
        self._load_list()
        self.listbox.select_row(self.listbox.get_row_at_index(idx - 1))

    def _move_down(self, _widget):
        idx = self._selected_index()
        cfg = load_config()
        scripts = cfg["scripts"]
        if idx < 0 or idx >= len(scripts) - 1:
            return
        scripts[idx + 1], scripts[idx] = scripts[idx], scripts[idx + 1]
        save_config(cfg)
        self._load_list()
        self.listbox.select_row(self.listbox.get_row_at_index(idx + 1))

    def _sort_scripts(self, mode):
        cfg = load_config()
        scripts = cfg["scripts"]
        selected_row = self.listbox.get_selected_row()
        selected_id = selected_row.script.get("id") if selected_row else None

        def _port_key(s):
            p = s.get("port", "").strip()
            return int(p) if p.isdigit() else 0

        running = get_running_ids()

        if mode == "name_asc":
            scripts.sort(key=lambda s: s.get("name", "").lower())
        elif mode == "name_desc":
            scripts.sort(key=lambda s: s.get("name", "").lower(), reverse=True)
        elif mode == "port_asc":
            scripts.sort(key=_port_key)
        elif mode == "port_desc":
            scripts.sort(key=_port_key, reverse=True)
        elif mode == "running_first":
            scripts.sort(key=lambda s: s.get("id", "") not in running)
        elif mode == "stopped_first":
            scripts.sort(key=lambda s: s.get("id", "") in running)

        save_config(cfg)
        self._load_list()
        if selected_id:
            for row in self.listbox.get_children():
                if row.script.get("id") == selected_id:
                    self.listbox.select_row(row)
                    break

    # -- CRUD -------------------------------------------------------------------

    def _new_script(self, _widget=None):
        script = new_script()
        cfg    = load_config()
        cfg["scripts"].append(script)
        save_config(cfg)
        if self._sidebar_mode != "all":
            self._switch_tab("all")
        self._load_list()
        # Select the new row
        last = self.listbox.get_row_at_index(len(cfg["scripts"]) - 1)
        if last:
            self.listbox.select_row(last)
            self.form.load_script(script)
            self.form.notebook.set_current_page(0)
            self.right_stack.set_visible_child_name("script")
            self.form.name_entry.grab_focus()

    def _save_script(self, updated: dict):
        cfg = load_config()
        for i, s in enumerate(cfg["scripts"]):
            if s["id"] == updated["id"]:
                cfg["scripts"][i] = updated
                break
        save_config(cfg)
        # Update the sidebar row in place
        ScriptRow._shared_error_states = get_error_states()
        for row in self.listbox.get_children():
            if row.script["id"] == updated["id"]:
                row.script = updated
                for child in row.get_children():
                    row.remove(child)
                row._build()
                break

    def _delete_script(self, script: dict):
        if not script:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Delete '{script.get('name', 'this script')}'?",
        )
        dialog.format_secondary_text("This cannot be undone.")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok_btn = dialog.add_button("Delete", Gtk.ResponseType.OK)
        ok_btn.get_style_context().add_class("btn-danger")
        resp = dialog.run()
        dialog.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        cfg = load_config()
        cfg["scripts"] = [s for s in cfg["scripts"] if s["id"] != script["id"]]
        save_config(cfg)
        self.form.clear()
        self._load_list()

    def _run_script(self, script: dict):
        if not script or not script.get("command", "").strip():
            return
        from tray import run_script
        run_script(script)
        # Refresh after a short delay so the process has time to register
        GLib.timeout_add(500, lambda: self._refresh_running_badges() and False)

    def _stop_script(self, script: dict):
        if not script:
            return
        stop_script(script.get("id", ""))
        # Also kill by port if configured (fallback for stubborn processes)
        port_str = script.get("port", "").strip()
        if port_str and port_str.isdigit():
            from tray import kill_port
            kill_port(int(port_str))
        self._refresh_running_badges()
        self._show_toast("Script stopped ■")

    # -- tray control ----------------------------------------------------------

    def _reload_tray(self, _widget=None):
        """Touch the config file to trigger hot-reload in the tray daemon."""
        if CONFIG_FILE.exists():
            CONFIG_FILE.touch()
        self._show_toast("Tray reloaded ↺")

    # -- duplicate / import / export -------------------------------------------

    def _duplicate_script(self, script: dict):
        if not script:
            return
        dup = dict(script)
        dup["id"] = str(uuid.uuid4())[:8]
        dup["name"] = script.get("name", "") + " (copy)"
        dup["groups"] = list(script.get("groups", []))
        cfg = load_config()
        # Insert after current script
        idx = -1
        for i, s in enumerate(cfg["scripts"]):
            if s["id"] == script.get("id"):
                idx = i
                break
        cfg["scripts"].insert(idx + 1, dup)
        save_config(cfg)
        self._load_list()
        row = self.listbox.get_row_at_index(idx + 1)
        if row:
            self.listbox.select_row(row)
            self.form.load_script(dup)
        self._show_toast("Script duplicated")

    def _import_config(self, _widget=None):
        dialog = Gtk.FileChooserDialog(
            title="Import Scripts",
            parent=self,
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
            with open(path) as f:
                imported = json.load(f)
            scripts = imported.get("scripts", [])
            if not scripts:
                self._show_toast("No scripts found in file")
                return
            cfg = load_config()
            existing_ids = {s["id"] for s in cfg["scripts"]}
            added = 0
            for s in scripts:
                if s.get("id") not in existing_ids:
                    cfg["scripts"].append(s)
                    added += 1
            existing_gids = {g["id"] for g in cfg.get("groups", [])}
            for g in imported.get("groups", []):
                if g.get("id") not in existing_gids:
                    cfg.setdefault("groups", []).append(g)
            save_config(cfg)
            self._load_list()
            self._show_toast(f"Imported {added} script(s)")
        except Exception as e:
            self._show_toast(f"Import failed: {e}")

    def _export_config(self, _widget=None):
        dialog = Gtk.FileChooserDialog(
            title="Export Scripts",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_current_name("lazylauncher-config.json")
        f = Gtk.FileFilter()
        f.set_name("JSON files")
        f.add_pattern("*.json")
        dialog.add_filter(f)
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            dialog.destroy()
            try:
                import shutil
                shutil.copy2(str(CONFIG_FILE), path)
                self._show_toast(f"Exported to {Path(path).name}")
            except Exception as e:
                self._show_toast(f"Export failed: {e}")
        else:
            dialog.destroy()

    # -- group management ------------------------------------------------------

    # -- toast -----------------------------------------------------------------

    def _show_toast(self, msg: str):
        """Briefly show a message in the header subtitle."""
        hb = self.get_titlebar()
        old = hb.get_subtitle() or "Manage your tray scripts"
        hb.set_subtitle(msg)
        GLib.timeout_add(1800, lambda: hb.set_subtitle(old) or False)


# -- application ----------------------------------------------------------------

class ManagerApp(Gtk.Application):
    def __init__(self):
        from gi.repository import Gio
        super().__init__(application_id="lazylauncher", flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = ManagerWindow(self)
        win.present()
        win.grab_focus()


def main():
    app = ManagerApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
