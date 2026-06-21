#!/usr/bin/env python3
"""
log_view.py — GTK-free helpers for LazyLauncher's log viewer.

The TextView/search widget itself still lives in manager.py; this module holds
the pure, unit-testable pieces it relies on: JSON block detection, fold
hashing/summaries, the ANSI-stripped→raw position map, and the theme palette.
"""
import json
import re

from . import ansi


def theme_log_colors(dark: bool) -> dict:
    """Return syntax-highlight colors for the log view, adapted to the theme."""
    if dark:
        return {
            "json-key": "#e08a6f",
            "json-string": "#2ecc71",
            "json-number": "#f0a050",
            "json-bool": "#b07ad8",
            "json-null": "#95a5a6",
            "json-brace": "#5dade2",
            "search-match-bg": "#e0c040",
            "search-match-fg": "#1a1a1a",
            "search-current-bg": "#f5a623",
            "search-current-fg": "#1a1a1a",
            "fold-accent": "#e08a6f",
            "fold-hint": "#95a5a6",
        }
    return {
        "json-key": "#a0522d",
        "json-string": "#1a7a40",
        "json-number": "#c06000",
        "json-bool": "#6a1b9a",
        "json-null": "#607d8b",
        "json-brace": "#1565c0",
        "search-match-bg": "#fff176",
        "search-match-fg": "#1a1a1a",
        "search-current-bg": "#ffb300",
        "search-current-fg": "#1a1a1a",
        "fold-accent": "#a0522d",
        "fold-hint": "#607d8b",
    }


def is_complex_json(obj) -> bool:
    """True if a parsed object is worth rendering as a collapsible block."""
    if not isinstance(obj, (dict, list)):
        return False
    if isinstance(obj, dict):
        return len(obj) >= 2 or any(isinstance(v, (dict, list)) for v in obj.values())
    return len(obj) >= 2 or any(isinstance(v, (dict, list)) for v in obj)


_JSON_START_RE = re.compile(r'^[ \t]*([{\[])', re.MULTILINE)


def find_json_blocks(text):
    """Find complex JSON blocks at line boundaries → list of (start, end, obj)."""
    decoder = json.JSONDecoder()
    blocks = []
    for m in _JSON_START_RE.finditer(text):
        i = m.start(1)
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            continue
        if is_complex_json(obj):
            blocks.append((i, end, obj))
    return blocks


def build_clean_to_raw_map(raw):
    """Map char positions in ANSI-stripped text back to positions in raw text."""
    pos_map = []
    raw_i = 0
    raw_n = len(raw)
    while raw_i < raw_n:
        m = ansi.OSC_RE.match(raw, raw_i)
        if m:
            raw_i = m.end()
            continue
        m = ansi.ALL_RE.match(raw, raw_i)
        if m:
            raw_i = m.end()
            continue
        pos_map.append(raw_i)
        raw_i += 1
    pos_map.append(raw_i)
    return pos_map


def hash_json(obj):
    """Stable-ish hash of a JSON value, for remembering fold state across reloads."""
    try:
        return hash(json.dumps(obj, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return id(obj)


def make_json_summary(obj, close_br):
    """One-line summary shown when a JSON block is collapsed."""
    if isinstance(obj, dict):
        preview = ", ".join(list(obj.keys())[:3])
        if len(obj) > 3:
            preview += ", …"
        return f" {preview} {close_br}"
    return f" {len(obj)} items {close_br}"
