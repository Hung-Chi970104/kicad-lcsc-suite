"""Tests for Phase 7 — board ↔ schematic sync.

The parsers are not retested here. ``test_schematic_sync.py`` and
``test_schematic_import.py`` cover reading and writing ``.kicad_sch`` as text,
those modules port unchanged, and duplicating their coverage would only make it
possible for the two copies to disagree. What is worth protecting is the layer
this phase added:

* **a merely-blank reference and a deliberately cleared one stay different.**
  This is the distinction ``schematic_cleared_refs`` has carried since Phase 3
  for no consumer, and it is load-bearing in exactly one place — the export. A
  board that never picked up a number must not tell the schematic to wipe it;
* **the confirmation names what it is about to destroy, read from the file.**
  Not from what this session believes it assigned: only the schematic knows what
  the schematic currently says;
* **the import goes through the funnel.** ``assign_number``, so the board, the
  project database, the cleared set and the undo history stay in step — and one
  Undo press, because an import is one thing the user did;
* **nothing is automatic.** No assignment, reload or close writes to either
  side on its own.

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

from PySide6.QtWidgets import QMessageBox, QTableWidget  # noqa: E402

from lcsc_suite import app as app_module, kicad_bridge, kicad_locks  # noqa: E402
from lcsc_suite.config import Settings  # noqa: E402
from lcsc_suite.controller import SuiteController  # noqa: E402
from lcsc_suite.parts import PartList, open_fixture_library  # noqa: E402
from lcsc_suite.schematic import ADD, CLEAR, REPLACE, SKIP, SchematicSync  # noqa: E402
from lcsc_suite.ui.schematic_dialog import (  # noqa: E402
    SchematicSyncDialog,
    nothing_to_do_message,
)

FIXTURE = (
    Path(__file__).resolve().parent.parent / "lcsc_suite" / "fixtures" / "board.json"
)

#: The fixture board is ``tempctrl.kicad_pcb``, so its root sheet is the file
#: ``find_root_schematic`` looks for first. Named rather than derived, because a
#: test that computed it the same way the code does would pass either way.
ROOT_SHEET = "tempctrl.kicad_sch"

#: Unassigned on the fixture board, and without an LCSC field at all.
UNASSIGNED = "G1"

#: Assigned on the fixture board.
ASSIGNED = "R1"


@pytest.fixture(scope="session", autouse=True)
def application():
    """Build the QApplication the widgets live in."""
    return app_module.build_application(theme_mode="light", offscreen=True)


def _board(tmp_path):
    """Return a fresh fixture board pointed at a writable project directory."""
    with open(FIXTURE, encoding="utf-8") as handle:
        board = kicad_bridge.FixtureBoard.from_dict(copy.deepcopy(json.load(handle)))
    board.relocate(str(tmp_path))
    return board


@pytest.fixture
def board(tmp_path):
    """Return the fixture board."""
    return _board(tmp_path)


@pytest.fixture
def parts(board, tmp_path):
    """Return a reconciler over the fixture board."""
    settings = Settings(path=str(tmp_path / "settings.json"))
    part_list = PartList(board, settings=settings)
    part_list.library = open_fixture_library(part_list.owner, str(tmp_path / "library"))
    part_list.refresh_from_board()
    return part_list


@pytest.fixture
def controller(board, parts):
    """Return a controller whose close does not stop to ask anything.

    The pending flag is cleared in teardown rather than the offer stubbed: these
    tests write a real schematic into the project directory, so unlike every
    other suite the close-time offer would find one and put up a modal nothing
    is there to answer.
    """
    result = SuiteController(board, parts, settings=parts.settings)
    yield result
    result.schematic_sync_pending = False
    result.window.close()


@pytest.fixture
def sync(board, parts):
    """Return the sourcing layer on its own, without a window."""
    return SchematicSync(board, parts)


# ---------------------------------------------------------------------------
# Building schematics to read
# ---------------------------------------------------------------------------


def symbol(reference: str, lcsc=None) -> str:
    """Build one KiCad 8+ symbol instance, optionally with an LCSC field."""
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


def write_sheet(tmp_path, symbols, name: str = ROOT_SHEET) -> Path:
    """Write a root sheet holding ``symbols`` into the project directory."""
    path = Path(tmp_path) / name
    path.write_text(
        '(kicad_sch\n\t(version 20260306)\n\t(generator "eeschema")\n'
        + "".join(symbols)
        + ")\n",
        encoding="utf-8",
    )
    return path


def kinds(plan) -> dict:
    """Map each reference in ``plan`` to the kind of change it would get."""
    return {
        change.reference: change.kind
        for change in list(plan.changes) + list(plan.skipped)
    }


def number_of(parts, reference: str) -> str:
    """Read one reference's number out of the project database."""
    for part in parts.store.read_all():
        if part["reference"] == reference:
            return part["lcsc"] or ""
    raise AssertionError(f"{reference} is not in the project database")


# ---------------------------------------------------------------------------
# Finding the files
# ---------------------------------------------------------------------------


def test_the_root_sheet_is_found_by_the_board_name(sync, tmp_path):
    """One path is the whole hierarchy; the sub-sheets are followed from it."""
    path = write_sheet(tmp_path, [symbol(ASSIGNED, "C111")])
    assert sync.default_paths() == [str(path)]


def test_a_project_with_no_schematic_offers_no_path(sync):
    """Nothing is guessed at — an empty list is what sends the user a dialog."""
    assert sync.default_paths() == []


def test_a_sheet_open_in_the_editor_is_reported(sync, tmp_path):
    """KiCad's ``~<name>.lck``, written by the KiCad that is running."""
    path = write_sheet(tmp_path, [symbol(ASSIGNED, "C111")])
    (tmp_path / f"~{ROOT_SHEET}.lck").write_text("locked", encoding="utf-8")
    assert sync.locked([str(path)]) == [str(path)]


def test_a_lock_left_by_a_dead_kicad_is_not_a_sheet_in_the_editor(
    sync, tmp_path, monkeypatch
):
    """The reported bug: a leftover lock refused every write, forever.

    The socket dates the running session; a lock older than it belongs to a
    KiCad that is gone, so there is no editor to close and nothing to warn
    about. ``stale_locks`` still names it, because a lock being disregarded is
    not something to do silently.
    """
    path = write_sheet(tmp_path, [symbol(ASSIGNED, "C111")])
    lock = tmp_path / f"~{ROOT_SHEET}.lck"
    lock.write_text('{"hostname":"Mac","username":"nobody"}', encoding="utf-8")
    os.utime(lock, (50.0, 50.0))
    monkeypatch.setattr(kicad_locks, "kicad_session_start", lambda: 100.0)
    # Same user, or it reads as somebody else's lock rather than a leftover.
    monkeypatch.setattr(kicad_locks.getpass, "getuser", lambda: "nobody")

    assert sync.locked([str(path)]) == []
    assert sync.plan_export([str(path)]).locked == []
    assert sync.plan_export([str(path)]).stale_locks == [str(path)]


# ---------------------------------------------------------------------------
# To schematic — the preview
# ---------------------------------------------------------------------------


def test_the_export_preview_splits_additions_from_replacements(sync, tmp_path):
    """Read from the file: only the schematic knows what it currently says."""
    path = write_sheet(
        tmp_path,
        [symbol(ASSIGNED), symbol("R2", "C999"), symbol("R3", "C1")],
    )
    plan = sync.plan_export([str(path)])
    seen = kinds(plan)
    assert seen[ASSIGNED] == ADD  # a symbol with no number yet
    assert seen["R2"] == REPLACE  # a different number, about to be destroyed
    assert plan.changes, "a divergent schematic should have something to write"


def test_a_reference_the_schematic_agrees_with_is_not_a_change(sync, tmp_path):
    """A sync with nothing to do must leave every file untouched."""
    assignments = sync.schematic_assignments()
    path = write_sheet(
        tmp_path, [symbol(ref, number) for ref, number in assignments.items()]
    )
    plan = sync.plan_export([str(path)])
    assert plan.changes == []
    assert plan.has_work() is False


def test_a_deliberately_cleared_reference_is_exported_as_a_clear(sync, tmp_path):
    """The one thing ``schematic_cleared_refs`` exists to carry."""
    path = write_sheet(tmp_path, [symbol(UNASSIGNED, "C111")])
    plan = sync.plan_export([str(path)], cleared={UNASSIGNED})
    assert kinds(plan)[UNASSIGNED] == CLEAR
    assert plan.payload[UNASSIGNED] == ""


def test_a_merely_blank_reference_is_left_alone(sync, tmp_path):
    """The counterpart, and the reason the distinction is tracked at all.

    ``G1`` is blank on the board because nothing has ever assigned it — not
    because anybody removed a number. A schematic that has one for it is ahead
    of the board, and wiping it would destroy the only copy.
    """
    path = write_sheet(tmp_path, [symbol(UNASSIGNED, "C111")])
    plan = sync.plan_export([str(path)])
    assert UNASSIGNED not in kinds(plan)
    assert UNASSIGNED not in plan.payload


def test_an_assigned_reference_with_no_symbol_is_skipped_not_written(sync, tmp_path):
    """It can be reported and nothing else; there is no symbol to write to."""
    path = write_sheet(tmp_path, [symbol("R2", "C999")])
    plan = sync.plan_export([str(path)])
    assert kinds(plan)[ASSIGNED] == SKIP
    assert all(change.reference != ASSIGNED for change in plan.changes)


# ---------------------------------------------------------------------------
# To schematic — the write
# ---------------------------------------------------------------------------


def test_the_export_writes_the_number_and_keeps_a_backup(sync, tmp_path):
    """The file changes, and the sheet as it was survives as ``_old``."""
    path = write_sheet(tmp_path, [symbol(ASSIGNED, "C999")])
    plan = sync.plan_export([str(path)])
    result = sync.write(plan)

    assert result.changes == len(plan.changes)
    assert (tmp_path / f"{ROOT_SHEET}_old").is_file()
    assert f'"{number_of(sync.parts, ASSIGNED)}"' in path.read_text(encoding="utf-8")
    assert "C999" not in path.read_text(encoding="utf-8")


def test_a_sheet_open_in_the_editor_is_not_written_to(sync, tmp_path):
    """Eeschema holds the whole document; a write under it is lost on save."""
    path = write_sheet(tmp_path, [symbol(ASSIGNED, "C999")])
    (tmp_path / f"~{ROOT_SHEET}.lck").write_text("locked", encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    result = sync.write(sync.plan_export([str(path)]))
    assert result.skipped_locked == [str(path)]
    assert path.read_text(encoding="utf-8") == before


def test_writing_anyway_is_possible_when_the_user_insists(sync, tmp_path):
    """``skip_locked=False`` is what the "Write to the file anyway?" answer sets."""
    path = write_sheet(tmp_path, [symbol(ASSIGNED, "C999")])
    (tmp_path / f"~{ROOT_SHEET}.lck").write_text("locked", encoding="utf-8")

    result = sync.write(sync.plan_export([str(path)]), skip_locked=False)
    assert result.skipped_locked == []
    assert "C999" not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# From schematic
# ---------------------------------------------------------------------------


def test_the_import_preview_splits_additions_from_replacements(sync, tmp_path):
    """Additions are free; replacements destroy a number on the board."""
    path = write_sheet(tmp_path, [symbol(UNASSIGNED, "C111"), symbol(ASSIGNED, "C222")])
    plan = sync.plan_import([str(path)])
    seen = kinds(plan)
    assert seen[UNASSIGNED] == ADD
    assert seen[ASSIGNED] == REPLACE
    assert plan.payload == {UNASSIGNED: "C111", ASSIGNED: "C222"}


def test_a_symbol_with_no_footprint_cannot_be_imported(sync, tmp_path):
    """Reported, never applied — there is nothing on the board to write to."""
    path = write_sheet(tmp_path, [symbol("X99", "C111")])
    plan = sync.plan_import([str(path)])
    assert kinds(plan)["X99"] == SKIP
    assert plan.payload == {}


def test_a_schematic_that_agrees_with_the_board_has_no_work(sync, tmp_path):
    """The other half of "nothing to do", read the other way round."""
    path = write_sheet(tmp_path, [symbol(ASSIGNED, number_of(sync.parts, ASSIGNED))])
    plan = sync.plan_import([str(path)])
    assert plan.has_work() is False
    assert plan.read == [str(path)]


def test_an_unreadable_schematic_is_reported_rather_than_treated_as_empty(sync):
    """Tell "nothing could be read" apart from "read fine, nothing in it"."""
    plan = sync.plan_import(["/nowhere/at/all.kicad_sch"])
    assert plan.read == []
    assert plan.missing == ["/nowhere/at/all.kicad_sch"]


# ---------------------------------------------------------------------------
# The import writes through the funnel
# ---------------------------------------------------------------------------


def test_the_import_reaches_the_board_and_the_database(controller, tmp_path):
    """Both halves, because a database claiming what the board denies is a bug."""
    path = write_sheet(tmp_path, [symbol(UNASSIGNED, "C111")])
    plan = controller.schematic.plan_import([str(path)])
    controller._apply_schematic_numbers(plan.payload)

    assert controller.board.footprint(UNASSIGNED).lcsc == "C111"
    assert number_of(controller.parts, UNASSIGNED) == "C111"


def test_the_whole_import_is_one_undo_press(controller, tmp_path):
    """It is one thing the user did, however many numbers it carried."""
    path = write_sheet(
        tmp_path,
        [symbol(UNASSIGNED, "C111"), symbol("G2", "C222"), symbol("G3", "C333")],
    )
    plan = controller.schematic.plan_import([str(path)])
    assert len(plan.payload) == 3, "the fixture should offer three fresh numbers"

    controller._apply_schematic_numbers(plan.payload)
    assert len(controller.undo_stack) == 1

    controller.undo_last()
    assert controller.board.footprint(UNASSIGNED).lcsc == ""


def test_importing_does_not_leave_the_schematic_marked_out_of_date(
    controller, tmp_path
):
    """The two sides now agree about everything the import touched.

    ``assign_number`` sets the flag, correctly — it is the funnel and every
    other caller of it *has* diverged from the schematic. The import is the one
    caller that has just done the opposite.
    """
    path = write_sheet(tmp_path, [symbol(UNASSIGNED, "C111")])
    controller._apply_schematic_numbers(
        controller.schematic.plan_import([str(path)]).payload
    )
    assert controller.schematic_sync_pending is False


def test_an_import_does_not_clear_a_flag_set_before_it(controller, tmp_path):
    """Edits made before the import are still unexported after it."""
    controller.assign_number([ASSIGNED], "C777")
    assert controller.schematic_sync_pending is True

    path = write_sheet(tmp_path, [symbol(UNASSIGNED, "C111")])
    controller._apply_schematic_numbers(
        controller.schematic.plan_import([str(path)]).payload
    )
    assert controller.schematic_sync_pending is True


def _accept_everything(controller, monkeypatch) -> None:
    """Answer the confirmation with Yes and swallow the report.

    The confirmation is a real modal and ``exec()`` never returns without one,
    so an end-to-end test of the button has to answer it. Only the answering is
    faked: the plan, the file, the write and the flags are all the real ones.
    """
    monkeypatch.setattr(
        controller,
        "build_confirmation",
        lambda plan: type(
            "Accepted",
            (),
            {"exec": lambda self: SchematicSyncDialog.DialogCode.Accepted},
        )(),
    )
    monkeypatch.setattr(
        "lcsc_suite.controller.QMessageBox.information", lambda *a, **k: None
    )


def test_a_successful_export_settles_the_pending_flag(
    controller, tmp_path, monkeypatch
):
    """And empties the cleared set: the schematic now holds the removals too."""
    controller.assign_number([UNASSIGNED], "C111")
    controller.window.select_references([UNASSIGNED])
    controller.remove()
    assert controller.schematic_cleared_refs == {UNASSIGNED}
    assert controller.schematic_sync_pending is True

    write_sheet(tmp_path, [symbol(UNASSIGNED, "C111"), symbol(ASSIGNED, "C999")])
    _accept_everything(controller, monkeypatch)
    assert controller.export_to_schematic() is not None

    assert controller.schematic_sync_pending is False
    assert controller.schematic_cleared_refs == set()


def test_the_export_button_writes_the_file_it_previewed(
    controller, tmp_path, monkeypatch
):
    """End to end from the toolbar action, with only the Yes press faked."""
    path = write_sheet(tmp_path, [symbol(ASSIGNED, "C999")])
    _accept_everything(controller, monkeypatch)
    controller.window.export_schematic_action.trigger()

    text = path.read_text(encoding="utf-8")
    assert "C999" not in text
    assert f'"{number_of(controller.parts, ASSIGNED)}"' in text


def test_the_import_button_reaches_the_board(controller, tmp_path, monkeypatch):
    """The other toolbar action, end to end from the trigger."""
    write_sheet(tmp_path, [symbol(UNASSIGNED, "C111")])
    _accept_everything(controller, monkeypatch)
    controller.window.import_schematic_action.trigger()

    assert controller.board.footprint(UNASSIGNED).lcsc == "C111"
    assert number_of(controller.parts, UNASSIGNED) == "C111"


def test_a_cancelled_export_writes_nothing(controller, tmp_path, monkeypatch):
    """Cancel means cancel, and the pending flag stays up."""
    path = write_sheet(tmp_path, [symbol(ASSIGNED, "C999")])
    before = path.read_text(encoding="utf-8")
    controller.assign_number([UNASSIGNED], "C111")
    monkeypatch.setattr(
        controller,
        "build_confirmation",
        lambda plan: type(
            "Rejected",
            (),
            {"exec": lambda self: SchematicSyncDialog.DialogCode.Rejected},
        )(),
    )
    assert controller.export_to_schematic() is None
    assert path.read_text(encoding="utf-8") == before
    assert controller.schematic_sync_pending is True


# ---------------------------------------------------------------------------
# Nothing happens on its own
# ---------------------------------------------------------------------------


def test_assigning_a_number_writes_nothing_to_the_schematic(controller, tmp_path):
    """The rule the whole phase is built around, asserted rather than assumed."""
    path = write_sheet(tmp_path, [symbol(UNASSIGNED)])
    before = path.read_text(encoding="utf-8")

    controller.assign_number([UNASSIGNED], "C111")
    controller.window.reload_parts()

    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_path / f"{ROOT_SHEET}_old").exists()


def test_closing_with_nothing_pending_asks_nothing(controller, tmp_path, monkeypatch):
    """The offer is for unexported changes, not for every close."""
    write_sheet(tmp_path, [symbol(UNASSIGNED)])
    asked = []
    monkeypatch.setattr(
        "lcsc_suite.controller.QMessageBox.question",
        lambda *args, **kwargs: asked.append(args) or 0,
    )
    controller.schematic_sync_pending = False
    controller.offer_schematic_export()
    assert asked == []


def test_closing_with_no_schematic_asks_nothing(controller, monkeypatch):
    """There is nowhere to put them, so the question has no useful answer."""
    asked = []
    monkeypatch.setattr(
        "lcsc_suite.controller.QMessageBox.question",
        lambda *args, **kwargs: asked.append(args) or 0,
    )
    controller.schematic_sync_pending = True
    controller.offer_schematic_export()
    assert asked == []


def test_closing_on_unexported_changes_does_ask(controller, tmp_path, monkeypatch):
    """A removal lives on the footprint alone until it is exported."""
    write_sheet(tmp_path, [symbol(UNASSIGNED)])
    asked = []
    monkeypatch.setattr(
        "lcsc_suite.controller.QMessageBox.question",
        lambda *args, **kwargs: asked.append(args) or QMessageBox.StandardButton.No,
    )
    controller.schematic_sync_pending = True
    controller.offer_schematic_export()
    assert len(asked) == 1


def test_the_close_offer_is_reached_from_the_window(controller, tmp_path, monkeypatch):
    """``about_to_close`` is the connection, not a call the controller makes."""
    write_sheet(tmp_path, [symbol(UNASSIGNED)])
    offered = []
    monkeypatch.setattr(
        "lcsc_suite.controller.QMessageBox.question",
        lambda *args, **kwargs: offered.append(args) or QMessageBox.StandardButton.No,
    )
    controller.schematic_sync_pending = True
    controller.window.close()
    assert len(offered) == 1


# ---------------------------------------------------------------------------
# The warning dialog
# ---------------------------------------------------------------------------


def test_the_dialog_lists_every_change_rather_than_a_sample(controller, tmp_path):
    """The wx version showed eight and said "... and 23 more"; this shows all."""
    references = [row.reference for row in controller.window.part_model.rows()][:30]
    path = write_sheet(tmp_path, [symbol(reference, "C1") for reference in references])
    plan = controller.schematic.plan_export([str(path)])
    dialog = controller.build_confirmation(plan)

    table = dialog.findChild(QTableWidget, "schematic-changes")
    assert table.rowCount() == len(plan.rows())
    assert table.rowCount() > 8, "the sample cap is what this replaces"
    dialog.close()


def test_the_dialog_names_the_direction_it_would_write_in(controller, tmp_path):
    """Two buttons, two titles, two verbs — never one dialog doing both."""
    path = write_sheet(tmp_path, [symbol(ASSIGNED, "C999"), symbol(UNASSIGNED, "C111")])

    export = controller.build_confirmation(
        controller.schematic.plan_export([str(path)])
    )
    assert export.windowTitle() == "To schematic"
    assert export.go.text() == "Write to schematic"

    imported = controller.build_confirmation(
        controller.schematic.plan_import([str(path)])
    )
    assert imported.windowTitle() == "From schematic"
    assert imported.go.text() == "Update the board"
    export.close()
    imported.close()


def test_the_dialog_counts_replacements_first(controller, tmp_path):
    """The destructive category leads the summary line because it is the point."""
    path = write_sheet(tmp_path, [symbol(ASSIGNED, "C999"), symbol("R2")])
    dialog = controller.build_confirmation(
        controller.schematic.plan_export([str(path)])
    )
    assert dialog._counts_line().startswith("1 REPLACED")
    dialog.close()


def test_nothing_to_do_still_says_what_was_skipped(sync, tmp_path):
    """Say why nothing happened, which is the actionable half of it."""
    path = write_sheet(tmp_path, [symbol("X99", "C111")])
    plan = sync.plan_import([str(path)])
    message = nothing_to_do_message(plan)
    assert "X99" in message
    assert "no footprint on this board" in message


def test_the_dialog_is_a_real_dialog_with_a_cancel(controller, tmp_path):
    """A sync the user can back out of, right up to the last press."""
    path = write_sheet(tmp_path, [symbol(ASSIGNED, "C999")])
    dialog = controller.build_confirmation(
        controller.schematic.plan_export([str(path)])
    )
    assert isinstance(dialog, SchematicSyncDialog)
    assert dialog.isModal()
    dialog.reject()
    assert dialog.result() == SchematicSyncDialog.DialogCode.Rejected
