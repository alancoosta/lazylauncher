#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LazyLauncher - Manager UI
A GTK3 window to add, edit, delete and reorder scripts.
Writes to ~/.config/lazylauncher/.lazylauncher-config.json.
The tray daemon hot-reloads that file automatically.
"""

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .common import (
    CONFIG_FILE,
    config_lock, load_config, save_config,
    get_error_states, get_running_ids, find_script_pid,
    _is_pid_alive, _mark_stopped,
    migrate_state, ensure_seed_config,
    load_ui_state, save_ui_state,
)
from .deps import run_group_ordered
from .sorting import sort_scripts
from .runner import set_prompter
from . import ansi
from . import config_io

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

from .ui_shared import (
    _STOP_LABEL,
    _TIP_SORT_NAME_AZ, _TIP_SORT_NAME_ZA,
    _TIP_RUNNING_FIRST, _TIP_STOPPED_FIRST,
    _is_dark_theme, new_script, new_group, make_tab_button,
)
from .rows import ScriptRow, GroupRow
from .home_view import HomeView
from .script_form import ScriptForm
from .group_form import GroupForm


class _GtkPrompter:
    """GTK3 implementation of runner's prompter protocol (used by the manager).

    runner is GTK-free; the manager installs this so the launch flow's yes/no
    dialogs render with the manager's toolkit.
    """

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

/* -- headerbar -- */
headerbar {
    padding: 4px 10px;
    min-height: 46px;
}
headerbar .title {
    font-size: 14px;
    font-weight: 700;
}
headerbar .subtitle {
    font-size: 11px;
    opacity: 0.6;
}

/* -- sidebar -- */
#sidebar {
    min-width: 230px;
}
#sidebar row {
    padding: 11px 16px;
}
#sidebar row:selected {
    background-color: @theme_base_color;
    color: @theme_text_color;
    border-left: 3px solid @theme_selected_bg_color;
}
#sidebar row:selected label {
    color: @theme_text_color;
}
#sidebar row:selected image {
    color: @theme_text_color;
}
#sidebar row:selected .badge-error,
#sidebar row:selected .badge-running,
#sidebar row:selected .badge-port,
#sidebar row:selected .badge-pinned {
    color: #ffffff;
}
#sidebar row:selected .script-cmd {
    opacity: 0.5;
}
.script-name {
    font-size: 13px;
    font-weight: 600;
}
.script-cmd {
    font-size: 11px;
    opacity: 0.5;
}
.script-disabled .script-name {
    opacity: 0.35;
}

/* -- home table -- */
.home-table treeview {
    font-size: 13px;
}
.home-table treeview header button {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.9px;
    padding: 4px 6px;
}
.home-table treeview row {
    min-height: 0;
}
/* Keep the per-row action icons one constant color, like the editor sidebar's
   icon buttons: hovering (or prelighting) a Home row must not recolor its
   symbolic icons. The green run icon is a fixed pixbuf, unaffected by this. */
.home-table treeview.view:hover,
.home-table treeview.view:selected:hover,
.home-table treeview.view row:hover {
    color: @theme_text_color;
}

/* -- badges (semantic colors kept) -- */
.badge-pinned {
    background-color: @theme_selected_bg_color;
    color: @theme_selected_fg_color;
    border-radius: 4px;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 700;
}
.badge-disabled {
    opacity: 0.5;
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
    padding: 6px 8px;
}
#list-toolbar entry {
    border-radius: 7px;
    padding: 6px 10px;
    font-size: 12px;
}

/* -- form panel -- */
.form-label {
    font-size: 10px;
    font-weight: 700;
    opacity: 0.6;
    letter-spacing: 0.9px;
    margin-bottom: 4px;
}
.form-entry {
    border-radius: 8px;
    padding: 9px 12px;
    font-size: 13px;
}
.form-hint {
    font-size: 11px;
    opacity: 0.45;
    margin-top: 2px;
}
.section-header {
    font-size: 10px;
    font-weight: 700;
    color: @theme_selected_bg_color;
    letter-spacing: 1.1px;
    padding-bottom: 6px;
    margin-top: 20px;
    margin-bottom: 12px;
}

/* -- buttons -- */
button:not(.titlebutton) {
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 12px;
    font-weight: 600;
}
headerbar button.titlebutton {
    background: transparent;
    border: none;
    box-shadow: none;
    padding: 4px;
    min-width: 24px;
    min-height: 24px;
}

/* -- colored button variants (semantic) -- */
button.btn-primary {
    background-image: none;
    background-color: @theme_selected_bg_color;
    color: @theme_selected_fg_color;
    border-radius: 8px;
    border: none;
}
button.btn-danger {
    background-image: none;
    background-color: #c0392b;
    color: #ffffff;
    border-radius: 8px;
    border: none;
}
button.btn-danger:hover {
    background-image: none;
    background-color: #d44637;
}
button.btn-success {
    background-image: none;
    background-color: #27ae60;
    color: #ffffff;
    border-radius: 8px;
    border: none;
}
button.btn-success:hover {
    background-image: none;
    background-color: #2ecc71;
}
button.btn-warning {
    background-image: none;
    background-color: #e67e22;
    color: #ffffff;
    border-radius: 8px;
    border: none;
}
button.btn-warning:hover {
    background-image: none;
    background-color: #f39c12;
}
button.btn-secondary {
    border-radius: 8px;
}
button.btn-icon {
    padding: 2px;
    min-width: 20px;
    min-height: 20px;
    background-image: none;
    background-color: transparent;
    border: none;
    border-radius: 4px;
    box-shadow: none;
    opacity: 0.6;
}
button.btn-icon:hover {
    background-image: none;
    opacity: 1;
}
button.btn-icon-run {
    opacity: 0.6;
}
button.btn-icon-run:hover {
    color: #27ae60;
    opacity: 1;
}
button.btn-icon-run.running {
    color: #27ae60;
    opacity: 1;
}
/* keep the running play icon green even when its row is selected
   (otherwise "#sidebar row:selected image" repaints it white) */
#sidebar row:selected button.btn-icon-run.running,
#sidebar row:selected button.btn-icon-run.running image {
    color: #27ae60;
    opacity: 1;
}

/* -- dialog -- */
dialog .dialog-action-area {
    padding: 8px;
}
dialog .dialog-action-area button {
    margin: 4px;
    min-width: 80px;
}

/* -- switches -- */
switch {
    border-radius: 12px;
}
switch:checked {
    background-color: @theme_selected_bg_color;
}
switch slider {
    border-radius: 10px;
}

/* -- empty state -- */
.empty-state {
    opacity: 0.45;
    font-size: 13px;
}

/* -- scrollbar -- */
scrollbar slider {
    border-radius: 4px;
    min-width: 4px;
    min-height: 4px;
}

/* -- notebook tabs -- */
notebook header tabs tab {
    padding: 6px 16px;
    border: none;
}
notebook header tabs tab:checked {
    border-bottom: 2px solid @theme_selected_bg_color;
}

/* -- log viewer -- */
.log-view {
    font-family: 'Ubuntu Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 12px;
    padding: 8px;
}

/* -- group tabs -- */
#group-tabs {
    min-height: 32px;
}
.group-tab {
    background-color: transparent;
    border: none;
    border-radius: 0;
    padding: 6px 12px;
    font-size: 11px;
    font-weight: 600;
    border-bottom: 2px solid transparent;
    min-width: 40px;
    opacity: 0.6;
}
.group-tab:checked {
    opacity: 1;
    border-bottom: 2px solid @theme_selected_bg_color;
    background-color: transparent;
}
.group-tab:hover:not(:checked) {
    opacity: 0.8;
}

/* -- group checkbox -- */
checkbutton.group-check {
    font-size: 12px;
    padding: 3px 0;
}

/* -- group card -- */
.group-card {
    border-radius: 8px;
}
.group-card-selected {
    background-color: @theme_base_color;
    color: @theme_text_color;
    border-left: 3px solid @theme_selected_bg_color;
}
.group-card-selected *:not(.badge-error):not(.badge-running):not(.badge-port):not(.badge-pinned) {
    color: @theme_text_color;
}

/* -- log search bar -- */
.log-search-bar {
    border-radius: 6px;
    padding: 0;
    background-color: @theme_bg_color;
    border: 1px solid @borders;
}
.log-search-bar button {
    border-radius: 0;
    padding: 6px 8px;
    min-width: 0;
    border: none;
    background-image: none;
    background-color: @theme_bg_color;
    box-shadow: none;
}
.log-search-entry-wrap {
    border-radius: 6px 0 0 6px;
    background-color: @theme_base_color;
}
.log-search-entry-wrap entry {
    border-radius: 6px 0 0 6px;
    border: none;
    box-shadow: none;
    background-color: @theme_base_color;
    background-image: none;
}
.log-search-count {
    font-size: 11px;
    opacity: 0.6;
    padding: 0 6px;
}
"""


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

        # Set ANSI colors based on theme
        ansi.set_theme(_is_dark_theme())

        # runner is GTK-free; install the manager's dialogs for the launch flow.
        set_prompter(_GtkPrompter())

        # CSS
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._build_headerbar()

        # Top-level stack: home table vs. detail editor
        self.outer_stack = Gtk.Stack()
        self.outer_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.outer_stack.set_transition_duration(100)
        self.add(self.outer_stack)

        # Main layout
        hpaned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        hpaned.set_position(400)

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

        self.sidebar_stack.add_named(self._build_all_page(), "all")

        self.sidebar_stack.add_named(self._build_groups_page(), "groups")

        left_box.pack_start(self.sidebar_stack, True, True, 0)

        hpaned.pack1(left_box, False, False)

        hpaned.pack2(self._build_right_stack(), True, False)

        # Register the two top-level screens; open on Home.
        self.home_view = HomeView(
            on_open_script=self._home_open_script,
            on_open_group=self._home_open_group,
            on_new_script=self._new_script,
            on_new_group=self._new_group_and_select,
            on_run_script=self._home_run_script,
            on_stop_script=self._home_stop_script,
            on_run_group=self._home_run_group,
            on_stop_group=self._home_stop_group,
            on_edit_script=self._home_edit_script,
            on_edit_group_name=self._home_edit_group_name,
            on_restart_script=self._home_restart_script,
            on_terminal_script=self._home_terminal_script,
            on_restart_group=self._home_restart_group,
            on_add_script_to_group=self._home_add_script_to_group,
        )
        self.outer_stack.add_named(self.home_view, "home")
        self.outer_stack.add_named(hpaned, "detail")
        self.outer_stack.set_visible_child_name("home")
        self.outer_stack.connect("notify::visible-child", self._on_view_changed)
        # Reopen on the last-used top-level view (Home/Editor). Done after the
        # signal is wired so _on_view_changed syncs the switch buttons.
        saved_view = load_ui_state().get("view")
        if saved_view in ("home", "detail"):
            self.outer_stack.set_visible_child_name(saved_view)

        ScriptRow._on_run = self._run_script
        ScriptRow._on_stop = self._stop_single_script
        ScriptRow._on_restart = self._restart_script
        ScriptRow._on_select_script_settings = self._open_script_settings
        ScriptRow._on_select_script_logs = self._open_script_logs
        ScriptRow._on_select_script_envs = self._open_script_envs
        ScriptRow._on_open_terminal = self._open_terminal
        self._load_list()
        # Ctrl+F accelerator (works globally, even when focus is on an entry)
        accel = Gtk.AccelGroup()
        accel.connect(Gdk.KEY_f, Gdk.ModifierType.CONTROL_MASK, 0,
                      lambda *_: self._toggle_log_search() or True)
        self.add_accel_group(accel)
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

    def _build_headerbar(self):
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
        open_cfg_item = Gtk.MenuItem(label="Open Config File")
        open_cfg_item.connect("activate", self._open_config_file)
        menu.append(open_cfg_item)
        menu.append(Gtk.SeparatorMenuItem())
        reload_item = Gtk.MenuItem(label="Reload Tray")
        reload_item.connect("activate", self._reload_tray)
        menu.append(reload_item)
        menu.show_all()
        menu_btn.set_popup(menu)
        hb.pack_start(menu_btn)

        # View switcher: Home (table) | Editor (sidebar + form)
        view_switch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        view_switch.set_name("group-tabs")
        self._switching_view = False
        self.view_home_btn = make_tab_button("_Home", "home", self._on_view_toggled, active=True)
        self.view_editor_btn = make_tab_button("_Editor", "detail", self._on_view_toggled)
        view_switch.pack_start(self.view_home_btn, False, False, 0)
        view_switch.pack_start(self.view_editor_btn, False, False, 0)
        hb.set_custom_title(view_switch)

    def _build_all_page(self):
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

        return all_page

    def _build_groups_page(self):
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

        return groups_page

    def _build_right_stack(self):
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
            on_restart=self._restart_script,
        )
        self.form.set_sensitive(False)
        self.right_stack.add_named(self.form, "script")

        self.group_form = GroupForm(
            on_save=self._save_group,
            on_delete=self._delete_group_from_form,
            on_run_all=self._run_group,
            on_stop_all=self._stop_group,
            on_restart_all=self._restart_group,
            on_scripts_changed=self._update_group_row,
            on_duplicate=self._duplicate_group,
        )
        self.group_form.set_sensitive(False)
        self.right_stack.add_named(self.group_form, "group")

        return self.right_stack

    def _toggle_log_search(self):
        if self.right_stack.get_visible_child_name() == "script":
            self.form.notebook.set_current_page(1)  # Logs tab
            self.form.log_panel.open_search()

    def _refresh_logs_tab(self) -> bool:
        # Check if error/running states changed — only then rebuild sidebar
        new_errors = get_error_states()
        new_running = get_running_ids()
        if self.form.notebook.get_current_page() == 1:
            self.form.log_panel.reload_log()
        else:
            self.form.log_panel.mark_pending()
        self.form.log_panel.update_error_banner(errors=new_errors)
        if new_errors != self._last_error_state or new_running != self._last_running_state:
            self._last_error_state = new_errors
            self._last_running_state = new_running
            self._refresh_running_badges()
            self.home_view.refresh_running(new_running)
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
            from .runner import _is_port_in_use
            port_busy = _is_port_in_use(int(port_str))
        self.form.stop_btn.set_sensitive(is_running or port_busy)
        self.form.restart_btn.set_sensitive(is_running)
        if is_running:
            from .runner import find_ports_for_pid
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
        self.home_view.reload_groups()
        for child in self.groups_listbox.get_children():
            self.groups_listbox.remove(child)

        cfg = load_config()
        groups = cfg.get("groups", [])
        scripts = cfg.get("scripts", [])
        running = get_running_ids()

        GroupRow._shared_running_ids = running
        GroupRow._on_run_group = self._run_group
        GroupRow._on_stop_group = self._stop_group
        GroupRow._on_restart_group = self._restart_group
        GroupRow._on_select_group_settings = self._select_group
        GroupRow._on_run_script = self._run_script
        GroupRow._on_stop_script = self._stop_single_script
        GroupRow._on_restart_script = self._restart_script
        GroupRow._on_select_script_settings = self._open_script_settings
        GroupRow._on_select_script_logs = self._open_script_logs
        GroupRow._on_select_script_envs = self._open_script_envs
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
        GroupRow._on_restart_group = self._restart_group
        GroupRow._on_select_group_settings = self._select_group
        GroupRow._on_run_script = self._run_script
        GroupRow._on_stop_script = self._stop_single_script
        GroupRow._on_restart_script = self._restart_script
        GroupRow._on_select_script_settings = self._open_script_settings
        GroupRow._on_select_script_logs = self._open_script_logs
        GroupRow._on_select_script_envs = self._open_script_envs
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
        if row and isinstance(row, GroupRow):
            if row.group.get("id") != self._selected_group_id or self.right_stack.get_visible_child_name() != "group":
                self._select_group(row.group)

    def _new_group_and_select(self):
        group = new_group("New Group")
        with config_lock():
            cfg = load_config()
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
        with config_lock():
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
        dup = new_group(f"{group.get('name', 'Group')} (copy)")
        dup["description"] = group.get("description", "")
        with config_lock():
            cfg = load_config()
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
            with config_lock():
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

    def _open_script_envs(self, script):
        """Navigate to a script's Envs tab."""
        self.form.load_script(script)
        self.form.notebook.set_current_page(2)
        self.right_stack.set_visible_child_name("script")

    def _run_group(self, group):
        cfg = load_config()
        gid = group["id"]
        from .runner import run_script
        group_scripts = [
            s for s in cfg.get("scripts", [])
            if gid in s.get("groups", []) and s.get("enabled", True)
        ]
        if not group_scripts:
            return
        run_group_ordered(
            group_scripts,
            run_one=run_script,
            dispatch=GLib.idle_add,
            already_running=get_running_ids(),
            on_event=self._on_dep_event,
        )
        GLib.timeout_add(500, lambda: self._refresh_running_badges() and False)
        GLib.timeout_add(600, lambda: self._rebuild_groups_view() or False)
        self._show_toast(f"Running '{group['name']}' in dependency order…")

    def _on_dep_event(self, kind, sid, detail):
        """Progress callback for run_group_ordered (already on the GTK thread)."""
        if kind == "timeout":
            self._show_toast(f"⏱ Timed out waiting for {detail}")
        elif kind == "error":
            self._show_toast(f"⚠ {detail}")
        elif kind == "ready":
            self._refresh_running_badges()
        return False

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
                    from .runner import kill_port
                    kill_port(int(port_str))
                count += 1
        self._refresh_running_badges()
        self._rebuild_groups_view()
        self._show_toast(f"Stopped {count} script(s) from '{group['name']}'")

    def _restart_group(self, group):
        cfg = load_config()
        gid = group["id"]
        running = get_running_ids()
        from .runner import run_script
        scripts_to_restart = []
        for script in cfg.get("scripts", []):
            sid = script.get("id", "")
            if gid in script.get("groups", []) and sid in running:
                stop_script(sid)
                port_str = script.get("port", "").strip()
                if port_str and port_str.isdigit():
                    from .runner import kill_port
                    kill_port(int(port_str))
                scripts_to_restart.append(script)
        self._refresh_running_badges()
        self._rebuild_groups_view()
        def _restart_all():
            for script in scripts_to_restart:
                run_script(script)
            GLib.timeout_add(500, lambda: self._refresh_running_badges() and False)
            GLib.timeout_add(600, lambda: self._rebuild_groups_view() or False)
            return False
        GLib.timeout_add(600, _restart_all)
        self._show_toast(f"Restarting {len(scripts_to_restart)} script(s) from '{group['name']}' ↻")

    def _move_group_up(self, _widget):
        group = self.group_form._group
        if not group:
            return
        with config_lock():
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
        with config_lock():
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
            from .runner import kill_port
            kill_port(int(port_str))
        self._refresh_running_badges()
        if self._sidebar_mode == "groups":
            self._rebuild_groups_view()

    # -- Home table bridge (HomeView delegates navigation + persistence here) ----

    def _find_script(self, script_id):
        return next((s for s in load_config().get("scripts", [])
                     if s.get("id") == script_id), None)

    def _find_group(self, group_id):
        return next((g for g in load_config().get("groups", [])
                     if g.get("id") == group_id), None)

    def _update_config_item(self, key, item_id, field, value):
        """Set a field on a script/group by id; return the updated dict or None."""
        updated = None
        with config_lock():
            cfg = load_config()
            for item in cfg.get(key, []):
                if item.get("id") == item_id:
                    item[field] = value
                    updated = dict(item)
                    break
            save_config(cfg)
        return updated

    def _home_open_script(self, script_id, page=0):
        """Open a script in the editor at the given notebook page (0=Settings,
        1=Logs, 2=Envs) and switch to the detail view."""
        script = self._find_script(script_id)
        if not script:
            return
        if self._sidebar_mode != "all":
            self._switch_tab("all")
        for row in self.listbox.get_children():
            if row.script.get("id") == script_id:
                self.listbox.select_row(row)
                break
        self.form.load_script(script)
        self.form.notebook.set_current_page(page)
        self.right_stack.set_visible_child_name("script")
        self.outer_stack.set_visible_child_name("detail")

    def _home_open_group(self, group_id):
        group = self._find_group(group_id)
        if not group:
            return
        self.outer_stack.set_visible_child_name("detail")
        self._switch_tab("groups")
        for row in self.groups_listbox.get_children():
            if isinstance(row, GroupRow) and row.group.get("id") == group_id:
                self.groups_listbox.select_row(row)
                break
        self._select_group(group)

    def _home_run_script(self, script_id):
        script = self._find_script(script_id)
        if script:
            self._run_script(script)

    def _home_stop_script(self, script_id):
        script = self._find_script(script_id)
        if script:
            self._stop_single_script(script)

    def _home_restart_script(self, script_id):
        script = self._find_script(script_id)
        if script:
            self._restart_script(script)

    def _home_terminal_script(self, script_id):
        script = self._find_script(script_id)
        if script:
            self._open_terminal(script)

    def _home_restart_group(self, group_id):
        group = self._find_group(group_id)
        if group:
            self._restart_group(group)

    def _home_add_script_to_group(self, script_id, group_id):
        """Add an already-created script to a group from the Home view."""
        with config_lock():
            cfg = load_config()
            for s in cfg.get("scripts", []):
                if s.get("id") == script_id:
                    groups = s.get("groups", [])
                    if group_id not in groups:
                        groups.append(group_id)
                    s["groups"] = groups
                    break
            save_config(cfg)
        self._load_list()  # rebuilds sidebar, group form and the home tables
        self._show_toast("Script added to group")

    def _home_run_group(self, group_id):
        group = self._find_group(group_id)
        if group:
            self._run_group(group)

    def _home_stop_group(self, group_id):
        group = self._find_group(group_id)
        if group:
            self._stop_group(group)

    def _home_edit_script(self, script_id, field, value):
        """Persist an inline scripts-table edit and resync the editor side."""
        updated = self._update_config_item("scripts", script_id, field, value)
        if updated is None:
            return None
        self._load_list()
        if self.form._script and self.form._script.get("id") == script_id:
            self.form.load_script(updated)
        return updated

    def _home_edit_group_name(self, group_id, value):
        """Persist an inline group-name edit and resync the editor side."""
        updated = self._update_config_item("groups", group_id, "name", value)
        if updated is None:
            return None
        self._rebuild_groups_view()
        self.form._rebuild_group_checkboxes()
        if self.group_form._group and self.group_form._group.get("id") == group_id:
            self.group_form.load_group(updated)
        return updated

    def _on_view_toggled(self, button, view):
        if self._switching_view or not button.get_active():
            return
        self.outer_stack.set_visible_child_name(view)

    def _on_view_changed(self, *_):
        name = self.outer_stack.get_visible_child_name()
        self._switching_view = True
        self.view_home_btn.set_active(name == "home")
        self.view_editor_btn.set_active(name == "detail")
        self._switching_view = False
        save_ui_state(view=name)
        if name == "home":
            self.home_view.reload_active()

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
        self.home_view.reload_active()

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
        with config_lock():
            cfg     = load_config()
            scripts = cfg["scripts"]
            scripts[idx - 1], scripts[idx] = scripts[idx], scripts[idx - 1]
            save_config(cfg)
        self._load_list()
        self.listbox.select_row(self.listbox.get_row_at_index(idx - 1))

    def _move_down(self, _widget):
        idx = self._selected_index()
        with config_lock():
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

        cfg["scripts"] = sort_scripts(scripts, mode, get_running_ids())

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
        with config_lock():
            cfg = load_config()
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
        with config_lock():
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
        with config_lock():
            cfg = load_config()
            cfg["scripts"] = [s for s in cfg["scripts"] if s["id"] != script["id"]]
            save_config(cfg)
        self.form.clear()
        self._load_list()

    def _run_script(self, script: dict):
        if not script or not script.get("command", "").strip():
            return
        from .runner import run_script
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
            from .runner import kill_port
            kill_port(int(port_str))
        self._refresh_running_badges()
        self._show_toast("Script stopped ■")

    def _restart_script(self, script: dict):
        if not script:
            return
        self._stop_single_script(script)
        GLib.timeout_add(600, lambda: self._run_script(script) or False)
        self._show_toast("Script restarting ↻")

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
        with config_lock():
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
            imported = config_io.read_config_file(path)
            if not imported.get("scripts"):
                self._show_toast("No scripts found in file")
                return
            with config_lock():
                cfg = load_config()
                cfg, added = config_io.merge_imported(cfg, imported)
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
                self._show_toast(f"Exported to {Path(path).name}")
            except Exception as e:
                self._show_toast(f"Export failed: {e}")
        else:
            dialog.destroy()

    def _open_config_file(self, _widget=None):
        """Open the config file in the system's default application."""
        try:
            subprocess.Popen(["xdg-open", str(CONFIG_FILE)])
            self._show_toast(f"Opening {CONFIG_FILE.name}")
        except Exception as e:
            self._show_toast(f"Could not open config: {e}")

    # -- group management ------------------------------------------------------

    # -- toast -----------------------------------------------------------------

    def _show_toast(self, msg: str):
        """Briefly show a message in the header subtitle."""
        hb = self.get_titlebar()
        old = hb.get_subtitle() or "Manage your tray scripts"
        hb.set_subtitle(msg)
        GLib.timeout_add(1800, lambda: hb.set_subtitle(old) or False)


# Valid reverse-DNS GApplication id (a bare "lazylauncher" is rejected, which
# disabled single-instance and spewed GLib-GIO-CRITICAL). The window keeps
# WM_CLASS "lazylauncher" (set_wmclass) so the .desktop's StartupWMClass match —
# and thus the dock icon — is unaffected.
APP_ID = "io.github.alancoosta.LazyLauncher"


class ManagerApp(Gtk.Application):
    def __init__(self):
        from gi.repository import Gio
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        # Single-instance: reactivating (e.g. the tray relaunching us) must raise
        # the existing window, not spawn a second one.
        win = self.get_active_window()
        if win is None:
            win = ManagerWindow(self)
        win.present()


def main():
    migrate_state()
    ensure_seed_config()
    app = ManagerApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
