#!/usr/bin/env python3
"""
graph_model.py — build a connection graph of scripts for the Map view.

GTK-free and pure (like deps.py / sorting.py) so it is unit-testable and can run
in CI. The only relationship visualised is the *inferred* link "this script's env
var points at another script's port":

    frontend env  API_URL=http://localhost:3000   ─┐
                                                     ├─►  backend (port 3000)
    backend  port 3000                              ─┘

These links are NOT stored anywhere — env vars are literal strings. We infer them
heuristically by scanning each (resolved) env value for the port number of some
*other* script. They are therefore best-effort guesses; the UI draws them dashed.
"""

import re
from collections import namedtuple

from .common import resolve_env_vars, global_env_map

# An inferred edge: script ``source_id`` references ``target_id`` because the env
# var ``env_key`` of the source contains the target's ``port``.
Edge = namedtuple("Edge", "source_id target_id env_key port")


def _port_of(script: dict):
    """Same numeric-port heuristic as deps._port_of (string -> int|None)."""
    p = str(script.get("port", "")).strip()
    return int(p) if p.isdigit() else None


def build_port_index(scripts: list) -> dict:
    """Map ``port -> [scripts that declare it]``.

    Scripts without a numeric port are ignored. Several scripts may share a port
    (e.g. two variants of the same service), hence a list per port.
    """
    index = {}
    for s in scripts:
        port = _port_of(s)
        if port is not None:
            index.setdefault(port, []).append(s)
    return index


def _ports_referenced(value: str, ports) -> set:
    """Return the ports from ``ports`` that appear as standalone tokens in value.

    The ``(?<!\\d)PORT(?!\\d)`` guard stops ``3000`` from matching inside
    ``30000`` or ``8080`` from matching inside ``18080``.
    """
    found = set()
    for port in ports:
        if re.search(rf"(?<!\d){port}(?!\d)", value):
            found.add(port)
    return found


def infer_env_port_edges(scripts: list, global_map: dict = None) -> list:
    """Infer script→script edges from env values that reference another's port.

    ``global_map`` resolves live references to the shared global pool so a global
    env var pointing at a port is detected too. Self-references (a script citing
    its own port) are skipped; edges are de-duplicated by (source, target, port).
    """
    index = build_port_index(scripts)
    if not index:
        return []
    ports = list(index.keys())
    global_map = global_map or {}

    edges = []
    seen = set()
    for src in scripts:
        src_id = src.get("id", "")
        for item in resolve_env_vars(src.get("env_vars"), global_map):
            value = item.get("value", "")
            if not value:
                continue
            key = item.get("key", "")
            for port in _ports_referenced(value, ports):
                for tgt in index[port]:
                    tgt_id = tgt.get("id", "")
                    if tgt_id == src_id:
                        continue  # a script referencing its own port is not a link
                    dedup = (src_id, tgt_id, port)
                    if dedup in seen:
                        continue
                    seen.add(dedup)
                    edges.append(Edge(src_id, tgt_id, key, port))
    return edges


def build_graph(cfg: dict, running_ids=None):
    """Return ``(nodes, edges)`` for the whole config.

    nodes: list of ``{id, name, port, running}`` — one per script.
    edges: list of :class:`Edge` (inferred env→port links).
    """
    scripts = cfg.get("scripts", [])
    running_ids = running_ids or set()
    gmap = global_env_map(cfg)
    nodes = [
        {
            "id": s.get("id", ""),
            "name": s.get("name", ""),
            "port": str(s.get("port", "")).strip(),
            "running": s.get("id", "") in running_ids,
        }
        for s in scripts
    ]
    edges = infer_env_port_edges(scripts, gmap)
    return nodes, edges
