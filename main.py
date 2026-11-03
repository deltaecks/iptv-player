"""Launch the IPTV desktop application."""

from __future__ import annotations

import logging
import sys

from windows_setup import prepare_windows_install


def main() -> int:
    if prepare_windows_install():
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        sys.stderr.write(
            "PyQt6 is not installed. From this folder run:\n"
            "    py -3 -m pip install -r requirements.txt\n"
        )
        return 1

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("IPTV Player")
    app.setOrganizationName("IPTVPlayer")
    from app_paths import icon_file

    bundled_icon = icon_file()
    if bundled_icon.is_file():
        app.setWindowIcon(QIcon(str(bundled_icon)))

    from ui_main import MainWindow

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
