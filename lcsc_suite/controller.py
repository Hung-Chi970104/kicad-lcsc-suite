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
import os
from typing import Optional, Sequence

from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFileDialog, QMessageBox

from . import APP_NAME
from .export import Exporter, ExportResult
from .kicad_bridge import sanitize_lcsc
from .schematic import SchematicSync, SyncPlan, basenames, lock_files_for
from .search_source import build_source
from .shared import fab_rules, highlight_terms
from .ui.assign_dialog import AssignNumberDialog, describe
from .ui.bom_estimator import BomEstimator, help_text as estimator_help_text
from .ui.corrections_dialog import (
    CorrectionsDialog,
    pattern_for_name,
    pattern_for_package,
    pattern_for_reference,
)
from .ui.explorer import ExplorerWindow
from .ui.main_window import MainWindow
from .ui.mappings_dialog import MappingsDialog
from .ui.part_details_dialog import PartDetailsDialog
from .ui.schematic_dialog import SchematicSyncDialog, nothing_to_do_message
from .ui.settings_dialog import SettingsDialog
from .undo import UndoStack

log = logging.getLogger(__name__)

#: How many trailing path components the report spells out.
#:
#: ``QMessageBox`` sizes itself to its informative text, so whatever goes on
#: this line decides how wide the dialog is. An absolute path made that a
#: property of where the user keeps their projects: the committed
#: ``export-summary.png`` is 408px and re-rendering it gave 411px, purely
#: because the probe writes to a temporary directory whose name changes per
#: run. A deeply-nested real project made it wider still, and a Windows path
#: wider again — so the one screen that reports a filesystem path could never
#: have passed a cross-platform comparison.
#:
#: Three components is ``<project>/jlcpcb/production_files``, which is the
#: whole of the answer: the export always lands in ``jlcpcb/production_files``
#: under the project, so the absolute prefix carries no information the user
#: does not already have. The full path is in the details pane for the times it
#: does — see ``build_export_report``.
PATH_COMPONENTS = 3


def export_location(directory: str) -> str:
    """Render *directory* as its last few components, for the report line.

    Returned with a leading ``…/`` when anything was dropped, so the line is
    never mistaken for an absolute path. The result's length is bounded by the
    project directory's own name rather than by its depth, which is what makes
    the dialog the same size on every machine.
    """
    parts = directory.replace("\\", "/").rstrip("/").split("/")
    kept = [part for part in parts[-PATH_COMPONENTS:] if part]
    if len(parts) > len(kept):
        return "…/" + "/".join(kept)
    return "/".join(kept)


#: Row-menu entries this controller answers. Every one of them, since Phase 5
#: brought the Corrections dialog the three ``Add correction …`` entries were
#: waiting for. ``MainWindow.set_row_menu_enabled`` still takes the set rather
#: than being deleted: it is what keeps a half-built entry visibly disabled
#: instead of quietly doing nothing when clicked.
HANDLED_ROW_MENU = frozenset(
    {
        "enter-lcsc",
        "copy-lcsc",
        "paste-lcsc",
        "find-mapping",
        "add-mapping",
        "correction-by-reference",
        "correction-by-package",
        "correction-by-name",
    }
)

#: Row-menu entry -> how to turn the row into a correction pattern. By
#: reference is anchored at both ends (one designator), by package only at the
#: start (a footprint and its variants), by name not at all (a value appearing
#: anywhere in the name). The wx plugin's three spellings, kept exactly: a
#: pattern that matches more than it did would silently rotate parts nobody
#: asked about.
CORRECTION_PATTERNS = {
    "correction-by-reference": ("reference", pattern_for_reference),
    "correction-by-package": ("footprint", pattern_for_package),
    "correction-by-name": ("value", pattern_for_name),
}


def _exclusion_name(bom: bool, pos: bool) -> str:
    """Name the attributes a toggle touched, for the Undo tooltip."""
    if bom and pos:
        return "BOM and POS"
    return "BOM" if bom else "POS"


class SuiteController(QObject):
    """Owns the part list, the window, and the decisions between them."""

    def __init__(self, board, parts, settings=None, source=None, parent=None) -> None:
        super().__init__(parent)
        self.board = board
        self.parts = parts
        self.settings = settings
        #: Where the Explorer's data comes from. Injected so the probe and the
        #: tests can pass a fixture — see :mod:`lcsc_suite.search_source`. Built
        #: lazily rather than here, because a session that never opens the
        #: Explorer should not pay for it.
        self._source = source
        self.window = MainWindow(board, settings=settings, parts=parts)
        #: The one Explorer, kept so the toolbar button re-targets it rather
        #: than opening a second. Two of them would each hold a search, a fill
        #: and a photo window, and only one can win the assign.
        self.explorer: Optional[ExplorerWindow] = None
        #: The Part Details window, likewise one at a time. It is opened *from*
        #: a selection and describes a part already on the board, so a second
        #: one is a stale copy of the same answer rather than a comparison.
        self.part_details: Optional[PartDetailsDialog] = None
        #: The cost estimator. Built here rather than by the window because it
        #: reads the project database and the part cache, which are the
        #: controller's collaborators, not the window's.
        #:
        #: It is handed the *injected* source, never ``self.source()``. The
        #: lazy accessor would build a ``LiveSource`` for a caller that passed
        #: nothing — which is every existing test — and the estimator's
        #: enrichment pass makes requests, so omitting a source has to mean "no
        #: network", not "the default one". ``__main__`` passes one explicitly
        #: for that reason.
        self.estimator = (
            BomEstimator(self.window, parts, source=source)
            if parts is not None
            else None
        )
        #: References whose number this session *removed*. "To schematic" needs
        #: them: a reference merely blank in the store may be one the schematic
        #: has and the board never picked up, and exporting those two states
        #: alike would wipe numbers the user never touched.
        self.schematic_cleared_refs: set[str] = set()
        #: Set when an assignment changes and no schematic sync has run since.
        #: Read on the way out, and *only* to ask — board<->schematic sync is
        #: never automatic. See :meth:`offer_schematic_export`.
        self.schematic_sync_pending = False
        #: Where the two directions get their files, their assignments and
        #: their previews. See :mod:`lcsc_suite.schematic`.
        self.schematic = SchematicSync(board, parts) if parts is not None else None
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
        window.export_action.triggered.connect(self.export_bom_cpl)
        window.explorer_action.triggered.connect(self.open_explorer)
        # Both of the gestures that mean "find a part for this footprint" go to
        # the Explorer with the row's own search already run, which is what the
        # wx plugin's ``select_part`` does and the one thing a dialog asking for
        # a number cannot: nobody knows an LCSC number by heart. Typing a known
        # one moved to the row menu — see ``enter-lcsc`` in ``on_row_menu``.
        window.assign_action.triggered.connect(lambda: self.open_explorer(search=True))
        window.part_activated.connect(lambda _refs: self.open_explorer(search=True))
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

        # Phase 7's two, and they stay two. Each writes to a place the other
        # half of the toolchain owns, so neither is ever run for the user.
        window.export_schematic_action.triggered.connect(self.export_to_schematic)
        window.import_schematic_action.triggered.connect(self.import_from_schematic)
        window.about_to_close.connect(self.offer_schematic_export)

        # Phase 5's five windows.
        window.settings_action.triggered.connect(self.open_settings)
        window.corrections_action.triggered.connect(self.open_corrections)
        window.mappings_action.triggered.connect(self.open_mappings)
        window.part_details_action.triggered.connect(self.open_part_details)
        window.estimator_help.clicked.connect(self.show_estimator_help)

        if self.estimator is not None:
            # One connection rather than a call after each of the six reload
            # sites — see MainWindow.parts_reloaded.
            window.parts_reloaded.connect(self.recompute_estimate)
            window.board_count_changed.connect(lambda _count: self.recompute_estimate())
            window.assembly_count_changed.connect(
                lambda _count: self.recompute_estimate()
            )
            window.force_standard.toggled.connect(
                lambda _checked: self.recompute_estimate()
            )
            # The window's own start-up reload happened before this object
            # existed, so its signal went nowhere. Catch up once.
            self.recompute_estimate()

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

    def assign(self, references=None, *_) -> None:
        """Ask for a number and put it on the selected footprints.

        The "I already know the number" route, reached from the row menu's
        ``Enter LCSC number…``. It is not the *main* route and has not been
        since Phase 4 landed the Explorer: the toolbar button and a double-click
        both search the catalogue now, because knowing an LCSC number by heart
        is not a thing anyone does. This stays because pasting one from a
        datasheet or an email is, and because ``Paste LCSC`` only reaches the
        clipboard.
        """
        references = (
            list(references) if references else self.window.selected_references()
        )
        if not references:
            return
        dialog = AssignNumberDialog(
            self.window, references, current=self._shared_number(references)
        )
        if dialog.exec() != AssignNumberDialog.DialogCode.Accepted:
            return
        self.assign_number(references, dialog.number())

    def assign_number(
        self,
        references: Sequence[str],
        number: str,
        stock=None,
        reload: bool = True,
    ) -> list:
        """Assign one number to ``references`` and refresh the list.

        The single funnel every source of a number goes through — this dialog,
        ``Paste LCSC``, ``Find mapping``, the Explorer in Phase 4 and the
        schematic import in Phase 7. Keeping one funnel is what stops the store,
        the board and the schematic-cleared set drifting apart per entry point,
        which is how the wx plugin ended up with the same eight lines written
        four times.

        ``reload=False`` is for a caller making many of these in a row: the list
        rebuild costs a full board read, and a schematic import of sixty
        distinct numbers would otherwise pay for it sixty times to show the same
        final state. Such a caller reloads once when it is done.

        Returns the references actually written, which is the subset that exists
        on the board.
        """
        if not references or not number:
            return []
        # Read before the write: this is what Undo has to put back, and after the
        # write the old numbers and stock figures are gone from both halves.
        before = self.parts.lcsc_state(references)
        cleared = self.schematic_cleared_refs.intersection(references)
        try:
            written = self.parts.assign(references, number, stock=stock)
        except ValueError as exc:
            self._warn("Assign LCSC number", str(exc))
            return []
        except Exception as exc:  # noqa: BLE001 - report it, keep the window up
            log.exception("Could not assign %s", number)
            self._failed_write(f"assign {number}", exc)
            return []
        if not written:
            return []
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
        if reload:
            self.window.reload_parts()
        return written

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

    # -- the Explorer -------------------------------------------------------

    def source(self):
        """Return the Explorer's data source, building it on first use.

        Live unless one was injected. Deferred because constructing the fixture
        source reads a megabyte of captured payloads, and a session that only
        assigns numbers by hand should not pay for that.
        """
        if self._source is None:
            self._source = build_source()
        return self._source

    def open_explorer(self, *_, search: bool = False) -> None:
        """Open the Explorer over the current selection, or re-target it.

        Re-targeted rather than reopened, for the same reason the app takes a
        single-instance lock on the main window: two Explorers would each hold a
        search, two background fills and a photo window, and both would be
        writing to the same board through this object.

        ``search`` runs the seeded keyword rather than just filling the box. It
        is set by the gestures that name a part — double-clicking a row, or
        ``Assign LCSC number`` over a selection — and left alone by the toolbar
        icon, which means "open the catalogue" and should not throw away a search
        the window is already showing.
        """
        references = self.window.selected_references()
        keyword = self.search_keyword(references)
        if self.explorer is not None:
            self.explorer.set_references(references)
            if search and keyword:
                self.explorer.search_for(keyword)
            self.explorer.show()
            self.explorer.raise_()
            self.explorer.activateWindow()
            return
        self.explorer = self.build_explorer(references)
        self.explorer.show()
        if search and keyword:
            self.explorer.search_for(keyword)

    def build_explorer(self, references=None, keyword: str = "") -> ExplorerWindow:
        """Construct the Explorer window and connect it. Also the probe's entry.

        ``keyword`` defaults to the value the selected footprints agree on, the
        way the wx plugin seeds its search from the part being replaced. A mixed
        selection seeds nothing rather than picking one arbitrarily.
        """
        references = list(references or [])
        info = self.board.info()
        explorer = ExplorerWindow(
            self.window,
            self.source(),
            settings=self.settings,
            references=references,
            keyword=keyword or self.search_keyword(references),
            board_path=info.path,
        )
        explorer.assign_requested.connect(
            lambda number, stock: self.assign_number(
                explorer.references, number, stock=stock
            )
        )
        explorer.finished.connect(self._on_explorer_closed)
        return explorer

    def _on_explorer_closed(self, *_) -> None:
        """Forget the closed Explorer so the next click builds a fresh one."""
        self.explorer = None

    def search_keyword(self, references) -> str:
        """Return the catalogue search ``references`` describes, or ``""``.

        The wx plugin's ``select_part`` rule, which is the one worth keeping:
        the value alone is a poor search — "1uF" matches fifteen thousand parts
        — so the package goes on the end, and a resistor's value gets the ohm
        sign the catalogue spells it with. "1uF 0805" is a search; "1uF" is a
        catalogue.

        A mixed selection seeds nothing rather than picking one arbitrarily:
        assigning one number to twenty footprints is a deliberate act, and
        searching for whichever of them the board listed first is not a guess
        worth making on the user's behalf.
        """
        wanted = set(references)
        views = [view for view in self.board.footprints() if view.reference in wanted]
        keywords = {self._keyword_for(view) for view in views}
        keywords.discard("")
        return keywords.pop() if len(keywords) == 1 else ""

    @staticmethod
    def _keyword_for(view) -> str:
        """Describe one footprint as a catalogue search."""
        value = (view.value or "").strip()
        if not value:
            return ""
        if view.reference.startswith("R"):
            # 390R / 390r / 390o are all how a schematic spells 390Ω when the
            # symbol editor will not take the character. The catalogue spells it
            # the one way.
            if value[-1] in "Rro":
                value = value[:-1]
            value += "Ω"
        package = highlight_terms.simplify_footprint_name(view.footprint or "")
        return f"{value} {package}".strip() if package else value

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
        if entry_id == "enter-lcsc":
            self.assign(references)
        elif entry_id == "copy-lcsc":
            self.copy_lcsc(references)
        elif entry_id == "paste-lcsc":
            self.paste_lcsc(references)
        elif entry_id == "add-mapping":
            self.add_mapping(references)
        elif entry_id == "find-mapping":
            self.find_mapping(references)
        elif entry_id in CORRECTION_PATTERNS:
            self.add_correction(entry_id, references)
        else:
            log.debug("Row-menu entry %r is not handled", entry_id)

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

    # -- Phase 5's windows --------------------------------------------------
    #
    # Each pair is ``build_x`` plus ``open_x``, the shape ``build_explorer`` /
    # ``open_explorer`` set in Phase 4. The build half constructs and connects
    # and is what ``qt_probe.py`` and the tests call; the open half runs it
    # modally, which a probe cannot do because ``exec()`` would never return.

    def build_settings_dialog(self) -> SettingsDialog:
        """Construct the Settings dialog and connect its change signal."""
        dialog = SettingsDialog(self.window, settings=self.settings)
        dialog.changed.connect(self.apply_setting)
        return dialog

    def open_settings(self, *_) -> None:
        """Show the Settings dialog."""
        self.build_settings_dialog().exec()

    def apply_setting(self, section: str, key: str, value) -> None:
        """Make a settings change take effect on the window already open.

        Settings persist themselves; what needs doing here is the part the file
        cannot do. Three of these repaint, one rebuilds the list and two reopen
        the databases — and the list is exhaustive on purpose, because a setting
        that only applies at the next start-up looks broken from inside a dialog
        that has just been told it is on.
        """
        window = self.window
        if (section, key) == ("highlighting", "matches"):
            window.params_delegate.set_enabled(bool(value))
            # The delegate holds the flag but does not repaint on its own, and
            # the model has not changed, so nothing else will ask it to.
            window.part_table.viewport().update()
        elif (section, key) == ("general", "highlight_standard_parts"):
            window.part_model.set_standard_trigger_highlighting_enabled(bool(value))
        elif (section, key) == ("general", "bom_estimator_show"):
            window.set_estimator_visible(bool(value))
        elif (section, key) == ("general", "lcsc_priority"):
            # Which of the board and the database wins is applied during
            # reconciliation, so it changes nothing until the next one.
            window.reload_parts()
        elif section == "library" and key in ("selected_library", "data_path"):
            if self.parts is not None:
                self.parts.open_libraries()
            window.reload_parts()

    def build_mappings_dialog(self) -> MappingsDialog:
        """Construct the Mappings Manager over the shared mappings table."""
        library = self.parts.library if self.parts is not None else None
        return MappingsDialog(self.window, library=library)

    def open_mappings(self, *_) -> None:
        """Show the Mappings Manager."""
        self.build_mappings_dialog().exec()

    def build_corrections_dialog(
        self, pattern: str = "", allow_network: bool = True
    ) -> CorrectionsDialog:
        """Construct the Corrections Manager, optionally seeded with a pattern."""
        library = self.parts.library if self.parts is not None else None
        dialog = CorrectionsDialog(
            self.window,
            library=library,
            pattern=pattern,
            allow_network=allow_network,
        )
        # A correction changes what the CPL will say about parts already on the
        # board, so the list is rebuilt — the same thing the wx dialog's
        # PopulateFootprintListEvent does.
        dialog.corrections_changed.connect(self.window.reload_parts)
        return dialog

    def open_corrections(self, *_) -> None:
        """Show the Corrections Manager."""
        self.build_corrections_dialog().exec()

    def add_correction(self, entry_id: str, references: Sequence[str]) -> None:
        """Open Corrections seeded with a pattern derived from the selected row.

        One dialog, from the first selected row. The wx plugin loops over the
        whole selection and opens a *modal* dialog per row, so twenty selected
        capacitors are twenty dialogs in a queue — a loop written for one row,
        like the clipboard one Phase 3 found next to it.
        """
        field, build_pattern = CORRECTION_PATTERNS[entry_id]
        seed = self._first_field(references, field)
        if not seed:
            log.info("No %s on the selected rows to build a correction from", field)
            return
        self.open_corrections_with(build_pattern(seed))

    def open_corrections_with(self, pattern: str) -> None:
        """Show the Corrections Manager with ``pattern`` in the Regex field."""
        self.build_corrections_dialog(pattern=pattern).exec()

    def _first_field(self, references: Sequence[str], field: str) -> str:
        """Return ``field`` off the first selected footprint that has one."""
        wanted = list(references)
        by_reference = {view.reference: view for view in self.board.footprints()}
        for reference in wanted:
            view = by_reference.get(reference)
            if view is None:
                continue
            value = reference if field == "reference" else getattr(view, field, "")
            if value:
                return str(value)
        return ""

    def build_part_details_dialog(self, number: str, references) -> PartDetailsDialog:
        """Construct the Part Details window for one LCSC number."""
        info = self.board.info()
        return PartDetailsDialog(
            self.window,
            source=self.source(),
            lcsc=number,
            references=list(references),
            project_path=info.project_path,
        )

    def open_part_details(self, *_) -> None:
        """Show the details of the selected part.

        One number, because the dialog describes one part. A selection whose
        rows carry different numbers uses the first assigned one and says so —
        better than opening four windows or silently picking without a word.
        """
        references = self.window.selected_references()
        if not references:
            return
        numbers = self._numbers_for(references)
        if not numbers:
            log.info("None of the selected parts has an LCSC number")
            return
        number = next(iter(numbers))
        if len(numbers) > 1:
            log.info(
                "The selection covers %d different numbers; showing %s",
                len(numbers),
                number,
            )
        # Modeless, unlike the other four: it is a reference window you keep
        # open while working in the list. Replacing the previous one keeps it
        # from becoming a pile of stale copies.
        previous, self.part_details = self.part_details, None
        if previous is not None:
            previous.close()
        self.part_details = self.build_part_details_dialog(number, numbers[number])
        self.part_details.finished.connect(self._on_part_details_closed)
        self.part_details.show()

    def _numbers_for(self, references: Sequence[str]) -> dict:
        """Return ``{number: [references]}`` for the assigned rows, in view order."""
        wanted = list(references)
        by_reference = {view.reference: view for view in self.board.footprints()}
        found: dict = {}
        for reference in wanted:
            view = by_reference.get(reference)
            if view is not None and view.lcsc:
                found.setdefault(view.lcsc, []).append(reference)
        return found

    def _on_part_details_closed(self, *_) -> None:
        """Forget the closed details window."""
        self.part_details = None

    # -- the cost estimator --------------------------------------------------

    def recompute_estimate(self, *_) -> None:
        """Recalculate the BOM estimate and repaint what it drives.

        Guarded rather than allowed to raise: this runs after every list
        rebuild, so a bad price ladder in the cache would otherwise make the
        window unusable rather than costing one wrong summary line.

        The enrichment pass follows, and is deliberately not conditional on the
        recompute having produced anything: a board with no assembly metadata
        yet is precisely the one that needs the lookup.
        """
        if self.estimator is None:
            return
        try:
            self.estimator.recompute()
        except Exception:  # noqa: BLE001 - a wrong estimate is not a dead window
            log.exception("Could not recompute the BOM estimate")
        try:
            self.estimator.enrich()
        except Exception:  # noqa: BLE001 - likewise; the next reload retries
            log.exception("Could not start the assembly metadata lookup")

    def show_estimator_help(self, *_) -> None:
        """Show the estimator's assumptions and limitations."""
        title, body = estimator_help_text()
        QMessageBox.information(self.window, title, body)

    # -- export -------------------------------------------------------------

    def exporter(self) -> Exporter:
        """Build the exporter over this session's board, store and corrections."""
        return Exporter(
            self.board,
            self.parts.store,
            library=self.parts.library,
            settings=self.settings,
        )

    def run_export(self) -> Optional[ExportResult]:
        """Write both files and log the outcome. No dialogs — see below."""
        try:
            result = self.exporter().export()
        except OSError as exc:
            log.exception("Could not write the BOM and CPL")
            return exc  # type: ignore[return-value]
        log.info(
            "Wrote %s (%d rows) and %s (%d rows)",
            os.path.basename(result.bom_path),
            result.bom_rows,
            os.path.basename(result.cpl_path),
            result.cpl_rows,
        )
        return result

    def export_bom_cpl(self, *_) -> Optional[ExportResult]:
        """Write the BOM and the CPL, after asking about anything implausible.

        The plausibility check comes first and can cancel the whole thing,
        because one LCSC number against two different values is nearly always a
        mistake and it is *much* cheaper to find here than on a reel.

        Split into ``run_export`` plus ``build_export_report`` for the reason
        Phase 5 split every other dialog: ``exec()`` never returns to a probe or
        a test, so the half that *does* the work has to be callable without it.
        """
        warnings = fab_rules.consistency_warnings(
            list(self.parts.store.read_bom_parts())
        )
        if warnings and not self._confirm_inconsistent(warnings):
            log.info("Export cancelled at the plausibility check")
            return None
        result = self.run_export()
        if isinstance(result, OSError):
            QMessageBox.critical(
                self.window,
                "Export BOM / CPL",
                f"Neither file was written.\n\n{result}",
            )
            return None
        self.build_export_report(result).exec()
        return result

    def _confirm_inconsistent(self, warnings: str) -> bool:
        """Ask whether to export anyway when one number carries two values."""
        answer = QMessageBox.warning(
            self.window,
            "Plausibility check",
            "These LCSC numbers are used for more than one value:\n\n"
            f"{warnings}\nExport anyway?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Ok

    def build_export_report(self, result) -> QMessageBox:
        """Say what was written and, as importantly, what was left out."""
        directory = os.path.dirname(result.bom_path)
        lines = [
            f"BOM: {result.bom_rows} rows",
            f"CPL: {result.cpl_rows} rows",
            "",
            export_location(directory),
        ]
        # The counts are the answer to "why is my BOM shorter than my board",
        # which is the first question anyone asks of a file like this. The wx
        # plugin answers it only in a log pane nobody scrolls back through.
        omitted = []
        if result.skipped_dnp:
            omitted.append(f"{len(set(result.skipped_dnp))} marked do-not-place")
        if result.skipped_no_lcsc:
            omitted.append(f"{len(set(result.skipped_no_lcsc))} with no LCSC number")
        if result.skipped_no_position:
            omitted.append(
                f"{len(set(result.skipped_no_position))} not in the project database"
            )
        if omitted:
            lines += ["", "Left out: " + ", ".join(omitted) + "."]
        box = QMessageBox(self.window)
        box.setWindowTitle("Export BOM / CPL")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("Wrote BOM and CPL.")
        box.setInformativeText("\n".join(lines))
        # The elided path above says roughly where; this says exactly where, and
        # is selectable, which is what somebody about to open a file terminal
        # actually needs. Always present, so "Show Details…" is never a button
        # that appears only when something went wrong.
        details = [f"Written to:\n{directory}"]
        if result.warnings:
            details.append(
                "These LCSC numbers are used for more than one value:\n\n"
                + result.warnings
            )
        box.setDetailedText("\n\n".join(details))
        return box

    # -- board <-> schematic (Phase 7) --------------------------------------
    #
    # Two buttons, and the rule they exist for: **sync is never automatic.**
    # Both directions overwrite a field in a file the other half of the
    # toolchain owns, and the side being overwritten may be the only place a
    # number exists. Nothing here is called from the assignment path, from a
    # timer or from a reload; each method starts at a button and shows what it
    # would destroy before destroying it.

    def export_to_schematic(self, *_) -> Optional[SyncPlan]:
        """Write the assigned LCSC numbers into the schematic symbols.

        Without this the numbers live only on the footprints, where the Symbol
        Fields Table cannot see them and where the next "Update PCB from
        Schematic" overwrites them.
        """
        if self.schematic is None:
            return None
        paths = self._schematic_paths("To schematic")
        if not paths:
            return None
        try:
            plan = self.schematic.plan_export(paths, self.schematic_cleared_refs)
        except OSError as exc:
            log.exception("Could not read the schematic")
            self._warn("To schematic", f"The schematic could not be read:\n\n{exc}")
            return None

        skip_locked = True
        if plan.locked and not self._confirm_write_under_editor(plan):
            return None
        if plan.locked:
            skip_locked = False
        if not self._confirm(plan):
            return None

        try:
            result = self.schematic.write(plan, skip_locked=skip_locked)
        except Exception as exc:  # noqa: BLE001 - a failed sync must not kill the window
            log.exception("Writing LCSC numbers to the schematic failed")
            self._warn("To schematic", f"The schematic could not be updated:\n\n{exc}")
            return None

        if not result.skipped_locked:
            # Only now: until the numbers are in the file, a removal this
            # session made exists nowhere else, and forgetting it would leave
            # the schematic holding a number the user deliberately took off.
            self.schematic_cleared_refs.clear()
            self.schematic_sync_pending = False
        log.info(result.summary())
        QMessageBox.information(self.window, "To schematic", result.summary())
        return plan

    def import_from_schematic(self, *_) -> Optional[SyncPlan]:
        """Copy the LCSC numbers in the schematic symbols onto the board.

        The direction that is routinely the useful one. A schematic is often
        ahead of the board — fields filled in eeschema, or a design that shipped
        with them — and a plugin that only reads footprints reports every one of
        those boards as completely unassigned.
        """
        if self.schematic is None:
            return None
        paths = self._schematic_paths("From schematic")
        if not paths:
            return None
        try:
            plan = self.schematic.plan_import(paths)
        except OSError as exc:
            log.exception("Reading LCSC numbers from the schematic failed")
            self._warn("From schematic", f"The schematic could not be read:\n\n{exc}")
            return None

        if not plan.read:
            self._warn(
                "From schematic",
                "No schematic could be read:\n\n" + basenames(plan.missing or paths),
            )
            return None
        if not self._confirm(plan):
            return None

        written = self._apply_schematic_numbers(plan.payload)
        QMessageBox.information(
            self.window,
            "From schematic",
            f"{len(written)} footprint(s) updated from the schematic.\n\n"
            "Save the PCB in KiCad to keep the change.",
        )
        return plan

    def _apply_schematic_numbers(self, numbers: dict) -> list:
        """Write imported numbers through the funnel, one commit per number.

        ``assign_number`` rather than a write of its own: it is the single place
        the board, the project database, the cleared set and the undo history
        are kept in step, and a second write path here is exactly how the wx
        plugin ended up with the same eight lines in four places. Grouped so the
        whole import is one Undo press — it is one thing the user did — and one
        list rebuild, not one per distinct number.
        """
        # Restored afterwards, because an import does not create a divergence:
        # the two sides now agree about everything it touched. What it must not
        # do is *clear* a flag set by edits made before it, which is why this is
        # a restore rather than a `= False`.
        pending = self.schematic_sync_pending
        by_number: dict = {}
        for reference, number in numbers.items():
            by_number.setdefault(number, []).append(reference)
        written: list = []
        with self._grouped(f"import {len(numbers)} number(s) from the schematic"):
            for number, references in sorted(by_number.items()):
                written.extend(
                    self.assign_number(sorted(references), number, reload=False)
                )
        self.schematic_sync_pending = pending
        self.window.reload_parts()
        log.info("Imported %d LCSC number(s) from the schematic", len(written))
        return written

    def _schematic_paths(self, title: str) -> list:
        """Return the sheets to act on, asking the user only when unclear.

        One path is normally the whole hierarchy — the sub-sheets are found by
        following the root's ``Sheetfile`` properties — so the file dialog only
        appears for a project whose root cannot be identified at all.
        """
        paths = self.schematic.default_paths()
        if paths:
            return paths
        info = self.board.info()
        chosen, _filter = QFileDialog.getOpenFileNames(
            self.window,
            f"{title}: choose the schematic",
            info.project_path,
            "KiCad schematics (*.kicad_sch)",
        )
        return list(chosen)

    def build_confirmation(self, plan: SyncPlan) -> SchematicSyncDialog:
        """Build the dialog that shows what ``plan`` would overwrite.

        Split from :meth:`_confirm` for the reason Phase 5 split every other
        dialog and Phase 6 split ``build_export_report``: ``exec()`` never
        returns to a probe or a test, so the half that *builds* the warning has
        to be reachable without it. This warning is the entire visible half of
        Phase 7, and a dialog that can only be reached through a modal is one
        that can never be screenshotted.
        """
        return SchematicSyncDialog(self.window, plan)

    def _confirm(self, plan: SyncPlan) -> bool:
        """Show what ``plan`` would overwrite and ask whether to go ahead."""
        log.info(plan.summary())
        if not plan.has_work():
            QMessageBox.information(
                self.window, plan.title, nothing_to_do_message(plan)
            )
            return False
        return (
            self.build_confirmation(plan).exec()
            == SchematicSyncDialog.DialogCode.Accepted
        )

    def _confirm_write_under_editor(self, plan: SyncPlan) -> bool:
        """Ask before writing to a sheet the Schematic Editor has open.

        Its own question rather than a line in the main confirmation, because
        the answer is usually "close eeschema and press it again": the editor
        holds the whole document in memory and saving there throws away
        everything written underneath it.

        The lock file is named because the evidence is a file, not a window.
        Leftovers from a crashed KiCad are recognised and ignored now
        (:mod:`kicad_locks`), but a lock this session could have written and a
        lock eeschema is really holding look identical, and a user who has no
        Schematic Editor on screen needs to be told what to go and look at.
        """
        answer = QMessageBox.warning(
            self.window,
            plan.title,
            "The schematic is open in the KiCad Schematic Editor:\n\n"
            f"{basenames(plan.locked)}\n\n"
            "The editor holds its own copy, so anything written now is lost as "
            "soon as you save there. Close the Schematic Editor and try again."
            "\n\nIf no Schematic Editor is open, KiCad has left a lock file "
            f"behind — {basenames(lock_files_for(plan.locked))} — and deleting "
            "it clears this."
            "\n\nWrite to the file anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def offer_schematic_export(self, *_) -> None:
        """Last chance to get this session's changes into the schematic.

        Nothing is written without being asked — that is the whole point of the
        two buttons. But a *removal* lives only on the footprint until it is
        exported (see ``schematic_cleared_refs``), so closing on unexported
        changes loses it silently. Ask rather than let that happen quietly.

        Silent when there is nothing pending, and silent when the project has no
        schematic this could be written to: a question whose only honest answer
        is "there is nowhere to put them" is not worth interrupting a close for.
        """
        if not self.schematic_sync_pending or self.schematic is None:
            return
        if not self.schematic.default_paths():
            log.debug("No schematic for this project, so nothing to offer on close")
            return
        answer = QMessageBox.question(
            self.window,
            "To schematic",
            "The LCSC numbers you changed here are on the footprints only. The "
            "Symbol Fields Table, the schematic BOM and the next 'Update PCB "
            "from Schematic' all read the symbols instead, so they will not see "
            "them.\n\nWrite them into the schematic now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.export_to_schematic()

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
            APP_NAME,
            f"KiCad did not accept the request to {what}.\n\n"
            f"{exc}\n\nThe board has been left as it was; see the log for details.",
        )


def build(board, parts, settings=None, source=None) -> SuiteController:
    """Build the controller and its window. The app's one assembly point."""
    return SuiteController(board, parts, settings=settings, source=source)


__all__ = ["HANDLED_ROW_MENU", "SuiteController", "build"]
