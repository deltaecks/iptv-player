"""PyQt6 video surface backed by a libVLC media player."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from config import VLC_ARGS


_DLL_DIRECTORY_HANDLES: list[object] = []


def _clock(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds) // 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def find_vlc_directory() -> Path | None:
    candidates = [
        os.environ.get("VLC_HOME", ""),
        r"C:\Program Files\VideoLAN\VLC",
        r"C:\Program Files (x86)\VideoLAN\VLC",
    ]
    for folder in candidates:
        if folder and Path(folder, "libvlc.dll").is_file():
            return Path(folder)
    return None


def prepare_vlc_environment() -> Path | None:
    """Make libvlc.dll and its plugins discoverable before python-vlc imports them."""
    folder = find_vlc_directory()
    if folder is None:
        return None
    folder_text = str(folder)
    plugin_path = str(folder / "plugins")
    os.environ["VLC_PLUGIN_PATH"] = plugin_path
    os.environ["PATH"] = folder_text + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(folder_text))
    return folder


class VideoSurface(QFrame):
    """Native window libVLC draws into. It stays free of child widgets so they cannot cover the video."""

    double_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(True)
        self.setMinimumSize(320, 180)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, Qt.GlobalColor.black)
        self.setPalette(palette)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class VlcPlayerWidget(QWidget):
    """Playback controls around an embedded VLC player."""

    error_occurred = pyqtSignal(str)
    play_requested = pyqtSignal()
    fullscreen_toggle_requested = pyqtSignal()
    stopped = pyqtSignal()
    volume_changed = pyqtSignal(int)
    mute_changed = pyqtSignal(bool)
    title_changed = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._instance = None
        self._player = None
        self._events = None
        self._error_callback = None
        self._vlc = None
        self._url = ""
        self._title = ""
        self._media = None
        self._attached_hwnd = 0
        self._expecting_error = False
        self._audio_applied = False
        self._seekable = False
        self._user_seeking = False
        self._shutting_down = False
        self._load_error = ""
        self._build_ui()
        self._load_vlc()
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._poll_state)
        if self._player is not None:
            self._timer.start()
        else:
            self._apply_unavailable_state()

    def _build_ui(self) -> None:
        self.surface = VideoSurface()
        self.play_button = QPushButton("Play")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")
        self.mute_button = QPushButton("Mute")
        self.mute_button.setCheckable(True)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(140)
        self.volume_label = QLabel("80")
        self.volume_label.setMinimumWidth(28)
        self.state_label = QLabel("Idle")
        self.title_label = QLabel("Select something to watch")
        self.title_label.setObjectName("nowPlaying")
        self.fullscreen_button = QPushButton("Fullscreen")
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setVisible(False)
        self.time_label = QLabel("")
        self.time_label.setObjectName("countLabel")
        self.time_label.setVisible(False)
        self.time_label.setMinimumWidth(96)

        for button in (
            self.play_button,
            self.pause_button,
            self.stop_button,
            self.mute_button,
            self.fullscreen_button,
        ):
            button.setCursor(Qt.CursorShape.PointingHandCursor)

        self.play_button.clicked.connect(self.play)
        self.pause_button.clicked.connect(self.pause)
        self.stop_button.clicked.connect(self.stop)
        self.mute_button.clicked.connect(self._toggle_mute)
        self.fullscreen_button.clicked.connect(self.fullscreen_toggle_requested.emit)
        self.volume_slider.valueChanged.connect(self._on_volume_slider)
        self.seek_slider.sliderPressed.connect(self._begin_seek)
        self.seek_slider.sliderReleased.connect(self._finish_seek)
        self.surface.double_clicked.connect(self.fullscreen_toggle_requested.emit)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        controls.addWidget(self.play_button)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.mute_button)
        controls.addWidget(self.volume_slider)
        controls.addWidget(self.volume_label)
        controls.addWidget(self.state_label)
        controls.addStretch(1)
        controls.addWidget(self.title_label, 1)
        controls.addWidget(self.fullscreen_button)

        timeline = QHBoxLayout()
        timeline.setContentsMargins(0, 0, 0, 0)
        timeline.setSpacing(8)
        timeline.addWidget(self.seek_slider, 1)
        timeline.addWidget(self.time_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.surface, 1)
        layout.addLayout(timeline)
        layout.addLayout(controls)

    def _load_vlc(self) -> None:
        if sys.platform != "win32":
            self._load_error = "Embedded playback is implemented for Windows."
            return
        folder = prepare_vlc_environment()
        if folder is None:
            self._load_error = (
                "64-bit VLC Media Player was not found. Install it from videolan.org, "
                "then restart this application."
            )
            return
        try:
            import vlc
        except Exception:
            self._load_error = (
                "VLC is installed, but Python could not load libvlc.dll. "
                "Use 64-bit Python with 64-bit VLC."
            )
            return
        args = list(VLC_ARGS)
        try:
            instance = vlc.Instance(*args)
            player = instance.media_player_new() if instance is not None else None
        except Exception:
            instance = None
            player = None
        if instance is None or player is None:
            self._load_error = "VLC could not be started with hardware acceleration enabled."
            return
        self._vlc = vlc
        self._instance = instance
        self._player = player
        self._error_callback = self._on_vlc_error
        self._events = player.event_manager()
        self._events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._error_callback)

    def _apply_unavailable_state(self) -> None:
        self.title_label.setText(self._load_error or "VLC is unavailable.")
        self.title_label.setToolTip(self.title_label.text())
        self.state_label.setText("Unavailable")
        for widget in (
            self.play_button,
            self.pause_button,
            self.stop_button,
            self.mute_button,
            self.volume_slider,
        ):
            widget.setEnabled(False)

    @property
    def available(self) -> bool:
        return self._player is not None

    @property
    def load_error(self) -> str:
        return self._load_error

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.attach_output()

    def attach_output(self) -> None:
        """Bind libVLC to the native video window after show, resize, or fullscreen."""
        self._attach_window(force=True)

    def play_url(self, url: str, title: str = "", seekable: bool = False) -> None:
        if self._player is None or self._instance is None:
            self.error_occurred.emit(self._load_error or "VLC is not available.")
            return
        if not url:
            self.error_occurred.emit("This channel does not have a playback address.")
            return
        self._url = url
        self._title = title or "Live channel"
        self._seekable = seekable
        self._user_seeking = False
        self.seek_slider.setVisible(False)
        self.time_label.setVisible(False)
        self.time_label.setText("")
        self.title_label.setText(self._title)
        self.title_label.setToolTip(self._title)
        self.title_changed.emit(self._title)
        self.attach_output()
        media = self._instance.media_new(url)
        media.add_option(":network-caching=3000")
        media.add_option(":http-user-agent=IPTVPlayer/1.0")
        previous = self._media
        self._media = media
        self._expecting_error = False
        self._player.set_media(media)
        if previous is not None:
            try:
                previous.release()
            except Exception:
                pass
        self._audio_applied = False
        self._expecting_error = True
        started = self._player.play()
        self._release_vlc_input()
        self.state_label.setText("Opening")
        if started == -1:
            self._expecting_error = False
            self.state_label.setText("Error")
            self.error_occurred.emit("VLC could not start playback for this stream.")

    def play(self) -> None:
        if self._player is None:
            self.error_occurred.emit(self._load_error or "VLC is not available.")
            return
        state = self._player.get_state()
        paused = state == self._vlc.State.Paused
        stopped = state in {
            self._vlc.State.Stopped,
            self._vlc.State.Ended,
            self._vlc.State.NothingSpecial,
            self._vlc.State.Error,
        }
        if paused:
            self._expecting_error = True
            self._attach_window(force=True)
            self._player.play()
            return
        if self._url and stopped:
            self.play_url(self._url, self._title, self._seekable)
            return
        self.play_requested.emit()

    def pause(self) -> None:
        if self._player is None:
            return
        if self._player.get_state() == self._vlc.State.Playing:
            self._player.pause()
            self.state_label.setText("Paused")

    def stop(self) -> None:
        self._expecting_error = False
        if self._player is not None:
            self._player.stop()
        self.state_label.setText("Stopped")
        self.title_label.setText("Nothing playing")
        self.seek_slider.setVisible(False)
        self.time_label.setVisible(False)
        self.title_label.setToolTip("")
        self.stopped.emit()

    def set_volume(self, value: int) -> None:
        volume = max(0, min(100, int(value)))
        if self.volume_slider.value() != volume:
            self.volume_slider.blockSignals(True)
            self.volume_slider.setValue(volume)
            self.volume_slider.blockSignals(False)
        self.volume_label.setText(str(volume))
        if self._player is not None:
            self._player.audio_set_volume(volume)

    def set_muted(self, muted: bool) -> None:
        if self.mute_button.isChecked() != muted:
            self.mute_button.blockSignals(True)
            self.mute_button.setChecked(muted)
            self.mute_button.blockSignals(False)
        self.mute_button.setText("Unmute" if muted else "Mute")
        if self._player is not None:
            self._player.audio_set_mute(muted)

    def set_fullscreen_button_text(self, fullscreen: bool) -> None:
        self.fullscreen_button.setText("Exit fullscreen" if fullscreen else "Fullscreen")

    def _toggle_mute(self, checked: bool) -> None:
        self.set_muted(checked)
        self.mute_changed.emit(checked)

    def _on_volume_slider(self, value: int) -> None:
        self.volume_label.setText(str(value))
        if self._player is not None:
            self._player.audio_set_volume(value)
        self.volume_changed.emit(value)

    def _attach_window(self, force: bool = False) -> None:
        if self._player is None or self._shutting_down:
            return
        hwnd = int(self.surface.winId())
        if hwnd and (force or hwnd != self._attached_hwnd):
            self._player.set_hwnd(hwnd)
            self._attached_hwnd = hwnd
            self._release_vlc_input()

    def _release_vlc_input(self) -> None:
        """Keep double-click and keyboard shortcuts on the Qt window."""
        if self._player is None:
            return
        try:
            self._player.video_set_mouse_input(False)
            self._player.video_set_key_input(False)
        except Exception:
            pass

    def _on_vlc_error(self, _event) -> None:
        if self._shutting_down or not self._expecting_error:
            return
        self._expecting_error = False
        self.error_occurred.emit(
            "This stream could not be played. The link may be offline, blocked, or no longer valid."
        )

    def _poll_state(self) -> None:
        if self._player is None or self._shutting_down or self._vlc is None:
            return
        try:
            state = self._player.get_state()
        except Exception:
            return
        labels = {
            self._vlc.State.Playing: "Playing",
            self._vlc.State.Paused: "Paused",
            self._vlc.State.Stopped: "Stopped",
            self._vlc.State.Ended: "Ended",
            self._vlc.State.Buffering: "Buffering",
            self._vlc.State.Opening: "Opening",
            self._vlc.State.Error: "Error",
            self._vlc.State.NothingSpecial: "Idle",
        }
        self.state_label.setText(labels.get(state, self.state_label.text()))
        playing = state == self._vlc.State.Playing
        if state in {self._vlc.State.Playing, self._vlc.State.Paused} and not self._audio_applied:
            self._player.audio_set_volume(self.volume_slider.value())
            self._player.audio_set_mute(self.mute_button.isChecked())
            self._release_vlc_input()
            self._audio_applied = True
        self._update_timeline(state)
        self.pause_button.setEnabled(playing)
        self.stop_button.setEnabled(
            state
            in {
                self._vlc.State.Playing,
                self._vlc.State.Paused,
                self._vlc.State.Buffering,
                self._vlc.State.Opening,
            }
        )

    def _begin_seek(self) -> None:
        self._user_seeking = True

    def _finish_seek(self) -> None:
        self._user_seeking = False
        if self._player is None or not self._seekable:
            return
        try:
            self._player.set_time(self.seek_slider.value())
        except Exception:
            return

    def _update_timeline(self, state) -> None:
        if self._player is None or self._vlc is None or not self._seekable:
            self.seek_slider.setVisible(False)
            self.time_label.setVisible(False)
            return
        if state not in {self._vlc.State.Playing, self._vlc.State.Paused}:
            return
        try:
            length = int(self._player.get_length() or 0)
            position = int(self._player.get_time() or 0)
        except Exception:
            return
        if length <= 0:
            return
        self.seek_slider.setVisible(True)
        self.time_label.setVisible(True)
        if self.seek_slider.maximum() != length:
            self.seek_slider.setRange(0, length)
        if not self._user_seeking:
            self.seek_slider.setValue(max(0, min(position, length)))
        self.time_label.setText(f"{_clock(position)} / {_clock(length)}")

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._expecting_error = False
        if hasattr(self, "_timer"):
            self._timer.stop()
        player = self._player
        events = self._events
        vlc_module = self._vlc
        self._player = None
        self._events = None
        if events is not None and vlc_module is not None and player is not None:
            try:
                events.event_detach(
                    vlc_module.EventType.MediaPlayerEncounteredError,
                    self._error_callback,
                )
            except Exception:
                pass
        if player is not None:
            try:
                player.stop()
            except Exception:
                pass
        if self._media is not None:
            try:
                self._media.release()
            except Exception:
                pass
            self._media = None
        if player is not None:
            try:
                player.release()
            except Exception:
                pass
        if self._instance is not None:
            try:
                self._instance.release()
            except Exception:
                pass
            self._instance = None
