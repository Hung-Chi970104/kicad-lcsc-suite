"""Naming an LCSC number by hand.

The wx plugin has no dialog like this: its ``Assign LCSC number`` button opens
the Explorer, and the only way to type a number in is to put one on the clipboard
and use ``Paste LCSC``. So this is not a new capability — it is the clipboard
route with a text field instead of a clipboard, and it exists because the
Explorer is Phase 4 and a phase has to end with the app runnable.

It earns its place past Phase 4 too: someone who already knows the number reaches
it in two keystrokes instead of a search. The Explorer becomes a *second* caller
of the same :meth:`lcsc_suite.parts.PartList.assign`, not a replacement for this
one.

The one rule worth stating: **OK stays disabled until the text contains a
number**. Assignment writes to the board, and a dialog that accepts nonsense and
then reports a failure has made the user do the validation twice.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout

from ..kicad_bridge import sanitize_lcsc

#: How many references to name before giving up and counting them. Eight fits
#: the dialog's width; a hundred-part selection is a number, not a list.
MAX_NAMED = 8

#: Wide enough that a pasted product URL is readable rather than scrolled out of
#: its own field — which is the input this dialog most has to be trusted with,
#: because the number it extracts is the one that reaches the board.
MINIMUM_WIDTH = 420


def describe(references) -> str:
    """Name the selection, or count it once naming stops being useful."""
    references = list(references)
    if not references:
        return "no footprints"
    if len(references) == 1:
        return references[0]
    if len(references) <= MAX_NAMED:
        return f"{', '.join(references[:-1])} and {references[-1]}"
    listed = ", ".join(references[:MAX_NAMED])
    return f"{listed} and {len(references) - MAX_NAMED} more"


class AssignNumberDialog(QDialog):
    """Ask for an LCSC number to put on the selected footprints."""

    def __init__(self, parent=None, references=(), current: str = "") -> None:
        super().__init__(parent)
        self.setObjectName("assign-number-dialog")
        self.setWindowTitle("Assign LCSC number")
        self.setModal(True)
        self.setMinimumWidth(MINIMUM_WIDTH)
        self._references = list(references)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        count = len(self._references)
        heading = QLabel(
            f"Assign an LCSC number to {count} footprint{'' if count == 1 else 's'}:",
            self,
        )
        heading.setObjectName("assign-heading")
        layout.addWidget(heading)

        target = QLabel(describe(self._references), self)
        target.setObjectName("assign-targets")
        target.setProperty("role", "status")
        target.setWordWrap(True)
        target.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(target)

        self.input = QLineEdit(current, self)
        self.input.setObjectName("assign-input")
        # A whole product URL is a legitimate paste — sanitize_lcsc pulls the
        # number out of one — so the field is not restricted to C+digits.
        self.input.setPlaceholderText("C1524, or paste an LCSC page URL")
        self.input.setClearButtonEnabled(True)
        layout.addWidget(self.input)

        self.hint = QLabel("", self)
        self.hint.setObjectName("assign-hint")
        self.hint.setProperty("role", "status")
        layout.addWidget(self.hint)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Assign")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.input.textChanged.connect(self._on_text_changed)
        self._on_text_changed(self.input.text())
        self.input.setFocus()

    def number(self) -> str:
        """Return the LCSC number the user named, or ``""``."""
        return sanitize_lcsc(self.input.text())

    def _on_text_changed(self, text: str) -> None:
        """Enable Assign only once the text actually contains a number."""
        number = sanitize_lcsc(text)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(number))
        if not text.strip():
            self.hint.setText("")
        elif number and number != text.strip().upper():
            # Say what was extracted, so a paste that found the wrong number in
            # a long string is visible before it reaches the board.
            self.hint.setText(f"Will assign {number}")
        elif number:
            self.hint.setText("")
        else:
            self.hint.setText("No LCSC number in that — expected C followed by digits")
