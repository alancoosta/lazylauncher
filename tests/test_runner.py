"""Tests for pure helpers in runner.py (launch / process / port engine).

runner imports Gtk at module load; on a headless box that may be missing, so we
skip the whole module rather than fail.
"""
import json
import os
import subprocess
import time
import types

import pytest

from lazylauncher import common
from lazylauncher import runner   # GTK-free since the launch engine was decoupled from the toolkit


class _PopenRec:
    """Records Popen calls and returns a harmless fake process."""
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return types.SimpleNamespace(pid=4242, wait=lambda: 0,
                                     poll=lambda: 0, returncode=0)


class _DummyThread:
    """Stand-in for threading.Thread so the notify thread never runs in tests."""
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


@pytest.fixture
def no_side_effects(monkeypatch):
    """Neuter the launch side effects (subprocess, notify thread, run state)."""
    rec = _PopenRec()
    monkeypatch.setattr(runner.subprocess, "Popen", rec)
    monkeypatch.setattr(runner.threading, "Thread", _DummyThread)
    monkeypatch.setattr(runner, "get_running_ids", lambda: set())
    monkeypatch.setattr(runner, "_mark_running", lambda *a: None)
    monkeypatch.setattr(runner, "_save_exit_status", lambda *a: None)
    return rec


# -- run_script: shell command construction (silent path) ---------------------

def test_run_script_silent_builds_shell_command(no_side_effects):
    runner.run_script({
        "command": "echo hi", "silent": True, "working_dir": "/tmp",
        "id": "", "name": "T", "login_shell": False,
        "env_vars": [{"key": "FOO", "value": "bar"}],
    })
    assert len(no_side_effects.calls) == 1
    args, kw = no_side_effects.calls[0]
    assert args[0] == [runner.USER_SHELL, "-c", "echo hi"]
    assert kw["cwd"] == "/tmp"
    assert kw["start_new_session"] is True
    assert kw["env"]["FOO"] == "bar"


def test_run_script_login_shell_uses_interactive_flag(no_side_effects):
    runner.run_script({"command": "x", "silent": True, "id": "",
                       "login_shell": True, "env_vars": []})
    assert no_side_effects.calls[0][0][0][1] == "-ilc"


def test_run_script_drops_blocked_env_keys(no_side_effects):
    runner.run_script({
        "command": "x", "silent": True, "id": "", "login_shell": False,
        "env_vars": [{"key": "PATH", "value": "/evil"},
                     {"key": "SAFE", "value": "ok"}],
    })
    env = no_side_effects.calls[0][1]["env"]
    assert env.get("PATH") != "/evil"   # PATH is blocklisted
    assert env["SAFE"] == "ok"


def test_run_script_empty_command_is_noop(no_side_effects):
    runner.run_script({"command": "   ", "silent": True, "id": ""})
    assert no_side_effects.calls == []


# -- _parse_env_vars ----------------------------------------------------------

def test_parse_env_vars_blocks_dangerous_and_keeps_safe():
    env = runner._parse_env_vars([{"key": "LD_PRELOAD", "value": "/x.so"},
                                  {"key": "MYVAR", "value": "v"}])
    assert "MYVAR" in env and env["MYVAR"] == "v"
    assert env.get("LD_PRELOAD") != "/x.so"
    assert "HOME" in env   # inherits the real environment


def test_parse_env_vars_resolves_global_reference():
    env = runner._parse_env_vars([{"key": "API", "global": True}],
                                 {"API": "https://x"})
    assert env["API"] == "https://x"


def test_parse_env_vars_orphan_reference_not_injected():
    env = runner._parse_env_vars([{"key": "GONE", "global": True}], {})
    assert "GONE" not in env


def test_parse_env_vars_blocklist_applies_to_resolved_global():
    # A pool entry named PATH must still be filtered after resolution.
    env = runner._parse_env_vars([{"key": "PATH", "global": True}], {"PATH": "/evil"})
    assert env.get("PATH") != "/evil"


# -- _mark_running: phantom-PID guard (the run_state write path) ---------------

def test_mark_running_rejects_pid_zero(monkeypatch, tmp_path):
    rs = tmp_path / "run_state.json"
    monkeypatch.setattr(runner, "RUN_STATE_FILE", rs)
    monkeypatch.setattr(common, "RUN_STATE_FILE", rs)
    monkeypatch.setattr(common, "RUN_LOCK_FILE", tmp_path / ".run.lock")
    monkeypatch.setattr(common, "STATE_DIR", tmp_path)
    monkeypatch.setattr(common, "CONFIG_FILE", tmp_path / "absent.json")
    runner._mark_running("ghost", 0)
    state = json.loads(rs.read_text()) if rs.exists() else {}
    assert "ghost" not in state
    runner._mark_running("real", os.getpid())
    assert "real" in json.loads(rs.read_text())


# -- ss output port parsing ---------------------------------------------------

SS_NODE = 'LISTEN 0 511 *:3000 *:* users:(("node",pid=1234,fd=20))'
SS_PG = 'LISTEN 0 511 127.0.0.1:5432 *:* users:(("postgres",pid=1234,fd=5))'


def test_parse_line_matching_pid():
    assert runner._parse_ports_from_line(SS_NODE, "1234") == [3000]


def test_parse_line_non_matching_pid():
    assert runner._parse_ports_from_line(SS_NODE, "9999") == []


def test_extract_multiple_ports_for_pid():
    out = "\n".join([SS_NODE, SS_PG])
    assert sorted(runner._extract_ports_from_ss(out, "1234")) == [3000, 5432]


# -- _descendant_pids (full process tree walk) --------------------------------

def test_descendant_pids_includes_self():
    assert os.getpid() in runner._descendant_pids(os.getpid())


def test_descendant_pids_finds_grandchild():
    # sh -> sh (subshell) -> sleep : the grandchild must be discovered, which the
    # old one-level pgrep -P walk could not do.
    proc = subprocess.Popen(
        [os.environ.get("SHELL", "/bin/sh"), "-c", "sh -c 'sleep 5'"],
    )
    try:
        time.sleep(0.5)  # let the subshell fork its child
        tree = runner._descendant_pids(proc.pid)
        assert proc.pid in tree
        assert len(tree) >= 2
    finally:
        proc.kill()
        proc.wait()


def test_descendant_pids_unknown_pid_returns_only_self():
    assert runner._descendant_pids(2_000_000_000) == [2_000_000_000]


# -- launcher self-delete + stale tempfile sweep ------------------------------

def test_write_launcher_self_deletes_and_is_executable():
    path = runner._write_launcher("true", "/tmp", "", "/tmp/x.pid", login_shell=False)
    try:
        body = open(path).read()
        assert f"rm -f {runner.shlex.quote(path)}" in body
        assert oct(os.stat(path).st_mode)[-3:] == "700"
    finally:
        os.unlink(path)


def test_cleanup_stale_tempfiles_removes_only_old(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    old_sh = tmp_path / "lazylauncher-abc.sh"
    old_pid = tmp_path / "lazylauncher-abc.pid"
    fresh = tmp_path / "lazylauncher-new.sh"
    unrelated = tmp_path / "keepme.sh"
    for f in (old_sh, old_pid, fresh, unrelated):
        f.write_text("x")
    old = time.time() - 48 * 3600
    os.utime(old_sh, (old, old))
    os.utime(old_pid, (old, old))
    runner._cleanup_stale_tempfiles()
    assert not old_sh.exists() and not old_pid.exists()
    assert fresh.exists()        # recent launcher left alone
    assert unrelated.exists()    # non-lazylauncher file untouched


# -- run_script: decision branches (duplicate-run / port-kill / confirm) --------
# These reach the branches the `no_side_effects` fixture hides: a real run_state
# (so "already running" is detectable), an injectable prompter, and a neutered
# kill/sleep so no real process is touched.

class _FakePrompter:
    """Records calls; returns whatever the test configured."""
    def __init__(self):
        self.duplicate_choice = "cancel"
        self.confirm_result = True
        self.calls = []

    def confirm(self, title, message=""):
        self.calls.append(("confirm", title))
        return self.confirm_result

    def duplicate_run(self, label, pid, ports):
        self.calls.append(("duplicate", label, pid, tuple(ports)))
        return self.duplicate_choice


@pytest.fixture
def launch_isolated(monkeypatch, tmp_path):
    """Real run_state/config under tmp_path; fake subprocess + no sleeps."""
    rs = tmp_path / "run_state.json"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"scripts": [], "groups": [], "global_env": []}))
    logs = tmp_path / "logs"
    logs.mkdir()
    for mod, name, val in [
        (common, "RUN_STATE_FILE", rs), (runner, "RUN_STATE_FILE", rs),
        (common, "RUN_LOCK_FILE", tmp_path / ".run.lock"),
        (common, "STATE_DIR", tmp_path), (common, "CONFIG_FILE", cfg),
        (common, "CONFIG_DIR", tmp_path), (common, "LOG_DIR", logs),
    ]:
        monkeypatch.setattr(mod, name, val)
    rec = _PopenRec()
    monkeypatch.setattr(runner.subprocess, "Popen", rec)
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="", stderr="", returncode=1))
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    monkeypatch.setattr(runner.threading, "Thread", _DummyThread)
    prompter = _FakePrompter()
    runner.set_prompter(prompter)
    return types.SimpleNamespace(rec=rec, prompter=prompter)


def _script(**over):
    base = {"command": "echo hi", "silent": True, "working_dir": "/tmp",
            "id": "s1", "name": "T", "login_shell": False, "env_vars": [],
            "confirm": False, "port": ""}
    base.update(over)
    return base


def test_run_script_duplicate_cancel_aborts(launch_isolated, monkeypatch):
    monkeypatch.setattr(runner, "_kill_safe", lambda *a: None)
    runner._mark_running("s1", os.getpid())          # already running
    launch_isolated.prompter.duplicate_choice = "cancel"
    runner.run_script(_script())
    assert launch_isolated.rec.calls == []            # aborted: no launch
    assert launch_isolated.prompter.calls[0][0] == "duplicate"


def test_run_script_duplicate_another_proceeds(launch_isolated, monkeypatch):
    monkeypatch.setattr(runner, "_kill_safe", lambda *a: None)
    runner._mark_running("s1", os.getpid())
    launch_isolated.prompter.duplicate_choice = "another"
    runner.run_script(_script())
    assert len(launch_isolated.rec.calls) == 1        # launched a second instance


def test_run_script_duplicate_restart_kills_and_relaunches(launch_isolated, monkeypatch):
    killed = []
    monkeypatch.setattr(runner, "_kill_safe", lambda fn, *a: killed.append(fn))
    runner._mark_running("s1", os.getpid())
    launch_isolated.prompter.duplicate_choice = "restart"
    runner.run_script(_script())
    assert len(launch_isolated.rec.calls) == 1        # relaunched after kill
    assert os.killpg in killed and os.kill in killed  # SIGTERM escalation attempted


def test_run_script_confirm_denied_aborts(launch_isolated):
    launch_isolated.prompter.confirm_result = False
    runner.run_script(_script(confirm=True))
    assert launch_isolated.rec.calls == []


def test_run_script_port_in_use_aborts_when_declined(launch_isolated, monkeypatch):
    monkeypatch.setattr(runner, "_is_port_in_use", lambda port: True)
    monkeypatch.setattr(runner, "_process_on_port", lambda port: ("node", 1234))
    launch_isolated.prompter.confirm_result = False
    runner.run_script(_script(port="3000"))
    assert launch_isolated.rec.calls == []
    assert launch_isolated.prompter.calls[0] == ("confirm", "Kill node (PID 1234) on port :3000?")


def test_run_script_port_in_use_proceeds_when_confirmed(launch_isolated, monkeypatch):
    killed = []
    monkeypatch.setattr(runner, "_is_port_in_use", lambda port: True)
    monkeypatch.setattr(runner, "_process_on_port", lambda port: ("node", 1234))
    monkeypatch.setattr(runner, "kill_port", lambda port: killed.append(port) or True)
    launch_isolated.prompter.confirm_result = True
    runner.run_script(_script(port="3000"))
    assert len(launch_isolated.rec.calls) == 1
    assert killed == [3000]


# -- stop_script (consolidated: single source of truth for tray + manager) -----

def test_stop_script_noop_on_untracked(monkeypatch, tmp_path):
    rs = tmp_path / "run_state.json"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"scripts": [], "groups": [], "global_env": []}))
    for mod, name, val in [(common, "RUN_STATE_FILE", rs), (runner, "RUN_STATE_FILE", rs),
                           (common, "RUN_LOCK_FILE", tmp_path / ".run.lock"),
                           (common, "STATE_DIR", tmp_path), (common, "CONFIG_FILE", cfg)]:
        monkeypatch.setattr(mod, name, val)
    runner.stop_script("nope")            # not tracked: must not raise
    assert not rs.exists() or "nope" not in rs.read_text()


def test_stop_script_kills_tracked_and_marks_stopped(monkeypatch, tmp_path):
    rs = tmp_path / "run_state.json"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"scripts": [], "groups": [], "global_env": []}))
    for mod, name, val in [(common, "RUN_STATE_FILE", rs), (runner, "RUN_STATE_FILE", rs),
                           (common, "RUN_LOCK_FILE", tmp_path / ".run.lock"),
                           (common, "STATE_DIR", tmp_path), (common, "CONFIG_FILE", cfg),
                           (common, "LOG_DIR", tmp_path / "logs")]:
        monkeypatch.setattr(mod, name, val)
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(runner.os, "kill", lambda *a: None)
    monkeypatch.setattr(runner.os, "killpg", lambda *a: None)
    monkeypatch.setattr(runner.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    runner._mark_running("s1", os.getpid())
    assert "s1" in json.loads(rs.read_text())
    runner.stop_script("s1")
    assert "s1" not in json.loads(rs.read_text())   # marked stopped


# -- _process_on_port / find_ports_for_pid (ss integration) --------------------

SS_LINE = 'LISTEN 0 511 *:3000 *:* users:(("node",pid=1234,fd=5))'


def _ss_run(stdout=""):
    return lambda *a, **k: types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def test_process_on_port_finds_listener(monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run", _ss_run(SS_LINE))
    assert runner._process_on_port(3000) == ("node", 1234)


def test_process_on_port_returns_none_when_no_match(monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run", _ss_run("nothing here"))
    assert runner._process_on_port(3000) == (None, None)


def test_process_on_port_swallows_ss_failure(monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no ss")))
    assert runner._process_on_port(3000) == (None, None)


def test_find_ports_for_pid_collects_listening_ports(monkeypatch):
    ss_out = (
        'LISTEN 0 511 *:3000 *:* users:(("node",pid=1234,fd=5))\n'
        'LISTEN 0 511 127.0.0.1:5432 *:* users:(("postgres",pid=1234,fd=3))'
    )
    monkeypatch.setattr(runner.subprocess, "run", _ss_run(ss_out))
    monkeypatch.setattr(runner, "_descendant_pids", lambda pid: [1234])
    assert sorted(runner.find_ports_for_pid(1234)) == [3000, 5432]
