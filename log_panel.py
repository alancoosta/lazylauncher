#!/usr/bin/env python3
"""log_panel.py — LogPanel: the script log viewer (ANSI colors, collapsible
JSON, tail-follow, search, error banner).

Extracted verbatim from ScriptForm so the log-rendering machinery lives in one
focused, self-contained widget. It owns its own ``_script`` (set via
``set_script``); everything else operates on its private TextView/buffer state.
"""
import json

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, Pango, GLib

from common import LOG_DIR, get_error_states, clear_error_state
import ansi
import log_view
from ui_shared import _is_dark_theme


class LogPanel(Gtk.Box):
    """The Logs tab content: toolbar, error banner, log view, search bar."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._script = None

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

        self.log_search_btn = Gtk.Button()
        self.log_search_btn.set_image(Gtk.Image.new_from_icon_name("edit-find-symbolic", Gtk.IconSize.MENU))
        self.log_search_btn.set_tooltip_text("Search (Ctrl+F)")
        self.log_search_btn.get_style_context().add_class("btn-secondary")
        self.log_search_btn.connect("clicked", lambda _: self._open_log_search())

        self.log_path_lbl = Gtk.Label(label="")
        self.log_path_lbl.get_style_context().add_class("form-hint")
        self.log_path_lbl.set_hexpand(True)
        self.log_path_lbl.set_halign(Gtk.Align.START)
        self.log_path_lbl.set_ellipsize(Pango.EllipsizeMode.START)

        logs_toolbar.pack_start(self.log_refresh_btn, False, False, 0)
        logs_toolbar.pack_start(self.log_clear_btn, False, False, 0)
        logs_toolbar.pack_start(self.log_dismiss_btn, False, False, 0)
        logs_toolbar.pack_start(self.log_path_lbl, True, True, 0)
        logs_toolbar.pack_end(self.log_search_btn, False, False, 0)
        self.pack_start(logs_toolbar, False, False, 0)

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
        self.pack_start(self.log_error_bar, False, False, 0)

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

        # JSON syntax highlight tags (theme-aware)
        self._tc = log_view.theme_log_colors(_is_dark_theme())
        buf = self.log_text_view.get_buffer()
        buf.create_tag("json-key", foreground=self._tc["json-key"], weight=Pango.Weight.BOLD)
        buf.create_tag("json-string", foreground=self._tc["json-string"])
        buf.create_tag("json-number", foreground=self._tc["json-number"])
        buf.create_tag("json-bool", foreground=self._tc["json-bool"], weight=Pango.Weight.BOLD)
        buf.create_tag("json-null", foreground=self._tc["json-null"], style=Pango.Style.ITALIC)
        buf.create_tag("json-brace", foreground=self._tc["json-brace"])
        buf.create_tag("search-match", background=self._tc["search-match-bg"], foreground=self._tc["search-match-fg"])
        buf.create_tag("search-current", background=self._tc["search-current-bg"], foreground=self._tc["search-current-fg"])

        self.log_scroll.add(self.log_text_view)
        # Track scroll position via vadjustment (catches scrollbar drag, keyboard, mouse wheel)
        vadj = self.log_scroll.get_vadjustment()
        vadj.connect("value-changed", self._on_log_vadj_changed)
        self.log_text_view.add_events(Gdk.EventMask.POINTER_MOTION_MASK)
        self.log_text_view.connect("button-press-event", self._on_log_click)
        self.log_text_view.connect("motion-notify-event", self._on_log_motion)

        # ── Log search bar (floating below search button) ──
        self._log_search_revealer = Gtk.Revealer()
        self._log_search_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._log_search_revealer.set_transition_duration(150)
        self._log_search_revealer.set_reveal_child(False)
        self._log_search_revealer.set_halign(Gtk.Align.END)
        self._log_search_revealer.set_valign(Gtk.Align.START)

        search_bar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        search_bar_box.get_style_context().add_class("log-search-bar")
        search_bar_box.set_margin_end(8)
        search_bar_box.set_margin_top(4)

        # Entry + count inside a wrapper to show count inside the input
        entry_wrapper = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        entry_wrapper.get_style_context().add_class("log-search-entry-wrap")
        entry_wrapper.set_size_request(300, -1)

        self._log_search_entry = Gtk.SearchEntry()
        self._log_search_entry.set_placeholder_text("Search…")
        self._log_search_entry.set_hexpand(True)
        self._log_search_entry.get_style_context().add_class("log-search-input")

        self._log_search_count = Gtk.Label(label="")
        self._log_search_count.get_style_context().add_class("log-search-count")

        entry_wrapper.pack_start(self._log_search_entry, True, True, 0)
        entry_wrapper.pack_start(self._log_search_count, False, False, 0)

        self._log_search_prev = Gtk.Button(label="▲")
        self._log_search_prev.set_tooltip_text("Previous (Shift+Enter)")
        self._log_search_next = Gtk.Button(label="▼")
        self._log_search_next.set_tooltip_text("Next (Enter)")

        self._log_search_close = Gtk.Button(label="✕")
        self._log_search_close.set_tooltip_text("Close (Esc)")

        search_bar_box.pack_start(entry_wrapper, False, False, 0)
        search_bar_box.pack_start(self._log_search_prev, False, False, 0)
        search_bar_box.pack_start(self._log_search_next, False, False, 0)
        search_bar_box.pack_start(self._log_search_close, False, False, 0)

        self._log_search_revealer.add(search_bar_box)

        self._log_search_matches = []
        self._log_search_idx = -1
        self._log_search_timeout_id = None

        self._log_search_entry.connect("search-changed", self._on_log_search_changed)
        self._log_search_prev.connect("clicked", lambda _: self._log_search_navigate(-1))
        self._log_search_next.connect("clicked", lambda _: self._log_search_navigate(1))
        self._log_search_close.connect("clicked", lambda _: self._close_log_search())
        self._log_search_entry.connect("key-press-event", self._on_log_search_key)

        # Overlay: log_scroll as base, search bar floating on top-right
        log_overlay = Gtk.Overlay()
        log_overlay.add(self.log_scroll)
        log_overlay.add_overlay(self._log_search_revealer)
        self.pack_start(log_overlay, True, True, 0)

        self.log_refresh_btn.connect("clicked", lambda _: self._reload_log())
        self.log_clear_btn.connect("clicked", self._clear_log)
        self.log_dismiss_btn.connect("clicked", self._dismiss_log_error)
        self._log_pending = False

    # ── public API (called by ScriptForm / ManagerWindow) ──

    def set_script(self, script):
        """Point the panel at a script and reset per-script view state."""
        self._script = script
        self._user_scrolled_up = False
        self._last_log_clean = ""
        self._fold_states = {}
        self.log_text_view.get_buffer().set_text("")
        self._log_pending = True

    def reload_log(self):
        self._reload_log()

    def reload_if_pending(self):
        if self._log_pending:
            self._reload_log()
            self._log_pending = False

    def mark_pending(self):
        self._log_pending = True

    def open_search(self):
        self._open_log_search()

    def update_error_banner(self, errors=None):
        self._update_error_banner(errors)


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

    _LOG_MAX_BYTES = 512 * 1024  # 512 KB tail

    def _read_log_raw(self, lp):
        """Read raw log text from file path (tail only for large files)."""
        self.log_path_lbl.set_text(str(lp))
        try:
            if not lp.exists():
                return "(no logs yet)"
            size = lp.stat().st_size
            if size <= self._LOG_MAX_BYTES:
                return lp.read_text(errors="replace")
            with open(lp, "rb") as f:
                f.seek(size - self._LOG_MAX_BYTES)
                data = f.read()
            text = data.decode(errors="replace")
            # skip first partial line
            nl = text.find('\n')
            if nl != -1:
                text = text[nl + 1:]
            return f"… (showing last {self._LOG_MAX_BYTES // 1024} KB of {size // 1024} KB)\n" + text
        except Exception as e:
            return f"Error reading log: {e}"

    def _append_tail_content(self, buf, raw, old_clean, clean):
        """Append only the new portion of log content (no flicker)."""
        pos_map = log_view.build_clean_to_raw_map(raw)
        raw_offset = pos_map[len(old_clean)] if len(old_clean) < len(pos_map) else len(raw)
        tail_raw = raw[raw_offset:]
        tail_clean = clean[len(old_clean):]

        self._log_scroll_internal = True
        cursor = buf.create_mark("log-cursor", buf.get_end_iter(), False)
        tail_blocks = log_view.find_json_blocks(tail_clean)
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
        pos_map = log_view.build_clean_to_raw_map(raw)
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
        clean = ansi.strip(raw)
        if clean == self._last_log_clean:
            return
        old_clean = self._last_log_clean
        self._last_log_clean = clean

        is_append = old_clean and clean.startswith(old_clean)

        if is_append:
            self._append_tail_content(buf, raw, old_clean, clean)
            self._reapply_log_search()
            return

        # Full rebuild needed
        saved_line = -1
        if self._user_scrolled_up:
            visible_rect = self.log_text_view.get_visible_rect()
            top_iter = self.log_text_view.get_iter_at_location(visible_rect.x, visible_rect.y)
            saved_line = top_iter[1].get_line() if isinstance(top_iter, tuple) else top_iter.get_line()
        self._log_scroll_internal = True
        self._build_collapsible_buffer(buf, raw)
        self._reapply_log_search()
        if saved_line == -1:
            GLib.idle_add(self._scroll_to_end)
        else:
            GLib.idle_add(self._scroll_to_line, saved_line)

    # ── collapsible JSON buffer builder ──

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
        clean = ansi.strip(raw)
        blocks = log_view.find_json_blocks(clean)
        cursor = buf.create_mark("log-cursor", buf.get_end_iter(), False)
        if not blocks:
            self._insert_ansi_text(buf, cursor, raw)
            buf.delete_mark(cursor)
            return
        pos_map = log_view.build_clean_to_raw_map(raw)
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

    def _insert_ansi_text(self, buf, cursor, text):
        """Insert text with ANSI color processing at cursor mark position."""
        text = ansi.OSC_RE.sub('', text)
        fg = None
        bold = False
        last = 0
        for m in ansi.SGR_RE.finditer(text):
            before = text[last:m.start()]
            if before:
                before = ansi.ALL_RE.sub('', before)
                if before:
                    self._insert_ansi_chunk(buf, cursor, before, fg, bold)
            fg, bold = ansi.parse_sgr(m.group(1))
            last = m.end()
        remaining = ansi.ALL_RE.sub('', text[last:])
        if remaining:
            self._insert_ansi_chunk(buf, cursor, remaining, fg, bold)

    def _insert_ansi_chunk(self, buf, cursor, text, fg_code, bold):
        """Insert a text chunk with ANSI-derived color at cursor mark."""
        it = buf.get_iter_at_mark(cursor)
        if fg_code and fg_code in ansi.ANSI_COLORS:
            tag_name = f"ansi-{fg_code}{'-b' if bold else ''}"
            if not buf.get_tag_table().lookup(tag_name):
                kw = {"foreground": ansi.ANSI_COLORS[fg_code]}
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

    def _insert_at_cursor(self, buf, cursor, text, tag=None):
        """Insert text at cursor mark, optionally with a tag."""
        it = buf.get_iter_at_mark(cursor)
        if tag:
            buf.insert_with_tags_by_name(it, text, tag)
        else:
            buf.insert(it, text)

    def _create_fold_tags(self, buf, fold_id, collapsed):
        """Create the four visibility tags for a JSON fold."""
        buf.create_tag(f"jte-{fold_id}", foreground=self._tc["fold-accent"], weight=Pango.Weight.BOLD,
                       invisible=collapsed)
        buf.create_tag(f"jtc-{fold_id}", foreground=self._tc["fold-accent"], weight=Pango.Weight.BOLD,
                       invisible=not collapsed)
        buf.create_tag(f"js-{fold_id}", foreground=self._tc["fold-hint"], style=Pango.Style.ITALIC,
                       invisible=not collapsed)
        buf.create_tag(f"jc-{fold_id}", invisible=collapsed)

    def _insert_fold_toggles(self, buf, cursor, fold_id):
        """Insert expand/collapse toggle markers."""
        self._insert_at_cursor(buf, cursor, "\u25BC ", f"jte-{fold_id}")
        self._insert_at_cursor(buf, cursor, "\u25B6 ", f"jtc-{fold_id}")

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
        content_hash = log_view.hash_json(obj)
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

        summary = log_view.make_json_summary(obj, close_br)
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

    # ── log search (gedit-style) ──

    def _open_log_search(self):
        self._log_search_revealer.set_reveal_child(True)
        self._log_search_entry.grab_focus()
        sel = self.log_text_view.get_buffer().get_selection_bounds()
        if sel:
            self._log_search_entry.set_text(
                sel[0].get_text(sel[1]))

    def _close_log_search(self):
        self._log_search_revealer.set_reveal_child(False)
        buf = self.log_text_view.get_buffer()
        buf.remove_tag_by_name("search-match", buf.get_start_iter(), buf.get_end_iter())
        buf.remove_tag_by_name("search-current", buf.get_start_iter(), buf.get_end_iter())
        self._log_search_matches.clear()
        self._log_search_idx = -1
        self._log_search_count.set_text("")
        self.log_text_view.grab_focus()

    def _on_log_search_key(self, widget, event):
        key = event.keyval
        if key == Gdk.KEY_Escape:
            self._close_log_search()
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if event.state & Gdk.ModifierType.SHIFT_MASK:
                self._log_search_navigate(-1)
            else:
                self._log_search_navigate(1)
            return True
        return False

    def _on_log_search_changed(self, entry):
        if self._log_search_timeout_id:
            GLib.source_remove(self._log_search_timeout_id)
        self._log_search_timeout_id = GLib.timeout_add(150, self._do_log_search)

    def _do_log_search(self):
        self._log_search_timeout_id = None
        buf = self.log_text_view.get_buffer()
        buf.remove_tag_by_name("search-match", buf.get_start_iter(), buf.get_end_iter())
        buf.remove_tag_by_name("search-current", buf.get_start_iter(), buf.get_end_iter())
        self._log_search_matches.clear()
        self._log_search_idx = -1

        query = self._log_search_entry.get_text().strip()
        if not query:
            self._log_search_count.set_text("")
            return False

        start = buf.get_start_iter()
        while True:
            result = start.forward_search(query, Gtk.TextSearchFlags.CASE_INSENSITIVE, None)
            if not result:
                break
            match_start, match_end = result
            buf.apply_tag_by_name("search-match", match_start, match_end)
            self._log_search_matches.append((match_start.get_offset(), match_end.get_offset()))
            start = match_end

        count = len(self._log_search_matches)
        if count == 0:
            self._log_search_count.set_text("0 results")
        else:
            self._log_search_idx = 0
            self._highlight_current_match()
        return False

    def _highlight_current_match(self):
        buf = self.log_text_view.get_buffer()
        buf.remove_tag_by_name("search-current", buf.get_start_iter(), buf.get_end_iter())
        if not self._log_search_matches or self._log_search_idx < 0:
            return
        off_start, off_end = self._log_search_matches[self._log_search_idx]
        it_start = buf.get_iter_at_offset(off_start)
        it_end = buf.get_iter_at_offset(off_end)
        buf.apply_tag_by_name("search-current", it_start, it_end)
        self._log_search_count.set_text(
            f"{self._log_search_idx + 1}/{len(self._log_search_matches)}")
        self.log_text_view.scroll_to_iter(it_start, 0.1, True, 0.0, 0.5)

    def _log_search_navigate(self, direction):
        if not self._log_search_matches:
            return
        self._log_search_idx = (self._log_search_idx + direction) % len(self._log_search_matches)
        self._highlight_current_match()

    def _reapply_log_search(self):
        if self._log_search_revealer.get_reveal_child() and self._log_search_entry.get_text().strip():
            self._do_log_search()
