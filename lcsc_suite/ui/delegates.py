"""Match highlighting on the LCSC Params column.

The Qt port of ``dataview_highlight.HighlightedTextRenderer``, and a good deal
smaller — not because the feature shrank, but because every wx problem that file
works around is absent here. Qt hands a delegate the real cell rectangle, so
there is no renderer painting into a width it was not given; and a column width
set before the window is shown survives, so there is no "restate the widths in
_on_first_shown" dance either.

**What is being highlighted is not a search.** The terms come from the row's own
Value and Footprint columns, so the highlight marks the parts of the *derived*
LCSC params that corroborate what the board declares. A `100K` resistor in an
`R_0402_1005Metric` footprint highlights `100kΩ` and `0402` inside
`100kΩ ±1% 0402`, and a row where nothing lights up is one where the derived
parameters and the board disagree — which is exactly the row worth looking at
twice.

The term expansion itself is *not* reimplemented here. ``expand_value`` and
``expand_footprint`` encode which spellings count as the same thing — `390R` is
`390Ω`, `10uF` is `10µF` is `10µ` — and that is domain knowledge with a long
tail, not formatting. They live in ``highlight_terms.py`` and are shared with
the wx plugin so the two halves cannot drift.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..shared import highlight_terms
from . import theme
from .models.part_table import MATCH_TERMS_ROLE

#: Left and right padding Qt's own text drawing applies inside a cell. Matched
#: by hand because the document is laid out independently of the style.
_TEXT_MARGIN = 4


class MatchHighlightDelegate(QStyledItemDelegate):
    """Paint a cell's text with the matching spans tinted.

    Falls back to the base implementation whenever there is nothing to mark, so
    an unhighlighted cell is painted by Qt exactly as every other cell is — the
    two must be indistinguishable apart from the colour of the matched runs.
    """

    def __init__(self, parent=None, enabled: bool = True) -> None:
        super().__init__(parent)
        self._enabled = enabled

    def set_enabled(self, enabled: bool) -> None:
        """Turn highlighting on or off; the Settings dialog's toggle."""
        self._enabled = bool(enabled)

    def paint(self, painter, option, index) -> None:
        """Draw the cell, tinting the spans that match the row's own terms."""
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        spans = self._spans(text, index)
        if not spans:
            super().paint(painter, option, index)
            return

        # Let the style paint the background, selection and focus ring, then put
        # the text on top. Painting those by hand is how a delegate ends up
        # looking subtly unlike the rest of the table on one platform.
        style_option = QStyleOptionViewItem(option)
        self.initStyleOption(style_option, index)
        style_option.text = ""
        widget = style_option.widget
        style = widget.style() if widget is not None else None
        if style is not None:
            style.drawControl(
                QStyle.ControlElement.CE_ItemViewItem, style_option, painter, widget
            )

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        document = self._document(text, spans, option, selected)

        painter.save()
        rect = option.rect.adjusted(_TEXT_MARGIN, 0, -_TEXT_MARGIN, 0)
        # Vertically centre the single line of text in the row.
        offset = (rect.height() - document.size().height()) / 2
        painter.translate(rect.left(), rect.top() + max(0.0, offset))
        document.drawContents(painter, QRectF(0, 0, rect.width(), rect.height()))
        painter.restore()

    # -- internals ----------------------------------------------------------

    def _spans(self, text: str, index) -> list:
        """Return the spans to tint, or an empty list to defer to Qt."""
        if not self._enabled or not text:
            return []
        terms = index.data(MATCH_TERMS_ROLE)
        if not terms:
            return []
        return highlight_terms.find_highlight_spans(text, list(terms))

    def _document(self, text: str, spans, option, selected: bool) -> QTextDocument:
        """Lay the cell's text out with the matched runs recoloured."""
        document = QTextDocument()
        document.setDefaultFont(option.font)
        document.setDocumentMargin(0)
        document.setPlainText(text)

        cursor = QTextCursor(document)
        base = QTextCharFormat()
        base.setForeground(
            option.palette.highlightedText() if selected else option.palette.text()
        )
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.mergeCharFormat(base)

        marked = QTextCharFormat()
        marked.setForeground(theme.highlight_ink(selected))
        marked.setFontWeight(700)
        for start, end in spans:
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(marked)
        return document
