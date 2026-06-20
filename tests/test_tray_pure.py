"""Tests for pure helpers in tray.py.

tray imports GTK/AppIndicator at module load; on a headless CI box those may be
missing, so we skip the whole module rather than fail.
"""
import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("gi")
try:
    import tray
except Exception:  # pragma: no cover - environment without GTK/AppIndicator
    pytest.skip("GTK/AppIndicator unavailable", allow_module_level=True)


# -- _is_substring_match (used by /proc fallback scan) ------------------------

def test_match_exact():
    assert tray._is_substring_match("npm run dev", "npm run dev")


def test_match_after_path_prefix():
    assert tray._is_substring_match("/usr/bin/npm run dev", "npm run dev")


def test_no_partial_token_match():
    assert not tray._is_substring_match("npm run development", "npm run dev")


def test_match_with_trailing_boundary():
    assert tray._is_substring_match("npm run dev | tee log", "npm run dev")


# -- ss output port parsing ---------------------------------------------------

SS_NODE = 'LISTEN 0 511 *:3000 *:* users:(("node",pid=1234,fd=20))'
SS_PG = 'LISTEN 0 511 127.0.0.1:5432 *:* users:(("postgres",pid=1234,fd=5))'


def test_parse_line_matching_pid():
    assert tray._parse_ports_from_line(SS_NODE, "1234") == [3000]


def test_parse_line_non_matching_pid():
    assert tray._parse_ports_from_line(SS_NODE, "9999") == []


def test_extract_multiple_ports_for_pid():
    out = "\n".join([SS_NODE, SS_PG])
    assert sorted(tray._extract_ports_from_ss(out, "1234")) == [3000, 5432]


# -- _descendant_pids (full process tree walk) --------------------------------

def test_descendant_pids_includes_self():
    assert os.getpid() in tray._descendant_pids(os.getpid())


def test_descendant_pids_finds_grandchild():
    # sh -> sh (subshell) -> sleep : the grandchild must be discovered, which the
    # old one-level pgrep -P walk could not do.
    proc = subprocess.Popen(
        [os.environ.get("SHELL", "/bin/sh"), "-c", "sh -c 'sleep 5'"],
    )
    try:
        time.sleep(0.5)  # let the subshell fork its child
        tree = tray._descendant_pids(proc.pid)
        assert proc.pid in tree
        # At least one descendant beyond the direct child exists in the tree.
        assert len(tree) >= 2
    finally:
        proc.kill()
        proc.wait()


def test_descendant_pids_unknown_pid_returns_only_self():
    # A PID with no children/parent entries yields just itself.
    assert tray._descendant_pids(2_000_000_000) == [2_000_000_000]


# -- launcher self-delete + stale tempfile sweep ------------------------------

def test_write_launcher_self_deletes_and_is_executable():
    path = tray._write_launcher("true", "/tmp", "", "/tmp/x.pid", login_shell=False)
    try:
        body = open(path).read()
        assert f"rm -f {tray.shlex.quote(path)}" in body
        assert oct(os.stat(path).st_mode)[-3:] == "700"
    finally:
        os.unlink(path)


def test_cleanup_stale_tempfiles_removes_only_old(tmp_path, monkeypatch):
    monkeypatch.setattr(tray.tempfile, "gettempdir", lambda: str(tmp_path))
    old_sh = tmp_path / "lazylauncher-abc.sh"
    old_pid = tmp_path / "lazylauncher-abc.pid"
    fresh = tmp_path / "lazylauncher-new.sh"
    unrelated = tmp_path / "keepme.sh"
    for f in (old_sh, old_pid, fresh, unrelated):
        f.write_text("x")
    old = time.time() - 48 * 3600
    os.utime(old_sh, (old, old))
    os.utime(old_pid, (old, old))
    tray._cleanup_stale_tempfiles()
    assert not old_sh.exists() and not old_pid.exists()
    assert fresh.exists()        # recent launcher left alone
    assert unrelated.exists()    # non-lazylauncher file untouched
