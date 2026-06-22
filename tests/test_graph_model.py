"""Tests for graph_model.py — inferring script→port connections from env vars.

graph_model.py is GTK-free, so these run anywhere (including CI).
"""
from lazylauncher.graph_model import (
    build_port_index, infer_env_port_edges, build_graph,
)


def _script(sid, port="", env=None, name=None):
    return {
        "id": sid,
        "name": name or sid,
        "port": port,
        "env_vars": env or [],
    }


# -- build_port_index ---------------------------------------------------------

def test_port_index_groups_by_port():
    scripts = [_script("a", "3000"), _script("b", "3000"), _script("c", "8080")]
    index = build_port_index(scripts)
    assert {s["id"] for s in index[3000]} == {"a", "b"}
    assert {s["id"] for s in index[8080]} == {"c"}


def test_port_index_ignores_blank_and_nonnumeric():
    scripts = [_script("a", ""), _script("b", "abc"), _script("c", "5000")]
    assert set(build_port_index(scripts).keys()) == {5000}


# -- infer_env_port_edges -----------------------------------------------------

def test_infers_edge_from_url_value():
    scripts = [
        _script("backend", "3000"),
        _script("frontend", env=[{"key": "API_URL", "value": "http://localhost:3000"}]),
    ]
    edges = infer_env_port_edges(scripts)
    assert len(edges) == 1
    e = edges[0]
    assert (e.source_id, e.target_id, e.env_key, e.port) == ("frontend", "backend", "API_URL", 3000)


def test_infers_edge_from_bare_port_and_host_port():
    scripts = [
        _script("db", "5432"),
        _script("api", env=[
            {"key": "DB_PORT", "value": "5432"},
            {"key": "DB_HOST", "value": "127.0.0.1:5432"},
        ]),
    ]
    targets = {(e.source_id, e.target_id, e.port) for e in infer_env_port_edges(scripts)}
    # one edge per (src,tgt,port) even though two env vars reference it
    assert targets == {("api", "db", 5432)}


def test_no_false_positive_substring_match():
    # 3000 must not match inside 30000
    scripts = [
        _script("svc", "3000"),
        _script("other", env=[{"key": "BIG", "value": "30000"}]),
    ]
    assert infer_env_port_edges(scripts) == []


def test_self_reference_skipped():
    scripts = [_script("solo", "8080", env=[{"key": "PORT", "value": "8080"}])]
    assert infer_env_port_edges(scripts) == []


def test_dedup_same_pair_same_port():
    scripts = [
        _script("t", "9000"),
        _script("s", env=[
            {"key": "A", "value": ":9000"},
            {"key": "B", "value": "host:9000"},
        ]),
    ]
    edges = infer_env_port_edges(scripts)
    assert len(edges) == 1


def test_legacy_string_env_format():
    # normalize_env_vars accepts a legacy "KEY=VALUE" string
    scripts = [
        _script("cache", "6379"),
        _script("worker", env="REDIS=redis://localhost:6379"),
    ]
    edges = infer_env_port_edges(scripts)
    assert len(edges) == 1
    assert edges[0].target_id == "cache"


def test_global_env_reference_resolved():
    scripts = [
        _script("svc", "7000"),
        _script("client", env=[{"key": "SVC_URL", "global": True}]),
    ]
    global_map = {"SVC_URL": "http://localhost:7000"}
    edges = infer_env_port_edges(scripts, global_map)
    assert len(edges) == 1
    assert edges[0].target_id == "svc"


# -- build_graph --------------------------------------------------------------

def test_build_graph_nodes_and_running_flag():
    cfg = {
        "scripts": [
            _script("backend", "3000"),
            _script("frontend", env=[{"key": "API_URL", "value": "http://localhost:3000"}]),
        ],
        "global_env": [],
    }
    nodes, edges = build_graph(cfg, running_ids={"backend"})
    by_id = {n["id"]: n for n in nodes}
    assert by_id["backend"]["running"] is True
    assert by_id["frontend"]["running"] is False
    assert len(edges) == 1
