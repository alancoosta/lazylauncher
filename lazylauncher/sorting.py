#!/usr/bin/env python3
"""sorting.py — pure ordering helpers for LazyLauncher scripts.

GTK-free so it can be unit-tested without a display. Both the module-level
``ManagerWindow`` sort and ``GroupRow``'s per-group sort funnel through here,
which is also why the two slightly different port-key implementations that used
to live in manager.py are now one.
"""

SORT_MODES = (
    "name_asc", "name_desc",
    "port_asc", "port_desc",
    "running_first", "stopped_first",
)


def port_sort_key(value) -> int:
    """Numeric sort key for a port string ('' / non-numeric sort as 0)."""
    p = str(value or "").strip()
    return int(p) if p.isdigit() else 0


def sort_scripts(scripts, mode, running_ids=frozenset()) -> list:
    """Return ``scripts`` ordered by ``mode`` (a copy; input is not mutated).

    Unknown modes return the list unchanged. ``running_ids`` is only consulted
    for the running/stopped modes. The sort is stable, so scripts that compare
    equal keep their original relative order.
    """
    s = list(scripts)
    if mode == "name_asc":
        s.sort(key=lambda x: x.get("name", "").lower())
    elif mode == "name_desc":
        s.sort(key=lambda x: x.get("name", "").lower(), reverse=True)
    elif mode == "port_asc":
        s.sort(key=lambda x: port_sort_key(x.get("port", "")))
    elif mode == "port_desc":
        s.sort(key=lambda x: port_sort_key(x.get("port", "")), reverse=True)
    elif mode == "running_first":
        s.sort(key=lambda x: x.get("id", "") not in running_ids)
    elif mode == "stopped_first":
        s.sort(key=lambda x: x.get("id", "") in running_ids)
    return s


GROUP_SORT_MODES = (
    "name_asc", "name_desc",
    "count_asc", "count_desc",
    "running_first", "stopped_first",
)


def sort_groups(groups, mode, scripts=(), running_ids=frozenset()) -> list:
    """Return ``groups`` ordered by ``mode`` (a copy; input is not mutated).

    ``scripts`` and ``running_ids`` are consulted for the count / running modes
    (how many enabled scripts belong to each group, and whether any is running).
    Unknown modes return the list unchanged.
    """
    g = list(groups)

    def _script_count(grp):
        gid = grp.get("id", "")
        return len([s for s in scripts
                    if gid in s.get("groups", []) and s.get("enabled", True)])

    def _any_running(grp):
        gid = grp.get("id", "")
        return any(s.get("id", "") in running_ids
                   for s in scripts
                   if gid in s.get("groups", []) and s.get("enabled", True))

    if mode == "name_asc":
        g.sort(key=lambda x: x.get("name", "").lower())
    elif mode == "name_desc":
        g.sort(key=lambda x: x.get("name", "").lower(), reverse=True)
    elif mode == "count_asc":
        g.sort(key=_script_count)
    elif mode == "count_desc":
        g.sort(key=_script_count, reverse=True)
    elif mode == "running_first":
        g.sort(key=lambda x: not _any_running(x))
    elif mode == "stopped_first":
        g.sort(key=lambda x: _any_running(x))
    return g
