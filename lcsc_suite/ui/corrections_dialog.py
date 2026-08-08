"""The Corrections Manager — rotation and offset fixes for the CPL (§5.4).

A correction says "any footprint whose name matches this pattern is placed by
JLC at the wrong angle, or off-centre, so rotate it by this much and shift it by
that much". It is the one subsystem the BOM/CPL export depends on that nothing
else in the toolchain provides, which is why dropping the CPL writer would have
cascaded into deleting all of this (plan §1).

Corrections live in one of two SQLite databases — a global one shared by every
project, or a board-local copy — and `Use global corrections` switches between
them. The switch is confirmed in both directions and for different reasons:
going local *copies* the global table so nothing is lost, while going global
abandons whatever the local one holds.

**Two corrections to §5.4.** It lists five columns, `Regex · Pattern · Rotation ·
Offset X · Offset Y`; the table has four. "Pattern" is what the CSV export calls
the same column that the table and the Add/Edit box call "Regex" — one column,
written down twice. And `Update` is not "update the selected row": it downloads
the community correction table from Matthew Lai's JLCKicadTools repository. It is
the only control in this dialog that touches the network, and the only one that
can be disabled by its caller.

The dialog owns its own store, for the reason
:mod:`lcsc_suite.ui.mappings_dialog` sets out. What it does *not* own is the
consequence: a correction changes what the CPL will contain for parts already on
the board, so every write here emits :attr:`CorrectionsDialog.corrections_changed`
and the controller rebuilds the part list — the same thing the wx dialog's
``PopulateFootprintListEvent`` does.
"""

from __future__ import annotations

import csv
import logging
import os
import re
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .explorer.tasks import Pool
from .icons import icon

log = logging.getLogger(__name__)

#: The wx dialog is 800x800.
DEFAULT_SIZE = (800, 800)

#: ``(heading, width)``. The three numbers are fixed and the pattern takes
#: whatever is left — a regex has no natural width, and giving it a fixed one
#: put the last column past the right edge and a horizontal scrollbar under a
#: table with four columns in it.
COLUMNS: tuple[tuple[str, int], ...] = (
    ("Regex", 0),
    ("Rotation", 90),
    ("Offset X", 90),
    ("Offset Y", 90),
)

REGEX, ROTATION, OFFSET_X, OFFSET_Y = 0, 1, 2, 3

#: The header the wx plugin writes on export. It calls the first column
#: "Pattern" where the table calls it "Regex"; kept as it is so a CSV written by
#: one half imports into the other.
CSV_HEADER = ("Pattern", "Rotation", "Offset X", "Offset Y")


def to_float(value) -> float:
    """Parse a number from a text field, treating anything else as zero.

    The wx dialog's rule, kept: these four fields are typed into freely and a
    half-finished "−" or "0." must not raise while the user is still typing.
    """
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def format_offset(value: float) -> str:
    """Render an offset the way the wx dialog does.

    Two decimals for short numbers, full precision for anything longer, so a
    correction of 0.5 reads as ``0.50`` and one of 0.123456 is not silently
    rounded away in the column the user is about to check.
    """
    text = str(value)
    return f"{value:.2f}" if len(text) < 4 else text


def looks_like_a_header(row) -> bool:
    """Report whether a CSV row is a header rather than a correction."""
    if not row:
        return False
    return str(row[0] or "").strip().lower() in ("pattern", "regex")


def pattern_for_reference(reference: str) -> str:
    """Anchored pattern matching exactly one designator."""
    return f"^{re.escape(reference)}$"


def pattern_for_package(footprint: str) -> str:
    """Left-anchored pattern matching a footprint and its variants."""
    return f"^{re.escape(footprint)}"


def pattern_for_name(value: str) -> str:
    """Unanchored pattern matching a part value anywhere in the name."""
    return re.escape(value)


class CorrectionsDialog(QDialog):
    """Browse and edit the rotation/offset corrections applied to the CPL."""

    #: Emitted after any write. The part list is rebuilt on it, because a
    #: correction changes what the CPL will say about parts already placed.
    corrections_changed = Signal()

    def __init__(
        self,
        parent=None,
        library=None,
        pattern: str = "",
        allow_network: bool = True,
    ) -> None:
        super().__init__(parent)
        self.library = library
        #: Whether `Update` may download the community table. Off for the probe
        #: and the tests, the same shape as ``Library(allow_network=False)`` and
        #: ``build_source(offline=True)``.
        self.allow_network = allow_network
        #: The pattern of the row currently selected, or ``None``. This is the
        #: table's key, so editing the Regex field means "rename this rule",
        #: which is a delete plus an insert rather than an update.
        self.selected_pattern: Optional[str] = None

        self.setWindowTitle("Corrections Manager")
        self.setObjectName("corrections-dialog")
        self.resize(*DEFAULT_SIZE)

        # One worker: the only background job here is a single HTTP GET, and two
        # of them at once would race to insert the same rows.
        self._pool = Pool("lcsc-corrections", 1, self._on_download_finished)

        self._build()
        self.reload()
        if pattern:
            self.regex.setText(pattern)
        self._update_buttons()

    # -- construction ---------------------------------------------------------

    def _build(self) -> None:
        """Assemble the Add/Edit box, the table and the button column."""
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        root.addWidget(self._build_editor())

        body = QHBoxLayout()
        body.setSpacing(8)
        body.addWidget(self._build_table(), 1)
        body.addWidget(self._build_buttons(), 0)
        root.addLayout(body, 1)

        self.status = QLabel("", self)
        self.status.setObjectName("corrections-status")
        self.status.setProperty("role", "status")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

    def _build_editor(self) -> QGroupBox:
        """Build the `Add / Edit` row of four fields."""
        box = QGroupBox("Add / Edit", self)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(12)

        self.regex = self._field(box, layout, "Regex", "", stretch=1)
        self.regex.setPlaceholderText("^R_0402_1005Metric")
        self.rotation = self._field(box, layout, "Rotation", "0", width=110)
        self.offset_x = self._field(box, layout, "Offset X", "0.00", width=110)
        self.offset_y = self._field(box, layout, "Offset Y", "0.00", width=110)

        for field in (self.regex, self.rotation, self.offset_x, self.offset_y):
            field.textChanged.connect(self._update_buttons)
        return box

    def _field(
        self,
        parent: QWidget,
        layout: QHBoxLayout,
        label: str,
        default: str,
        width: int = 0,
        stretch: int = 0,
    ) -> QLineEdit:
        """Add one labelled entry field to the editor row."""
        column = QVBoxLayout()
        column.setSpacing(2)
        caption = QLabel(label, parent)
        caption.setProperty("role", "section")
        column.addWidget(caption)
        entry = QLineEdit(default, parent)
        entry.setObjectName(f"correction-{label.lower().replace(' ', '-')}")
        if width:
            entry.setFixedWidth(width)
        column.addWidget(entry)
        layout.addLayout(column, stretch)
        return entry

    def _build_table(self) -> QTableWidget:
        """Build the corrections list."""
        table = QTableWidget(0, len(COLUMNS), self)
        table.setObjectName("corrections-table")
        table.setHorizontalHeaderLabels([heading for heading, _ in COLUMNS])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Single, unlike the mappings table: selecting a row loads it into the
        # editor above, and there is no sensible thing to load from two.
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(REGEX, QHeaderView.ResizeMode.Stretch)
        for index, (_heading, width) in enumerate(COLUMNS):
            if width:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
                table.setColumnWidth(index, width)
        table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table = table
        return table

    def _build_buttons(self) -> QWidget:
        """Build §5.4's right-hand button column."""
        panel = QWidget(self)
        # 190, not 160: `Use global corrections` is the longest label here and
        # at 160 it elides to "Use global correction:", which reads as a typo
        # rather than as a truncation.
        panel.setFixedWidth(190)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.save_button = QPushButton(
            icon("mdi-content-save-outline.png"), "Save", panel
        )
        self.save_button.setToolTip("Add the rule above, or update it if it exists")
        self.save_button.clicked.connect(self.save_correction)
        layout.addWidget(self.save_button)

        self.delete_button = QPushButton(
            icon("mdi-trash-can-outline.png"), "Delete", panel
        )
        self.delete_button.setToolTip("Remove the selected rule")
        self.delete_button.clicked.connect(self.delete_correction)
        layout.addWidget(self.delete_button)

        layout.addStretch(1)

        self.update_button = QPushButton(
            icon("mdi-cloud-download-outline.png"), "Update", panel
        )
        self.update_button.setToolTip(
            "Download the community correction table from Matthew Lai's "
            "JLCKicadTools repository. Existing rules are kept — only patterns "
            "you do not already have are added."
        )
        self.update_button.clicked.connect(self.download_corrections)
        layout.addWidget(self.update_button)

        self.import_button = QPushButton(
            icon("mdi-database-import-outline.png"), "Import", panel
        )
        self.import_button.setToolTip("Merge corrections from a CSV file")
        self.import_button.clicked.connect(self.import_csv)
        layout.addWidget(self.import_button)

        self.export_button = QPushButton(
            icon("mdi-database-export-outline.png"), "Export", panel
        )
        self.export_button.setToolTip("Write every correction to a CSV file")
        self.export_button.clicked.connect(self.export_csv)
        layout.addWidget(self.export_button)

        self.global_corrections = QCheckBox("Use global corrections", panel)
        self.global_corrections.setObjectName("use-global-corrections")
        self.global_corrections.setToolTip(
            "Whether corrections come from the database shared by every project "
            "or from a copy kept beside this board"
        )
        self.global_corrections.setChecked(self._uses_global())
        self.global_corrections.clicked.connect(self.switch_database)
        layout.addWidget(self.global_corrections)

        close = QPushButton("Close", panel)
        close.clicked.connect(self.reject)
        layout.addWidget(close)

        if not self.allow_network:
            self.update_button.setEnabled(False)
            self.update_button.setToolTip(
                "Downloading corrections is disabled in this session"
            )
        return panel

    # -- contents -------------------------------------------------------------

    def _uses_global(self) -> bool:
        """Report which corrections database is in use."""
        if self.library is None:
            return True
        try:
            return bool(self.library.uses_global_correction_database())
        except Exception:  # noqa: BLE001 - report a state, never block the dialog
            log.debug("Could not read the corrections database mode", exc_info=True)
            return True

    def rows(self) -> list[tuple[str, int, float, float]]:
        """Return every correction as ``(pattern, rotation, offset x, offset y)``."""
        if self.library is None:
            return []
        try:
            stored = self.library.get_all_correction_data()
        except Exception:  # noqa: BLE001 - an unreadable table is an empty list
            log.warning("Could not read the corrections table", exc_info=True)
            return []
        return [
            (str(pattern), int(rotation), float(offset[0]), float(offset[1]))
            for pattern, rotation, offset in (stored or [])
        ]

    def reload(self) -> None:
        """Re-read the table, keeping the selected pattern selected."""
        table = self.table
        table.setSortingEnabled(False)
        table.clearContents()
        rows = self.rows()
        table.setRowCount(len(rows))
        for index, (pattern, rotation, offset_x, offset_y) in enumerate(rows):
            cells = (
                pattern,
                str(rotation),
                format_offset(offset_x),
                format_offset(offset_y),
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column != REGEX:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                table.setItem(index, column, item)
        table.setSortingEnabled(True)
        # Stated rather than left to Qt: the header shows a sort indicator on
        # column 0 from the start, and without this the rows arrive in the
        # opposite order to the one it is pointing at.
        table.sortItems(REGEX, Qt.SortOrder.AscendingOrder)
        self._reselect()
        self._set_status(rows)

    def _reselect(self) -> None:
        """Put the selection back on :attr:`selected_pattern` after a reload."""
        if self.selected_pattern is None:
            return
        for row in range(self.table.rowCount()):
            item = self.table.item(row, REGEX)
            if item is not None and item.text() == self.selected_pattern:
                self.table.selectRow(row)
                return

    def _set_status(self, rows) -> None:
        """Say how many rules there are and which database they came from."""
        if self.library is None:
            self.status.setText(
                "No parts library is open, so corrections cannot be read or "
                "saved. Check the database directory in Settings."
            )
            return
        where = "global" if self._uses_global() else "board-local"
        if not rows:
            self.status.setText(
                f"No corrections in the {where} database. `Update` fetches the "
                "community table, or add a rule above."
            )
            return
        self.status.setText(
            f"{len(rows)} correction{'' if len(rows) == 1 else 's'} "
            f"in the {where} database"
        )

    def _on_selection_changed(self) -> None:
        """Load the selected rule into the editor."""
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            self.selected_pattern = None
            self._update_buttons()
            return
        row = indexes[0].row()

        def text(column: int) -> str:
            item = self.table.item(row, column)
            return "" if item is None else item.text()

        self.selected_pattern = text(REGEX)
        self.regex.setText(self.selected_pattern)
        self.rotation.setText(str(int(to_float(text(ROTATION)))))
        self.offset_x.setText(format_offset(to_float(text(OFFSET_X))))
        self.offset_y.setText(format_offset(to_float(text(OFFSET_Y))))
        self._update_buttons()

    def _update_buttons(self, *_) -> None:
        """Enable Save only with all four fields filled, Delete with a selection."""
        complete = all(
            field.text().strip()
            for field in (self.regex, self.rotation, self.offset_x, self.offset_y)
        )
        self.save_button.setEnabled(bool(complete) and self.library is not None)
        self.delete_button.setEnabled(
            self.selected_pattern is not None and self.library is not None
        )

    # -- editing --------------------------------------------------------------

    def values(self) -> tuple[str, int, float, float]:
        """Read the editor as ``(pattern, rotation, offset x, offset y)``."""
        return (
            self.regex.text().strip(),
            int(to_float(self.rotation.text())),
            to_float(self.offset_x.text()),
            to_float(self.offset_y.text()),
        )

    def save_correction(self) -> None:
        """Add the rule in the editor, or update the one it names.

        The three cases the wx dialog distinguishes, kept, because each is a
        different intent:

        * the pattern is the selected row's — the user edited its numbers, so
          update in place;
        * the pattern is new — the user renamed the selected rule (delete the
          old, insert the new) or is adding one from scratch;
        * the pattern already belongs to a *different* row — the two would
          collide, so ask before overwriting, and say which values are being
          replaced by which.
        """
        if self.library is None:
            return
        pattern, rotation, offset_x, offset_y = self.values()
        if not pattern:
            return
        offset = (offset_x, offset_y)

        if pattern == self.selected_pattern:
            self.library.update_correction_data(pattern, rotation, offset)
            self._after_write(pattern, rotation, offset)
            return

        existing = self._row_for(pattern)
        if existing is None:
            if self.selected_pattern is not None:
                # A rename: the old row would otherwise stay behind as a rule
                # nobody meant to keep.
                self.library.delete_correction_data(self.selected_pattern)
            self.library.insert_correction_data(pattern, rotation, offset)
            self._after_write(pattern, rotation, offset)
            return

        if existing == (pattern, rotation, offset_x, offset_y):
            # Identical to what is already stored: select it rather than
            # rewriting the same values and claiming a change.
            self.selected_pattern = pattern
            self.reload()
            return

        if not self._confirm_overwrite(existing, (rotation, offset_x, offset_y)):
            return
        if self.selected_pattern is not None:
            self.library.delete_correction_data(self.selected_pattern)
        self.library.update_correction_data(pattern, rotation, offset)
        self._after_write(pattern, rotation, offset)

    def _row_for(self, pattern: str):
        """Return the stored row for ``pattern``, or ``None``.

        Read from the database rather than from the table. The wx version reads
        it out of the grid and — because it reuses the loop variable after the
        loop has ended — reads whichever row happened to be *last*, so the
        "already exists with different values" prompt quotes the wrong numbers
        and the comparison that decides whether to prompt at all is made against
        an unrelated rule.
        """
        for row in self.rows():
            if row[0] == pattern:
                return row
        return None

    def _confirm_overwrite(self, existing, replacement) -> bool:
        """Ask before replacing a rule that already exists under this pattern."""
        pattern, rotation, offset_x, offset_y = existing
        new_rotation, new_x, new_y = replacement
        was = f"({rotation}°, {format_offset(offset_x)}/{format_offset(offset_y)})"
        now = f"({new_rotation}°, {format_offset(new_x)}/{format_offset(new_y)})"
        if self.selected_pattern is None:
            detail = f"Update the correction {was} to {now}?"
        else:
            detail = (
                f"Replace the correction {was} with {now}, "
                f"removing the rule for '{self.selected_pattern}'?"
            )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Correction exists")
        box.setText(f"A rule for '{pattern}' already exists.")
        box.setInformativeText(detail)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _after_write(self, pattern: str, rotation: int, offset) -> None:
        """Normalise the editor, reload and announce."""
        self.selected_pattern = pattern
        self.rotation.setText(str(rotation))
        self.offset_x.setText(format_offset(offset[0]))
        self.offset_y.setText(format_offset(offset[1]))
        log.info(
            "Correction '%s': %d°, %s/%s",
            pattern,
            rotation,
            format_offset(offset[0]),
            format_offset(offset[1]),
        )
        self.reload()
        self.corrections_changed.emit()

    def delete_correction(self) -> None:
        """Remove the selected rule."""
        if self.library is None or self.selected_pattern is None:
            return
        pattern = self.selected_pattern
        self.library.delete_correction_data(pattern)
        self.selected_pattern = None
        log.info("Deleted the correction for '%s'", pattern)
        self.reload()
        self.corrections_changed.emit()

    # -- the two databases ----------------------------------------------------

    def switch_database(self) -> None:
        """Move between the global corrections database and the board-local one.

        Confirmed in both directions, with different defaults, because the two
        are not symmetrical: switching to local *copies* the global table, so
        nothing is lost and Yes is a safe default; switching to global abandons
        whatever the local one holds, so the default is No.
        """
        if self.library is None:
            self.global_corrections.setChecked(self._uses_global())
            return
        currently_global = self._uses_global()
        box = QMessageBox(self)
        box.setWindowTitle("Switching corrections database")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if currently_global:
            box.setIcon(QMessageBox.Icon.Question)
            box.setText("Switch to the board-local corrections database?")
            box.setInformativeText(
                "The current global database is copied, so this board keeps "
                "every rule it has now and later changes affect only this board."
            )
            box.setDefaultButton(QMessageBox.StandardButton.Yes)
        else:
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText("Switch to the global corrections database?")
            box.setInformativeText(
                "The rules in this board's local database will no longer be "
                "used. Export them first if you want to keep them."
            )
            box.setDefaultButton(QMessageBox.StandardButton.No)

        if box.exec() != QMessageBox.StandardButton.Yes:
            self.global_corrections.setChecked(currently_global)
            return

        self.library.switch_to_global_correction_database(not currently_global)
        self.global_corrections.setChecked(self._uses_global())
        log.info(
            "Corrections now come from the %s database",
            "global" if self._uses_global() else "board-local",
        )
        self.reload()
        self.corrections_changed.emit()

    # -- the community table --------------------------------------------------

    def download_corrections(self) -> None:
        """Fetch the community correction table, off the UI thread.

        On a worker because ``fetch_remote_corrections`` is a synchronous HTTP
        GET with a ten-second timeout, and a ten-second freeze of the whole
        window is indistinguishable from a hang. The button is disabled while it
        runs so a second press cannot race the first into the same table.
        """
        if self.library is None or not self.allow_network:
            return
        self.update_button.setEnabled(False)
        self.status.setText("Downloading the community correction table …")
        library = self.library

        def work():
            library.create_correction_table()
            library.fetch_remote_corrections()
            return True

        self._pool.start(0, "corrections", work)

    def _on_download_finished(self, _token, _key, result) -> None:
        """Reload once the download worker returns.

        ``fetch_remote_corrections`` swallows its own failures and logs them, so
        there is no error to report here that the log pane does not already
        have; what this can say is whether anything arrived.
        """
        self.update_button.setEnabled(self.allow_network)
        before = self.table.rowCount()
        self.reload()
        added = self.table.rowCount() - before
        if result is None:
            self.status.setText(
                "The correction download did not complete; see the log for details."
            )
            return
        if added > 0:
            log.info("Added %d correction%s", added, "" if added == 1 else "s")
        self.corrections_changed.emit()

    # -- CSV ------------------------------------------------------------------

    def import_csv(self) -> None:
        """Merge corrections from a CSV file."""
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import corrections", "", "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return
        try:
            written = self.load_csv(path)
        except OSError as exc:
            QMessageBox.warning(
                self, "Import corrections", f"Could not read the file.\n\n{exc}"
            )
            return
        log.info(
            "Imported %d correction%s from %s",
            written,
            "" if written == 1 else "s",
            path,
        )
        self.reload()
        self.corrections_changed.emit()

    def load_csv(self, path: str) -> int:
        """Read ``path`` into the corrections table. Returns how many were written."""
        if self.library is None:
            return 0
        written = 0
        with open(path, encoding="utf-8", newline="") as handle:
            for index, row in enumerate(csv.reader(handle)):
                if index == 0 and looks_like_a_header(row):
                    continue
                if not row or not str(row[0] or "").strip():
                    continue
                pattern = str(row[0]).strip()
                rotation = int(to_float(row[1])) if len(row) > 1 else 0
                offset = (
                    to_float(row[2]) if len(row) > 2 else 0.0,
                    to_float(row[3]) if len(row) > 3 else 0.0,
                )
                if self.library.get_correction_data(pattern):
                    self.library.update_correction_data(pattern, rotation, offset)
                else:
                    self.library.insert_correction_data(pattern, rotation, offset)
                written += 1
        return written

    def export_csv(self) -> None:
        """Write every correction to a CSV file."""
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export corrections",
            os.path.join(os.path.expanduser("~"), "corrections.csv"),
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        rows = self.rows()
        try:
            self.write_csv(path, rows)
        except OSError as exc:
            QMessageBox.warning(
                self, "Export corrections", f"Could not write the file.\n\n{exc}"
            )
            return
        log.info(
            "Exported %d correction%s to %s",
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
            for pattern, rotation, offset_x, offset_y in rows:
                writer.writerow([pattern, rotation, offset_x, offset_y])

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Drop any queued download on the way out."""
        self._pool.clear()
        super().closeEvent(event)


__all__ = [
    "COLUMNS",
    "CSV_HEADER",
    "DEFAULT_SIZE",
    "CorrectionsDialog",
    "format_offset",
    "looks_like_a_header",
    "pattern_for_name",
    "pattern_for_package",
    "pattern_for_reference",
    "to_float",
]
