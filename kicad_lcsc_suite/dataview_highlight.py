"""The wx DataView renderer for match highlighting.

The term expansion and span finding moved to :mod:`highlight_terms`, which is
free of wx so the Qt app can share it — see that module for why a conditional
import was not enough. Every name is re-exported here, so importers of this
module did not have to change.
"""

from __future__ import annotations

from collections.abc import Callable

from .highlight_terms import (  # noqa: F401 - re-exported for existing importers
    _HIGHLIGHT_FG,
    _HIGHLIGHT_FG_SELECTED,
    _MIN_HIGHLIGHT_TERM_LENGTH,
    HighlightQueryCache,
    decode_highlighted_value,
    encode_highlighted_value,
    expand_footprint,
    expand_value,
    filtered_highlight_terms,
    find_highlight_spans,
    normalize_highlight_terms,
    simplify_footprint_name,
)

try:
    import wx  # pylint: disable=import-error
    import wx.dataview as dv  # pylint: disable=import-error
except ImportError:  # pragma: no cover - test environments may not have wx
    wx = None  # type: ignore[assignment]
    dv = None  # type: ignore[assignment]

if wx is not None and dv is not None:  # pragma: no branch

    class HighlightedTextRenderer(dv.DataViewCustomRenderer):
        """Simple text renderer that highlights keyword matches."""

        def __init__(
            self,
            highlight_text_getter: Callable[[], str] | None = None,
            align: int = wx.ALIGN_LEFT,
            value_decoder: Callable[[str], tuple[str, list[str]]] | None = None,
        ):
            super().__init__("string", dv.DATAVIEW_CELL_INERT, align)
            self._highlight_text_getter = highlight_text_getter
            self._value_decoder = value_decoder
            self._value = ""
            self._query_cache = HighlightQueryCache()

        def SetValue(self, value: str) -> bool:
            """Store value to render for the current cell."""
            self._value = "" if value is None else str(value)
            return True

        def GetValue(self) -> str:
            """Return current cell value."""
            return self._value

        def _resolve_text_and_terms(self) -> tuple[str, list[str]]:
            """Resolve display text and normalized highlight terms."""
            if self._value_decoder is not None:
                return self._value_decoder(self._value)

            highlight_text = (
                self._highlight_text_getter() if self._highlight_text_getter else ""
            )
            self._query_cache.prepare(highlight_text)
            return self._value, self._query_cache.get_terms()

        def GetSize(self):
            """Return a best-effort size for the current text."""
            owner = self.GetOwner()
            font = (
                owner.GetOwner().GetFont()
                if owner is not None and owner.GetOwner() is not None
                else wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
            )
            display_text, _ = self._resolve_text_and_terms()
            dc = wx.ScreenDC()
            dc.SetFont(font)
            width, height = dc.GetTextExtent(display_text or "Hg")
            return wx.Size(width + 8, height + 6)

        def Render(self, rect, dc, state):
            """Draw the cell text and highlight search-term matches."""
            selected = bool(state & dv.DATAVIEW_CELL_SELECTED)
            foreground = wx.SystemSettings.GetColour(
                wx.SYS_COLOUR_HIGHLIGHTTEXT if selected else wx.SYS_COLOUR_LISTBOXTEXT
            )
            highlight = wx.Colour(
                *(_HIGHLIGHT_FG_SELECTED if selected else _HIGHLIGHT_FG)
            )

            dc.SetTextForeground(foreground)
            dc.SetBackgroundMode(wx.TRANSPARENT)

            text, terms = self._resolve_text_and_terms()
            if not text:
                return True

            if self._value_decoder is None and not terms:
                text_height = dc.GetTextExtent("Hg")[1]
                x = rect.x + 4
                y = rect.y + max(0, (rect.height - text_height) // 2)

                dc.SetClippingRegion(rect)
                try:
                    dc.DrawText(text, x, y)
                finally:
                    dc.DestroyClippingRegion()
                return True

            spans = (
                self._query_cache.get_spans(text)
                if self._value_decoder is None
                else find_highlight_spans(text, terms)
            )
            text_height = dc.GetTextExtent("Hg")[1]
            x = rect.x + 4
            y = rect.y + max(0, (rect.height - text_height) // 2)

            dc.SetClippingRegion(rect)
            try:
                cursor = 0
                for start, end in spans:
                    if start > cursor:
                        segment = text[cursor:start]
                        dc.SetTextForeground(foreground)
                        dc.DrawText(segment, x, y)
                        x += dc.GetTextExtent(segment)[0]

                    segment = text[start:end]
                    segment_width, _ = dc.GetTextExtent(segment)
                    dc.SetTextForeground(highlight)
                    dc.DrawText(segment, x, y)
                    x += segment_width
                    cursor = end

                if cursor < len(text):
                    dc.SetTextForeground(foreground)
                    dc.DrawText(text[cursor:], x, y)
            finally:
                dc.DestroyClippingRegion()
            return True
