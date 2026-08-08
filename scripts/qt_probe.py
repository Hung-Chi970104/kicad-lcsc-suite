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
import contextlib
import logging
import os
import shutil
import sys
import tempfile
import time
import traceback
from typing import Optional

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

#: The name of the throwaway project directory every screen renders against.
#: It matches the fixture board's own file so the one screen that shows a path
#: reads like a real project rather than like a temporary directory. Fixed
#: rather than generated — see ``open_board``.
PROJECT_NAME = "tempctrl"

#: Where those project directories go. Fixed, and for the same reason
#: ``PROJECT_NAME`` is: the Explorer's "Library folder" field shows
#: ``<project>/lcsc-lib``, so a ``mkdtemp`` suffix anywhere in the project path
#: is a ``mkdtemp`` suffix on screen. ``PROJECT_NAME`` closed that hole one
#: level down for ``export-summary`` and left it open one level up, where 12 of
#: the Explorer's PNGs were reading it.
#:
#: Deterministic paths give up ``mkdtemp``'s collision safety, so ``open_board``
#: wipes each screen's directory before use rather than trusting it to be
#: absent. Two probe runs at once on one machine would still fight; that is a
#: trade for screenshots whose bytes depend only on the UI.
PROBE_PROJECT_ROOT = os.path.join(tempfile.gettempdir(), "lcsc-probe")

#: What every log line in every screenshot is stamped with.
#:
#: A screenshot's clock carries no information about the app, and it changes on
#: every run: re-rendering with no code change at all rewrote all 8 main-window
#: PNGs on the timestamp alone. The rule this whole harness exists to serve is
#: "read the PNG diff", and a diff of moving clock hands is one nobody reads.
#:
#: Chosen to be obviously not-now, so nobody reads a screenshot's timestamp as
#: evidence of when anything happened.
FIXED_LOG_TIME = time.struct_time((2026, 1, 1, 12, 0, 0, 2, 1, 0))

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


def freeze_log_clock() -> None:
    """Stamp every log line this process formats with :data:`FIXED_LOG_TIME`.

    ``Formatter.converter`` is the documented seam for this and it is a *class*
    attribute, so one assignment covers the log pane's own formatter — which the
    pane builds itself, out of reach of the probe — as well as the stderr one.

    ``staticmethod`` is not decoration. The stdlib's default is ``time.localtime``,
    a builtin, and builtins are not descriptors; a plain Python function assigned
    to the same attribute *is* one, and would be handed ``self`` as its seconds
    argument the first time a record was formatted.
    """
    logging.Formatter.converter = staticmethod(lambda _seconds=None: FIXED_LOG_TIME)


def freeze_cursor_blink(application: QApplication) -> None:
    """Stop the text caret blinking, so a grab cannot catch it mid-blink.

    The Explorer's search field holds focus in three screens, and a blinking
    caret is one column of pixels that is present or absent depending on the
    millisecond the grab happened — 15 pixels, and enough to rewrite the file.

    Zero means "do not flash" to Qt, which leaves the caret drawn. Drawn is the
    right answer: the field really does have focus, and a screenshot that hid
    that would be showing something the app never shows.
    """
    application.setCursorFlashTime(0)


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


_FIXTURE_SOURCE = None


def fixture_source():
    """Return the captured search source, built once per process.

    Every screen gets one, not just the Explorer's. Since Phase 5 the BOM
    estimator looks up assembly metadata through the source as well, so a
    controller built without one would either skip that lookup or — worse, if
    it fell back to the lazy default — make live requests from a screenshot
    run. Cached because the constructor reads a megabyte of payloads and
    ``install()`` is idempotent.
    """
    global _FIXTURE_SOURCE  # noqa: PLW0603 - one capture per process, by design
    if _FIXTURE_SOURCE is None:
        from lcsc_suite.search_source import FixtureSource

        _FIXTURE_SOURCE = FixtureSource()
    return _FIXTURE_SOURCE


def _controller(context, source=None):
    """Build the controller, its part list and its window — the whole app.

    Through the controller rather than by constructing a ``MainWindow``
    directly, because a screenshot of a window whose buttons are inert is not
    evidence about the app the user runs. It is also what lets a screen *do*
    something before it is grabbed, which is how the assignment screens below
    show a real write rather than a mock-up.
    """
    from lcsc_suite.controller import build
    from lcsc_suite.parts import PartList, open_fixture_library

    source = source or fixture_source()
    parts = PartList(context.board, settings=context.settings)
    # A throwaway data directory seeded from fixtures/part_details.json, never
    # the developer's real part cache. Type / LCSC Params / JLC Stock are only
    # evidence if they say the same thing on every machine — and the fixture
    # deliberately leaves seven of its 29 numbers uncached so the "?" that means
    # "nobody answered" appears in the same screenshot as real figures.
    parts.library = open_fixture_library(
        parts.owner, tempfile.mkdtemp(prefix="lcsc-probe-library-")
    )
    controller = build(context.board, parts, settings=context.settings, source=source)
    controller.window.show()
    return controller


def _main_window(context):
    """Build the main window against the probe's board and settings."""
    return _controller(context).window


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
    window = _main_window(context)
    _scroll_to(
        window,
        [row.reference for row in window.part_model.rows() if row.needs_a_number],
    )
    return window


def _unassigned_references(window, limit: int = 4) -> list:
    """Return the first few references that are in the BOM with no number."""
    return [row.reference for row in window.part_model.rows() if row.needs_a_number][
        :limit
    ]


def _scroll_to(window, references) -> None:
    """Scroll the table so the first of ``references`` is at the top."""
    table = window.part_table
    model = table.model()
    from lcsc_suite.ui.models.part_table import REFERENCE_ROLE

    wanted = set(references)
    for row in range(model.rowCount()):
        if model.data(model.index(row, 0), REFERENCE_ROLE) in wanted:
            table.scrollTo(model.index(row, 0), table.ScrollHint.PositionAtTop)
            return


def screen_assign_dialog(context) -> QWidget:
    """Build the LCSC number entry dialog over a real selection (Phase 3).

    The references are the fixture's own unassigned parts, not invented ones, so
    the dialog is shown doing the job it exists for: naming a number for the
    rows the list is marking red.
    """
    from lcsc_suite.ui.assign_dialog import AssignNumberDialog

    controller = _controller(context)
    window = controller.window
    references = _unassigned_references(window)
    window.select_references(references)
    dialog = AssignNumberDialog(window, references)
    # A pasted product URL rather than a bare number: it exercises the hint line
    # that says which number was extracted, which is the part of this dialog
    # worth being able to look at.
    dialog.input.setText("https://www.lcsc.com/product-detail/C1524.html")
    dialog.show()
    return dialog


def screen_mainwindow_assigned(context) -> QWidget:
    """Build the main window just after an assignment landed (Phase 3).

    The counterpart to ``mainwindow-unassigned``: the same rows, assigned. Red
    and bold has gone, the LCSC column carries the number, and Type / JLC Stock
    fill from the seeded cache — which together are the evidence that the write
    reached the board *and* the project database and that the list re-resolved
    afterwards.
    """
    controller = _controller(context)
    window = controller.window
    references = _unassigned_references(window)
    # C1525 because it is one of the numbers `fixtures/part_details.json` seeds
    # the cache with, so Type / JLC Stock / LCSC Params fill in and the row is
    # complete. It is a 100nF 0402, which is not what any of these four parts
    # actually is — the fixture's only unassigned-and-in-the-BOM references are
    # three logos and a jumper. That mismatch is visible and correct: nothing
    # lights up in LCSC Params, because match highlighting marks where the
    # derived parameters agree with the board, and here they do not.
    controller.assign_number(references, "C1525")
    window.select_references(references)
    _scroll_to(window, references)
    return window


# ---------------------------------------------------------------------------
# Phase 4 — the LCSC Explorer
# ---------------------------------------------------------------------------
#
# Every one of these runs against ``lcsc_suite/fixtures/explorer/``, never the
# live endpoints, and that is not a convenience. CI asserts the committed PNGs
# match what renders; a grid built from live search results renders differently
# every run, because stock figures are the most volatile numbers on the screen
# and three columns show them. ``FixtureSource`` also makes reaching the network
# structurally impossible — see its module docstring.


def _explorer(context, references=None, keyword: str = ""):
    """Build a controller and its Explorer over the captured result set.

    Through the controller, like every other screen here, because the Explorer's
    assign path runs through ``SuiteController.assign_number`` and a screenshot
    of buttons wired to nothing is not evidence about the app.
    """
    source = fixture_source()
    controller = _controller(context, source=source)
    window = controller.window
    if references is None:
        references = _unassigned_references(window, limit=3)
    window.select_references(references)
    explorer = controller.build_explorer(references, keyword=keyword or source.keyword)
    explorer.show()
    explorer.start_search()
    # The search, the facet rebuild, the thumbnail fill and the retail fill all
    # land through the event loop. One settle here means the later ones in
    # ``render`` are spent on layout rather than on waiting for data.
    settle(900)
    return explorer


def screen_explorer(context) -> QWidget:
    """Build the Explorer as it opens: results, facets, no selection (§5.2)."""
    return _explorer(context)


def screen_explorer_detail(context) -> QWidget:
    """Build the Explorer with a part selected, details in the side panel.

    Row 0 of the captured set deliberately: it is one of the three the capture
    took a full-size photo and an EasyEDA CAD record for, so the symbol, the
    footprint and the photo tiles are all filled rather than showing their
    "no drawing for this part" placeholders. Those placeholders are real states
    and worth keeping, but a screenshot of three of them says nothing about
    whether the previews work.
    """
    explorer = _explorer(context)
    explorer.select_row(0)
    settle(900)
    return explorer


def screen_explorer_inline(context) -> QWidget:
    """Build the same details as a full-width expanded row under the part.

    A separate screen because "Side panel" and "Inline below" are different
    layouts of the same widgets, and the inline one is the arrangement the wx
    version needed an overlay window and a 100ms tracking timer to fake. Here it
    is a spanned row carrying the pane as an index widget, so what this picture
    shows is a genuinely different mechanism, not just a different position.
    """
    explorer = _explorer(context)
    explorer.detail_layout_choice.setCurrentIndex(1)
    explorer.select_row(1)
    settle(900)
    # Framed on the *selected* row, not on the pane. Scrolling to the pane
    # centres it and pushes the row it belongs to off the top, which loses the
    # one thing this screen is meant to show: that the details are attached to
    # a particular result and sit inside the list rather than beside it.
    rows = explorer.results.selectionModel().selectedRows()
    if rows:
        explorer.results.scrollTo(rows[0], explorer.results.ScrollHint.PositionAtTop)
        settle(200)
    return explorer


def screen_explorer_reopened(context) -> QWidget:
    """Build the Explorer after the sequence that used to destroy the pane.

    Not a layout — a *history*. Every earlier explorer screen builds its pane
    once and photographs it, and the four bugs the user reported all needed the
    pane to be placed a second time: switch inventory with a row expanded, or
    switch layout, or simply click another part, and the grid deleted the widget
    it had been lent. The window then held a destroyed object, so nothing
    reopened, and in the inline layout the view painted through the dangling
    pointer and took the process with it.

    So this screen walks that path — inline, expand, switch inventory, expand a
    different part — and the render is the assertion: a pane that did not
    survive cannot be photographed.
    """
    explorer = _explorer(context)
    explorer.detail_layout_choice.setCurrentIndex(1)
    explorer.select_row(1)
    settle(700)
    explorer.inventory.setCurrentIndex(1)  # JLC assembly → LCSC retail
    settle(700)
    explorer.select_row(3)
    settle(900)
    rows = explorer.results.selectionModel().selectedRows()
    if rows:
        explorer.results.scrollTo(rows[0], explorer.results.ScrollHint.PositionAtTop)
        settle(200)
    return explorer


def screen_explorer_retail(context) -> QWidget:
    """Build the Explorer on the LCSC retail inventory, sorted on it.

    The counterpart to ``explorer``: same result set, other warehouse. Worth its
    own screen because the two figures disagree on 96 of the fixture's 100 rows,
    so this is where the Inventory selector is visibly doing something rather
    than relabelling a column. Sorted retail-high-first, which is the ordering
    that cannot be produced from the search response alone — every figure in it
    came from a per-part lookup.
    """
    explorer = _explorer(context)
    explorer.inventory.setCurrentIndex(1)
    explorer.sort_mode.setCurrentIndex(2)
    settle(900)
    # The fill lands row by row; re-sorting once it has settled is what the
    # completion handler does in a live session.
    explorer.apply_filters()
    settle(600)
    return explorer


def screen_explorer_facets(context) -> QWidget:
    """Build the Explorer with a parametric filter applied.

    Two tolerance values ticked and one voltage rating: the result count in the
    status line, and the fact that it is lower than 100, is the evidence that
    the semantics are the ones the catalogue needs. Ticking two values inside
    one attribute must *widen* the result, and adding a second attribute must
    narrow it.
    """
    explorer = _explorer(context)
    controls = explorer.facets.controls()
    tolerance = controls.get("Tolerance")
    if tolerance is not None:
        values = [value for value, _count in tolerance._values][:2]
        tolerance.set_selected(values)
        explorer.facets._on_changed("Tolerance", set(values))
    voltage = controls.get("Voltage Rating")
    if voltage is not None:
        chosen = [value for value, _count in voltage._values][:1]
        voltage.set_selected(chosen)
        explorer.facets._on_changed("Voltage Rating", set(chosen))
    explorer.apply_filters()
    settle(700)
    return explorer


def screen_photo_viewer(context) -> QWidget:
    """Build the photo viewer on a product photo at full size (§5.7)."""
    explorer = _explorer(context)
    hits = explorer.model.hits()
    explorer.open_photo_viewer(hits[0])
    settle(700)
    return explorer._photo_viewer


# ---------------------------------------------------------------------------
# Phase 5 — the remaining dialogs
# ---------------------------------------------------------------------------


def screen_settings(context) -> QWidget:
    """Build the Settings dialog (§5.3).

    Rendered at the shipped defaults, like every other screen, which means the
    inverted labels are all showing their *ticked* wording except
    ``lcsc_priority`` — the one setting that ships off. That contrast is worth
    having in the picture: it is the whole argument for the paired labels, and
    a screen where every row read the same way would not show it.
    """
    from lcsc_suite.ui.settings_dialog import SettingsDialog

    controller = _controller(context)
    dialog = SettingsDialog(controller.window, settings=context.settings)
    dialog.changed.connect(controller.apply_setting)
    dialog.show()
    return dialog


def screen_mappings(context) -> QWidget:
    """Build the Mappings Manager over mappings this board actually produced.

    Seeded by pressing `Save mappings`, not by writing rows into the table: the
    93 assigned parts on the fixture board collapse to the distinct
    footprint+value pairs among them, which is exactly what a user gets from
    that button and is a truer picture of the window than invented entries.
    """
    from lcsc_suite.ui.mappings_dialog import MappingsDialog

    controller = _controller(context)
    controller.save_mappings()
    dialog = MappingsDialog(controller.window, library=controller.parts.library)
    dialog.table.selectRow(1)
    dialog.show()
    return dialog


#: A few corrections to render the manager with. Deliberately *not* a copy of
#: the community rotation table: `Update` is what fetches that, it cannot run in
#: a probe, and shipping a half-remembered version of it would be a file people
#: could mistake for authoritative. These are patterns built from the fixture
#: board's own footprints with illustrative values — enough to show a populated
#: table, and obviously local to this board.
SAMPLE_CORRECTIONS = (
    ("^SOT-23", 180, 0.0, 0.0),
    ("^SOT-223", 180, 0.0, 0.0),
    ("^R_0402_1005Metric", 0, 0.0, 0.0),
    ("^C_0603_1608Metric", 0, 0.0, 0.0),
    ("^TerminalBlock", 90, 0.0, -1.25),
    ("^LED_", 270, 0.15, 0.0),
)


def screen_corrections(context) -> QWidget:
    """Build the Corrections Manager with a rule selected for editing (§5.4).

    A row is selected on purpose: selecting one loads it into the Add / Edit box
    above, which is the interaction the whole layout exists for and is invisible
    in a screenshot of an idle table. `Update` is disabled, because
    ``allow_network=False`` — the same switch the probe hands every other
    outward-facing control.
    """
    controller = _controller(context)
    library = controller.parts.library
    if library is not None:
        library.create_correction_table()
        for pattern, rotation, offset_x, offset_y in SAMPLE_CORRECTIONS:
            library.insert_correction_data(pattern, rotation, (offset_x, offset_y))
    dialog = controller.build_corrections_dialog(allow_network=False)
    dialog.table.selectRow(4)
    dialog.show()
    return dialog


def screen_part_details(context) -> QWidget:
    """Build the Part Details window for a part in the capture (§5.6).

    The number comes from the captured search rather than from the fixture
    board: the board's own numbers have cached *summaries* (enough for the part
    table's three columns) but no assembly record, and this window is built
    almost entirely out of the assembly record. Row 0 of the capture is a part
    every endpoint answered for, so the price ladders and the photo are all
    filled.
    """
    from lcsc_suite.ui.part_details_dialog import PartDetailsDialog

    source = fixture_source()
    controller = _controller(context, source=source)
    hits = source.hits()[1]
    dialog = PartDetailsDialog(
        controller.window,
        source=source,
        lcsc=hits[0].lcsc,
        references=["C12", "C13", "C14"],
        project_path=context.board.info().project_path,
    )
    dialog.show()
    settle(700)
    return dialog


def screen_mainwindow_estimate(context) -> QWidget:
    """Build the main window with a real BOM estimate and an amber trigger.

    The screen that closes two items the plan has carried open since Phase 2:
    the summary line said "no assigned BOM parts" on every board because nothing
    computed it, and ``set_standard_trigger_refs`` was called by nobody, so the
    amber Standard-mode advisory could not be seen at all.

    ``component_product_type`` is seeded directly for two references rather than
    fetched. It is the one estimator input that lives on neither the board nor
    the part cache — it comes from JLC's assembly record, one request per number
    — and the explorer capture holds records for its own search results, not for
    this board's parts. Writing it here is exactly what
    ``BomEstimator.enrich()`` writes when it does run, so the colour and the
    summary line are produced by the real code path from the real database; only
    the provenance of that one flag is short-circuited.
    """
    controller = _controller(context)
    window = controller.window
    store = controller.parts.store
    # Let the enrichment pass the controller started on construction finish
    # first. It asks the capture about this board's numbers, the capture holds
    # a different search, and a lookup that lands after the seed below would
    # have nothing to say about these two parts — which is a race, not a screen.
    settle(400)
    triggers = [
        row.reference
        for row in window.part_model.rows()
        if row.assigned and not row.exclude_from_bom
    ][:2]
    for reference in triggers:
        store.set_assembly_metadata(reference, "SMT", 1)
    # recompute, not reload_parts: a reload starts another enrichment pass, and
    # the point here is to render what the estimator does with the metadata, not
    # to race it again.
    controller.estimator.recompute()
    _scroll_to(window, triggers)
    return window


def screen_export_summary(context) -> QWidget:
    """Build the report shown after a real BOM/CPL export (Phase 6).

    The export has no window of its own — it writes two files — so this dialog
    is the whole of its user interface, and the part of it worth looking at is
    the last line. "Left out: 8 marked do-not-place" is the answer to the first
    question anyone asks of a BOM, and the wx plugin answers it only in a log
    pane that has already scrolled.

    The files are really written, into the throwaway project directory
    ``open_board`` made, by the same ``Exporter`` the button uses. Nothing here
    is mocked; the counts on screen are counts of rows on disk.
    """
    controller = _controller(context)
    result = controller.run_export()
    box = controller.build_export_report(result)
    box.show()
    settle(60)
    return box


#: name -> builder. Grows one entry per phase; ``--all`` renders every one, so
#: adding a screen here is what puts it under CI.
# ---------------------------------------------------------------------------
# Board <-> schematic (Phase 7)
#
# These two are the only screens that need a *second* fixture file: a schematic
# whose LCSC fields disagree with the board's in every way the warning has to
# report. It is written next to the relocated fixture project rather than
# committed, because what makes the picture worth looking at is that the two
# sides disagree — and the disagreement has to be built from whatever the board
# fixture currently says, not frozen against a copy of it that can drift.
#
# The symbol template is a deliberate second copy of the one in
# ``tests/test_schematic_sync.py``. That one is asserted against and is the
# parser's specification; this one exists to make a picture, and a probe script
# importing a test module to draw one would be the wrong dependency.
# ---------------------------------------------------------------------------


def _symbol(reference: str, lcsc=None) -> str:
    """Build one KiCad 8+ symbol instance, optionally carrying an LCSC field."""
    field = ""
    if lcsc is not None:
        field = f'\t\t(property "LCSC" "{lcsc}"\n\t\t\t(at 10.16 20.32 0)\n\t\t)\n'
    return (
        "\t(symbol\n"
        '\t\t(lib_id "Device:C")\n'
        f'\t\t(property "Reference" "{reference}"\n\t\t\t(at 12.7 17.78 0)\n\t\t)\n'
        f'\t\t(property "Value" "10uF"\n\t\t\t(at 12.7 20.32 0)\n\t\t)\n'
        f"{field}\t)\n"
    )


def _write_schematic(board, symbols) -> list:
    """Write a root sheet into the board's project and return its path.

    Named after the board, which is the first place ``find_root_schematic``
    looks — so the app finds it exactly as it would find a real one, and the
    screens exercise the lookup rather than being handed a path.
    """
    info = board.info()
    path = os.path.join(info.project_path, f"{info.name.split('.')[0]}.kicad_sch")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            '(kicad_sch\n\t(version 20260306)\n\t(generator "eeschema")\n'
            + "".join(symbols)
            + ")\n"
        )
    return [path]


def _diverging_schematic(controller, divergences: dict, omit=(), extra=()) -> list:
    """Write a schematic that agrees with the board except where told to differ.

    **Starting from agreement is the point.** A schematic built only from the
    handful of rows a screen wants to show is a schematic missing 100 symbols,
    and every one of those is a legitimate "no symbol for this reference" the
    dialog then reports — so the first attempt at this screen showed "85
    skipped" and buried the eight rows worth looking at. A real project's
    schematic has a symbol for every footprint; the fixture's must too.

    ``divergences`` maps a reference to the number its *symbol* should carry,
    ``None`` meaning "a symbol with no LCSC field at all". Anything not named
    gets the number the board already has. ``omit`` leaves a reference out of
    the file entirely and ``extra`` adds symbols the board has no footprint for
    — the two ways the sides can fail to line up at all.
    """
    symbols = []
    for row in controller.window.part_model.rows():
        if row.reference in omit:
            continue
        if row.reference in divergences:
            symbols.append(_symbol(row.reference, divergences[row.reference]))
        else:
            symbols.append(_symbol(row.reference, row.lcsc or None))
    symbols += [_symbol(reference, lcsc) for reference, lcsc in extra]
    return _write_schematic(controller.board, symbols)


def screen_schematic_export(context) -> QWidget:
    """Build the `To schematic` warning over a schematic that disagrees.

    Every category the dialog can report is present on purpose, because the
    categories are the whole content: an addition is free, a REPLACED row
    destroys a number somebody chose, a cleared row removes one outright, and a
    skipped reference cannot be written at all. A screenshot showing only
    additions would prove nothing about the case the warning exists for.
    """
    controller = _controller(context)
    rows = controller.window.part_model.rows()
    assigned = [row for row in rows if row.assigned]
    unassigned = [row for row in rows if not row.assigned]

    divergences = {}
    for row in assigned[:3]:
        divergences[row.reference] = None  # no field yet: these gain one
    for row in assigned[3:6]:
        divergences[row.reference] = "C1"  # a different number: destructive
    for row in unassigned[:2]:
        # Blank on the board, numbered in the schematic — and cleared *here*,
        # this session. That is the one state the cleared set exists to carry:
        # without it these two are indistinguishable from a reference the board
        # simply never picked up, and the export would leave them alone.
        divergences[row.reference] = "C2"
        controller.schematic_cleared_refs.add(row.reference)
    # One assigned reference the schematic has no symbol for at all.
    paths = _diverging_schematic(controller, divergences, omit={assigned[6].reference})

    plan = controller.schematic.plan_export(paths, controller.schematic_cleared_refs)
    dialog = controller.build_confirmation(plan)
    dialog.show()
    settle(200)
    return dialog


def screen_schematic_import(context) -> QWidget:
    """Build the `From schematic` warning — the direction usually worth pressing.

    A schematic is routinely ahead of the board: fields filled in eeschema, or a
    design that shipped with them, on a board whose footprints carry nothing.
    The mirror of the other screen's caveat applies — a symbol whose reference
    is not on this board cannot be imported, and saying so is the difference
    between "nothing happened" and "nothing happened, and here is why".
    """
    controller = _controller(context)
    rows = controller.window.part_model.rows()
    assigned = [row for row in rows if row.assigned]
    unassigned = [row for row in rows if not row.assigned]

    divergences = {row.reference: "C15195" for row in unassigned[:4]}
    divergences.update({row.reference: "C1524" for row in assigned[:3]})
    # X99 is a symbol for a part that is not on this board at all — the import
    # cannot invent a footprint for it, so it can only be reported.
    paths = _diverging_schematic(controller, divergences, extra=[("X99", "C60133")])

    dialog = controller.build_confirmation(controller.schematic.plan_import(paths))
    dialog.show()
    settle(200)
    return dialog


SCREENS = {
    "assign-dialog": screen_assign_dialog,
    "corrections": screen_corrections,
    "explorer": screen_explorer,
    "explorer-detail": screen_explorer_detail,
    "explorer-facets": screen_explorer_facets,
    "explorer-inline": screen_explorer_inline,
    "explorer-reopened": screen_explorer_reopened,
    "explorer-retail": screen_explorer_retail,
    "export-summary": screen_export_summary,
    "mainwindow": screen_mainwindow,
    "mainwindow-assigned": screen_mainwindow_assigned,
    "mainwindow-estimate": screen_mainwindow_estimate,
    "mainwindow-unassigned": screen_mainwindow_unassigned,
    "mappings": screen_mappings,
    "part-details": screen_part_details,
    "photo-viewer": screen_photo_viewer,
    "schematic-export": screen_schematic_export,
    "schematic-import": screen_schematic_import,
    "settings": screen_settings,
}


class Context:
    """What a screen builder is handed."""

    def __init__(self, board, settings, args) -> None:
        self.board = board
        self.settings = settings
        self.args = args


def open_board(args, screen: str = PROJECT_NAME):
    """Open the board *screen* should render against.

    Called **once per screen**, not once per run. The fixture board is mutable
    and screens now write to it: ``mainwindow-assigned`` assigns a number, and
    sharing one board would leave ``mainwindow-unassigned`` — rendered after it,
    alphabetically — with nothing unassigned left to show. A screenshot that
    depends on which screens ran before it is not evidence about anything.

    Each gets its own project directory for the same reason: store.py really does
    create ``<project>/jlcpcb/project.db``, and a probe run must not write into
    the checkout or carry state between runs. Hence the wipe — the path is fixed
    now, so being fresh is this function's job rather than ``mkdtemp``'s.

    **Every component of that path is fixed**, because two screens read parts of
    it onto the screen and a path is text. ``export-summary`` shows the project
    directory: ``mkdtemp``'s suffix is a fixed number of *characters* in a
    proportional font, so the same screen rendered at 354px, 346px and 353px on
    three consecutive runs and the CI size check was deciding by coin flip which
    of those it had. Naming the directory ``tempctrl`` fixed the width but left
    the random parent one level up, which the Explorer's "Library folder" field
    shows in full — so all 12 Explorer PNGs changed on every run instead.

    ``--live`` is the exception — there is one KiCad and one open board, and the
    live path is for looking at real data, not for reproducible screenshots.
    """
    if args.live:
        return kicad_bridge.connect()
    board = kicad_bridge.open_fixture(args.fixture)
    owned = os.path.join(PROBE_PROJECT_ROOT, screen)
    shutil.rmtree(owned, ignore_errors=True)
    project = os.path.join(owned, PROJECT_NAME)
    os.makedirs(project, exist_ok=True)
    board.relocate(project)
    return board


@contextlib.contextmanager
def _capture(path: Optional[str]):
    """Send everything printed in the block to *path*, or leave stdout alone.

    The geometry dump is a **cross-platform reference**, so it has to be
    produced by a command spelled identically on macOS, Linux and Windows —
    which rules out piping stdout through ``grep`` to strip the progress lines.
    """
    if not path:
        yield
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with (
        open(path, "a", encoding="utf-8", newline="\n") as handle,
        contextlib.redirect_stdout(handle),
    ):
        yield


def render(name: str, context: Context, output_dir: str, mode: str) -> tuple[bool, str]:
    """Build one screen, grab it to a PNG, optionally dump its geometry.

    **Grabbed twice, and the second one is the screenshot.** Offscreen, nothing
    paints until something asks it to, and the first ``grab()`` is what asks.
    Widgets that animate their own appearance start doing so at that first paint
    — a ``QLineEdit``'s clear button fades in over a couple of hundred
    milliseconds — so the first grab catches the fade partway through, at
    whatever opacity the clock happened to be at. The settle before it cannot
    help: the animation has not started yet. Grabbing again after another settle
    catches the state the user would actually see.
    """
    builder = SCREENS[name]
    suffix = "" if mode == "light" else f"-{mode}"
    target = os.path.join(output_dir, f"{name}{suffix}.png")
    try:
        widget = builder(context)
        settle()
        widget.grab()
        settle()
        pixmap = widget.grab()
        os.makedirs(output_dir, exist_ok=True)
        if not pixmap.save(target, "PNG"):
            return False, f"{name}: grab() produced nothing to save"
        print(
            f"{name}: {pixmap.width()}x{pixmap.height()} -> "
            f"{os.path.relpath(target, _ROOT)}"
        )
        if context.args.geometry or context.args.geometry_out:
            with _capture(context.args.geometry_out):
                print(f"--- {name}{suffix} geometry ---")
                dump_tree(widget)
                for view in _descendants(widget):
                    if hasattr(view, "horizontalHeader") and hasattr(
                        view, "columnWidth"
                    ):
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
    parser.add_argument(
        "--geometry-out",
        metavar="FILE",
        help=(
            "write the geometry dump to FILE instead of stdout. This is the "
            "cross-platform reference: run it on two platforms and diff."
        ),
    )
    parser.add_argument("--list", action="store_true", help="list the screens and exit")
    args = parser.parse_args(argv)
    # INFO, so the log pane shows what a real session shows. store.py logs a
    # DEBUG line per part, and derive_params calls basicConfig(DEBUG) at import,
    # which would otherwise fill the pane with reconciliation chatter.
    app_module.configure_logging(logging.INFO)
    # Before any window exists, because the pane's handler formats records as
    # they arrive and the first of them is logged while the window is building.
    freeze_log_clock()

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
    if args.geometry_out:
        # Truncate once here rather than per screen: ``_capture`` appends, so
        # each screen adds to one report, and a second run must replace that
        # report rather than double it.
        os.makedirs(
            os.path.dirname(os.path.abspath(args.geometry_out)) or ".", exist_ok=True
        )
        open(args.geometry_out, "w", encoding="utf-8").close()
    failures = []
    live_board = None
    for mode in modes:
        application = app_module.build_application(theme_mode=mode, offscreen=True)
        freeze_cursor_blink(application)
        for name in names:
            # Fresh board and fresh settings per screen. The board because
            # screens write to it (see open_board); the settings because the
            # main window saves its geometry on close and the next screen would
            # restore it — which offscreen means being clamped to the 800x800
            # virtual screen and rendering at the wrong size for a reason that
            # has nothing to do with the screen under review.
            if args.live:
                live_board = live_board or open_board(args)
                board = live_board
            else:
                board = open_board(args, name)
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
