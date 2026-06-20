"""Tests for sorting.py — pure script ordering (GTK-free)."""
import sorting


def _scripts():
    return [
        {"id": "b", "name": "Beta", "port": "8080"},
        {"id": "a", "name": "alpha", "port": ""},
        {"id": "c", "name": "Gamma", "port": "30"},
    ]


# -- port_sort_key ------------------------------------------------------------

def test_port_key_numeric():
    assert sorting.port_sort_key("8080") == 8080


def test_port_key_blank_and_garbage_are_zero():
    assert sorting.port_sort_key("") == 0
    assert sorting.port_sort_key("abc") == 0
    assert sorting.port_sort_key(None) == 0


# -- sort_scripts -------------------------------------------------------------

def test_sort_name_asc_case_insensitive():
    out = [s["id"] for s in sorting.sort_scripts(_scripts(), "name_asc")]
    assert out == ["a", "b", "c"]   # alpha, Beta, Gamma


def test_sort_name_desc():
    out = [s["id"] for s in sorting.sort_scripts(_scripts(), "name_desc")]
    assert out == ["c", "b", "a"]


def test_sort_port_asc_blank_first():
    out = [s["id"] for s in sorting.sort_scripts(_scripts(), "port_asc")]
    assert out == ["a", "c", "b"]   # 0, 30, 8080


def test_sort_port_desc():
    out = [s["id"] for s in sorting.sort_scripts(_scripts(), "port_desc")]
    assert out == ["b", "c", "a"]


def test_sort_running_first():
    out = [s["id"] for s in sorting.sort_scripts(_scripts(), "running_first", {"c"})]
    assert out[0] == "c"


def test_sort_stopped_first_keeps_running_last():
    out = [s["id"] for s in sorting.sort_scripts(_scripts(), "stopped_first", {"b"})]
    assert out[-1] == "b"


def test_unknown_mode_returns_unchanged_copy():
    src = _scripts()
    out = sorting.sort_scripts(src, "nope")
    assert [s["id"] for s in out] == ["b", "a", "c"]
    assert out is not src   # input not mutated


def test_input_not_mutated():
    src = _scripts()
    before = [s["id"] for s in src]
    sorting.sort_scripts(src, "name_asc")
    assert [s["id"] for s in src] == before
