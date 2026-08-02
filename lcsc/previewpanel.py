"""Preview tiles for the LCSC Explorer's detail pane.

Two flavours share one painting scaffold:

:class:`SvgPreviewPanel`
    Symbol and footprint drawings. The vendored easyeda2kicad renders the
    EasyEDA shape list to SVG locally, so previews need no extra network
    round-trip beyond the CAD data we already fetch, and they work for any
    part that has an EasyEDA drawing. wxPython bundles a nanosvg-backed
    rasteriser (``wx.svg``), which KiCad ships, so this stays
    dependency-free.

:class:`BitmapPreviewPanel`
    The product photo from LCSC, which arrives as JPEG bytes.

Both scale to fit, preserve aspect ratio, keep a caption, and follow the
desktop light/dark appearance.
"""

from __future__ import annotations

import io
import logging
from typing import Optional

import wx  # pylint: disable=import-error

from . import theme

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


def _wrap(dc, text: str, max_width: int):
    """Word-wrap ``text`` to ``max_width`` using ``dc``'s current font."""
    lines = []
    current = ""
    for word in (text or "").split():
        candidate = f"{current} {word}".strip()
        if current and dc.GetTextExtent(candidate)[0] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


class _PreviewPanel(wx.Panel):
    """Caption + centred artwork on a card background, scaled to fit."""

    def __init__(self, parent, min_size=(160, 170), caption: str = ""):
        super().__init__(parent, style=wx.BORDER_NONE)
        self.SetMinSize(wx.Size(*min_size))
        self._source = None
        self._bitmap: Optional[wx.Bitmap] = None
        self._placeholder = "No preview"
        self._caption = caption
        self._last_size = (0, 0)

        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, self._on_size)

    # -- public API ------------------------------------------------------

    def clear(self, placeholder: str = "No preview") -> None:
        """Drop the current artwork and show ``placeholder`` instead."""
        self._set_source(None, placeholder)

    def set_caption(self, caption: str) -> None:
        """Change the small label drawn in the tile's top-left corner."""
        self._caption = caption
        self.Refresh()

    # -- subclass hooks --------------------------------------------------

    def _set_source(self, source, placeholder: str) -> None:
        """Store new artwork and invalidate the cached rasterisation."""
        self._placeholder = placeholder
        self._source = source
        self._bitmap = None
        self._last_size = (0, 0)
        self.Refresh()

    def _rasterise(self, width: int, height: int) -> Optional[wx.Bitmap]:
        """Render the current source at up to ``width`` x ``height``."""
        raise NotImplementedError

    # -- painting --------------------------------------------------------

    def _on_size(self, event) -> None:
        """Invalidate the cached bitmap so it re-rasterises at the new size."""
        self._bitmap = None
        self.Refresh()
        event.Skip()

    def _on_paint(self, _event) -> None:
        """Paint the tile: rounded card, caption, then artwork or placeholder."""
        dc = wx.AutoBufferedPaintDC(self)
        background = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        dc.SetBackground(wx.Brush(background))
        dc.Clear()

        width, height = self.GetClientSize()
        self._draw_card(dc, width, height)

        caption_h = 0
        if self._caption:
            dc.SetTextForeground(theme.colour("muted"))
            dc.SetFont(theme.scaled(self.GetFont(), 0.85))
            _, caption_h = dc.GetTextExtent(self._caption)
            dc.DrawText(self._caption, 7, 4)
            caption_h += 6
            dc.SetFont(self.GetFont())

        top = caption_h
        avail_h = height - caption_h - 4
        if self._source is not None and avail_h > 8:
            if self._bitmap is None or self._last_size != (width, avail_h):
                self._bitmap = self._rasterise(width - 14, avail_h - 8)
                self._last_size = (width, avail_h)
            if self._bitmap is not None:
                bw, bh = self._bitmap.GetSize()
                dc.DrawBitmap(
                    self._bitmap,
                    max(0, (width - bw) // 2),
                    top + max(0, (avail_h - bh) // 2),
                    True,
                )
                return

        dc.SetTextForeground(theme.colour("muted"))
        lines = _wrap(dc, self._placeholder, max(20, width - 16))
        line_h = dc.GetTextExtent("Hg")[1]
        y = top + max(0, (avail_h - line_h * len(lines)) // 2)
        for line in lines:
            tw, _ = dc.GetTextExtent(line)
            dc.DrawText(line, max(0, (width - tw) // 2), y)
            y += line_h

    def _draw_card(self, dc, width: int, height: int) -> None:
        """Fill the tile with a subtly raised rounded rectangle."""
        if width < 4 or height < 4:
            return
        dc.SetBrush(wx.Brush(theme.card_background()))
        dc.SetPen(wx.Pen(theme.colour("rule")))
        dc.DrawRoundedRectangle(0, 0, width - 1, height - 1, 6)


class SvgPreviewPanel(_PreviewPanel):
    """Draws an SVG document scaled to fit, preserving aspect ratio."""

    def set_svg(self, svg: Optional[str], placeholder: str = "No preview") -> None:
        """Show ``svg`` (an SVG document string), or a placeholder if None."""
        self._set_source(
            svg.encode("utf-8") if svg else None,
            placeholder if SVG_AVAILABLE else NO_SVG_MESSAGE,
        )

    def _rasterise(self, width: int, height: int) -> Optional[wx.Bitmap]:
        """Rasterise the SVG to fit ``width`` x ``height``."""
        if not SVG_AVAILABLE or not self._source or width < 8 or height < 8:
            return None
        try:
            image = _wxsvg.SVGimage.CreateFromBytes(self._source)
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


class BitmapPreviewPanel(_PreviewPanel):
    """Draws a photo (JPEG/PNG bytes) scaled to fit, preserving aspect ratio."""

    def set_image_bytes(
        self, data: Optional[bytes], placeholder: str = "No photo"
    ) -> None:
        """Show the decoded ``data``, or a placeholder if it is missing or bad."""
        image = None
        if data:
            try:
                image = wx.Image(io.BytesIO(data))
                if not image.IsOk():
                    image = None
            except Exception as exc:  # noqa: BLE001 - a bad JPEG is not fatal
                logger.debug("Product image decode failed: %r", exc)
                image = None
        self._set_source(image, placeholder)

    def _rasterise(self, width: int, height: int) -> Optional[wx.Bitmap]:
        """Scale the decoded image down to fit the tile."""
        if self._source is None or width < 8 or height < 8:
            return None
        src_w = max(1, self._source.GetWidth())
        src_h = max(1, self._source.GetHeight())
        # Never upscale: LCSC thumbnails look worse blown up than centred.
        scale = min(width / src_w, height / src_h, 1.0)
        out_w = max(1, int(src_w * scale))
        out_h = max(1, int(src_h * scale))
        try:
            return wx.Bitmap(self._source.Scale(out_w, out_h, wx.IMAGE_QUALITY_HIGH))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Product image scale failed: %r", exc)
            return None
