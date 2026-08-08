"""Board ↔ schematic sync — Phase 7.

The plugin lives in pcbnew and can only reach the board. The Symbol Fields
Table, the schematic BOM exporters and "Update PCB from Schematic" all read the
*schematic*, so an LCSC number that exists only on a footprint is invisible to
them, and the next push from schematic to board wipes it. The other direction is
just as common: a design arrives with the numbers already in its symbols, and a
plugin that only looks at footprints reports the whole board as unassigned.

**Neither direction ever runs on its own.** Two explicit buttons, each showing
what it is about to overwrite before it does. That is a rule about consequences,
not about taste: both directions destroy data that the other side may be the
only holder of, and a sync that fires on assignment would do it while the user
is looking somewhere else.

**The parsers are not reimplemented here.** ``schematicexport`` and
``schematicimport`` read and write ``.kicad_sch`` as text and neither imports
``wx`` or ``pcbnew``, so both port unchanged — the same arrangement Phase 6
arrived at for ``fab_rules``. What this module owns is what Phase 6's
``export.py`` owns: the *sourcing*. Where the assignments come from now that
there is no in-process ``store`` to read, which sheets to touch, and what to
tell the user before touching them.

The one thing that genuinely could not be ported is the KiCad version. In
process it comes from ``pcbnew.GetBuildVersion()``, which picks the v6 / v7 /
v8+ writer branch; out of process it comes over the IPC API and is passed to
``load_schematic`` explicitly. Phase 0 added that argument for this phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import re
from typing import Optional

from .shared import schematicexport, schematicimport

log = logging.getLogger(__name__)

#: What a single reference's change is. Four kinds, because they need four
#: different things said about them: an addition is free, a replacement destroys
#: a number somebody chose, a clear removes one outright, and a skip cannot be
#: applied at all because the other side has no such part.
ADD = "add"
REPLACE = "replace"
CLEAR = "clear"
SKIP = "skip"

#: Column heading for the "what happens" column, per kind. Spelled as the past
#: tense of what the button does, because by the time this is read again the
#: button has been pressed.
KIND_LABELS = {
    ADD: "Added",
    REPLACE: "REPLACED",
    CLEAR: "Cleared",
    SKIP: "Skipped",
}

#: Both directions, and the words that differ between them. Kept in one table
#: rather than in two near-identical dialog subclasses: the shape of the warning
#: is the same and only the nouns move, so a change to one direction's wording
#: that forgets the other is the failure worth designing out.
DIRECTIONS = {
    "to": {
        "title": "To schematic",
        "verb": "Write to schematic",
        "lead": (
            "This writes the LCSC numbers assigned here into the schematic "
            "symbols. A number the schematic has and the board does not is left "
            "alone; everything listed below is changed."
        ),
        "agree": "The schematic already carries the numbers the board has.",
        "orphan_noun": "assigned reference(s)",
        "orphan_reason": "no symbol in the schematic",
    },
    "from": {
        "title": "From schematic",
        "verb": "Update the board",
        "lead": (
            "This writes the schematic's LCSC numbers onto the footprints. A "
            "number the board has and the schematic does not is left alone; "
            "everything listed below is changed."
        ),
        "agree": "The board already carries the numbers the schematic has.",
        "orphan_noun": "symbol(s)",
        "orphan_reason": "no footprint on this board",
    },
}


def natural_key(text: str):
    """Sort R2 before R10, as the part table and the wx plugin both do."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"([0-9]+)", text or "")
    ]


@dataclass(frozen=True)
class Change:
    """One reference, what it holds now and what the sync would leave on it."""

    reference: str
    before: str
    after: str
    kind: str

    @property
    def label(self) -> str:
        """Name this change for the confirmation table."""
        return KIND_LABELS.get(self.kind, self.kind)


@dataclass
class SyncPlan:
    """Everything one direction would do, worked out before anything is done.

    Built by reading both sides, so the numbers in it are what is actually on
    disk rather than what this session believes it assigned. That is the whole
    value of the preview: only the schematic knows what the schematic currently
    says, and "this number is about to be destroyed" is not a claim worth making
    from memory.
    """

    direction: str
    #: Written if the user goes ahead, most destructive kinds included.
    changes: list = field(default_factory=list)
    #: Cannot be applied — the other side has no such reference.
    skipped: list = field(default_factory=list)
    #: Sheets the Schematic Editor has open. Not fatal in either direction, but
    #: it means different things: reading gets the last *saved* state, writing
    #: gets overwritten as soon as the user saves in eeschema.
    locked: list = field(default_factory=list)
    #: Sheets named but not found on disk.
    missing: list = field(default_factory=list)
    #: Sheets actually read. Empty means nothing could be read at all, which the
    #: caller has to tell apart from "read fine, nothing to do".
    read: list = field(default_factory=list)
    paths: list = field(default_factory=list)
    #: What to hand the writer or the applier. A superset of ``changes`` in the
    #: export direction: the exporter is given every assignment and leaves
    #: alone the references it does not find, which is what stops a sync wiping
    #: numbers the schematic has and the board has never seen.
    payload: dict = field(default_factory=dict)

    @property
    def words(self) -> dict:
        """The nouns and verbs of this direction."""
        return DIRECTIONS[self.direction]

    @property
    def title(self) -> str:
        """The window title, which is also the button that opened it."""
        return self.words["title"]

    def counts(self) -> dict:
        """How many changes of each kind, skips included."""
        tally: dict = {}
        for change in list(self.changes) + list(self.skipped):
            tally[change.kind] = tally.get(change.kind, 0) + 1
        return tally

    def has_work(self) -> bool:
        """Whether anything would actually be written."""
        return bool(self.changes)

    def rows(self) -> list:
        """Every change to show, the applied ones first, then the skipped."""
        return sorted(
            self.changes, key=lambda change: natural_key(change.reference)
        ) + sorted(self.skipped, key=lambda change: natural_key(change.reference))

    def summary(self) -> str:
        """One line naming what would happen, for the log."""
        counts = self.counts()
        parts = [
            f"{counts[kind]} {KIND_LABELS[kind].lower()}"
            for kind in (ADD, REPLACE, CLEAR, SKIP)
            if counts.get(kind)
        ]
        return f"{self.title}: " + (", ".join(parts) if parts else "nothing to do")


class SchematicSync:
    """Reads and writes the ``.kicad_sch`` files belonging to one board."""

    def __init__(self, board, parts) -> None:
        self.board = board
        self.parts = parts

    # -- which files ---------------------------------------------------------

    def default_paths(self) -> list:
        """Root sheet of this project, or nothing if it cannot be identified.

        One path is normally the whole hierarchy: the sub-sheets below the root
        are found by following its ``Sheetfile`` properties, in both directions.
        A caller that gets an empty list asks the user instead.
        """
        info = self.board.info()
        root = schematicexport.find_root_schematic(info.project_path, info.name)
        return [root] if root else []

    def locked(self, paths) -> list:
        """Which of ``paths`` the Schematic Editor currently has open."""
        return [path for path in paths if schematicexport.is_open_in_editor(path)]

    # -- what each side says -------------------------------------------------

    def board_assignments(self) -> dict:
        """Every reference on this board mapped to its LCSC number.

        Unassigned footprints are present with an empty string, and that is the
        distinction the import turns on: a reference the board does not have at
        all cannot be assigned, one that is merely blank can.
        """
        return {
            str(part["reference"]): str(part["lcsc"] or "")
            for part in self.parts.store.read_all()
        }

    def schematic_assignments(self, cleared=()) -> dict:
        """LCSC numbers the schematic should end up carrying.

        Assigned numbers, plus the references this session *deliberately
        cleared*. A reference that is merely blank in the store is left out on
        purpose: it may be one the schematic has a number for that the board has
        never picked up, and exporting "blank" and "cleared" alike would wipe it.
        Telling those two apart is what ``schematic_cleared_refs`` is for, and
        why it is tracked from the assignment path rather than derived here.
        """
        assignments = {
            str(part["reference"]): str(part["lcsc"])
            for part in self.parts.store.read_all()
            if part.get("lcsc")
        }
        for reference in cleared:
            assignments.setdefault(str(reference), "")
        return assignments

    # -- planning ------------------------------------------------------------

    def plan_export(self, paths, cleared=()) -> SyncPlan:
        """Work out what writing the board's numbers into ``paths`` would do.

        Read from the files rather than guessed. The confirmation has to name
        the numbers that are about to be destroyed, and only the schematic knows
        what those currently are.
        """
        assignments = self.schematic_assignments(cleared)
        current = schematicimport.read_schematic(paths)
        changes, skipped = [], []
        for reference in sorted(assignments, key=natural_key):
            want = assignments[reference]
            have = current.numbers.get(reference, "")
            if want == have:
                continue
            if reference not in current.references:
                # No symbol to write to. Only worth reporting when there was a
                # number for it: clearing a reference the schematic never had is
                # not a change anybody needs told about.
                if want:
                    skipped.append(Change(reference, "", want, SKIP))
            elif not want:
                changes.append(Change(reference, have, "", CLEAR))
            elif have:
                changes.append(Change(reference, have, want, REPLACE))
            else:
                changes.append(Change(reference, "", want, ADD))
        return SyncPlan(
            direction="to",
            changes=changes,
            skipped=skipped,
            locked=self.locked(paths),
            missing=list(current.missing),
            read=list(current.read),
            paths=list(paths),
            payload=assignments,
        )

    def plan_import(self, paths) -> SyncPlan:
        """Work out what copying ``paths``' numbers onto the board would do."""
        found = schematicimport.read_schematic(paths)
        log.info(found.summary())
        diff = schematicimport.diff_against_board(
            found.numbers, self.board_assignments()
        )
        changes = [
            Change(reference, "", lcsc, ADD) for reference, lcsc in diff.added
        ] + [
            Change(reference, current, lcsc, REPLACE)
            for reference, current, lcsc in diff.replaced
        ]
        skipped = [
            Change(reference, "", found.numbers.get(reference, ""), SKIP)
            for reference in diff.unknown
        ]
        return SyncPlan(
            direction="from",
            changes=changes,
            skipped=skipped,
            locked=list(found.locked),
            missing=list(found.missing),
            read=list(found.read),
            paths=list(paths),
            payload=diff.assignments(),
        )

    # -- doing it ------------------------------------------------------------

    def write(self, plan: SyncPlan, skip_locked: bool = True):
        """Write ``plan``'s assignments into the schematic and report.

        ``parent=None``: the exporter only touches its parent to derive the
        assignments when it is not given any, and it is always given them here.
        Passing the window instead would make a UI object a dependency of a text
        rewriter for no reason.
        """
        exporter = schematicexport.SchematicExport(
            None, plan.payload, skip_locked=skip_locked
        )
        return exporter.load_schematic(plan.paths, version=self._version())

    def _version(self) -> Optional[str]:
        """Return the KiCad version string that picks the file-format branch.

        Over IPC rather than from ``pcbnew.GetBuildVersion()``, which does not
        exist in this process. Absent it, ``load_schematic`` falls back to the
        modern writer — the only format KiCad 10 produces anyway.
        """
        try:
            return self.board.info().kicad_version or None
        except Exception:  # noqa: BLE001 - a version is a nicety, not a blocker
            log.debug("Could not read the KiCad version", exc_info=True)
            return None


def basenames(paths) -> str:
    """Name a list of sheets the way a message should say them."""
    return ", ".join(os.path.basename(path) for path in paths)
