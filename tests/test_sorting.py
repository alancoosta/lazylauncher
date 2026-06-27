"""Tests for sorting.py — pure script ordering (GTK-free)."""
from lazylauncher import sorting


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


# -- sort_groups --------------------------------------------------------------

def _groups_scripts():
    # g1 has 2 enabled scripts (one running), g2 has 1, g3 has none
    scripts = [
        {"id": "s1", "groups": ["g1"], "enabled": True},
        {"id": "s2", "groups": ["g1", "g2"], "enabled": True},
        {"id": "s3", "groups": ["g3"], "enabled": False},
    ]
    groups = [{"id": "g1", "name": "Bravo"}, {"id": "g2", "name": "alpha"}, {"id": "g3", "name": "Charlie"}]
    return groups, scripts


def test_sort_groups_name_case_insensitive():
    groups, _ = _groups_scripts()
    out = sorting.sort_groups(groups, "name_asc")
    assert [g["id"] for g in out] == ["g2", "g1", "g3"]   # alpha, Bravo, Charlie


def test_sort_groups_by_enabled_count():
    groups, scripts = _groups_scripts()
    out = sorting.sort_groups(groups, "count_desc", scripts=scripts)
    # g1=2, g2=1, g3=0 (disabled s3 doesn't count)
    assert [g["id"] for g in out] == ["g1", "g2", "g3"]


def test_sort_groups_running_first():
    groups, scripts = _groups_scripts()
    out = sorting.sort_groups(groups, "running_first", scripts=scripts, running_ids={"s1"})
    assert out[0]["id"] == "g1"   # only g1 has a running script


def test_sort_groups_unknown_mode_noop():
    groups, _ = _groups_scripts()
    assert sorting.sort_groups(groups, "bogus") == groups
