"""The Settings dialog — what is left of it once Gerber output went (§5.3).

The wx dialog is a two-column grid of icon + checkbox, and most of what it holds
is Gerber-plotting settings: tented vias, fill zones, force DRC, plot values,
plot references, subtract soldermask, the order-number placeholder check and the
whole `Generation hooks` group. All of that went with the plot path (plan §1), so
what remains is small enough to read as a single column.

Two pieces of wx machinery are deleted rather than ported:

* **`create_disabled_bitmap`** — the hand-drawn red X over a bitmap, which
  existed because wx would not dim a `wx.Bitmap` for a disabled control. Qt
  renders the disabled state from the same `QIcon`.
* **The paired icon per row.** Each wx checkbox swapped a bitmap as well as its
  label (`bom.png` / `no_bom.png`). Two synchronised statements of the same fact
  is one more than is useful in a single column, and the icons were the half
  that could go wrong silently — `icons.icon()` returns an empty `QIcon` rather
  than raising, which is how a whole toolbar lost its images once.

**The inverted labels stay.** They are not decoration: a checkbox labelled
"Highlight search matches" tells you what the box *is* when ticked and leaves you
to infer the other state, and these settings are ones where the inferred state is
the surprising one. The label states the behaviour in force, so the row reads the
same way whichever way it is set.

This dialog writes its own settings. That does not contradict controller.py's
rule — the writes it makes are to the settings file, not to the board or the
project database — but a setting can have an effect that outlives the dialog, so
every change is also announced on :attr:`SettingsDialog.changed` and the
controller decides what to do about it.
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..shared import bom_help_text, dblib

log = logging.getLogger(__name__)

#: Opening size. Narrow, because a single column of checkboxes that is 900px
#: wide puts the tick and the end of its label a screen apart.
DEFAULT_SIZE = (560, 470)

#: ``(section, key, ticked label, unticked label, tooltip)``.
#:
#: The ``lcsc_priority`` pair departs from the wx wording, which reads "LCSC
#: numbers from **schematic** have priority". The value it compares is the
#: footprint's own LCSC field — the board's, not the schematic's — and in this
#: app the schematic is a separate place with two explicit buttons of its own
#: (Phase 7). Naming the wrong one of two things the app can actually do is
#: worse than departing from the original wording.
TOGGLES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "general",
        "lcsc_priority",
        "LCSC numbers on the board have priority",
        "LCSC numbers from the database have priority",
        "When a footprint and the project database disagree about a part's "
        "number, which one wins. The database keeps a number after the "
        "footprint's field has been cleared, so 'database' is what survives a "
        "board edit and 'board' is what lets one overwrite the record.",
    ),
    (
        "general",
        "order_number",
        "Add parts without LCSC number to BOM/POS",
        "Don't add parts without LCSC number to BOM/POS",
        "Whether an unassigned part still gets a line in the exported BOM and "
        "position files. Off keeps the files to what JLC can actually place.",
    ),
    (
        "highlighting",
        "matches",
        "Highlight search matches",
        "Do not highlight search matches",
        "Tint the parts in the LCSC Params column that corroborate the row's "
        "own value and footprint. A row with nothing lit is one where the "
        "derived parameters and the board disagree.",
    ),
    (
        "general",
        "highlight_standard_parts",
        "Highlight standard-mode trigger parts",
        "Do not highlight standard-mode trigger parts",
        "Colour the parts that individually push the board into JLC's Standard "
        "assembly pricing. Advisory amber, not the unassigned red: nothing is "
        "broken, it just costs more.",
    ),
    (
        "general",
        "bom_estimator_show",
        "Show BOM cost estimator",
        "Hide BOM cost estimator",
        "Whether the board-count row and the cost summary appear under the "
        "main window's toolbar",
    ),
)

#: Which toggle the Help button sits beside — the estimator's, as in the wx
#: dialog, because the help text is about the estimator and not about settings.
HELP_AFTER = "bom_estimator_show"


class InvertedToggle(QCheckBox):
    """A checkbox whose label states the behaviour currently in force.

    ``setChecked`` is overridden rather than relying on ``toggled`` alone
    because ``toggled`` does not fire when the state asked for is the state
    already held — so a dialog built from a stored ``False`` would show the
    ticked wording over an unticked box.
    """

    def __init__(self, on_text: str, off_text: str, parent=None) -> None:
        super().__init__(on_text, parent)
        self.on_text = on_text
        self.off_text = off_text
        self.toggled.connect(lambda checked: self.relabel(checked))

    def relabel(self, checked: bool) -> None:
        """Show the label for ``checked``."""
        self.setText(self.on_text if checked else self.off_text)

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt override
        """Set the state and the matching label."""
        super().setChecked(bool(checked))
        self.relabel(self.isChecked())


class SettingsDialog(QDialog):
    """The application's preferences."""

    #: ``(section, key, value)`` for every change, as it is made. The controller
    #: listens: some of these have to be applied to a window that is already
    #: open, and a few mean the part list has to be rebuilt.
    changed = Signal(str, str, object)

    def __init__(self, parent=None, settings=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("LCSC Suite settings")
        self.setObjectName("settings-dialog")
        self.resize(*DEFAULT_SIZE)

        self.toggles: dict[str, InvertedToggle] = {}
        self._build()

    # -- construction ---------------------------------------------------------

    def _build(self) -> None:
        """Assemble the single column."""
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        root.addWidget(self._build_behaviour_group())
        root.addWidget(self._build_highlighting_group())
        root.addWidget(self._build_library_group())
        root.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

    def _build_behaviour_group(self) -> QGroupBox:
        """Build the assignment and export behaviour group."""
        box = QGroupBox("Parts and output", self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(6)
        for key in ("lcsc_priority", "order_number", "bom_estimator_show"):
            layout.addWidget(self._row(box, key))
        return box

    def _build_highlighting_group(self) -> QGroupBox:
        """Build the two colour advisories, under §5.3's `Match highlighting` label."""
        box = QGroupBox("Match highlighting", self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(6)
        for key in ("matches", "highlight_standard_parts"):
            layout.addWidget(self._row(box, key))
        return box

    def _row(self, parent: QWidget, key: str) -> QWidget:
        """Build one checkbox row, with the Help button if it belongs here."""
        section, _key, on_text, off_text, tooltip = next(
            entry for entry in TOGGLES if entry[1] == key
        )
        toggle = InvertedToggle(on_text, off_text, parent)
        toggle.setObjectName(f"setting-{key}")
        toggle.setToolTip(tooltip)
        toggle.setChecked(bool(self._setting(section, key)))
        toggle.toggled.connect(
            lambda checked, s=section, k=key: self._store(s, k, bool(checked))
        )
        self.toggles[key] = toggle

        if key != HELP_AFTER:
            return toggle

        row = QWidget(parent)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(toggle, 1)
        self.help_button = QPushButton("Help", row)
        self.help_button.setToolTip("Show BOM estimator assumptions and limitations")
        self.help_button.clicked.connect(self.show_help)
        layout.addWidget(self.help_button, 0)
        return row

    def _build_library_group(self) -> QGroupBox:
        """Build the parts-database selector and the directory it is read from."""
        box = QGroupBox("Parts library", self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(6)

        picker = QHBoxLayout()
        picker.setSpacing(8)
        picker.addWidget(QLabel("Parts Library:", box))
        self.library_choice = QComboBox(box)
        self.library_choice.setObjectName("library-choice")
        self.library_choice.setToolTip(
            "Which variant of the offline parts database to read. The default "
            "excludes parts that have been out of stock for a year, which is "
            "most of the file and none of the parts you can buy."
        )
        # Insertion order of dblib.LIBRARY_CONFIGS, so the two halves of the
        # migration list the same options in the same order.
        for key, config in dblib.LIBRARY_CONFIGS.items():
            self.library_choice.addItem(config.display_name, key)
        self._select_library(self._setting("library", "selected_library"))
        self.library_choice.currentIndexChanged.connect(self._on_library_changed)
        picker.addWidget(self.library_choice, 1)
        layout.addLayout(picker)

        directory = QHBoxLayout()
        directory.setSpacing(8)
        directory.addWidget(QLabel("Database directory:", box))
        self.data_path = QLineEdit(
            str(self._setting("library", "data_path") or ""), box
        )
        self.data_path.setObjectName("data-path")
        self.data_path.setPlaceholderText(
            "Leave blank for the default under the plugin's data directory"
        )
        self.data_path.setToolTip(
            "Where the parts database, the part cache, the mappings table and "
            "the global corrections database are kept. Both halves of the "
            "migration read the same directory, so changing it moves both."
        )
        # Show the start of a long path rather than its end. QLineEdit leaves
        # the cursor after the text it was given, which scrolls a database
        # directory to the last thirty characters — the half that is the same
        # on every machine.
        self.data_path.setCursorPosition(0)
        self.data_path.editingFinished.connect(self._on_data_path_edited)
        directory.addWidget(self.data_path, 1)

        self.browse_button = QPushButton("Browse", box)
        self.browse_button.clicked.connect(self.browse_for_directory)
        directory.addWidget(self.browse_button, 0)
        layout.addLayout(directory)

        note = QLabel(
            "A library or directory change takes effect the next time the part "
            "list is rebuilt.",
            box,
        )
        note.setProperty("role", "status")
        note.setWordWrap(True)
        layout.addWidget(note)
        return box

    # -- state ----------------------------------------------------------------

    def _setting(self, section: str, key: str, default=None):
        """Read one setting, tolerating there being no Settings object."""
        if self.settings is None:
            return default
        return self.settings.get(section, key, default)

    def _store(self, section: str, key: str, value) -> None:
        """Persist one setting and announce it."""
        if self.settings is not None:
            self.settings.set(section, key, value)
        log.debug("Setting %s.%s = %r", section, key, value)
        self.changed.emit(section, key, value)

    def _select_library(self, key) -> None:
        """Select the stored library variant, falling back to the default."""
        index = self.library_choice.findData(key)
        if index < 0:
            index = self.library_choice.findData(dblib.DEFAULT_LIBRARY)
        self.library_choice.setCurrentIndex(max(0, index))

    def _on_library_changed(self, index: int) -> None:
        """Persist the parts-database variant."""
        key = self.library_choice.itemData(index)
        if key:
            self._store("library", "selected_library", key)

    def _on_data_path_edited(self) -> None:
        """Persist a typed directory, if it changed.

        ``editingFinished`` fires on focus loss as well as on Return, so this
        runs on every trip through the field. Comparing first keeps the settings
        file from being rewritten — and the part list from being rebuilt — every
        time the user tabs past it.
        """
        typed = self.data_path.text().strip()
        if typed == str(self._setting("library", "data_path") or ""):
            return
        if typed and not os.path.isdir(typed):
            # Stored anyway: the directory may be on a volume that is not
            # mounted yet, and refusing the value would lose a correct path for
            # a temporary reason. open_library already degrades to blank columns
            # rather than refusing to open the window.
            log.warning("Database directory %s does not exist yet", typed)
        self._store("library", "data_path", typed)

    def browse_for_directory(self) -> None:
        """Pick the database directory with a file dialog."""
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Database directory",
            self.data_path.text().strip() or os.path.expanduser("~"),
        )
        if not chosen:
            return
        self.data_path.setText(chosen)
        self._on_data_path_edited()

    def show_help(self) -> None:
        """Show the shared BOM estimator help text.

        The same string the main window's Help button shows, from
        ``bom_estimation/help_text.py`` — one wording, in one place, as that
        module exists to guarantee.
        """
        QMessageBox.information(
            self,
            bom_help_text.BOM_ESTIMATOR_HELP_TITLE,
            bom_help_text.get_bom_estimator_help_text(),
            QMessageBox.StandardButton.Ok,
        )

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Close on the shortcuts every other window here binds."""
        if event.key() == Qt.Key.Key_Escape and (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.close()
            return
        super().keyPressEvent(event)


__all__ = ["DEFAULT_SIZE", "TOGGLES", "InvertedToggle", "SettingsDialog"]
