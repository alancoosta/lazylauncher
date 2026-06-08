#!/usr/bin/env python3
"""
LazyLauncher - unified entry point.
  lazylauncher          → start the tray daemon
  lazylauncher manage   → open the manager GUI
"""
import ctypes
import sys
from pathlib import Path

# Set process name so system monitor shows "lazylauncher" instead of "python3"
ctypes.CDLL("libc.so.6").prctl(15, b"lazylauncher", 0, 0, 0)  # PR_SET_NAME

# Ensure the install directory is importable
sys.path.insert(0, str(Path(__file__).parent))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "manage":
        sys.argv = [sys.argv[0]] + sys.argv[2:]  # strip "manage" from argv
        from manager import main as manager_main
        manager_main()
    else:
        from tray import main as tray_main
        tray_main()


if __name__ == "__main__":
    main()
