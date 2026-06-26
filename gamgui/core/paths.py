"""Per-OS location for GamGUI's local data.

Keeps the on-disk footprint portable so the app isn't macOS-bound: the audit log, onboarding
runbooks, calendar index, and the ephemeral GAM config all live under one app-data directory that
resolves to the right place per platform — macOS Application Support, Windows %LOCALAPPDATA%,
Linux $XDG_DATA_HOME (or ~/.local/share). (`keyring` already abstracts the secret store per-OS.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "GamGUI"


def app_data_dir() -> Path:
    """The base directory for GamGUI's local data on this OS (not created here)."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:  # linux / other POSIX
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / APP_NAME
