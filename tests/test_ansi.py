"""Tests for ansi.py — SGR parsing, escape stripping, theme palette. GTK-free."""
from lazylauncher import ansi


def setup_function(_):
    # Each test starts from the default (dark) palette.
    ansi.set_theme(True)


# -- strip --------------------------------------------------------------------

def test_strip_sgr_codes():
    assert ansi.strip("\x1b[31mred\x1b[0m") == "red"


def test_strip_osc_sequence():
    # OSC (e.g. window title): ESC ] ... BEL
    assert ansi.strip("\x1b]0;title\x07hello") == "hello"


def test_strip_cursor_moves():
    assert ansi.strip("a\x1b[2Kb\x1b[1Gc") == "abc"


def test_strip_plain_text_unchanged():
    assert ansi.strip("nothing to strip") == "nothing to strip"


# -- parse_sgr ----------------------------------------------------------------

def test_parse_color_code():
    assert ansi.parse_sgr("31") == (31, False)


def test_parse_bold_color():
    assert ansi.parse_sgr("1;32") == (32, True)


def test_parse_reset():
    assert ansi.parse_sgr("0") == (None, False)


def test_parse_empty_is_reset():
    # bare ESC[m means reset
    assert ansi.parse_sgr("") == (None, False)


def test_parse_bold_off():
    assert ansi.parse_sgr("1;22") == (None, False)


def test_parse_default_fg():
    assert ansi.parse_sgr("31;39") == (None, False)


def test_parse_unknown_code_ignored():
    assert ansi.parse_sgr("7") == (None, False)


# -- set_theme ----------------------------------------------------------------

def test_set_theme_switches_palette():
    ansi.set_theme(True)
    assert ansi.ANSI_COLORS[31] == "#c0392b"
    ansi.set_theme(False)
    assert ansi.ANSI_COLORS[31] == "#a31515"


def test_both_palettes_share_keys():
    assert set(ansi._COLORS_DARK) == set(ansi._COLORS_LIGHT)
