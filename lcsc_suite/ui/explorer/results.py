"""The results grid: a table model over search hits, and the cells that draw it.

The Qt port of the wx explorer's ``DataViewListCtrl`` and its four custom
renderers. Three things shrank on the way across, all for the same underlying
reason — Qt draws its own widgets, so there is no native control to fight:

* **The column-width machinery is gone.** The wx version carried
  ``FLEX_WEIGHTS``, ``SHRINK_ORDER``, ``MIN_SHARE``, ``_squeeze()`` and a
  ``_measure_grid_metrics()`` that read the platform's undeclared header height,
  row indent and per-column padding off a populated grid — about 150 lines,
  written because the macOS DataView redistributes every width whenever one
  changes and adds overhead it will not report. ``QHeaderView`` has resize modes.
  :func:`configure_header` is the whole replacement.
* **Renderers are not told their own width.** ``CatalogTextCell.set_cell_width``
  and its two siblings existed because a wx custom renderer is handed the rect
  it asked for in ``GetSize()``, not its column's — which is what once wrapped
  "Multilayer Ceramic Capacitor" inside a 100px box in a 470px column. A
  ``QStyledItemDelegate`` is handed ``option.rect``, correct by construction.
* **The thumbnail can live in the model.** wx could not hold an invalid
  ``wx.Bitmap`` in a ``DataViewListCtrl``, so the cell value was the LCSC code
  and the renderer looked the artwork up through a callback. ``QPixmap`` has a
  null state, so a photo that has not arrived is just a null pixmap.

What did *not* change is the meaning of the three stock spellings, which the
model is careful to keep apart because conflating them is the bug this fork
exists to fix:

``…``  not asked yet — the retail fill has not reached this row
``?``  asked, and nobody answered
``0``  asked, and the answer was none
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QHeaderView, QStyle, QStyledItemDelegate, QTableView

from .. import theme

#: ``(key, header, width)``. ``key`` is how the rest of the package refers to a
#: column; the grid's order is this list's order.
#:
#: Ordered after LCSC's own results table — photo, part identity, description,
#: then the numbers — because that is the order the decision gets made in.
#: Related fields are stacked inside a row rather than spread across twelve
#: narrow columns: model over LCSC/type, manufacturer over package, unit price
#: over minimum order. That keeps the decision data visible while leaving room
#: for a genuinely useful photo.
COLUMNS: list[tuple[str, str, int]] = [
    ("photo", "", 112),
    ("part", "Part", 260),
    ("description", "Description", 420),
    ("manufacturer", "Manufacturer / Package", 200),
    ("jlc_stock", "JLC assembly", 120),
    ("retail_stock", "LCSC retail", 120),
    ("price", "Unit price", 120),
]
COLUMN_INDEX = {key: index for index, (key, _label, _width) in enumerate(COLUMNS)}

#: Thumbnail edge and the row height that holds it. A product photo is the
#: fastest way to see that a search for "0402" has handed you a resistor array
#: or a through-hole part — the mistake the grid could not previously show.
THUMB_PX = 108
ROW_HEIGHT_PX = 140

#: The height the inline detail row asks for, and how much of the visible grid
#: it may claim on the way there. Three rows' worth is genuinely comfortable,
#: but not at the cost of leaving no results on screen around it — the pane is
#: an *expanded row*, and a row that fills the viewport is a page.
INLINE_DETAIL_PX = 400
INLINE_DETAIL_MAX_SHARE = 0.62
#: Below this the pane stops being readable and a scrollbar is the lesser evil.
INLINE_DETAIL_MIN_PX = 240


def inline_detail_height(viewport_height: int, wanted: int = 0) -> int:
    """Return the height the inline detail row should take.

    The wx version worked in whole rows, because its ``SetRowHeight`` applied to
    every row at once and the pane had to be built out of a whole number of
    140px placeholders. A ``QTableView`` sizes rows individually, so this is
    just a clamp.

    ``wanted`` is the pane's own size hint, and passing it is what stops the row
    being 400px of which the bottom 110 are empty. The constant remains as the
    ceiling: a pane that asks for more than that is asking for the whole grid.
    """
    ceiling = min(INLINE_DETAIL_PX, int(viewport_height * INLINE_DETAIL_MAX_SHARE))
    return max(INLINE_DETAIL_MIN_PX, min(wanted or INLINE_DETAIL_PX, ceiling))


#: Grid text for a cell not yet fetched, versus one where the endpoint answered
#: but had nothing to say. Conflating them shows a part as out of stock when we
#: simply have not looked.
PENDING = "…"
UNKNOWN = "?"

#: Roles the delegates read. ``SECONDARY`` is the quieter line under a cell's
#: primary value; ``STOCK`` is the figure behind a formatted string, so the
#: colour is chosen from a number rather than by parsing the text back — which
#: is what the wx renderer had to do, ``DataViewListCtrl`` storing only strings.
SECONDARY_ROLE = Qt.ItemDataRole.UserRole + 1
STOCK_ROLE = Qt.ItemDataRole.UserRole + 2
PIXMAP_ROLE = Qt.ItemDataRole.UserRole + 3
HIT_ROLE = Qt.ItemDataRole.UserRole + 4
#: True on the placeholder row the "Inline below" layout puts the detail pane in.
INLINE_ROLE = Qt.ItemDataRole.UserRole + 5


def format_count(value: Optional[int]) -> str:
    """Format a stock figure, keeping ``None`` distinct from zero."""
    return UNKNOWN if value is None else f"{value:,}"


class ResultsModel(QAbstractTableModel):
    """The fetched search hits, as rows.

    Holds the hits, the retail figures fetched for them so far and the decoded
    thumbnails. ``None`` and "absent" mean different things in both of the
    latter two dictionaries and the distinction is load-bearing:

    * ``_retail[lcsc] is None`` — asked, no answer. Draws ``?``.
    * ``lcsc not in _retail`` — not asked yet. Draws ``…``.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._hits: list = []
        self._retail: dict[str, Optional[int]] = {}
        self._thumbs: dict[str, Optional[QPixmap]] = {}
        self._show_retail = False
        #: Index of the placeholder row carrying the inline detail pane, or -1.
        self._inline_row = -1

    # -- contents ------------------------------------------------------------

    def set_hits(self, hits) -> None:
        """Replace the visible result set."""
        self.beginResetModel()
        self._hits = list(hits)
        self._inline_row = -1
        self.endResetModel()

    def hits(self) -> list:
        """Return the visible hits, without the inline placeholder."""
        return list(self._hits)

    def hit_at(self, row: int):
        """Return the hit on ``row``, or ``None`` for the inline placeholder."""
        if row < 0 or row >= self.rowCount():
            return None
        if self._inline_row >= 0:
            if row == self._inline_row:
                return None
            if row > self._inline_row:
                row -= 1
        return self._hits[row] if row < len(self._hits) else None

    def row_of(self, lcsc: str) -> int:
        """Return the display row showing ``lcsc``, or -1."""
        for index, hit in enumerate(self._hits):
            if hit.lcsc == lcsc:
                return index + (1 if 0 <= self._inline_row <= index else 0)
        return -1

    def set_show_retail(self, shown: bool) -> None:
        """Note whether the retail column is on show, so cells format for it."""
        if shown == self._show_retail:
            return
        self._show_retail = shown
        self._repaint_column("retail_stock")

    def set_retail(self, lcsc: str, stock: Optional[int]) -> None:
        """Record one retail figure. ``None`` means "asked, no answer"."""
        self._retail[lcsc] = stock
        row = self.row_of(lcsc)
        if row >= 0:
            index = self.index(row, COLUMN_INDEX["retail_stock"])
            self.dataChanged.emit(index, index)

    def retail(self) -> dict:
        """Return every retail figure fetched so far."""
        return dict(self._retail)

    def known_retail(self, lcsc: str) -> Optional[int]:
        """Return the retail figure for ``lcsc``, or ``None`` if unknown."""
        return self._retail.get(lcsc)

    def asked_retail(self, lcsc: str) -> bool:
        """Report whether a retail lookup has been recorded for ``lcsc``."""
        return lcsc in self._retail

    def forget_fetched(self) -> None:
        """Drop retail figures and thumbnails — what Refresh does."""
        self._retail = {}
        self._thumbs = {}

    def set_thumbnail(self, lcsc: str, pixmap: Optional[QPixmap]) -> None:
        """Record one decoded thumbnail; ``None`` means "asked, no photo"."""
        self._thumbs[lcsc] = pixmap
        row = self.row_of(lcsc)
        if row >= 0:
            index = self.index(row, COLUMN_INDEX["photo"])
            self.dataChanged.emit(index, index)

    def has_thumbnail(self, lcsc: str) -> bool:
        """Report whether a thumbnail lookup has been recorded for ``lcsc``."""
        return lcsc in self._thumbs

    # -- the inline detail placeholder ---------------------------------------

    def set_inline_row(self, after: int) -> None:
        """Insert a full-width placeholder row below display row ``after``.

        This is how "Inline below" is done — an actual row in the model, spanned
        across every column by the view, with the detail pane set on it as an
        index widget. The wx version could not do that: a native DataView will
        not host a child window, so the details were a *sibling* panel clipped
        to the rectangle of some reserved placeholder rows, repositioned by a
        100ms timer because scroll notifications from that control are not
        dependable. That timer, ``inline_clip``, ``_position_inline_detail`` and
        ``_inline_placed`` all exist to keep an overlay glued to rows it is not
        part of. Here the row scrolls because it is a row.
        """
        if after < 0 or after == self._inline_row:
            self.clear_inline_row()
            return
        # ``after`` is a display row, and clearing the old placeholder renumbers
        # every row under it. Translated to an index into ``_hits`` first, so
        # that a placeholder sitting above the new anchor does not push the pane
        # one part further down the grid.
        anchor = after - (1 if 0 <= self._inline_row < after else 0)
        self.clear_inline_row()
        if anchor >= len(self._hits):
            return
        target = anchor + 1
        self.beginInsertRows(QModelIndex(), target, target)
        self._inline_row = target
        self.endInsertRows()

    def clear_inline_row(self) -> None:
        """Remove the placeholder row, if one is present."""
        if self._inline_row < 0:
            return
        row = self._inline_row
        self.beginRemoveRows(QModelIndex(), row, row)
        self._inline_row = -1
        self.endRemoveRows()

    def inline_row(self) -> int:
        """Return the placeholder row's index, or -1."""
        return self._inline_row

    # -- QAbstractTableModel -------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008 - Qt override
        """Return the number of display rows, placeholder included."""
        if parent.isValid():
            return 0
        return len(self._hits) + (1 if self._inline_row >= 0 else 0)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008 - Qt
        """Return the number of columns."""
        del parent
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        """Return a column header."""
        if (
            orientation is not Qt.Orientation.Horizontal
            or role != Qt.ItemDataRole.DisplayRole
        ):
            return None
        return COLUMNS[section][1]

    def flags(self, index):
        """Make the inline placeholder unselectable; it is not a result."""
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        if index.row() == self._inline_row:
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        """Return one cell's value for ``role``."""
        if not index.isValid():
            return None
        row, column = index.row(), index.column()
        if row == self._inline_row:
            return True if role == INLINE_ROLE else None

        hit = self.hit_at(row)
        if hit is None:
            return None
        key = COLUMNS[column][0]

        if role == HIT_ROLE:
            return hit
        if role == PIXMAP_ROLE:
            return self._thumbs.get(hit.lcsc) if key == "photo" else None
        if role == STOCK_ROLE:
            if key == "jlc_stock":
                return hit.stock
            if key == "retail_stock":
                return self._retail.get(hit.lcsc)
            return None
        if role == SECONDARY_ROLE:
            return self._secondary(hit, key)
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(hit, key)
        if role == Qt.ItemDataRole.TextAlignmentRole and key in (
            "jlc_stock",
            "retail_stock",
        ):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def _display(self, hit, key: str) -> str:
        """Return a cell's primary line."""
        if key == "part":
            return hit.model or "—"
        if key == "description":
            return hit.description or "—"
        if key == "manufacturer":
            return hit.brand or "—"
        if key == "jlc_stock":
            return format_count(hit.stock)
        if key == "retail_stock":
            if not self._show_retail:
                return ""
            if hit.lcsc not in self._retail:
                return PENDING
            return format_count(self._retail[hit.lcsc])
        if key == "price":
            return f"${hit.price:.4f}" if hit.price is not None else "—"
        return ""

    def _secondary(self, hit, key: str) -> str:
        """Return a cell's quieter supporting line."""
        if key == "part":
            return " · ".join(v for v in (hit.lcsc, hit.library_type) if v)
        if key == "description":
            return hit.category or ""
        if key == "manufacturer":
            return hit.package or "Package not specified"
        if key == "price":
            return f"Min order {hit.min_qty or 1:,}"
        return ""

    def _repaint_column(self, key: str) -> None:
        """Tell the view one column's cells changed."""
        rows = self.rowCount()
        if not rows:
            return
        column = COLUMN_INDEX[key]
        self.dataChanged.emit(self.index(0, column), self.index(rows - 1, column))


# ---------------------------------------------------------------------------
# Delegates
# ---------------------------------------------------------------------------


class CatalogDelegate(QStyledItemDelegate):
    """A primary value over quieter supporting metadata, on a roomy row."""

    def __init__(self, parent=None, bold: bool = False, primary_lines: int = 1) -> None:
        super().__init__(parent)
        self._bold = bold
        self._primary_lines = primary_lines

    def paint(self, painter, option, index) -> None:
        """Paint the two-tier cell."""
        if index.data(INLINE_ROLE):
            return
        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else None
        if style is not None:
            # Selection fill and focus rect, drawn by the style so the row looks
            # the same as every other selected row in the app. The text is
            # suppressed first: we draw it ourselves, in two tiers.
            option.text = ""
            style.drawControl(
                QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget
            )

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        ink = (
            option.palette.highlightedText().color()
            if selected
            else option.palette.text().color()
        )
        primary = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        secondary = str(index.data(SECONDARY_ROLE) or "")

        rect = option.rect.adjusted(8, 6, -8, -6)
        painter.save()
        painter.setClipRect(option.rect)

        base = option.font
        primary_font = theme.bold(base) if self._bold else base
        painter.setFont(primary_font)
        painter.setPen(ink)
        metrics = painter.fontMetrics()
        primary_height = metrics.height() * self._primary_lines

        secondary_font = theme.scaled(base, 0.86)
        secondary_height = 0
        if secondary:
            painter.setFont(secondary_font)
            secondary_height = painter.fontMetrics().height() * 2
            painter.setFont(primary_font)

        gap = 5 if secondary else 0
        block = min(rect.height(), primary_height + secondary_height + gap)
        top = rect.top() + max(0, (rect.height() - block) // 2)

        primary_rect = QRect(rect.left(), top, rect.width(), primary_height)
        painter.drawText(
            primary_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            | int(Qt.TextFlag.TextWordWrap),
            metrics.elidedText(
                primary, Qt.TextElideMode.ElideRight, rect.width() * self._primary_lines
            ),
        )

        if secondary:
            painter.setFont(secondary_font)
            painter.setPen(ink if selected else theme.colour("muted"))
            secondary_rect = QRect(
                rect.left(),
                top + primary_height + gap,
                rect.width(),
                max(0, rect.bottom() - (top + primary_height + gap)),
            )
            painter.drawText(
                secondary_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                | int(Qt.TextFlag.TextWordWrap),
                painter.fontMetrics().elidedText(
                    secondary,
                    Qt.TextElideMode.ElideRight,
                    secondary_rect.width() * 2,
                ),
            )
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt override
        """Ask for the tall catalogue row."""
        del option, index
        return QSize(80, ROW_HEIGHT_PX)


class StockDelegate(QStyledItemDelegate):
    """A stock figure, right-aligned and coloured by how healthy it is.

    A grid of five- and seven-digit numbers is unreadable at a glance; colour
    turns it into something scannable. The number comes from :data:`STOCK_ROLE`
    rather than being parsed back out of the formatted text, which is what the
    wx renderer had to do.
    """

    def paint(self, painter, option, index) -> None:
        """Paint the coloured figure."""
        if index.data(INLINE_ROLE):
            return
        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else None
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if style is not None:
            option.text = ""
            style.drawControl(
                QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget
            )
        if not text:
            return

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            # The selection bar already carries meaning; a coloured number on
            # top of it is worse than a plain readable one.
            ink: QColor = option.palette.highlightedText().color()
        elif text in (PENDING, UNKNOWN):
            ink = theme.colour("unknown")
        else:
            ink = theme.stock_colour(index.data(STOCK_ROLE))

        painter.save()
        painter.setClipRect(option.rect)
        painter.setFont(theme.bold(theme.scaled(option.font, 1.05)))
        painter.setPen(ink)
        painter.drawText(
            option.rect.adjusted(4, 0, -8, 0),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            text,
        )
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt override
        """Ask for the tall catalogue row."""
        del option, index
        return QSize(80, ROW_HEIGHT_PX)


class ThumbnailDelegate(QStyledItemDelegate):
    """The row's product photo, or a frame while it is missing.

    A frame rather than nothing: an empty cell reads as "this part has no
    photo", which is usually wrong and always unhelpful.
    """

    def paint(self, painter, option, index) -> None:
        """Paint the thumbnail centred in its cell."""
        if index.data(INLINE_ROLE):
            return
        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else None
        if style is not None:
            option.text = ""
            style.drawControl(
                QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget
            )

        pixmap = index.data(PIXMAP_ROLE)
        painter.save()
        painter.setClipRect(option.rect)
        if pixmap is None or pixmap.isNull():
            side = min(THUMB_PX, option.rect.height() - 8, option.rect.width() - 8)
            if side > 6:
                painter.setPen(theme.colour("rule"))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(
                    QRect(
                        option.rect.left() + (option.rect.width() - side) // 2,
                        option.rect.top() + (option.rect.height() - side) // 2,
                        side,
                        side,
                    ),
                    3,
                    3,
                )
        else:
            painter.drawPixmap(
                option.rect.left() + (option.rect.width() - pixmap.width()) // 2,
                option.rect.top() + (option.rect.height() - pixmap.height()) // 2,
                pixmap,
            )
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt override
        """Reserve a square big enough for the thumbnail."""
        del option, index
        return QSize(THUMB_PX + 4, ROW_HEIGHT_PX)


def decode_thumbnail(data: Optional[bytes], size: int = THUMB_PX) -> Optional[QPixmap]:
    """Decode image bytes into a square-fitted pixmap of at most ``size`` px.

    Returns ``None`` for missing or undecodable bytes; the caller records that
    as "asked, nothing to show" so the part is not fetched again. Never upscales
    — LCSC's smallest thumbnail is already larger than a grid row, and a blown-up
    one looks worse than a small centred one.

    Safe on a worker thread, unlike its wx counterpart: ``QPixmap`` is not, but
    this returns one built from a ``QImage``, and the caller decodes on the UI
    thread for exactly that reason.
    """
    if not data or size < 4:
        return None
    pixmap = QPixmap()
    if not pixmap.loadFromData(data):
        return None
    if pixmap.width() > size or pixmap.height() > size:
        pixmap = pixmap.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pixmap


#: Columns whose width does not change with the window. A stock figure is
#: already given room for its largest plausible value, so widening it just moves
#: the number further from its neighbour; a thumbnail does not reflow at all.
FIXED_WIDTHS = {"photo": 112, "jlc_stock": 120, "retail_stock": 120, "price": 120}

#: How the remaining width is shared between the three columns that hold text
#: and therefore genuinely reflow into it.
FLEX_WEIGHTS = {"part": 0.28, "description": 0.48, "manufacturer": 0.24}

#: How far each may be squeezed before a horizontal scrollbar becomes the lesser
#: evil. A row that fits the window beats a full-width description — nobody
#: should scroll sideways to read a result — but only down to a point.
FLEX_FLOORS = {"part": 118, "description": 150, "manufacturer": 96}


class ResultsView(QTableView):
    """The grid, which re-fits its own columns whenever its viewport changes.

    A subclass rather than a hook on each caller, because "the width changed" has
    four causes — the window resized, the splitter moved, the detail pane opened
    or closed, a stock column was hidden — and only the view sees all of them.
    Wiring three of them by hand is how the inline layout first rendered a
    724px-wide expanded row inside a 1440px grid: the pane had left the splitter,
    the columns had not been told.
    """

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Re-fit the columns to the viewport's new width."""
        super().resizeEvent(event)
        fit_columns(self)


def configure_header(view) -> None:
    """Set up the header. Widths come from :func:`fit_columns`."""
    header = view.horizontalHeader()
    for index in range(len(COLUMNS)):
        header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(False)
    header.setHighlightSections(False)
    fit_columns(view)


def fit_columns(view) -> None:
    """Fit the columns to the width the grid actually has right now.

    This is the whole width story, and it is worth measuring against what it
    replaces. The wx original needed ``_resize_columns``, ``_squeeze``,
    ``FLEX_WEIGHTS``, ``SHRINK_ORDER``, ``MIN_SHARE``, a throwaway trailing
    spacer column, ``_set_column_hidden`` and a ``_measure_grid_metrics`` that
    read the platform's *undeclared* header height, row indent and per-column
    padding off a populated grid — roughly 150 lines, because the macOS
    DataView redistributes every width whenever one changes and adds overhead
    it will not report. Ignoring that overhead once made a header whose numbers
    summed to exactly the client width overflow it by 135px.

    None of that is true of ``QTableView``: the viewport reports its own width
    honestly and a column stays where it is put. What is left is the part that
    was always real work — deciding who gets the surplus.
    """
    available = view.viewport().width()
    if available <= 0:
        return
    used = sum(
        width
        for key, width in FIXED_WIDTHS.items()
        if not view.isColumnHidden(COLUMN_INDEX[key])
    )
    for key, width in FIXED_WIDTHS.items():
        view.setColumnWidth(COLUMN_INDEX[key], width)

    surplus = max(0, available - used - 2)
    total = sum(FLEX_WEIGHTS.values())
    for key, weight in FLEX_WEIGHTS.items():
        view.setColumnWidth(
            COLUMN_INDEX[key],
            max(FLEX_FLOORS[key], int(surplus * weight / total)),
        )


__all__ = [
    "COLUMNS",
    "COLUMN_INDEX",
    "HIT_ROLE",
    "INLINE_DETAIL_MAX_SHARE",
    "INLINE_DETAIL_PX",
    "INLINE_ROLE",
    "PENDING",
    "PIXMAP_ROLE",
    "ROW_HEIGHT_PX",
    "SECONDARY_ROLE",
    "STOCK_ROLE",
    "THUMB_PX",
    "UNKNOWN",
    "CatalogDelegate",
    "ResultsModel",
    "ResultsView",
    "StockDelegate",
    "ThumbnailDelegate",
    "configure_header",
    "decode_thumbnail",
    "fit_columns",
    "inline_detail_height",
    "format_count",
]
