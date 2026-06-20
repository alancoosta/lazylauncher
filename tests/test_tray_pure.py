"""Tests for pure helpers in tray.py (the /proc cmdline-match heuristic).

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
