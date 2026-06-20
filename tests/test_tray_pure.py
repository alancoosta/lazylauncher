"""Tests for pure helpers in tray.py.

tray imports GTK/AppIndicator at module load; on a headless CI box those may be
missing, so we skip the whole module rather than fail.
"""
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
