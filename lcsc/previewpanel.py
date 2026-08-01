"""Symbol / footprint preview rendered from EasyEDA data.

The vendored easyeda2kicad renders the EasyEDA shape list to SVG locally, so
previews need no extra network round-trip beyond the CAD data we already
fetch, and they work for any part that has an EasyEDA drawing.

wxPython bundles a nanosvg-backed rasteriser (``wx.svg``), which KiCad ships,
so this stays dependency-free.
"""

from __future__ import annotations

import logging
from typing import Optional

import wx  # pylint: disable=import-error

logger = logging.getLogger(__name__)

# wx.svg arrived in wxPython 4.1. Every KiCad 7-10 build ships 4.1+, but a
# system-Python Linux build could be older, so degrade to a message rather
# than breaking the dialog.
try:
    import wx.svg as _wxsvg  # pylint: disable=import-error

    SVG_AVAILABLE = hasattr(_wxsvg, "SVGimage")
except ImportError:  # pragma: no cover - depends on the wxPython build
    _wxsvg = None
    SVG_AVAILABLE = False

NO_SVG_MESSAGE = "Previews need wxPython 4.1+ (wx.svg)"


class SvgPreviewPanel(wx.Panel):
    """Draws an SVG document scaled to fit, preserving aspect ratio."""

    def __init__(self, parent, min_size=(320, 240), caption: str = ""):
        super().__init__(parent, style=wx.BORDER_THEME)
        self.SetMinSize(wx.Size(*min_size))
        self._svg_bytes: Optional[bytes] = None
        self._bitmap: Optional[wx.Bitmap] = None
        self._placeholder = "No preview"
        self._caption = caption
        self._last_size = (0, 0)

        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, self._on_size)

    # -- public API ------------------------------------------------------

    def set_svg(self, svg: Optional[str], placeholder: str = "No preview") -> None:
        """Show ``svg`` (an SVG document string), or a placeholder if None."""
        self._placeholder = placeholder if SVG_AVAILABLE else NO_SVG_MESSAGE
        self._svg_bytes = svg.encode("utf-8") if svg else None
        self._bitmap = None
        self._last_size = (0, 0)
        self.Refresh()

    def clear(self, placeholder: str = "No preview") -> None:
        """Drop the current preview."""
        self.set_svg(None, placeholder)

    # -- painting --------------------------------------------------------

    def _on_size(self, event) -> None:
        """Invalidate the cached bitmap so it re-rasterises at the new size."""
        self._bitmap = None
        self.Refresh()
        event.Skip()

    def _background_colour(self) -> wx.Colour:
        """Panel background, following the system light/dark appearance."""
        return wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)

    def _rasterise(self, width: int, height: int) -> Optional[wx.Bitmap]:
        """Rasterise the SVG to fit ``width`` x ``height``."""
        if not SVG_AVAILABLE or not self._svg_bytes or width < 8 or height < 8:
            return None
        try:
            image = _wxsvg.SVGimage.CreateFromBytes(self._svg_bytes)
        except Exception as exc:  # noqa: BLE001 - bad SVG must not kill the UI
            logger.debug("SVG parse failed: %r", exc)
            return None

        src_w = max(1.0, float(image.width or 1))
        src_h = max(1.0, float(image.height or 1))
        scale = min(width / src_w, height / src_h)
        out_w = max(1, int(src_w * scale))
        out_h = max(1, int(src_h * scale))
        try:
            return image.ConvertToScaledBitmap(wx.Size(out_w, out_h))
        except Exception as exc:  # noqa: BLE001
            logger.debug("SVG rasterise failed: %r", exc)
            return None

    def _on_paint(self, _event) -> None:
        """Paint the preview, centred, with an optional caption."""
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self._background_colour()))
        dc.Clear()

        width, height = self.GetClientSize()
        caption_h = 0
        if self._caption:
            dc.SetTextForeground(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            font = self.GetFont()
            dc.SetFont(font)
            _, caption_h = dc.GetTextExtent(self._caption)
            dc.DrawText(self._caption, 4, 2)
            caption_h += 4

        avail_h = height - caption_h
        if self._svg_bytes:
            if self._bitmap is None or self._last_size != (width, avail_h):
                self._bitmap = self._rasterise(width - 8, avail_h - 8)
                self._last_size = (width, avail_h)
            if self._bitmap is not None:
                bw, bh = self._bitmap.GetSize()
                dc.DrawBitmap(
                    self._bitmap,
                    max(0, (width - bw) // 2),
                    caption_h + max(0, (avail_h - bh) // 2),
                    True,
                )
                return

        dc.SetTextForeground(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        tw, th = dc.GetTextExtent(self._placeholder)
        dc.DrawText(
            self._placeholder,
            max(0, (width - tw) // 2),
            caption_h + max(0, (avail_h - th) // 2),
        )
