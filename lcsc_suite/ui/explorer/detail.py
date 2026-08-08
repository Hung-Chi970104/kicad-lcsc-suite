"""The selected part's detail pane: previews, both stock cards, caveats, params.

The Qt port of the wx explorer's ``_build_detail_panel`` / ``_layout_detail_panel``
/ ``_layout_availability`` / ``_apply_detail_minimums`` group and of its
``StockCard``.

Two layouts, the same widgets. ``side`` stacks them in a column beside the
catalogue; ``below`` sets them in a row inside a full-width expanded result row.
The wx original had to detach and re-add every child from its sizers to switch,
having first computed different minimum sizes for each arrangement — because a
``wxBoxSizer`` multiplies a stretchable child's minimum by the total proportion,
so a comfortable minimum in one layout inflated the other past the width it had.
Here the two arrangements are two ``QLayout``s and switching is
``QStackedLayout``-free reparenting of three blocks; Qt's size policies do the
rest.

**Both stock cards are always built, and only one is shown.** Which one depends
on the Inventory selector, and that is the point: JLC assembly and LCSC retail
are separate warehouses whose figures routinely differ by orders of magnitude,
so each keeps its own card, its own colour and its own caveats. Never collapse
them into one number.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from .preview import PreviewTile
from .results import UNKNOWN, format_count

#: Padding inside the pane. Its content is dense — three previews, a stock card,
#: a caveat list and a parameter table — and whitespace is the whole difference
#: between that reading as a dashboard and as a wall.
DETAIL_PAD = 8

#: The assembly endpoint spells the library type on the wire; the search
#: response spells it for people. ``SearchHit`` already maps it, ``StockReport``
#: deliberately does not — it reports what the endpoint said — so the card ended
#: up reading "expand part". Mapped here rather than in ``api.py``, which is
#: copied and not edited: if a UI need seems to require an API change, change
#: the UI. Found by looking at the screenshot, which is the point of the rule.
_LIBRARY_TYPES = {"base": "Basic", "expand": "Extended"}


def library_label(value) -> str:
    """Name a library type for display, whichever spelling arrived."""
    text = str(value or "").strip()
    return _LIBRARY_TYPES.get(text.lower(), text)


class StockCard(QFrame):
    """One inventory's headline figure, in its source colour, with its caveats.

    Painted rather than assembled from labels, as in wx and for the same reason:
    the source identity (a coloured rule and title), the figure and the
    supporting lines have to line up the same way in both cards and in both
    appearances.
    """

    PAD = 12
    LINE_GAP = 3

    def __init__(
        self, title: str, accent: str, footnote: str = "", parent=None
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._accent = accent
        self._footnote = footnote
        self._count: Optional[int] = None
        self._lines: list[str] = []
        self._pending = True
        # 168 wide left 144 for text after the padding, and the footnotes are
        # what did not fit: "what JLC can place on a board" rendered as "what
        # JLC can place on a…", which says the opposite of nothing but not much
        # more. Sized to the longer of the two footnotes rather than to a round
        # number, because that string is the constraint.
        self.setMinimumSize(196, 118)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set_pending(self) -> None:
        """Show the card as still loading."""
        self._pending = True
        self._count = None
        self._lines = []
        self.update()

    def set_value(self, count: Optional[int], lines=None) -> None:
        """Show ``count`` with up to two supporting ``lines``."""
        self._pending = False
        self._count = count
        self._lines = [line for line in (lines or []) if line][:2]
        self.update()

    def value(self) -> Optional[int]:
        """Return the figure on show. For tests."""
        return None if self._pending else self._count

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Paint the card."""
        del event
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            width, height = self.width(), self.height()
            pad = self.PAD
            accent = theme.colour(self._accent)

            painter.setBrush(theme.card_background())
            painter.setPen(theme.colour("rule"))
            painter.drawRoundedRect(0, 0, width - 1, height - 1, 6, 6)

            # A short rule in the source colour: the fastest way to tell the two
            # cards apart without reading either title.
            painter.setBrush(accent)
            painter.setPen(accent)
            painter.drawRect(pad, pad, max(12, min(46, width - 2 * pad)), 3)

            base = self.font()
            painter.setFont(theme.bold(theme.scaled(base, 0.85)))
            painter.setPen(accent)
            title_y = pad + 8
            metrics = painter.fontMetrics()
            painter.drawText(pad, title_y + metrics.ascent(), self._title)
            y = title_y + metrics.height() + self.LINE_GAP

            if self._pending:
                painter.setFont(base)
                painter.setPen(theme.colour("muted"))
                painter.drawText(
                    pad, y + painter.fontMetrics().ascent() + 4, "loading …"
                )
                return

            figure = UNKNOWN if self._count is None else f"{self._count:,}"
            painter.setFont(theme.bold(theme.scaled(base, 1.6)))
            painter.setPen(theme.stock_colour(self._count))
            metrics = painter.fontMetrics()
            painter.drawText(pad, y + metrics.ascent(), figure)
            y += metrics.height() + self.LINE_GAP

            painter.setFont(theme.scaled(base, 0.85))
            painter.setPen(theme.colour("muted"))
            metrics = painter.fontMetrics()
            available = max(20, width - 2 * pad)
            for line in self._lines + ([self._footnote] if self._footnote else []):
                if y > height - pad // 2:
                    break
                painter.drawText(
                    pad,
                    y + metrics.ascent(),
                    metrics.elidedText(line, Qt.TextElideMode.ElideRight, available),
                )
                y += metrics.height() + self.LINE_GAP
        finally:
            painter.end()


class DetailPane(QWidget):
    """Everything known about the selected part, in either of two layouts."""

    #: The photo tile was clicked — the caller opens the full-size viewer.
    photo_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout_mode = "side"

        self.heading = QLabel("Select a part.", self)
        self.heading.setFont(theme.bold(theme.scaled(theme.base_font(), 1.15)))
        self.subheading = QLabel(" ", self)
        self.subheading.setProperty("role", "status")
        self.subheading.setWordWrap(True)

        self.symbol_preview = PreviewTile("Symbol", self)
        self.footprint_preview = PreviewTile("Footprint", self)
        # "Photo (click to enlarge)" is what the wx tile said, and at 140px it
        # elides to "to (click to enla" — which advertises nothing. The tooltip
        # and the cursor carry the rest of the affordance.
        self.photo_preview = PreviewTile("Photo (enlarge)", self, clickable=True)
        self.photo_preview.setToolTip("Click for the full-size product photo")
        self.photo_preview.mousePressEvent = self._on_photo_clicked  # type: ignore[method-assign]

        self._previews = QWidget(self)
        preview_row = QHBoxLayout(self._previews)
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_row.setSpacing(6)
        for tile in (self.symbol_preview, self.footprint_preview, self.photo_preview):
            preview_row.addWidget(tile, 1)

        self.jlc_card = StockCard(
            "JLC ASSEMBLY", "jlc", "what JLC can place on a board"
        )
        self.retail_card = StockCard("LCSC RETAIL", "retail", "what you can buy loose")

        self.warnings = QTextEdit(self)
        self.warnings.setReadOnly(True)
        self.warnings.setFrameShape(QFrame.Shape.NoFrame)
        self.warnings.setMinimumHeight(96)
        self.warnings.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._availability = QGroupBox("Availability", self)
        self._avail_layout = QHBoxLayout(self._availability)
        self._avail_layout.setContentsMargins(6, 4, 6, 6)
        self._avail_layout.setSpacing(6)
        # Top, not centred. The card is a fixed 118px tall inside a group that
        # the previews stretch to ~200, so left to itself it floated in the
        # middle with a band of nothing above and below and its headline figure
        # nowhere near the first caveat line. They are two readings of the same
        # question and they should start on the same line.
        top = Qt.AlignmentFlag.AlignTop
        self._avail_layout.addWidget(self.jlc_card, 0, top)
        self._avail_layout.addWidget(self.retail_card, 0, top)
        self._avail_layout.addWidget(self.warnings, 1)

        self.parameters = QTableWidget(0, 2, self)
        self.parameters.setHorizontalHeaderLabels(["Parameter", "Value"])
        self.parameters.verticalHeader().setVisible(False)
        self.parameters.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.parameters.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.parameters.setAlternatingRowColors(True)
        # Two fixed widths could not serve both layouts in wx: the same 390px
        # that fit the side panel painted names over values in the narrower
        # inline column, so the original bound EVT_SIZE to re-split them.
        # ResizeToContents plus a stretched value column is the whole fix.
        header = self.parameters.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.parameters.setMinimumHeight(90)

        self._parameter_box = QGroupBox("Parameters", self)
        param_layout = QVBoxLayout(self._parameter_box)
        param_layout.setContentsMargins(6, 4, 6, 6)
        param_layout.addWidget(self.parameters)

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(DETAIL_PAD, DETAIL_PAD, DETAIL_PAD, DETAIL_PAD)
        self._root.setSpacing(6)
        self._body: Optional[QWidget] = None
        self.set_layout_mode("side")

    # -- layout --------------------------------------------------------------

    def set_layout_mode(self, mode: str) -> None:
        """Arrange for ``side`` (a column) or ``below`` (a row)."""
        mode = mode if mode in ("side", "below") else "side"
        self._layout_mode = mode

        # Detach everything, then rebuild. Reparenting to None first is what
        # stops a widget being owned by a layout that is about to be deleted.
        for widget in (
            self.heading,
            self.subheading,
            self._previews,
            self._availability,
            self._parameter_box,
        ):
            widget.setParent(self)
        while self._root.count():
            item = self._root.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self._body:
                widget.setParent(self)
        if self._body is not None:
            self._body.setParent(None)
            self._body.deleteLater()
            self._body = None

        self._root.addWidget(self.heading)
        self._root.addWidget(self.subheading)

        if mode == "side":
            self._root.addWidget(self._previews)
            self._root.addWidget(self._availability)
            self._root.addWidget(self._parameter_box, 1)
            self._avail_layout.setDirection(QHBoxLayout.Direction.TopToBottom)
        else:
            body = QWidget(self)
            row = QHBoxLayout(body)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            # The previews go in unstretched, at the natural width of three
            # tiles: they have a fixed aspect and the least to gain from surplus
            # width. What is left is divided between the two blocks that read as
            # text, 13:7 rather than 2:1 because the availability block spends
            # its first 350px on cards of fixed width — the split has to read as
            # "the caveats get a little less than the parameter table".
            row.addWidget(self._previews, 0)
            row.addWidget(self._availability, 13)
            row.addWidget(self._parameter_box, 7)
            self._body = body
            self._root.addWidget(body, 1)
            self._avail_layout.setDirection(QHBoxLayout.Direction.LeftToRight)

    def layout_mode(self) -> str:
        """Return the arrangement in force."""
        return self._layout_mode

    def show_inventory(self, view: str) -> None:
        """Show only the card for the inventory now selected."""
        self.jlc_card.setVisible(view == "jlc")
        self.retail_card.setVisible(view == "retail")

    # -- contents ------------------------------------------------------------

    def show_pending(self, hit) -> None:
        """Fill in what the search result already knows, and mark the rest loading.

        The search response carries assembly stock, library type and the part's
        identity for every row, so the card that matters can be right
        immediately; only the retail figure and the caveats need a lookup.
        """
        self.heading.setText(f"{hit.lcsc}  ·  {hit.model or '—'}")
        self.subheading.setText(
            " · ".join(p for p in (hit.brand, hit.package, hit.category) if p) or " "
        )
        self.jlc_card.set_value(
            hit.stock, [f"{hit.library_type} part" if hit.library_type else ""]
        )
        self.retail_card.set_pending()
        self.warnings.setPlainText("")
        self.parameters.setRowCount(0)
        for tile in (self.symbol_preview, self.footprint_preview, self.photo_preview):
            tile.clear("Loading …")

    def show_report(self, hit, report, quantity: int) -> None:
        """Render a completed availability report."""
        library_type = library_label(report.library_type or hit.library_type)
        self.jlc_card.set_value(
            report.jlc_stock if report.jlc_stock is not None else hit.stock,
            [
                f"{library_type} part" if library_type else "",
                f"min purchase {report.min_purchase:,}" if report.min_purchase else "",
            ],
        )
        detail = []
        if report.retail_domestic is not None or report.retail_overseas is not None:
            detail.append(
                f"CN {format_count(report.retail_domestic)} · "
                f"intl {format_count(report.retail_overseas)}"
            )
        if report.retail_min_buy:
            detail.append(f"min buy {report.retail_min_buy:,}")
        self.retail_card.set_value(report.retail_stock, detail)

        lines: list[str] = []
        if report.retail_ladder:
            from ...shared import lcsc_api as api  # noqa: PLC0415 - local to the use

            unit = api.unit_price_at(report.retail_ladder, quantity)
            if unit is not None:
                lines.append(
                    f"LCSC retail unit price at qty {quantity}: ${unit:.4f} "
                    f"(total ${unit * quantity:.2f})"
                )
        lines.extend(report.warnings)
        self.warnings.setPlainText("\n".join(f"• {line}" for line in lines))

        rows = list(report.parameters) or list(hit.attributes.items())
        self.parameters.setRowCount(len(rows))
        for row, (name, value) in enumerate(rows):
            self.parameters.setItem(row, 0, QTableWidgetItem(str(name)))
            self.parameters.setItem(row, 1, QTableWidgetItem(str(value)))

    def show_previews(self, symbol: Optional[str], footprint: Optional[str]) -> None:
        """Render the two drawings."""
        self.symbol_preview.set_svg(symbol, "No EasyEDA symbol for this part")
        self.footprint_preview.set_svg(footprint, "No EasyEDA footprint for this part")

    def show_photo(self, data: Optional[bytes]) -> None:
        """Render the product photo."""
        self.photo_preview.set_image_bytes(data, "Photo unavailable")

    def _on_photo_clicked(self, event) -> None:
        """Report a click on the photo tile."""
        del event
        self.photo_clicked.emit()


__all__ = ["DETAIL_PAD", "DetailPane", "StockCard"]
