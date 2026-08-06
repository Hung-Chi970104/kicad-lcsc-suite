"""Application bootstrap: one place that builds a QApplication our way.

Kept separate from ``__main__`` so ``scripts/qt_probe.py`` can build the very
same application — same style, same palette, same font — without going through
the launcher. A screenshot is only evidence if it came out of the app the user
runs.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from PySide6.QtWidgets import QApplication

from .ui import theme

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(funcName)s %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"


def configure_logging(level: int = logging.INFO) -> None:
    """Send logs to stderr.

    The launcher redirects stderr to a file, which is the only place a crash
    during start-up can be read from: KiCad shows nothing at all when an
    ``exec`` plugin dies.
    """
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    # Both are chatty at DEBUG and neither tells us anything we want.
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_application(
    argv: Optional[list] = None,
    theme_mode: Optional[str] = None,
    offscreen: bool = False,
) -> QApplication:
    """Return a QApplication with Fusion and our palette already applied.

    ``offscreen`` sets ``QT_QPA_PLATFORM`` before Qt initialises, which has to
    happen before the first ``QApplication`` exists — hence the flag rather than
    leaving it to the caller's environment.
    """
    if offscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        # Fonts are the one thing the offscreen platform still probes for, and
        # the warning it prints otherwise ends up in every screenshot log.
        os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

    existing = QApplication.instance()
    if existing is not None:
        theme.apply(existing, theme_mode)
        return existing

    app = QApplication(argv if argv is not None else sys.argv[:1])
    # Both names feed QStandardPaths, which qualifies the config directory by
    # organisation *and* application. Keeping them distinct avoids the
    # ".../LCSC Suite/LCSC Suite/" a matching pair produces.
    app.setOrganizationName("kicad-lcsc-suite")
    app.setApplicationName("LCSC Suite")
    app.setApplicationDisplayName("LCSC Suite")
    theme.apply(app, theme_mode)
    return app
