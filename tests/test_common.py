"""Tests for the GTK-free helpers in common.py (run anywhere, no display needed)."""
import json
import os
import time

from lazylauncher import common


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


def test_normalize_preserves_global_marker():
    # A live reference keeps the marker and drops any value.
    assert common.normalize_env_vars([{"key": "A", "global": True, "value": "ignored"}]) == [
        {"key": "A", "global": True}]


def test_normalize_preserves_alias_ref():
    # An alias names the pool key to reference; the value is dropped.
    assert common.normalize_env_vars([{"key": "DB_URL", "global": "DATABASE_URL", "value": "ignored"}]) == [
        {"key": "DB_URL", "global": "DATABASE_URL"}]


def test_normalize_non_string_global_falls_back_to_legacy():
    # A truthy-but-not-string global behaves like the legacy same-key reference.
    assert common.normalize_env_vars([{"key": "A", "global": 1}]) == [{"key": "A", "global": True}]


# -- global_env_map / resolve_env_vars ---------------------------------------

def test_global_env_map_from_cfg():
    cfg = {"global_env": [{"key": "API", "value": "https://x"}, {"key": "  ", "value": "skip"}]}
    assert common.global_env_map(cfg) == {"API": "https://x"}


def test_resolve_reference_pulls_from_pool():
    items = [{"key": "API", "global": True}]
    assert common.resolve_env_vars(items, {"API": "https://x"}) == [
        {"key": "API", "value": "https://x"}]


def test_resolve_orphan_reference_dropped():
    # A reference whose key is gone from the pool is simply not injected.
    assert common.resolve_env_vars([{"key": "GONE", "global": True}], {}) == []


def test_resolve_own_value_passthrough_and_mix():
    items = [{"key": "API", "global": True}, {"key": "LOCAL", "value": "1"}]
    assert common.resolve_env_vars(items, {"API": "https://x"}) == [
        {"key": "API", "value": "https://x"}, {"key": "LOCAL", "value": "1"}]


def test_resolve_alias_pulls_from_other_key():
    # An alias local key takes another global's value.
    items = [{"key": "DB_URL", "global": "DATABASE_URL"}]
    assert common.resolve_env_vars(items, {"DATABASE_URL": "postgres://x"}) == [
        {"key": "DB_URL", "value": "postgres://x"}]


def test_resolve_alias_orphan_dropped():
    # An alias whose referenced global is gone is not injected.
    assert common.resolve_env_vars([{"key": "DB_URL", "global": "MISSING"}], {}) == []


def test_resolve_alias_mixed_with_own_value():
    items = [{"key": "DB_URL", "global": "DATABASE_URL"}, {"key": "PORT", "value": "5432"}]
    assert common.resolve_env_vars(items, {"DATABASE_URL": "postgres://x"}) == [
        {"key": "DB_URL", "value": "postgres://x"}, {"key": "PORT", "value": "5432"}]


# -- ui_state (last-used tab) -------------------------------------------------

def _patch_ui_state(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path)
    monkeypatch.setattr(common, "UI_STATE_FILE", tmp_path / "ui_state.json")


def test_ui_state_missing_returns_empty(monkeypatch, tmp_path):
    _patch_ui_state(monkeypatch, tmp_path)
    assert common.load_ui_state() == {}


def test_ui_state_roundtrip_and_merge(monkeypatch, tmp_path):
    _patch_ui_state(monkeypatch, tmp_path)
    common.save_ui_state(view="detail")
    common.save_ui_state(home_tab="groups")  # merges, does not clobber
    assert common.load_ui_state() == {"view": "detail", "home_tab": "groups"}


def test_ui_state_corrupt_file_returns_empty(monkeypatch, tmp_path):
    _patch_ui_state(monkeypatch, tmp_path)
    (tmp_path / "ui_state.json").write_text("{not json")
    assert common.load_ui_state() == {}


def test_ui_state_non_dict_returns_empty(monkeypatch, tmp_path):
    _patch_ui_state(monkeypatch, tmp_path)
    (tmp_path / "ui_state.json").write_text("[1, 2, 3]")
    assert common.load_ui_state() == {}


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


# -- _is_pid_alive (phantom guard) --------------------------------------------

def test_is_pid_alive_rejects_zero():
    # os.kill(0, 0) signals our own process group and would lie "alive".
    assert common._is_pid_alive(0) is False
    assert common._is_pid_alive(-1) is False


def test_is_pid_alive_self_is_true():
    assert common._is_pid_alive(os.getpid()) is True


def test_get_running_ids_prunes_invalid_pid(tmp_path, monkeypatch):
    rs = tmp_path / "run_state.json"
    monkeypatch.setattr(common, "STATE_DIR", tmp_path)
    monkeypatch.setattr(common, "RUN_STATE_FILE", rs)
    monkeypatch.setattr(common, "RUN_LOCK_FILE", tmp_path / ".run.lock")
    # A phantom pid-0 entry and a real one (our own pid).
    rs.write_text('{"ghost": {"pid": 0, "start_time": ""}, '
                  f'"real": {{"pid": {os.getpid()}, "start_time": ""}}}}')
    assert common.get_running_ids() == {"real"}
    # The phantom was pruned from disk too.
    import json
    assert "ghost" not in json.loads(rs.read_text())


# -- load_config / save_config corruption safety ------------------------------

def test_load_config_missing_returns_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(common, "CONFIG_FILE", tmp_path / "cfg.json")
    monkeypatch.setattr(common, "CONFIG_BAK", tmp_path / "cfg.json.bak")
    assert common.load_config() == {"scripts": [], "groups": [], "global_env": []}


def test_save_config_keeps_backup(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    bak = tmp_path / "cfg.json.bak"
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(common, "CONFIG_FILE", cfg)
    monkeypatch.setattr(common, "CONFIG_BAK", bak)
    common.save_config({"scripts": [{"id": "a"}], "groups": []})
    common.save_config({"scripts": [{"id": "b"}], "groups": []})
    import json
    assert json.loads(bak.read_text())["scripts"][0]["id"] == "a"
    assert json.loads(cfg.read_text())["scripts"][0]["id"] == "b"


def test_load_config_corrupt_preserves_and_recovers_from_bak(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    bak = tmp_path / "cfg.json.bak"
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(common, "CONFIG_FILE", cfg)
    monkeypatch.setattr(common, "CONFIG_BAK", bak)
    bak.write_text('{"scripts": [{"id": "good"}], "groups": []}')
    cfg.write_text("{ this is not json")
    recovered = common.load_config()
    assert recovered["scripts"][0]["id"] == "good"
    # Corrupt file was preserved aside, not silently discarded.
    assert list(tmp_path.glob(".lazylauncher-config.corrupt-*.json")) != []
    # Recovery repaired the on-disk config from .bak, so a later save_config does
    # NOT copy the (formerly corrupt) file over the last-good backup.
    assert json.loads(cfg.read_text())["scripts"][0]["id"] == "good"
    common.save_config({"scripts": [{"id": "new"}], "groups": []})
    assert json.loads(bak.read_text())["scripts"][0]["id"] == "good"


def test_run_state_lock_acquire_release(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path)
    monkeypatch.setattr(common, "RUN_LOCK_FILE", tmp_path / ".run.lock")
    with common.run_state_lock():
        pass
    with common.run_state_lock():
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


# -- scripts_in_group / normalize_script ---------------------------------------

def test_scripts_in_group_filters_enabled():
    scripts = [
        {"id": "s1", "groups": ["g1"], "enabled": True},
        {"id": "s2", "groups": ["g1"], "enabled": False},
        {"id": "s3", "groups": ["g2"], "enabled": True},
    ]
    assert [s["id"] for s in common.scripts_in_group(scripts, "g1")] == ["s1"]


def test_scripts_in_group_includes_disabled_when_asked():
    scripts = [{"id": "s1", "groups": ["g1"], "enabled": False}]
    out = common.scripts_in_group(scripts, "g1", only_enabled=False)
    assert [s["id"] for s in out] == ["s1"]


def test_normalize_script_drops_legacy_keys():
    s = {"id": "x", "icon": "foo.png", "pinned_icon": True, "name": "X"}
    out = common.normalize_script(s)
    assert "icon" not in out and "pinned_icon" not in out
    assert out["name"] == "X"


# -- get_logger (must never raise, even when STATE_DIR is unwritable) -----------

def test_get_logger_tolerates_unwritable_state_dir(monkeypatch, tmp_path):
    import logging
    log = logging.getLogger("lazylauncher")
    saved_handlers = list(log.handlers)
    saved_logger = common._logger
    log.handlers.clear()
    monkeypatch.setattr(common, "_logger", None)
    monkeypatch.setattr(common, "STATE_DIR", tmp_path)

    def boom_handler(*a, **k):
        raise OSError("cannot open log file")

    monkeypatch.setattr(common, "RotatingFileHandler", boom_handler)
    try:
        out = common.get_logger()                       # must not raise
        assert isinstance(out, logging.Logger)
        assert any(isinstance(h, logging.NullHandler) for h in out.handlers)
    finally:
        log.handlers.clear()
        log.handlers.extend(saved_handlers)
        common._logger = saved_logger
