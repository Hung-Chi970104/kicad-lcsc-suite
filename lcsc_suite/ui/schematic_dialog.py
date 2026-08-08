"""The two warning dialogs that guard board ↔ schematic sync (§5.1, Phase 7).

One dialog, two directions. The wx plugin asked with a ``wx.MessageBox`` holding
a hand-assembled wall of text, capped at eight references per category and
followed by "... and 23 more" — which is the sentence a user reads just before
pressing Yes on twenty-three changes they were not shown. The categories, the
counts and the warning survive; the sample does not. A table shows every row and
scrolls, so "what is about to happen to my schematic" is a question with a
complete answer.

The two directions share this class rather than subclassing it, because the
shape of the warning is identical and only the nouns move: a change to how
replacements are shown that reached only one direction is exactly the bug worth
designing out. What differs lives in :data:`lcsc_suite.schematic.DIRECTIONS`.

**REPLACED is the row that matters.** An addition costs nothing and a skip does
nothing; a replacement destroys a number somebody chose by hand, in a file the
other half of the toolchain owns. It is spelled in caps, coloured, and counted
on its own line above the table — the same emphasis the wx text had, kept
because it is the only reason this dialog exists rather than the write simply
happening.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..schematic import ADD, CLEAR, REPLACE, SKIP, SyncPlan, basenames
from . import theme

#: Wide enough for four columns of references and numbers without eliding, tall
#: enough that a dozen rows are visible before the table starts scrolling. A
#: confirmation the user has to scroll to read the *start* of is one they will
#: dismiss unread.
DEFAULT_SIZE = (620, 460)

COLUMNS = ("Reference", "Now", "Becomes", "")

#: Nothing, rather than a placeholder, for a value that does not exist. An
#: em dash in the "Now" column reads as a value and this column's whole job is
#: to say what is being destroyed.
EMPTY = ""


def _tint(kind: str) -> QColor:
    """Colour one change by how much it costs to get wrong.

    Replacements take the palette's ``bad`` for the same reason the part table
    does: it is the row that loses something somebody chose. Clears are amber —
    the field goes away, but only because the board was told to remove it.
    Additions and skips keep the ordinary text colour, because colouring
    everything is the same as colouring nothing.
    """
    if kind == REPLACE:
        return theme.colour("bad")
    if kind == CLEAR:
        return theme.colour("low")
    return theme.chrome("text")


class SchematicSyncDialog(QDialog):
    """Show every change a sync would make, and ask whether to make it."""

    def __init__(self, parent, plan: SyncPlan) -> None:
        super().__init__(parent)
        self.plan = plan
        self.setObjectName(f"schematic-{plan.direction}-dialog")
        self.setWindowTitle(plan.title)
        self.setModal(True)
        self.resize(*DEFAULT_SIZE)
        self._build()

    # -- construction --------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        lead = QLabel(self._lead(), self)
        lead.setWordWrap(True)
        root.addWidget(lead)

        counts = self._counts_line()
        if counts:
            label = QLabel(counts, self)
            label.setProperty("role", "section")
            label.setWordWrap(True)
            root.addWidget(label)

        root.addWidget(self._build_table(), 1)

        note = QLabel(self._note(), self)
        note.setProperty("role", "status")
        note.setWordWrap(True)
        root.addWidget(note)

        buttons = QDialogButtonBox(self)
        buttons.setObjectName("schematic-buttons")
        self.go = buttons.addButton(
            self.plan.words["verb"], QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.go.setDefault(True)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_table(self) -> QTableWidget:
        """List every change, applied ones first, skipped ones after."""
        rows = self.plan.rows()
        table = QTableWidget(len(rows), len(COLUMNS), self)
        table.setObjectName("schematic-changes")
        table.setHorizontalHeaderLabels(list(COLUMNS))
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Nothing here is picked, only read. Without this the first cell keeps a
        # focus rectangle that reads as "this row is selected" in a table where
        # selecting a row means nothing.
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        for index in range(len(COLUMNS) - 1):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)

        for row, change in enumerate(rows):
            colour = _tint(change.kind)
            for column, text in enumerate(
                (
                    change.reference,
                    change.before or EMPTY,
                    change.after or EMPTY,
                    change.label,
                )
            ):
                item = QTableWidgetItem(text)
                item.setForeground(colour)
                if column == 0:
                    item.setFont(theme.bold(theme.base_font()))
                table.setItem(row, column, item)
        return table

    # -- the words -----------------------------------------------------------

    def _lead(self) -> str:
        """Say plainly which file is about to be changed, and what survives."""
        return self.plan.words["lead"]

    def _counts_line(self) -> str:
        """One line of totals, replacements named first because they cost."""
        counts = self.plan.counts()
        parts = []
        if counts.get(REPLACE):
            parts.append(f"{counts[REPLACE]} REPLACED")
        if counts.get(ADD):
            parts.append(f"{counts[ADD]} gain a number")
        if counts.get(CLEAR):
            parts.append(f"{counts[CLEAR]} cleared")
        if counts.get(SKIP):
            parts.append(f"{counts[SKIP]} skipped ({self.plan.words['orphan_reason']})")
        return " · ".join(parts)

    def _note(self) -> str:
        """State the consequence that is not visible in the table."""
        lines = []
        if self.plan.direction == "to":
            lines.append("Each sheet that changes is backed up as <sheet>_old.")
            if self.plan.locked:
                lines.append(
                    f"{basenames(self.plan.locked)} is open in the Schematic "
                    "Editor. The editor holds its own copy, so anything written "
                    "now is lost as soon as you save there."
                )
        else:
            lines.append(
                "The board is only changed in memory — save the PCB in KiCad "
                "to keep it."
            )
            if self.plan.locked:
                lines.append(
                    f"{basenames(self.plan.locked)} is open in the Schematic "
                    "Editor. This reads the file on disk, so anything unsaved "
                    "there is not included."
                )
        if self.plan.missing:
            lines.append(f"Not found: {basenames(self.plan.missing)}.")
        return "\n".join(lines)


def nothing_to_do_message(plan: SyncPlan) -> str:
    """Say that both sides already agree, and what was skipped getting there.

    Still worth saying which references were *skipped*: "nothing happened" and
    "nothing happened because eleven of your symbols do not exist on the board"
    are different answers, and only the second one is actionable.
    """
    words = plan.words
    message = [words["agree"]]
    if plan.skipped:
        message.append(
            f"\n{len(plan.skipped)} {words['orphan_noun']} have "
            f"{words['orphan_reason']}, so they were skipped:\n"
            + ", ".join(change.reference for change in plan.skipped)
        )
    if plan.locked:
        message.append(
            f"\n{basenames(plan.locked)} is open in the Schematic Editor, so "
            "this reflects the file on disk rather than what is on screen."
        )
    return "\n".join(message)
