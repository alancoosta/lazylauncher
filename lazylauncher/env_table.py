#!/usr/bin/env python3
"""env_table.py — EnvVarsTable: key/value editor widget for env_vars."""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GObject, Gdk

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
    value. The "⇄" button links a row to any pool variable, creating an *alias*
    (local key K takes another global's value): serialized as
    ``{"key": K, "global": X}``. Rows with an own value whose key is not yet in
    the pool offer a "＋ global" button that calls the ``on_promote(key, value)``
    callback.
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    _DEFAULT_KEY_WIDTH = 320  # initial KEY-column width (draggable divider)

    def __init__(self, on_promote=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._rows = []          # list of row dicts (see _add_row)
        self._suppress = False   # silence ``changed`` while loading
        self._pool = {}          # global pool: {key: value}
        self._on_promote = on_promote
        self._pool_model = Gtk.ListStore(str)  # pool keys, shared by all combos
        self._col_pos = self._DEFAULT_KEY_WIDTH  # shared KEY/value divider pos
        self._syncing = False    # guard against divider-sync recursion

        sort_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        sort_bar.set_halign(Gtk.Align.START)
        sort_lbl = Gtk.Label(label="Sort")
        sort_lbl.get_style_context().add_class("form-hint")
        sort_lbl.set_margin_end(4)
        sort_bar.pack_start(sort_lbl, False, False, 0)
        for label, field, reverse, tip in (
            ("Key A→Z", "key", False, "Sort variables by name, A→Z"),
            ("Key Z→A", "key", True,  "Sort variables by name, Z→A"),
            ("Value A→Z", "value", False, "Sort variables by value, A→Z"),
            ("Value Z→A", "value", True,  "Sort variables by value, Z→A"),
        ):
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("btn-icon")
            btn.set_tooltip_text(tip)
            btn.connect("clicked", lambda _w, f=field, r=reverse: self._sort_rows(f, r))
            sort_bar.pack_start(btn, False, False, 0)
        self.pack_start(sort_bar, False, False, 0)

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

    def _add_row(self, key="", value="", is_ref=False, ref="", focus=False):
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

        link_btn = Gtk.Button(label="⇄")
        link_btn.get_style_context().add_class("btn-icon")
        link_btn.set_valign(Gtk.Align.CENTER)
        link_btn.set_tooltip_text("Link this variable to a global")
        link_btn.set_no_show_all(True)

        remove_btn = Gtk.Button(label="✕")
        remove_btn.get_style_context().add_class("btn-icon")
        remove_btn.set_valign(Gtk.Align.CENTER)

        row = {"box": row_box, "combo": key_combo, "key": key_entry,
               "val": val_entry, "make": make_btn, "link": link_btn,
               "is_ref": is_ref, "ref": ref}

        remove_btn.connect("clicked", lambda _: self._remove_row(row))
        make_btn.connect("clicked", lambda _: self._promote_row(row))
        link_btn.connect("clicked", lambda _: self._open_link_menu(row, link_btn))
        key_entry.connect("changed", lambda _: self._on_key_changed(row))
        val_entry.connect("changed", lambda _: self._emit_changed())
        row_box.connect("notify::position", self._sync_divider)

        val_box.pack_start(val_entry, True, True, 0)
        val_box.pack_start(make_btn, False, False, 0)
        val_box.pack_start(link_btn, False, False, 0)
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
        # the row a live reference (under that same key); anything else is an
        # own value. An explicit alias (``ref``) is left untouched so renaming
        # the local key doesn't drop the link. Skipped while loading
        # (``_suppress``) so loaded own-value rows are never converted.
        if not self._suppress:
            if not row["ref"]:
                key = row["key"].get_text().strip()
                row["is_ref"] = bool(key) and key in self._pool
            self._refresh_row(row)
        self._emit_changed()

    def _refresh_row(self, row):
        """Sync a row's value entry and buttons to its reference state."""
        key = row["key"].get_text().strip()
        ref_key = (row["ref"] or key) if row["is_ref"] else ""
        val_entry = row["val"]
        ctx = val_entry.get_style_context()
        ctx.remove_class("env-ref")
        ctx.remove_class("env-ref-missing")

        if row["is_ref"]:
            val_entry.set_text("")
            val_entry.set_editable(False)
            val_entry.set_can_focus(False)
            if ref_key in self._pool:
                val_entry.set_placeholder_text(f"↳ {ref_key} = {self._pool[ref_key]}  (global)")
                ctx.add_class("env-ref")
            else:
                msg = f"⚠ {ref_key} not in pool" if ref_key else "⚠ global var missing from pool"
                val_entry.set_placeholder_text(msg)
                ctx.add_class("env-ref-missing")
            row["make"].hide()
            row["link"].show()
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
            row["link"].set_visible(bool(self._pool))

    def _set_ref(self, row, ref):
        """Link a row to a pool variable (alias): local key takes that value."""
        row["is_ref"] = True
        row["ref"] = ref
        self._refresh_row(row)
        self._emit_changed()

    def _unlink(self, row):
        """Drop the reference/alias and go back to an editable own value."""
        row["is_ref"] = False
        row["ref"] = ""
        self._refresh_row(row)
        self._emit_changed()

    def _open_link_menu(self, row, btn):
        """Popup listing the pool variables to link to (plus unlink)."""
        menu = Gtk.Menu()
        for gkey in sorted(self._pool):
            item = Gtk.MenuItem(label=f"↳ {gkey}")
            item.connect("activate", lambda _w, k=gkey: self._set_ref(row, k))
            menu.append(item)
        if row["is_ref"]:
            menu.append(Gtk.SeparatorMenuItem())
            unl = Gtk.MenuItem(label="Use own value")
            unl.connect("activate", lambda _w: self._unlink(row))
            menu.append(unl)
        menu.show_all()
        menu.popup_at_widget(btn, Gdk.Gravity.SOUTH, Gdk.Gravity.NORTH, None)

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

    def _sort_rows(self, field, reverse):
        """Reorder the rows alphabetically by KEY or by value (A→Z / Z→A).

        Reference rows sort on their resolved pool value so the order matches
        what the user sees. Empty-key rows are dropped, like on save.
        """
        items = []
        for row in self._rows:
            key = row["key"].get_text().strip()
            if not key:
                continue
            if row["is_ref"]:
                ref = row["ref"] or key
                items.append((key, self._pool.get(ref, ""), True, ref))
            else:
                items.append((key, row["val"].get_text(), False, ""))

        idx = 0 if field == "key" else 1
        items.sort(key=lambda t: t[idx].lower(), reverse=reverse)

        self._suppress = True
        for row in self._rows:
            self._rows_box.remove(row["box"])
        self._rows.clear()
        for key, value, is_ref, ref in items:
            if is_ref:
                self._add_row(key, "", is_ref=True, ref=ref)
            else:
                self._add_row(key, value)
        self._suppress = False
        self._emit_changed()

    def _emit_changed(self):
        if not self._suppress:
            self.emit("changed")

    # -- serialization ----------------------------------------------------

    def get_env_vars(self):
        """Return the table as a list of dicts, skipping rows with an empty key.

        A same-key reference is serialized as ``{"key", "global": True}``; an
        alias to another pool key as ``{"key", "global": "<ref>"}``; an own-value
        row as ``{"key", "value"}``.
        """
        result = []
        for row in self._rows:
            key = row["key"].get_text().strip()
            if not key:
                continue
            if row["is_ref"]:
                ref = row["ref"] or key
                # Same-key reference stays the legacy ``global: True``; an alias
                # to another key is serialized as ``global: "<ref>"``.
                if ref == key:
                    result.append({"key": key, "global": True})
                else:
                    result.append({"key": key, "global": ref})
            else:
                result.append({"key": key, "value": row["val"].get_text()})
        return result

    def set_env_vars(self, raw):
        """Populate rows from a list of dicts or a legacy KEY=VALUE string.

        Reference markers are preserved: ``global: True`` (same-key reference)
        and ``global: "<ref>"`` (alias to another pool key).
        """
        self._suppress = True
        for row in self._rows:
            self._rows_box.remove(row["box"])
        self._rows.clear()
        for item in normalize_env_vars(raw):
            g = item.get("global")
            if g:
                ref = g if isinstance(g, str) else ""
                self._add_row(item["key"], "", is_ref=True, ref=ref)
            else:
                self._add_row(item["key"], item["value"])
        self._suppress = False
