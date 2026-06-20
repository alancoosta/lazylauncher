<p align="center">
  <img src="icons/logo.svg" alt="LazyLauncher" width="200">
</p>

<h1 align="center">LazyLauncher</h1>

<p align="center">Manage and run shell scripts. Organize with groups, pin favorites, monitor ports, and run silently.</p>

<p align="center">
  <img src="assets/demo.png" alt="LazyLauncher Demo" width="1923">
</p>

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/alancoosta/lazylauncher/main/install.sh | bash
```

Autostart is set up automatically — just log out and back in after install.

## Features

### Scripts
- Tray menu with all your scripts — click to run in a terminal
- Manager UI to add, edit, delete, reorder, duplicate scripts
- Pin scripts as dedicated tray icons
- Per-script working directory and environment variables
- Silent mode — run in background, get notified when done
- Confirmation dialogs for dangerous scripts
- Per-script logs with viewer
- Port monitoring — auto-kill busy ports before running

### Groups
- Organize scripts into groups
- Run/Stop all scripts in a group at once
- **Dependency ordering** — set `depends on` per script; "Run All" starts them in
  topological order, waiting for each dependency's port to accept connections
  before launching the next (cycle-safe). Scripts without a port can't be waited
  on, so they're launched immediately.
- Duplicate groups with all script associations
- Group settings panel with notebook tabs (same layout as scripts)
- Per-group script list with inline actions (Settings, Logs, Run, Stop)
- Port badges visible on each script inside the group card

### Sorting & Filtering
- Filter scripts and groups by name
- Sort scripts by name (A-Z / Z-A), port (1-100 / 100-1), or status (running/stopped first)
- Sort groups by name, script count, or running status
- Sort scripts within a group card independently
- Manual reorder with up/down buttons

### General
- Import/export config between machines
- Hot-reload — tray updates when you save
- Login-shell toggle per script (source `~/.profile` / `~/.bashrc`, or run a clean deterministic shell)

## CLI

```bash
lazylauncher                # start the tray daemon
lazylauncher manage         # open the manager GUI
lazylauncher run <id>       # run a configured script by id (great for global hotkeys)
lazylauncher --version
lazylauncher --config-path  # print the config file path
lazylauncher --uninstall    # remove installed files (keeps config; add --purge to wipe)
```

### Global hotkey

Bind a key to run your favorite script via the CLI. On GNOME:
**Settings → Keyboard → Custom Shortcuts**, command:

```bash
lazylauncher run <script-id>
```

(`lazylauncher --config-path` shows the file where each script's `id` lives.)

## Uninstall

```bash
lazylauncher --uninstall          # keeps your config
lazylauncher --uninstall --purge  # also removes config + state
```

## Paths

- Config: `~/.config/lazylauncher/` (portable — safe to sync/export)
- State (logs, run state, app log): `~/.local/state/lazylauncher/`
