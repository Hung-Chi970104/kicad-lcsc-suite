"""The LCSC Suite main window.

Phase 0 of the migration: the window, its title and its size, so that the
toolbar button has something to open and ``qt_probe.py`` has something to
screenshot. Toolbars, the part table and the log pane arrive in Phases 1 and 2.

The target layout is the plan's §5.1, at 1300x772.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QVBoxLayout, QWidget

from .. import __version__
from ..kicad_bridge import Board
from . import theme

#: The wx window is 1300x772. Stated rather than derived so the screenshots are
#: the same size on both platforms and a parity diff means something.
DEFAULT_SIZE = (1300, 772)


class MainWindow(QMainWindow):
    """Top-level window. Owns the board connection and every dialog."""

    def __init__(self, board: Board, settings=None, parent=None) -> None:
        super().__init__(parent)
        self.board = board
        self.settings = settings
        self.setObjectName("lcsc-suite-main")

        info = board.info()
        self.setWindowTitle(f"LCSC Suite — {info.name}")
        self.resize(*DEFAULT_SIZE)

        central = QWidget(self)
        central.setObjectName("central")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)

        placeholder = QLabel(
            f"Connected to KiCad {info.kicad_version}\n"
            f"{info.path}\n"
            f"{len(board.footprints())} footprints\n\n"
            f"LCSC Suite {__version__} — Phase 0 skeleton",
            central,
        )
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setProperty("role", "status")
        placeholder.setFont(theme.base_font())
        layout.addWidget(placeholder)

        self.setCentralWidget(central)
