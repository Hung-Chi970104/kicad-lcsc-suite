"""Tests for the main window shell and the single-instance guard.

These are not a substitute for looking at ``docs/screens/mainwindow.png`` —
nothing here can tell you whether a label is elided or a pane collapsed, and
believing otherwise is the mistake that prompted this migration. What they *can*
do is fix the things a screenshot review would not notice going wrong:

* every control §5.1 lists exists, in the order it lists them, and the ones §1
  deletes are gone;
* the enable/disable rule distinguishes per-part actions from modes;
* the single-instance guard is scoped per board, so two KiCad windows with
  different boards open get a window each.

Rendered offscreen, so they need no display.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QToolButton  # noqa: E402

from lcsc_suite import (
    app as app_module,  # noqa: E402
    kicad_bridge,  # noqa: E402
    single_instance,  # noqa: E402
)
from lcsc_suite.config import Settings  # noqa: E402
from lcsc_suite.single_instance import SingleInstance, _socket_name  # noqa: E402
from lcsc_suite.ui import icons  # noqa: E402
from lcsc_suite.ui.main_window import COLUMNS, DEFAULT_SIZE, MainWindow  # noqa: E402

FIXTURE = (
    Path(__file__).resolve().parent.parent / "lcsc_suite" / "fixtures" / "board.json"
)


@pytest.fixture(scope="session")
def application():
    """Build the one QApplication the tests share."""
    return app_module.build_application(theme_mode="light", offscreen=True)


@pytest.fixture
def board():
    """Return the fixture board."""
    with open(FIXTURE, encoding="utf-8") as handle:
        return kicad_bridge.FixtureBoard.from_dict(copy.deepcopy(json.load(handle)))


@pytest.fixture
def window(application, board, tmp_path):
    """Build a main window against the fixture board and throwaway settings."""
    settings = Settings(path=str(tmp_path / "settings.json"))
    result = MainWindow(board, settings=settings)
    yield result
    result.log_pane.uninstall()
    result.close()
    result.deleteLater()


def _labels(toolbar) -> list[str]:
    """Return the button labels of a toolbar as displayed, in order.

    ``&&`` is collapsed to ``&`` because that is what Qt paints: a single ``&``
    in an action's text is a mnemonic marker, so "Toggle BOM & POS" has to be
    written "Toggle BOM && POS" and reads correctly on screen.
    """
    return [
        button.text().replace("&&", "&")
        for button in toolbar.findChildren(QToolButton)
        if button.objectName() != "qt_toolbar_ext_button" and button.text()
    ]


# ---------------------------------------------------------------------------
# The controls §5.1 asks for
# ---------------------------------------------------------------------------


def test_window_identity(window):
    """Titled for the app and the board, at the reference size."""
    assert window.windowTitle() == "LCSC Suite — tempctrl.kicad_pcb"
    assert (window.width(), window.height()) == (1300, 772)


def test_top_toolbar_has_the_buttons_in_order(window):
    """§5.1's upper toolbar, left group then right group.

    ``Undo`` is ours, not §5.1's: the wx plugin had no undo of its own and left
    the board's history to KiCad, which cannot reach the project database. See
    lcsc_suite.undo.
    """
    assert _labels(window.main_toolbar) == [
        "Undo",
        "Export BOM / CPL",
        "From schematic",
        "To schematic",
        "Corrections",
        "Mappings",
        "LCSC Explorer",
        "Import libs",
        "Offline DB",
        "Settings",
    ]


def test_gerber_controls_are_gone(window):
    """``Generate`` and its ``Auto`` layer dropdown are deleted, not hidden.

    Gerber and drill output is out of scope (§1). A disabled Generate button
    would read as "broken" rather than "not this plugin's job".
    """
    labels = _labels(window.main_toolbar)
    assert "Generate" not in labels
    assert not any("Layer" in label for label in labels)
    assert window.findChild(QToolButton, "layer-selection") is None


def test_schematic_sync_stays_two_explicit_buttons(window):
    """Board↔schematic is never automatic, in either direction.

    The two sides are separate stores of the same fact, and the plugin does not
    get to decide which one wins — so there is a button per direction and no
    "sync" that does both.
    """
    labels = _labels(window.main_toolbar)
    assert "From schematic" in labels
    assert "To schematic" in labels
    assert not any(label.lower() == "sync schematic" for label in labels)


def test_part_toolbar_has_the_buttons_in_order(window):
    """§5.1's right-hand vertical toolbar, top to bottom."""
    assert _labels(window.part_toolbar) == [
        "Assign LCSC number",
        "Remove LCSC number",
        "Auto-select alike",
        # The running plugin's label; §5.1 renders it without the ampersand.
        "Toggle BOM & POS",
        "Toggle BOM",
        "Toggle POS",
        "Part details",
        "Hide excluded BOM",
        "Hide excluded POS",
        "Save mappings",
    ]


def test_every_part_button_fits_without_an_extension_arrow(window):
    """All ten must be reachable at the default window size.

    Qt hides overflow behind an extension arrow, which is exactly the "scrolled
    out of sight on a default-sized window" problem §5.1 records about the wx
    original. Losing ``Save mappings`` that way is a regression, not a detail.
    """
    window.show()
    window.resize(1300, 772)
    buttons = [
        button
        for button in window.part_toolbar.findChildren(QToolButton)
        if button.objectName() != "qt_toolbar_ext_button"
    ]
    assert len(buttons) == 10
    hidden = [button.text() for button in buttons if not button.isVisible()]
    assert hidden == []


def test_every_toolbar_button_has_its_icon(window):
    """No button may render as a bare label.

    The icon set lives in the legacy package's directory, so a path that stops
    resolving is a real possibility whenever the layout moves. ``icons.icon()``
    returns an empty ``QIcon`` for anything it cannot load rather than raising —
    right for a single typo, but it means a wrong *directory* silently strips
    every icon in the window, and the resulting screenshot reads as a restyling
    rather than as a bug. It happened exactly once; this is why not twice.
    """
    assert os.path.isdir(icons.ICON_DIR), f"icon directory missing: {icons.ICON_DIR}"
    bare = [
        action.text()
        for bar in (window.main_toolbar, window.part_toolbar)
        for action in bar.actions()
        if action.text() and action.icon().isNull()
    ]
    assert bare == []


def test_estimator_row(window):
    """`Boards:` spins in fives from five; Force Standard and Help are present."""
    assert window.boards_input.value() == 5
    assert window.boards_input.minimum() == 5
    # JLC quotes assembly in multiples of five and the estimator's ladders are
    # keyed off that, so the step matches rather than being 1.
    assert window.boards_input.singleStep() == 5
    assert window.force_standard.text() == "Force Standard"
    assert window.estimator_help.text() == "Help"


def test_status_line_names_the_board_count(window):
    """§5.1's status line, verbatim."""
    assert (
        window.summary_label.text() == "BOM Estimate (5 boards): no assigned BOM parts"
    )


def test_table_columns(window):
    """Nine columns, in §5.1's order."""
    model = window.part_table.model()
    assert model.columnCount() == len(COLUMNS)
    headers = [
        model.headerData(index, Qt.Orientation.Horizontal)
        for index in range(model.columnCount())
    ]
    assert headers == [
        "Ref",
        "Value (Name)",
        "Footprint",
        "LCSC Params",
        "LCSC",
        "Type",
        # Named for the warehouse it reports on: JLC *assembly*, never LCSC
        # retail. A bare "Stock" left that to be guessed at.
        "JLC Stock",
        "BOM",
        "POS",
    ]


def test_table_allows_multiple_selection(window):
    """Assign-to-many is the main loop; single selection would break it."""
    assert (
        window.part_table.selectionMode()
        == window.part_table.SelectionMode.ExtendedSelection
    )
    assert (
        window.part_table.selectionBehavior()
        == window.part_table.SelectionBehavior.SelectRows
    )


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def test_per_part_buttons_start_disabled(window):
    """Nothing is selected yet, so the per-part actions are unavailable."""
    assert not window.assign_action.isEnabled()
    assert not window.remove_action.isEnabled()
    assert not window.part_details_action.isEnabled()


def test_modes_stay_available_with_no_selection(window):
    """The toggles that are modes rather than actions are never disabled.

    ``Auto-select alike`` and the two ``Hide excluded`` filters change how the
    list behaves, not what happens to a part, so greying them out with an empty
    selection would be wrong.
    """
    window.set_part_buttons_enabled(False)
    assert window.select_alike_action.isEnabled()
    assert window.hide_bom_action.isEnabled()
    assert window.hide_pos_action.isEnabled()


def test_selecting_enables_the_per_part_buttons(window):
    """And selecting something turns them back on."""
    window.set_part_buttons_enabled(True)
    assert window.assign_action.isEnabled()
    assert window.toggle_bom_action.isEnabled()


def test_board_count_is_persisted_and_republished(window):
    """A new board count reaches both the settings file and any listener."""
    seen = []
    window.board_count_changed.connect(seen.append)
    window.boards_input.setValue(25)

    assert seen == [25]
    assert window.settings.get("general", "bom_estimator_boards") == 25


def test_force_standard_is_persisted(window):
    """The Force Standard toggle survives a restart."""
    window.force_standard.setChecked(True)
    assert window.settings.get("general", "bom_estimator_force_standard") is True


def test_geometry_round_trips(application, board, tmp_path):
    """The window comes back the size it was closed at.

    Height only. ``restoreGeometry`` clamps to the screen, and the offscreen
    platform's virtual screen is 800x800 — so a restored *width* offscreen tells
    you about Qt's clamping, not about our persistence. ``resize`` does not
    clamp, which is why the screenshots still come out at 1300x772.
    """
    settings = Settings(path=str(tmp_path / "settings.json"))
    first = MainWindow(board, settings=settings)
    # Shown before resizing: saveGeometry records normalGeometry, which a window
    # that was never mapped does not have.
    first.show()
    first.resize(1111, 640)
    first.close()

    stored = settings.get("window", "main_geometry")
    assert stored

    second = MainWindow(board, settings=settings)
    assert second.height() == 640
    assert second.height() != DEFAULT_SIZE[1]
    second.log_pane.uninstall()
    second.close()


def test_unreadable_geometry_falls_back_to_the_default(application, board, tmp_path):
    """Garbage in the settings file must not stop the window opening."""
    settings = Settings(path=str(tmp_path / "settings.json"))
    settings.set("window", "main_geometry", "not base64 at all !!")

    result = MainWindow(board, settings=settings)
    assert (result.width(), result.height()) == (1300, 772)
    result.log_pane.uninstall()
    result.close()


def test_the_log_pane_shows_what_was_logged(window):
    """The log pane is the only place a background failure can be seen."""
    logging.getLogger("lcsc_suite.test").warning("something worth reading")
    # The handler hands the line to the UI thread through a queued signal — the
    # marshalling that keeps a worker thread from touching the widget — so the
    # event loop has to run before the text is there.
    QApplication.processEvents()
    assert "something worth reading" in window.log_pane.toPlainText()


def test_progress_bar_is_hidden_until_there_is_progress(window):
    """An always-visible empty bar reads as a stuck operation."""
    assert not window.progress.isVisible()
    window.set_progress(40)
    assert window.progress.value() == 40
    window.set_progress(None)
    assert not window.progress.isVisible()


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------


def test_the_lock_is_scoped_per_board():
    """Two boards are two pieces of work and get a window each."""
    assert _socket_name("/a/one.kicad_pcb") != _socket_name("/b/two.kicad_pcb")


def test_the_same_board_by_a_different_path_is_the_same_lock():
    """A relative path, a trailing slash or a different case is one board.

    Paths compare via normcase/abspath elsewhere in this project for the same
    reason; a second window opened because the path was spelled differently
    would still share the project database.
    """
    assert _socket_name("/a/../a/one.kicad_pcb") == _socket_name("/a/one.kicad_pcb")


def test_a_second_launch_is_turned_away_and_raises_the_first(application, tmp_path):
    """The guard both locks and carries the "come forward" request."""
    path = str(tmp_path / "board.kicad_pcb")
    first = SingleInstance(path)
    assert first.acquire() is True

    raised = []
    first.raise_requested.connect(lambda: raised.append(True))

    second = SingleInstance(path)
    assert second.acquire() is False

    # The request arrives over a local socket, so give the event loop a turn.
    loop = QEventLoop()
    QTimer.singleShot(300, loop.quit)
    loop.exec()

    assert raised == [True]
    first.release()


def test_a_stale_socket_does_not_lock_the_app_out_for_ever(
    application, tmp_path, monkeypatch
):
    """A process killed with SIGKILL leaves its socket file behind.

    Refusing to start because of it would be unrecoverable without knowing to
    delete a file in a temp directory, so a bound name that nobody answers on is
    reclaimed. "Nobody answered" is the whole condition — reclaiming a name a
    *live* instance holds would defeat the guard, which is why the reclaim
    happens only after the connect attempt has failed.
    """
    path = str(tmp_path / "board.kicad_pcb")
    abandoned = SingleInstance(path)
    assert abandoned.acquire() is True

    # A dead process cannot be simulated in-process: the abandoned QLocalServer
    # is still listening. So stand in for it — nobody answers, but the name is
    # still bound, which is exactly the state a SIGKILL leaves behind.
    monkeypatch.setattr(single_instance, "notify_existing", lambda name: False)

    replacement = SingleInstance(path)
    assert replacement.acquire() is True
    replacement.release()
    abandoned.release()
