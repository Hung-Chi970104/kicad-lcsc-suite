"""What the buttons and the row menu actually do.

Phase 2 left one question open: where the dispatch for
``MainWindow.row_menu_triggered`` lives. This module is the answer, and the rule
it settles on is one line:

    **The window builds, displays and reports. The controller decides and
    writes.**

So the window owns its layout, its selection, its model and the *appearance* of
the row menu; everything that changes the board, the project database or the
mappings table goes through here. The alternative — the window growing a handler
per action — was already unattractive at 767 lines, and Phase 5 adds five
dialogs that all need the same three collaborators this object already holds.

Two consequences worth knowing:

* ``MainWindow`` is still constructible on its own (a test that only cares about
  layout should not have to build a controller), but the *app* and the *probe*
  both build one, because a screenshot of a window whose buttons are inert is
  not evidence about the app the user runs.
* Every write path here goes to the board first and the project database second.
  The bridge proves its writes by re-reading, so a refused write raises before
  the database has been told it succeeded. A database that disagrees with the
  board is how a BOM comes out wrong.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import Optional, Sequence

from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMessageBox

from .kicad_bridge import sanitize_lcsc
from .ui.assign_dialog import AssignNumberDialog, describe
from .ui.main_window import MainWindow
from .undo import UndoStack

log = logging.getLogger(__name__)

#: Row-menu entries this controller answers. The rest of
#: ``main_window.ROW_MENU`` is greyed out rather than hidden: the entries are
#: part of the wx plugin's menu and will be back in Phase 5, and a menu that
#: changes shape between releases is harder to relearn than one with a disabled
#: entry in it.
HANDLED_ROW_MENU = frozenset({"copy-lcsc", "paste-lcsc", "find-mapping", "add-mapping"})


def _exclusion_name(bom: bool, pos: bool) -> str:
    """Name the attributes a toggle touched, for the Undo tooltip."""
    if bom and pos:
        return "BOM and POS"
    return "BOM" if bom else "POS"


class SuiteController(QObject):
    """Owns the part list, the window, and the decisions between them."""

    def __init__(self, board, parts, settings=None, parent=None) -> None:
        super().__init__(parent)
        self.board = board
        self.parts = parts
        self.settings = settings
        self.window = MainWindow(board, settings=settings, parts=parts)
        #: References whose number this session *removed*. Phase 7's "To
        #: schematic" needs them: a reference merely blank in the store may be
        #: one the schematic has and the board never picked up, and exporting
        #: those two states alike would wipe numbers the user never touched.
        self.schematic_cleared_refs: set[str] = set()
        #: Set when an assignment changes and no schematic sync has run since.
        #: Phase 7 reads it; nothing here acts on it, because board<->schematic
        #: sync is never automatic.
        self.schematic_sync_pending = False
        #: Reversible actions, most recent last. See :mod:`lcsc_suite.undo` for
        #: why KiCad's own undo history is not enough on its own.
        self.undo_stack = UndoStack()
        # Set while a reversal is running, so the reverting writes do not record
        # reversals of their own and turn Undo into a toggle.
        self._reverting = False
        # Collects reversals while one user action performs several writes. See
        # _grouped.
        self._group: Optional[list] = None
        self._connect()

    # -- wiring -------------------------------------------------------------

    def _connect(self) -> None:
        """Connect every window action this object answers."""
        window = self.window
        window.set_row_menu_enabled(HANDLED_ROW_MENU)
        window.row_menu_triggered.connect(self.on_row_menu)

        window.undo_action.triggered.connect(self.undo_last)
        window.assign_action.triggered.connect(self.assign)
        window.remove_action.triggered.connect(self.remove)
        window.save_mappings_action.triggered.connect(self.save_mappings)
        window.toggle_bom_pos_action.triggered.connect(
            lambda: self.toggle_exclusions(bom=True, pos=True)
        )
        window.toggle_bom_action.triggered.connect(
            lambda: self.toggle_exclusions(bom=True)
        )
        window.toggle_pos_action.triggered.connect(
            lambda: self.toggle_exclusions(pos=True)
        )
        self._publish_undo()

    # -- undo ---------------------------------------------------------------

    def _record(self, description: str, revert) -> None:
        """Remember how to reverse an action that has already succeeded."""
        if self._reverting:
            # A reversal's own writes are not separately reversible; recording
            # them would make the button alternate between two states forever.
            return
        if self._group is not None:
            self._group.append((description, revert))
            return
        self.undo_stack.push(description, revert)
        self._publish_undo()

    @contextmanager
    def _grouped(self, description: str):
        """Record every write inside this block as a single reversal.

        ``Find mapping`` is one thing the user did and up to one commit per
        distinct number — the same reason the forward write batches by number.
        Without this, reversing it would take one Undo press per number, which
        is not the action anybody performed.
        """
        if self._reverting or self._group is not None:
            yield
            return
        self._group = []
        try:
            yield
        finally:
            collected, self._group = self._group, None
        if not collected:
            return
        # Reversed: undo the last write first, so nested changes to the same
        # reference come back in the order they were made.
        reverts = [revert for _, revert in reversed(collected)]

        def revert_all() -> None:
            for revert in reverts:
                revert()

        self.undo_stack.push(description, revert_all)
        self._publish_undo()

    def _publish_undo(self) -> None:
        """Tell the window what the Undo button would reverse, if anything."""
        self.window.set_undo_available(self.undo_stack.description)

    def undo_last(self, *_) -> None:
        """Reverse the most recent recorded action, board and database both.

        A reversal is a *new* verified write, not a rollback — see
        :mod:`lcsc_suite.undo`. If it fails the board is unchanged, so the entry
        goes back on the stack rather than being lost: the user's next move is to
        fix whatever refused the write and press Undo again.
        """
        entry = self.undo_stack.pop()
        if entry is None:
            return
        self._reverting = True
        try:
            entry.revert()
        except Exception as exc:  # noqa: BLE001 - report it, keep the window up
            log.exception("Could not reverse %s", entry.description)
            self.undo_stack.push(entry.description, entry.revert)
            self._failed_write(f"reverse {entry.description}", exc)
        else:
            log.info("Reversed: %s", entry.description)
        finally:
            self._reverting = False
            self._publish_undo()
            self.window.reload_parts()

    def _lcsc_reversal(self, state: dict, cleared: set):
        """Build the callable that puts an LCSC snapshot back.

        Grouped by ``(number, stock)`` so a batch that was one commit going
        forward is one commit coming back — twenty identical capacitors are one
        entry in KiCad's undo history either way.

        ``cleared`` restores ``schematic_cleared_refs`` as well, because a
        reference *deliberately cleared* and one merely blank are different to
        Phase 7 and that difference cannot be reconstructed later. An undo that
        left the set alone would quietly tell the schematic export to wipe a
        number the user had just got back.
        """
        references = list(state)

        def revert() -> None:
            by_value: dict = {}
            for reference, (number, stock) in state.items():
                by_value.setdefault((number, stock), []).append(reference)
            for (number, stock), refs in by_value.items():
                if number:
                    self.parts.assign(sorted(refs), number, stock=stock)
                else:
                    self.parts.clear(sorted(refs))
            self.schematic_cleared_refs.difference_update(references)
            self.schematic_cleared_refs.update(cleared)
            self.schematic_sync_pending = True

        return revert

    def _exclusion_reversal(self, state: dict, bom: bool, pos: bool):
        """Build the callable that puts a BOM/POS snapshot back.

        ``set_exclusions`` rather than ``toggle_exclusions``: a restore has a
        target state and toggling would flip whatever the board happens to say
        now, which after any other change is the wrong answer.
        """

        def revert() -> None:
            if bom:
                for wanted in (True, False):
                    refs = [ref for ref, (b, _) in state.items() if b is wanted]
                    if refs:
                        self.parts.set_exclusions(sorted(refs), bom=wanted)
            if pos:
                for wanted in (True, False):
                    refs = [ref for ref, (_, p) in state.items() if p is wanted]
                    if refs:
                        self.parts.set_exclusions(sorted(refs), pos=wanted)

        return revert

    # -- assignment ---------------------------------------------------------

    def assign(self, *_) -> None:
        """Ask for a number and put it on the selected footprints.

        Until Phase 4 this dialog is the only source of numbers; after it, the
        Explorer becomes a second caller of :meth:`assign_number` rather than a
        replacement for this one — someone who knows the number should not have
        to search for it.
        """
        references = self.window.selected_references()
        if not references:
            return
        dialog = AssignNumberDialog(
            self.window, references, current=self._shared_number(references)
        )
        if dialog.exec() != AssignNumberDialog.DialogCode.Accepted:
            return
        self.assign_number(references, dialog.number())

    def assign_number(self, references: Sequence[str], number: str, stock=None) -> None:
        """Assign one number to ``references`` and refresh the list.

        The single funnel every source of a number goes through — this dialog,
        ``Paste LCSC``, ``Find mapping``, and the Explorer in Phase 4. Keeping
        one funnel is what stops the store, the board and the schematic-cleared
        set drifting apart per entry point, which is how the wx plugin ended up
        with the same eight lines written four times.
        """
        if not references or not number:
            return
        # Read before the write: this is what Undo has to put back, and after the
        # write the old numbers and stock figures are gone from both halves.
        before = self.parts.lcsc_state(references)
        cleared = self.schematic_cleared_refs.intersection(references)
        try:
            written = self.parts.assign(references, number, stock=stock)
        except ValueError as exc:
            self._warn("Assign LCSC number", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - report it, keep the window up
            log.exception("Could not assign %s", number)
            self._failed_write(f"assign {number}", exc)
            return
        if not written:
            return
        self.schematic_cleared_refs.difference_update(written)
        self.schematic_sync_pending = True
        log.info("Assigned %s to %s", number, describe(written))
        self._record(
            f"assign {number} to {describe(written)}",
            self._lcsc_reversal(
                {ref: before[ref] for ref in written if ref in before},
                cleared.intersection(written),
            ),
        )
        self.window.reload_parts()

    def remove(self, *_) -> None:
        """Clear the LCSC number from the selected footprints."""
        references = self.window.selected_references()
        if not references:
            return
        # Which of them actually carried a number, read *before* the write. Only
        # a removal the user could see should propagate to the schematic:
        # clearing an already-blank row must not wipe a number the schematic has
        # and the board never picked up.
        selected = set(references)
        had_numbers = {
            view.reference
            for view in self.board.footprints()
            if view.reference in selected and view.lcsc
        }
        before = self.parts.lcsc_state(references)
        cleared = self.schematic_cleared_refs.intersection(references)
        try:
            written = self.parts.clear(references)
        except Exception as exc:  # noqa: BLE001 - report it, keep the window up
            log.exception("Could not remove the LCSC numbers")
            self._failed_write("remove the LCSC number", exc)
            return
        if not written:
            return
        self.schematic_cleared_refs.update(had_numbers.intersection(written))
        self.schematic_sync_pending = True
        log.info("Removed the LCSC number from %s", describe(written))
        self._record(
            f"remove the LCSC number from {describe(written)}",
            self._lcsc_reversal(
                {ref: before[ref] for ref in written if ref in before},
                cleared.intersection(written),
            ),
        )
        self.window.reload_parts()

    def _shared_number(self, references: Sequence[str]) -> str:
        """Return the number every one of ``references`` already carries.

        Pre-fills the dialog when a selection is already uniform, which is the
        common case for "assign the same thing to all of these, but a different
        one this time". A mixed selection pre-fills nothing rather than picking
        one arbitrarily.
        """
        wanted = set(references)
        numbers = {
            view.lcsc for view in self.board.footprints() if view.reference in wanted
        }
        return numbers.pop() if len(numbers) == 1 else ""

    # -- exclusions ---------------------------------------------------------

    def toggle_exclusions(self, bom: bool = False, pos: bool = False) -> None:
        """Flip BOM and/or POS exclusion on the selected rows."""
        references = self.window.selected_references()
        if not references:
            return
        before = self.parts.exclusion_state(references)
        try:
            self.parts.toggle_exclusions(references, bom=bom, pos=pos)
        except Exception as exc:  # noqa: BLE001 - report it, keep the window up
            log.exception("Could not change the BOM/POS exclusions")
            self._failed_write("change the BOM/POS attributes", exc)
        # Recorded even when the write raised, and deliberately: this helper
        # writes BOM and POS in two commits, so a failure on the second leaves
        # the first applied. Restoring to a state the board is already in is a
        # verified no-op; not being able to get back from a half-applied toggle
        # is not.
        if before:
            self._record(
                f"toggle {_exclusion_name(bom, pos)} on {describe(list(before))}",
                self._exclusion_reversal(before, bom=bom, pos=pos),
            )
        self.window.reload_parts()

    # -- mappings -----------------------------------------------------------

    def save_mappings(self, *_) -> None:
        """Remember every footprint+value -> LCSC assignment on this board."""
        written = self.parts.save_all_mappings()
        if written:
            log.info("Saved %d mapping%s", written, "" if written == 1 else "s")
        else:
            log.warning(
                "Nothing to save: no assigned part has both a value and a footprint"
            )

    def add_mapping(self, references: Sequence[str]) -> None:
        """Remember the mappings for just the selected rows."""
        written = self.parts.remember_mappings(references)
        log.info("Saved %d mapping%s", written, "" if written == 1 else "s")

    def find_mapping(self, references: Sequence[str]) -> None:
        """Assign each selected row the number remembered for its footprint+value.

        One write per distinct number rather than one per row: a selection of
        twenty identical capacitors is one board commit, not twenty, and KiCad's
        undo history reads the same way the user thinks about the action.
        """
        found = self.parts.mapped_numbers(references)
        if not found:
            log.info("No mapping remembered for %s", describe(list(references)))
            return
        by_number: dict = {}
        for reference, number in found.items():
            by_number.setdefault(number, []).append(reference)
        with self._grouped(f"assign remembered numbers to {describe(sorted(found))}"):
            for number, refs in by_number.items():
                self.assign_number(sorted(refs), number)

    # -- the row menu -------------------------------------------------------

    def on_row_menu(self, entry_id: str, references: list) -> None:
        """Dispatch one row-menu entry."""
        if entry_id == "copy-lcsc":
            self.copy_lcsc(references)
        elif entry_id == "paste-lcsc":
            self.paste_lcsc(references)
        elif entry_id == "add-mapping":
            self.add_mapping(references)
        elif entry_id == "find-mapping":
            self.find_mapping(references)
        else:
            # The three "Add correction ..." entries. They need Phase 5's
            # Corrections dialog; until then they are disabled in the menu and
            # this branch is only reachable by emitting the signal directly.
            log.debug("Row-menu entry %r is not handled yet", entry_id)

    def copy_lcsc(self, references: Sequence[str]) -> None:
        """Put the selected rows' LCSC numbers on the clipboard.

        Newline-separated and de-duplicated. The wx plugin loops and overwrites
        the clipboard once per selected row, so a multi-row copy silently keeps
        whichever row it visited last; that is a loop that was written for one
        row, not a decision. Paste still reads the first number it finds, so the
        single-row behaviour is unchanged.
        """
        wanted = set(references)
        numbers = []
        for view in self.board.footprints():
            if view.reference in wanted and view.lcsc and view.lcsc not in numbers:
                numbers.append(view.lcsc)
        if not numbers:
            log.info("Nothing to copy: none of those parts has an LCSC number")
            return
        QGuiApplication.clipboard().setText("\n".join(numbers))
        log.info("Copied %s", ", ".join(numbers))

    def paste_lcsc(self, references: Sequence[str]) -> None:
        """Assign the LCSC number on the clipboard to the selected rows."""
        number = sanitize_lcsc(QGuiApplication.clipboard().text())
        if not number:
            log.warning("The clipboard has no LCSC number in it")
            return
        self.assign_number(references, number)

    # -- reporting ----------------------------------------------------------

    def _warn(self, title: str, text: str) -> None:
        """Show a warning that is the user's to fix."""
        QMessageBox.warning(self.window, title, text)

    def _failed_write(self, what: str, exc: Exception) -> None:
        """Report a write KiCad refused.

        Named for what was attempted rather than for the exception, because the
        one thing the user needs to know is that the board was *not* changed —
        the bridge drops the commit when its read-back disagrees.
        """
        QMessageBox.critical(
            self.window,
            "LCSC Suite",
            f"KiCad did not accept the request to {what}.\n\n"
            f"{exc}\n\nThe board has been left as it was; see the log for details.",
        )


def build(board, parts, settings=None) -> SuiteController:
    """Build the controller and its window. The app's one assembly point."""
    return SuiteController(board, parts, settings=settings)


__all__ = ["HANDLED_ROW_MENU", "SuiteController", "build"]
