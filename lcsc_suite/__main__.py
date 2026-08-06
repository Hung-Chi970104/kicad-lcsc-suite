"""Entry point: ``python -m lcsc_suite``.

KiCad launches ``run.sh``, which clears the poisoned environment (trap 1) and
execs this. Everything it needs — the socket path and the token — arrives in the
environment KiCad set.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import app as app_module, kicad_bridge
from .config import Settings, legacy_settings_path
from .single_instance import SingleInstance

log = logging.getLogger(__name__)


def parse_args(argv=None) -> argparse.Namespace:
    """Parse the launcher's arguments."""
    parser = argparse.ArgumentParser(prog="lcsc_suite", description=__doc__)
    parser.add_argument(
        "--fixture",
        metavar="PATH",
        help="Run against a JSON fixture board instead of a live KiCad. "
        "Used by the probe and by anyone without KiCad to hand.",
    )
    parser.add_argument(
        "--theme",
        choices=("light", "dark"),
        help="Force an appearance instead of following the desktop.",
    )
    parser.add_argument("--debug", action="store_true", help="Log at DEBUG.")
    parser.add_argument(
        "--allow-multiple",
        action="store_true",
        help="Skip the single-instance guard. For debugging only — two windows "
        "on one board share a project database and will overwrite each other.",
    )
    # KiCad appends its own arguments to the entrypoint; accept and ignore
    # anything unrecognised rather than exiting with a usage error the user
    # will never see.
    return parser.parse_known_args(argv)[0]


def main(argv=None) -> int:
    """Open the main window against the live board, or a fixture."""
    args = parse_args(argv)
    app_module.configure_logging(logging.DEBUG if args.debug else logging.INFO)

    application = app_module.build_application(theme_mode=args.theme)

    try:
        if args.fixture:
            board = kicad_bridge.open_fixture(args.fixture)
        else:
            board = kicad_bridge.connect()
    except kicad_bridge.NotConnected as exc:
        log.error("%s", exc)
        log.error("Environment: %s", kicad_bridge.environment_report())
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(None, "LCSC Suite", str(exc))
        return 1

    info = board.info()

    # KiCad runs the launcher afresh on every toolbar click, so the guard has to
    # live here rather than in a window lookup. Two windows on one board share a
    # project database and would quietly overwrite each other.
    guard = SingleInstance(info.path)
    if not args.allow_multiple and not guard.acquire():
        log.info("An LCSC Suite window is already open for %s; raised it.", info.name)
        return 0

    from .ui.main_window import MainWindow

    settings = Settings(legacy_path=legacy_settings_path())
    window = MainWindow(board, settings=settings)
    guard.raise_requested.connect(window.raise_to_front)
    application.aboutToQuit.connect(guard.release)
    window.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
