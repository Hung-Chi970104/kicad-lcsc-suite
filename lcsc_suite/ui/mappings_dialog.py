"""The Mappings Manager — the remembered footprint+value → LCSC table (§5.5).

A mapping is "a part with this footprint and this value is normally this LCSC
number". It is the project-independent half of the assignment work: the next
board with a 100nF 0402 on it should not have to be told again. `Save mappings`,
`Add mapping` and `Find mapping` all landed in Phase 3; this is the window that
shows what they wrote and lets a wrong entry be taken back out.

The table is shared with the wx plugin — one mappings database in the configured
data directory — so both halves see each other's entries, and this dialog can
delete one the wx plugin wrote.

**These manager dialogs own their own store.** controller.py's rule is that the
window reports and the controller writes, and the mappings table is named in it;
the refinement Phase 5 makes is that the rule is about writes that are a
*consequence* of an action taken elsewhere — assigning a part also remembers a
mapping, and that pairing is a decision. A dialog whose entire purpose is to edit
one table is that table's editor, the way the Settings dialog is the settings
file's, and routing its deletes through the controller would add pass-through
methods that decide nothing. Nothing outside this window reads the mappings
table except `Find mapping`, which reads it live, so there is no state to keep in
step either.
"""

from __future__ import annotations

import csv
import logging
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .icons import icon

log = logging.getLogger(__name__)

#: §5.5 measured the wx dialog at 800x772.
DEFAULT_SIZE = (800, 772)

#: ``(heading, width)``. Footprint names run to fifty characters
#: (``TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal``) and the
#: other two do not, so it takes whatever is left rather than a fixed share —
#: which at a fixed 340 pushed the LCSC number off the right edge.
COLUMNS: tuple[tuple[str, int], ...] = (
    ("Footprint", 0),
    ("Value", 180),
    ("LCSC Part", 110),
)

FOOTPRINT, VALUE, LCSC = 0, 1, 2

#: The header the wx plugin writes when exporting, kept byte for byte so a CSV
#: exported by one half imports into the other.
CSV_HEADER = ("Footprint", "Part Value", "LCSC Part")


def looks_like_a_header(row) -> bool:
    """Report whether a CSV row is a header rather than a mapping.

    The wx importer calls ``next(csvreader)`` unconditionally, so a file with no
    header line silently loses its first mapping — which is invisible, because
    the other rows all import fine. Checking is two lines and cannot be wrong in
    the direction that matters: a footprint named "footprint" is not a footprint.
    """
    if not row:
        return False
    first = str(row[0] or "").strip().lower()
    return first in ("footprint", "footprint name")


class MappingsDialog(QDialog):
    """Browse, delete, import and export the remembered part mappings."""

    #: Emitted after anything in the table changed, so a caller can re-read it.
    #: Nothing needs this today — `Find mapping` reads live — but a deletion is
    #: the one action here that another window could be showing the result of.
    mappings_changed = Signal()

    def __init__(self, parent=None, library=None) -> None:
        super().__init__(parent)
        self.library = library
        self.setWindowTitle("Mappings Manager")
        self.setObjectName("mappings-dialog")
        self.resize(*DEFAULT_SIZE)

        self._build()
        self.reload()

    # -- construction ---------------------------------------------------------

    def _build(self) -> None:
        """Assemble the table and its right-hand button column."""
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        body = QHBoxLayout()
        body.setSpacing(8)
        body.addWidget(self._build_table(), 1)
        body.addWidget(self._build_buttons(), 0)
        root.addLayout(body, 1)

        self.status = QLabel("", self)
        self.status.setObjectName("mappings-status")
        self.status.setProperty("role", "status")
        root.addWidget(self.status)

    def _build_table(self) -> QTableWidget:
        """Build the three-column list."""
        table = QTableWidget(0, len(COLUMNS), self)
        table.setObjectName("mappings-table")
        table.setHorizontalHeaderLabels([heading for heading, _ in COLUMNS])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(FOOTPRINT, QHeaderView.ResizeMode.Stretch)
        for index, (_heading, width) in enumerate(COLUMNS):
            if width:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
                table.setColumnWidth(index, width)
        table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table = table
        return table

    def _build_buttons(self) -> QWidget:
        """Build §5.5's Delete / Import / Export column."""
        panel = QWidget(self)
        panel.setFixedWidth(150)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.delete_button = QPushButton(
            icon("mdi-trash-can-outline.png"), "Delete", panel
        )
        self.delete_button.setToolTip("Forget the selected mappings")
        self.delete_button.clicked.connect(self.delete_selected)
        layout.addWidget(self.delete_button)

        layout.addStretch(1)

        self.import_button = QPushButton(
            icon("mdi-database-import-outline.png"), "Import", panel
        )
        self.import_button.setToolTip("Merge mappings from a CSV file")
        self.import_button.clicked.connect(self.import_csv)
        layout.addWidget(self.import_button)

        self.export_button = QPushButton(
            icon("mdi-database-export-outline.png"), "Export", panel
        )
        self.export_button.setToolTip("Write every mapping to a CSV file")
        self.export_button.clicked.connect(self.export_csv)
        layout.addWidget(self.export_button)

        close = QPushButton("Close", panel)
        close.clicked.connect(self.reject)
        layout.addWidget(close)

        self.delete_button.setEnabled(False)
        return panel

    # -- contents -------------------------------------------------------------

    def rows(self) -> list[tuple[str, str, str]]:
        """Return every mapping as ``(footprint, value, lcsc)``."""
        if self.library is None:
            return []
        try:
            stored = self.library.get_all_mapping_data()
        except Exception:  # noqa: BLE001 - an unreadable table is an empty list
            log.warning("Could not read the mappings table", exc_info=True)
            return []
        return [
            (str(row[0] or ""), str(row[1] or ""), str(row[2] or ""))
            for row in (stored or [])
            if len(row) >= 3
        ]

    def reload(self) -> None:
        """Re-read the table from the database, keeping the sort order."""
        table = self.table
        # Sorting is switched off while filling: with it on, every inserted row
        # is re-sorted immediately and setItem lands in whichever row the item
        # has just been moved to.
        table.setSortingEnabled(False)
        table.clearContents()
        rows = self.rows()
        table.setRowCount(len(rows))
        for index, (footprint, value, number) in enumerate(rows):
            for column, text in enumerate((footprint, value, number)):
                item = QTableWidgetItem(text)
                if column == LCSC:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(index, column, item)
        table.setSortingEnabled(True)
        table.sortItems(FOOTPRINT, Qt.SortOrder.AscendingOrder)
        self._set_status(rows)
        self._on_selection_changed()

    def _set_status(self, rows) -> None:
        """Say how many mappings there are, or why there are none."""
        if self.library is None:
            self.status.setText(
                "No parts library is open, so mappings cannot be read or saved. "
                "Check the database directory in Settings."
            )
            return
        if not rows:
            self.status.setText(
                "No mappings remembered yet. `Save mappings` on the main window "
                "records one for every assigned part on the board."
            )
            return
        self.status.setText(f"{len(rows)} mapping{'' if len(rows) == 1 else 's'}")

    def selected_rows(self) -> list[tuple[str, str]]:
        """Return ``(footprint, value)`` for each selected row — the table's key."""
        selected = []
        for index in self.table.selectionModel().selectedRows():
            footprint = self.table.item(index.row(), FOOTPRINT)
            value = self.table.item(index.row(), VALUE)
            if footprint is not None and value is not None:
                selected.append((footprint.text(), value.text()))
        return selected

    def _on_selection_changed(self) -> None:
        """Enable Delete only when there is something to delete."""
        self.delete_button.setEnabled(bool(self.selected_rows()))

    # -- actions --------------------------------------------------------------

    def delete_selected(self) -> None:
        """Forget every selected mapping."""
        targets = self.selected_rows()
        if not targets or self.library is None:
            return
        for footprint, value in targets:
            self.library.delete_mapping_data(footprint, value)
        log.info("Deleted %d mapping%s", len(targets), "" if len(targets) == 1 else "s")
        self.reload()
        self.mappings_changed.emit()

    def import_csv(self) -> None:
        """Merge mappings from a CSV file."""
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import mappings", "", "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return
        try:
            written = self.load_csv(path)
        except OSError as exc:
            QMessageBox.warning(
                self, "Import mappings", f"Could not read the file.\n\n{exc}"
            )
            return
        log.info(
            "Imported %d mapping%s from %s", written, "" if written == 1 else "s", path
        )
        self.reload()
        self.mappings_changed.emit()

    def load_csv(self, path: str) -> int:
        """Read ``path`` into the mappings table. Returns how many were written.

        Insert-or-update per row, as the wx importer does, so importing a file
        twice is not an error and a re-export of an edited file is a merge
        rather than a duplication.
        """
        if self.library is None:
            return 0
        written = 0
        with open(path, encoding="utf-8", newline="") as handle:
            for index, row in enumerate(csv.reader(handle)):
                if index == 0 and looks_like_a_header(row):
                    continue
                if len(row) < 3:
                    continue
                footprint, value, number = (str(cell or "").strip() for cell in row[:3])
                if not (footprint and value and number):
                    # The same floor PartList._remember_one applies: a mapping
                    # keyed on an empty footprint would match every part without
                    # one and hand them all the same number.
                    log.debug("Skipping incomplete mapping row %r", row)
                    continue
                if self.library.get_mapping_data(footprint, value):
                    self.library.update_mapping_data(footprint, value, number)
                else:
                    self.library.insert_mapping_data(footprint, value, number)
                written += 1
        return written

    def export_csv(self) -> None:
        """Write every mapping to a CSV file."""
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export mappings",
            os.path.join(os.path.expanduser("~"), "mapping.csv"),
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        rows = self.rows()
        try:
            self.write_csv(path, rows)
        except OSError as exc:
            QMessageBox.warning(
                self, "Export mappings", f"Could not write the file.\n\n{exc}"
            )
            return
        log.info(
            "Exported %d mapping%s to %s",
            len(rows),
            "" if len(rows) == 1 else "s",
            path,
        )

    @staticmethod
    def write_csv(path: str, rows) -> None:
        """Write ``rows`` to ``path`` in the wx plugin's CSV dialect."""
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, quotechar='"', quoting=csv.QUOTE_ALL)
            writer.writerow(CSV_HEADER)
            for row in rows:
                writer.writerow(list(row))


__all__ = [
    "COLUMNS",
    "CSV_HEADER",
    "DEFAULT_SIZE",
    "MappingsDialog",
    "looks_like_a_header",
]
