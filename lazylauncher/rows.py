#!/usr/bin/env python3
"""rows.py — ScriptRow and GroupRow list widgets for the manager sidebar."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango

from .deps import resolve_order
from .sorting import sort_scripts
from .ui_shared import (
    _TIP_SORT_NAME_AZ, _TIP_SORT_NAME_ZA,
    _TIP_RUNNING_FIRST, _TIP_STOPPED_FIRST,
)


class ScriptRow(Gtk.ListBoxRow):
    _shared_error_states: dict = {}
    _shared_running_ids: set = set()
    _on_run = None
    _on_stop = None
    _on_restart = None
    _on_select_script_settings = None
    _on_select_script_logs = None
    _on_select_script_envs = None
    _on_open_terminal = None

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

        restart_btn = Gtk.Button()
        restart_btn.set_image(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.MENU))
        restart_btn.get_style_context().add_class("btn-icon")
        restart_btn.set_tooltip_text("Restart")
        restart_btn.set_sensitive(is_running)
        if ScriptRow._on_restart:
            restart_btn.connect("clicked", lambda _, s=self.script: ScriptRow._on_restart(s))

        run_btn = Gtk.Button()
        run_btn.set_image(Gtk.Image.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.MENU))
        run_btn.get_style_context().add_class("btn-icon")
        run_btn.get_style_context().add_class("btn-icon-run")
        if is_running:
            run_btn.get_style_context().add_class("running")
        run_btn.set_tooltip_text("Run")
        if ScriptRow._on_run:
            run_btn.connect("clicked", lambda _, s=self.script: ScriptRow._on_run(s))

        term_btn = Gtk.Button()
        term_btn.set_image(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic", Gtk.IconSize.MENU))
        term_btn.get_style_context().add_class("btn-icon")
        term_btn.set_tooltip_text("Open Terminal")
        if ScriptRow._on_open_terminal:
            term_btn.connect("clicked", lambda _, s=self.script: ScriptRow._on_open_terminal(s))

        logs_btn = Gtk.Button()
        logs_btn.set_image(Gtk.Image.new_from_icon_name("text-x-generic-symbolic", Gtk.IconSize.MENU))
        logs_btn.get_style_context().add_class("btn-icon")
        logs_btn.set_tooltip_text("Logs")
        if ScriptRow._on_select_script_logs:
            logs_btn.connect("clicked", lambda _, s=self.script: ScriptRow._on_select_script_logs(s))

        envs_btn = Gtk.Button()
        envs_btn.set_image(Gtk.Image.new_from_icon_name("dialog-password-symbolic", Gtk.IconSize.MENU))
        envs_btn.get_style_context().add_class("btn-icon")
        envs_btn.set_tooltip_text("Env Vars")
        if ScriptRow._on_select_script_envs:
            envs_btn.connect("clicked", lambda _, s=self.script: ScriptRow._on_select_script_envs(s))

        settings_btn = Gtk.Button()
        settings_btn.set_image(Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.MENU))
        settings_btn.get_style_context().add_class("btn-icon")
        settings_btn.set_tooltip_text("Settings")
        if ScriptRow._on_select_script_settings:
            settings_btn.connect("clicked", lambda _, s=self.script: ScriptRow._on_select_script_settings(s))

        self._action_box.pack_end(stop_btn, False, False, 0)
        self._action_box.pack_end(restart_btn, False, False, 0)
        self._action_box.pack_end(run_btn, False, False, 0)
        self._action_box.pack_end(term_btn, False, False, 0)
        self._action_box.pack_end(logs_btn, False, False, 0)
        self._action_box.pack_end(envs_btn, False, False, 0)
        self._action_box.pack_end(settings_btn, False, False, 0)

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

        deps = self.script.get("depends_on", [])
        if deps:
            badge = Gtk.Label(label=f"⛓ {len(deps)}")
            badge.get_style_context().add_class("badge-pinned")
            badge.set_tooltip_text(f"Depends on {len(deps)} script(s)")
            self._badge_box.pack_start(badge, False, False, 0)

        if not self.script.get("enabled", True):
            badge = Gtk.Label(label="OFF")
            badge.get_style_context().add_class("badge-disabled")
            self._badge_box.pack_start(badge, False, False, 0)

        self._action_box.show_all()
        self._badge_box.show_all()


class GroupRow(Gtk.ListBoxRow):
    """Sidebar row for a group – mirrors ScriptRow layout with script sub-rows."""
    _on_run_group = None
    _on_stop_group = None
    _on_restart_group = None
    _on_select_group_settings = None
    _on_run_script = None
    _on_stop_script = None
    _on_restart_script = None
    _on_select_script_settings = None
    _on_select_script_logs = None
    _on_select_script_envs = None
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
        elif self._scripts:
            # Default: show in dependency start order (falls back on a cycle).
            try:
                order = resolve_order(self._scripts)
                pos = {sid: i for i, sid in enumerate(order)}
                self._scripts = sorted(self._scripts, key=lambda s: pos.get(s.get("id", ""), 0))
            except ValueError:
                pass

        if self._scripts:
            names = {s.get("id", ""): s.get("name", s.get("id", "")) for s in self._scripts}
            for script in self._scripts:
                box.pack_start(self._build_script_row(script), False, False, 0)
                dep_ids = [d for d in script.get("depends_on", []) if d in names]
                if dep_ids:
                    dep_lbl = Gtk.Label(label="    ↳ depends on " + ", ".join(names[d] for d in dep_ids))
                    dep_lbl.set_halign(Gtk.Align.START)
                    dep_lbl.get_style_context().add_class("form-hint")
                    dep_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                    box.pack_start(dep_lbl, False, False, 0)
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

        s_restart = Gtk.Button()
        s_restart.set_image(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.MENU))
        s_restart.get_style_context().add_class("btn-icon")
        s_restart.set_tooltip_text("Restart")
        s_restart.set_sensitive(is_running)
        if GroupRow._on_restart_script:
            s_restart.connect("clicked", lambda _, s=script: GroupRow._on_restart_script(s))

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

        s_envs = Gtk.Button()
        s_envs.set_image(Gtk.Image.new_from_icon_name("dialog-password-symbolic", Gtk.IconSize.MENU))
        s_envs.get_style_context().add_class("btn-icon")
        s_envs.set_tooltip_text("Env Vars")
        if GroupRow._on_select_script_envs:
            s_envs.connect("clicked", lambda _, s=script: GroupRow._on_select_script_envs(s))

        s_settings = Gtk.Button()
        s_settings.set_image(Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.MENU))
        s_settings.get_style_context().add_class("btn-icon")
        s_settings.set_tooltip_text("Settings")
        if GroupRow._on_select_script_settings:
            s_settings.connect("clicked", lambda _, s=script: GroupRow._on_select_script_settings(s))

        row_box.pack_end(s_stop, False, False, 0)
        row_box.pack_end(s_restart, False, False, 0)
        row_box.pack_end(s_run, False, False, 0)
        row_box.pack_end(s_term, False, False, 0)
        row_box.pack_end(s_logs, False, False, 0)
        row_box.pack_end(s_envs, False, False, 0)
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

        restart_btn = Gtk.Button()
        restart_btn.set_image(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.MENU))
        restart_btn.get_style_context().add_class("btn-icon")
        restart_btn.set_tooltip_text("Restart All")
        restart_btn.set_sensitive(any_running)
        if GroupRow._on_restart_group:
            restart_btn.connect("clicked", lambda _, g=self.group: GroupRow._on_restart_group(g))

        stop_btn = Gtk.Button()
        stop_btn.set_image(Gtk.Image.new_from_icon_name("media-playback-stop-symbolic", Gtk.IconSize.MENU))
        stop_btn.get_style_context().add_class("btn-icon")
        stop_btn.set_tooltip_text("Stop All")
        stop_btn.set_sensitive(any_running)
        if GroupRow._on_stop_group:
            stop_btn.connect("clicked", lambda _, g=self.group: GroupRow._on_stop_group(g))

        settings_btn = Gtk.Button()
        settings_btn.set_image(Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.MENU))
        settings_btn.get_style_context().add_class("btn-icon")
        settings_btn.set_tooltip_text("Group Settings")
        if GroupRow._on_select_group_settings:
            settings_btn.connect("clicked", lambda _, g=self.group: GroupRow._on_select_group_settings(g))

        self._action_box.pack_start(settings_btn, False, False, 0)
        self._action_box.pack_start(run_btn, False, False, 0)
        self._action_box.pack_start(restart_btn, False, False, 0)
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
        return sort_scripts(scripts, mode, GroupRow._shared_running_ids)
