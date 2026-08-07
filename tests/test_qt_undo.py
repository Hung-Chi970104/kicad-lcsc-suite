"""Tests for the app's own undo — the button, and what it puts back.

The bug these exist for: ``Remove LCSC number``, then Cmd+Z, and nothing
happened. Three separate reasons, each of which needs its own guard here:

* **the keystroke went nowhere.** This window bound Ctrl+W, Ctrl+Q and
  Shift+Esc and nothing else, so Cmd+Z with our window focused — which is where
  focus *is* right after using our button — reached no action at all;
* **KiCad's undo cannot reach the project database.** A removal clears the board
  field *and* the number and stock figure in ``project.db``. Undoing only the
  board half leaves the table, which reads the database, still saying
  unassigned;
* **a reversal has to be verified like any other write.** It goes through the
  same bridge, so trap 2 applies to it too, and a reversal that silently failed
  would be worse than no button.

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

from PySide6.QtGui import QKeySequence  # noqa: E402

from lcsc_suite import app as app_module, kicad_bridge  # noqa: E402
from lcsc_suite.config import Settings  # noqa: E402
from lcsc_suite.controller import SuiteController  # noqa: E402
from lcsc_suite.parts import PartList, open_fixture_library  # noqa: E402
from lcsc_suite.undo import DEPTH, UndoStack  # noqa: E402

FIXTURE = (
    Path(__file__).resolve().parent.parent / "lcsc_suite" / "fixtures" / "board.json"
)

#: Unassigned in the fixture, and without an LCSC field at all — so assigning to
#: it and reversing that exercises trap 3 in both directions.
UNASSIGNED = "G1"

#: Ships with a number on it, so a removal here has something to put back.
ASSIGNED = "R1"

#: Three references that carry the same number in the fixture, for the batch
#: cases.
SAME_NUMBER = ("J1", "J2", "J3")


@pytest.fixture(scope="session", autouse=True)
def application():
    """Build the QApplication the widgets live in."""
    return app_module.build_application(theme_mode="light", offscreen=True)


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """Collect the message boxes instead of showing them.

    ``QMessageBox.critical`` blocks on the offscreen platform exactly as it does
    on a real one, so a test that provokes a refused write would hang rather
    than fail. Returns the list, for the tests that assert something was said.
    """
    shown: list = []
    for name in ("critical", "warning", "information"):
        monkeypatch.setattr(
            f"lcsc_suite.controller.QMessageBox.{name}",
            lambda *args, **kwargs: shown.append(args),
        )
    return shown


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
    """Return the fixture board, pointed at a writable project directory."""
    return _board(tmp_path)


@pytest.fixture
def trapped_board(tmp_path):
    """Return a board that accepts writes and changes nothing — trap 2."""
    return _board(tmp_path, honour_footprint_writes=False)


def _controller(board, tmp_path):
    settings = Settings(path=str(tmp_path / "settings.json"))
    parts = PartList(board, settings=settings)
    parts.library = open_fixture_library(parts.owner, str(tmp_path / "library"))
    parts.refresh_from_board()
    return SuiteController(board, parts, settings=settings)


@pytest.fixture
def controller(board, tmp_path):
    """Return a controller over the fixture board, with its window."""
    result = _controller(board, tmp_path)
    yield result
    result.window.close()


def _select(controller, *references):
    """Select rows by reference, the way a click would."""
    controller.window.select_references(list(references))


def _on_board(board, reference: str) -> str:
    return board.footprint(reference).lcsc


def _stored(controller, reference: str) -> dict:
    for part in controller.parts.store.read_all():
        if part["reference"] == reference:
            return part
    raise AssertionError(f"{reference} is not in the project database")


# --- the stack itself ------------------------------------------------------


def test_an_empty_stack_has_nothing_to_describe():
    """The button reads this to decide whether it is enabled at all."""
    stack = UndoStack()
    assert not stack
    assert stack.description is None
    assert stack.pop() is None


def test_the_stack_is_last_in_first_out():
    """Undo walks back through history, most recent first."""
    done = []
    stack = UndoStack()
    stack.push("first", lambda: done.append("first"))
    stack.push("second", lambda: done.append("second"))

    assert stack.description == "second"
    stack.pop().revert()
    assert done == ["second"]
    assert stack.description == "first"


def test_the_stack_is_bounded():
    """An open window is a long-lived object; the stack must not grow forever."""
    stack = UndoStack(depth=3)
    for index in range(10):
        stack.push(str(index), lambda: None)
    assert len(stack) == 3
    assert stack.description == "9"


# --- the button ------------------------------------------------------------


def test_undo_is_the_first_button_and_starts_disabled(controller):
    """Nothing has been done, so there is nothing to reverse."""
    window = controller.window
    assert window.main_toolbar.actions()[0] is window.undo_action
    assert window.undo_action.isEnabled() is False
    assert "Nothing to reverse yet" in window.undo_action.toolTip()


def test_undo_binds_the_platform_undo_shortcut(controller):
    """The bug: this window bound no undo key, so Cmd+Z reached nothing.

    ``StandardKey.Undo`` is Cmd+Z on macOS and Ctrl+Z elsewhere, which is why
    the binding is stated that way rather than as a literal sequence.
    """
    expected = QKeySequence(QKeySequence.StandardKey.Undo)
    assert controller.window.undo_action.shortcut() == expected


def test_the_tooltip_names_the_action_it_would_reverse(controller):
    """The label stays "Undo", so the tooltip is where the action is named."""
    _select(controller, ASSIGNED)
    controller.remove()

    action = controller.window.undo_action
    assert action.isEnabled() is True
    assert f"Reverse: remove the LCSC number from {ASSIGNED}" in action.toolTip()


def test_the_button_goes_flat_again_once_the_stack_is_empty(controller):
    """Nothing left to reverse reads as a disabled button, not a silent no-op."""
    _select(controller, ASSIGNED)
    controller.remove()
    controller.undo_last()

    assert controller.window.undo_action.isEnabled() is False
    assert "Nothing to reverse yet" in controller.window.undo_action.toolTip()


# --- reversing a removal ---------------------------------------------------


def test_reversing_a_removal_puts_the_number_back_on_the_board(controller, board):
    """The board half — the one KiCad's own undo could in principle cover."""
    before = _on_board(board, ASSIGNED)
    _select(controller, ASSIGNED)
    controller.remove()
    assert _on_board(board, ASSIGNED) == ""

    controller.undo_last()
    assert _on_board(board, ASSIGNED) == before


def test_reversing_a_removal_puts_the_number_back_in_the_database(controller):
    """The half KiCad's own Cmd+Z cannot reach, and the half the table reads."""
    before = _stored(controller, ASSIGNED)["lcsc"]
    _select(controller, ASSIGNED)
    controller.remove()
    assert _stored(controller, ASSIGNED)["lcsc"] == ""

    controller.undo_last()
    assert _stored(controller, ASSIGNED)["lcsc"] == before


def test_reversing_a_removal_puts_the_stock_figure_back(controller):
    """Not decoration: without it the row comes back showing ``?``.

    ``clear()`` drops the stock figure with the number and ``assign()`` does not
    re-derive one, so a reversal that restored only the number would leave a
    column that had a figure in it a moment earlier reading "nobody answered".

    The figure is put on deliberately here rather than taken from the fixture:
    the fixture's parts get their stock from the *cache*, and only a caller with
    a figure at assignment time — the Explorer, from Phase 4 — writes the store's
    column at all. That is the value with nowhere else to come back from.
    """
    _select(controller, ASSIGNED)
    controller.assign_number([ASSIGNED], "C99999", stock=4321)
    assert _stored(controller, ASSIGNED)["stock"] == 4321

    controller.remove()
    assert _stored(controller, ASSIGNED)["stock"] is None

    controller.undo_last()
    assert _stored(controller, ASSIGNED)["stock"] == 4321


def test_an_unknown_stock_figure_survives_a_reversal(controller):
    """``''`` in the store means unknown, and ``int('')`` raises.

    ``store.create_part`` defaults the column to the empty string rather than to
    SQL ``NULL``, so every fixture part starts there. Snapshotting the raw value
    and handing it back to ``assign`` is how the first version of this crashed.
    """
    assert _stored(controller, ASSIGNED)["stock"] in ("", None)

    _select(controller, ASSIGNED)
    controller.remove()
    controller.undo_last()

    assert _stored(controller, ASSIGNED)["stock"] is None


def test_reversing_a_removal_restores_the_row_to_what_it_was(controller):
    """End to end, in the terms the user sees: the row is as it was."""
    window = controller.window

    def row():
        return next(r for r in window.part_model.rows() if r.reference == ASSIGNED)

    before = row()
    _select(controller, ASSIGNED)
    controller.remove()
    assert row().lcsc == ""

    controller.undo_last()
    after = row()
    assert (after.lcsc, after.stock, after.part_type, after.params) == (
        before.lcsc,
        before.stock,
        before.part_type,
        before.params,
    )


# --- reversing an assignment -----------------------------------------------


def test_reversing_an_assignment_clears_a_reference_that_had_no_number(
    controller, board
):
    """The reverse of "it had nothing" is "give it nothing back", not "skip it"."""
    _select(controller, UNASSIGNED)
    controller.assign_number([UNASSIGNED], "C99999")
    assert _on_board(board, UNASSIGNED) == "C99999"

    controller.undo_last()
    assert _on_board(board, UNASSIGNED) == ""
    assert _stored(controller, UNASSIGNED)["lcsc"] == ""


def test_reversing_an_assignment_restores_a_different_previous_number(
    controller, board
):
    """Overwriting an assignment is reversible too, not only creating one."""
    before = _on_board(board, ASSIGNED)
    assert before

    _select(controller, ASSIGNED)
    controller.assign_number([ASSIGNED], "C99999")
    controller.undo_last()

    assert _on_board(board, ASSIGNED) == before


def test_a_mixed_selection_comes_back_row_by_row(controller, board):
    """One reference had a number and one did not; both go back to their own."""
    previous = {
        ASSIGNED: _on_board(board, ASSIGNED),
        UNASSIGNED: _on_board(board, UNASSIGNED),
    }
    _select(controller, ASSIGNED, UNASSIGNED)
    controller.assign_number([ASSIGNED, UNASSIGNED], "C99999")
    controller.undo_last()

    assert {ref: _on_board(board, ref) for ref in previous} == previous


# --- batching --------------------------------------------------------------


def test_a_batch_reverses_in_one_commit(controller, board):
    """Same rule as the forward write: one action, one entry in KiCad's history.

    Twenty identical capacitors are one undo step going forward; the reversal
    groups by ``(number, stock)`` so they are one coming back too.
    """
    controller.assign_number(list(SAME_NUMBER), "C99999")
    commits = len(board.commits)

    controller.undo_last()
    assert len(board.commits) == commits + 1
    assert all(_on_board(board, ref) != "C99999" for ref in SAME_NUMBER)


def test_find_mapping_reverses_as_one_press(controller, board):
    """One thing the user did, however many commits it took.

    ``Find mapping`` writes one commit per distinct number, so without grouping
    a selection spanning three numbers would need three Undo presses — which is
    not an action anybody performed.
    """
    controller.parts.remember_mappings()
    references = list(SAME_NUMBER) + [ASSIGNED]
    previous = {ref: _on_board(board, ref) for ref in references}

    controller.window.select_references(references)
    controller.remove()
    controller.find_mapping(references)
    assert {ref: _on_board(board, ref) for ref in references} == previous

    # Two presses: one for the find-mapping group, one for the removal.
    controller.undo_last()
    assert all(_on_board(board, ref) == "" for ref in references)
    controller.undo_last()
    assert {ref: _on_board(board, ref) for ref in references} == previous


# --- exclusions ------------------------------------------------------------


def test_reversing_a_bom_toggle_restores_the_previous_state(controller, board):
    """Exclusions are board attributes, so they need reversing as much as fields."""
    before = board.footprint(ASSIGNED).exclude_from_bom
    _select(controller, ASSIGNED)
    controller.toggle_exclusions(bom=True)
    assert board.footprint(ASSIGNED).exclude_from_bom is not before

    controller.undo_last()
    assert board.footprint(ASSIGNED).exclude_from_bom is before


def test_reversing_a_combined_toggle_restores_both_attributes(controller, board):
    """``Toggle BOM & POS`` writes two commits; one press has to undo both."""
    view = board.footprint(ASSIGNED)
    before = (view.exclude_from_bom, view.exclude_from_pos)

    _select(controller, ASSIGNED)
    controller.toggle_exclusions(bom=True, pos=True)
    controller.undo_last()

    view = board.footprint(ASSIGNED)
    assert (view.exclude_from_bom, view.exclude_from_pos) == before


def test_a_mixed_exclusion_selection_is_restored_per_reference(controller, board):
    """Restore each reference to its own previous state.

    A toggle on a mixed selection has no single target state, and neither does
    its reversal.
    """
    references = [ASSIGNED, "R12"]
    controller.window.select_references([references[0]])
    controller.toggle_exclusions(bom=True)

    before = {ref: board.footprint(ref).exclude_from_bom for ref in references}
    assert len(set(before.values())) == 2, "selection must be mixed to be a test"

    controller.window.select_references(references)
    controller.toggle_exclusions(bom=True)
    controller.undo_last()

    assert {ref: board.footprint(ref).exclude_from_bom for ref in references} == before


# --- failure and safety ----------------------------------------------------


def test_a_reversal_that_the_board_refuses_keeps_the_entry(tmp_path, no_modal_dialogs):
    """Trap 2 applies to reversals too, so it must not swallow the entry.

    The board is unchanged after a refused write, which means the reversal is
    still valid — losing it would leave the user with no way back at all.
    """
    board = _board(tmp_path)
    controller = _controller(board, tmp_path)
    try:
        _select(controller, ASSIGNED)
        controller.remove()
        assert controller.undo_stack.description is not None

        board._honour = False  # the board starts ignoring footprint writes
        controller.undo_last()

        assert no_modal_dialogs, "a refused reversal has to be reported"
        assert controller.undo_stack.description is not None
        assert controller.window.undo_action.isEnabled() is True
    finally:
        controller.window.close()


def test_a_refused_write_records_nothing_to_reverse(trapped_board, tmp_path):
    """Nothing changed, so there is nothing to put back.

    A stack entry here would offer to "reverse" a write that never landed, and
    performing it would write the *old* value as if it were a change.
    """
    controller = _controller(trapped_board, tmp_path)
    try:
        _select(controller, ASSIGNED)
        controller.remove()
        assert controller.undo_stack.description is None
        assert controller.window.undo_action.isEnabled() is False
    finally:
        controller.window.close()


def test_reversing_does_not_stack_a_reversal_of_its_own(controller):
    """Walk back through history rather than alternating between two states.

    A reversal that recorded a reversal of its own would make the button flip
    the same change back and forth forever.
    """
    _select(controller, ASSIGNED)
    controller.remove()
    _select(controller, UNASSIGNED)
    controller.assign_number([UNASSIGNED], "C99999")
    assert len(controller.undo_stack) == 2

    controller.undo_last()
    assert len(controller.undo_stack) == 1
    controller.undo_last()
    assert len(controller.undo_stack) == 0


def test_undo_with_an_empty_stack_does_nothing(controller, board):
    """The button is disabled, but the shortcut and a stray call must be safe."""
    commits = len(board.commits)
    controller.undo_last()
    assert len(board.commits) == commits


def test_walking_back_through_several_actions(controller, board):
    """The stack is a history, not a single slot."""
    original = _on_board(board, ASSIGNED)
    _select(controller, ASSIGNED)
    controller.assign_number([ASSIGNED], "C11111")
    controller.assign_number([ASSIGNED], "C22222")
    controller.assign_number([ASSIGNED], "C33333")

    for expected in ("C22222", "C11111", original):
        controller.undo_last()
        assert _on_board(board, ASSIGNED) == expected


# --- the schematic-sync bookkeeping ---------------------------------------


def test_reversing_a_removal_takes_it_out_of_the_cleared_set(controller):
    """Phase 7 exports ``schematic_cleared_refs`` as deliberate removals.

    A reference whose removal has been reversed is not a deliberate removal any
    more, and leaving it in the set would tell the schematic export to wipe a
    number the user had just got back — a distinction Phase 7 cannot
    reconstruct.
    """
    _select(controller, ASSIGNED)
    controller.remove()
    assert ASSIGNED in controller.schematic_cleared_refs

    controller.undo_last()
    assert ASSIGNED not in controller.schematic_cleared_refs


def test_reversing_an_assignment_puts_a_cleared_reference_back_in_the_set(controller):
    """Put a reference back in the cleared set when its assignment is reversed.

    The other direction of the rule above: assigning took it out of the set, so
    reversing the assignment has to return it.
    """
    _select(controller, ASSIGNED)
    controller.remove()
    controller.assign_number([ASSIGNED], "C99999")
    assert ASSIGNED not in controller.schematic_cleared_refs

    controller.undo_last()
    assert ASSIGNED in controller.schematic_cleared_refs


def test_the_default_depth_is_stated_not_incidental():
    """A bound exists on purpose; this pins it so a change is a decision."""
    assert DEPTH == 50
    assert len(UndoStack()._entries.maxlen * [0]) == DEPTH
