"""Main window: sign-in, categories, searchable channel list, and the video player."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from collections.abc import Callable

from PyQt6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QRegularExpression,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    QUrl,
)
from PyQt6.QtGui import (
    QAction,
    QCloseEvent,
    QIcon,
    QKeySequence,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QTableView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from api_client import (
    AccountInfo,
    ApiWorker,
    Credentials,
    LiveCategory,
    MediaItem,
    XtreamClient,
    XtreamError,
    normalize_server_url,
)
from config import APP_NAME, AppConfig, ConfigError
from player import VlcPlayerWidget


STREAM_ROLE = Qt.ItemDataRole.UserRole
LOGO_ROLE = Qt.ItemDataRole.UserRole + 1
FAVOURITES_ID = "__favourites__"
SEARCH_RESULT_LIMIT = 500

ERROR_TITLES = {
    "auth": "Sign-in failed",
    "offline": "Server unreachable",
    "timeout": "Server timed out",
    "ssl": "Secure connection failed",
    "http": "Server error",
    "invalid_response": "Unexpected server response",
    "stream": "Playback failed",
    "unexpected": "Something went wrong",
}


class ChannelFilterProxy(QSortFilterProxyModel):
    """Case-insensitive channel names and numeric stream ids."""

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        if left.column() == 2:
            return _stream_sort_key(left.data()) < _stream_sort_key(right.data())
        return str(left.data() or "").casefold() < str(right.data() or "").casefold()


def _stream_sort_key(value: object) -> tuple[int, str]:
    text = str(value or "")
    try:
        return (0, f"{int(text):012d}")
    except ValueError:
        return (1, text.casefold())


class LogoLoader(QObject):
    """Download channel logos without blocking the UI."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._network = QNetworkAccessManager(self)
        self._network.finished.connect(self._on_finished)
        self._cache: dict[str, QIcon] = {}
        self._inflight: dict[str, int] = {}
        self._queue: deque[tuple[str, int, Callable[[QIcon], None]]] = deque()
        self._active: dict[QNetworkReply, tuple[str, int, Callable[[QIcon], None]]] = {}
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def reset(self) -> int:
        self._generation += 1
        self._queue.clear()
        self._inflight.clear()
        for reply in list(self._active):
            reply.abort()
        return self._generation

    def request(self, url: str, generation: int, on_ready: Callable[[QIcon], None]) -> None:
        if generation != self._generation or not url:
            return
        cached = self._cache.get(url)
        if cached is not None:
            on_ready(cached)
            return
        if url in self._inflight:
            return
        qurl = QUrl(url)
        if not qurl.isValid() or qurl.scheme() not in {"http", "https"}:
            return
        self._inflight[url] = generation
        self._queue.append((url, generation, on_ready))
        self._pump()

    def _pump(self) -> None:
        while len(self._active) < 6 and self._queue:
            url, generation, on_ready = self._queue.popleft()
            if generation != self._generation:
                if self._inflight.get(url) == generation:
                    self._inflight.pop(url, None)
                continue
            request = QNetworkRequest(QUrl(url))
            request.setTransferTimeout(8000)
            request.setAttribute(
                QNetworkRequest.Attribute.RedirectPolicyAttribute,
                QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
            )
            reply = self._network.get(request)
            self._active[reply] = (url, generation, on_ready)

    def _on_finished(self, reply: QNetworkReply) -> None:
        meta = self._active.pop(reply, None)
        data = b""
        if reply.error() == QNetworkReply.NetworkError.NoError:
            data = bytes(reply.readAll())
        reply.deleteLater()
        self._pump()
        if meta is None:
            return
        url, generation, on_ready = meta
        if self._inflight.get(url) == generation:
            self._inflight.pop(url, None)
        if generation != self._generation or not data:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        scaled = pixmap.scaled(
            28,
            28,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        icon = QIcon(scaled)
        self._cache[url] = icon
        if len(self._cache) > 800:
            self._cache.pop(next(iter(self._cache)))
        if generation == self._generation:
            on_ready(icon)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = AppConfig.load()
        self.client: XtreamClient | None = None
        self.account: AccountInfo | None = None
        self._pending_credentials: Credentials | None = None
        self._workers: list[ApiWorker] = []
        self._closing = False
        self._session_id = 0
        self._category_generation = 0
        self._stream_generation = 0
        self._sorted_channels = False
        self._splitter_sizes: list[int] | None = None
        self._resume_on_launch = bool(self.config.last_stream_id)
        self._section = self.config.last_section if self.config.last_section in {"live", "movies", "series"} else "live"
        self._episode_mode = False
        self._current_series: MediaItem | None = None
        self._series_items: list[MediaItem] = []
        self._detail_generation = 0
        self._category_items: list[MediaItem] = []
        self._category_names: dict[str, str] = {}
        self._catalog_cache: dict[str, list[MediaItem]] = {}
        self._catalog_loading_section = ""
        self._search_generation = 0
        self._global_search = False
        self._viewing_favourites = False
        self._return_items: list[MediaItem] | None = None
        self._return_global = False

        self.setWindowTitle(APP_NAME)
        self.resize(1280, 760)
        self.setMinimumSize(960, 600)
        icon_file = Path(__file__).resolve().parent / "assets" / "iptv-player.ico"
        if icon_file.is_file():
            self.setWindowIcon(QIcon(str(icon_file)))
        else:
            self.setWindowIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))

        self._build_login_page()
        self._build_browser_page()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.login_page)
        self.stack.addWidget(self.browser_page)
        self.setCentralWidget(self.stack)
        self.setStatusBar(QStatusBar())
        self.account_label = QLabel("Not signed in")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(120)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.account_label)
        self._build_menu()
        self.setStyleSheet(_STYLESHEET)
        self._fill_login_fields()
        self.player.set_volume(self.config.volume)
        self.player.set_muted(self.config.muted)
        self._connect_player()
        self._sync_session_actions()

        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._persist_preferences)
        self._logo_timer = QTimer(self)
        self._logo_timer.setSingleShot(True)
        self._logo_timer.setInterval(40)
        self._logo_timer.timeout.connect(self._load_visible_logos)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._run_search)
        self.table.verticalScrollBar().valueChanged.connect(self._schedule_logos)
        self.proxy.layoutChanged.connect(self._schedule_logos)
        QTimer.singleShot(0, self._startup)

    def _build_login_page(self) -> None:
        self.login_page = QWidget()
        card = QFrame()
        card.setObjectName("loginCard")
        card.setFixedWidth(440)
        title = QLabel(APP_NAME)
        title.setObjectName("loginTitle")
        subtitle = QLabel("Sign in with your Xtream Codes server.")
        subtitle.setObjectName("loginSubtitle")
        subtitle.setWordWrap(True)
        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText("http://example.com:8080")
        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("Username")
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("Password")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_button = QPushButton("Test connection and sign in")
        self.login_button.setObjectName("primary")
        self.login_button.setDefault(True)
        self.login_status = QLabel("")
        self.login_status.setWordWrap(True)
        self.login_status.setObjectName("loginStatus")

        form = QVBoxLayout(card)
        form.setContentsMargins(28, 28, 28, 28)
        form.setSpacing(10)
        form.addWidget(title)
        form.addWidget(subtitle)
        form.addSpacing(8)
        form.addWidget(QLabel("Server URL"))
        form.addWidget(self.server_edit)
        form.addWidget(QLabel("Username"))
        form.addWidget(self.username_edit)
        form.addWidget(QLabel("Password"))
        form.addWidget(self.password_edit)
        form.addSpacing(6)
        form.addWidget(self.login_button)
        form.addWidget(self.login_status)

        outer = QVBoxLayout(self.login_page)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

        self.login_button.clicked.connect(self._sign_in)
        self.password_edit.returnPressed.connect(self._sign_in)
        self.server_edit.returnPressed.connect(self._sign_in)
        self.username_edit.returnPressed.connect(self._sign_in)

    def _build_browser_page(self) -> None:
        self.browser_page = QWidget()
        self.logos = LogoLoader(self)

        self.section_bar = QWidget()
        section_row = QHBoxLayout(self.section_bar)
        section_row.setContentsMargins(12, 10, 12, 0)
        section_row.setSpacing(8)
        self._section_group = QButtonGroup(self)
        self._section_group.setExclusive(True)
        for index, (key, label) in enumerate((("live", "Live TV"), ("movies", "Movies"), ("series", "Series"))):
            button = QPushButton(label)
            button.setObjectName("section")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setChecked(key == self._section)
            self._section_group.addButton(button, index)
            section_row.addWidget(button)
        section_row.addStretch(1)
        self._section_group.idClicked.connect(self._on_section_clicked)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(self._on_category_changed)

        category_title = QLabel("Categories")
        category_title.setObjectName("sectionTitle")
        category_layout = QVBoxLayout()
        category_layout.setContentsMargins(12, 12, 6, 12)
        category_layout.setSpacing(8)
        category_layout.addWidget(category_title)
        category_layout.addWidget(self.tree, 1)
        self.category_panel = QWidget()
        self.category_panel.setLayout(category_layout)
        self.category_panel.setMinimumWidth(200)

        self.back_button = QPushButton("Back")
        self.back_button.setVisible(False)
        self.back_button.clicked.connect(self._back_to_series)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search all channels")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        self.search.returnPressed.connect(self._play_selected)
        self.favourite_button = QPushButton("Favourite")
        self.favourite_button.setEnabled(False)
        self.favourite_button.clicked.connect(self._toggle_selected_favourite)
        self.count_label = QLabel("No channels")
        self.count_label.setObjectName("countLabel")

        self.model = QStandardItemModel(0, 4, self)
        self.model.setHorizontalHeaderLabels(["", "Channel", "ID", "Category"])
        self.proxy = ChannelFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterKeyColumn(1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setDynamicSortFilter(True)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setIconSize(QSize(28, 28))
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        self.table.setColumnWidth(0, 48)
        self.table.setColumnWidth(3, 160)
        self.table.activated.connect(self._play_index)
        self.table.customContextMenuRequested.connect(self._channel_menu)
        selection = self.table.selectionModel()
        if selection is not None:
            selection.selectionChanged.connect(lambda *_args: self._update_favourite_button())

        self.list_title = QLabel("Channels")
        self.list_title.setObjectName("sectionTitle")
        search_row = QHBoxLayout()
        search_row.addWidget(self.back_button)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.favourite_button)
        search_row.addWidget(self.count_label)
        channel_layout = QVBoxLayout()
        channel_layout.setContentsMargins(6, 12, 6, 12)
        channel_layout.setSpacing(8)
        channel_layout.addWidget(self.list_title)
        channel_layout.addLayout(search_row)
        channel_layout.addWidget(self.table, 1)
        self.channel_panel = QWidget()
        self.channel_panel.setLayout(channel_layout)
        self.channel_panel.setMinimumWidth(320)

        self.player = VlcPlayerWidget()
        player_wrap = QVBoxLayout()
        player_wrap.setContentsMargins(6, 12, 12, 12)
        player_wrap.addWidget(self.player, 1)
        self.player_panel = QWidget()
        self.player_panel.setLayout(player_wrap)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.category_panel)
        self.splitter.addWidget(self.channel_panel)
        self.splitter.addWidget(self.player_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setStretchFactor(2, 3)
        self.splitter.setSizes([240, 460, 580])
        self.splitter.setChildrenCollapsible(False)

        layout = QVBoxLayout(self.browser_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.section_bar)
        layout.addWidget(self.splitter, 1)
        self._apply_labels()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        self.logout_action = QAction("Log out", self)
        self.logout_action.triggered.connect(self._logout)
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(self.logout_action)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("View")
        self.refresh_action = QAction("Refresh categories", self)
        self.refresh_action.setShortcut(QKeySequence("Ctrl+R"))
        self.refresh_action.triggered.connect(self._load_categories)
        fullscreen_action = QAction("Fullscreen\tF", self)
        fullscreen_action.triggered.connect(self.toggle_fullscreen)
        view_menu.addAction(self.refresh_action)
        view_menu.addAction(fullscreen_action)

        help_menu = self.menuBar().addMenu("Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _connect_player(self) -> None:
        self.player.volume_changed.connect(lambda _value: self._schedule_save())
        self.player.mute_changed.connect(lambda _value: self._schedule_save())
        self.player.play_requested.connect(self._play_selected)
        self.player.error_occurred.connect(self._on_player_error)
        self.player.fullscreen_toggle_requested.connect(self.toggle_fullscreen)
        self.player.stopped.connect(self._on_player_stopped)

    def _startup(self) -> None:
        if self._closing:
            return
        if not self.player.available:
            self.statusBar().showMessage(self.player.load_error)
        if not self.config.credentials_readable:
            self.stack.setCurrentWidget(self.login_page)
            self._report_error(
                "unexpected",
                "Saved sign-in data could not be decrypted on this Windows account. Sign in again.",
            )
            return
        if self.config.has_credentials:
            self.stack.setCurrentWidget(self.browser_page)
            self._begin_authentication(
                Credentials(self.config.server_url, self.config.username, self.config.password)
            )
            return
        self.stack.setCurrentWidget(self.login_page)
        self.statusBar().showMessage("Enter your server, username, and password.")

    def _fill_login_fields(self) -> None:
        self.server_edit.setText(self.config.server_url)
        self.username_edit.setText(self.config.username)
        self.password_edit.setText(self.config.password)

    def _sign_in(self) -> None:
        try:
            server = normalize_server_url(self.server_edit.text())
        except XtreamError as exc:
            self.login_status.setText(str(exc))
            self._report_error(exc.code, str(exc))
            return
        username = self.username_edit.text().strip()
        password = self.password_edit.text()
        if not username or not password:
            message = "Enter the username and password."
            self.login_status.setText(message)
            self._report_error("auth", message)
            return
        self.server_edit.setText(server)
        self._begin_authentication(Credentials(server, username, password))

    def _begin_authentication(self, credentials: Credentials) -> None:
        self.client = XtreamClient(credentials)
        self._pending_credentials = credentials
        self._session_id += 1
        session = self._session_id
        self.login_button.setEnabled(False)
        self.login_status.setText("Testing connection…")
        self.statusBar().showMessage("Testing connection…")
        self._start_worker(
            self.client.authenticate,
            lambda account, s=session: self._on_authenticated(s, account),
            lambda code, message, s=session: self._on_auth_failed(s, code, message),
        )

    def _on_authenticated(self, session: int, account: object) -> None:
        if self._closing or session != self._session_id or not isinstance(account, AccountInfo):
            return
        credentials = self._pending_credentials
        if credentials is None:
            return
        self.account = account
        self.config.server_url = credentials.server_url
        self.config.username = credentials.username
        self.config.password = credentials.password
        self.config.credentials_readable = True
        self.account_label.setText(account.summary)
        self.login_button.setEnabled(True)
        self.login_status.setText("")
        self.stack.setCurrentWidget(self.browser_page)
        self._sync_session_actions()
        self.statusBar().showMessage("Signed in")
        try:
            self.config.save()
        except ConfigError as exc:
            self._report_error("unexpected", str(exc))
        self._load_categories()

    def _on_auth_failed(self, session: int, code: str, message: str) -> None:
        if self._closing or session != self._session_id:
            return
        self.client = None
        self.account = None
        self.login_button.setEnabled(True)
        self.login_status.setText(message)
        self.account_label.setText("Not signed in")
        self.stack.setCurrentWidget(self.login_page)
        self._sync_session_actions()
        self.statusBar().showMessage(message)
        if code == "auth":
            self._clear_saved_password()
        self._report_error(code, message)

    def _on_section_clicked(self, button_id: int) -> None:
        section = ("live", "movies", "series")[button_id]
        if section == self._section and not self._episode_mode:
            return
        self._resume_on_launch = False
        self._section = section
        self.config.last_section = section
        self._episode_mode = False
        self._current_series = None
        self._global_search = False
        self._viewing_favourites = False
        self._category_items = []
        self._detail_generation += 1
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.proxy.setFilterRegularExpression(QRegularExpression())
        self._schedule_save()
        self._apply_labels()
        self.model.removeRows(0, self.model.rowCount())
        self.tree.clear()
        self._update_channel_count()
        if self.client is not None and self.account is not None:
            self._load_categories()

    def _load_categories(self) -> None:
        if self.client is None or self.account is None:
            return
        client = self.client
        section = self._section
        self._category_generation += 1
        generation = self._category_generation
        session = self._session_id
        self.statusBar().showMessage("Loading categories…")
        self._start_worker(
            lambda: client.get_categories(section),
            lambda categories, s=session, g=generation: self._on_categories(s, g, categories),
            lambda code, message, s=session, g=generation: self._on_load_failed(
                s, g, "categories", code, message
            ),
        )

    def _on_categories(self, session: int, generation: int, categories: object) -> None:
        if (
            self._closing
            or session != self._session_id
            or generation != self._category_generation
            or not isinstance(categories, list)
        ):
            return
        self._show_categories(categories)
        if categories:
            self.statusBar().showMessage(f"Loaded {len(categories)} categories")
        else:
            label = {"live": "live TV", "movies": "movie", "series": "series"}.get(self._section, "live TV")
            self.statusBar().showMessage(f"No {label} categories were returned for this account.")

    def _show_categories(self, categories: list[LiveCategory]) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        nodes: dict[str, tuple[QTreeWidgetItem, str]] = {}
        for category in categories:
            item = QTreeWidgetItem([category.name])
            item.setData(0, Qt.ItemDataRole.UserRole, category.category_id)
            item.setToolTip(0, category.name)
            nodes[category.category_id] = (item, category.parent_id)
        roots: list[QTreeWidgetItem] = []
        for category_id, (item, parent_id) in nodes.items():
            parent = nodes.get(parent_id)
            if parent is not None and parent_id not in {category_id, "0"}:
                parent[0].addChild(item)
            else:
                roots.append(item)
        self._category_names = {category.category_id: category.name for category in categories}
        self.tree.addTopLevelItems(roots)
        self.tree.sortItems(0, Qt.SortOrder.AscendingOrder)
        favourites = QTreeWidgetItem(["Favourites"])
        favourites.setData(0, Qt.ItemDataRole.UserRole, FAVOURITES_ID)
        favourites.setToolTip(0, "Channels, movies, or series you have saved")
        font = favourites.font(0)
        font.setBold(True)
        favourites.setFont(0, font)
        self.tree.insertTopLevelItem(0, favourites)
        self.tree.expandToDepth(0)
        self.tree.blockSignals(False)
        target = None
        remembered = self._remembered_category()
        if remembered:
            target = self._find_category(remembered)
        if target is None:
            for index in range(self.tree.topLevelItemCount()):
                candidate = self.tree.topLevelItem(index)
                if candidate.data(0, Qt.ItemDataRole.UserRole) != FAVOURITES_ID:
                    target = candidate
                    break
        if target is None and self.tree.topLevelItemCount():
            target = self.tree.topLevelItem(0)
        if target is not None:
            self.tree.setCurrentItem(target)

    def _find_category(self, category_id: str) -> QTreeWidgetItem | None:
        def walk(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if item.data(0, Qt.ItemDataRole.UserRole) == category_id:
                return item
            for index in range(item.childCount()):
                found = walk(item.child(index))
                if found is not None:
                    return found
            return None

        for index in range(self.tree.topLevelItemCount()):
            found = walk(self.tree.topLevelItem(index))
            if found is not None:
                return found
        return None

    def _on_category_changed(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        if current is None or self.client is None:
            return
        category_id = current.data(0, Qt.ItemDataRole.UserRole)
        if not category_id:
            return
        if str(category_id) == FAVOURITES_ID:
            self._remember_category(FAVOURITES_ID)
            self._viewing_favourites = True
            self._global_search = False
            self._episode_mode = False
            self._current_series = None
            self._detail_generation += 1
            self._stream_generation += 1
            self._schedule_save()
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
            self.proxy.setFilterRegularExpression(QRegularExpression())
            self._show_favourite_list()
            return
        self._viewing_favourites = False
        self._global_search = False
        self._remember_category(str(category_id))
        self._episode_mode = False
        self._current_series = None
        self._detail_generation += 1
        self._schedule_save()
        self._apply_labels()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.proxy.setFilterRegularExpression(QRegularExpression())
        client = self.client
        category_id = str(category_id)
        section = self._section
        self._stream_generation += 1
        generation = self._stream_generation
        session = self._session_id
        self.model.removeRows(0, self.model.rowCount())
        self.logos.reset()
        self._update_channel_count()
        self.statusBar().showMessage(self._loading_message())
        self._start_worker(
            lambda: client.get_catalog(section, category_id),
            lambda streams, s=session, g=generation: self._on_streams(s, g, streams),
            lambda code, message, s=session, g=generation: self._on_load_failed(
                s, g, "streams", code, message
            ),
        )

    def _on_streams(self, session: int, generation: int, streams: object) -> None:
        if (
            self._closing
            or session != self._session_id
            or generation != self._stream_generation
            or not isinstance(streams, list)
        ):
            return
        items = [item for item in streams if isinstance(item, MediaItem)]
        self._category_items = items
        if self._section == "series":
            self._series_items = items
        if self._global_search:
            return
        self._show_items(items)
        if self._resume_on_launch:
            self._resume_on_launch = False
            if (
                self._section == "series"
                and self.config.last_kind == "episode"
                and self.config.last_series_id
            ):
                match = next((item for item in items if item.item_id == self.config.last_series_id), None)
                if match is not None:
                    self._open_series(match, resume=True)
                    return
            if self._resume_last_item(items):
                return
        noun = self._noun(len(items))
        self.statusBar().showMessage(f"Loaded {len(items)} {noun}")

    def _show_items(self, items: list[MediaItem]) -> None:
        self.logos.reset()
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.model.blockSignals(True)
        try:
            self.model.removeRows(0, self.model.rowCount())
            for item in items:
                logo = QStandardItem()
                logo.setEditable(False)
                logo.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if item.icon_url:
                    logo.setData(item.icon_url, LOGO_ROLE)
                category_name = self._category_names.get(item.category_id, "")
                name = QStandardItem(self._display_name(item))
                name.setEditable(False)
                name.setData(item, STREAM_ROLE)
                name.setToolTip(f"{item.name}\n{category_name}".strip())
                item_id = QStandardItem(item.item_id)
                item_id.setEditable(False)
                item_id.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                category = QStandardItem(category_name)
                category.setEditable(False)
                category.setToolTip(category_name)
                self.model.appendRow([logo, name, item_id, category])
        finally:
            self.model.blockSignals(False)
        self.model.layoutChanged.emit()
        self.proxy.invalidate()
        self.table.setUpdatesEnabled(True)
        header = self.table.horizontalHeader()
        if self._sorted_channels and header.sortIndicatorSection() >= 0:
            column = header.sortIndicatorSection()
            order = header.sortIndicatorOrder()
        else:
            column = 1
            order = Qt.SortOrder.AscendingOrder
            self._sorted_channels = True
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(column, order)
        self._apply_labels()
        self._update_favourite_button()
        self._schedule_logos()

    def _schedule_logos(self, *_args) -> None:
        if not self._closing:
            self._logo_timer.start()

    def _load_visible_logos(self) -> None:
        count = self.proxy.rowCount()
        if self._closing or count == 0:
            return
        viewport = self.table.viewport()
        top = self.table.indexAt(viewport.rect().topLeft())
        if not top.isValid():
            first = 0
            last = min(count - 1, 40)
        else:
            bottom = self.table.indexAt(viewport.rect().bottomLeft())
            first = max(0, top.row() - 4)
            last = bottom.row() if bottom.isValid() else min(count - 1, first + 40)
            last = min(count - 1, last + 6)
        generation = self.logos.generation
        for row in range(first, last + 1):
            source = self.proxy.mapToSource(self.proxy.index(row, 0))
            item = self.model.item(source.row(), 0)
            if item is None or not item.icon().isNull():
                continue
            url = item.data(LOGO_ROLE)
            if not url:
                continue
            self.logos.request(
                str(url),
                generation,
                lambda icon, logo_item=item, token=generation: self._set_logo(token, logo_item, icon),
            )

    def _set_logo(self, generation: int, item: QStandardItem, icon: QIcon) -> None:
        if generation != self.logos.generation:
            return
        item.setIcon(icon)
        self._schedule_logos()

    def _on_search(self, text: str) -> None:
        if self._episode_mode:
            expression = QRegularExpression(
                QRegularExpression.escape(text.strip()),
                QRegularExpression.PatternOption.CaseInsensitiveOption,
            )
            self.proxy.setFilterRegularExpression(expression)
            self._update_channel_count()
            return
        self._search_timer.start()

    def _run_search(self) -> None:
        if self._closing or self._episode_mode:
            return
        text = self.search.text().strip()
        if not text:
            self._global_search = False
            self.proxy.setFilterRegularExpression(QRegularExpression())
            self._show_items(self._favourite_media() if self._viewing_favourites else self._category_items)
            noun = "favourites" if self._viewing_favourites else self._noun(len(self._category_items))
            count = len(self._favourite_media()) if self._viewing_favourites else len(self._category_items)
            self.statusBar().showMessage(f"{count} {noun}" if self._viewing_favourites else f"Loaded {count} {noun}")
            return
        cached = self._catalog_cache.get(self._section)
        if cached is None:
            self.statusBar().showMessage("Searching all categories…")
            self._load_all_items()
            return
        self._show_global_matches(text)

    def _load_all_items(self) -> None:
        if (
            self.client is None
            or self._section in self._catalog_cache
            or self._section == self._catalog_loading_section
        ):
            return
        client = self.client
        section = self._section
        self._catalog_loading_section = section
        self._search_generation += 1
        generation = self._search_generation
        session = self._session_id
        self._start_worker(
            lambda: client.get_all(section),
            lambda items, s=session, g=generation, sec=section: self._on_all_items(s, g, sec, items),
            lambda code, message, s=session, g=generation: self._on_load_failed(s, g, "search", code, message),
        )

    def _on_all_items(self, session: int, generation: int, section: str, items: object) -> None:
        if self._catalog_loading_section == section:
            self._catalog_loading_section = ""
        if (
            self._closing
            or session != self._session_id
            or generation != self._search_generation
            or not isinstance(items, list)
        ):
            return
        catalog = [item for item in items if isinstance(item, MediaItem)]
        self._catalog_cache[section] = catalog
        if self._section == section and not self._episode_mode and self.search.text().strip():
            self._show_global_matches(self.search.text().strip())

    def _show_global_matches(self, text: str) -> None:
        query = text.casefold()
        matches = [
            item for item in self._catalog_cache.get(self._section, []) if query in item.name.casefold()
        ]
        matches.sort(key=lambda item: (0 if item.name.casefold().startswith(query) else 1, item.name.casefold()))
        total = len(matches)
        self._global_search = True
        self.proxy.setFilterRegularExpression(QRegularExpression())
        self._show_items(matches[:SEARCH_RESULT_LIMIT])
        if total > SEARCH_RESULT_LIMIT:
            self.count_label.setText(f"{SEARCH_RESULT_LIMIT} of {total}")
            self.statusBar().showMessage(
                f"Showing {SEARCH_RESULT_LIMIT} of {total} matches. Type more to narrow the list."
            )
        else:
            noun = "match" if total == 1 else "matches"
            self.count_label.setText(f"{total} {noun}")
            self.statusBar().showMessage(f"{total} {noun} across all categories")

    def _update_channel_count(self) -> None:
        total = self.model.rowCount()
        shown = self.proxy.rowCount()
        if total == 0:
            self.count_label.setText(f"No {self._noun(0)}")
        elif shown == total:
            self.count_label.setText(f"{total} {self._noun(total)}")
        else:
            self.count_label.setText(f"{shown} of {total}")

    def _apply_labels(self) -> None:
        placeholder = {
            "live": "Search all channels",
            "movies": "Search all movies",
            "series": "Search all series",
        }.get(self._section, "Search all channels")
        if self._episode_mode and self._current_series is not None:
            title = self._current_series.name
            column = "Episode"
            placeholder = "Search episodes"
            self.back_button.setVisible(True)
        elif self._global_search:
            title, column = "All categories", self._column_title()
            self.back_button.setVisible(False)
        elif self._viewing_favourites:
            title, column = "Favourites", self._column_title()
            self.back_button.setVisible(False)
        else:
            title, column = self._section_heading()
            self.back_button.setVisible(False)
        self.list_title.setText(title)
        self.search.setPlaceholderText(placeholder)
        self.model.setHorizontalHeaderLabels(["", column, "ID", "Category"])
        self.table.setColumnHidden(3, self._episode_mode)
        self._update_channel_count()

    def _section_heading(self) -> tuple[str, str]:
        if self._section == "movies":
            return "Movies", "Movie"
        if self._section == "series":
            return "Series", "Series"
        return "Channels", "Channel"

    def _column_title(self) -> str:
        return self._section_heading()[1]

    def _display_name(self, item: MediaItem) -> str:
        if item.kind != "episode" and self.config.is_favourite(self._section, item.item_id):
            return f"★ {item.name}"
        return item.name

    def _favourite_media(self) -> list[MediaItem]:
        items = [
            MediaItem(
                item_id=entry["item_id"],
                name=entry["name"],
                icon_url=entry["icon_url"],
                category_id=entry["category_id"],
                kind=entry["kind"],
                extension=entry["extension"],
                direct_url=entry["direct_url"],
            )
            for entry in self.config.favourites_for(self._section)
        ]
        items.sort(key=lambda item: item.name.casefold())
        return items

    def _show_favourite_list(self) -> None:
        items = self._favourite_media()
        self._show_items(items)
        if self._resume_on_launch:
            self._resume_on_launch = False
            if (
                self._section == "series"
                and self.config.last_kind == "episode"
                and self.config.last_series_id
            ):
                match = next((item for item in items if item.item_id == self.config.last_series_id), None)
                if match is not None:
                    self._open_series(match, resume=True)
                    return
            if self._resume_last_item(items):
                return
        count = len(items)
        self.statusBar().showMessage("No favourites yet" if count == 0 else f"{count} favourites")

    def _items_from_model(self) -> list[MediaItem]:
        items: list[MediaItem] = []
        for row in range(self.model.rowCount()):
            name_item = self.model.item(row, 1)
            item = name_item.data(STREAM_ROLE) if name_item is not None else None
            if isinstance(item, MediaItem):
                items.append(item)
        return items

    def _favourite_target(self) -> MediaItem | None:
        if self._episode_mode and self._current_series is not None:
            return self._current_series
        item = self._item_from_index(self.table.currentIndex())
        if item is None or item.kind == "episode":
            return None
        return item

    def _update_favourite_button(self) -> None:
        target = self._favourite_target()
        if target is None:
            self.favourite_button.setEnabled(False)
            self.favourite_button.setText("Favourite")
            return
        saved = self.config.is_favourite(self._section, target.item_id)
        self.favourite_button.setEnabled(True)
        self.favourite_button.setText("Unfavourite" if saved else "Favourite")

    def _toggle_selected_favourite(self) -> None:
        target = self._favourite_target()
        if target is None or not self.config.server_url or not self.config.username:
            return
        entry = {
            "section": self._section,
            "kind": target.kind,
            "item_id": target.item_id,
            "name": target.name,
            "icon_url": target.icon_url,
            "category_id": target.category_id,
            "extension": target.extension,
            "direct_url": target.direct_url,
            "server_url": self.config.server_url,
            "username": self.config.username,
        }
        added = self.config.toggle_favourite(entry)
        self._schedule_save()
        if self._viewing_favourites and not self._global_search and not self._episode_mode:
            self._show_favourite_list()
        else:
            self._refresh_favourite_marks()
        self._update_favourite_button()
        self.statusBar().showMessage("Added to favourites" if added else "Removed from favourites")

    def _refresh_favourite_marks(self) -> None:
        for row in range(self.model.rowCount()):
            name_item = self.model.item(row, 1)
            item = name_item.data(STREAM_ROLE) if name_item is not None else None
            if isinstance(item, MediaItem):
                name_item.setText(self._display_name(item))

    def _noun(self, count: int) -> str:
        if self._episode_mode:
            return "episode" if count == 1 else "episodes"
        if self._section == "movies":
            return "movie" if count == 1 else "movies"
        if self._section == "series":
            return "series"
        return "channel" if count == 1 else "channels"

    def _loading_message(self) -> str:
        if self._section == "movies":
            return "Loading movies…"
        if self._section == "series":
            return "Loading series…"
        return "Loading channels…"

    def _remembered_category(self) -> str:
        if self._section == "movies":
            return self.config.last_movie_category_id
        if self._section == "series":
            return self.config.last_series_category_id
        return self.config.last_live_category_id or self.config.last_category_id

    def _remember_category(self, category_id: str) -> None:
        if self._section == "movies":
            self.config.last_movie_category_id = category_id
        elif self._section == "series":
            self.config.last_series_category_id = category_id
        else:
            self.config.last_live_category_id = category_id
            self.config.last_category_id = category_id

    def _channel_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        self.table.selectRow(index.row())
        item = self._item_from_index(index)
        if item is None:
            return
        menu = QMenu(self)
        action = menu.addAction("Episodes" if item.kind == "series" else "Play")
        favourite_target = self._current_series if item.kind == "episode" and self._current_series else item
        favourite_action = None
        if favourite_target is not None and favourite_target.kind in {"live", "movie", "series"}:
            saved = self.config.is_favourite(self._section, favourite_target.item_id)
            label = "Remove from favourites" if saved else "Add to favourites"
            if item.kind == "episode":
                label = "Unfavourite this series" if saved else "Favourite this series"
            favourite_action = menu.addAction(label)
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is action:
            self._activate_item(item)
        elif favourite_action is not None and chosen is favourite_action:
            if item.kind == "episode":
                self.table.clearSelection()
            self._toggle_selected_favourite()

    def _play_selected(self) -> None:
        self._play_index(self.table.currentIndex())

    def _play_index(self, index: QModelIndex) -> None:
        item = self._item_from_index(index)
        if item is None:
            self.statusBar().showMessage("Select a title first.")
            return
        self._activate_item(item)

    def _item_from_index(self, index: QModelIndex) -> MediaItem | None:
        if not index.isValid():
            return None
        source = self.proxy.mapToSource(index)
        name_item = self.model.item(source.row(), 1)
        if name_item is None:
            return None
        item = name_item.data(STREAM_ROLE)
        return item if isinstance(item, MediaItem) else None

    def _activate_item(self, item: MediaItem) -> None:
        if item.kind == "series":
            self._open_series(item)
            return
        self._play_item(item)

    def _open_series(self, series: MediaItem, resume: bool = False) -> None:
        if self.client is None:
            self._report_error("auth", "Sign in before opening a series.")
            return
        self._return_items = self._items_from_model()
        self._return_global = self._global_search
        client = self.client
        self._current_series = series
        self._detail_generation += 1
        generation = self._detail_generation
        session = self._session_id
        self.statusBar().showMessage(f"Loading episodes for {series.name}…")
        self._start_worker(
            lambda: client.get_series_episodes(series.item_id, series.name, series.icon_url),
            lambda episodes, s=session, g=generation, r=resume: self._on_episodes(s, g, episodes, r),
            lambda code, message, s=session, g=generation: self._on_load_failed(
                s, g, "episodes", code, message
            ),
        )

    def _on_episodes(self, session: int, generation: int, episodes: object, resume: bool) -> None:
        if (
            self._closing
            or session != self._session_id
            or generation != self._detail_generation
            or not isinstance(episodes, list)
        ):
            return
        items = [item for item in episodes if isinstance(item, MediaItem)]
        self._episode_mode = True
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.proxy.setFilterRegularExpression(QRegularExpression())
        self._show_items(items)
        if resume and self._resume_last_item(items):
            return
        noun = self._noun(len(items))
        name = self._current_series.name if self._current_series is not None else "this series"
        self.statusBar().showMessage(f"{len(items)} {noun} in {name}")

    def _back_to_series(self) -> None:
        self._episode_mode = False
        self._current_series = None
        self._detail_generation += 1
        self._global_search = self._return_global
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.proxy.setFilterRegularExpression(QRegularExpression())
        if self._return_items is not None:
            items = self._return_items
        elif self._viewing_favourites:
            items = self._favourite_media()
        else:
            items = self._series_items
        self._show_items(items)
        if self._global_search:
            self.statusBar().showMessage(f"{len(items)} matches across all categories")
        elif self._viewing_favourites:
            self.statusBar().showMessage(f"{len(items)} favourites")
        else:
            self.statusBar().showMessage(f"Loaded {len(items)} {self._noun(len(items))}")

    def _play_item(self, item: MediaItem) -> None:
        if not item.playable:
            return
        if self.client is None:
            self._report_error("auth", "Sign in before playing.")
            return
        if not self.player.available:
            self._report_error("stream", self.player.load_error or "VLC is not available.")
            return
        self.config.last_section = self._section
        self.config.last_stream_id = item.item_id
        self.config.last_stream_name = item.name
        self.config.last_kind = item.kind
        self.config.last_series_id = item.series_id or (
            self._current_series.item_id if item.kind == "episode" and self._current_series is not None else ""
        )
        self._schedule_save()
        self._select_item(item.item_id)
        self.setWindowTitle(f"{item.name} — {APP_NAME}")
        self.statusBar().showMessage(f"Playing {item.name}")
        self.player.play_url(self.client.url_for(item), item.name, seekable=item.kind != "live")

    def _resume_last_item(self, items: list[MediaItem]) -> bool:
        wanted = self.config.last_stream_id
        if not wanted:
            return False
        for item in items:
            if item.item_id == wanted and item.playable:
                self._play_item(item)
                return True
        return False

    def _select_item(self, item_id: str) -> None:
        for row in range(self.model.rowCount()):
            name_item = self.model.item(row, 1)
            item = name_item.data(STREAM_ROLE) if name_item is not None else None
            if not isinstance(item, MediaItem) or item.item_id != item_id:
                continue
            proxy_index = self.proxy.mapFromSource(name_item.index())
            if proxy_index.isValid():
                self.table.selectRow(proxy_index.row())
                self.table.scrollTo(proxy_index)
            return

    def _on_player_error(self, message: str) -> None:
        if self._closing:
            return
        self.player.state_label.setText("Error")
        self.player.title_label.setText("Playback failed")
        self.statusBar().showMessage(message)
        self.setWindowTitle(APP_NAME)
        self._report_error("stream", message)

    def _on_player_stopped(self) -> None:
        if not self._closing:
            self.setWindowTitle(APP_NAME)

    def _on_load_failed(self, session: int, generation: int, kind: str, code: str, message: str) -> None:
        if self._closing or session != self._session_id:
            return
        if kind == "categories":
            current = self._category_generation
        elif kind == "episodes":
            current = self._detail_generation
        elif kind == "search":
            current = self._search_generation
        else:
            current = self._stream_generation
        if generation != current:
            return
        if kind in {"streams", "episodes"}:
            self._resume_on_launch = False
        if kind == "search":
            self._catalog_loading_section = ""
        self.statusBar().showMessage(message)
        if code == "auth":
            self.client = None
            self.account = None
            self.player.stop()
            self.login_button.setEnabled(True)
            self.login_status.setText(message)
            self.account_label.setText("Not signed in")
            self.stack.setCurrentWidget(self.login_page)
            self._sync_session_actions()
            self._clear_saved_password()
        self._report_error(code, message)

    def _clear_saved_password(self) -> None:
        self.config.password = ""
        self.password_edit.clear()
        try:
            self.config.save()
        except ConfigError:
            pass

    def _logout(self) -> None:
        if self.isFullScreen():
            self.set_fullscreen(False)
        self._session_id += 1
        self._category_generation += 1
        self._stream_generation += 1
        self._resume_on_launch = False
        self._catalog_cache.clear()
        self._catalog_loading_section = ""
        self._category_items = []
        self._category_names = {}
        self._global_search = False
        self._viewing_favourites = False
        self.player.stop()
        self.client = None
        self.account = None
        self._pending_credentials = None
        self.config.clear_credentials()
        self.tree.clear()
        self.model.removeRows(0, self.model.rowCount())
        self.logos.reset()
        self.search.clear()
        self._update_channel_count()
        self.password_edit.clear()
        self.account_label.setText("Not signed in")
        self.stack.setCurrentWidget(self.login_page)
        self.login_button.setEnabled(True)
        self.login_status.setText("Signed out.")
        self._sync_session_actions()
        self.setWindowTitle(APP_NAME)
        self.statusBar().showMessage("Signed out")
        try:
            self.config.save()
        except ConfigError as exc:
            self._report_error("unexpected", str(exc))

    def _sync_session_actions(self) -> None:
        signed_in = self.client is not None and self.account is not None
        self.logout_action.setEnabled(signed_in)
        self.refresh_action.setEnabled(signed_in)

    def _start_worker(self, func, on_ok, on_fail) -> None:
        worker = ApiWorker(func)
        worker.succeeded.connect(on_ok, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(on_fail, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(lambda w=worker: self._forget_worker(w))
        self._workers.append(worker)
        self.progress.setVisible(True)
        worker.start()

    def _forget_worker(self, worker: ApiWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        self.progress.setVisible(bool(self._workers))

    def _schedule_save(self) -> None:
        if self.config.credentials_readable and not self._closing:
            self._save_timer.start()

    def _persist_preferences(self) -> None:
        if not self.config.credentials_readable:
            return
        self.config.volume = self.player.volume_slider.value()
        self.config.muted = self.player.mute_button.isChecked()
        try:
            self.config.save()
        except ConfigError as exc:
            self.statusBar().showMessage(str(exc))

    def toggle_fullscreen(self) -> None:
        if self.stack.currentWidget() is not self.browser_page:
            return
        self.set_fullscreen(not self.isFullScreen())

    def set_fullscreen(self, enabled: bool) -> None:
        if self.stack.currentWidget() is not self.browser_page:
            return
        if enabled == self.isFullScreen():
            self.player.set_fullscreen_button_text(enabled)
            return
        if enabled:
            self._splitter_sizes = self.splitter.sizes()
            self.section_bar.hide()
            self.category_panel.hide()
            self.channel_panel.hide()
            self.menuBar().hide()
            self.statusBar().hide()
            self.showFullScreen()
        else:
            self.menuBar().show()
            self.statusBar().show()
            self.section_bar.show()
            self.category_panel.show()
            self.channel_panel.show()
            self.showNormal()
            sizes = self._splitter_sizes
            if sizes:
                QTimer.singleShot(0, lambda saved=list(sizes): self.splitter.setSizes(saved))
        self.player.set_fullscreen_button_text(self.isFullScreen())
        QTimer.singleShot(0, self.player.attach_output)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.KeyPress and QApplication.activeModalWidget() is None:
            if event.isAutoRepeat():
                return super().eventFilter(watched, event)
            focus = QApplication.focusWidget()
            typing = isinstance(focus, QLineEdit)
            no_modifiers = event.modifiers() == Qt.KeyboardModifier.NoModifier
            if (
                event.key() == Qt.Key.Key_F
                and no_modifiers
                and not typing
                and self.stack.currentWidget() is self.browser_page
            ):
                self.toggle_fullscreen()
                return True
            if event.key() == Qt.Key.Key_Escape and no_modifiers and self.isFullScreen():
                self.set_fullscreen(False)
                return True
        return super().eventFilter(watched, event)

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "About IPTV Player",
            "IPTV Player plays live channels, movies, and series from an Xtream Codes server.\n\n"
            "Sign in with the server URL, username, and password from your provider. "
            "The password is encrypted with Windows DPAPI and stored in your roaming profile.\n\n"
            "Use Live TV, Movies, or Series at the top. Search covers every category in that section. "
            "Favourite a channel, movie, or series to keep it in Favourites. "
            "Double-click a series to open its episodes. "
            "Movies and episodes have a timeline under the video. "
            "Press F or double-click the video for fullscreen. Press Esc to leave fullscreen.",
        )

    def _report_error(self, code: str, message: str) -> None:
        if self._closing:
            return
        QMessageBox.warning(self, ERROR_TITLES.get(code, "Something went wrong"), message)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._closing = True
        self._session_id += 1
        application = QApplication.instance()
        if application is not None:
            application.removeEventFilter(self)
        self._persist_preferences()
        self.player.shutdown()
        for worker in list(self._workers):
            worker.wait(1500)
        event.accept()


_STYLESHEET = """
QMainWindow, QWidget {
    background: #1b1e22;
    color: #e6e8eb;
    font-family: "Segoe UI";
    font-size: 10pt;
}
QLabel#sectionTitle, QLabel#loginTitle {
    font-size: 14pt;
    font-weight: 600;
    background: transparent;
}
QLabel#loginSubtitle, QLabel#loginStatus, QLabel#countLabel {
    color: #9aa3ad;
    background: transparent;
}
QLabel#nowPlaying {
    color: #d5dbe3;
    background: transparent;
}
QFrame#loginCard {
    background: #23272e;
    border: 1px solid #343a42;
    border-radius: 12px;
}
QLineEdit {
    background: #15181c;
    border: 1px solid #343a42;
    border-radius: 6px;
    padding: 8px;
    selection-background-color: #2f5f9e;
}
QTreeWidget, QTableView {
    background: #23272e;
    alternate-background-color: #1e2228;
    border: 1px solid #343a42;
    border-radius: 6px;
    selection-background-color: #2f5f9e;
    selection-color: #ffffff;
    outline: none;
}
QHeaderView::section {
    background: #23272e;
    color: #9aa3ad;
    border: none;
    padding: 6px;
}
QPushButton {
    background: #2c333d;
    border: 1px solid #3c4450;
    border-radius: 6px;
    padding: 6px 12px;
}
QPushButton:hover { background: #36404c; }
QPushButton:disabled { color: #7d868f; background: #262b32; }
QPushButton#primary {
    background: #2f6fed;
    border-color: #2f6fed;
    color: white;
    padding: 8px 12px;
}
QPushButton#primary:hover { background: #3d7cf5; }
QPushButton#section:checked {
    background: #2f6fed;
    border-color: #2f6fed;
    color: white;
}
QStatusBar { background: #15181c; color: #9aa3ad; }
QMenuBar { background: #15181c; }
QMenuBar::item:selected, QMenu::item:selected { background: #2f5f9e; }
QMenu { background: #23272e; border: 1px solid #343a42; }
QSplitter::handle { background: #15181c; }
QProgressBar { border: none; background: #2c333d; max-height: 8px; }
QProgressBar::chunk { background: #3d8bfd; }
QSlider::groove:horizontal { height: 4px; background: #3c4450; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 14px;
    margin: -5px 0;
    background: #e6e8eb;
    border-radius: 7px;
}
"""
