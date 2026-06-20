#!/usr/bin/env python3
"""
ansi.py — ANSI/SGR parsing and theme palettes for LazyLauncher's log view.

GTK-free: the actual TextBuffer insertion lives in the manager's log view; here
we keep the color tables, the escape-sequence regexes, the SGR state parser and
a strip() helper, so they can be unit-tested in isolation.
"""
import re

# ANSI color code → hex color, tuned per background.
_COLORS_DARK = {
    30: "#555555",  31: "#c0392b",  32: "#27ae60",  33: "#f39c12",
    34: "#3498db",  35: "#8e44ad",  36: "#1abc9c",  37: "#c4bdb5",
    90: "#7a746c",  91: "#e74c3c",  92: "#2ecc71",  93: "#f1c40f",
    94: "#5dade2",  95: "#af7ac5",  96: "#48c9b0",  97: "#ede8e1",
}
_COLORS_LIGHT = {
    30: "#1a1a1a",  31: "#a31515",  32: "#1a7a40",  33: "#b06000",
    34: "#1565c0",  35: "#6a1b9a",  36: "#00796b",  37: "#4a4a4a",
    90: "#6a6a6a",  91: "#d32f2f",  92: "#2e7d32",  93: "#e0a000",
    94: "#1976d2",  95: "#7b1fa2",  96: "#00897b",  97: "#2a2a2a",
}

# Active palette — rebound by set_theme(). Always read it as ``ansi.ANSI_COLORS``
# (module attribute) so callers see the current theme; do not
# ``from ansi import ANSI_COLORS``.
ANSI_COLORS = _COLORS_DARK

SGR_RE = re.compile(r'\x1b\[([0-9;]*)m')
ALL_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
OSC_RE = re.compile(r'\x1b\][^\x07]*\x07')


def set_theme(dark: bool):
    """Select the dark or light palette as the active ANSI_COLORS."""
    global ANSI_COLORS
    ANSI_COLORS = _COLORS_DARK if dark else _COLORS_LIGHT


def strip(raw: str) -> str:
    """Remove OSC sequences and all CSI escape codes from text."""
    return ALL_RE.sub('', OSC_RE.sub('', raw))


def parse_sgr(codes_str):
    """Parse an ANSI SGR code string into (fg, bold) state.

    ``fg`` is an int color code present in ANSI_COLORS, or None.
    """
    fg, bold = None, False
    codes = [int(c) for c in codes_str.split(';') if c.isdigit()] if codes_str else [0]
    for code in codes:
        if code == 0:
            fg, bold = None, False
        elif code == 1:
            bold = True
        elif code == 22:
            bold = False
        elif code in ANSI_COLORS:
            fg = code
        elif code == 39:
            fg = None
    return fg, bold
