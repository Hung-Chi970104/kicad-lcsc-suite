#!/usr/bin/env python3
"""Render any LCSC Suite screen offscreen, screenshot it, dump its geometry.

**This is the acceptance tool for every phase of the Qt migration.** A UI change
is not done until the screen it touched has been rendered here and the PNG has
been looked at. That rule exists because the previous UI could only be checked
by geometry dumps, and geometry dumps miss what users see — which is the reason
this migration is happening at all.

    .venv/bin/python scripts/qt_probe.py mainwindow
    .venv/bin/python scripts/qt_probe.py --all --theme dark
    .venv/bin/python scripts/qt_probe.py explorer --geometry

No display, no window manager, no screen-recording permission: Qt renders into
an offscreen platform plugin and ``QWidget.grab()`` produces the pixels. Because
the style is forced to Fusion and the fonts are stated explicitly, the PNG this
writes on macOS is evidence about Windows too.

Screens are built against a **fixture board** (``lcsc_suite/fixtures/board.json``,
derived from a real 110-footprint PCB) rather than a live KiCad, so a run is
reproducible and works in CI. ``--live`` connects to a running KiCad instead,
for the times the question is about real data.

Exit status is nonzero if any screen raised while building.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Offscreen has to be chosen before Qt initialises. Doing it here rather than
# leaving it to the caller's environment is what makes the CI job a one-liner.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from lcsc_suite import (
    app as app_module,  # noqa: E402
    kicad_bridge,  # noqa: E402
)
from lcsc_suite.config import DEFAULTS, Settings  # noqa: E402

DEFAULT_FIXTURE = os.path.join(_ROOT, "lcsc_suite", "fixtures", "board.json")
DEFAULT_OUTPUT_DIR = os.path.join(_ROOT, "docs", "screens")

#: How long to let the event loop run before grabbing. Layout, deferred column
#: sizing and any single-shot timer a screen uses to finish itself off all need
#: a turn of the loop; 400ms is comfortably more than any of them take and
#: still keeps ``--all`` under a couple of seconds.
SETTLE_MS = 400


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def settle(milliseconds: int = SETTLE_MS) -> None:
    """Run the event loop for a while so deferred layout completes."""
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()
    QApplication.processEvents()


def probe_settings() -> Settings:
    """Build settings for a probe run: the shipped defaults, never the user's.

    A screenshot that depends on whatever the developer last toggled is not
    evidence about anything. Writes go to a throwaway path.
    """
    # A path inside a throwaway directory, not a file: Settings treats an
    # existing-but-empty file as corrupt and says so, which is noise here.
    scratch = tempfile.mkdtemp(prefix="lcsc-probe-")
    settings = Settings(path=os.path.join(scratch, "settings.json"))
    settings.values.clear()
    settings.values.update({key: dict(value) for key, value in DEFAULTS.items()})
    return settings


def dump_tree(widget: QWidget, depth: int = 0) -> None:
    """Print the widget tree with sizes — the geometry half of the probe.

    Useful for finding a pane that collapsed to zero, which a screenshot shows
    but does not measure. It is a *supplement* to the PNG, never a substitute.
    """
    geometry = widget.geometry()
    name = widget.objectName() or ""
    text = ""
    for getter in ("text", "windowTitle"):
        value = getattr(widget, getter, None)
        if callable(value):
            try:
                text = (value() or "")[:40]
            except TypeError:
                text = ""
            if text:
                break
    print(
        f"{'  ' * depth}{type(widget).__name__}"
        f"{f'#{name}' if name else ''} "
        f"{geometry.width()}x{geometry.height()}@{geometry.x()},{geometry.y()}"
        f"{f' {text!r}' if text else ''}"
        f"{'' if widget.isVisible() else ' [hidden]'}"
    )
    for child in widget.children():
        if isinstance(child, QWidget):
            dump_tree(child, depth + 1)


def _descendants(widget: QWidget):
    """Yield every widget beneath ``widget``, depth first."""
    for child in widget.children():
        if isinstance(child, QWidget):
            yield child
            yield from _descendants(child)


def dump_table(view, label: str) -> None:
    """Print a table view's columns and widths.

    Column widths are the classic silent regression: a column that collapses is
    obvious in the PNG only if you know what it should have been.
    """
    model = view.model()
    if model is None:
        print(f"{label}: no model")
        return
    print(f"{label}: {model.rowCount()} rows, {model.columnCount()} columns")
    total = 0
    for column in range(model.columnCount()):
        header = model.headerData(column, view.horizontalHeader().orientation())
        width = view.columnWidth(column)
        hidden = view.isColumnHidden(column)
        total += 0 if hidden else width
        print(
            f"  {column}: {str(header):<20} width={width:>4}"
            f"{' HIDDEN' if hidden else ''}"
        )
    print(f"  total visible width={total}, viewport={view.viewport().width()}")


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


def _main_window(context):
    """Build the main window against the probe's board and settings."""
    from lcsc_suite.parts import PartList
    from lcsc_suite.ui.main_window import MainWindow

    window = MainWindow(
        context.board,
        settings=context.settings,
        parts=PartList(context.board, settings=context.settings),
    )
    window.show()
    return window


def screen_mainwindow(context) -> QWidget:
    """Build the main window (plan §5.1)."""
    return _main_window(context)


def screen_mainwindow_unassigned(context) -> QWidget:
    """Build the main window scrolled to the first part needing a number.

    A separate screen because the default view does not reach one: the fixture's
    unassigned-and-in-the-BOM parts sit well down an alphabetical list, and the
    row colouring is the thing most worth being able to look at. Red here means
    "in the BOM with nothing for JLC to place" — the one actionable failure the
    list can show. Mounting holes and test points are excluded from the BOM and
    are deliberately *not* marked.
    """
    from lcsc_suite.ui.models.part_table import REFERENCE_ROLE

    window = _main_window(context)
    table = window.part_table
    model = table.model()
    wanted = {row.reference for row in window.part_model.rows() if row.needs_a_number}
    for row in range(model.rowCount()):
        if model.data(model.index(row, 0), REFERENCE_ROLE) in wanted:
            table.scrollTo(model.index(row, 0), table.ScrollHint.PositionAtTop)
            break
    return window


#: name -> builder. Grows one entry per phase; ``--all`` renders every one, so
#: adding a screen here is what puts it under CI.
SCREENS = {
    "mainwindow": screen_mainwindow,
    "mainwindow-unassigned": screen_mainwindow_unassigned,
}


class Context:
    """What a screen builder is handed."""

    def __init__(self, board, settings, args) -> None:
        self.board = board
        self.settings = settings
        self.args = args


def open_board(args):
    """Open the board a probe run should render against.

    The fixture is pointed at a fresh temporary project directory: store.py
    really does create ``<project>/jlcpcb/project.db``, and a probe run must not
    write into the checkout or carry state between runs.
    """
    if args.live:
        return kicad_bridge.connect()
    board = kicad_bridge.open_fixture(args.fixture)
    board.relocate(tempfile.mkdtemp(prefix="lcsc-probe-project-"))
    return board


def render(name: str, context: Context, output_dir: str, mode: str) -> tuple[bool, str]:
    """Build one screen, grab it to a PNG, optionally dump its geometry."""
    builder = SCREENS[name]
    suffix = "" if mode == "light" else f"-{mode}"
    target = os.path.join(output_dir, f"{name}{suffix}.png")
    try:
        widget = builder(context)
        settle()
        pixmap = widget.grab()
        os.makedirs(output_dir, exist_ok=True)
        if not pixmap.save(target, "PNG"):
            return False, f"{name}: grab() produced nothing to save"
        print(
            f"{name}: {pixmap.width()}x{pixmap.height()} -> "
            f"{os.path.relpath(target, _ROOT)}"
        )
        if context.args.geometry:
            print(f"--- {name} geometry ---")
            dump_tree(widget)
            for view in _descendants(widget):
                if hasattr(view, "horizontalHeader") and hasattr(view, "columnWidth"):
                    dump_table(view, view.objectName() or type(view).__name__)
        widget.close()
        widget.deleteLater()
        settle(50)
    except Exception:  # noqa: BLE001 - a raising screen is the thing we report
        traceback.print_exc()
        return False, f"{name}: raised while building"
    return True, ""


def main(argv=None) -> int:
    """Render the requested screens."""
    parser = argparse.ArgumentParser(
        prog="qt_probe.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "screens",
        nargs="*",
        metavar="SCREEN",
        help=f"screens to render: {', '.join(sorted(SCREENS))}",
    )
    parser.add_argument("--all", action="store_true", help="render every screen")
    parser.add_argument(
        "--theme",
        choices=("light", "dark", "both"),
        default="light",
        help="appearance to render (default light; 'both' writes -dark files too)",
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUTPUT_DIR, help="output directory for the PNGs"
    )
    parser.add_argument(
        "--fixture", default=DEFAULT_FIXTURE, help="fixture board to render against"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="connect to a running KiCad instead of using the fixture",
    )
    parser.add_argument(
        "--geometry", action="store_true", help="also dump the widget tree and columns"
    )
    parser.add_argument("--list", action="store_true", help="list the screens and exit")
    args = parser.parse_args(argv)
    # INFO, so the log pane shows what a real session shows. store.py logs a
    # DEBUG line per part, and derive_params calls basicConfig(DEBUG) at import,
    # which would otherwise fill the pane with reconciliation chatter.
    app_module.configure_logging(logging.INFO)

    if args.list:
        for name in sorted(SCREENS):
            print(name)
        return 0

    names = sorted(SCREENS) if args.all or not args.screens else args.screens
    unknown = [name for name in names if name not in SCREENS]
    if unknown:
        parser.error(
            f"unknown screen(s): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(SCREENS))}"
        )

    modes = ("light", "dark") if args.theme == "both" else (args.theme,)
    failures = []
    for mode in modes:
        application = app_module.build_application(theme_mode=mode, offscreen=True)
        board = open_board(args)
        for name in names:
            # Fresh settings per screen: the main window saves its geometry on
            # close, and the next screen would restore it — which offscreen means
            # being clamped to the 800x800 virtual screen and rendering at the
            # wrong size for a reason that has nothing to do with the screen
            # under review.
            context = Context(board, probe_settings(), args)
            ok, problem = render(name, context, args.out, mode)
            if not ok:
                failures.append(problem)
        application.processEvents()

    if failures:
        print("\nFAILED:", file=sys.stderr)
        for problem in failures:
            print(f"  {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
