#!/usr/bin/env python3
"""
LazyLauncher - unified entry point.
  lazylauncher              → start the tray daemon
  lazylauncher manage       → open the manager GUI
  lazylauncher run <id>     → run a configured script by id (for global hotkeys)
  lazylauncher --version    → print version
  lazylauncher --config-path→ print the config file path
  lazylauncher --uninstall  → remove installed files (keeps config; add --purge to wipe)
"""
import ctypes
import sys
from pathlib import Path

# Set process name so system monitor shows "lazylauncher" instead of "python3"
try:
    ctypes.CDLL("libc.so.6").prctl(15, b"lazylauncher", 0, 0, 0)  # PR_SET_NAME
except Exception:
    pass


def _uninstall(purge: bool):
    """Remove files placed by install.sh. Keep user config unless --purge."""
    import shutil
    home = Path.home()
    targets = [
        home / ".local/share/lazylauncher",
        home / ".local/bin/lazylauncher",
        home / ".config/autostart/lazylauncher.desktop",
        home / ".local/share/applications/lazylauncher.desktop",
        home / ".local/share/icons/hicolor/scalable/apps/lazylauncher.svg",
    ]
    for size in (48, 64, 128, 256, 512):
        targets.append(home / f".local/share/icons/hicolor/{size}x{size}/apps/lazylauncher.png")
    if purge:
        from .common import CONFIG_DIR, STATE_DIR
        targets += [CONFIG_DIR, STATE_DIR]

    for t in targets:
        try:
            if t.is_dir():
                shutil.rmtree(t)
                print(f"removed dir   {t}")
            elif t.exists():
                t.unlink()
                print(f"removed       {t}")
        except OSError as e:
            print(f"skip {t}: {e}")
    if not purge:
        print("\nUser config kept. Re-run with --purge to also remove config and state.")
    print("Uninstalled. Note: a running tray (if any) keeps going until you log out or kill it.")


def _run_by_id(script_id: str) -> int:
    """Run a configured script by id, without opening the manager."""
    from .common import load_config
    script = next((s for s in load_config().get("scripts", []) if s.get("id") == script_id), None)
    if script is None:
        print(f"No script with id '{script_id}'", file=sys.stderr)
        return 1
    from .tray import run_script
    run_script(script)
    return 0


def main():
    args = sys.argv[1:]

    # GTK-free fast paths (work even without a display / GTK installed).
    if args and args[0] in ("--version", "-V"):
        from .common import VERSION
        print(f"lazylauncher {VERSION}")
        return
    if args and args[0] == "--config-path":
        from .common import CONFIG_FILE
        print(CONFIG_FILE)
        return
    if args and args[0] == "--uninstall":
        _uninstall(purge="--purge" in args)
        return
    if args and args[0] in ("--help", "-h"):
        print(__doc__.strip())
        return

    if args and args[0] == "run":
        if len(args) < 2:
            print("usage: lazylauncher run <script-id>", file=sys.stderr)
            sys.exit(2)
        sys.exit(_run_by_id(args[1]))

    if args and args[0] == "manage":
        sys.argv = [sys.argv[0]] + args[1:]  # strip "manage" from argv
        from .manager import main as manager_main
        manager_main()
    else:
        from .tray import main as tray_main
        tray_main()


if __name__ == "__main__":
    main()
