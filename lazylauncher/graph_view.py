#!/usr/bin/env python3
"""
graph_view.py — the Map: a read-only, pan/zoom canvas of how scripts connect.

Nodes are scripts; dashed edges are the *inferred* "env var references another
script's port" links from graph_model. Pure Cairo on a Gtk.DrawingArea — no
external graph dependency. Layout is a small Fruchterman-Reingold spring model
run once per reload. Click a node to open it in the editor.
"""

import math
import random

import cairo

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GObject

from .graph_model import build_graph
from .common import load_ui_state, save_ui_state
from .ui_shared import _is_dark_theme


_PALETTE_DARK = {
    "bg":            (0.12, 0.12, 0.13),
    "node_fill":     (0.18, 0.19, 0.21),
    "node_border":   (0.30, 0.31, 0.34),
    "node_running":  (0.15, 0.68, 0.38),
    "text":          (0.90, 0.91, 0.92),
    "muted":         (0.58, 0.60, 0.63),
    "edge":          (0.48, 0.50, 0.54),
    "accent":        (0.30, 0.62, 0.86),
    "label_bg":      (0.10, 0.10, 0.11),
    "dot":           (0.22, 0.23, 0.25),
}
_PALETTE_LIGHT = {
    "bg":            (0.97, 0.97, 0.98),
    "node_fill":     (1.00, 1.00, 1.00),
    "node_border":   (0.80, 0.81, 0.83),
    "node_running":  (0.15, 0.68, 0.38),
    "text":          (0.13, 0.14, 0.16),
    "muted":         (0.45, 0.47, 0.50),
    "edge":          (0.62, 0.64, 0.67),
    "accent":        (0.16, 0.50, 0.73),
    "label_bg":      (0.93, 0.93, 0.95),
    "dot":           (0.85, 0.86, 0.88),
}

_MIN_SCALE = 0.2
_MAX_SCALE = 3.0
_CLICK_SLOP = 5  # px of pointer travel still counted as a click, not a drag


class GraphView(Gtk.Box):
    """A pan/zoom Cairo canvas of the script connection graph."""

    __gsignals__ = {
        # emitted with a script id when a node is clicked
        "script-activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self._nodes = []
        self._edges = []
        self._idx = {}            # script id -> node dict
        self._scale = 1.0
        self._ox = 0.0            # screen offset x
        self._oy = 0.0            # screen offset y
        self._need_fit = True
        self._palette = _PALETTE_DARK if _is_dark_theme() else _PALETTE_LIGHT

        # cached dot-grid tile (rebuilt only when zoom step or theme changes), so
        # panning/dragging repaints the background with one fill, not a per-dot loop
        self._dot_tile = None
        self._dot_tile_gap = None
        self._dot_tile_color = None

        # pan / drag / click bookkeeping
        self._press = None        # (x, y) of button-press
        self._press_node = None
        self._node_start = None   # node's world (x, y) at press, for dragging
        self._moved = False
        self._offset_start = (0.0, 0.0)
        self._hover_node = None

        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.set_can_focus(True)  # so the canvas can receive key shortcuts
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
        )
        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_press)
        self.area.connect("button-release-event", self._on_release)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("scroll-event", self._on_scroll)
        self.area.connect("key-press-event", self._on_key)
        self.pack_start(self.area, True, True, 0)

    def grab_canvas_focus(self):
        """Focus the canvas so keyboard shortcuts work (call on view switch)."""
        self.area.grab_focus()

    # -- data ------------------------------------------------------------------

    def reload(self, cfg: dict, running_ids=None):
        """Rebuild nodes/edges from config and recompute the layout."""
        self._palette = _PALETTE_DARK if _is_dark_theme() else _PALETTE_LIGHT
        self._nodes, self._edges = build_graph(cfg, running_ids)
        self._idx = {n["id"]: n for n in self._nodes}
        self._measure_nodes()
        # Restore any positions the user dragged before; only auto-layout when at
        # least one node is unplaced (e.g. a newly added script).
        saved = self._load_positions()
        if not (self._nodes and all(n["id"] in saved for n in self._nodes)):
            self._layout()
        for n in self._nodes:
            if n["id"] in saved:
                n["x"], n["y"] = saved[n["id"]]
        self._need_fit = True
        self.area.queue_draw()

    def refresh_running(self, running_ids):
        """Recolour running nodes without recomputing the layout."""
        running = running_ids or set()
        for n in self._nodes:
            n["running"] = n["id"] in running
        self.area.queue_draw()

    def _load_positions(self) -> dict:
        """Read persisted node positions as ``{id: (x, y)}`` (best-effort)."""
        raw = load_ui_state().get("graph_positions", {})
        out = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    out[k] = (float(v[0]), float(v[1]))
                except (TypeError, ValueError, IndexError):
                    pass
        return out

    def _save_positions(self):
        """Persist current node positions so a dragged layout survives reopen.

        Writes only live nodes, so deleted scripts drop out of the saved map.
        """
        save_ui_state(graph_positions={n["id"]: [n["x"], n["y"]] for n in self._nodes})

    # -- sizing + layout -------------------------------------------------------

    def _measure_nodes(self):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        cr = cairo.Context(surf)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(13)
        for n in self._nodes:
            ext = cr.text_extents(n["name"] or "untitled")
            n["w"] = max(ext.width + 30, 96)
            n["h"] = 54 if n["port"] else 40

    def _layout(self):
        """Fruchterman-Reingold spring layout (deterministic seed)."""
        nodes = self._nodes
        n = len(nodes)
        if n == 0:
            return
        rnd = random.Random(42)
        # ideal edge length ~200px (k = sqrt(area/n)); keeps the layout compact
        # enough that the auto-fit lands near 1:1 for a handful of scripts.
        area = n * 40000.0
        k = math.sqrt(area / n)
        radius = k * math.sqrt(n) * 0.5
        for i, nd in enumerate(nodes):
            ang = 2 * math.pi * i / n
            nd["x"] = math.cos(ang) * radius + rnd.uniform(-5, 5)
            nd["y"] = math.sin(ang) * radius + rnd.uniform(-5, 5)
        if n == 1:
            nodes[0]["x"] = nodes[0]["y"] = 0.0
            return

        edges = [
            (self._idx[e.source_id], self._idx[e.target_id])
            for e in self._edges
            if e.source_id in self._idx and e.target_id in self._idx
        ]
        temp = radius if radius else k
        for _ in range(150):
            disp = {id(nd): [0.0, 0.0] for nd in nodes}
            # repulsion between every pair
            for i in range(n):
                a = nodes[i]
                for j in range(i + 1, n):
                    b = nodes[j]
                    dx = a["x"] - b["x"]
                    dy = a["y"] - b["y"]
                    dist = math.hypot(dx, dy) or 0.01
                    force = k * k / dist
                    ux, uy = dx / dist, dy / dist
                    disp[id(a)][0] += ux * force
                    disp[id(a)][1] += uy * force
                    disp[id(b)][0] -= ux * force
                    disp[id(b)][1] -= uy * force
            # attraction along edges
            for a, b in edges:
                dx = a["x"] - b["x"]
                dy = a["y"] - b["y"]
                dist = math.hypot(dx, dy) or 0.01
                force = dist * dist / k
                ux, uy = dx / dist, dy / dist
                disp[id(a)][0] -= ux * force
                disp[id(a)][1] -= uy * force
                disp[id(b)][0] += ux * force
                disp[id(b)][1] += uy * force
            # move, capped by temperature, and confine to the frame so that
            # disconnected nodes can't drift off to infinity.
            half = math.sqrt(area) / 2
            for nd in nodes:
                dx, dy = disp[id(nd)]
                dlen = math.hypot(dx, dy) or 0.01
                nd["x"] += dx / dlen * min(dlen, temp)
                nd["y"] += dy / dlen * min(dlen, temp)
                nd["x"] = max(-half, min(half, nd["x"]))
                nd["y"] = max(-half, min(half, nd["y"]))
            temp *= 0.95

    def _fit(self, w, h):
        if not self._nodes:
            return
        pad = 60
        minx = min(n["x"] - n["w"] / 2 for n in self._nodes)
        maxx = max(n["x"] + n["w"] / 2 for n in self._nodes)
        miny = min(n["y"] - n["h"] / 2 for n in self._nodes)
        maxy = max(n["y"] + n["h"] / 2 for n in self._nodes)
        bw = (maxx - minx) or 1.0
        bh = (maxy - miny) or 1.0
        # auto-fit may zoom out further than the user-zoom floor so everything
        # is always visible; capped at 1.5 so a tiny graph isn't blown up.
        self._scale = max(0.1, min((w - 2 * pad) / bw, (h - 2 * pad) / bh, 1.5))
        cx = (minx + maxx) / 2
        cy = (miny + maxy) / 2
        self._ox = w / 2 - cx * self._scale
        self._oy = h / 2 - cy * self._scale

    # -- drawing ---------------------------------------------------------------

    def _on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        w, h = alloc.width, alloc.height
        pal = self._palette
        cr.set_source_rgb(*pal["bg"])
        cr.paint()

        if self._nodes and self._need_fit:
            self._fit(w, h)
            self._need_fit = False

        self._draw_dots(cr, w, h)

        if not self._nodes:
            self._center_text(cr, w, h, "No scripts yet — add some to see the map.")
            return False

        cr.save()
        cr.translate(self._ox, self._oy)
        cr.scale(self._scale, self._scale)
        for e in self._edges:
            self._draw_edge(cr, e)
        for n in self._nodes:
            self._draw_node(cr, n)
        cr.restore()

        if not self._edges:
            self._top_hint(cr, w, "No env var points at another script's port yet.")
        self._draw_help(cr, h)
        return False

    def _draw_help(self, cr, h):
        cr.set_source_rgb(*self._palette["muted"])
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(11)
        cr.move_to(12, h - 12)
        cr.show_text("scroll/+- zoom · drag move · arrows pan · 0 or F fit · R reset")

    def _draw_dots(self, cr, w, h):
        """Dot-grid background that pans/zooms with the canvas (React-Flow style).

        Painted as a single repeating tile pattern (rebuilt only on zoom/theme
        change) so panning and dragging stay fluid — one ``fill`` per frame
        instead of a per-dot Python loop. Skipped when zoomed far out.
        """
        gap = int(round(28 * self._scale))
        if gap < 7:
            return
        color = self._palette["dot"]
        if self._dot_tile is None or self._dot_tile_gap != gap or self._dot_tile_color != color:
            r = max(0.8, min(1.8, 1.1 * self._scale))
            tile = cairo.ImageSurface(cairo.FORMAT_ARGB32, gap, gap)
            tcr = cairo.Context(tile)
            tcr.set_source_rgb(*color)
            # a dot at the tile corner; the repeat supplies the other quadrants,
            # so a full dot lands on every lattice point.
            tcr.arc(0, 0, r, 0, 2 * math.pi)
            tcr.fill()
            self._dot_tile = tile
            self._dot_tile_gap = gap
            self._dot_tile_color = color

        pattern = cairo.SurfacePattern(self._dot_tile)
        pattern.set_extend(cairo.Extend.REPEAT)
        mat = cairo.Matrix()
        mat.translate(-(self._ox % gap), -(self._oy % gap))
        pattern.set_matrix(mat)
        cr.set_source(pattern)
        cr.rectangle(0, 0, w, h)
        cr.fill()

    def _draw_edge(self, cr, edge):
        a = self._idx.get(edge.source_id)
        b = self._idx.get(edge.target_id)
        if not a or not b:
            return
        pal = self._palette
        p1 = self._border_point(a["x"], a["y"], a, toward=(b["x"], b["y"]))
        p2 = self._border_point(b["x"], b["y"], b, toward=(a["x"], a["y"]))

        cr.set_source_rgb(*pal["edge"])
        cr.set_line_width(1.6)
        cr.set_dash([6, 4])
        cr.move_to(*p1)
        cr.line_to(*p2)
        cr.stroke()
        cr.set_dash([])

        # arrowhead at the target border
        ang = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
        size = 9
        cr.move_to(*p2)
        cr.line_to(p2[0] - size * math.cos(ang - 0.4), p2[1] - size * math.sin(ang - 0.4))
        cr.line_to(p2[0] - size * math.cos(ang + 0.4), p2[1] - size * math.sin(ang + 0.4))
        cr.close_path()
        cr.fill()

        # label: "ENV_KEY :port" with a small backing plate
        label = f"{edge.env_key}  :{edge.port}" if edge.env_key else f":{edge.port}"
        mx = (p1[0] + p2[0]) / 2
        my = (p1[1] + p2[1]) / 2
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)
        ext = cr.text_extents(label)
        pad = 4
        cr.set_source_rgb(*pal["label_bg"])
        self._rounded_rect(cr, mx - ext.width / 2 - pad, my - ext.height / 2 - pad,
                           ext.width + 2 * pad, ext.height + 2 * pad, 4)
        cr.fill()
        cr.set_source_rgb(*pal["muted"])
        cr.move_to(mx - ext.width / 2 - ext.x_bearing, my - ext.height / 2 - ext.y_bearing)
        cr.show_text(label)

    def _draw_node(self, cr, n):
        pal = self._palette
        x = n["x"] - n["w"] / 2
        y = n["y"] - n["h"] / 2
        self._rounded_rect(cr, x, y, n["w"], n["h"], 8)
        cr.set_source_rgb(*pal["node_fill"])
        cr.fill_preserve()
        cr.set_line_width(2 if n["running"] else 1.2)
        cr.set_source_rgb(*(pal["node_running"] if n["running"] else pal["node_border"]))
        cr.stroke()

        # name
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(13)
        name = n["name"] or "untitled"
        ext = cr.text_extents(name)
        ty = n["y"] - 8 if n["port"] else n["y"]
        cr.set_source_rgb(*pal["text"])
        cr.move_to(n["x"] - ext.width / 2 - ext.x_bearing, ty - ext.height / 2 - ext.y_bearing)
        cr.show_text(name)

        # port badge
        if n["port"]:
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(11)
            badge = f":{n['port']}"
            bext = cr.text_extents(badge)
            cr.set_source_rgb(*pal["accent"])
            cr.move_to(n["x"] - bext.width / 2 - bext.x_bearing,
                       n["y"] + 11 - bext.height / 2 - bext.y_bearing)
            cr.show_text(badge)

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()

    @staticmethod
    def _border_point(cx, cy, node, toward):
        """Point on ``node``'s box border on the line from its center toward
        ``toward`` (used to anchor edges at box edges, not centers)."""
        dx = toward[0] - cx
        dy = toward[1] - cy
        if dx == 0 and dy == 0:
            return (cx, cy)
        hw, hh = node["w"] / 2, node["h"] / 2
        sx = hw / abs(dx) if dx else float("inf")
        sy = hh / abs(dy) if dy else float("inf")
        s = min(sx, sy)
        return (cx + dx * s, cy + dy * s)

    def _center_text(self, cr, w, h, msg):
        cr.set_source_rgb(*self._palette["muted"])
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(14)
        ext = cr.text_extents(msg)
        cr.move_to(w / 2 - ext.width / 2, h / 2)
        cr.show_text(msg)

    def _top_hint(self, cr, w, msg):
        cr.set_source_rgb(*self._palette["muted"])
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(12)
        ext = cr.text_extents(msg)
        cr.move_to(w / 2 - ext.width / 2, 24)
        cr.show_text(msg)

    # -- interaction -----------------------------------------------------------

    def _node_at(self, sx, sy):
        wx = (sx - self._ox) / self._scale
        wy = (sy - self._oy) / self._scale
        for n in reversed(self._nodes):
            if abs(wx - n["x"]) <= n["w"] / 2 and abs(wy - n["y"]) <= n["h"] / 2:
                return n
        return None

    def _on_press(self, _widget, event):
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self._need_fit = True
            self.area.queue_draw()
            return True
        self.area.grab_focus()  # clicking the canvas enables key shortcuts
        self._press = (event.x, event.y)
        self._press_node = self._node_at(event.x, event.y)
        self._node_start = (
            (self._press_node["x"], self._press_node["y"])
            if self._press_node else None
        )
        self._moved = False
        self._offset_start = (self._ox, self._oy)
        if self._press_node is not None:
            self._set_cursor("grabbing")
        return True

    def _on_motion(self, _widget, event):
        if self._press is None:
            # idle hover: show a hand over nodes so they read as interactive
            hov = self._node_at(event.x, event.y)
            if hov is not self._hover_node:
                self._hover_node = hov
                self._set_cursor("pointer" if hov else None)
            return False
        dx = event.x - self._press[0]
        dy = event.y - self._press[1]
        if abs(dx) > _CLICK_SLOP or abs(dy) > _CLICK_SLOP:
            self._moved = True
        if self._press_node is not None:  # drag the node
            self._press_node["x"] = self._node_start[0] + dx / self._scale
            self._press_node["y"] = self._node_start[1] + dy / self._scale
            self.area.queue_draw()
        else:                              # pan the canvas
            self._ox = self._offset_start[0] + dx
            self._oy = self._offset_start[1] + dy
            self.area.queue_draw()
        return True

    def _on_release(self, _widget, event):
        node = self._press_node
        moved = self._moved
        self._press = None
        self._press_node = None
        self._node_start = None
        self._moved = False
        self._set_cursor("pointer" if self._node_at(event.x, event.y) else None)
        if node is not None and not moved:
            self.emit("script-activated", node["id"])
        elif node is not None and moved:
            self._save_positions()  # persist the dragged layout
        return True

    def _set_cursor(self, name):
        """Best-effort cursor change; no-op before the window is realized."""
        win = self.area.get_window()
        if win is None:
            return
        cursor = None
        if name:
            display = win.get_display()
            cursor = Gdk.Cursor.new_from_name(display, name)
            if cursor is None and name == "grabbing":
                cursor = Gdk.Cursor.new_from_name(display, "grab")
        win.set_cursor(cursor)

    def _on_scroll(self, _widget, event):
        direction = event.direction
        if direction == Gdk.ScrollDirection.SMOOTH:
            _, _, dy = event.get_scroll_deltas()
            factor = 1.1 if dy < 0 else 0.9 if dy > 0 else 1.0
        elif direction == Gdk.ScrollDirection.UP:
            factor = 1.1
        elif direction == Gdk.ScrollDirection.DOWN:
            factor = 0.9
        else:
            return False
        self._zoom(factor, event.x, event.y)
        return True

    def _zoom(self, factor, cx, cy):
        """Scale by ``factor`` keeping the world point under (cx, cy) fixed."""
        old = self._scale
        new = max(_MIN_SCALE, min(_MAX_SCALE, old * factor))
        if new == old:
            return
        wx = (cx - self._ox) / old
        wy = (cy - self._oy) / old
        self._scale = new
        self._ox = cx - wx * new
        self._oy = cy - wy * new
        self.area.queue_draw()

    # -- shortcut actions (also driven by keys) --------------------------------

    def zoom_in(self):
        a = self.area.get_allocation()
        self._zoom(1.2, a.width / 2, a.height / 2)

    def zoom_out(self):
        a = self.area.get_allocation()
        self._zoom(1 / 1.2, a.width / 2, a.height / 2)

    def fit_view(self):
        """Re-centre and zoom so the whole graph is visible."""
        self._need_fit = True
        self.area.queue_draw()

    def reset_layout(self):
        """Discard saved positions and recompute the automatic layout."""
        save_ui_state(graph_positions={})
        self._layout()
        self._need_fit = True
        self.area.queue_draw()

    def _on_key(self, _widget, event):
        kv = event.keyval
        pan = 40
        if kv in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.zoom_in()
        elif kv in (Gdk.KEY_minus, Gdk.KEY_underscore, Gdk.KEY_KP_Subtract):
            self.zoom_out()
        elif kv in (Gdk.KEY_0, Gdk.KEY_KP_0, Gdk.KEY_f, Gdk.KEY_F):
            self.fit_view()
        elif kv in (Gdk.KEY_r, Gdk.KEY_R):
            self.reset_layout()
        elif kv in (Gdk.KEY_Left, Gdk.KEY_h):
            self._ox += pan
            self.area.queue_draw()
        elif kv in (Gdk.KEY_Right, Gdk.KEY_l):
            self._ox -= pan
            self.area.queue_draw()
        elif kv in (Gdk.KEY_Up, Gdk.KEY_k):
            self._oy += pan
            self.area.queue_draw()
        elif kv in (Gdk.KEY_Down, Gdk.KEY_j):
            self._oy -= pan
            self.area.queue_draw()
        else:
            return False
        return True
