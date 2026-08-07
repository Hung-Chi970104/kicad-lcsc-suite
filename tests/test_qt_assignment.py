"""Tests for Phase 3 — the assignment path.

What is worth protecting here is not that a button is connected. It is that:

* **a write KiCad refused never reaches the project database.** This is trap 2,
  and the fixture board reproduces it exactly
  (``honour_footprint_writes=False``), so these tests prove the read-back
  assertion fires rather than assuming it would;
* **a footprint with no LCSC field gets one** (trap 3) rather than the write
  silently going nowhere;
* **removal only propagates for a number the user could see.** Clearing an
  already-blank row must not tell the schematic to wipe a number it has and the
  board never picked up;
* **the funnel is single.** The dialog, ``Paste LCSC``, ``Find mapping`` and
  (from Phase 4) the Explorer all reach the board through one method, because
  four copies of the same eight lines is how the wx plugin's four entry points
  drifted apart.

Rendered offscreen, so they need no display.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QDialogButtonBox  # noqa: E402

from lcsc_suite import app as app_module, kicad_bridge  # noqa: E402
from lcsc_suite.config import Settings  # noqa: E402
from lcsc_suite.controller import HANDLED_ROW_MENU, SuiteController  # noqa: E402
from lcsc_suite.kicad_bridge import WriteVerificationError, sanitize_lcsc  # noqa: E402
from lcsc_suite.parts import PartList, open_fixture_library  # noqa: E402
from lcsc_suite.ui.assign_dialog import AssignNumberDialog, describe  # noqa: E402
from lcsc_suite.ui.main_window import ROW_MENU, MainWindow  # noqa: E402

FIXTURE = (
    Path(__file__).resolve().parent.parent / "lcsc_suite" / "fixtures" / "board.json"
)

#: A reference the fixture leaves unassigned *and* without an LCSC field at all,
#: so assigning to it exercises trap 3 (the field has to be created).
UNASSIGNED = "G1"

#: A reference the fixture ships with a number already on it.
ASSIGNED = "R1"


@pytest.fixture(scope="session", autouse=True)
def application():
    """Build the QApplication the widgets and the clipboard live in."""
    return app_module.build_application(theme_mode="light", offscreen=True)


def _board(tmp_path, **kwargs):
    """Return a fresh fixture board pointed at a writable project directory."""
    with open(FIXTURE, encoding="utf-8") as handle:
        board = kicad_bridge.FixtureBoard.from_dict(
            copy.deepcopy(json.load(handle)), **kwargs
        )
    board.relocate(str(tmp_path))
    return board


@pytest.fixture
def board(tmp_path):
    """Return the fixture board."""
    return _board(tmp_path)


@pytest.fixture
def trapped_board(tmp_path):
    """Return a board that accepts writes and changes nothing — trap 2."""
    return _board(tmp_path, honour_footprint_writes=False)


def _part_list(board, tmp_path, name="library"):
    """Return a reconciler with a throwaway seeded library."""
    settings = Settings(path=str(tmp_path / "settings.json"))
    parts = PartList(board, settings=settings)
    parts.library = open_fixture_library(parts.owner, str(tmp_path / name))
    parts.refresh_from_board()
    return parts


@pytest.fixture
def parts(board, tmp_path):
    """Return a reconciler over the fixture board."""
    return _part_list(board, tmp_path)


@pytest.fixture
def controller(board, parts):
    """Return a controller, its part list and its window."""
    result = SuiteController(board, parts, settings=parts.settings)
    yield result
    result.window.close()


def _lcsc_on_board(board, reference: str) -> str:
    """Read one reference's LCSC number straight off the board."""
    return board.footprint(reference).lcsc


def _stored(parts, reference: str) -> dict:
    """Read one reference's row out of the project database."""
    for part in parts.store.read_all():
        if part["reference"] == reference:
            return part
    raise AssertionError(f"{reference} is not in the project database")


def _row(controller, reference: str):
    """Return the displayed row for ``reference``."""
    return next(
        row for row in controller.window.part_model.rows() if row.reference == reference
    )


# --- reading input ---------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("C1524", "C1524"),
        ("  C1524  ", "C1524"),
        ("c1524", "C1524"),
        ("https://www.lcsc.com/product-detail/C1524.html", "C1524"),
        ("LCSC: C1524\tExtended", "C1524"),
        ("", ""),
        ("no number here", ""),
        ("R_0402_1005Metric", ""),
    ],
)
def test_a_number_is_found_in_whatever_was_pasted(text, expected):
    """A clipboard rarely holds a bare number; it holds the page it came from."""
    assert sanitize_lcsc(text) == expected


def test_text_with_no_number_reads_as_nothing_not_as_a_removal():
    """The distinction that stops a failed paste clearing an assignment."""
    assert sanitize_lcsc("paste went wrong") == ""


# --- the write path --------------------------------------------------------


def test_assigning_writes_the_board_and_then_the_database(parts, board):
    """Both sides end up saying the same thing, which is the whole job."""
    parts.assign([UNASSIGNED], "C1525")

    assert _lcsc_on_board(board, UNASSIGNED) == "C1525"
    assert _stored(parts, UNASSIGNED)["lcsc"] == "C1525"


def test_assigning_creates_the_field_on_a_footprint_that_had_none(parts, board):
    """Trap 3: the LCSC field is not an attribute, it has to be added."""
    assert board.footprint(UNASSIGNED).lcsc_field == ""

    parts.assign([UNASSIGNED], "C1525")

    view = board.footprint(UNASSIGNED)
    assert view.lcsc == "C1525"
    assert view.lcsc_field == kicad_bridge.DEFAULT_LCSC_FIELD
    # Hidden, as the wx plugin's is: a number on the silkscreen is not what the
    # user asked for by assigning one.
    assert view.lcsc_visible is False


def test_a_refused_write_never_reaches_the_database(trapped_board, tmp_path):
    """Trap 2 in the act: success is reported and the board does not change."""
    parts = _part_list(trapped_board, tmp_path)
    before = _stored(parts, UNASSIGNED)["lcsc"]

    with pytest.raises(WriteVerificationError):
        parts.assign([UNASSIGNED], "C1525")

    assert _lcsc_on_board(trapped_board, UNASSIGNED) == ""
    assert _stored(parts, UNASSIGNED)["lcsc"] == before


def test_a_refused_write_puts_the_board_back(trapped_board, tmp_path):
    """Trap 4: it has to be pushed to be verified, so undoing takes a commit.

    The board ends up exactly as it was, which is what matters. What it costs is
    two entries in KiCad's undo history instead of none — see the bridge's
    ``test_a_failed_write_costs_two_undo_entries``.
    """
    parts = _part_list(trapped_board, tmp_path)
    before = {view.reference: view for view in trapped_board.footprints()}

    with pytest.raises(WriteVerificationError):
        parts.assign([UNASSIGNED], "C1525")

    after = {view.reference: view for view in trapped_board.footprints(refresh=True)}
    assert after == before


def test_assigning_rejects_something_that_is_not_a_number(parts, board):
    """Refused before the board is touched, not reported after it is."""
    with pytest.raises(ValueError):
        parts.assign([UNASSIGNED], "not a number")

    assert _lcsc_on_board(board, UNASSIGNED) == ""


def test_assigning_accepts_a_pasted_url(parts, board):
    """The same input the clipboard path takes, through the same funnel."""
    parts.assign([UNASSIGNED], "https://www.lcsc.com/product-detail/C1525.html")

    assert _lcsc_on_board(board, UNASSIGNED) == "C1525"


def test_one_number_reaches_every_selected_reference(parts, board):
    """Assigning to a selection is the tedium this plugin exists to remove."""
    references = ["G1", "G2", "G3"]

    written = parts.assign(references, "C1525")

    assert written == references
    for reference in references:
        assert _lcsc_on_board(board, reference) == "C1525"


def test_a_reference_that_is_not_on_the_board_is_skipped(parts, board):
    """A footprint deleted in KiCad while this window was open."""
    written = parts.assign([UNASSIGNED, "NOPE99"], "C1525")

    assert written == [UNASSIGNED]
    assert _lcsc_on_board(board, UNASSIGNED) == "C1525"


def test_a_selection_with_nothing_on_the_board_writes_nothing(parts, board):
    """No commit at all, rather than an empty one."""
    assert parts.assign(["NOPE99"], "C1525") == []
    assert board.commits == []


# --- clearing --------------------------------------------------------------


def test_clearing_removes_the_number_from_the_board_and_the_database(parts, board):
    """Same two-sided rule as assigning, in the other direction."""
    assert _lcsc_on_board(board, ASSIGNED) != ""

    parts.clear([ASSIGNED])

    assert _lcsc_on_board(board, ASSIGNED) == ""
    assert (_stored(parts, ASSIGNED)["lcsc"] or "") == ""


def test_clearing_takes_the_stock_figure_with_it(parts):
    """A count against a part that no longer names one is worse than a blank."""
    parts.assign([UNASSIGNED], "C1525", stock=4321)
    assert _stored(parts, UNASSIGNED)["stock"] == 4321

    parts.clear([UNASSIGNED])

    assert _stored(parts, UNASSIGNED)["stock"] is None


def test_an_unresolved_stock_is_stored_as_unknown_not_as_zero(parts):
    """``None`` is "nobody answered"; 0 is "a source said none"."""
    parts.assign([UNASSIGNED], "C1525", stock=None)

    assert _stored(parts, UNASSIGNED)["stock"] is None


def test_a_confirmed_zero_is_stored_as_zero(parts):
    """The other half of the distinction the whole fork exists to keep."""
    parts.assign([UNASSIGNED], "C1525", stock=0)

    assert _stored(parts, UNASSIGNED)["stock"] == 0


# --- the controller's decisions --------------------------------------------


def test_assigning_through_the_controller_refreshes_the_list(controller):
    """The three cache-derived columns fill in without a second user action."""
    controller.assign_number([UNASSIGNED], "C1525")

    row = _row(controller, UNASSIGNED)
    assert row.lcsc == "C1525"
    # Re-resolved from the seeded cache by the rebuild, not written by the
    # assignment: the number is the only thing the board is told.
    assert row.part_type == "Basic"
    assert row.params


def test_an_assigned_row_stops_being_marked_as_needing_a_number(controller):
    """Red means "in the BOM with nothing for JLC to place"; now there is."""
    assert _row(controller, UNASSIGNED).needs_a_number

    controller.assign_number([UNASSIGNED], "C1525")

    assert not _row(controller, UNASSIGNED).needs_a_number


def test_removal_records_only_the_references_that_had_a_number(controller):
    """What Phase 7 may export as a deliberate clearing, and what it may not."""
    controller.window.select_references([ASSIGNED, UNASSIGNED])

    controller.remove()

    assert controller.schematic_cleared_refs == {ASSIGNED}


def test_assigning_takes_a_reference_back_off_the_cleared_list(controller):
    """A number reinstated here must not still read as a removal to export."""
    controller.window.select_references([ASSIGNED])
    controller.remove()
    assert ASSIGNED in controller.schematic_cleared_refs

    controller.assign_number([ASSIGNED], "C1525")

    assert ASSIGNED not in controller.schematic_cleared_refs


def test_an_assignment_marks_the_schematic_out_of_date(controller):
    """A flag only. Board<->schematic sync is never automatic."""
    assert controller.schematic_sync_pending is False

    controller.assign_number([UNASSIGNED], "C1525")

    assert controller.schematic_sync_pending is True


def test_a_refused_write_is_reported_and_the_window_survives(
    trapped_board, tmp_path, monkeypatch
):
    """Trap 2 reaching the user as a message rather than as a dead window."""
    parts = _part_list(trapped_board, tmp_path)
    controller = SuiteController(trapped_board, parts, settings=parts.settings)
    shown = []
    monkeypatch.setattr(
        "lcsc_suite.controller.QMessageBox.critical",
        lambda *args, **kwargs: shown.append(args),
    )

    controller.assign_number([UNASSIGNED], "C1525")

    assert shown, "a refused write must say so"
    assert controller.window.isEnabled()
    controller.window.close()


def test_nothing_selected_assigns_nothing(controller, board):
    """The dialog is not even opened, so nothing can be typed into nowhere."""
    controller.window.part_table.selectionModel().clearSelection()

    controller.assign()

    assert board.commits == []


# --- auto-select alike -----------------------------------------------------


def test_assigning_reaches_every_alike_part_the_selection_grew_to(controller, board):
    """What ``Auto-select alike`` adds in this phase: assign them in one go."""
    window = controller.window
    window.select_alike_action.setChecked(True)
    alike = controller.parts.alike("R4")
    assert len(alike) > 1

    window.select_references(["R4"])
    references = window.selected_references()
    assert set(references) == set(alike)

    controller.assign_number(references, "C1525")

    for reference in alike:
        assert _lcsc_on_board(board, reference) == "C1525"


def test_alike_parts_are_assigned_as_one_undo_step(controller, board):
    """One commit, because the user performed one action."""
    before = len(board.commits)

    controller.assign_number(controller.parts.alike("R4"), "C1525")

    assert len(board.commits) == before + 1


# --- clipboard -------------------------------------------------------------


def test_copying_puts_the_number_on_the_clipboard(controller, board):
    """The single-row case, which is the one the wx plugin got right."""
    controller.copy_lcsc([ASSIGNED])

    assert QGuiApplication.clipboard().text() == _lcsc_on_board(board, ASSIGNED)


def test_copying_several_rows_keeps_every_distinct_number(controller, board):
    """The wx plugin overwrites the clipboard per row and keeps only the last."""
    controller.copy_lcsc(["R1", "R2", "R4"])

    copied = QGuiApplication.clipboard().text().splitlines()
    assert copied == sorted(set(copied), key=copied.index)
    assert _lcsc_on_board(board, "R1") in copied
    assert _lcsc_on_board(board, "R2") in copied


def test_copying_a_row_with_no_number_copies_nothing(controller):
    """Better to leave the clipboard alone than to empty it."""
    QGuiApplication.clipboard().setText("untouched")

    controller.copy_lcsc([UNASSIGNED])

    assert QGuiApplication.clipboard().text() == "untouched"


def test_pasting_assigns_the_clipboard_number(controller, board):
    """The wx plugin's only route to a typed number, kept working."""
    QGuiApplication.clipboard().setText("C1525")

    controller.paste_lcsc([UNASSIGNED])

    assert _lcsc_on_board(board, UNASSIGNED) == "C1525"


def test_pasting_junk_changes_nothing(controller, board):
    """A clipboard holding something else is not an instruction to clear."""
    QGuiApplication.clipboard().setText("this is not a part number")

    controller.paste_lcsc([UNASSIGNED])

    assert _lcsc_on_board(board, UNASSIGNED) == ""
    assert board.commits == []


def test_a_copy_then_a_paste_round_trips(controller, board):
    """The two halves agree on what a number looks like on the way through."""
    controller.copy_lcsc([ASSIGNED])

    controller.paste_lcsc([UNASSIGNED])

    assert _lcsc_on_board(board, UNASSIGNED) == _lcsc_on_board(board, ASSIGNED)


# --- mappings --------------------------------------------------------------


def test_save_mappings_remembers_every_assigned_part(controller, parts):
    """The next board with this footprint+value on it should not have to ask."""
    written = parts.save_all_mappings()

    assert written > 0
    view = controller.board.footprint(ASSIGNED)
    row = parts.library.get_mapping_data(view.footprint, view.value)
    assert row is not None
    assert row[2] == view.lcsc


def test_a_part_with_no_number_is_not_remembered(parts, board):
    """A mapping keyed on a blank would hand every such part the same number."""
    parts.save_all_mappings()

    view = board.footprint(UNASSIGNED)
    assert parts.library.get_mapping_data(view.footprint, view.value) is None


def test_remembering_the_same_pair_twice_updates_rather_than_duplicates(parts, board):
    """Two rows for one footprint+value would make the lookup order-dependent."""
    view = board.footprint(ASSIGNED)
    parts.save_all_mappings()

    parts.assign([ASSIGNED], "C1525")
    parts.save_all_mappings()

    assert parts.library.get_mapping_data(view.footprint, view.value)[2] == "C1525"
    rows = [
        row
        for row in parts.library.get_all_mapping_data()
        if row[0] == view.footprint and row[1] == view.value
    ]
    assert len(rows) == 1


def test_add_mapping_records_only_the_selected_rows(controller, parts, board):
    """The row-menu entry is per-selection; ``Save mappings`` is the whole board."""
    controller.add_mapping([ASSIGNED])

    view = board.footprint(ASSIGNED)
    assert parts.library.get_mapping_data(view.footprint, view.value) is not None
    other = board.footprint("R2")
    assert parts.library.get_mapping_data(other.footprint, other.value) is None


def test_find_mapping_assigns_what_was_remembered(controller, parts, board):
    """Teach it R1's number, clear R1, and ask for it back."""
    parts.save_all_mappings()
    remembered = _lcsc_on_board(board, ASSIGNED)
    controller.window.select_references([ASSIGNED])
    controller.remove()
    assert _lcsc_on_board(board, ASSIGNED) == ""

    controller.find_mapping([ASSIGNED])

    assert _lcsc_on_board(board, ASSIGNED) == remembered


def test_find_mapping_with_nothing_remembered_changes_nothing(controller, board):
    """Nothing to say is not the same as something to clear."""
    before = len(board.commits)

    controller.find_mapping([UNASSIGNED])

    assert _lcsc_on_board(board, UNASSIGNED) == ""
    assert len(board.commits) == before


def test_find_mapping_writes_one_commit_per_distinct_number(controller, parts, board):
    """Twenty identical capacitors are one action, not twenty undo steps."""
    parts.save_all_mappings()
    alike = parts.alike("R4")
    assert len(alike) > 1
    controller.window.select_references(alike)
    controller.remove()
    before = len(board.commits)

    controller.find_mapping(alike)

    assert len(board.commits) == before + 1


def test_mappings_are_a_no_op_without_a_library(board, tmp_path):
    """An unreadable data directory costs mappings, not the window."""
    settings = Settings(path=str(tmp_path / "settings.json"))
    parts = PartList(board, settings=settings)
    parts.library = None
    parts.refresh_from_board()

    assert parts.save_all_mappings() == 0
    assert parts.remember_mappings([ASSIGNED]) == 0
    assert parts.mapped_numbers([ASSIGNED]) == {}


# --- the row menu ----------------------------------------------------------


def test_every_handled_entry_is_a_real_menu_entry():
    """A dispatch id with no menu entry behind it is unreachable code."""
    ids = {entry_id for entry_id, _ in ROW_MENU if entry_id}
    assert HANDLED_ROW_MENU.issubset(ids)


def test_the_corrections_entries_wait_for_their_dialog():
    """Phase 5 owns them; until then they are greyed out, not removed."""
    assert not any(entry_id.startswith("correction-") for entry_id in HANDLED_ROW_MENU)


def test_the_controller_declares_which_entries_are_live(controller):
    """The window does not guess which entries do something."""
    assert controller.window._enabled_row_menu == set(HANDLED_ROW_MENU)


def test_a_window_without_a_controller_disables_nothing(board):
    """A layout test should not have to build a controller to get a menu."""
    window = MainWindow(board)
    assert window._enabled_row_menu is None
    window.close()


def test_the_row_menu_dispatches_to_the_controller(controller, board):
    """The signal Phase 2 left emitting into nothing now reaches something."""
    QGuiApplication.clipboard().setText("C1525")

    controller.window.row_menu_triggered.emit("paste-lcsc", [UNASSIGNED])

    assert _lcsc_on_board(board, UNASSIGNED) == "C1525"


def test_an_unhandled_entry_is_ignored_rather_than_raising(controller):
    """The menu disables them, but the signal is public and takes any id."""
    controller.window.row_menu_triggered.emit("correction-by-reference", [ASSIGNED])


# --- exclusions, now on the controller -------------------------------------


def test_toggling_bom_through_the_controller_writes_the_board(controller, board):
    """Moved off the window in this phase; it must still reach the board."""
    before = board.footprint(ASSIGNED).exclude_from_bom
    controller.window.select_references([ASSIGNED])

    controller.toggle_exclusions(bom=True)

    assert board.footprint(ASSIGNED).exclude_from_bom is not before


def test_toggling_with_nothing_selected_writes_nothing(controller, board):
    """An empty selection is not "every part"."""
    controller.window.part_table.selectionModel().clearSelection()
    before = len(board.commits)

    controller.toggle_exclusions(bom=True, pos=True)

    assert len(board.commits) == before


# --- the dialog ------------------------------------------------------------


def test_the_dialog_will_not_accept_an_empty_field(application):
    """Assignment writes to the board; validation belongs before that."""
    dialog = AssignNumberDialog(references=["R1"])
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)

    assert not ok.isEnabled()
    dialog.close()


def test_the_dialog_accepts_once_a_number_is_typed(application):
    """The straightforward path: someone who already knows the number."""
    dialog = AssignNumberDialog(references=["R1"])
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)

    dialog.input.setText("C1525")

    assert ok.isEnabled()
    assert dialog.number() == "C1525"
    dialog.close()


def test_the_dialog_extracts_a_number_from_a_pasted_url(application):
    """Pasting the product page is what people actually do."""
    dialog = AssignNumberDialog(references=["R1"])

    dialog.input.setText("https://www.lcsc.com/product-detail/C1525.html")

    assert dialog.number() == "C1525"
    # Says which number it found, because a long paste can contain more than one
    # and the wrong one reaching the board is otherwise silent.
    assert "C1525" in dialog.hint.text()
    dialog.close()


def test_the_dialog_says_so_when_there_is_no_number(application):
    """A disabled button with no explanation reads as a broken dialog."""
    dialog = AssignNumberDialog(references=["R1"])

    dialog.input.setText("R_0402_1005Metric")

    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert dialog.hint.text()
    dialog.close()


def test_the_dialog_pre_fills_a_number_the_whole_selection_shares(controller, board):
    """Pre-filling serves "the same as before, but a different one this time"."""
    number = _lcsc_on_board(board, "R4")
    alike = controller.parts.alike("R4")

    assert controller._shared_number(alike) == number


def test_a_mixed_selection_pre_fills_nothing(controller):
    """Picking one of several arbitrarily would be a suggestion, not a fact."""
    assert controller._shared_number([ASSIGNED, UNASSIGNED]) == ""


@pytest.mark.parametrize(
    "references,expected",
    [
        ([], "no footprints"),
        (["R1"], "R1"),
        (["R1", "R2"], "R1 and R2"),
        (["R1", "R2", "R3"], "R1, R2 and R3"),
    ],
)
def test_the_dialog_names_the_selection(references, expected):
    """The user should be able to see what is about to be written to."""
    assert describe(references) == expected


def test_a_long_selection_is_counted_rather_than_listed():
    """A hundred-part selection is a number, not a list."""
    described = describe([f"R{index}" for index in range(1, 21)])

    assert described.endswith("and 12 more")
