"""Local settings and Windows DPAPI credential storage."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "IPTV Player"
ORGANIZATION = "IPTVPlayer"
CONFIG_VERSION = 1

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Hardware decoding is required for smooth HD playback. Caching values are
# milliseconds and apply to live .ts streams.
VLC_ARGS = [
    "--avcodec-hw=any",
    "--network-caching=3000",
    "--live-caching=3000",
    "--no-video-title-show",
    "--no-stats",
    "--verbose=-1",
]

AUTH_TIMEOUT = (5, 20)
CATEGORY_TIMEOUT = (5, 40)
STREAM_TIMEOUT = (5, 60)

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class ConfigError(Exception):
    """The local configuration file could not be read or written."""


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def get_config_path() -> Path:
    root = os.environ.get("APPDATA")
    if not root:
        root = str(Path.home() / "AppData" / "Roaming")
    return Path(root) / ORGANIZATION / "config.json"


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _bind_dpapi() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def protect_secret(data: bytes) -> bytes:
    """Encrypt bytes with the current Windows user account via DPAPI."""
    if not _dpapi_available():
        raise ConfigError("Saving a password requires Windows DPAPI.")
    if not data:
        raise ConfigError("Cannot protect an empty secret.")
    crypt32, kernel32 = _bind_dpapi()
    source = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "IPTV Player credentials",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ConfigError("Windows could not encrypt the saved password.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def unprotect_secret(data: bytes) -> bytes:
    """Decrypt a DPAPI blob created by protect_secret."""
    if not _dpapi_available():
        raise ConfigError("Reading the saved password requires Windows DPAPI.")
    if not data:
        raise ConfigError("Saved password data is empty.")
    crypt32, kernel32 = _bind_dpapi()
    source = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ConfigError("Windows could not decrypt the saved password.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _clean_favourites(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    cleaned: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        section = str(entry.get("section") or "")
        kind = str(entry.get("kind") or "")
        item_id = str(entry.get("item_id") or "").strip()
        name = str(entry.get("name") or "").strip()
        server_url = str(entry.get("server_url") or "").strip()
        username = str(entry.get("username") or "").strip()
        if section not in {"live", "movies", "series"} or kind not in {"live", "movie", "series"}:
            continue
        if not item_id or not name or not server_url or not username:
            continue
        key = (server_url, username, section, item_id)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(
            {
                "section": section,
                "kind": kind,
                "item_id": item_id,
                "name": name[:200],
                "icon_url": str(entry.get("icon_url") or "")[:1000],
                "category_id": str(entry.get("category_id") or ""),
                "extension": str(entry.get("extension") or "")[:8],
                "direct_url": str(entry.get("direct_url") or "")[:2000],
                "server_url": server_url,
                "username": username,
            }
        )
        if len(cleaned) >= 500:
            break
    return cleaned


def _clamp_volume(value: object) -> int:
    try:
        volume = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 80
    return max(0, min(100, volume))


@dataclass
class AppConfig:
    volume: int = 80
    muted: bool = False
    last_section: str = "live"
    last_category_id: str = ""
    last_live_category_id: str = ""
    last_movie_category_id: str = ""
    last_series_category_id: str = ""
    last_stream_id: str = ""
    last_stream_name: str = ""
    last_kind: str = ""
    last_series_id: str = ""
    favourites: list | None = None
    server_url: str = ""
    username: str = ""
    password: str = ""
    credentials_readable: bool = True

    @property
    def has_credentials(self) -> bool:
        return bool(self.server_url and self.username and self.password)

    def clear_credentials(self) -> None:
        self.server_url = ""
        self.username = ""
        self.password = ""
        self.last_section = "live"
        self.last_category_id = ""
        self.last_live_category_id = ""
        self.last_movie_category_id = ""
        self.last_series_category_id = ""
        self.last_stream_id = ""
        self.last_stream_name = ""
        self.last_kind = ""
        self.last_series_id = ""
        self.credentials_readable = True

    def __post_init__(self) -> None:
        if self.favourites is None:
            self.favourites = []

    def favourites_for(self, section: str) -> list[dict]:
        return [
            entry
            for entry in self.favourites or []
            if entry.get("section") == section
            and entry.get("server_url") == self.server_url
            and entry.get("username") == self.username
        ]

    def is_favourite(self, section: str, item_id: str) -> bool:
        return any(entry.get("item_id") == item_id for entry in self.favourites_for(section))

    def toggle_favourite(self, entry: dict) -> bool:
        """Add the entry, or remove it if it is already saved. Returns True when it is now saved."""
        if self.favourites is None:
            self.favourites = []
        key = (
            entry.get("server_url"),
            entry.get("username"),
            entry.get("section"),
            entry.get("item_id"),
        )
        kept = [
            existing
            for existing in self.favourites
            if (
                existing.get("server_url"),
                existing.get("username"),
                existing.get("section"),
                existing.get("item_id"),
            )
            != key
        ]
        added = len(kept) == len(self.favourites)
        if added:
            kept.append(entry)
        self.favourites = kept[-500:]
        return added

    def save(self) -> None:
        path = get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        protected: str | None = None
        if self.server_url or self.username or self.password:
            payload = json.dumps(
                {
                    "server_url": self.server_url,
                    "username": self.username,
                    "password": self.password,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            protected = base64.b64encode(protect_secret(payload)).decode("ascii")
        document = {
            "version": CONFIG_VERSION,
            "volume": _clamp_volume(self.volume),
            "muted": bool(self.muted),
            "last_section": self.last_section,
            "last_category_id": self.last_category_id,
            "last_live_category_id": self.last_live_category_id,
            "last_movie_category_id": self.last_movie_category_id,
            "last_series_category_id": self.last_series_category_id,
            "last_stream_id": self.last_stream_id,
            "last_stream_name": self.last_stream_name,
            "last_kind": self.last_kind,
            "last_series_id": self.last_series_id,
            "favourites": self.favourites or [],
            "protected_credentials": protected,
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def load(cls) -> AppConfig:
        path = get_config_path()
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        last_category_id = str(raw.get("last_category_id") or "")
        last_live_category_id = str(raw.get("last_live_category_id") or "") or last_category_id
        last_section = str(raw.get("last_section") or "live")
        if last_section not in {"live", "movies", "series"}:
            last_section = "live"
        last_kind = str(raw.get("last_kind") or "")
        if last_kind not in {"", "live", "movie", "episode"}:
            last_kind = ""
        config = cls(
            volume=_clamp_volume(raw.get("volume", 80)),
            muted=bool(raw.get("muted", False)),
            last_section=last_section,
            last_category_id=last_category_id,
            last_live_category_id=last_live_category_id,
            last_movie_category_id=str(raw.get("last_movie_category_id") or ""),
            last_series_category_id=str(raw.get("last_series_category_id") or ""),
            last_stream_id=str(raw.get("last_stream_id") or ""),
            last_stream_name=str(raw.get("last_stream_name") or ""),
            last_kind=last_kind,
            last_series_id=str(raw.get("last_series_id") or ""),
            favourites=_clean_favourites(raw.get("favourites")),
        )
        encoded = raw.get("protected_credentials")
        if not encoded:
            return config
        try:
            decrypted = unprotect_secret(base64.b64decode(str(encoded)))
            secret = json.loads(decrypted.decode("utf-8"))
        except (ConfigError, json.JSONDecodeError, ValueError, UnicodeError):
            config.credentials_readable = False
            return config
        if not isinstance(secret, dict):
            config.credentials_readable = False
            return config
        config.server_url = str(secret.get("server_url") or "")
        config.username = str(secret.get("username") or "")
        config.password = str(secret.get("password") or "")
        return config
