#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LazyLauncher - Manager UI
A GTK3 window to add, edit, delete and reorder scripts.
Writes to ~/.config/lazylauncher/.lazylauncher-config.json.
The tray daemon hot-reloads that file automatically.
"""

import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .common import (
    CONFIG_FILE,
    config_lock, load_config, save_config,
    get_error_states, get_running_ids, find_script_pid,
    migrate_state, ensure_seed_config,
    load_ui_state, save_ui_state, scripts_in_group,
)
from .deps import run_group_ordered
from .sorting import sort_scripts, sort_groups
from .runner import (
    set_prompter, run_script, stop_script, kill_port,
    find_ports_for_pid, _is_port_in_use,
)
from . import ansi
from .config_actions import ConfigActions

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

from .ui_shared import (
    _STOP_LABEL,
    _TIP_SORT_NAME_AZ, _TIP_SORT_NAME_ZA,
    _TIP_RUNNING_FIRST, _TIP_STOPPED_FIRST,
    _is_dark_theme, new_script, new_group, make_tab_button, make_icon_button,
    GtkPrompter, resolve_app_icon,
)
from .rows import ScriptRow, GroupRow
from .home_view import HomeView
from .graph_view import GraphView
from .env_table import EnvVarsTable
from .script_form import ScriptForm
from .group_form import GroupForm


_STYLES_FILE = Path(__file__).parent / "styles.css"


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
        icon = resolve_app_icon()
        if icon == "lazylauncher":
            self.set_icon_name(icon)
        elif icon:
            self.set_icon_from_file(icon)

        # Set ANSI colors based on theme
        ansi.set_theme(_is_dark_theme())

        # runner is GTK-free; install the manager's dialogs for the launch flow.
        set_prompter(GtkPrompter())

        # CSS
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(str(_STYLES_FILE))
        except GLib.Error:
            # Stylesheet missing/unreadable — fall back to no styling rather than
            # crash. Shouldn't happen in an installed copy (the package dir ships
            # styles.css), but keeps a raw checkout runnable.
            pass
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._config_actions = ConfigActions(self, self._load_list, self._show_toast)

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
        self.graph_view = GraphView()
        self.graph_view.connect("script-activated",
                                lambda _w, sid: self._home_open_script(sid))
        self.outer_stack.add_named(self.home_view, "home")
        self.outer_stack.add_named(hpaned, "detail")
        self.outer_stack.add_named(self._build_global_editor(), "envs")
        self.outer_stack.add_named(self.graph_view, "graph")
        self.outer_stack.set_visible_child_name("home")
        # Capture the saved view *before* wiring _on_view_changed and show_all():
        # show_all() emits notify::visible-child for the current ("home") page,
        # which would overwrite the persisted value before we get to read it.
        self._saved_view = load_ui_state().get("view")
        self.outer_stack.connect("notify::visible-child", self._on_view_changed)
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

        # Reopen on the last-used top-level view (Home/Editor/Envs/Map) and Home
        # sub-tab. Done *after* show_all(): Gtk.Stack silently ignores
        # set_visible_child_name for children that aren't shown yet, so restoring
        # before show_all leaves the stack on its first page (an empty Scripts
        # table under a highlighted Groups tab, etc.).
        self.home_view.restore_subtab()
        if self._saved_view in ("home", "detail", "envs", "graph"):
            self.outer_stack.set_visible_child_name(self._saved_view)

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
        import_item.connect("activate", self._config_actions.import_config)
        menu.append(import_item)
        export_item = Gtk.MenuItem(label="Export Scripts…")
        export_item.connect("activate", self._config_actions.export_config)
        menu.append(export_item)
        open_cfg_item = Gtk.MenuItem(label="Open Config File")
        open_cfg_item.connect("activate", self._config_actions.open_config_file)
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
        self.view_envs_btn = make_tab_button("En_vs", "envs", self._on_view_toggled)
        self.view_envs_btn.set_tooltip_text("Shared env vars reused across scripts")
        self.view_map_btn = make_tab_button("_Map", "graph", self._on_view_toggled)
        self.view_map_btn.set_tooltip_text("Show how scripts connect via ports referenced in env vars")
        view_switch.pack_start(self.view_home_btn, False, False, 0)
        view_switch.pack_start(self.view_editor_btn, False, False, 0)
        view_switch.pack_start(self.view_envs_btn, False, False, 0)
        view_switch.pack_start(self.view_map_btn, False, False, 0)
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
        order_bar.pack_start(make_icon_button("↑", "Move up", self._move_up), False, False, 0)
        order_bar.pack_start(make_icon_button("↓", "Move down", self._move_down), False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_start(4)
        sep.set_margin_end(4)
        order_bar.pack_start(sep, False, False, 0)

        order_bar.pack_start(make_icon_button("A→Z", _TIP_SORT_NAME_AZ, lambda _: self._sort_scripts("name_asc")), False, False, 0)
        order_bar.pack_start(make_icon_button("Z→A", _TIP_SORT_NAME_ZA, lambda _: self._sort_scripts("name_desc")), False, False, 0)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.set_margin_start(4)
        sep2.set_margin_end(4)
        order_bar.pack_start(sep2, False, False, 0)

        order_bar.pack_start(make_icon_button("P↑", "Sort by port 1→100", lambda _: self._sort_scripts("port_asc")), False, False, 0)
        order_bar.pack_start(make_icon_button("P↓", "Sort by port 100→1", lambda _: self._sort_scripts("port_desc")), False, False, 0)

        sep3 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep3.set_margin_start(4)
        sep3.set_margin_end(4)
        order_bar.pack_start(sep3, False, False, 0)

        order_bar.pack_start(make_icon_button("▶↑", _TIP_RUNNING_FIRST, lambda _: self._sort_scripts("running_first")), False, False, 0)
        order_bar.pack_start(make_icon_button("■↑", _TIP_STOPPED_FIRST, lambda _: self._sort_scripts("stopped_first")), False, False, 0)

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
        groups_order_bar.pack_start(make_icon_button("↑", "Move group up", self._move_group_up), False, False, 0)
        groups_order_bar.pack_start(make_icon_button("↓", "Move group down", self._move_group_down), False, False, 0)

        g_sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        g_sep.set_margin_start(4)
        g_sep.set_margin_end(4)
        groups_order_bar.pack_start(g_sep, False, False, 0)

        groups_order_bar.pack_start(make_icon_button("A→Z", _TIP_SORT_NAME_AZ, lambda _: self._sort_groups("name_asc")), False, False, 0)
        groups_order_bar.pack_start(make_icon_button("Z→A", _TIP_SORT_NAME_ZA, lambda _: self._sort_groups("name_desc")), False, False, 0)

        g_sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        g_sep2.set_margin_start(4)
        g_sep2.set_margin_end(4)
        groups_order_bar.pack_start(g_sep2, False, False, 0)

        groups_order_bar.pack_start(make_icon_button("S↑", "Fewer scripts first", lambda _: self._sort_groups("count_asc")), False, False, 0)
        groups_order_bar.pack_start(make_icon_button("S↓", "More scripts first", lambda _: self._sort_groups("count_desc")), False, False, 0)

        g_sep3 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        g_sep3.set_margin_start(4)
        g_sep3.set_margin_end(4)
        groups_order_bar.pack_start(g_sep3, False, False, 0)

        groups_order_bar.pack_start(make_icon_button("▶↑", _TIP_RUNNING_FIRST, lambda _: self._sort_groups("running_first")), False, False, 0)
        groups_order_bar.pack_start(make_icon_button("■↑", _TIP_STOPPED_FIRST, lambda _: self._sort_groups("stopped_first")), False, False, 0)

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

    def _build_global_editor(self):
        """Top-level Envs view — the global env key/value table."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_margin_start(20)
        inner.set_margin_end(20)
        inner.set_margin_top(16)
        inner.set_margin_bottom(20)

        header = Gtk.Label(label="GLOBAL ENVIRONMENT")
        header.set_halign(Gtk.Align.START)
        header.get_style_context().add_class("section-header")
        inner.pack_start(header, False, False, 0)

        blurb = Gtk.Label(
            label="Variables defined here can be reused by any script. In a "
                  "script's Envs tab, type a key listed here to reference it — "
                  "edit the value once and every script that uses it follows.")
        blurb.set_halign(Gtk.Align.START)
        blurb.set_xalign(0)
        blurb.set_line_wrap(True)
        blurb.set_margin_bottom(12)
        blurb.get_style_context().add_class("form-hint")
        inner.pack_start(blurb, False, False, 0)

        self.global_env_table = EnvVarsTable()
        self.global_env_table.set_hexpand(True)
        self.global_env_table.connect("changed", lambda _: self._save_global_env())
        inner.pack_start(self.global_env_table, False, False, 0)

        scroll.add(inner)
        box.pack_start(scroll, True, True, 0)
        return box

    def _save_global_env(self):
        """Persist the global env pool and make the change visible everywhere.

        Saves under the config lock, touches the config so the tray hot-reloads,
        and re-injects the pool into the open script form so its references and
        autocomplete reflect the new pool immediately.
        """
        with config_lock():
            cfg = load_config()
            cfg["global_env"] = self.global_env_table.get_env_vars()
            save_config(cfg)
        try:
            CONFIG_FILE.touch()
        except OSError:
            pass
        self.form.refresh_global_pool()

    def _toggle_log_search(self):
        if self.right_stack.get_visible_child_name() == "script":
            self.form.show_logs()
            self.form.log_panel.open_search()

    def _refresh_logs_tab(self) -> bool:
        # Check if error/running states changed — only then rebuild sidebar
        new_errors = get_error_states()
        new_running = get_running_ids()
        if self.form.logs_tab_active():
            self.form.log_panel.reload_log()
        else:
            self.form.log_panel.mark_pending()
        self.form.log_panel.update_error_banner(errors=new_errors)
        if new_errors != self._last_error_state or new_running != self._last_running_state:
            self._last_error_state = new_errors
            self._last_running_state = new_running
            self._refresh_running_badges()
            self.home_view.refresh_running(new_running)
            if self.outer_stack.get_visible_child_name() == "graph":
                self.graph_view.refresh_running(new_running)
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
            port_busy = _is_port_in_use(int(port_str))
        self.form.stop_btn.set_sensitive(is_running or port_busy)
        self.form.restart_btn.set_sensitive(is_running)
        if is_running:
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
            group_scripts = scripts_in_group(scripts, gid)
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
        group_scripts = scripts_in_group(scripts, gid)

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
        # A background rebuild (e.g. after stop/restart) re-selects the group to
        # keep its highlight in the list; that must not yank the editor away from
        # the script the user is viewing. Only follow the selection on the Groups
        # sidebar.
        if self._sidebar_mode != "groups":
            return
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
        self.form.show_settings()
        self.right_stack.set_visible_child_name("script")

    def _open_terminal(self, script):
        """Open a terminal in the script's working directory."""
        cwd = script.get("working_dir", str(Path.home()))
        cwd = str(Path(cwd).expanduser())
        subprocess.Popen(["x-terminal-emulator"], cwd=cwd)

    def _open_script_logs(self, script):
        """Navigate to a script's Logs tab from group view."""
        self.form.load_script(script)
        self.form.show_logs()
        self.right_stack.set_visible_child_name("script")

    def _open_script_envs(self, script):
        """Navigate to a script's Envs tab."""
        self.form.load_script(script)
        self.form.show_envs()
        self.right_stack.set_visible_child_name("script")

    def _run_group(self, group):
        cfg = load_config()
        gid = group["id"]
        group_scripts = scripts_in_group(cfg.get("scripts", []), gid)
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

    def _stop_scripts_threaded(self, scripts, on_done=None):
        """Stop scripts off the UI thread so the SIGTERM→SIGKILL grace period
        (and any port kill) doesn't freeze the window. Badges refresh once done.

        scripts: script dicts to stop (only those currently running are touched).
        on_done: optional no-arg callback run on the GTK thread afterwards.
        """
        running = get_running_ids()
        targets = [s for s in scripts if s.get("id", "") in running]
        if not targets:
            if on_done:
                GLib.idle_add(on_done)
            return

        def _work():
            for s in targets:
                stop_script(s.get("id", ""))
                port_str = s.get("port", "").strip()
                if port_str.isdigit():
                    kill_port(int(port_str))
            GLib.idle_add(self._refresh_running_badges)
            GLib.idle_add(self._rebuild_groups_view)
            if on_done:
                GLib.idle_add(on_done)

        threading.Thread(target=_work, daemon=True).start()

    def _stop_group(self, group):
        cfg = load_config()
        gid = group["id"]
        running = get_running_ids()
        targets = [s for s in cfg.get("scripts", [])
                   if gid in s.get("groups", []) and s.get("id", "") in running]
        self._stop_scripts_threaded(targets)
        self._show_toast(f"Stopping {len(targets)} script(s) from '{group['name']}'…")

    def _restart_group(self, group):
        cfg = load_config()
        gid = group["id"]
        running = get_running_ids()
        targets = [s for s in cfg.get("scripts", [])
                   if gid in s.get("groups", []) and s.get("id", "") in running]
        if not targets:
            return

        def _relaunch():
            for script in targets:
                run_script(script)
            GLib.timeout_add(500, lambda: self._refresh_running_badges() and False)
            GLib.timeout_add(600, lambda: self._rebuild_groups_view() or False)

        self._stop_scripts_threaded(targets, on_done=_relaunch)
        self._show_toast(f"Restarting {len(targets)} script(s) from '{group['name']}' ↻")

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
        with config_lock():
            cfg = load_config()
            cfg["groups"] = sort_groups(
                cfg.get("groups", []), mode,
                scripts=cfg.get("scripts", []),
                running_ids=get_running_ids(),
            )
            save_config(cfg)
        self._rebuild_groups_view()

    def _stop_single_script(self, script):
        self._stop_scripts_threaded([script])
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

    def _home_open_script(self, script_id, tab="settings"):
        """Open a script in the editor on the given tab
        ('settings'/'envs'/'logs') and switch to the detail view."""
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
        if tab == "logs":
            self.form.show_logs()
        elif tab == "envs":
            self.form.show_envs()
        else:
            self.form.show_settings()
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
        self.view_envs_btn.set_active(name == "envs")
        self.view_map_btn.set_active(name == "graph")
        self._switching_view = False
        save_ui_state(view=name)
        if name == "home":
            self.home_view.reload_active()
        elif name == "envs":
            self.global_env_table.set_env_vars(load_config().get("global_env"))
        elif name == "graph":
            self.graph_view.reload(load_config(), get_running_ids())
            self.graph_view.grab_canvas_focus()

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
            self.form.show_settings()
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
        run_script(script)
        # Refresh after a short delay so the process has time to register
        GLib.timeout_add(500, lambda: self._refresh_running_badges() and False)

    def _stop_script(self, script: dict):
        if not script:
            return
        self._stop_scripts_threaded([script])
        self._show_toast("Stopping script ■")

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
