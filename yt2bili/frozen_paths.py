"""
Path resolution helper for PyInstaller-frozen and dev environments.

In a PyInstaller --onefile EXE:
- sys.executable is the EXE path (writable user data goes next to it)
- sys._MEIPASS is the temp extraction directory (read-only bundled data)

In development:
- Everything is relative to the repository root (the package's parent dir)
"""

import sys
from pathlib import Path


def is_frozen() -> bool:
    """Return True when running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def user_data_dir() -> Path:
    """
    Return the writable directory for mutable runtime files.

    - Frozen: next to the EXE (e.g. D:\\yt2bili\\)
    - Dev:    the project root (the package's parent directory)
    """
    if is_frozen():
        return Path(sys.executable).parent.resolve()
    return Path(__file__).resolve().parent.parent
