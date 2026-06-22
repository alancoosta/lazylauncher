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

    When a global pool is supplied via :meth:`set_global_pool`, the KEY field
    is a combo whose dropdown lists the pool keys (click to pick, or type a new
    one). A row whose key matches a pool key becomes a *live reference*: its
    value is locked to the pool value (resolved at launch time) and the row is
    serialized as ``{"key": K, "global": True}`` instead of carrying its own
    value. Rows with an own value whose key is not yet in the pool offer a
    "＋ global" button that calls the ``on_promote(key, value)`` callback.
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    _DEFAULT_KEY_WIDTH = 220  # initial KEY-column width (draggable divider)

    def __init__(self, on_promote=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._rows = []          # list of row dicts (see _add_row)
        self._suppress = False   # silence ``changed`` while loading
        self._pool = {}          # global pool: {key: value}
        self._on_promote = on_promote
        self._pool_model = Gtk.ListStore(str)  # pool keys, shared by all combos
        self._col_pos = self._DEFAULT_KEY_WIDTH  # shared KEY/value divider pos
        self._syncing = False    # guard against divider-sync recursion

        self._rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.pack_start(self._rows_box, False, False, 0)

        add_btn = Gtk.Button(label="+ Add variable")
        add_btn.get_style_context().add_class("btn-secondary")
        add_btn.set_halign(Gtk.Align.START)
        add_btn.connect("clicked", lambda _: self._add_row(focus=True))
        self.pack_start(add_btn, False, False, 0)

    # -- global pool ------------------------------------------------------

    def set_global_pool(self, pool):
        """Set the known global env pool ({key: value}) and refresh rows.

        Updates the KEY dropdown (shared by all rows) and re-evaluates every
        row's reference state so locked values / warnings reflect the pool.
        """
        self._pool = dict(pool or {})
        self._pool_model.clear()
        for key in self._pool:
            self._pool_model.append([key])
        for row in self._rows:
            self._refresh_row(row)

    # -- rows -------------------------------------------------------------

    def _add_row(self, key="", value="", is_ref=False, focus=False):
        # Each row is a horizontal Paned so the KEY/value divider is draggable;
        # all rows share one divider position (kept aligned by _sync_divider).
        row_box = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        row_box.set_wide_handle(True)
        row_box.set_position(self._col_pos)

        # KEY as a combo-with-entry: click opens the pool keys, type to filter
        # or to enter a brand-new key.
        key_combo = Gtk.ComboBox.new_with_entry()
        key_combo.set_model(self._pool_model)
        key_combo.set_entry_text_column(0)
        key_combo.set_active(-1)
        key_entry = key_combo.get_child()
        key_entry.get_style_context().add_class("form-entry")
        key_entry.set_placeholder_text("KEY")
        key_entry.set_width_chars(8)
        key_entry.set_text(key)

        val_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        val_entry = Gtk.Entry()
        val_entry.get_style_context().add_class("form-entry")
        val_entry.set_placeholder_text("value")
        val_entry.set_width_chars(8)
        val_entry.set_hexpand(True)
        val_entry.set_text(value)

        make_btn = Gtk.Button(label="＋ global")
        make_btn.get_style_context().add_class("btn-secondary")
        make_btn.set_valign(Gtk.Align.CENTER)
        make_btn.set_tooltip_text("Add this variable to the global pool")
        make_btn.set_no_show_all(True)

        remove_btn = Gtk.Button(label="✕")
        remove_btn.get_style_context().add_class("btn-icon")
        remove_btn.set_valign(Gtk.Align.CENTER)

        row = {"box": row_box, "combo": key_combo, "key": key_entry,
               "val": val_entry, "make": make_btn, "is_ref": is_ref}

        remove_btn.connect("clicked", lambda _: self._remove_row(row))
        make_btn.connect("clicked", lambda _: self._promote_row(row))
        key_entry.connect("changed", lambda _: self._on_key_changed(row))
        val_entry.connect("changed", lambda _: self._emit_changed())
        row_box.connect("notify::position", self._sync_divider)

        val_box.pack_start(val_entry, True, True, 0)
        val_box.pack_start(make_btn, False, False, 0)
        val_box.pack_start(remove_btn, False, False, 0)
        # pack1 fixed (follows the divider), pack2 takes the rest.
        row_box.pack1(key_combo, False, False)
        row_box.pack2(val_box, True, False)
        self._rows_box.pack_start(row_box, False, False, 0)
        self._rows.append(row)
        row_box.show_all()

        self._refresh_row(row)
        if focus:
            key_entry.grab_focus()
        self._emit_changed()

    def _sync_divider(self, paned, _pspec):
        """Keep every row's KEY/value divider at the same position."""
        if self._syncing:
            return
        self._syncing = True
        self._col_pos = paned.get_position()
        for row in self._rows:
            if row["box"] is not paned:
                row["box"].set_position(self._col_pos)
        self._syncing = False

    def _on_key_changed(self, row):
        # Picking from the dropdown or typing a key present in the pool makes
        # the row a live reference; anything else is an own value. Skipped while
        # loading (``_suppress``) so loaded own-value rows are never converted.
        if not self._suppress:
            key = row["key"].get_text().strip()
            row["is_ref"] = bool(key) and key in self._pool
            self._refresh_row(row)
        self._emit_changed()

    def _refresh_row(self, row):
        """Sync a row's value entry and buttons to its reference state."""
        key = row["key"].get_text().strip()
        val_entry = row["val"]
        ctx = val_entry.get_style_context()
        ctx.remove_class("env-ref")
        ctx.remove_class("env-ref-missing")

        if row["is_ref"]:
            val_entry.set_text("")
            val_entry.set_editable(False)
            val_entry.set_can_focus(False)
            if key in self._pool:
                val_entry.set_placeholder_text(f"↳ {self._pool[key]}  (global)")
                ctx.add_class("env-ref")
            else:
                val_entry.set_placeholder_text("⚠ global var missing from pool")
                ctx.add_class("env-ref-missing")
            row["make"].hide()
        else:
            val_entry.set_editable(True)
            val_entry.set_can_focus(True)
            val_entry.set_placeholder_text("value")
            can_promote = (
                self._on_promote is not None
                and bool(key)
                and key not in self._pool
                and val_entry.get_text() != ""
            )
            row["make"].set_visible(can_promote)

    def _promote_row(self, row):
        if self._on_promote is None:
            return
        key = row["key"].get_text().strip()
        value = row["val"].get_text()
        if not key:
            return
        # Convert to a live reference; the form adds it to the pool and calls
        # set_global_pool, which refreshes this row to show the locked value.
        row["is_ref"] = True
        self._on_promote(key, value)
        self._refresh_row(row)
        self._emit_changed()

    def _remove_row(self, row):
        if row not in self._rows:
            return
        self._rows_box.remove(row["box"])
        self._rows.remove(row)
        self._emit_changed()

    def _emit_changed(self):
        if not self._suppress:
            self.emit("changed")

    # -- serialization ----------------------------------------------------

    def get_env_vars(self):
        """Return the table as a list of dicts, skipping rows with an empty key.

        Reference rows are serialized as ``{"key", "global": True}``; own-value
        rows as ``{"key", "value"}``.
        """
        result = []
        for row in self._rows:
            key = row["key"].get_text().strip()
            if not key:
                continue
            if row["is_ref"]:
                result.append({"key": key, "global": True})
            else:
                result.append({"key": key, "value": row["val"].get_text()})
        return result

    def set_env_vars(self, raw):
        """Populate rows from a list of dicts or a legacy KEY=VALUE string.

        Reference markers (``{"key", "global": True}``) are preserved.
        """
        self._suppress = True
        for row in self._rows:
            self._rows_box.remove(row["box"])
        self._rows.clear()
        for item in normalize_env_vars(raw):
            if item.get("global"):
                self._add_row(item["key"], "", is_ref=True)
            else:
                self._add_row(item["key"], item["value"])
        self._suppress = False
