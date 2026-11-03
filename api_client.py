"""Xtream Codes API client and background fetch workers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from urllib.parse import quote, urlparse

import requests
from PyQt6.QtCore import QThread, pyqtSignal
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RequestException, SSLError, Timeout

from config import AUTH_TIMEOUT, CATEGORY_TIMEOUT, STREAM_TIMEOUT, USER_AGENT

logger = logging.getLogger(__name__)


class XtreamError(Exception):
    """A failed Xtream request with a stable code for the user interface."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Credentials:
    server_url: str
    username: str
    password: str


@dataclass(frozen=True)
class AccountInfo:
    username: str
    status: str
    expiry: str
    message: str

    @property
    def summary(self) -> str:
        parts = [self.username or "Account", self.status or "Signed in"]
        if self.expiry:
            parts.append(f"expires {self.expiry}")
        return " · ".join(parts)


@dataclass(frozen=True)
class LiveCategory:
    category_id: str
    name: str
    parent_id: str


@dataclass(frozen=True)
class MediaItem:
    """A channel, movie, series, or episode returned by the Xtream API."""

    item_id: str
    name: str
    icon_url: str
    category_id: str
    kind: str
    extension: str = ""
    direct_url: str = ""
    series_id: str = ""
    season: int = 0
    episode: int = 0

    @property
    def playable(self) -> bool:
        return self.kind in {"live", "movie", "episode"}


def normalize_server_url(url: str) -> str:
    text = url.strip()
    if not text:
        raise XtreamError("auth", "Enter the server URL.")
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if parsed.username or parsed.password:
        raise XtreamError(
            "auth",
            "Enter the server address only. Username and password belong in their own fields.",
        )
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise XtreamError("auth", "Enter a valid server URL, including the port if it uses one.")
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    for suffix in ("/player_api.php", "/panel_api.php", "/get.php", "/xmltv.php"):
        if lowered.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def live_playback_url(credentials: Credentials, stream_id: str) -> str:
    user = quote(credentials.username, safe="")
    password = quote(credentials.password, safe="")
    stream = quote(str(stream_id), safe="")
    return f"{credentials.server_url}/live/{user}/{password}/{stream}.ts"


def media_playback_url(credentials: Credentials, item: MediaItem) -> str:
    if item.direct_url:
        return item.direct_url
    user = quote(credentials.username, safe="")
    password = quote(credentials.password, safe="")
    media_id = quote(str(item.item_id), safe="")
    if item.kind == "movie":
        extension = _safe_extension(item.extension, "mp4")
        return f"{credentials.server_url}/movie/{user}/{password}/{media_id}.{extension}"
    if item.kind == "episode":
        extension = _safe_extension(item.extension, "mp4")
        return f"{credentials.server_url}/series/{user}/{password}/{media_id}.{extension}"
    return f"{credentials.server_url}/live/{user}/{password}/{media_id}.ts"


def _safe_extension(value: object, default: str) -> str:
    text = str(value or "").strip().lower().lstrip(".")
    if text.isascii() and text.isalnum() and 1 <= len(text) <= 5:
        return text
    return default


def _direct_url(value: object) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return text
    return ""


def _positive_int(value: object) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _episode_label(season: int, episode: int, title: str) -> str:
    label = title.strip() or "Episode"
    if season > 0 and episode > 0:
        return f"S{season:02d}E{episode:02d}  {label}"
    if episode > 0:
        return f"E{episode:02d}  {label}"
    return label


def _episode_rows(payload: dict) -> list[tuple[int, dict]]:
    found: list[tuple[int, dict]] = []
    episodes = payload.get("episodes")
    if isinstance(episodes, dict):
        for key, rows in episodes.items():
            season_hint = _positive_int(key)
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        found.append((season_hint, row))
    elif isinstance(episodes, list):
        for row in episodes:
            if isinstance(row, dict):
                found.append((_positive_int(row.get("season")), row))
    if found:
        return found
    seasons = payload.get("seasons")
    if isinstance(seasons, list):
        for season_row in seasons:
            if not isinstance(season_row, dict):
                continue
            season_hint = _positive_int(season_row.get("season_number") or season_row.get("season"))
            rows = season_row.get("episodes")
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        found.append((season_hint, row))
    return found


def _format_expiry(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"", "0", "none", "null", "unlimited"}:
        return "Unlimited" if text == "0" or text.lower() == "unlimited" else ""
    try:
        return datetime.fromtimestamp(int(text)).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return text


def _safe_server_message(message: object, password: str) -> str:
    text = str(message or "").strip()
    if not text or len(text) > 180:
        return ""
    if password and password in text:
        return ""
    return text


def _auth_ok(info: dict) -> bool:
    if "auth" in info and info.get("auth") not in (None, ""):
        try:
            return int(info.get("auth")) == 1
        except (TypeError, ValueError):
            return bool(info.get("auth"))
    status = str(info.get("status") or "").strip().lower()
    return status in {"active", "trial"}


class XtreamClient:
    """Blocking Xtream Codes calls. Run them on an ApiWorker, not the UI thread."""

    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials

    def __repr__(self) -> str:
        return (
            f"XtreamClient(server={self.credentials.server_url!r}, "
            f"username={self.credentials.username!r})"
        )

    def playback_url(self, stream_id: str) -> str:
        return live_playback_url(self.credentials, stream_id)

    def url_for(self, item: MediaItem) -> str:
        return media_playback_url(self.credentials, item)

    def authenticate(self) -> AccountInfo:
        payload = self._get({}, AUTH_TIMEOUT)
        if not isinstance(payload, dict):
            raise XtreamError("invalid_response", "The server returned an unexpected sign-in response.")
        info = payload.get("user_info")
        if not isinstance(info, dict):
            raise XtreamError("auth", "The server did not accept these credentials.")
        if not _auth_ok(info):
            detail = _safe_server_message(info.get("message"), self.credentials.password)
            raise XtreamError(
                "auth",
                detail or "The username or password was rejected by the server.",
            )
        status = str(info.get("status") or "Active").strip() or "Active"
        if status.lower() not in {"active", "trial"}:
            raise XtreamError("auth", f"This account is not active ({status}).")
        return AccountInfo(
            username=str(info.get("username") or self.credentials.username),
            status=status,
            expiry=_format_expiry(info.get("exp_date")),
            message=_safe_server_message(info.get("message"), self.credentials.password),
        )

    def get_live_categories(self) -> list[LiveCategory]:
        return self.get_categories("live")

    def get_categories(self, section: str) -> list[LiveCategory]:
        action = {
            "live": "get_live_categories",
            "movies": "get_vod_categories",
            "series": "get_series_categories",
        }.get(section)
        if action is None:
            raise XtreamError("invalid_response", "Unknown catalog.")
        payload = self._get({"action": action}, CATEGORY_TIMEOUT)
        rows = self._expect_list(payload, "categories")
        categories: list[LiveCategory] = []
        for item in rows:
            if not isinstance(item, dict) or item.get("category_id") is None:
                continue
            name = str(item.get("category_name") or "Untitled").strip() or "Untitled"
            parent = item.get("parent_id")
            categories.append(
                LiveCategory(
                    category_id=str(item["category_id"]),
                    name=name,
                    parent_id="0" if parent is None else str(parent),
                )
            )
        categories.sort(key=lambda category: category.name.casefold())
        return categories

    def get_live_streams(self, category_id: str) -> list[MediaItem]:
        return self.get_catalog("live", category_id)

    def get_catalog(self, section: str, category_id: str) -> list[MediaItem]:
        if not str(category_id).strip():
            raise XtreamError("invalid_response", "Choose a category first.")
        return self._load_catalog(section, str(category_id))

    def get_all(self, section: str) -> list[MediaItem]:
        """Every channel, movie, or series in the section, across all categories."""
        return self._load_catalog(section, None)

    def _load_catalog(self, section: str, category_id: str | None) -> list[MediaItem]:
        action, label = {
            "live": ("get_live_streams", "channels"),
            "movies": ("get_vod_streams", "movies"),
            "series": ("get_series", "series"),
        }.get(section, ("", ""))
        if not action:
            raise XtreamError("invalid_response", "Unknown catalog.")
        params = {"action": action}
        fallback = ""
        if category_id is not None:
            params["category_id"] = category_id
            fallback = category_id
        payload = self._get(params, STREAM_TIMEOUT)
        rows = self._expect_list(payload, label)
        items: list[MediaItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = self._media_from_row(section, row, str(row.get("category_id") or fallback))
            if item is not None:
                items.append(item)
        return items

    def get_series_episodes(self, series_id: str, series_name: str, cover_url: str) -> list[MediaItem]:
        if not str(series_id).strip():
            raise XtreamError("invalid_response", "Choose a series first.")
        payload = self._get(
            {"action": "get_series_info", "series_id": str(series_id)},
            STREAM_TIMEOUT,
        )
        if not isinstance(payload, dict):
            raise XtreamError("invalid_response", "The server did not return episode details.")
        items: list[MediaItem] = []
        for season_hint, row in _episode_rows(payload):
            info = row.get("info") if isinstance(row.get("info"), dict) else {}
            episode_id = row.get("id")
            if episode_id is None:
                episode_id = row.get("episode_id") or row.get("stream_id")
            if episode_id is None:
                continue
            season = _positive_int(row.get("season") or info.get("season")) or season_hint
            episode = _positive_int(row.get("episode_num") or info.get("episode_num") or row.get("episode"))
            title = str(
                info.get("title") or info.get("name") or row.get("title") or row.get("name") or ""
            ).strip()
            icon = self._clean_icon_url(info.get("movie_image") or row.get("cover")) or cover_url
            items.append(
                MediaItem(
                    item_id=str(episode_id),
                    name=_episode_label(season, episode, title),
                    icon_url=icon,
                    category_id=str(series_id),
                    kind="episode",
                    extension=_safe_extension(
                        row.get("container_extension") or info.get("container_extension"),
                        "mp4",
                    ),
                    direct_url=_direct_url(row.get("direct_source")),
                    series_id=str(series_id),
                    season=season,
                    episode=episode,
                )
            )
        items.sort(key=lambda item: (item.season, item.episode, item.name.casefold()))
        if not items:
            name = series_name.strip() or "this series"
            raise XtreamError("invalid_response", f"No episodes were found for {name}.")
        return items

    def _media_from_row(self, section: str, row: dict, category_id: str) -> MediaItem | None:
        if section == "series":
            raw_id = row.get("series_id")
            if raw_id is None:
                return None
            name = str(row.get("name") or "Untitled series").strip() or "Untitled series"
            return MediaItem(
                item_id=str(raw_id),
                name=name,
                icon_url=self._clean_icon_url(row.get("cover") or row.get("stream_icon")),
                category_id=str(row.get("category_id") or category_id),
                kind="series",
            )
        raw_id = row.get("stream_id")
        if raw_id is None:
            return None
        if section == "movies":
            name = str(row.get("name") or "Untitled movie").strip() or "Untitled movie"
            kind = "movie"
            extension = _safe_extension(row.get("container_extension"), "mp4")
        else:
            name = str(row.get("name") or "Untitled channel").strip() or "Untitled channel"
            kind = "live"
            extension = "ts"
        return MediaItem(
            item_id=str(raw_id),
            name=name,
            icon_url=self._clean_icon_url(row.get("stream_icon")),
            category_id=str(row.get("category_id") or category_id),
            kind=kind,
            extension=extension,
            direct_url=_direct_url(row.get("direct_source")),
        )

    def _expect_list(self, payload: object, label: str) -> list:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            detail = _safe_server_message(payload.get("message"), self.credentials.password)
            raise XtreamError(
                "invalid_response",
                detail or f"The server did not return a {label} list.",
            )
        raise XtreamError("invalid_response", f"The server did not return a {label} list.")

    def _clean_icon_url(self, icon: object) -> str:
        text = str(icon or "").strip()
        if not text or text.lower() in {"null", "none", "undefined"}:
            return ""
        if text.startswith("//"):
            scheme = urlparse(self.credentials.server_url).scheme or "http"
            return f"{scheme}:{text}"
        parsed = urlparse(text)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return text
        if text.startswith("/"):
            origin = urlparse(self.credentials.server_url)
            return f"{origin.scheme}://{origin.netloc}{text}"
        return ""

    def _get(self, params: dict[str, str], timeout: tuple[int, int]) -> object:
        query = {
            "username": self.credentials.username,
            "password": self.credentials.password,
            **params,
        }
        url = f"{self.credentials.server_url}/player_api.php"
        try:
            response = requests.get(
                url,
                params=query,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"},
            )
        except Timeout:
            raise XtreamError("timeout", "The server took too long to respond.") from None
        except SSLError:
            raise XtreamError(
                "ssl",
                "The secure connection failed. Check the server URL and certificate.",
            ) from None
        except RequestsConnectionError:
            raise XtreamError(
                "offline",
                "The server could not be reached. Check the URL, port, and your connection.",
            ) from None
        except RequestException:
            raise XtreamError(
                "offline",
                "The request failed before the server returned any data.",
            ) from None

        if response.status_code in {401, 403}:
            raise XtreamError("auth", "The username or password was rejected by the server.")
        if response.status_code >= 500:
            raise XtreamError("offline", f"The server returned an error ({response.status_code}).")
        if response.status_code != 200:
            raise XtreamError("http", f"The server returned HTTP {response.status_code}.")

        text = response.text.lstrip("\ufeff").strip()
        if not text:
            raise XtreamError("invalid_response", "The server returned an empty response.")
        if text[0] not in "{[":
            raise XtreamError(
                "invalid_response",
                "The server did not return Xtream API data. Check the URL and port.",
            )
        try:
            payload = response.json()
        except ValueError:
            raise XtreamError("invalid_response", "The server returned invalid JSON.") from None
        if isinstance(payload, dict):
            info = payload.get("user_info")
            if isinstance(info, dict) and "auth" in info and info.get("auth") not in (None, "") and not _auth_ok(info):
                detail = _safe_server_message(info.get("message"), self.credentials.password)
                raise XtreamError("auth", detail or "The username or password was rejected by the server.")
        return payload


class ApiWorker(QThread):
    """Run one blocking API call and deliver the result to the UI thread."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str, str)

    def __init__(self, func: Callable[[], object], parent=None) -> None:
        super().__init__(parent)
        self._func = func

    def run(self) -> None:
        try:
            result = self._func()
        except XtreamError as exc:
            self.failed.emit(exc.code, str(exc))
        except Exception:
            logger.exception("Xtream request failed")
            self.failed.emit(
                "unexpected",
                "An unexpected error occurred while contacting the server.",
            )
        else:
            self.succeeded.emit(result)
