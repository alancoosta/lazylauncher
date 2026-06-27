"""Tests for config_io.py — the GTK-free import merge logic."""
import json

from lazylauncher import config_io


def test_merge_adds_new_scripts_and_groups():
    cfg = {"scripts": [{"id": "a"}], "groups": [{"id": "g1"}]}
    imported = {"scripts": [{"id": "b"}, {"id": "c"}], "groups": [{"id": "g2"}]}
    out, added = config_io.merge_imported(cfg, imported)
    assert added == 2
    assert [s["id"] for s in out["scripts"]] == ["a", "b", "c"]
    assert [g["id"] for g in out["groups"]] == ["g1", "g2"]


def test_merge_skips_existing_ids():
    cfg = {"scripts": [{"id": "a"}], "groups": [{"id": "g1"}]}
    imported = {"scripts": [{"id": "a"}, {"id": "b"}], "groups": [{"id": "g1"}]}
    out, added = config_io.merge_imported(cfg, imported)
    assert added == 1
    assert [s["id"] for s in out["scripts"]] == ["a", "b"]
    assert [g["id"] for g in out["groups"]] == ["g1"]


def test_merge_empty_import_is_noop():
    cfg = {"scripts": [{"id": "a"}], "groups": []}
    out, added = config_io.merge_imported(cfg, {})
    assert added == 0
    assert out["scripts"] == [{"id": "a"}]


def test_merge_into_empty_config():
    cfg = {"scripts": [], "groups": []}
    out, added = config_io.merge_imported(cfg, {"scripts": [{"id": "x"}]})
    assert added == 1
    assert out["scripts"] == [{"id": "x"}]


def test_merge_skips_entries_without_id():
    # A malformed imported entry (no id) is skipped, not a KeyError crash.
    cfg = {"scripts": [{"id": "a"}], "groups": [{"id": "g1"}]}
    imported = {"scripts": [{"name": "no id"}, {"id": "b"}],
                "groups": [{"name": "no id"}, {"id": "g2"}]}
    out, added = config_io.merge_imported(cfg, imported)
    assert added == 1
    assert [s["id"] for s in out["scripts"]] == ["a", "b"]
    assert [g["id"] for g in out["groups"]] == ["g1", "g2"]


def test_read_config_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"scripts": [{"id": "z"}], "groups": []}))
    assert config_io.read_config_file(p)["scripts"][0]["id"] == "z"
