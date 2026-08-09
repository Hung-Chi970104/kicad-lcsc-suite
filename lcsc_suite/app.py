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

from . import config
from .ui import brand, theme

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(funcName)s %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"


def configure_logging(level: int = logging.INFO) -> None:
    """Send logs to stderr.

    The launcher redirects stderr to a file, which is the only place a crash
    during start-up can be read from: KiCad shows nothing at all when an
    ``exec`` plugin dies.
    """
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    # ``basicConfig`` does nothing at all when the root already has handlers, and
    # by the time this runs it does: importing ``shared`` pulls in
    # ``derive_params``, which calls ``basicConfig(DEBUG)`` of its own at import.
    # So the level has to be set outright, or the log pane fills with a DEBUG
    # line per part and the app looks like it is malfunctioning.
    logging.getLogger().setLevel(level)
    # Both are chatty at DEBUG and neither tells us anything we want.
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _ensure_offscreen_fonts() -> None:
    """Point Qt at the system fonts when the offscreen platform cannot find any.

    On Linux and macOS the offscreen plugin reaches the platform's own font
    database and this does nothing. **On Windows it does not**: it falls back to
    a basic database that reads a directory, and with no directory to read every
    glyph renders as a missing-glyph box.

    That is not cosmetic and it is not only CI's problem. It silently converts a
    screenshot into a picture of tofu whose *metrics* are wrong too — the first
    Windows render came back with the main toolbar's extension arrow showing,
    which reads exactly like the real "the buttons do not fit on Windows" bug
    this migration exists to catch. A gate that cannot tell those two apart is
    worse than no gate.

    Only ever a default: an explicit ``QT_QPA_FONTDIR`` wins.
    """
    if sys.platform != "win32" or os.environ.get("QT_QPA_FONTDIR"):
        return
    fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    if os.path.isdir(fonts):
        os.environ["QT_QPA_FONTDIR"] = fonts


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
        _ensure_offscreen_fonts()

    existing = QApplication.instance()
    if existing is not None:
        theme.apply(existing, theme_mode)
        return existing

    app = QApplication(argv if argv is not None else sys.argv[:1])
    # Both names feed QStandardPaths, which qualifies the config directory by
    # organisation *and* application. Keeping them distinct avoids the
    # ".../LCSC Suite/LCSC Suite/" a matching pair produces.
    #
    # **These two are storage keys and the rebrand deliberately did not touch
    # them.** They are half the path under which every existing user's settings
    # and their optional 750MB parts database already sit. Renaming them here
    # does not move that data, it strands it — silently, because a fresh config
    # directory looks exactly like a first run. Only the *display* name below
    # became EasyAssembly; see ``config.adopt_data_directory`` for the cost of
    # getting this wrong, which this project has already paid twice.
    app.setOrganizationName("kicad-lcsc-suite")
    app.setApplicationName(config.APPLICATION_NAME)
    # What the user sees: window titles that do not state their own, the macOS
    # menu bar, the task switcher.
    app.setApplicationDisplayName(brand.APP_NAME)
    theme.apply(app, theme_mode)
    return app
