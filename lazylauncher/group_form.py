#!/usr/bin/env python3
"""group_form.py — GroupForm: the per-group editor panel."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango

from .common import config_lock, load_config, save_config, get_running_ids


class GroupForm(Gtk.Box):
    """Right-hand panel — shows when a group is selected."""

    def __init__(self, on_save, on_delete, on_run_all, on_stop_all, on_scripts_changed=None, on_duplicate=None, on_restart_all=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_name("form-panel")
        self._on_save = on_save
        self._on_delete = on_delete
        self._on_run_all = on_run_all
        self._on_stop_all = on_stop_all
        self._on_restart_all = on_restart_all
        self._on_scripts_changed = on_scripts_changed
        self._on_duplicate = on_duplicate
        self._group = None
        self._loading = False
        self._script_checkboxes = {}
        self._build()

    def _build(self):
        # ── Title bar: which group is being edited ──
        self.title_label = Gtk.Label(label="")
        self.title_label.set_name("form-title")
        self.title_label.set_halign(Gtk.Align.START)
        self.title_label.set_xalign(0.0)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_max_width_chars(60)
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title_box.pack_start(self.title_label, True, True, 0)
        self.pack_start(title_box, False, False, 0)

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

        self.restart_btn = Gtk.Button(label="↻  Restart All")
        self.restart_btn.get_style_context().add_class("btn-warning")
        self.restart_btn.connect("clicked", lambda _: self._on_restart_all and self._on_restart_all(self._group))
        self.restart_btn.set_sensitive(False)
        btn_box.pack_start(self.restart_btn, False, False, 0)

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
        self.name_entry.connect("changed", lambda _: self._update_title())
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
        self._update_title()

    def clear(self):
        self._group = None
        self.set_sensitive(False)
        self.title_label.set_text("")

    def _update_title(self):
        name = ""
        if self._group:
            name = self.name_entry.get_text().strip() or self._group.get("name", "")
        self.title_label.set_text(name)

    def _rebuild_script_checkboxes(self):
        for child in self._scripts_box.get_children():
            self._scripts_box.remove(child)
        self._script_checkboxes.clear()
        cfg = load_config()
        gid = self._group["id"] if self._group else ""
        self._loading = True
        scripts = sorted(cfg.get("scripts", []),
                         key=lambda s: s.get("name", "Unnamed").lower())
        for s in scripts:
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
        with config_lock():
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
        self.restart_btn.set_sensitive(any_running)
        self.run_btn.set_sensitive(len(group_scripts) > 0)
