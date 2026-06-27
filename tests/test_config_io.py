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


def test_merge_reassociates_existing_scripts_to_imported_group():
    # A script that already lives here (same id) is skipped on import, but the
    # imported group should still claim it — otherwise the imported group looks
    # empty even though the export said the script belongs to it.
    cfg = {"scripts": [{"id": "s1", "name": "local", "groups": []}], "groups": []}
    imported = {"scripts": [{"id": "s1", "name": "remote", "groups": ["g1"]}],
                "groups": [{"id": "g1", "name": "G1"}]}
    out, added = config_io.merge_imported(cfg, imported)
    assert added == 0
    assert [g["id"] for g in out["groups"]] == ["g1"]
    s1 = next(s for s in out["scripts"] if s["id"] == "s1")
    assert "g1" in s1["groups"]            # re-associated to the imported group
    assert s1["name"] == "local"           # existing script not overwritten


def test_merge_does_not_duplicate_group_membership():
    cfg = {"scripts": [{"id": "s1", "groups": ["g1"]}], "groups": [{"id": "g1"}]}
    imported = {"scripts": [{"id": "s1", "groups": ["g1"]}], "groups": [{"id": "g1"}]}
    out, _ = config_io.merge_imported(cfg, imported)
    s1 = next(s for s in out["scripts"] if s["id"] == "s1")
    assert s1["groups"] == ["g1"]          # not ["g1", "g1"]


def test_read_config_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"scripts": [{"id": "z"}], "groups": []}))
    assert config_io.read_config_file(p)["scripts"][0]["id"] == "z"
