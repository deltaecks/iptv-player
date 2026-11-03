"""Launch the IPTV desktop application."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def main() -> int:
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
    icon_file = Path(__file__).resolve().parent / "assets" / "iptv-player.ico"
    if icon_file.is_file():
        app.setWindowIcon(QIcon(str(icon_file)))

    from ui_main import MainWindow

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
