"""The LCSC Explorer — one window for everything LCSC.

Combines what previously needed three separate tools:

* keyword search over the JLC parts library, with **real parametric facets**
  built from the attributes the API returns (LCSC's filter sidebar, rebuilt);
* stock from **one of two separate inventories** — JLC assembly or LCSC retail,
  chosen with the "Inventory" switch. They disagree routinely, so each has its
  own column, its own colour and its own detail card, and the window commits to
  one at a time rather than showing both: retail stock costs one request per
  part, and fetching it for a page of results the user did not ask about is
  what gets the caller rate-limited. See :data:`STOCK_VIEWS`;
* symbol / footprint previews rendered locally from EasyEDA data, plus product
  photos — clickable, for a full-size look at a marking or a polarity band;
* one-click import of symbol + footprint + 3D model into a registered KiCad
  library, and assignment of the LCSC number to the selected footprints.

Fetch ordering matters here. The keyword search returns JLC assembly stock in
bulk but says nothing about retail, which is only available per part. So the
window fills in progressively, cheapest and most useful first:

1. the search itself, which paints the grid and hands out the photo ids;
2. in parallel, the row thumbnails and — in the LCSC retail view only — retail
   stock for the visible rows, each on its own small thread pool. They share no
   requests, so neither has any reason to wait for the other;
3. the selected part's availability report;
4. its symbol and footprint drawings;
5. its photo — last, always, because it is the one thing nobody needs to make
   a decision.

Every stage is token-guarded so a superseded fetch cannot write over newer
results, and every UI callback checks the window still exists: these fetches
outlive the dialog when the user closes it mid-flight.
"""

from __future__ import annotations

from contextlib import suppress
import io
import logging
from pathlib import Path
import queue
import threading
from typing import Dict, List, Optional, Set, Tuple
import webbrowser

import wx  # pylint: disable=import-error
import wx.dataview as dv  # pylint: disable=import-error

from ..events import AssignPartsEvent
from ..helpers import HighResWxSize
from . import api, theme
from .facetfilter import FacetFilterCtrl
from .importer import DEFAULT_LIB_NAME, LcscImporter, is_inside
from .photoviewer import PhotoViewerDialog
from .previewpanel import BitmapPreviewPanel, SvgPreviewPanel

logger = logging.getLogger(__name__)

#: ``(key, header, width)``. ``key`` is how the rest of this module refers to
#: a column; the grid's column order is this list's order.
#:
#: Ordered after LCSC's own results table — photo, part identity, description,
#: then the numbers — because that is the order the decision actually gets
#: made in. Related fields are deliberately stacked inside a row instead of
#: spread across twelve narrow columns: model + LCSC/type, manufacturer +
#: package, and unit price + minimum order. That keeps all the decision data
#: visible while making room for a genuinely useful photo.
#:
#: ``spacer`` is an empty trailing column, the same trick the main part list
#: uses: the last column in a DataViewCtrl absorbs the leftover width, and
#: without a throwaway one to take that role Description gets collapsed to
#: nothing whenever the stock columns are toggled.
COLUMNS: List[Tuple[str, str, int]] = [
    ("photo", "", 112),
    ("part", "Part", 260),
    ("description", "Description", 520),
    ("manufacturer", "Manufacturer / Package", 220),
    ("jlc_stock", "JLC assembly", 125),
    ("retail_stock", "LCSC retail", 125),
    ("price", "Unit price", 130),
    ("spacer", " ", 24),
]
COLUMN_INDEX = {key: index for index, (key, _label, _width) in enumerate(COLUMNS)}

#: How surplus grid width is shared out once every column has its base width.
#: The widths above are minimums, not targets: with the detail pane closed the
#: grid is half as wide again, and that surplus should go into the columns that
#: were truncating rather than into the trailing spacer. Only text columns
#: appear here — a stock figure is already given room for its largest possible
#: value, so widening it just moves the number further from its neighbour.
FLEX_WEIGHTS: List[Tuple[str, float]] = [
    ("description", 0.58),
    ("part", 0.24),
    ("manufacturer", 0.18),
]

#: The order columns give width back in when the window is too narrow for
#: everything: the throwaway spacer first, then the text columns, which reflow
#: into fewer characters per line, and only then the numeric ones, which lose
#: padding around a figure whose length is fixed.
SHRINK_ORDER: List[List[str]] = [
    ["spacer"],
    ["description", "part", "manufacturer"],
    ["price", "jlc_stock", "retail_stock"],
]

#: How far below its base width a column may be squeezed. A row that fits the
#: window beats a full-width description — the user should never have to scroll
#: sideways to read a result — but only down to a point, and these floors are
#: it. They add up to 780px, so with the platform's own overhead the columns
#: stop shrinking at around a 940px-wide grid and a horizontal scrollbar
#: becomes the lesser evil; that is below the width the grid has with the
#: detail pane open, so it takes a deliberately small window to reach.
#: The photo has no entry because a thumbnail does not reflow: it keeps its
#: base width at every window size.
MIN_SHARE: Dict[str, float] = {
    "part": 0.46,
    "description": 0.38,
    "manufacturer": 0.40,
    "jlc_stock": 0.70,
    "retail_stock": 0.70,
    "price": 0.70,
    "spacer": 0.0,
}

#: Thumbnail edge length and the row height needed to hold it, both in
#: unscaled pixels. A product photo is the fastest way to spot that a search
#: for "0402" has handed you a resistor array or a through-hole part, which is
#: the mistake the grid could not previously show at all.
THUMB_PX = 108
ROW_HEIGHT_PX = 140

#: The inline detail panel occupies whole placeholder rows inside the native
#: DataView. The panel is overlaid on those rows, so the result below it moves
#: down exactly as it does on JLCPCB rather than the details becoming a remote
#: drawer at the bottom of the window.
#:
#: Space is reserved in whole rows, so the height is always a multiple of
#: :data:`ROW_HEIGHT_PX`: this is the height aimed for, and
#: ``INLINE_DETAIL_MAX_SHARE`` is how much of the visible grid the details may
#: claim on the way there. Three rows' worth is genuinely comfortable, but not
#: at the cost of leaving no results on screen around it — so a short grid gets
#: two and a tall one gets three.
INLINE_DETAIL_PX = 400
INLINE_DETAIL_MAX_SHARE = 0.62

#: Padding inside the detail pane. Its content is dense — three previews, two
#: stock cards, a caveat list and a parameter table — and whitespace is the
#: whole difference between that reading as a dashboard and as a wall.
DETAIL_PAD = 8

#: How often the open inline panel re-checks where its rows have moved to.
#: Scroll notifications from a native DataView are not dependable — a trackpad
#: scroll or a programmatic ``EnsureVisible`` can move the rows without any
#: event reaching us — and an overlay left behind at a stale position is the
#: whole reason the details used to detach from their row. Cheap enough to just
#: keep asking: one rect lookup, and it only runs while the panel is open.
INLINE_TRACK_MS = 100

#: How long a facet tick waits before the grid is rebuilt around it.
#:
#: Re-filtering is not cheap — it empties the grid, appends up to
#: :data:`PAGE_SIZE` rows through custom renderers, re-measures the header and
#: relaunches the background fills — and it used to run on *every* tick, on the
#: UI thread, while the checkbox popup was still open. Picking three tolerance
#: values meant three full rebuilds, and on wxOSX a popup whose owner is busy
#: stops highlighting rows and can read the next click as a click outside and
#: dismiss itself: the control looked like it was ignoring the mouse.
#:
#: Coalescing a burst of ticks into one rebuild fixes that, and the tick itself
#: still paints immediately — the checkbox and the collapsed summary are the
#: feedback, and neither waits on this.
FILTER_DEBOUNCE_MS = 220

#: The two inventories, as a mutually exclusive choice: the window reports on
#: exactly one of them at a time.
#:
#: There used to be a third "Both inventories" option, and it was the default.
#: It could not be made to work. The keyword search returns JLC assembly stock
#: for a hundred rows in one request, but retail stock is one request *per
#: part* — so showing both meant a hundred extra lookups per search, re-fired
#: on every filter change. LCSC's own host answers those with a 403 in some
#: regions, and the EasyEDA fallback rate-limits a burst that size and then
#: refuses the caller's address outright for minutes. The retail column
#: therefore filled with "?" — indistinguishable from "out of stock" — while
#: the window sat there spending its event loop on doomed requests.
#:
#: One inventory at a time is what makes the cost honest: pick JLC assembly and
#: the window issues no retail requests at all.
STOCK_VIEWS: List[Tuple[str, str]] = [
    ("jlc", "JLC assembly"),
    ("retail", "LCSC retail"),
]

SORT_MODES: List[Tuple[str, str]] = [
    ("relevance", "Best match"),
    ("jlc", "JLC assembly stock (high first)"),
    ("retail", "LCSC retail stock (high first)"),
    ("price", "Unit price (low first)"),
    ("min_qty", "Minimum quantity (low first)"),
]

#: Where the selected part's full details appear. ``below`` is the desktop
#: equivalent of JLCPCB's expanded result row: it keeps the catalogue full
#: width and inserts details immediately under the selection. ``side`` is
#: better on wide screens.
DETAIL_LAYOUTS: List[Tuple[str, str]] = [
    ("side", "Side panel"),
    ("below", "Inline below"),
]

PAGE_SIZE = 100

#: Retail stock is one HTTP request per part, so the fill is bounded on both
#: axes: how many rows are worth filling, and how hard we hit LCSC doing it.
#:
#: Two workers, not five. Five fetched a 100-row page in about ten seconds and
#: that was the problem: EasyEDA — the fallback that answers when
#: ``wmsc.lcsc.com`` will not — sits behind a rate limiter that reads eight
#: requests a second as abuse and then 403s *every* request from that address
#: for minutes afterwards. The fill was fast enough to get the user banned from
#: the data it was fetching, and every subsequent search re-earned the ban. At
#: two workers the page takes most of a minute and arrives.
RETAIL_FILL_LIMIT = 120
RETAIL_FILL_WORKERS = 2

#: Thumbnails run as a second, deliberately smaller pass after the stock fill
#: settles. The JSON is cached by then so no part is fetched twice, but image
#: bytes are still the heaviest and least decision-critical thing the window
#: pulls, so fewer rows and fewer workers than the numbers got.
THUMB_FILL_LIMIT = 60
THUMB_FILL_WORKERS = 3

#: Decoded thumbnails kept across searches, so going back to a previous
#: keyword repaints instantly. Several pages' worth, dropped wholesale rather
#: than by age — these are small row thumbnails and an LRU would cost more to
#: maintain than the memory it saved.
#: than the memory it saved.
MAX_CACHED_THUMBS = 400

#: Grid text for a retail cell that has not been fetched yet, versus one where
#: the endpoint answered but had nothing to say. Conflating the two would show
#: a part as out of stock when we simply have not looked.
PENDING = "…"
UNKNOWN = "?"


def _elided(dc, text: str, width: int) -> str:
    """Shorten ``text`` with an ellipsis until it fits ``width``.

    Only ever the *last* line of a cell needs this, and it needs it visibly:
    text that merely overruns the clipping region is cut mid-glyph, which reads
    as a rendering fault rather than as "there is more here".
    """
    if dc.GetTextExtent(text)[0] <= width:
        return text
    lo = 0
    hi = len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if dc.GetTextExtent(text[:mid] + "…")[0] <= width:
            lo = mid
        else:
            hi = mid - 1
    return f"{text[:lo].rstrip()}…" if lo else "…"


def _wrapped_lines(dc, text: str, width: int, maximum: int) -> List[str]:
    """Fit ``text`` into at most ``maximum`` lines for a catalogue cell."""
    words: List[str] = []
    for word in str(text or "").split():
        remaining = word
        while remaining and dc.GetTextExtent(remaining)[0] > width:
            lo = 1
            hi = len(remaining)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if dc.GetTextExtent(remaining[:mid])[0] <= width:
                    lo = mid
                else:
                    hi = mid - 1
            words.append(remaining[:lo])
            remaining = remaining[lo:]
        if remaining:
            words.append(remaining)
    if not words:
        return [""]
    lines: List[str] = []
    current = ""
    while words:
        word = words.pop(0)
        candidate = f"{current} {word}".strip()
        if current and dc.GetTextExtent(candidate)[0] > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > maximum:
        tail = " ".join(lines[maximum - 1 :])
        lines = lines[: maximum - 1] + [_elided(dc, tail, width)]
    return lines


class CatalogTextCell(dv.DataViewCustomRenderer):
    """Draw a primary value and quieter supporting metadata on a roomy row."""

    def __init__(
        self,
        row_height: int,
        primary_bold: bool = False,
        primary_lines: int = 1,
    ):
        super().__init__("string", dv.DATAVIEW_CELL_INERT, wx.ALIGN_LEFT)
        self._row_height = row_height
        self._primary_bold = primary_bold
        self._primary_lines = primary_lines
        self._value = ""
        self._cell_width = 100

    def set_cell_width(self, width: int) -> None:
        """Adopt the width of the column this renderer draws into.

        The rect a custom renderer is handed is sized from :meth:`GetSize`, not
        from its column — the macOS DataView hands over exactly what was asked
        for and leaves the rest of the column blank. A renderer that does not
        know its column's width therefore wraps and clips inside a 100px box in
        a 470px column, which is what shortened "Multilayer Ceramic Capacitor"
        to "Capacito". Restated by :meth:`LcscExplorerDialog._resize_columns`
        every time the widths change.
        """
        self._cell_width = max(40, int(width))

    def SetValue(self, value) -> bool:
        """Store the newline-separated primary and secondary values."""
        self._value = "" if value is None else str(value)
        return True

    def GetValue(self) -> str:
        """Return the stored cell value."""
        return self._value

    def GetSize(self):
        """Ask for the full column, at the thumbnail's generous row height."""
        return wx.Size(self._cell_width, self._row_height)

    def Render(self, rect, dc, state) -> bool:
        """Paint a compact hierarchy that remains legible when selected."""
        primary, _separator, secondary = self._value.partition("\n")
        base = dc.GetFont()
        primary_font = theme.bold(base) if self._primary_bold else base
        secondary_font = theme.scaled(base, 0.86)
        available = max(8, rect.width - 16)

        dc.SetFont(primary_font)
        lines = _wrapped_lines(dc, primary, available, self._primary_lines)
        primary_height = dc.GetTextExtent("Hg")[1]
        secondary_height = 0
        secondary_lines: List[str] = []
        if secondary:
            dc.SetFont(secondary_font)
            secondary_height = dc.GetTextExtent("Hg")[1]
            secondary_lines = _wrapped_lines(dc, secondary, available, 2)
        gap = 5 if secondary else 0
        content_height = (
            primary_height * len(lines) + secondary_height * len(secondary_lines) + gap
        )
        y = rect.y + max(4, (rect.height - content_height) // 2)

        selected = bool(state & dv.DATAVIEW_CELL_SELECTED)
        foreground = wx.SystemSettings.GetColour(
            wx.SYS_COLOUR_HIGHLIGHTTEXT if selected else wx.SYS_COLOUR_WINDOWTEXT
        )
        dc.SetBackgroundMode(wx.TRANSPARENT)
        dc.SetClippingRegion(rect)
        try:
            dc.SetFont(primary_font)
            dc.SetTextForeground(foreground)
            for line in lines:
                dc.DrawText(line, rect.x + 8, y)
                y += primary_height
            if secondary:
                y += gap
                dc.SetFont(secondary_font)
                dc.SetTextForeground(foreground if selected else theme.colour("muted"))
                for line in secondary_lines:
                    dc.DrawText(line, rect.x + 8, y)
                    y += secondary_height
        finally:
            dc.DestroyClippingRegion()
            dc.SetFont(base)
        return True


class StockCell(dv.DataViewCustomRenderer):
    """Draws a stock figure in a colour that matches how healthy it is.

    A grid of five- and seven-digit numbers is unreadable at a glance; colour
    turns it into something you can scan. The renderer parses the formatted
    text back into a number rather than being handed the raw value, because
    ``DataViewListCtrl`` stores plain strings.
    """

    def __init__(self, row_height: int, align=wx.ALIGN_RIGHT):
        super().__init__("string", dv.DATAVIEW_CELL_INERT, align)
        self._row_height = row_height
        self._value = ""
        self._cell_width = 120

    def set_cell_width(self, width: int) -> None:
        """Adopt the width of the column this renderer draws into.

        See :meth:`CatalogTextCell.set_cell_width`. Here it is what puts the
        figure against the column's own right edge instead of the right edge of
        a box the width of the number itself, which drifted with every value.
        """
        self._cell_width = max(40, int(width))

    def SetValue(self, value) -> bool:
        """Store the text for the cell about to be drawn."""
        self._value = "" if value is None else str(value)
        return True

    def GetValue(self) -> str:
        """Return the stored cell text."""
        return self._value

    def GetSize(self):
        """Ask for the full column so the figure right-aligns against it."""
        return wx.Size(self._cell_width, self._row_height)

    def Render(self, rect, dc, state) -> bool:
        """Draw the figure right-aligned and colour-coded."""
        text = self._value
        if not text:
            return True

        if state & dv.DATAVIEW_CELL_SELECTED:
            # The selection bar already carries meaning; a coloured number on
            # top of it is worse than a plain readable one.
            dc.SetTextForeground(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHTTEXT)
            )
        elif text in (PENDING, UNKNOWN):
            dc.SetTextForeground(theme.colour("unknown"))
        else:
            dc.SetTextForeground(theme.stock_colour(_parse_count(text)))

        base = dc.GetFont()
        dc.SetFont(theme.bold(theme.scaled(base, 1.05)))
        dc.SetBackgroundMode(wx.TRANSPARENT)
        width, height = dc.GetTextExtent(text)
        dc.SetClippingRegion(rect)
        try:
            dc.DrawText(
                text,
                rect.x + max(4, rect.width - width - 6),
                rect.y + max(0, (rect.height - height) // 2),
            )
        finally:
            dc.DestroyClippingRegion()
            dc.SetFont(base)
        return True


class ThumbCell(dv.DataViewCustomRenderer):
    """Draws a product thumbnail for the row's part, once one has arrived.

    The cell value is the **LCSC code**, not a bitmap: photos land long after
    the rows do, and ``DataViewListCtrl`` has no null-bitmap state to sit in
    while waiting — handing it an invalid ``wx.Bitmap`` is one of the calls
    that raises rather than warns in KiCad's wx. So the model keeps holding a
    plain string and the renderer looks the artwork up through ``lookup``,
    which lets a photo appear with a row refresh and nothing else.
    """

    def __init__(self, lookup, size: int):
        super().__init__("string", dv.DATAVIEW_CELL_INERT, wx.ALIGN_CENTER)
        self._lookup = lookup
        self._size = size
        self._value = ""
        self._cell_width = size + 6

    def set_cell_width(self, width: int) -> None:
        """Adopt the width of the column this renderer draws into.

        See :meth:`CatalogTextCell.set_cell_width`; for the photo it is what
        centres the thumbnail in its column rather than in a box pinned to the
        column's left edge.
        """
        self._cell_width = max(self._size + 6, int(width))

    def SetValue(self, value) -> bool:
        """Store the LCSC code for the cell about to be drawn."""
        self._value = "" if value is None else str(value)
        return True

    def GetValue(self) -> str:
        """Return the stored LCSC code."""
        return self._value

    def GetSize(self):
        """Reserve a square big enough for the thumbnail."""
        return wx.Size(self._cell_width, self._size + 4)

    def Render(self, rect, dc, state) -> bool:
        """Draw the thumbnail, or a faint placeholder while it is missing."""
        del state
        if not self._value:
            return True
        bitmap = self._lookup(self._value)
        if bitmap is None:
            # A frame rather than nothing: an empty cell reads as "this part
            # has no photo", which is usually wrong and always unhelpful.
            dc.SetBrush(wx.Brush(wx.TRANSPARENT_BRUSH))
            dc.SetPen(wx.Pen(theme.colour("rule")))
            side = min(self._size, rect.height - 4, rect.width - 4)
            if side > 6:
                dc.DrawRoundedRectangle(
                    rect.x + (rect.width - side) // 2,
                    rect.y + (rect.height - side) // 2,
                    side,
                    side,
                    3,
                )
            return True

        width, height = bitmap.GetSize()
        dc.SetClippingRegion(rect)
        try:
            dc.DrawBitmap(
                bitmap,
                rect.x + max(0, (rect.width - width) // 2),
                rect.y + max(0, (rect.height - height) // 2),
                True,
            )
        finally:
            dc.DestroyClippingRegion()
        return True


class StockCard(wx.Panel):
    """One inventory's headline number, with its source colour and caveats.

    Purpose-drawn rather than assembled from ``StaticText`` so the source
    identity (a coloured rule and title), the figure and its supporting
    detail line up the same way in both cards and in both appearances.
    """

    #: Inset from the card's edge to its content, and the gap between the
    #: figure and the lines under it. Both were tighter, which read as cramped
    #: and — with two detail lines and a footnote to fit — cut the footnote off
    #: the bottom of the card.
    PAD = 12
    LINE_GAP = 3

    def __init__(self, parent, title: str, accent: str, footnote: str = ""):
        super().__init__(parent, style=wx.BORDER_NONE)
        self._title = title
        self._accent = accent
        self._footnote = footnote
        self._count: Optional[int] = None
        self._lines: List[str] = []
        self._pending = True

        self.SetMinSize(wx.Size(168, 124))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    def set_pending(self) -> None:
        """Show the card as still loading."""
        self._pending = True
        self._count = None
        self._lines = []
        self.Refresh()

    def set_value(
        self, count: Optional[int], lines: Optional[List[str]] = None
    ) -> None:
        """Show ``count`` with up to two supporting ``lines``."""
        self._pending = False
        self._count = count
        self._lines = [line for line in (lines or []) if line][:2]
        self.Refresh()

    def _on_paint(self, _event) -> None:
        """Paint the card."""
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)))
        dc.Clear()

        width, height = self.GetClientSize()
        if width < 8 or height < 8:
            return

        pad = self.PAD
        accent = theme.colour(self._accent)
        dc.SetBrush(wx.Brush(theme.card_background()))
        dc.SetPen(wx.Pen(theme.colour("rule")))
        dc.DrawRoundedRectangle(0, 0, width - 1, height - 1, 6)
        # A short rule in the source colour: the fastest way to tell the two
        # cards apart without reading either title.
        dc.SetBrush(wx.Brush(accent))
        dc.SetPen(wx.Pen(accent))
        dc.DrawRectangle(pad, pad, max(12, min(46, width - 2 * pad)), 3)

        base = self.GetFont()
        dc.SetFont(theme.bold(theme.scaled(base, 0.85)))
        dc.SetTextForeground(accent)
        title_y = pad + 8
        dc.DrawText(self._title, pad, title_y)
        title_h = dc.GetTextExtent(self._title)[1]

        y = title_y + title_h + self.LINE_GAP
        if self._pending:
            dc.SetFont(base)
            dc.SetTextForeground(theme.colour("muted"))
            dc.DrawText("loading …", pad, y + 4)
            return

        figure = UNKNOWN if self._count is None else f"{self._count:,}"
        dc.SetFont(theme.bold(theme.scaled(base, 1.6)))
        dc.SetTextForeground(theme.stock_colour(self._count))
        dc.DrawText(figure, pad, y)
        y += dc.GetTextExtent(figure)[1] + self.LINE_GAP

        dc.SetFont(theme.scaled(base, 0.85))
        dc.SetTextForeground(theme.colour("muted"))
        for line in self._lines + ([self._footnote] if self._footnote else []):
            if y > height - pad // 2:
                break
            dc.DrawText(_elided(dc, line, max(20, width - 2 * pad)), pad, y)
            y += dc.GetTextExtent(line)[1] + self.LINE_GAP


class LcscExplorerDialog(wx.Dialog):
    """Search, inspect, import and assign LCSC parts."""

    def __init__(self, parent, parts=None, initial_keyword: str = "", references=None):
        """Open the explorer for ``parts``.

        ``parts`` is the ``{reference: search string}`` mapping the main
        window builds from the current footprint selection — the same shape
        the superseded part selector took, so this is a drop-in replacement
        for it. ``references``/``initial_keyword`` remain accepted for the
        toolbar entry point, which has no per-footprint values.
        """
        super().__init__(
            parent,
            id=wx.ID_ANY,
            title="LCSC Explorer",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(1500, 900)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        self.parent = parent
        self.parts = dict(parts or {})
        self.references = list(references or self.parts.keys())
        self.logger = logging.getLogger(__name__)

        self._hits: List[api.SearchHit] = []
        self._visible: List[api.SearchHit] = []
        #: Physical DataView row -> hit. Inline-detail spacer rows are ``None``.
        self._row_hits: List[Optional[api.SearchHit]] = []
        self._facets: Dict[str, List[Tuple[str, int]]] = {}
        self._facet_controls: Dict[str, FacetFilterCtrl] = {}
        #: attribute -> the set of values ticked for it. Empty set or absent
        #: key both mean "no constraint on this attribute".
        self._selected_facets: Dict[str, Set[str]] = {}
        self._report: Optional[api.StockReport] = None
        #: lcsc -> retail stock. A present ``None`` means "asked, no answer";
        #: an absent key means "not asked yet".
        self._retail: Dict[str, Optional[int]] = {}
        #: lcsc -> decoded grid thumbnail. A present ``None`` means "asked,
        #: this part has no usable photo" and stops us asking again; an absent
        #: key means "not asked yet". Either way the cell draws a placeholder.
        self._thumbs: Dict[str, Optional[wx.Bitmap]] = {}
        self._thumb_refresh_scheduled = False
        #: The full-size photo window, kept so clicking a second thumbnail
        #: retargets it instead of opening another. Falsy once the user closes
        #: it, because a destroyed wx proxy is.
        self._photo_viewer: Optional[PhotoViewerDialog] = None
        saved_layout = (
            getattr(self.parent, "settings", {})
            .get("lcsc", {})
            .get("explorer_detail_layout", "side")
        )
        self._detail_layout = (
            saved_layout
            if saved_layout in {key for key, _label in DETAIL_LAYOUTS}
            else "side"
        )
        #: Whether the detail pane is currently split in, and where the sash
        #: was the last time it was. Starts closed — see :meth:`_build_ui`.
        self._details_shown = False
        self._side_sash_pos = 0
        self._inline_after = wx.NOT_FOUND
        self._inline_rows = 0
        self._inline_reposition_scheduled = False
        #: The clip rectangle :meth:`_position_inline_detail` last applied, so
        #: the tracking timer can do nothing at all when nothing has moved.
        self._inline_placed: Optional[Tuple[int, int, int, int]] = None
        self._suppress_selection = False
        self._selection_just_moved = False
        self._resize_scheduled = False
        #: The ``(widths, hidden)`` last pushed into the grid header, so a
        #: resize storm does not restate identical numbers on every
        #: ``EVT_SIZE``.
        self._applied: Tuple[Dict[str, int], Dict[str, bool]] = ({}, {})
        #: Grid geometry the platform decides and will not declare: the column
        #: header's height, the indent before the first cell, and the padding
        #: added to every column's set width. Measured off a real row by
        #: :meth:`_measure_grid_metrics`; zero is the right assumption until
        #: then, and stays right on the generic DataView.
        self._header_px = 0
        self._grid_indent = 0
        self._cell_overhead = 0
        self._search_token = 0
        self._detail_token = 0
        self._retail_token = 0
        self._thumb_token = 0

        # A modeless wx.Dialog hides rather than destroys on close; override
        # so the main window's singleton reference is actually released.
        self.Bind(wx.EVT_CLOSE, self._on_close)

        # Runs only while the inline detail is open — see INLINE_TRACK_MS.
        self._inline_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_inline_tick, self._inline_timer)

        # One-shot, restarted by each facet tick — see FILTER_DEBOUNCE_MS.
        self._filter_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_filter_tick, self._filter_timer)

        self._build_ui()
        self.Centre(wx.BOTH)

        # Deferred rather than run inline: __init__ returns before the caller
        # calls Show(), and touching controls on a window that has not been
        # shown yet has been a source of unpainted dialogs on macOS.
        keyword = initial_keyword or self.common_value(self.parts)
        if keyword:
            self.keyword.SetValue(keyword)
        wx.CallAfter(self._on_first_shown, bool(keyword))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _post(self, handler, *args) -> bool:
        """Hand a worker thread's result to the UI thread, if one is still there.

        Every fetch here can outlive the dialog, and ``wx.CallAfter`` is not
        safe to call once it has gone: it reaches into the app object and the
        dialog's proxy, and raises on the *worker* thread when either has been
        torn down. Nothing catches an exception there, so it surfaces as a
        traceback on stderr from a thread the user cannot see — which is how
        closing the window mid-fill used to print a stack trace into KiCad's
        console.

        Returns whether the callback was posted, so a loop can stop pulling
        work it has nowhere to deliver.
        """
        try:
            if not self._alive():
                return False
            wx.CallAfter(handler, *args)
            return True
        except (RuntimeError, AssertionError):  # pragma: no cover - teardown race
            # The window went away between the check and the post. That is the
            # race this exists for, and there is nothing left to update.
            return False

    def _alive(self) -> bool:
        """Report whether this dialog's wx window still exists.

        Every network fetch here can outlive the dialog: the user closes the
        window and the worker's ``wx.CallAfter`` then lands on a deleted C++
        object, which raises ``RuntimeError`` inside wx's event loop. The
        dialog proxy is checked *and* one of its children, because a
        top-level window's ``Destroy()`` is deferred to idle time and its
        children go first.
        """
        try:
            return bool(self) and bool(self.results)
        except RuntimeError:  # pragma: no cover - depends on teardown timing
            return False

    def _on_first_shown(self, run_search: bool) -> None:
        """Force a layout pass once shown, then start the initial search."""
        if not self._alive():
            return
        self.Layout()
        # Column widths set during construction do not stick: the native
        # DataView has not been realised yet and discards them. Restating them
        # here, once the window is on screen, is what makes them hold.
        self._apply_stock_view()
        self._apply_row_height()
        self.Refresh()
        if run_search:
            self._start_search()

    def _apply_row_height(self) -> None:
        """Make rows tall enough for a thumbnail, if this build allows it.

        ``SetRowHeight`` is honoured by the generic DataView and ignored by
        some native ones, and it is only meaningful once the control has been
        realised — hence the call from here rather than construction. When it
        does not take, ``ThumbCell.GetSize`` still asks for the space and the
        worst case is a photo scaled down to whatever height the platform
        chose, which is a cosmetic loss rather than a broken grid.
        """
        with suppress(AttributeError, RuntimeError, wx.wxAssertionError):
            self.results.SetRowHeight(int(self.parent.scale_factor * ROW_HEIGHT_PX))

    @staticmethod
    def common_value(parts) -> str:
        """Return the shared search string when every selected part agrees."""
        values = {v for v in (parts or {}).values() if v}
        return values.pop() if len(values) == 1 else ""

    def update_for(self, parts) -> None:
        """Re-target an already-open explorer at a new footprint selection.

        Invoked when the user activates another footprint while this window
        is open, instead of spawning a second one.
        """
        self.parts = dict(parts or {})
        self.references = list(self.parts.keys())
        keyword = self.common_value(self.parts)
        self.keyword.ChangeValue(keyword)
        self._update_target_label()
        if keyword:
            self._start_search()
        else:
            self._update_actions()

    def _cancel_pending(self) -> None:
        """Invalidate every in-flight fetch.

        Bumping the tokens makes the worker threads fall out of their loops on
        the next check and makes their ``wx.CallAfter`` callbacks return
        without touching any control.
        """
        self._search_token += 1
        self._detail_token += 1
        self._retail_token += 1
        self._thumb_token += 1

    def _on_close(self, _event) -> None:
        """Release the main window's reference, then destroy."""
        self._cancel_pending()
        self._inline_timer.Stop()
        self._filter_timer.Stop()
        # The photo window is a child of this dialog, so wx would take it down
        # anyway — but only after this returns, leaving it briefly on screen
        # with nothing behind it.
        viewer = self._photo_viewer
        self._photo_viewer = None
        if viewer:
            viewer.Destroy()
        if getattr(self.parent, "_part_selector", None) is self:
            self.parent._part_selector = None
        self.Destroy()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Assemble the dialog layout."""
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(self._build_search_bar(), 0, wx.EXPAND | wx.ALL, 6)

        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE | wx.SP_THIN_SASH)
        splitter.SetMinimumPaneSize(int(self.parent.scale_factor * 220))
        self.splitter = splitter

        left = wx.Panel(splitter)
        self.left_panel = left
        self.left_sizer = wx.BoxSizer(wx.VERTICAL)
        self.left_sizer.Add(self._build_facet_panel(left), 0, wx.EXPAND | wx.ALL, 4)
        self.left_sizer.Add(self._build_results(left), 1, wx.EXPAND | wx.ALL, 4)
        left.SetSizer(self.left_sizer)

        # The window the inline details are clipped to. A sibling of the grid
        # rather than a child of it — a native DataView will not host one — and
        # deliberately outside ``left_sizer``, because its rectangle tracks the
        # placeholder rows it covers instead of a layout slot. It exists so the
        # details can be *partly* on screen: sized to the visible slice while
        # the detail panel inside it keeps its full height and slides, the
        # panel scrolls under the header like the tall row it is standing in
        # for, rather than vanishing the moment it no longer fits whole.
        self.inline_clip = wx.Panel(left)
        self.inline_clip.Hide()

        right = wx.Panel(splitter)
        self.right_panel = right
        right.SetSizer(self._build_detail_panel(right))

        self._side_sash_pos = int(self.GetSize().x * 0.66)
        splitter.SplitVertically(left, right, self._side_sash_pos)
        # Built, then immediately put away. The grid is what this window is
        # for, and the detail pane costs it a third of its width for something
        # you only want once you have picked a candidate out of the list — so
        # it stays closed until a row is clicked.
        splitter.Unsplit(right)
        splitter.Bind(wx.EVT_SPLITTER_SASH_POS_CHANGED, self._on_sash_moved)
        root.Add(splitter, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        root.Add(self._build_action_bar(), 0, wx.EXPAND | wx.ALL, 6)

        self.SetSizer(root)
        self.Layout()
        self._apply_stock_view()
        self._update_target_label()
        self._update_actions()

    def _update_target_label(self) -> None:
        """Show which footprints an assign would be applied to."""
        if not self.references:
            self.target_label.SetLabel(
                "No footprint selected — Import still works; select "
                "footprints in the main window to enable Assign."
            )
            return
        shown = ", ".join(self.references[:12])
        if len(self.references) > 12:
            shown += f", … (+{len(self.references) - 12})"
        self.target_label.SetLabel(
            f"Assigning to {len(self.references)} footprint(s): {shown}"
        )

    def _build_search_bar(self) -> wx.Sizer:
        """Build a compact search toolbar with the common decisions in one row."""
        box = wx.StaticBoxSizer(wx.VERTICAL, self, "Find parts")
        panel = box.GetStaticBox()

        query = wx.BoxSizer(wx.HORIZONTAL)
        self.keyword = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.keyword.SetHint("e.g. 22k 0805 0.1%   or   AD7124   or   C374726")
        self.keyword.Bind(wx.EVT_TEXT_ENTER, lambda _e: self._start_search())
        query.Add(self.keyword, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)

        self.search_button = wx.Button(panel, label="Search")
        self.search_button.SetDefault()
        query.Add(self.search_button, 0, wx.ALL, 4)
        self.search_button.Bind(wx.EVT_BUTTON, lambda _e: self._start_search())

        self.refresh_button = wx.Button(panel, label="Refresh data")
        self.refresh_button.SetToolTip(
            "Drop cached stock/price data and re-query. Stock figures are "
            "cached for 5 minutes."
        )
        self.refresh_button.Bind(wx.EVT_BUTTON, self._on_refresh)
        query.Add(self.refresh_button, 0, wx.ALL, 4)
        box.Add(query, 0, wx.EXPAND)

        options = wx.BoxSizer(wx.HORIZONTAL)
        options.Add(
            wx.StaticText(panel, label="Inventory:"),
            0,
            wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        self.stock_view = wx.Choice(
            panel, choices=[label for _key, label in STOCK_VIEWS]
        )
        self.stock_view.SetToolTip(
            "JLC assembly and LCSC retail are separate warehouses whose stock "
            "routinely disagrees. Pick the one you are ordering from: the whole "
            "window — column, filter and detail card — reports on that one."
        )
        self.stock_view.SetSelection(0)
        self.stock_view.Bind(wx.EVT_CHOICE, self._on_stock_view_changed)
        options.Add(self.stock_view, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 12)

        options.Add(
            wx.StaticText(panel, label="Library:"),
            0,
            wx.RIGHT | wx.ALIGN_CENTER_VERTICAL,
            4,
        )
        self.lib_filter = wx.Choice(
            panel, choices=["All", "Basic only", "Extended only"]
        )
        self.lib_filter.SetSelection(0)
        self.lib_filter.Bind(wx.EVT_CHOICE, lambda _e: self._start_search())
        options.Add(self.lib_filter, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 12)

        options.Add(
            wx.StaticText(panel, label="Sort:"),
            0,
            wx.RIGHT | wx.ALIGN_CENTER_VERTICAL,
            4,
        )
        self.sort_choice = wx.Choice(panel, choices=[name for _key, name in SORT_MODES])
        self.sort_choice.SetSelection(0)
        self.sort_choice.Bind(wx.EVT_CHOICE, lambda _e: self._apply_filters())
        self.sort_choice.SetMinSize(wx.Size(int(self.parent.scale_factor * 210), -1))
        options.Add(self.sort_choice, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 12)

        self.in_stock_only = wx.CheckBox(panel, label="In stock only")
        self.in_stock_only.Bind(wx.EVT_CHECKBOX, lambda _e: self._apply_filters())
        options.Add(self.in_stock_only, 0, wx.ALIGN_CENTER_VERTICAL)
        options.AddStretchSpacer()

        # The filter panel is space the grid could be using instead, and which
        # of the two you want is a per-moment thing: filters while narrowing
        # down, results while scanning. The detail pane has no button of its
        # own — selecting a part opens it and clicking that part again closes
        # it, which is the same gesture and one control fewer.
        self.filter_toggle = wx.ToggleButton(panel, label="Filters ▴")
        self.filter_toggle.SetValue(True)
        self.filter_toggle.SetToolTip(
            "Show or hide the parametric filters. Hidden, the result list "
            "gets their height."
        )
        self.filter_toggle.Bind(wx.EVT_TOGGLEBUTTON, self._on_toggle_filters)
        options.Add(self.filter_toggle, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 10)

        options.Add(
            wx.StaticText(panel, label="Details:"),
            0,
            wx.RIGHT | wx.ALIGN_CENTER_VERTICAL,
            4,
        )
        self.detail_layout_choice = wx.Choice(
            panel, choices=[label for _key, label in DETAIL_LAYOUTS]
        )
        self.detail_layout_choice.SetSelection(
            next(
                index
                for index, (key, _label) in enumerate(DETAIL_LAYOUTS)
                if key == self._detail_layout
            )
        )
        self.detail_layout_choice.SetToolTip(
            "Show selected-part details beside the catalogue, or in a full-width "
            "expanded row directly under the part like the JLCPCB parts library."
        )
        self.detail_layout_choice.Bind(wx.EVT_CHOICE, self._on_detail_layout_changed)
        options.Add(self.detail_layout_choice, 0, wx.ALIGN_CENTER_VERTICAL)

        box.Add(options, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        return box

    def _build_facet_panel(self, parent) -> wx.Sizer:
        """Container for the dynamically-built parametric filters.

        The number of facets is only known after a search, so the controls
        live in a scrolled window with a bounded height — a category with a
        dozen attributes must not squeeze the result list off screen.
        """
        self.facet_box = wx.StaticBoxSizer(
            wx.VERTICAL, parent, "Parametric filters (from part attributes)"
        )

        self.facet_scroller = wx.ScrolledWindow(
            self.facet_box.GetStaticBox(), style=wx.VSCROLL
        )
        self.facet_scroller.SetScrollRate(0, 12)
        self.facet_scroller.SetMinSize(wx.Size(-1, int(self.parent.scale_factor * 96)))
        self.facet_grid = wx.FlexGridSizer(0, 4, 6, 8)
        self.facet_grid.AddGrowableCol(1, 1)
        self.facet_grid.AddGrowableCol(3, 1)
        self.facet_scroller.SetSizer(self.facet_grid)
        self.facet_box.Add(self.facet_scroller, 1, wx.EXPAND | wx.ALL, 4)

        self.facet_hint = wx.StaticText(
            self.facet_box.GetStaticBox(), label="Run a search to populate filters."
        )
        self.facet_hint.SetForegroundColour(theme.colour("muted"))
        self.facet_box.Add(self.facet_hint, 0, wx.LEFT | wx.BOTTOM, 6)
        return self.facet_box

    def _build_results(self, parent) -> wx.Sizer:
        """Build the result list and its status line."""
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.status = wx.StaticText(parent, label="Ready.")
        sizer.Add(self.status, 0, wx.LEFT | wx.BOTTOM, 4)

        self.results = dv.DataViewListCtrl(
            parent, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_SINGLE
        )
        self.result_columns: Dict[str, dv.DataViewColumn] = {}
        self.column_widths: Dict[str, int] = {}
        self.thumb_px = int(self.parent.scale_factor * THUMB_PX)
        self.row_px = int(self.parent.scale_factor * ROW_HEIGHT_PX)
        #: Held back from the width the columns may share out, so that filling
        #: the grid can never be what summons a horizontal scrollbar. Small,
        #: because the platform's own overhead is measured rather than guessed
        #: at — this covers only rounding.
        self._grid_slack = int(self.parent.scale_factor * 8)
        for key, label, width in COLUMNS:
            scaled_width = int(self.parent.scale_factor * width)
            self.column_widths[key] = scaled_width
            if key in ("jlc_stock", "retail_stock"):
                column = dv.DataViewColumn(
                    label,
                    StockCell(self.row_px),
                    COLUMN_INDEX[key],
                    width=scaled_width,
                    align=wx.ALIGN_RIGHT,
                )
                self.results.AppendColumn(column, "string")
            elif key == "photo":
                column = dv.DataViewColumn(
                    label,
                    ThumbCell(self._thumbs.get, self.thumb_px),
                    COLUMN_INDEX[key],
                    width=scaled_width,
                    align=wx.ALIGN_CENTER,
                )
                self.results.AppendColumn(column, "string")
            elif key in ("part", "description", "manufacturer", "price"):
                renderer = CatalogTextCell(
                    self.row_px,
                    primary_bold=key in ("part", "price"),
                    primary_lines={
                        "part": 2,
                        "description": 5,
                        "manufacturer": 2,
                    }.get(key, 1),
                )
                column = dv.DataViewColumn(
                    label,
                    renderer,
                    COLUMN_INDEX[key],
                    width=scaled_width,
                    align=wx.ALIGN_LEFT,
                )
                self.results.AppendColumn(column, "string")
            else:
                column = self.results.AppendTextColumn(
                    label, width=scaled_width, mode=dv.DATAVIEW_CELL_INERT
                )
            self.result_columns[key] = column

        self.results.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._on_row_selected)
        self.results.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self._on_row_activated)
        self.results.Bind(wx.EVT_LEFT_DOWN, self._on_grid_click)
        self.results.Bind(wx.EVT_SIZE, self._on_grid_resized)
        self.results.Bind(wx.EVT_SCROLLWIN, self._on_grid_scrolled)
        self.results.Bind(wx.EVT_MOUSEWHEEL, self._on_grid_scrolled)
        sizer.Add(self.results, 1, wx.EXPAND)
        return sizer

    def _build_detail_panel(self, parent) -> wx.Sizer:
        """Previews, dual-stock cards, warnings and parameters.

        Builds the controls only; :meth:`_layout_detail_panel` is what puts
        them in a sizer, because the arrangement differs between the side
        panel and the inline row and has to be able to change while the window
        is open.
        """
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.detail_sizer = sizer
        self.detail_body_sizer = None
        self.avail_sizer = None

        self.part_heading = wx.StaticText(parent, label="Select a part.")
        self.part_heading.SetFont(theme.bold(theme.scaled(parent.GetFont(), 1.15)))

        self.part_subheading = wx.StaticText(parent, label=" ")
        self.part_subheading.SetForegroundColour(theme.colour("muted"))

        # Three equal tiles. Equal proportions with a modest minimum is what
        # keeps them equal: a large minimum makes the first tile claim its
        # full width and squeezes the last one to a sliver.
        previews = wx.BoxSizer(wx.HORIZONTAL)
        self.preview_sizer = previews
        self.symbol_preview = SvgPreviewPanel(parent, (140, 165), caption="Symbol")
        self.footprint_preview = SvgPreviewPanel(
            parent, (140, 165), caption="Footprint"
        )
        self.photo_preview = BitmapPreviewPanel(
            parent, (140, 165), caption="Photo (click to enlarge)"
        )
        # The tile is 140px of a 900px photo, so the click that enlarges it is
        # the point of having it. Advertised with the cursor as well as the
        # caption, since a preview tile does not otherwise look interactive.
        self.photo_preview.SetCursor(wx.Cursor(wx.CURSOR_MAGNIFIER))
        self.photo_preview.Bind(wx.EVT_LEFT_DOWN, self._on_photo_tile_click)
        for tile in (self.symbol_preview, self.footprint_preview, self.photo_preview):
            previews.Add(tile, 1, wx.EXPAND | wx.ALL, 4)

        stock_box = wx.StaticBoxSizer(wx.VERTICAL, parent, "Availability")
        self.stock_box = stock_box
        self.cards_sizer = wx.BoxSizer(wx.HORIZONTAL)
        cards = self.cards_sizer
        self.jlc_card = StockCard(
            stock_box.GetStaticBox(),
            "JLC ASSEMBLY",
            "jlc",
            "what JLC can place on a board",
        )
        self.retail_card = StockCard(
            stock_box.GetStaticBox(),
            "LCSC RETAIL",
            "retail",
            "what you can buy loose",
        )
        cards.Add(self.jlc_card, 1, wx.EXPAND | wx.ALL, 4)
        cards.Add(self.retail_card, 1, wx.EXPAND | wx.ALL, 4)

        self.warning_text = wx.TextCtrl(
            stock_box.GetStaticBox(),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_NONE,
        )
        # A multiline TextCtrl's best width is its longest line, and a sizer
        # treats that as a minimum: one long caveat was claiming 600px of the
        # inline row and squeezing the parameter table beside it into a sliver.
        # An explicit minimum in both axes is what puts the proportions back in
        # charge of the width.
        self.warning_text.SetMinSize(
            wx.Size(
                int(self.parent.scale_factor * 200),
                int(self.parent.scale_factor * 104),
            )
        )
        # Left to itself this is a stark near-black rectangle against the cards
        # in dark mode; on the card background it reads as part of the same box.
        self.warning_text.SetBackgroundColour(theme.card_background())

        param_box = wx.StaticBoxSizer(wx.VERTICAL, parent, "Parameters")
        self.param_box = param_box
        self.param_list = dv.DataViewListCtrl(
            param_box.GetStaticBox(), style=dv.DV_ROW_LINES | dv.DV_SINGLE
        )
        self.param_list.AppendTextColumn(
            "Parameter", width=int(self.parent.scale_factor * 130)
        )
        self.param_list.AppendTextColumn(
            "Value", width=int(self.parent.scale_factor * 150)
        )
        # Two fixed widths cannot serve both layouts: the same 390px that fits
        # the side panel does not fit the narrower inline column, where the
        # names and values then paint over each other.
        self.param_list.Bind(wx.EVT_SIZE, self._on_param_resized)
        # A DataViewListCtrl reports a best width of ~16px, so a sizer hands it
        # whatever is left after its neighbours have taken their minimums —
        # which inline was 85px, narrow enough that the value column collapsed
        # to nothing. This is the parameter table; it needs a real minimum.
        self.param_list.SetMinSize(
            wx.Size(
                int(self.parent.scale_factor * 260),
                int(self.parent.scale_factor * 90),
            )
        )
        param_box.Add(self.param_list, 1, wx.EXPAND | wx.ALL, 4)

        self._layout_detail_panel()
        return sizer

    def _layout_detail_panel(self) -> None:
        """Reflow the same detail controls for a side panel or bottom drawer."""
        pad = int(self.parent.scale_factor * DETAIL_PAD)
        self._apply_detail_minimums()
        if self.detail_body_sizer is not None:
            self.detail_body_sizer.Detach(self.preview_sizer)
            self.detail_body_sizer.Detach(self.stock_box)
            self.detail_body_sizer.Detach(self.param_box)
            self.detail_sizer.Detach(self.detail_body_sizer)
            self.detail_body_sizer = None
        self.detail_sizer.Detach(self.part_heading)
        self.detail_sizer.Detach(self.part_subheading)
        self.detail_sizer.Detach(self.preview_sizer)
        self.detail_sizer.Detach(self.stock_box)
        self.detail_sizer.Detach(self.param_box)
        self._layout_availability(pad)

        self.detail_sizer.Add(
            self.part_heading, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad + 4
        )
        self.detail_sizer.Add(
            self.part_subheading, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad + 3
        )
        if self._detail_layout == "side":
            self.detail_sizer.Add(
                self.preview_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad // 2
            )
            self.detail_sizer.Add(self.stock_box, 0, wx.EXPAND | wx.ALL, pad)
            self.detail_sizer.Add(self.param_box, 1, wx.EXPAND | wx.ALL, pad)
        else:
            body = wx.BoxSizer(wx.HORIZONTAL)
            self.detail_body_sizer = body
            # The previews go in unstretched, at the natural width of three
            # tiles: they are the block with a fixed aspect and the least to
            # gain from surplus width, and leaving them out of the proportional
            # split keeps their 440px minimum from being multiplied by it. What
            # is left is divided between the two blocks that read as text.
            body.Add(self.preview_sizer, 0, wx.EXPAND | wx.ALL, pad)
            # 13:7 rather than 2:1 because the availability block spends its
            # first 350px on the two cards, which have a fixed width — the
            # split has to be read as "the caveats get a little less than the
            # parameter table", not "availability gets twice as much".
            body.Add(self.stock_box, 13, wx.EXPAND | wx.ALL, pad)
            body.Add(self.param_box, 7, wx.EXPAND | wx.ALL, pad)
            self.detail_sizer.Add(body, 1, wx.EXPAND)

    def _apply_detail_minimums(self) -> None:
        """Set the minimum heights the current layout can actually afford.

        The two layouts have opposite scarcities. Inline is wide and short, and
        everything is side by side, so heights barely matter. The side panel is
        tall but everything is stacked, and the blocks above the parameter table
        take their minimums first — asking for a comfortable preview *and* a
        comfortable caveat box there leaves the table 50px, which is the one
        thing in the pane you actually read.
        """
        scale = self.parent.scale_factor
        side = self._detail_layout == "side"
        for tile in (self.symbol_preview, self.footprint_preview, self.photo_preview):
            tile.SetMinSize(
                wx.Size(int(scale * 140), int(scale * (140 if side else 138)))
            )
        # Width, deliberately modest: a stretchable item's minimum is what a
        # wxBoxSizer multiplies by the *total* proportion to work out its own
        # minimum, so every pixel claimed here is claimed several times over.
        self.warning_text.SetMinSize(
            wx.Size(int(scale * 190), int(scale * (84 if side else 104)))
        )

    def _layout_availability(self, pad: int) -> None:
        """Stack the stock cards above the caveats, or set them side by side.

        Inline, the detail is wide and short: two cards *above* a caveat box
        need more height than two rows have, which is what made availability
        the most cramped thing in the window. Beside each other they need half
        the height and waste none of the width the inline layout has spare.

        The cards go in unstretched in both layouts. They have a natural width —
        wide enough for a seven-figure stock number — and nothing to gain from
        more, and a stretchable item with a 350px minimum is exactly what
        inflates a sizer's own minimum past the width it has to work with.
        """
        if self.avail_sizer is not None:
            self.avail_sizer.Detach(self.cards_sizer)
            self.avail_sizer.Detach(self.warning_text)
            self.stock_box.Detach(self.avail_sizer)
            self.avail_sizer = None
        else:
            self.stock_box.Detach(self.cards_sizer)
            self.stock_box.Detach(self.warning_text)

        if self._detail_layout == "side":
            self.stock_box.Add(self.cards_sizer, 0, wx.EXPAND)
            self.stock_box.Add(self.warning_text, 1, wx.EXPAND | wx.ALL, pad // 2)
        else:
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(self.cards_sizer, 0, wx.EXPAND)
            row.Add(self.warning_text, 1, wx.EXPAND | wx.ALL, pad // 2)
            self.avail_sizer = row
            self.stock_box.Add(row, 1, wx.EXPAND)

    def _build_action_bar(self) -> wx.Sizer:
        """Import / assign / external-link buttons and the library path."""
        outer = wx.BoxSizer(wx.VERTICAL)

        # Which footprints an assign will land on. Shown because this window
        # can be re-targeted while open, so the selection is not obvious.
        self.target_label = wx.StaticText(self, label="")
        self.target_label.SetFont(theme.bold(self.target_label.GetFont()))
        outer.Add(self.target_label, 0, wx.LEFT | wx.BOTTOM, 6)

        lib_row = wx.BoxSizer(wx.HORIZONTAL)
        lib_row.Add(
            wx.StaticText(self, label="Library folder:"),
            0,
            wx.ALL | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        self.lib_path_ctrl = wx.TextCtrl(self, value=self._default_lib_root())
        lib_row.Add(self.lib_path_ctrl, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        browse = wx.Button(self, label="Browse…")
        browse.Bind(wx.EVT_BUTTON, self._on_browse)
        lib_row.Add(browse, 0, wx.ALL, 4)
        self.overwrite_cb = wx.CheckBox(self, label="Overwrite existing")
        lib_row.Add(self.overwrite_cb, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        outer.Add(lib_row, 0, wx.EXPAND)

        row = wx.BoxSizer(wx.HORIZONTAL)
        self.import_assign_button = wx.Button(self, label="Import and assign")
        self.import_assign_button.SetFont(
            theme.bold(self.import_assign_button.GetFont())
        )
        self.import_assign_button.SetToolTip(
            "Import the symbol, footprint and 3D model, then assign this LCSC "
            "number to the selected board footprints."
        )
        self.import_assign_button.Bind(
            wx.EVT_BUTTON, lambda _e: self._on_import(assign_after=True)
        )
        row.Add(self.import_assign_button, 0, wx.ALL, 4)

        self.assign_button = wx.Button(self, label="Assign number only")
        self.assign_button.SetToolTip(
            "Assign the LCSC number without importing library assets."
        )
        self.assign_button.Bind(wx.EVT_BUTTON, lambda _e: self._on_assign())
        row.Add(self.assign_button, 0, wx.ALL, 4)

        self.import_button = wx.Button(self, label="Import library assets")
        self.import_button.SetToolTip(
            "Import the EasyEDA symbol, footprint and 3D model without assigning it."
        )
        self.import_button.Bind(wx.EVT_BUTTON, lambda _e: self._on_import())
        row.Add(self.import_button, 0, wx.ALL, 4)

        row.AddStretchSpacer()

        self.lcsc_link = wx.Button(self, label="Open LCSC")
        self.lcsc_link.Bind(wx.EVT_BUTTON, lambda _e: self._open("lcsc"))
        row.Add(self.lcsc_link, 0, wx.ALL, 4)

        self.jlc_link = wx.Button(self, label="Open JLCPCB")
        self.jlc_link.Bind(wx.EVT_BUTTON, lambda _e: self._open("jlc"))
        row.Add(self.jlc_link, 0, wx.ALL, 4)

        self.datasheet_link = wx.Button(self, label="Open datasheet")
        self.datasheet_link.Bind(wx.EVT_BUTTON, lambda _e: self._open("datasheet"))
        row.Add(self.datasheet_link, 0, wx.ALL, 4)

        close = wx.Button(self, wx.ID_CANCEL, label="Close")
        row.Add(close, 0, wx.ALL, 4)

        outer.Add(row, 0, wx.EXPAND)
        return outer

    # ------------------------------------------------------------------
    # Stock view
    # ------------------------------------------------------------------

    def _view(self) -> str:
        """Return the active stock view key."""
        return STOCK_VIEWS[self.stock_view.GetSelection()][0]

    def _shows(self, source: str) -> bool:
        """Report whether ``source`` ("jlc"/"retail") is the active inventory."""
        return self._view() == source

    def _on_stock_view_changed(self, _event) -> None:
        """Re-shape the window around the newly chosen inventory."""
        self._apply_stock_view()
        self._apply_filters()

    def _apply_stock_view(self) -> None:
        """Hide the columns and cards the active view does not care about."""
        self._resize_columns()
        # Hidden via the sizer, not the window: a merely hidden window still
        # holds its slot, which would leave a gap where the other card was.
        self.cards_sizer.Show(self.jlc_card, self._shows("jlc"))
        self.cards_sizer.Show(self.retail_card, self._shows("retail"))

        label = {
            "jlc": "In JLC stock",
            "retail": "In retail stock",
        }[self._view()]
        self.in_stock_only.SetLabel(label)
        self.stock_box.Layout()
        self.right_panel.Layout()
        self.Layout()

    # ------------------------------------------------------------------
    # Grid geometry
    # ------------------------------------------------------------------

    def _resize_columns(self) -> None:
        """Fit the columns to the width the grid actually has right now.

        Every column's width is restated, not just the ones being toggled: the
        macOS DataView redistributes widths across the whole header whenever
        one changes, which otherwise leaves Description collapsed. Surplus
        width — from closing the detail pane, dropping a stock column or
        maximising the window — is shared out per :data:`FLEX_WEIGHTS`, and a
        shortfall is taken back per :data:`SHRINK_ORDER`, so a row fits the
        window at every size worth using instead of running off the right-hand
        edge behind a horizontal scrollbar.

        The width being fitted to is not the client width: the control keeps
        :attr:`_grid_indent` to itself before the first cell and adds
        :attr:`_cell_overhead` to every column it lays out. Both are measured
        rather than assumed — see :meth:`_measure_grid_metrics`. Ignoring them
        is what made a header whose numbers added up to exactly the client
        width overflow it by 135px.
        """
        if not self._alive():
            return
        hidden = {
            "jlc_stock": not self._shows("jlc"),
            "retail_stock": not self._shows("retail"),
        }
        widths = dict(self.column_widths)
        visible = [key for key, _label, _width in COLUMNS if not hidden.get(key, False)]
        used = sum(widths[key] for key in visible)
        reserve = (
            self._grid_indent + self._grid_slack + self._cell_overhead * len(visible)
        )
        slack = self.results.GetClientSize().width - reserve - used
        if slack > 0:
            for key, weight in FLEX_WEIGHTS:
                widths[key] += int(slack * weight)
        elif slack < 0:
            floors = {
                key: int(self.column_widths[key] * MIN_SHARE.get(key, 1.0))
                for key in visible
            }
            deficit = -slack
            for group in SHRINK_ORDER:
                if deficit <= 0:
                    break
                deficit = _squeeze(
                    widths, [key for key in group if key in visible], floors, deficit
                )
            # A deficit still left over means the window is narrower than the
            # floors allow for. That is the cutoff: past it the cells stop
            # being readable at all, and a scrollbar is the lesser evil.

        # Both halves matter: at a window too narrow for any slack, toggling a
        # stock column changes nothing about the widths, and a widths-only
        # guard would then skip the hide entirely.
        if (widths, hidden) == self._applied:
            return
        self._applied = (widths, hidden)
        for key, column in self.result_columns.items():
            _set_column_hidden(column, hidden.get(key, False), widths[key])
            # The renderer draws into the size it asks for, so it has to be
            # told; without this the cells paint inside a 100px box whatever
            # the column does. See CatalogTextCell.set_cell_width.
            renderer = column.GetRenderer()
            setter = getattr(renderer, "set_cell_width", None)
            if setter is not None:
                setter(widths[key])

    def _measure_grid_metrics(self) -> None:
        """Learn the grid geometry the platform decides for itself.

        Three numbers, none of them knowable before a row has been laid out and
        all three different between the native and generic DataView: the height
        of the column header, the indent before the first cell, and the padding
        the control adds to every column's set width. On this macOS build that
        padding is 17px a column — eight columns' worth of scrollbar that no
        arithmetic over the client width can see coming. Measured off row 0
        while the grid is scrolled to the top, which is where a fresh populate
        leaves it.
        """
        if not self._alive() or not self.results.GetItemCount():
            return
        item = self.results.RowToItem(0)
        rect = self.results.GetItemRect(item)
        if rect.height <= 0:
            return
        if 0 <= rect.y <= 80:
            self._header_px = rect.y

        laid_out = [
            column for column in self.results.GetColumns() if column.GetWidth() > 0
        ]
        if len(laid_out) < 2:
            return
        edges = [
            (self.results.GetItemRect(item, column).x, column.GetWidth())
            for column in laid_out
        ]
        self._grid_indent = max(0, min(64, edges[0][0]))
        # Gaps between consecutive column origins, not the row width: the last
        # column absorbs whatever width is left over, so measuring the total
        # would feed that back in and creep wider on every pass.
        gaps = [
            edges[index + 1][0] - edges[index][0] - edges[index][1]
            for index in range(len(edges) - 1)
        ]
        gaps = [gap for gap in gaps if 0 <= gap <= 48]
        if not gaps or max(gaps) == self._cell_overhead:
            return
        self._cell_overhead = max(gaps)
        self._applied = ({}, {})
        self._resize_columns()

    def _on_param_resized(self, event) -> None:
        """Share the parameter table's width between its two columns."""
        event.Skip()
        columns = self.param_list.GetColumns()
        if len(columns) != 2:
            return
        width = self.param_list.GetClientSize().width - (
            self._grid_indent + 2 * self._cell_overhead + self._grid_slack
        )
        if width < 80:
            return
        name_width = max(70, int(width * 0.42))
        columns[0].SetWidth(name_width)
        columns[1].SetWidth(max(60, width - name_width))

    def _on_grid_resized(self, event) -> None:
        """Re-fit the columns after the grid settles at its new size."""
        event.Skip()
        if not self._resize_scheduled:
            self._resize_scheduled = True
            wx.CallAfter(self._flush_column_resize)
        self._schedule_inline_reposition()

    def _on_grid_scrolled(self, event) -> None:
        """Keep the inline details physically attached while the list scrolls."""
        event.Skip()
        self._schedule_inline_reposition()

    def _flush_column_resize(self) -> None:
        """Run one deferred column re-fit for a burst of size events."""
        self._resize_scheduled = False
        self._resize_columns()

    def _schedule_inline_reposition(self) -> None:
        """Coalesce native scroll/resize bursts into one overlay placement."""
        if self._inline_rows and not self._inline_reposition_scheduled:
            self._inline_reposition_scheduled = True
            wx.CallAfter(self._position_inline_detail)

    def _inline_row_count(self) -> int:
        """How many placeholder rows the details get, given the grid's height.

        Space is reserved in whole rows, so this is the only dial there is.
        Three rows is the comfortable height for the detail content; two is the
        floor, and what a short grid gets — details that leave no result
        visible around them have stopped being an expanded row.
        """
        wanted = int(self.parent.scale_factor * INLINE_DETAIL_PX)
        count = max(2, int(round(wanted / max(1, self.row_px))))
        visible = self.results.GetClientSize().height - self._header_px
        if visible > 0:
            affordable = int(visible * INLINE_DETAIL_MAX_SHARE) // max(1, self.row_px)
            count = min(count, max(2, affordable))
        return count

    def _show_inline_detail(self) -> None:
        """Insert full-width space immediately after the selected result."""
        row = self.results.GetSelectedRow()
        if row == wx.NOT_FOUND or row >= len(self._row_hits):
            return
        if self._row_hits[row] is None:
            return

        count = self._inline_row_count()
        empty = [""] * len(COLUMNS)
        self._suppress_selection = True
        try:
            for offset in range(count):
                self.results.InsertItem(row + 1 + offset, empty)
                self._row_hits.insert(row + 1 + offset, None)
            self.results.SelectRow(row)
        finally:
            self._suppress_selection = False

        self._inline_after = row
        self._inline_rows = count
        self._inline_placed = None
        self.right_panel.Reparent(self.inline_clip)
        self.right_panel.SetMinSize(wx.Size(-1, count * self.row_px))
        self.right_panel.Show()
        last = self.results.RowToItem(row + count)
        self.results.EnsureVisible(last)
        self._inline_timer.Start(INLINE_TRACK_MS)
        wx.CallAfter(self._position_inline_detail)

    def _hide_inline_detail(self) -> None:
        """Remove the placeholder rows and return the panel to the splitter."""
        if not self._inline_rows:
            return
        selected_hit = self._current_hit()
        start = self._inline_after + 1
        self._inline_timer.Stop()
        self.inline_clip.Hide()
        self.right_panel.Hide()
        self._suppress_selection = True
        try:
            for _index in range(self._inline_rows):
                self.results.DeleteItem(start)
                del self._row_hits[start]
            if selected_hit is not None:
                for row, hit in enumerate(self._row_hits):
                    if hit is selected_hit:
                        self.results.SelectRow(row)
                        break
        finally:
            self._suppress_selection = False
        self._inline_after = wx.NOT_FOUND
        self._inline_rows = 0
        self._inline_reposition_scheduled = False
        self._inline_placed = None
        self.right_panel.Reparent(self.splitter)

    def _move_inline_detail(self, selected_hit: api.SearchHit) -> None:
        """Move an open inline panel underneath a newly selected part."""
        self._hide_inline_detail()
        self._suppress_selection = True
        try:
            for row, hit in enumerate(self._row_hits):
                if hit is selected_hit:
                    self.results.SelectRow(row)
                    break
        finally:
            self._suppress_selection = False
        self._show_inline_detail()

    def _row_top(self, row: int) -> Optional[int]:
        """Client y of ``row``'s top edge, on screen or not.

        ``GetItemRect`` answers ``(0, 0, 0, 0)`` for a row the native DataView
        has scrolled out of view, which is no use for placing an overlay that
        is meant to scroll *with* those rows. Every row is the same height, so
        one visible row — the top one, which is visible by definition — fixes
        the position of all of them.
        """
        with suppress(AttributeError, RuntimeError, wx.wxAssertionError):
            item = self.results.GetTopItem()
            if item and item.IsOk():
                anchor = self.results.ItemToRow(item)
                rect = self.results.GetItemRect(item)
                if rect.height > 0 and anchor != wx.NOT_FOUND:
                    return rect.y + (row - anchor) * rect.height
        rect = self.results.GetItemRect(self.results.RowToItem(row))
        return rect.y if rect.height > 0 else None

    def _position_inline_detail(self) -> None:
        """Clip the detail panel to whatever of its rows is on screen.

        The panel keeps its full height and slides inside a clipping window, so
        a half-scrolled detail shows its top or bottom half instead of
        disappearing: it behaves like the one very tall row it is standing in
        for. Only the slice between the header and the bottom of the grid is
        ever shown — an overlay does not clip itself, and drawing over the
        column header would look like a glitch.
        """
        self._inline_reposition_scheduled = False
        if not self._alive() or not self._inline_rows:
            return
        first = self._inline_after + 1
        if first >= self.results.GetItemCount():
            return
        row_top = self._row_top(first)
        if row_top is None:
            return

        grid_pos = self.results.GetPosition()
        grid_size = self.results.GetClientSize()
        height = self._inline_rows * self.row_px
        top = grid_pos.y + row_top
        content_top = grid_pos.y + self._header_px
        content_bottom = grid_pos.y + grid_size.height
        visible_top = max(top, content_top)
        visible_bottom = min(top + height, content_bottom)
        if visible_bottom - visible_top < 8:
            self.inline_clip.Hide()
            self._inline_placed = None
            return

        placed = (
            grid_pos.x,
            visible_top,
            grid_size.width,
            visible_bottom - visible_top,
        )
        if placed != self._inline_placed:
            self._inline_placed = placed
            self.inline_clip.SetSize(*placed)
            self.right_panel.SetSize(0, top - visible_top, grid_size.width, height)
        if not self.inline_clip.IsShown():
            self.inline_clip.Show()
            self.inline_clip.Raise()

    def _on_inline_tick(self, _event) -> None:
        """Keep the open inline panel over its rows, event or no event."""
        if self._inline_rows and self._alive():
            self._position_inline_detail()

    def _on_sash_moved(self, event) -> None:
        """Remember where the user put the sash, for the next time it opens."""
        event.Skip()
        if self._detail_layout == "side":
            self._side_sash_pos = event.GetSashPosition()

    # ------------------------------------------------------------------
    # Panel toggles
    # ------------------------------------------------------------------

    def _on_toggle_filters(self, event) -> None:
        """Handle the Filters toggle button."""
        self._set_filters_shown(event.IsChecked())

    def _set_filters_shown(self, show: bool) -> None:
        """Collapse the filter panel so the result list gets its height."""
        if not self._alive():
            return
        self.left_sizer.Show(self.facet_box, show)
        self.filter_toggle.SetValue(show)
        self.filter_toggle.SetLabel("Filters ▴" if show else "Filters ▾")
        self.left_panel.Layout()
        self._schedule_inline_reposition()

    def _on_detail_layout_changed(self, _event) -> None:
        """Move details between the side panel and the drawer below the list."""
        layout = DETAIL_LAYOUTS[self.detail_layout_choice.GetSelection()][0]
        self._set_detail_layout(layout)

    def _set_detail_layout(self, layout: str, persist: bool = True) -> None:
        """Switch detail orientation without discarding the loaded part."""
        valid = {key for key, _label in DETAIL_LAYOUTS}
        if not self._alive() or layout not in valid or layout == self._detail_layout:
            return

        was_shown = self._details_shown
        if self._detail_layout == "below":
            self._hide_inline_detail()
        elif self.splitter.IsSplit():
            self._side_sash_pos = self.splitter.GetSashPosition()
            self.splitter.Unsplit(self.right_panel)

        self._detail_layout = layout
        self.detail_layout_choice.SetSelection(
            next(
                index
                for index, (key, _label) in enumerate(DETAIL_LAYOUTS)
                if key == layout
            )
        )
        self._layout_detail_panel()
        if was_shown:
            self._split_details()
        self.right_panel.Layout()
        self.Layout()
        self._applied = ({}, {})
        wx.CallAfter(self._resize_columns)

        if persist:
            settings = getattr(self.parent, "settings", None)
            if isinstance(settings, dict):
                settings.setdefault("lcsc", {})["explorer_detail_layout"] = layout
                save = getattr(self.parent, "save_settings", None)
                if callable(save):
                    try:
                        save()
                    except Exception:  # noqa: BLE001 - preference is non-critical
                        self.logger.debug(
                            "could not save explorer detail layout", exc_info=True
                        )

    def _split_details(self) -> None:
        """Open the detail pane using the selected orientation and saved sash."""
        if self._detail_layout == "below":
            self._show_inline_detail()
        else:
            minimum = int(self.parent.scale_factor * 300)
            self.splitter.SetMinimumPaneSize(minimum)
            extent = self.splitter.GetClientSize().width
            position = self._side_sash_pos or int(extent * 0.66)
            if extent > 2 * minimum:
                position = max(minimum, min(position, extent - minimum))
            else:
                position = max(1, extent // 2)
            self.splitter.SplitVertically(self.left_panel, self.right_panel, position)

    def _set_details_shown(self, show: bool) -> None:
        """Split the detail pane in or out, keeping the sash where it was."""
        if not self._alive() or show == self._details_shown:
            return
        if show and self._current_hit() is None:
            return
        if show:
            self._split_details()
        elif self._detail_layout == "below":
            self._hide_inline_detail()
        else:
            self._side_sash_pos = self.splitter.GetSashPosition()
            self.splitter.Unsplit(self.right_panel)
        self._details_shown = show
        self.Layout()
        if show:
            # Nothing was fetched for the selected row while the pane was
            # closed, so opening it is what starts the work.
            self._load_details()

    def _on_grid_click(self, event) -> None:
        """Open the photo viewer on a thumbnail, else toggle the detail pane.

        ``EVT_DATAVIEW_SELECTION_CHANGED`` does not fire for a click on the
        row that is already selected — which is exactly the gesture that has
        to close the pane again — so the raw click is watched as well.

        Two guards on the toggle, because the two events arrive in a
        platform-dependent order. "The row under the pointer is already the
        selected one" catches a repeat click where the mouse event comes
        first; the ``_selection_just_moved`` flag catches the reverse, where
        the control has already moved the selection by the time the click
        reaches us and every click would otherwise look like a repeat. A click
        that moved the selection is handled by :meth:`_on_row_selected`.

        The photo column is taken out ahead of all of that, and deliberately
        without either guard: clicking a picture means "show me that picture"
        on any row, selected or not, and it must not also toggle the pane shut
        underneath the window it just opened.
        """
        event.Skip()
        item, column = self.results.HitTest(event.GetPosition())
        if not item or not item.IsOk():
            return
        row = self.results.ItemToRow(item)

        if self._is_photo_column(column) and 0 <= row < len(self._row_hits):
            # Deferred for the same reason as the toggle below: opening a
            # window under a mouse-down wx is still dispatching can strand the
            # capture on the grid.
            wx.CallAfter(self._open_photo_viewer, self._row_hits[row])
            return

        selected = self.results.GetSelectedRow()
        if selected == wx.NOT_FOUND or self._selection_just_moved:
            return
        if row != selected:
            return
        # Deferred: re-splitting underneath a mouse-down that wx has not
        # finished dispatching is how you end up with a stuck capture.
        wx.CallAfter(self._set_details_shown, not self._details_shown)

    def _on_photo_tile_click(self, event) -> None:
        """Enlarge the detail pane's photo tile into the viewer window."""
        event.Skip()
        hit = self._current_hit()
        if hit is not None:
            wx.CallAfter(self._open_photo_viewer, hit)

    def _is_photo_column(self, column) -> bool:
        """Report whether ``column`` from a hit test is the thumbnail column.

        Compared by model index rather than by identity: ``HitTest`` hands
        back a fresh proxy around the same C++ column, and two proxies for one
        object are not ``==`` on every wxPython build.
        """
        if column is None:
            return False
        try:
            return column.GetModelColumn() == COLUMN_INDEX["photo"]
        except (RuntimeError, AttributeError):  # pragma: no cover - proxy churn
            return False

    def _open_photo_viewer(self, hit: api.SearchHit) -> None:
        """Show ``hit``'s photos full size, reusing an already-open viewer.

        Reused rather than stacked: the gesture this is for is clicking down a
        column of thumbnails comparing packages, and that should retarget one
        window instead of burying the screen in them.
        """
        if not self._alive() or hit is None:
            return
        subtitle = " · ".join(
            part for part in (hit.model, hit.brand, hit.package) if part
        )
        viewer = self._photo_viewer
        # A destroyed wx window's proxy is falsy, which is how a viewer the
        # user closed is told apart from one still on screen.
        if viewer:
            viewer.show_part(hit.lcsc, subtitle, hit.photo_url)
            viewer.Raise()
            return
        self._photo_viewer = PhotoViewerDialog(self, hit.lcsc, subtitle, hit.photo_url)
        self._photo_viewer.Show()

    def _on_row_activated(self, _event) -> None:
        """Assign the double-clicked part's LCSC number, then get out of the way.

        Double-click is the gesture a trackpad produces by accident, so it does
        the one thing here that is cheap to undo: it writes the LCSC number onto
        the selected footprints and closes the window. Importing symbol,
        footprint and 3D model into a library on disk is a side effect nobody
        wants to discover they triggered, so it stays behind the buttons in the
        action bar.
        """
        hit = self._current_hit()
        if hit is None:
            return
        if not self.references:
            self.status.SetLabel(
                f"{hit.lcsc}: no footprint selected, so there is nothing to assign — "
                "use the buttons below to import it into a library."
            )
            return
        self._on_assign()
        self.Close()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_refresh(self, _event) -> None:
        """Clear caches and re-run the current search."""
        api.clear_cache()
        self._retail = {}
        self._thumbs = {}
        self._start_search()

    def _start_search(self) -> None:
        """Kick off a background search for the current keyword."""
        keyword = self.keyword.GetValue().strip()
        if not keyword:
            self.status.SetLabel("Enter a keyword or an LCSC part number.")
            return

        self._search_token += 1
        self._retail_token += 1  # a new result set invalidates the retail fill
        token = self._search_token
        self.status.SetLabel(f"Searching for '{keyword}' …")
        self.search_button.Disable()

        part_type = {1: "base", 2: "expand"}.get(self.lib_filter.GetSelection())

        def work() -> None:
            total, hits = api.jlc_search(
                keyword=keyword, page_size=PAGE_SIZE, part_type=part_type
            )
            self._post(self._search_done, token, keyword, total, hits)

        threading.Thread(target=work, daemon=True, name="LcscSearch").start()

    def _search_done(
        self, token: int, keyword: str, total: int, hits: List[api.SearchHit]
    ) -> None:
        """Receive search results on the UI thread."""
        if not self._alive() or token != self._search_token:
            return  # a newer search superseded this one, or we are closing
        self.search_button.Enable()
        # A new result set has no selected part, so the pane would be sitting
        # there describing one that is no longer on screen. Closing it also
        # hands the grid its full width back for the first read of the
        # results, which is when the width matters most.
        self._set_details_shown(False)
        self._hits = hits
        self._facets = api.build_facets(hits)
        self._selected_facets = {}
        self._rebuild_facets()
        self._apply_filters()

        if total > len(hits):
            self.status.SetLabel(
                f"{total} parts match '{keyword}'; showing the first {len(hits)}. "
                "Narrow the keyword to see the rest."
            )
        elif not hits:
            self.status.SetLabel(f"No parts found for '{keyword}'.")

    def _rebuild_facets(self) -> None:
        """Rebuild the parametric filter controls for the current results."""
        self.facet_scroller.Freeze()
        try:
            self.facet_grid.Clear(delete_windows=True)
            self._facet_controls = {}

            if not self._facets:
                self.facet_hint.SetLabel(
                    "No discriminating attributes in these results — "
                    "the JLC parts library returned no parametric data for them."
                )
            else:
                parent = self.facet_scroller
                self.facet_hint.SetLabel(
                    f"{len(self._facets)} attributes available; tick any number of "
                    "values per attribute. Counts are over the fetched result set."
                )
                for name in sorted(self._facets):
                    self.facet_grid.Add(
                        wx.StaticText(parent, label=f"{name}:"),
                        0,
                        wx.ALIGN_CENTER_VERTICAL,
                    )
                    control = FacetFilterCtrl(
                        parent,
                        name,
                        self._facets[name],
                        self._on_facet_changed,
                    )
                    self._facet_controls[name] = control
                    self.facet_grid.Add(control, 1, wx.EXPAND)

                clear = wx.Button(parent, label="Clear filters")
                clear.Bind(wx.EVT_BUTTON, self._on_clear_facets)
                self.facet_grid.Add(clear, 0, wx.ALIGN_CENTER_VERTICAL)

            # The control count changed, so the scroller needs a fresh virtual
            # size and every ancestor needs to re-run its layout — otherwise
            # the new rows draw on top of each other.
            self.facet_grid.Layout()
            self.facet_scroller.FitInside()
            self.facet_box.Layout()
            self.left_panel.Layout()
            self.Layout()
        finally:
            self.facet_scroller.Thaw()

    def _on_facet_changed(self, name: str, values: Set[str]) -> None:
        """Record one attribute's ticked values and schedule a re-filter.

        Deferred rather than applied here: the popup is still open and the user
        is most likely mid-selection. See :data:`FILTER_DEBOUNCE_MS`.
        """
        if values:
            self._selected_facets[name] = set(values)
        else:
            self._selected_facets.pop(name, None)
        self._schedule_filters()

    def _schedule_filters(self) -> None:
        """Re-filter shortly, collapsing a burst of ticks into one pass."""
        # Restarting a running one-shot timer is the debounce: the rebuild
        # happens once the user pauses, not once per click.
        self._filter_timer.Start(FILTER_DEBOUNCE_MS, wx.TIMER_ONE_SHOT)

    def _on_filter_tick(self, _event) -> None:
        """Apply the filters the ticks have accumulated."""
        if not self._alive():
            return
        self._apply_filters()

    def _on_clear_facets(self, _event) -> None:
        """Untick every facet, then re-filter once."""
        self._selected_facets = {}
        for control in self._facet_controls.values():
            control.clear()
        self._apply_filters()

    def _apply_filters(self) -> None:
        """Apply facets, the stock toggle and the sort, then repopulate."""
        # A debounced pass may still be queued behind a direct call — the sort
        # choice and the stock toggle both come straight here — and running it
        # afterwards would rebuild the same grid a second time.
        self._filter_timer.Stop()
        hits = api.filter_hits(self._hits, self._selected_facets)
        if self.in_stock_only.GetValue():
            hits = [hit for hit in hits if self._has_stock(hit)]
        self._visible = self._sorted(hits)
        self._populate(self._visible)
        # Both passes start together. Thumbnails used to wait for the stock
        # fill, because the photo URL came out of the retail response and
        # starting early would only have duplicated its requests. The search
        # now carries the photo ids, so the two passes share nothing, and
        # making pictures wait on a hundred sequential stock lookups just left
        # the grid grey for the whole fill — or forever, when retail was
        # unreachable and the pass never finished.
        self._start_thumb_fill()
        if self._shows("retail"):
            self._start_retail_fill()

    def _has_stock(self, hit: api.SearchHit) -> bool:
        """Report whether ``hit`` has stock in the inventory now on show."""
        if self._view() == "jlc":
            return (hit.stock or 0) > 0
        # A retail figure we have not fetched yet counts as "keep it" — hiding
        # rows we simply have not looked at would silently shrink the result
        # set as the background fill progressed.
        if hit.lcsc not in self._retail:
            return True
        return (self._retail.get(hit.lcsc) or 0) > 0

    def _sorted(self, hits: List[api.SearchHit]) -> List[api.SearchHit]:
        """Order ``hits`` by the active sort mode.

        Unknown values sort last in every mode so that a part we have no data
        for never displaces one we do.
        """
        mode = SORT_MODES[self.sort_choice.GetSelection()][0]
        if mode == "relevance":
            return list(hits)
        if mode == "jlc":
            return sorted(hits, key=lambda h: -(h.stock or 0))
        if mode == "retail":
            return sorted(hits, key=lambda h: -(self._retail.get(h.lcsc) or 0))
        if mode == "price":
            return sorted(
                hits, key=lambda h: h.price if h.price is not None else float("inf")
            )
        return sorted(hits, key=lambda h: h.min_qty or 1)

    def _retail_text(self, hit: api.SearchHit) -> str:
        """Format the retail cell for ``hit`` from whatever we know so far."""
        if not self._shows("retail"):
            return ""
        if hit.lcsc not in self._retail:
            return PENDING
        stock = self._retail[hit.lcsc]
        return UNKNOWN if stock is None else f"{stock:,}"

    def _populate(self, hits: List[api.SearchHit]) -> None:
        """Fill the result grid.

        The photo cell carries the LCSC code rather than artwork — see
        :class:`ThumbCell` for why the bitmap stays out of the model.
        """
        if self._details_shown:
            self._set_details_shown(False)
        # Frozen for the duration: every AppendItem on a DataView with custom
        # renderers is a chance to repaint, and a hundred of them is long
        # enough on the UI thread to be felt as a stall by whatever the user
        # is clicking. Thawed in a finally so an exception mid-fill cannot
        # leave the grid permanently unpainted.
        self.results.Freeze()
        try:
            self.results.DeleteAllItems()
            self._row_hits = []
            for hit in hits:
                part_meta = " · ".join(
                    value for value in (hit.lcsc, hit.library_type) if value
                )
                maker_meta = hit.package or "Package not specified"
                price = f"${hit.price:.4f}" if hit.price is not None else "—"
                minimum = f"Min order {hit.min_qty or 1:,}"
                self.results.AppendItem(
                    [
                        hit.lcsc,
                        f"{hit.model or '—'}\n{part_meta}",
                        f"{hit.description or '—'}\n{hit.category}",
                        f"{hit.brand or '—'}\n{maker_meta}",
                        f"{hit.stock:,}" if hit.stock is not None else UNKNOWN,
                        self._retail_text(hit),
                        f"{price}\n{minimum}",
                        "",
                    ]
                )
                self._row_hits.append(hit)
        finally:
            self.results.Thaw()
        # A populated, scrolled-to-the-top grid is the only place the
        # platform's own geometry can be read off, and it may re-fit the
        # columns — so it happens here rather than at construction.
        self._measure_grid_metrics()
        self._update_status()
        self._update_actions()

    def _update_status(self) -> None:
        """Describe the visible result set in terms of the active view."""
        total = len(self._hits)
        shown = len(self._visible)
        parts = [f"{shown} shown of {total} fetched"]
        if self._shows("jlc"):
            count = sum(1 for hit in self._visible if (hit.stock or 0) > 0)
            parts.append(f"{count} with JLC assembly stock")
        if self._shows("retail"):
            count = sum(
                1 for hit in self._visible if (self._retail.get(hit.lcsc) or 0) > 0
            )
            asked = sum(1 for hit in self._visible if hit.lcsc in self._retail)
            pending = shown - asked
            # Distinguishing a refused host from an empty warehouse is the whole
            # point: they look identical in the grid, and only one of them means
            # "pick a different part". The rows are filled top-first, so a fill
            # that stopped early has still answered for what is on screen.
            unreachable = api.retail_unreachable()
            if unreachable and asked:
                parts.append(
                    f"{count} with LCSC retail stock — LCSC stopped answering "
                    f"after {asked} of {shown} rows (rate limit); Refresh to "
                    "continue"
                )
            elif unreachable:
                parts.append(
                    "LCSC retail stock unavailable — both lcsc.com and easyeda.com "
                    "are refusing requests. Try again in a few minutes, or switch "
                    "Inventory to JLC assembly"
                )
            else:
                suffix = f" ({pending} still loading)" if pending else ""
                parts.append(f"{count} with LCSC retail stock{suffix}")
        self.status.SetLabel(" — ".join(parts) + ".")

    # ------------------------------------------------------------------
    # LCSC retail stock fill
    # ------------------------------------------------------------------

    def _start_retail_fill(self) -> bool:
        """Fetch LCSC retail stock for the visible rows in the background.

        The keyword search only reports JLC assembly stock, so the retail
        column has to be filled one part at a time. Bounded in breadth
        (``RETAIL_FILL_LIMIT`` rows) and in concurrency
        (``RETAIL_FILL_WORKERS``), and every response is cached for five
        minutes, so paging back and forth costs nothing.

        Returns whether a fill was actually launched.
        """
        # Checked before anything is spawned, not just inside the workers: a
        # fill that ends without recording anything leaves its rows pending, and
        # when the user is filtering or sorting on retail stock ``_retail_fill_
        # done`` re-filters — which would start another fill over the same rows,
        # for as long as the hosts stay blocked. Refresh re-arms them.
        if api.retail_unreachable():
            return False
        wanted = [
            hit.lcsc
            for hit in self._visible[:RETAIL_FILL_LIMIT]
            if hit.lcsc and hit.lcsc not in self._retail
        ]
        if not wanted:
            return False

        self._retail_token += 1
        token = self._retail_token

        # Plain daemon threads over a shared queue rather than a
        # ThreadPoolExecutor: the executor's workers are non-daemon and its
        # shutdown joins them, so a fill in progress would both refuse to
        # cancel promptly when the user retypes and hold up KiCad's own exit.
        # Draining a queue costs nothing and dies with the process.
        work_queue: queue.Queue = queue.Queue()
        for lcsc in wanted:
            work_queue.put(lcsc)
        remaining = threading.Semaphore(0)
        workers = min(RETAIL_FILL_WORKERS, len(wanted))

        def worker() -> None:
            # The release has to happen however this exits, or the reaper
            # waits on a semaphore no one will ever post to.
            try:
                while token == self._retail_token:
                    # Every source refusing us is not a fact about these parts,
                    # and recording it per row would draw the whole column as
                    # "?" — which reads as "out of stock". Leave the rest
                    # pending and let the status line say what happened.
                    if api.retail_unreachable():
                        return
                    try:
                        lcsc = work_queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        stock = api.retail_stock(lcsc)
                    except Exception:  # noqa: BLE001 - one bad part, not a crash
                        logger.debug("retail stock for %s failed", lcsc, exc_info=True)
                        stock = None
                    if not self._post(self._retail_ready, token, lcsc, stock):
                        return
            finally:
                remaining.release()

        def reaper() -> None:
            for _ in range(workers):
                remaining.acquire()
            self._post(self._retail_fill_done, token)

        for index in range(workers):
            threading.Thread(
                target=worker, daemon=True, name=f"LcscRetail{index}"
            ).start()
        threading.Thread(target=reaper, daemon=True, name="LcscRetailDone").start()
        return True

    def _retail_ready(self, token: int, lcsc: str, stock: Optional[int]) -> None:
        """Write one retail figure into the grid. Runs on the UI thread."""
        if not self._alive() or token != self._retail_token:
            return
        self._retail[lcsc] = stock
        self._set_retail_cell(lcsc, stock)
        self._update_status()

    def _retail_fill_done(self, token: int) -> None:
        """Settle the view once, now that the retail figures are all in.

        Re-filtering and re-sorting are deferred to here rather than applied
        per arrival: rows rearranging themselves one at a time under the
        cursor is far worse than a single settled update.
        """
        if not self._alive() or token != self._retail_token:
            return
        filtering_on_retail = self.in_stock_only.GetValue() and self._shows("retail")
        sorting_on_retail = SORT_MODES[self.sort_choice.GetSelection()][0] == "retail"
        if filtering_on_retail or sorting_on_retail:
            # Re-filtering repopulates and starts the thumbnail pass itself.
            self._apply_filters()
        else:
            self._update_status()
            self._start_thumb_fill()

    def _set_retail_cell(self, lcsc: str, stock: Optional[int]) -> None:
        """Update every visible row for ``lcsc`` with a retail figure."""
        if not self._shows("retail"):
            return
        text = UNKNOWN if stock is None else f"{stock:,}"
        column = COLUMN_INDEX["retail_stock"]
        for row, hit in enumerate(self._row_hits):
            if hit is not None and hit.lcsc == lcsc:
                self.results.SetTextValue(text, row, column)

    # ------------------------------------------------------------------
    # Grid thumbnails
    # ------------------------------------------------------------------

    def _start_thumb_fill(self) -> None:
        """Fetch product thumbnails for the top of the grid, in the background.

        The photo URL is already in hand: the search response carries a file
        id for every row's primary shot, so this pass is pure image bytes with
        no JSON lookup in front of it. That was not always true — the URL used
        to come from the per-part retail record, which made a thumbnail cost a
        request even when the photo itself was cached, and made the whole grid
        go blank whenever ``lcsc.com`` was unreachable.

        Still bounded harder than the stock fill in both breadth and
        concurrency: a picture never decides a part choice on its own.
        """
        wanted = [
            (hit.lcsc, hit.thumbnail_url)
            for hit in self._visible[:THUMB_FILL_LIMIT]
            if hit.lcsc and hit.lcsc not in self._thumbs
        ]
        if not wanted:
            return

        self._thumb_token += 1
        token = self._thumb_token
        size = self.thumb_px

        work_queue: queue.Queue = queue.Queue()
        for item in wanted:
            work_queue.put(item)

        def worker() -> None:
            while token == self._thumb_token:
                try:
                    lcsc, url = work_queue.get_nowait()
                except queue.Empty:
                    break
                data = None
                try:
                    # A part with no id in the search payload still gets one
                    # chance through the slower per-part lookup.
                    data = api.fetch_image(url or api.retail_thumbnail_url(lcsc))
                except Exception:  # noqa: BLE001 - one missing photo, not a crash
                    logger.debug("thumbnail for %s failed", lcsc, exc_info=True)
                if not self._post(self._thumb_ready, token, lcsc, data, size):
                    return

        for index in range(min(THUMB_FILL_WORKERS, len(wanted))):
            threading.Thread(
                target=worker, daemon=True, name=f"LcscThumb{index}"
            ).start()

    def _thumb_ready(
        self, token: int, lcsc: str, data: Optional[bytes], size: int
    ) -> None:
        """Decode one thumbnail and schedule a repaint. Runs on the UI thread.

        Decoding happens here rather than in the worker because ``wx.Image``
        and ``wx.Bitmap`` are not safe to build off the UI thread. A 224px JPEG
        is a fraction of a millisecond to scale, so this does not stutter.
        """
        if not self._alive() or token != self._thumb_token:
            return
        if len(self._thumbs) >= MAX_CACHED_THUMBS:
            self._thumbs.clear()
        self._thumbs[lcsc] = _decode_thumbnail(data, size)
        # One repaint per idle drain rather than one per arrival: three workers
        # landing photos on a 60-row grid would otherwise force 60 full
        # redraws of a control that has no single-row invalidation.
        if not self._thumb_refresh_scheduled:
            self._thumb_refresh_scheduled = True
            wx.CallAfter(self._flush_thumb_refresh)

    def _flush_thumb_refresh(self) -> None:
        """Repaint the grid once for a batch of arrived thumbnails."""
        self._thumb_refresh_scheduled = False
        if self._alive():
            self.results.Refresh()

    # ------------------------------------------------------------------
    # Detail pane
    # ------------------------------------------------------------------

    def _current_hit(self) -> Optional[api.SearchHit]:
        """Return the currently selected search hit, if any."""
        row = self.results.GetSelectedRow()
        if row == wx.NOT_FOUND or row >= len(self._row_hits):
            return None
        return self._row_hits[row]

    def _on_row_selected(self, _event) -> None:
        """Open the detail pane on the newly selected part and fill it.

        Picking a part is the gesture that asks for its details, so this is
        what opens the pane; clicking that same part again is what closes it.
        The flag tells :meth:`_on_grid_click` that this click moved the
        selection and so is not the close gesture — it is cleared as soon as
        the event burst for the click has drained.
        """
        if self._suppress_selection:
            return
        hit = self._current_hit()
        if hit is None:
            if self._inline_rows and self._inline_after != wx.NOT_FOUND:
                self._suppress_selection = True
                try:
                    self.results.SelectRow(self._inline_after)
                finally:
                    self._suppress_selection = False
            return
        self._detail_token += 1  # anything in flight is for the old row
        self._report = None
        self._selection_just_moved = True
        wx.CallAfter(self._clear_selection_moved)
        self._update_actions()
        if self._details_shown:
            if self._detail_layout == "below":
                self._move_inline_detail(hit)
            self._load_details()
        else:
            self._set_details_shown(True)  # which loads

    def _clear_selection_moved(self) -> None:
        """Forget that the last click moved the selection."""
        self._selection_just_moved = False

    def _load_details(self) -> None:
        """Load availability, previews and photo for the selected row.

        Three independent fetches rather than one chained job: the numbers a
        decision actually rests on arrive in a fraction of a second, and the
        drawings and the photo fill in behind them instead of holding them up.
        """
        hit = self._current_hit()
        if hit is None:
            return

        self._detail_token += 1
        token = self._detail_token
        self._report = None

        self.part_heading.SetLabel(f"{hit.lcsc}  ·  {hit.model or '—'}")
        self.part_subheading.SetLabel(
            " · ".join(part for part in (hit.brand, hit.package, hit.category) if part)
            or " "
        )
        self.jlc_card.set_value(
            hit.stock, [f"{hit.library_type} part" if hit.library_type else ""]
        )
        self.retail_card.set_pending()
        self.warning_text.SetValue("")
        self.param_list.DeleteAllItems()
        self.symbol_preview.clear("Loading …")
        self.footprint_preview.clear("Loading …")
        self.photo_preview.clear("Loading …")
        self.right_panel.Layout()

        needed = max(1, len(self.references))

        def fetch_report() -> None:
            report = api.stock_report(hit.lcsc, needed_qty=needed)
            self._post(self._report_done, token, hit, report)

        def fetch_previews() -> None:
            symbol_svg = footprint_svg = None
            try:
                # Deferred: vendored, and only needed when a row is selected.
                from easyeda2kicad.easyeda.easyeda_api import (  # noqa: PLC0415
                    EasyedaApi,
                )
                from easyeda2kicad.easyeda.easyeda_svg_renderer import (  # noqa: PLC0415
                    render_footprint_svg,
                    render_symbol_svg,
                )

                cad = EasyedaApi().get_cad_data_of_component(lcsc_id=hit.lcsc)
                if cad:
                    try:
                        symbol_svg = render_symbol_svg(cad)
                    except Exception:  # noqa: BLE001
                        logger.debug("symbol SVG render failed", exc_info=True)
                    try:
                        footprint_svg = render_footprint_svg(cad)
                    except Exception:  # noqa: BLE001
                        logger.debug("footprint SVG render failed", exc_info=True)
            except Exception:  # noqa: BLE001
                logger.debug("EasyEDA preview fetch failed", exc_info=True)
            self._post(self._previews_done, token, symbol_svg, footprint_svg)

        threading.Thread(target=fetch_report, daemon=True, name="LcscReport").start()
        threading.Thread(target=fetch_previews, daemon=True, name="LcscPreview").start()

    def _report_done(
        self, token: int, hit: api.SearchHit, report: api.StockReport
    ) -> None:
        """Render the availability report. Runs on the UI thread."""
        if not self._alive() or token != self._detail_token:
            return
        self._report = report
        self._retail[hit.lcsc] = report.retail_stock
        self._refresh_retail_card()

        library_type = report.library_type or hit.library_type
        self.jlc_card.set_value(
            report.jlc_stock if report.jlc_stock is not None else hit.stock,
            [
                f"{library_type} part" if library_type else "",
                f"min purchase {report.min_purchase:,}" if report.min_purchase else "",
            ],
        )

        lines: List[str] = []
        if report.retail_ladder:
            qty = max(1, len(self.references))
            unit = api.unit_price_at(report.retail_ladder, qty)
            if unit is not None:
                lines.append(
                    f"LCSC retail unit price at qty {qty}: ${unit:.4f} "
                    f"(total ${unit * qty:.2f})"
                )
        lines.extend(report.warnings)
        self.warning_text.SetValue("\n".join(f"• {line}" for line in lines))

        self.param_list.DeleteAllItems()
        for name, value in report.parameters or list(hit.attributes.items()):
            self.param_list.AppendItem([name, value])

        self.stock_box.Layout()
        self.right_panel.Layout()
        self._update_actions()
        self._set_retail_cell(hit.lcsc, report.retail_stock)

        # Last of everything: the photo is the only thing here that nobody
        # needs in order to choose a part, so it never delays the rest.
        self._start_photo_fetch(token, report)

    def _refresh_retail_card(self) -> None:
        """Restate the retail card from the current report."""
        report = self._report
        if report is None:
            return
        detail = []
        if report.retail_domestic is not None or report.retail_overseas is not None:
            detail.append(
                f"CN {_fmt(report.retail_domestic)} · intl {_fmt(report.retail_overseas)}"
            )
        if report.retail_min_buy:
            detail.append(f"min buy {report.retail_min_buy:,}")
        self.retail_card.set_value(report.retail_stock, detail)

    def _previews_done(
        self, token: int, symbol_svg: Optional[str], footprint_svg: Optional[str]
    ) -> None:
        """Render the symbol and footprint drawings. Runs on the UI thread."""
        if not self._alive() or token != self._detail_token:
            return
        self.symbol_preview.set_svg(symbol_svg, "No EasyEDA symbol for this part")
        self.footprint_preview.set_svg(
            footprint_svg, "No EasyEDA footprint for this part"
        )

    def _start_photo_fetch(self, token: int, report: api.StockReport) -> None:
        """Fetch the product photo — the lowest-priority thing in the window."""
        urls = list(report.images)
        if not urls:
            self.photo_preview.clear("No photo for this part")
            return

        def work() -> None:
            data = None
            for url in urls:
                if token != self._detail_token:
                    return
                data = api.fetch_image(url)
                if data:
                    break
            self._post(self._photo_done, token, data)

        threading.Thread(target=work, daemon=True, name="LcscPhoto").start()

    def _photo_done(self, token: int, data: Optional[bytes]) -> None:
        """Show the fetched photo. Runs on the UI thread."""
        if not self._alive() or token != self._detail_token:
            return
        self.photo_preview.set_image_bytes(data, "Photo unavailable")

    def _update_actions(self) -> None:
        """Enable/disable buttons based on the current selection."""
        hit = self._current_hit()
        has = hit is not None
        for button in (
            self.import_button,
            self.assign_button,
            self.import_assign_button,
            self.lcsc_link,
            self.jlc_link,
        ):
            button.Enable(has)
        self.assign_button.Enable(has and bool(self.references))
        self.import_assign_button.Enable(has and bool(self.references))
        self.datasheet_link.Enable(
            has
            and bool(
                (self._report and self._report.datasheet) or (hit and hit.datasheet)
            )
        )

    # ------------------------------------------------------------------
    # Library root
    # ------------------------------------------------------------------

    def _default_lib_root(self) -> str:
        """Default to a project-local library so the design stays portable."""
        try:
            board_file = self.parent.pcbnew.GetBoard().GetFileName()
        except Exception:  # noqa: BLE001 - standalone / no board open
            board_file = ""
        configured = (
            self.parent.settings.get("lcsc", {}).get("library_root")
            if hasattr(self.parent, "settings")
            else None
        )
        if configured:
            return str(configured)
        if board_file:
            return str(Path(board_file).parent / "lcsc-lib")
        return str(Path.home() / "Documents" / "KiCad" / "lcsc-lib")

    def _importer(self) -> LcscImporter:
        """Build an importer for the currently configured library root."""
        root = self.lib_path_ctrl.GetValue().strip() or self._default_lib_root()
        try:
            board_file = self.parent.pcbnew.GetBoard().GetFileName()
        except Exception:  # noqa: BLE001
            board_file = ""
        project_dir = str(Path(board_file).parent) if board_file else ""
        project_relative = is_inside(str(root), project_dir)
        return LcscImporter(
            root=root, lib_name=DEFAULT_LIB_NAME, project_relative=project_relative
        )

    def _on_browse(self, _event) -> None:
        """Pick the directory the library triplet is written into."""
        with wx.DirDialog(
            self,
            "Choose the directory for the LCSC library",
            defaultPath=self.lib_path_ctrl.GetValue(),
        ) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.lib_path_ctrl.SetValue(dialog.GetPath())

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _open(self, which: str) -> None:
        """Open the LCSC page, JLC page or datasheet in a browser."""
        hit = self._current_hit()
        if hit is None:
            return
        if which == "lcsc":
            url = f"https://www.lcsc.com/product-detail/{hit.lcsc}.html"
        elif which == "jlc":
            url = "https://jlcpcb.com/parts/componentSearch?searchTxt=" + hit.lcsc
        else:
            url = (self._report.datasheet if self._report else "") or hit.datasheet
        if url:
            webbrowser.open(url)

    def _on_import(self, assign_after: bool = False) -> None:
        """Import the selected part into the KiCad library."""
        hit = self._current_hit()
        if hit is None:
            return
        importer = self._importer()
        overwrite = self.overwrite_cb.GetValue()

        self.import_button.Disable()
        self.import_assign_button.Disable()
        self.status.SetLabel(f"Importing {hit.lcsc} …")

        try:
            board_file = self.parent.pcbnew.GetBoard().GetFileName()
        except Exception:  # noqa: BLE001
            board_file = ""
        project_dir = str(Path(board_file).parent) if board_file else ""

        def work() -> None:
            result = importer.import_part(hit.lcsc, overwrite=overwrite)
            actions = []
            if result.ok:
                actions = importer.register_libraries(project_dir=project_dir)
            self._post(self._import_done, hit, result, actions, assign_after)

        threading.Thread(target=work, daemon=True, name="LcscImport").start()

    def _import_done(self, hit, result, actions, assign_after: bool) -> None:
        """Report the import outcome and optionally assign the part."""
        del hit
        if not self._alive():
            return
        self.import_button.Enable()
        self._update_actions()
        self.status.SetLabel(result.describe())

        for line in actions:
            self.logger.info(line)

        if result.errors:
            wx.MessageBox(
                result.describe() + "\n\n" + "\n".join(actions),
                "LCSC import failed",
                style=wx.ICON_ERROR,
            )
            return

        message = result.describe()
        if actions:
            message += "\n\n" + "\n".join(actions)
        message += (
            "\n\nKiCad caches library tables at startup — if the new library "
            "does not appear in the symbol chooser, restart KiCad."
        )
        wx.MessageBox(message, "LCSC import", style=wx.ICON_INFORMATION)

        if assign_after:
            self._on_assign()

    def _on_assign(self) -> None:
        """Assign the selected LCSC number to the footprints we were opened for."""
        hit = self._current_hit()
        if hit is None or not self.references:
            return
        wx.PostEvent(
            self.parent,
            AssignPartsEvent(
                lcsc=hit.lcsc,
                type=hit.library_type,
                # Not `or 0`: a search that reported no figure means we do not
                # know the stock, and the part list draws that as blank rather
                # than claiming the part is out of stock.
                stock=hit.stock,
                references=self.references,
            ),
        )
        self.status.SetLabel(
            f"Assigned {hit.lcsc} to {len(self.references)} footprint(s)."
        )


def _fmt(value: Optional[int]) -> str:
    """Format an optional integer for display."""
    return UNKNOWN if value is None else f"{value:,}"


def _decode_thumbnail(data: Optional[bytes], size: int) -> Optional[wx.Bitmap]:
    """Decode ``data`` into a square-fitted bitmap of at most ``size`` px.

    Returns ``None`` for missing or undecodable bytes; the caller stores that
    as "asked, nothing to show" so the part is not fetched again.
    """
    if not data or size < 4:
        return None
    try:
        image = wx.Image(io.BytesIO(data))
        if not image.IsOk():
            return None
        src_w = max(1, image.GetWidth())
        src_h = max(1, image.GetHeight())
        # Never upscale — LCSC's smallest thumbnail is already larger than a
        # grid row, and a blown-up one looks worse than a small centred one.
        scale = min(size / src_w, size / src_h, 1.0)
        return wx.Bitmap(
            image.Scale(
                max(1, int(src_w * scale)),
                max(1, int(src_h * scale)),
                wx.IMAGE_QUALITY_HIGH,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a bad JPEG must not kill the grid
        logger.debug("thumbnail decode failed: %r", exc)
        return None


def _parse_count(text: str) -> Optional[int]:
    """Read a formatted stock figure such as ``"14,248,812"`` back to an int."""
    try:
        return int(text.replace(",", "").strip())
    except (AttributeError, ValueError):
        return None


def _squeeze(
    widths: Dict[str, int], keys: List[str], floors: Dict[str, int], deficit: int
) -> int:
    """Take up to ``deficit`` pixels back from ``keys``, in proportion to room.

    Room is what a column has above its floor, so the widest column gives up
    the most and none of them is squeezed past the point of being readable.
    Returns whatever deficit is left for the next group to absorb.
    """
    room = {key: widths[key] - floors[key] for key in keys if widths[key] > floors[key]}
    total = sum(room.values())
    if total <= 0 or deficit <= 0:
        return deficit
    take = min(deficit, total)
    given = 0
    for key, available in room.items():
        share = min(available, int(round(take * available / total)))
        widths[key] -= share
        given += share
    return max(0, deficit - given)


def _set_column_hidden(column, hidden: bool, width: int) -> None:
    """Hide or show a DataView column.

    ``SetHidden`` is the documented call, but the macOS native DataView
    ignores it — ``IsHidden()`` keeps reporting ``False`` and the column stays
    put — so the width is always set as well. ``width`` has to be passed in
    because a column collapsed to zero no longer remembers what it was, and
    restoring it is what makes the view switch reversible.
    """
    with suppress(AttributeError, RuntimeError):
        column.SetHidden(hidden)
    column.SetWidth(0 if hidden else width)
