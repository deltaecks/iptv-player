"""Locations for source runs and a PyInstaller build."""

from __future__ import annotations

import sys
from pathlib import Path


def bundle_root() -> Path:
    """Folder that contains assets, whether from source or a frozen build."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def icon_file() -> Path:
    return bundle_root() / "assets" / "iptv-player.ico"
