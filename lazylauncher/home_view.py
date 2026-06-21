#!/usr/bin/env python3
"""home_view.py — HomeView: the at-a-glance Home tables (scripts + groups).

Two TreeView tables — a flat scripts table and a group->scripts tree — with
inline cell editing and per-row action icons. HomeView owns only the table
rendering, filtering and edit validation; navigation (opening the editor) and
persistence are delegated to the manager via the callbacks passed to __init__.
"""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, Pango

from .common import load_config, get_running_ids, load_ui_state, save_ui_state
from .sorting import port_sort_key
from .ui_shared import make_tab_button

# The running play icon is tinted with the same green the sidebar uses.
_RUNNING_GREEN = "#27ae60"


class HomeView(Gtk.Box):
    # ListStore column indices: id is hidden, the rest map to script fields.
    _HOME_COL_ID, _HOME_COL_NAME, _HOME_COL_PORT, _HOME_COL_CMD, _HOME_COL_WD = range(5)
    _HOME_FIELDS = {
        _HOME_COL_NAME: "name",
        _HOME_COL_PORT: "port",
        _HOME_COL_CMD: "command",
        _HOME_COL_WD: "working_dir",
    }

    # Group table is a tree: each group row holds its scripts as child rows.
    (_HOME_G_COL_ID, _HOME_G_COL_KIND, _HOME_G_COL_NAME,
     _HOME_G_COL_PORT, _HOME_G_COL_CMD, _HOME_G_COL_WD) = range(6)
    _HOME_G_FIELDS = {
        _HOME_G_COL_NAME: "name",
        _HOME_G_COL_PORT: "port",
        _HOME_G_COL_CMD: "command",
        _HOME_G_COL_WD: "working_dir",
    }

    # Per-row action icons shared by both home tables (title, icon, key). Icons
    # mirror the sidebar's button set so the two views stay recognisable.
    _HOME_ACTIONS = [
        ("Run", "media-playback-start-symbolic", "run"),
        ("Stop", "media-playback-stop-symbolic", "stop"),
        ("Restart", "view-refresh-symbolic", "restart"),
        ("Terminal", "utilities-terminal-symbolic", "terminal"),
        ("Settings", "emblem-system-symbolic", "settings"),
        ("Logs", "text-x-generic-symbolic", "logs"),
        ("Env", "dialog-password-symbolic", "envs"),
    ]

    def __init__(self, *, on_open_script, on_open_group, on_new_script,
                 on_new_group, on_run_script, on_stop_script, on_run_group,
                 on_stop_group, on_edit_script, on_edit_group_name,
                 on_restart_script, on_terminal_script, on_restart_group,
                 on_add_script_to_group):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.get_style_context().add_class("home-table")
        self._on_open_script = on_open_script
        self._on_open_group = on_open_group
        self._on_new_script = on_new_script
        self._on_new_group = on_new_group
        self._on_run_script = on_run_script
        self._on_stop_script = on_stop_script
        self._on_run_group = on_run_group
        self._on_stop_group = on_stop_group
        self._on_edit_script = on_edit_script
        self._on_edit_group_name = on_edit_group_name
        self._on_restart_script = on_restart_script
        self._on_terminal_script = on_terminal_script
        self._on_restart_group = on_restart_group
        self._on_add_script_to_group = on_add_script_to_group
        # Ids of currently-running scripts; drives the green run icon. Refreshed
        # by reload_*/refresh_running.
        self._running_ids = set()
        self._green_run_pix = False  # lazy-built cache (None once a build fails)

        # Sub-tabs: Scripts | Groups (same pattern as the editor sidebar)
        self._home_mode = "scripts"
        self._switching_home_tab = False
        tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tab_bar.set_name("group-tabs")
        tab_bar.set_margin_start(8)
        tab_bar.set_margin_end(8)
        tab_bar.set_margin_top(8)
        self.home_scripts_tab = make_tab_button(
            "Scripts", "scripts", self._on_home_tab_toggled, active=True)
        self.home_groups_tab = make_tab_button(
            "Groups", "groups", self._on_home_tab_toggled)
        tab_bar.pack_start(self.home_scripts_tab, True, True, 0)
        tab_bar.pack_start(self.home_groups_tab, True, True, 0)
        self.pack_start(tab_bar, False, False, 0)

        self.home_inner_stack = Gtk.Stack()
        self.home_inner_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.home_inner_stack.set_transition_duration(150)
        self.home_inner_stack.add_named(self._build_scripts_page(), "scripts")
        self.home_inner_stack.add_named(self._build_groups_page(), "groups")
        self.pack_start(self.home_inner_stack, True, True, 0)

        # Restore the last-used Home sub-tab (Scripts/Groups); toggling the
        # button drives _on_home_tab_toggled which switches the stack.
        if load_ui_state().get("home_tab") == "groups":
            self.home_groups_tab.set_active(True)

    # -- toolbar / column helpers ------------------------------------------------

    def _build_home_toolbar(self, new_label, new_cb, placeholder, search_cb):
        """Build the shared '+ New …' button + filter entry toolbar.
        Returns (toolbar, search_entry)."""
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_name("list-toolbar")
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(6)

        new_btn = Gtk.Button(label=new_label)
        new_btn.get_style_context().add_class("btn-primary")
        new_btn.connect("clicked", new_cb)
        toolbar.pack_start(new_btn, False, False, 0)

        search = Gtk.SearchEntry()
        search.get_style_context().add_class("form-entry")
        search.set_placeholder_text(placeholder)
        search.set_hexpand(True)
        search.connect("search-changed", search_cb)
        toolbar.pack_start(search, True, True, 0)
        return toolbar, search

    def _add_text_columns(self, tree, columns, on_edited):
        """Append editable, ellipsized text columns from (title, col, width,
        expand) specs to a home table; wires sorting and the edit callback."""
        for title, col, width, expand in columns:
            renderer = Gtk.CellRendererText()
            renderer.set_property("editable", True)
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            renderer.set_padding(6, 1)
            renderer.connect("edited", on_edited, col)
            column = Gtk.TreeViewColumn(title, renderer, text=col)
            column.set_resizable(True)
            column.set_sort_column_id(col)
            column.set_expand(expand)
            column.set_min_width(width if not expand else 160)
            if not expand:
                column.set_fixed_width(width)
            tree.append_column(column)

    def _append_action_column(self, tree, title, icon_name):
        """Add a fixed-width clickable icon column to a home table. Returns
        (column, renderer) so callers can attach a cell-data func."""
        renderer = Gtk.CellRendererPixbuf()
        renderer.set_property("icon-name", icon_name)
        renderer.set_alignment(0.5, 0.5)
        renderer.set_padding(6, 1)
        column = Gtk.TreeViewColumn(title, renderer)
        column.set_alignment(0.5)
        column.set_resizable(False)
        column.set_min_width(56)
        column.set_fixed_width(64)
        tree.append_column(column)
        return column, renderer

    def _add_action_columns(self, tree, on_click, is_running):
        """Append the shared per-row action icon columns to a home table and
        wire its button-press handler. ``is_running(model, iter)`` decides when a
        row's run icon turns green. Returns the {column: key} map."""
        action_columns = {}
        for title, icon, key in self._HOME_ACTIONS:
            column, renderer = self._append_action_column(tree, title, icon)
            action_columns[column] = key
            if key == "run":
                column.set_cell_data_func(renderer, self._run_cell_data, is_running)
        tree.connect("button-press-event", on_click)
        return action_columns

    # -- running indicator (green run icon) --------------------------------------

    def _green_run_pixbuf(self):
        """Lazily build (and cache) the green-tinted play icon; None on failure."""
        if self._green_run_pix is not False:
            return self._green_run_pix
        self._green_run_pix = None
        info = Gtk.IconTheme.get_default().lookup_icon(
            "media-playback-start-symbolic", 16, 0)
        if info is not None:
            color = Gdk.RGBA()
            color.parse(_RUNNING_GREEN)
            try:
                self._green_run_pix, _ = info.load_symbolic(color, None, None, None)
            except Exception:
                self._green_run_pix = None
        return self._green_run_pix

    def _run_cell_data(self, _column, cell, model, it, is_running):
        """Paint the run icon green for running rows, plain otherwise."""
        if is_running(model, it):
            cell.set_property("pixbuf", self._green_run_pixbuf())
        else:
            cell.set_property("pixbuf", None)
            cell.set_property("icon-name", "media-playback-start-symbolic")

    def _script_row_running(self, model, it):
        return model[it][self._HOME_COL_ID] in self._running_ids

    def _group_row_running(self, model, it):
        """A script row is running by id; a group row is 'running' if any of its
        member scripts are (matching the sidebar's group run badge)."""
        row = model[it]
        if row[self._HOME_G_COL_KIND] == "script":
            return row[self._HOME_G_COL_ID] in self._running_ids
        child = model.iter_children(it)
        while child is not None:
            if model[child][self._HOME_G_COL_ID] in self._running_ids:
                return True
            child = model.iter_next(child)
        return False

    def _group_add_cell_data(self, _column, cell, model, it, _data):
        """The 'add existing script' icon only makes sense on group rows."""
        is_group = model[it][self._HOME_G_COL_KIND] == "group"
        cell.set_property("icon-name", "list-add-symbolic" if is_group else None)

    def refresh_running(self, running_ids):
        """Update the cached running set and repaint the run icons in place
        (no model rebuild, so selection/scroll are preserved)."""
        self._running_ids = set(running_ids)
        for tree_attr in ("home_tree", "home_groups_tree"):
            tree = getattr(self, tree_attr, None)
            if tree is not None:
                tree.queue_draw()

    def _set_port_sort(self, store, col):
        """Sort a home table's port column numerically (blank/non-numeric = 0)."""
        store.set_sort_func(
            col,
            lambda m, a, b, _: port_sort_key(m[a][col]) - port_sort_key(m[b][col]),
        )

    # -- page builders -----------------------------------------------------------

    def _build_scripts_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        toolbar, self.home_search_entry = self._build_home_toolbar(
            "+ New Script", self._on_new_script, "Filter scripts…", self.reload_table)
        page.pack_start(toolbar, False, False, 0)

        self.home_store = Gtk.ListStore(str, str, str, str, str)
        self._set_port_sort(self.home_store, self._HOME_COL_PORT)

        self.home_tree = Gtk.TreeView(model=self.home_store)
        self.home_tree.set_enable_search(False)
        self._add_text_columns(self.home_tree, [
            ("Name", self._HOME_COL_NAME, 220, False),
            ("Port", self._HOME_COL_PORT, 70, False),
            ("Command", self._HOME_COL_CMD, 320, True),
            ("Working directory", self._HOME_COL_WD, 280, True),
        ], self._home_cell_edited)
        # Per-row action icons — discoverable access to a script's config.
        self._home_action_columns = self._add_action_columns(
            self.home_tree, self._home_tree_button_press, self._script_row_running)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.add(self.home_tree)
        page.pack_start(scroll, True, True, 0)
        return page

    def _build_groups_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        toolbar, self.home_groups_search_entry = self._build_home_toolbar(
            "+ New Group", lambda _: self._on_new_group(),
            "Filter groups…", self.reload_groups)
        page.pack_start(toolbar, False, False, 0)

        # TreeStore: group parent rows + script child rows, mirroring the
        # scripts table columns (id, kind, name, port, command, working dir).
        self.home_groups_store = Gtk.TreeStore(str, str, str, str, str, str)
        self._set_port_sort(self.home_groups_store, self._HOME_G_COL_PORT)

        self.home_groups_tree = Gtk.TreeView(model=self.home_groups_store)
        self.home_groups_tree.set_enable_search(False)
        self.home_groups_tree.set_enable_tree_lines(True)
        self._add_text_columns(self.home_groups_tree, [
            ("Name", self._HOME_G_COL_NAME, 300, False),
            ("Port", self._HOME_G_COL_PORT, 70, False),
            ("Command", self._HOME_G_COL_CMD, 320, True),
            ("Working directory", self._HOME_G_COL_WD, 280, True),
        ], self._home_group_cell_edited)
        # Same action set as the scripts table; on group rows run/stop/restart/
        # settings act on the whole group while terminal/logs/env are no-ops.
        self._home_group_action_columns = self._add_action_columns(
            self.home_groups_tree, self._home_groups_tree_button_press,
            self._group_row_running)
        # Extra group-only column: add an already-created script to the group.
        add_col, add_renderer = self._append_action_column(
            self.home_groups_tree, "Add", "list-add-symbolic")
        add_col.set_cell_data_func(add_renderer, self._group_add_cell_data, None)
        self._home_group_add_column = add_col

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.add(self.home_groups_tree)
        page.pack_start(scroll, True, True, 0)
        return page

    def _on_home_tab_toggled(self, button, mode):
        if self._switching_home_tab or not button.get_active():
            return
        self._switching_home_tab = True
        self.home_scripts_tab.set_active(mode == "scripts")
        self.home_groups_tab.set_active(mode == "groups")
        self._switching_home_tab = False
        self._home_mode = mode
        self.home_inner_stack.set_visible_child_name(mode)
        save_ui_state(home_tab=mode)
        if mode == "groups":
            self.reload_groups()
        else:
            self.reload_table()

    # -- validation / filtering --------------------------------------------------

    @staticmethod
    def _is_valid_port(value):
        """A port field edit is accepted only if empty or all digits."""
        return not value or value.isdigit()

    @staticmethod
    def _query_matches_script(query, script):
        """True if a lowercased filter query matches a script's name or command."""
        return query in (script.get("name", "") + " " + script.get("command", "")).lower()

    def _query_matches_group(self, query, group, members):
        """True if the query matches a group's name/description or any member script."""
        hay = (group.get("name", "") + " " + group.get("description", "")).lower()
        return query in hay or any(self._query_matches_script(query, s) for s in members)

    # -- reload (public: called by the manager after CRUD elsewhere) -------------

    def reload_active(self, *_):
        """Reload only the currently visible home sub-table."""
        if self._home_mode == "groups":
            self.reload_groups()
        else:
            self.reload_table()

    def reload_table(self, *_):
        if not hasattr(self, "home_store"):
            return
        self._running_ids = get_running_ids()
        query = self.home_search_entry.get_text().lower()
        self.home_store.clear()
        for s in load_config().get("scripts", []):
            if query and not self._query_matches_script(query, s):
                continue
            self.home_store.append([
                s.get("id", ""),
                s.get("name", ""),
                s.get("port", ""),
                s.get("command", ""),
                s.get("working_dir", ""),
            ])

    def reload_groups(self, *_):
        if not hasattr(self, "home_groups_store"):
            return
        self._running_ids = get_running_ids()
        query = self.home_groups_search_entry.get_text().lower()
        cfg = load_config()
        scripts = cfg.get("scripts", [])
        self.home_groups_store.clear()
        for g in cfg.get("groups", []):
            gid = g.get("id", "")
            members = [s for s in scripts if gid in s.get("groups", [])]
            if query and not self._query_matches_group(query, g, members):
                continue
            parent = self.home_groups_store.append(None, [
                gid, "group", g.get("name", ""), "", "", "",
            ])
            for s in members:
                self.home_groups_store.append(parent, [
                    s.get("id", ""), "script", s.get("name", ""),
                    s.get("port", ""), s.get("command", ""), s.get("working_dir", ""),
                ])
        self.home_groups_tree.expand_all()

    # -- inline cell edits (persistence delegated to the manager) ----------------

    def _home_cell_edited(self, _renderer, path, new_text, col):
        field = self._HOME_FIELDS.get(col)
        if field is None:
            return
        new_text = new_text.strip()
        if field == "port" and not self._is_valid_port(new_text):
            return  # ports are numeric; ignore invalid edits
        script_id = self.home_store[path][self._HOME_COL_ID]
        # The manager persists the edit and reloads the table from config (which
        # repaints this cell), so there's no need to poke the row model here.
        self._on_edit_script(script_id, field, new_text)

    def _home_group_cell_edited(self, _renderer, path, new_text, col):
        new_text = new_text.strip()
        row = self.home_groups_store[path]
        obj_id = row[self._HOME_G_COL_ID]
        if row[self._HOME_G_COL_KIND] == "group":
            # Group rows only carry a name; ignore edits on the script columns.
            if col != self._HOME_G_COL_NAME or not new_text:
                return
            self._on_edit_group_name(obj_id, new_text)
        else:  # script child row — same fields as the scripts table
            field = self._HOME_G_FIELDS[col]
            if field == "name" and not new_text:
                return
            if field == "port" and not self._is_valid_port(new_text):
                return  # ports are numeric; ignore invalid edits
            self._on_edit_script(obj_id, field, new_text)

    # -- per-row action icons ----------------------------------------------------

    @staticmethod
    def _action_column_hit(tree, event, action_columns):
        """Hit-test a left-click on an icon column. Returns (key, path) for the
        clicked action, or (None, None) when the click isn't on one."""
        if event.button != 1:
            return None, None
        info = tree.get_path_at_pos(int(event.x), int(event.y))
        if not info:
            return None, None
        path, column, _, _ = info
        return action_columns.get(column), path

    def _home_tree_button_press(self, tree, event):
        key, path = self._action_column_hit(tree, event, self._home_action_columns)
        if not key:
            return False
        self._home_script_action(key, self.home_store[path][self._HOME_COL_ID])
        return True

    def _home_groups_tree_button_press(self, tree, event):
        if event.button != 1:
            return False
        info = tree.get_path_at_pos(int(event.x), int(event.y))
        if not info:
            return False
        path, column, _, _ = info
        row = self.home_groups_store[path]
        # The 'add existing script' affordance lives only on group rows.
        if column is self._home_group_add_column:
            if row[self._HOME_G_COL_KIND] == "group":
                self._popup_add_script_menu(row[self._HOME_G_COL_ID], event)
            return True
        key = self._home_group_action_columns.get(column)
        if not key:
            return False
        obj_id = row[self._HOME_G_COL_ID]
        if row[self._HOME_G_COL_KIND] == "script":
            self._home_script_action(key, obj_id)
        else:
            self._home_group_action(key, obj_id)
        return True

    def _popup_add_script_menu(self, group_id, event):
        """Pop up a menu of scripts not yet in the group; picking one adds it."""
        addable = [s for s in load_config().get("scripts", [])
                   if group_id not in s.get("groups", [])]
        menu = Gtk.Menu()
        if not addable:
            item = Gtk.MenuItem(label="All scripts already in this group")
            item.set_sensitive(False)
            menu.append(item)
        else:
            for s in addable:
                item = Gtk.MenuItem(label=s.get("name", "") or "(unnamed)")
                item.connect(
                    "activate",
                    lambda _i, sid=s.get("id", ""): self._on_add_script_to_group(sid, group_id))
                menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _home_script_action(self, key, script_id):
        direct = {
            "run": self._on_run_script,
            "stop": self._on_stop_script,
            "restart": self._on_restart_script,
            "terminal": self._on_terminal_script,
        }
        if key in direct:
            direct[key](script_id)
        else:
            self._on_open_script(script_id, {"settings": 0, "logs": 1, "envs": 2}[key])

    def _home_group_action(self, key, group_id):
        # run/stop/restart/settings act on the whole group; terminal/logs/env
        # are n/a for groups.
        if key == "settings":
            self._on_open_group(group_id)
        elif key == "run":
            self._on_run_group(group_id)
        elif key == "stop":
            self._on_stop_group(group_id)
        elif key == "restart":
            self._on_restart_group(group_id)
