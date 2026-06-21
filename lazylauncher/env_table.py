#!/usr/bin/env python3
"""env_table.py — EnvVarsTable: key/value editor widget for env_vars."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GObject

from .common import normalize_env_vars


class EnvVarsTable(Gtk.Box):
    """Key/value table for environment variables.

    Reads / writes the ``env_vars`` field as a list of ``{"key", "value"}``
    dicts (``get_env_vars`` / ``set_env_vars``); ``set_env_vars`` also accepts
    the legacy space-separated string. Emits ``changed`` on any edit.
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._rows = []          # list of (row_box, key_entry, val_entry)
        self._suppress = False   # silence ``changed`` while loading

        self._rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.pack_start(self._rows_box, False, False, 0)

        add_btn = Gtk.Button(label="+ Add variable")
        add_btn.get_style_context().add_class("btn-secondary")
        add_btn.set_halign(Gtk.Align.START)
        add_btn.connect("clicked", lambda _: self._add_row(focus=True))
        self.pack_start(add_btn, False, False, 0)

    def _add_row(self, key="", value="", focus=False):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        key_entry = Gtk.Entry()
        key_entry.get_style_context().add_class("form-entry")
        key_entry.set_placeholder_text("KEY")
        key_entry.set_width_chars(28)
        key_entry.set_text(key)

        val_entry = Gtk.Entry()
        val_entry.get_style_context().add_class("form-entry")
        val_entry.set_placeholder_text("value")
        val_entry.set_hexpand(True)
        val_entry.set_text(value)

        remove_btn = Gtk.Button(label="✕")
        remove_btn.get_style_context().add_class("btn-icon")
        remove_btn.set_valign(Gtk.Align.CENTER)

        entry = (row, key_entry, val_entry)
        remove_btn.connect("clicked", lambda _: self._remove_row(entry))
        key_entry.connect("changed", lambda _: self._emit_changed())
        val_entry.connect("changed", lambda _: self._emit_changed())

        row.pack_start(key_entry, False, False, 0)
        row.pack_start(val_entry, True, True, 0)
        row.pack_start(remove_btn, False, False, 0)
        self._rows_box.pack_start(row, False, False, 0)
        self._rows.append(entry)
        row.show_all()

        if focus:
            key_entry.grab_focus()
        self._emit_changed()

    def _remove_row(self, entry):
        if entry not in self._rows:
            return
        row = entry[0]
        self._rows_box.remove(row)
        self._rows.remove(entry)
        self._emit_changed()

    def _emit_changed(self):
        if not self._suppress:
            self.emit("changed")

    def get_env_vars(self):
        """Return the table as a list of ``{"key", "value"}`` dicts, skipping
        rows with an empty key."""
        result = []
        for _, key_entry, val_entry in self._rows:
            key = key_entry.get_text().strip()
            if key:
                result.append({"key": key, "value": val_entry.get_text()})
        return result

    def set_env_vars(self, raw):
        """Populate rows from a list of dicts or a legacy KEY=VALUE string."""
        self._suppress = True
        for row, _, _ in self._rows:
            self._rows_box.remove(row)
        self._rows.clear()
        for item in normalize_env_vars(raw):
            self._add_row(item["key"], item["value"])
        self._suppress = False
