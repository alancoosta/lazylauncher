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

pytest.importorskip("gi")
try:
    import runner
    import common
except Exception:  # pragma: no cover - environment without GTK
    pytest.skip("GTK unavailable", allow_module_level=True)


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
