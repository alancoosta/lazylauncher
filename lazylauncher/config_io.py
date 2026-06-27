#!/usr/bin/env python3
"""config_io.py — import/export helpers for the manager (GTK-free, testable).

The file-dialog plumbing stays in the UI; the data logic (parsing a config file
and merging its scripts/groups into the current config) lives here so it can be
unit-tested without a display.
"""
import json
import shutil

from .common import CONFIG_FILE


def read_config_file(path) -> dict:
    """Parse a config JSON file and return the dict."""
    with open(path) as f:
        return json.load(f)


def merge_imported(cfg: dict, imported: dict):
    """Merge ``imported``'s scripts and groups into ``cfg`` by id.

    Entries whose id already exists in ``cfg`` are skipped. ``cfg`` is mutated in
    place and also returned, alongside the number of scripts actually added.

    A script that already lived here (same id) is not overwritten, but it IS
    re-associated with any imported group the export said it belongs to —
    otherwise an imported group would look empty when its scripts already
    existed locally.
    """
    existing_ids = {s.get("id") for s in cfg.get("scripts", [])}
    added = 0
    for s in imported.get("scripts", []):
        sid = s.get("id")
        if sid is not None and sid not in existing_ids:
            cfg.setdefault("scripts", []).append(s)
            added += 1
    existing_gids = {g.get("id") for g in cfg.get("groups", [])}
    for g in imported.get("groups", []):
        gid = g.get("id")
        if gid is not None and gid not in existing_gids:
            cfg.setdefault("groups", []).append(g)

    # Re-associate: copy imported-group memberships onto scripts that already
    # existed here (they were skipped above) so the imported group isn't empty.
    imported_gids = {g.get("id") for g in imported.get("groups", [])
                     if g.get("id") is not None}
    if imported_gids:
        src_groups = {s.get("id"): s.get("groups", []) for s in imported.get("scripts", [])}
        for s in cfg.get("scripts", []):
            for gid in src_groups.get(s.get("id"), []):
                if gid in imported_gids:
                    groups = s.setdefault("groups", [])
                    if gid not in groups:
                        groups.append(gid)
    return cfg, added


def export_config_to(path):
    """Copy the active config file to ``path``."""
    shutil.copy2(str(CONFIG_FILE), path)
