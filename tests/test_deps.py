"""Tests for deps.py — topological ordering, cycle detection, orchestration.

deps.py is GTK-free, so these run anywhere.
"""
import pytest

from deps import resolve_order, run_group_ordered, _port_of


# -- resolve_order ------------------------------------------------------------

def test_order_respects_deps():
    scripts = [
        {"id": "a", "depends_on": []},
        {"id": "b", "depends_on": ["a"]},
        {"id": "c", "depends_on": ["b"]},
    ]
    order = resolve_order(scripts)
    assert order.index("a") < order.index("b") < order.index("c")


def test_cycle_raises():
    scripts = [{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}]
    with pytest.raises(ValueError):
        resolve_order(scripts)


def test_dep_outside_group_is_ignored():
    scripts = [{"id": "a", "depends_on": ["ghost"]}]
    assert resolve_order(scripts) == ["a"]


def test_diamond_dependency():
    scripts = [
        {"id": "root", "depends_on": []},
        {"id": "left", "depends_on": ["root"]},
        {"id": "right", "depends_on": ["root"]},
        {"id": "join", "depends_on": ["left", "right"]},
    ]
    order = resolve_order(scripts)
    assert order.index("root") < order.index("left")
    assert order.index("root") < order.index("right")
    assert order.index("left") < order.index("join")
    assert order.index("right") < order.index("join")


def test_self_cycle_raises():
    with pytest.raises(ValueError):
        resolve_order([{"id": "a", "depends_on": ["a"]}])


# -- _port_of -----------------------------------------------------------------

def test_port_of_parses_digits():
    assert _port_of({"port": "8080"}) == 8080


def test_port_of_blank_is_none():
    assert _port_of({"port": ""}) is None
    assert _port_of({}) is None
    assert _port_of({"port": "abc"}) is None


# -- run_group_ordered (synchronous via dispatch=None) ------------------------

def test_orchestration_launches_in_order_without_ports():
    # No ports → nothing to wait on; should still launch every script in
    # dependency order. dispatch=None runs the worker thread but launches
    # synchronously inside it; join the thread before asserting.
    launched = []
    scripts = [
        {"id": "a", "depends_on": [], "port": ""},
        {"id": "b", "depends_on": ["a"], "port": ""},
        {"id": "c", "depends_on": ["b"], "port": ""},
    ]
    t = run_group_ordered(scripts, run_one=lambda s: launched.append(s["id"]))
    t.join(timeout=5)
    assert launched == ["a", "b", "c"]


def test_orchestration_skips_already_running():
    launched = []
    scripts = [
        {"id": "a", "depends_on": [], "port": ""},
        {"id": "b", "depends_on": ["a"], "port": ""},
    ]
    t = run_group_ordered(
        scripts, run_one=lambda s: launched.append(s["id"]),
        already_running={"a"},
    )
    t.join(timeout=5)
    assert launched == ["b"]


def test_orchestration_reports_cycle_via_event():
    events = []
    scripts = [{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}]
    t = run_group_ordered(
        scripts, run_one=lambda s: None,
        on_event=lambda kind, sid, detail: events.append((kind, sid)),
    )
    t.join(timeout=5)
    assert any(kind == "error" for kind, _ in events)
