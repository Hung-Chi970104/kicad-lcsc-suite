"""Entry point: ``python -m lcsc_suite``.

KiCad launches ``run.sh``, which clears the poisoned environment (trap 1) and
execs this. Everything it needs — the socket path and the token — arrives in the
environment KiCad set.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile

from . import APP_NAME, app as app_module, board_watch, kicad_bridge
from .config import Settings, adopt_data_directory, legacy_settings_path
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
            # The committed fixture names a project path that does not exist,
            # deliberately — nothing should be able to write into the checkout
            # by opening it. But store.py really does create
            # ``<project>/jlcpcb/project.db``, so opening it here without
            # somewhere to put that died on `Read-only file system: '/fixture'`.
            # The probe and the tests have always relocated first; this entry
            # point never did, which made a documented way to run the app one
            # that could not start.
            board.relocate(
                os.path.join(tempfile.mkdtemp(prefix="lcsc-fixture-"), "tempctrl")
            )
        else:
            board = kicad_bridge.connect()
    except kicad_bridge.NotConnected as exc:
        log.error("%s", exc)
        log.error("Environment: %s", kicad_bridge.environment_report())
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(None, APP_NAME, str(exc))
        return 1

    info = board.info()

    # KiCad runs the launcher afresh on every toolbar click, so the guard has to
    # live here rather than in a window lookup. Two windows on one board share a
    # project database and would quietly overwrite each other.
    guard = SingleInstance(info.path)
    if not args.allow_multiple and not guard.acquire():
        log.info("An %s window is already open for %s; raised it.", APP_NAME, info.name)
        return 0

    from .controller import build as build_controller
    from .parts import PartList
    from .search_source import build_source

    settings = Settings(legacy_path=legacy_settings_path())
    # Before any Library is built: an install that has never had a database
    # directory configured is pointed at one it already has, and the answer is
    # written to settings so it stops depending on where any module lives. See
    # config.adopt_data_directory for what that dependency has already cost.
    adopt_data_directory(settings)
    parts = PartList(board, settings=settings)
    parts.open_libraries()
    # Named here rather than left to the controller's lazy default. Building a
    # LiveSource costs nothing — it is a namespace over lcsc/api.py — and the
    # BOM estimator only runs its assembly-metadata lookup when it has been
    # given a source, so that omitting one means "no network" everywhere else.
    # This is the one place that is meant to have one.
    controller = build_controller(
        board, parts, settings=settings, source=build_source()
    )
    window = controller.window
    guard.raise_requested.connect(window.raise_to_front)
    application.aboutToQuit.connect(guard.release)
    # Follow the board out. Scoped to *this* board in *this* KiCad, so a second
    # project closing leaves its window alone; see board_watch's docstring. A
    # fixture board never closes, so this costs the probe and CI nothing.
    board_watch.close_window_with_board(board, window, application)
    window.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
