"""Tests for the GTK-free helpers in common.py (run anywhere, no display needed)."""
import os
import time

import common


# -- normalize_env_vars -------------------------------------------------------

def test_normalize_list_passthrough():
    assert common.normalize_env_vars([{"key": "A", "value": "1"}]) == [{"key": "A", "value": "1"}]


def test_normalize_legacy_string_drops_tokens_without_eq():
    assert common.normalize_env_vars("A=1 garbage B=2") == [
        {"key": "A", "value": "1"}, {"key": "B", "value": "2"}]


def test_normalize_empty_key_skipped():
    assert common.normalize_env_vars([{"key": "  ", "value": "x"}]) == []


def test_normalize_list_value_keeps_spaces():
    assert common.normalize_env_vars([{"key": "MSG", "value": "hello world"}]) == [
        {"key": "MSG", "value": "hello world"}]


def test_normalize_garbage_type():
    assert common.normalize_env_vars(123) == []


# -- _safe_write --------------------------------------------------------------

def test_safe_write_atomic_perms_no_leftover(tmp_path):
    p = tmp_path / "c.json"
    common._safe_write(p, '{"a":1}')
    assert p.read_text() == '{"a":1}'
    assert oct(p.stat().st_mode)[-3:] == "600"
    assert list(tmp_path.glob("*.tmp")) == []


def test_safe_write_bytes(tmp_path):
    p = tmp_path / "b.bin"
    common._safe_write(p, b"\x00\x01\x02")
    assert p.read_bytes() == b"\x00\x01\x02"


# -- rotate_log ---------------------------------------------------------------

def test_rotate_log_keeps_one_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "MAX_LOG_SIZE", 5)
    lg = tmp_path / "s.log"
    lg.write_text("0123456789")
    common.rotate_log(lg)
    assert not lg.exists()
    assert (tmp_path / "s.log.1").read_text() == "0123456789"


def test_rotate_log_small_file_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "MAX_LOG_SIZE", 1000)
    lg = tmp_path / "s.log"
    lg.write_text("hi")
    common.rotate_log(lg)
    assert lg.read_text() == "hi"


def test_rotate_log_deletes_aged_out(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "MAX_LOG_AGE", 1)
    lg = tmp_path / "s.log"
    lg.write_text("x")
    old = time.time() - 100
    os.utime(lg, (old, old))
    common.rotate_log(lg)
    assert not lg.exists()


# -- config_lock --------------------------------------------------------------

def test_config_lock_acquire_release(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(common, "LOCK_FILE", tmp_path / ".lock")
    with common.config_lock():
        pass
    # Reacquiring after release must not block.
    with common.config_lock():
        pass


# -- ensure_seed_config -------------------------------------------------------

def test_seed_only_when_absent(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(common, "CONFIG_FILE", cfg)
    common.ensure_seed_config()
    first = cfg.read_text()
    assert "Dev environment" in first
    # Second call must be a no-op (don't clobber user edits).
    cfg.write_text('{"scripts": [], "groups": []}')
    common.ensure_seed_config()
    assert cfg.read_text() == '{"scripts": [], "groups": []}'
