#!/usr/bin/env python3
"""
deps.py — dependency ordering + port readiness for LazyLauncher.

Fits the existing data model:
  - each script already has "id", "port" (string) and (new) "depends_on": [ids].
  - readiness = the script's "port" accepts a TCP connection (same idea as the
    tray's _is_port_in_use()).
"""

import socket
import threading
import time


# ---------- readiness (same TCP probe as tray._is_port_in_use) ----------

def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _port_of(script: dict):
    p = str(script.get("port", "")).strip()
    return int(p) if p.isdigit() else None


def _wait_ready(port: int, timeout: float = 60.0, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port):
            return True
        time.sleep(interval)
    return False


# ---------- ordering (topological sort + cycle detection) ----------

def resolve_order(scripts: list) -> list:
    """
    Take the group's scripts and return ids in start order.
    Only considers dependencies that are within the group.
    Raises ValueError on a cycle.
    """
    by_id = {s.get("id", ""): s for s in scripts if s.get("id")}
    state = {}   # id -> "visiting" | "done"
    order = []

    def visit(sid, stack):
        st = state.get(sid)
        if st == "done":
            return
        if st == "visiting":
            raise ValueError("Circular dependency: " + " -> ".join(stack + [sid]))
        state[sid] = "visiting"
        for dep in by_id[sid].get("depends_on", []):
            if dep in by_id:           # ignore deps outside the group
                visit(dep, stack + [sid])
        state[sid] = "done"
        order.append(sid)

    for sid in by_id:
        visit(sid, [])
    return order


# ---------- orchestration ----------

def run_group_ordered(scripts, run_one, dispatch=None,
                      already_running=None, ready_timeout=60.0, on_event=None):
    """
    Start the group's scripts in dependency order, waiting for each dependency
    (that has a port) to be ready before launching the next.

    Runs in a daemon thread so it does NOT block the GTK main loop.
    Each launch is marshalled via `dispatch` (use GLib.idle_add) because run_one
    may open GTK dialogs, which must run on the main thread.

    run_one(script): existing function that actually runs a script.
    dispatch(fn, *args): GLib.idle_add (or None to call directly, e.g. in tests).
    already_running: set of ids already up (skipped, no rerun).
    on_event(kind, sid, detail): optional progress callback.
        kinds: "waiting" | "ready" | "launching" | "timeout" | "skip" | "error"
    """
    by_id = {s.get("id", ""): s for s in scripts if s.get("id")}
    running = set(already_running or ())

    def emit(kind, sid, detail=""):
        if on_event:
            (dispatch or (lambda f, *a: f(*a)))(on_event, kind, sid, detail)

    def launch(script):
        if dispatch:
            dispatch(run_one, script)
        else:
            run_one(script)

    def worker():
        try:
            order = resolve_order(scripts)
        except ValueError as e:
            emit("error", "", str(e))
            return

        for sid in order:
            script = by_id[sid]

            if sid in running:
                emit("skip", sid, "already running")
                continue

            for dep in script.get("depends_on", []):
                dep_script = by_id.get(dep)
                if not dep_script:
                    continue
                port = _port_of(dep_script)
                if port is None:
                    continue
                emit("waiting", sid, f"'{dep}' on port {port}")
                if not _wait_ready(port, ready_timeout):
                    emit("timeout", dep, f"port {port} in {ready_timeout:.0f}s")
                    return
                emit("ready", dep, f"port {port}")

            emit("launching", sid)
            launch(script)
            running.add(sid)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    grp = [
        {"id": "eureka",    "name": "Eureka",    "port": "8761", "depends_on": []},
        {"id": "service-a", "name": "Service A", "port": "9081", "depends_on": ["eureka"]},
        {"id": "service-b", "name": "Service B", "port": "9082", "depends_on": ["eureka"]},
    ]
    print("Order:", resolve_order(grp))
