"""Tests for log_view.py pure helpers. GTK-free."""
from lazylauncher import log_view


# -- is_complex_json ----------------------------------------------------------

def test_complex_dict_two_keys():
    assert log_view.is_complex_json({"a": 1, "b": 2})


def test_complex_dict_nested():
    assert log_view.is_complex_json({"a": {"b": 1}})


def test_simple_dict_single_scalar_not_complex():
    assert not log_view.is_complex_json({"a": 1})


def test_scalar_not_complex():
    assert not log_view.is_complex_json(42)
    assert not log_view.is_complex_json("hi")


def test_list_complex_by_length():
    assert log_view.is_complex_json([1, 2])
    assert not log_view.is_complex_json([1])


# -- find_json_blocks ---------------------------------------------------------

def test_find_block_in_log_line():
    text = 'INFO starting\n{"service": "api", "port": 8080}\ndone\n'
    blocks = log_view.find_json_blocks(text)
    assert len(blocks) == 1
    start, end, obj = blocks[0]
    assert obj == {"service": "api", "port": 8080}
    assert text[start] == "{"


def test_ignores_non_boundary_and_simple():
    # inline brace not at line start, and a too-simple object
    text = 'log {"a":1} here\n{"x": 1}\n'
    assert log_view.find_json_blocks(text) == []


def test_finds_multiple_blocks():
    text = '{"a":1,"b":2}\nmid\n[1,2,3]\n'
    blocks = log_view.find_json_blocks(text)
    assert [b[2] for b in blocks] == [{"a": 1, "b": 2}, [1, 2, 3]]


# -- build_clean_to_raw_map ---------------------------------------------------

def test_map_identity_without_escapes():
    raw = "hello"
    pos = log_view.build_clean_to_raw_map(raw)
    assert pos == [0, 1, 2, 3, 4, 5]


def test_map_accounts_for_sgr_escape():
    raw = "\x1b[31mAB\x1b[0m"
    pos = log_view.build_clean_to_raw_map(raw)
    # clean text is "AB"; positions point past the leading escape
    assert raw[pos[0]] == "A"
    assert raw[pos[1]] == "B"


# -- hash_json ----------------------------------------------------------------

def test_hash_stable_across_key_order():
    assert log_view.hash_json({"a": 1, "b": 2}) == log_view.hash_json({"b": 2, "a": 1})


def test_hash_handles_unserializable():
    # object() isn't JSON-serializable; must not raise
    assert isinstance(log_view.hash_json(object()), int)


# -- make_json_summary --------------------------------------------------------

def test_summary_dict_keys_preview():
    assert log_view.make_json_summary({"a": 1, "b": 2}, "}") == " a, b }"


def test_summary_dict_truncates_after_three():
    s = log_view.make_json_summary({"a": 1, "b": 2, "c": 3, "d": 4}, "}")
    assert s == " a, b, c, … }"


def test_summary_list_counts_items():
    assert log_view.make_json_summary([1, 2, 3], "]") == " 3 items ]"


# -- theme_log_colors ---------------------------------------------------------

def test_theme_colors_have_required_keys():
    for dark in (True, False):
        c = log_view.theme_log_colors(dark)
        for key in ("json-key", "json-brace", "search-match-bg", "fold-accent"):
            assert key in c


def test_theme_colors_differ_by_theme():
    assert log_view.theme_log_colors(True) != log_view.theme_log_colors(False)
