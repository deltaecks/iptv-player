"""Install a downloaded Windows build and add a desktop shortcut."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
MB_OK = 0x00000000
MB_YESNO = 0x00000004
MB_ICONINFORMATION = 0x00000040
IDYES = 6


def prepare_windows_install() -> bool:
    """Copy a downloaded exe into place, shortcut it, and relaunch.

    Returns True when this process should exit because the installed copy was started.
    Source checkouts are left untouched.
    """
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return False

    current = Path(sys.executable).resolve()
    target = _installed_executable().resolve()
    if not _same_path(current, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if _needs_copy(current, target):
                shutil.copy2(current, target)
        except OSError:
            if not target.is_file():
                _message(
                    "IPTV Player could not be copied onto this PC. "
                    "Close any open copy and run the setup again."
                )
                return True
        _create_shortcut(target)
        subprocess.Popen([str(target)], cwd=str(target.parent), close_fds=True)
        return True

    _create_shortcut(target)
    _offer_vlc()
    return False


def _installed_executable() -> Path:
    override = os.environ.get("IPTV_INSTALL_DIR", "").strip()
    folder = Path(override) if override else _local_app_folder()
    return folder / "IPTV Player.exe"


def _local_app_folder() -> Path:
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return Path.home() / "AppData" / "Local" / "IPTV Player"
    return Path(local) / "IPTV Player"


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _needs_copy(current: Path, target: Path) -> bool:
    if not target.is_file():
        return True
    current_info = current.stat()
    target_info = target.stat()
    return (
        current_info.st_size != target_info.st_size
        or current_info.st_mtime > target_info.st_mtime
    )


def _testing_install() -> bool:
    return bool(os.environ.get("IPTV_INSTALL_DIR", "").strip())


def _create_shortcut(target: Path) -> None:
    if _testing_install():
        return
    command = f"""
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'IPTV Player.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = {_ps_quote(str(target))}
$shortcut.WorkingDirectory = {_ps_quote(str(target.parent))}
$shortcut.WindowStyle = 1
$shortcut.Description = 'IPTV Player'
$shortcut.IconLocation = {_ps_quote(str(target) + ',0')}
$shortcut.Save()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )


def _offer_vlc() -> None:
    if _testing_install() or _vlc_present():
        return
    answer = ctypes.windll.user32.MessageBoxW(
        None,
        "IPTV Player needs the free 64-bit VLC player. Install it now?\n\n"
        "Windows may ask for permission. The download can take a few minutes.",
        "IPTV Player",
        MB_YESNO | MB_ICONINFORMATION,
    )
    if answer != IDYES:
        return
    try:
        completed = subprocess.run(
            [
                "winget",
                "install",
                "--id",
                "VideoLAN.VLC",
                "-e",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--disable-interactivity",
            ],
            creationflags=CREATE_NEW_CONSOLE,
        )
        installed = completed.returncode == 0
    except OSError:
        installed = False
    if installed and _vlc_present():
        return
    _message("VLC was not installed. Its website will open so the 64-bit player can be downloaded.")
    os.startfile("https://www.videolan.org/vlc/")


def _vlc_present() -> bool:
    candidates = [
        os.environ.get("VLC_HOME", ""),
        r"C:\Program Files\VideoLAN\VLC",
        r"C:\Program Files (x86)\VideoLAN\VLC",
    ]
    return any(bool(folder) and Path(folder, "libvlc.dll").is_file() for folder in candidates)


def _message(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, "IPTV Player", MB_OK | MB_ICONINFORMATION)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
