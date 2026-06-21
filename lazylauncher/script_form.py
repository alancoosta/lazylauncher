#!/usr/bin/env python3
"""script_form.py — ScriptForm: the per-script editor panel (Settings/Logs/Envs tabs)."""
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .common import load_config
from .env_table import EnvVarsTable
from .log_panel import LogPanel
from .ui_shared import _STOP_LABEL


class ScriptForm(Gtk.Box):
    """Right-hand panel - shows when a script is selected."""

    def __init__(self, on_save, on_delete, on_run, on_duplicate=None, on_stop=None, on_restart=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_name("form-panel")
        self._on_save   = on_save
        self._on_delete = on_delete
        self._on_run    = on_run
        self._on_duplicate = on_duplicate
        self._on_stop   = on_stop
        self._on_restart = on_restart
        self._script    = None
        self._loading   = False
        self._group_checkboxes = {}
        self._dep_checkboxes = {}
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

        self.restart_btn = Gtk.Button(label="↻  Restart")
        self.restart_btn.get_style_context().add_class("btn-warning")
        self.restart_btn.connect("clicked", lambda _: self._on_restart and self._on_restart(self._script))
        self.restart_btn.set_sensitive(False)
        btn_box.pack_start(self.restart_btn, False, False, 0)

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
        self.login_shell_switch = Gtk.Switch()

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
        options_grid.attach(
            _option_cell("Login shell", "Source ~/.profile & ~/.bashrc (PATH, nvm…)", self.login_shell_switch),
            0, 2, 1, 1)

        inner.pack_start(options_grid, False, False, 0)

        # -- Port --
        spacer = Gtk.Box(); spacer.set_size_request(-1, 12)
        inner.pack_start(spacer, False, False, 0)

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
        port_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        port_box.pack_start(self.port_entry, False, False, 0)
        inner.pack_start(port_header, False, False, 0)
        inner.pack_start(port_box, False, False, 0)

        # -- Groups --
        spacer = Gtk.Box(); spacer.set_size_request(-1, 12)
        inner.pack_start(spacer, False, False, 0)
        section("GROUPS")

        self._groups_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.pack_start(self._groups_box, False, False, 0)

        # -- Depends on --
        spacer = Gtk.Box(); spacer.set_size_request(-1, 12)
        inner.pack_start(spacer, False, False, 0)
        dep_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dep_lbl = Gtk.Label(label="DEPENDS ON")
        dep_lbl.set_halign(Gtk.Align.START)
        dep_lbl.get_style_context().add_class("section-header")
        dep_hint = Gtk.Label(label="Start after these (waits on their port)")
        dep_hint.set_halign(Gtk.Align.START)
        dep_hint.get_style_context().add_class("form-hint")
        dep_header.pack_start(dep_lbl, False, False, 0)
        dep_header.pack_start(dep_hint, False, False, 0)
        inner.pack_start(dep_header, False, False, 0)

        self._deps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.pack_start(self._deps_box, False, False, 0)

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
        self.log_panel = LogPanel()
        self.notebook.append_page(self.log_panel, Gtk.Label(label="Logs"))

        # ── Envs tab ──
        envs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        env_scroll = Gtk.ScrolledWindow()
        env_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        env_scroll.set_hexpand(True)
        env_scroll.set_vexpand(True)

        env_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        env_inner.set_margin_start(20)
        env_inner.set_margin_end(20)
        env_inner.set_margin_top(16)
        env_inner.set_margin_bottom(20)

        env_section = Gtk.Label(label="ENVIRONMENT")
        env_section.set_halign(Gtk.Align.START)
        env_section.get_style_context().add_class("section-header")
        env_inner.pack_start(env_section, False, False, 0)

        env_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        env_lbl = Gtk.Label(label="ENV VARS")
        env_lbl.set_halign(Gtk.Align.START)
        env_lbl.get_style_context().add_class("form-label")
        env_hint = Gtk.Label(label="One KEY / value pair per row")
        env_hint.set_halign(Gtk.Align.START)
        env_hint.get_style_context().add_class("form-hint")
        env_header.pack_start(env_lbl, False, False, 0)
        env_header.pack_start(env_hint, False, False, 0)
        self.env_entry = EnvVarsTable()
        self.env_entry.set_hexpand(True)
        env_inner.pack_start(env_header, False, False, 0)
        env_spacer = Gtk.Box(); env_spacer.set_size_request(-1, 8)
        env_inner.pack_start(env_spacer, False, False, 0)
        env_inner.pack_start(self.env_entry, False, False, 0)

        env_scroll.add(env_inner)
        envs_box.pack_start(env_scroll, True, True, 0)
        self.notebook.append_page(envs_box, Gtk.Label(label="Envs"))

        self.notebook.connect("switch-page", self._on_tab_switched)

        # Auto-save on any field change
        self.name_entry.connect("changed", lambda _: self._auto_save())
        self.desc_entry.connect("changed", lambda _: self._auto_save())
        self.cmd_entry.connect("changed", lambda _: self._auto_save())
        self.wd_entry.connect("changed", lambda _: self._auto_save())
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
        self.pin_switch.set_active(script.get("pinned_icon", False))
        self.enabled_switch.set_active(script.get("enabled", True))
        self.env_entry.set_env_vars(script.get("env_vars", ""))
        self.port_entry.set_text(script.get("port", ""))
        self.confirm_switch.set_active(script.get("confirm", False))
        self.silent_switch.set_active(script.get("silent", False))
        self.login_shell_switch.set_active(script.get("login_shell", True))
        self.run_btn.set_sensitive(bool(script.get("command", "").strip()))
        self.set_sensitive(True)
        self._loading = False
        self.log_panel.set_script(script)
        if self.notebook.get_current_page() == 1:
            self.log_panel.reload_if_pending()
        self.log_panel.update_error_banner()
        self._rebuild_group_checkboxes()
        self._rebuild_dep_checkboxes()

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

    def _rebuild_dep_checkboxes(self):
        """List other scripts this one can depend on (multi-select).

        Dependencies only take effect within a shared group at run time
        (see deps.resolve_order), and a dependency is only waitable when it
        has a port configured — that's flagged inline.
        """
        for child in self._deps_box.get_children():
            self._deps_box.remove(child)
        self._dep_checkboxes.clear()
        if not self._script:
            return
        my_id = self._script.get("id", "")
        cfg = load_config()
        others = [s for s in cfg.get("scripts", []) if s.get("id") and s.get("id") != my_id]
        if not others:
            empty = Gtk.Label(label="No other scripts yet", xalign=0)
            empty.get_style_context().add_class("form-hint")
            self._deps_box.pack_start(empty, False, False, 0)
            self._deps_box.show_all()
            return
        for s in others:
            sid = s["id"]
            has_port = str(s.get("port", "")).strip().isdigit()
            suffix = "" if has_port else "   (no port — can't wait)"
            cb = Gtk.CheckButton(label=f"{s.get('name', sid)}{suffix}")
            cb.get_style_context().add_class("group-check")
            cb.connect("toggled", lambda *_: self._auto_save())
            self._dep_checkboxes[sid] = cb
            self._deps_box.pack_start(cb, False, False, 0)
        # Restore selection from current script
        depends_on = self._script.get("depends_on", [])
        self._loading = True
        for sid, cb in self._dep_checkboxes.items():
            cb.set_active(sid in depends_on)
        self._loading = False
        self._deps_box.show_all()
        self._groups_box.show_all()

    def _auto_save(self):
        if self._loading or not self._script:
            return
        self._save()

    def _on_tab_switched(self, notebook, page, page_num):
        if page_num == 1:
            self.log_panel.reload_if_pending()

    def _save(self, _widget=None):
        if not self._script:
            return
        self._script["name"]        = self.name_entry.get_text().strip() or self._script.get("name", "New Script")
        self._script["description"] = self.desc_entry.get_text().strip()
        self._script["command"]     = self.cmd_entry.get_text().strip()
        self._script["working_dir"] = self.wd_entry.get_text().strip() or str(Path.home())
        self._script["pinned_icon"] = self.pin_switch.get_active()
        self._script["enabled"]     = self.enabled_switch.get_active()
        self._script["env_vars"]    = self.env_entry.get_env_vars()
        self._script["port"]        = self.port_entry.get_text().strip()
        self._script["confirm"]     = self.confirm_switch.get_active()
        self._script["silent"]      = self.silent_switch.get_active()
        self._script["login_shell"] = self.login_shell_switch.get_active()
        self._script["depends_on"]  = [sid for sid, cb in self._dep_checkboxes.items() if cb.get_active()]
        self._script["groups"]      = [gid for gid, cb in self._group_checkboxes.items() if cb.get_active()]
        self._script.pop("icon", None)  # custom-icon feature removed; drop stale data
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
            "env_vars":    self.env_entry.get_env_vars(),
            "confirm":     self.confirm_switch.get_active(),
            "silent":      self.silent_switch.get_active(),
            "login_shell": self.login_shell_switch.get_active(),
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
