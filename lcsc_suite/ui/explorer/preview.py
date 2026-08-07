"""Preview tiles: the symbol and footprint drawings, and the product photo.

The Qt port of ``lcsc/previewpanel.py`` (``SvgPreviewPanel`` and
``BitmapPreviewPanel``). Both wx classes hand-painted a captioned frame, scaled
their content into it and drew placeholder text through a ``wx.PaintDC``;
``QLabel`` inside a framed ``QWidget`` does all of that declaratively.

The SVG half needs no third-party renderer: ``QSvgRenderer`` is part of PySide6,
and the markup comes from the vendored ``easyeda2kicad`` renderer, which is pure
string-building over the EasyEDA CAD payload and touches no network of its own.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout

from .. import theme

log = logging.getLogger(__name__)

#: The tile's drawing area, before the caption. Three of these sit side by side
#: in the detail pane, so the width is what the layout can afford rather than
#: what a symbol would like.
TILE_SIZE = QSize(140, 132)


class PreviewTile(QFrame):
    """A captioned frame holding one drawing, photo or placeholder message."""

    def __init__(self, caption: str, parent=None, clickable: bool = False) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 3)
        layout.setSpacing(2)

        self._canvas = QLabel(self)
        self._canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._canvas.setMinimumSize(TILE_SIZE)
        self._canvas.setWordWrap(True)
        self._canvas.setScaledContents(False)
        layout.addWidget(self._canvas, 1)

        self._caption = QLabel(caption, self)
        self._caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._caption.setFont(theme.scaled(theme.base_font(), 0.85))
        self._caption.setProperty("role", "status")
        layout.addWidget(self._caption, 0)

        if clickable:
            # A preview tile does not otherwise look interactive, and the tile is
            # 140px of a 900px photo — so the click that enlarges it is the point
            # of having it. Advertised with the cursor as well as the caption.
            self._canvas.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear("—")

    def clear(self, message: str = "") -> None:
        """Drop the artwork and show ``message`` in its place."""
        self._canvas.setPixmap(QPixmap())
        self._canvas.setText(message)

    def set_pixmap(self, pixmap: Optional[QPixmap], fallback: str) -> None:
        """Show ``pixmap``, scaled to fit, or ``fallback`` when there is none."""
        if pixmap is None or pixmap.isNull():
            self.clear(fallback)
            return
        self._canvas.setText("")
        self._canvas.setPixmap(
            pixmap.scaled(
                self._canvas.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def set_image_bytes(self, data: Optional[bytes], fallback: str) -> None:
        """Decode and show image bytes, or ``fallback``."""
        if not data:
            self.clear(fallback)
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.clear(fallback)
            return
        self.set_pixmap(pixmap, fallback)

    def set_svg(self, markup: Optional[str], fallback: str) -> None:
        """Rasterise SVG ``markup`` into the tile, or show ``fallback``."""
        pixmap = render_svg(markup, self._canvas.size())
        self.set_pixmap(pixmap, fallback)


def render_svg(markup: Optional[str], size: QSize) -> Optional[QPixmap]:
    """Rasterise SVG markup to a pixmap that fits ``size``, or ``None``.

    Rendered onto a transparent ``QImage`` rather than straight to a pixmap:
    the symbol renderer emits a white background and the footprint renderer a
    black one, both of which are wrong in one of the two appearances. Drawing
    the vectors over transparency lets the tile's own card colour show through
    in either.
    """
    if not markup:
        return None
    renderer = QSvgRenderer(QByteArray(markup.encode("utf-8")))
    if not renderer.isValid():
        log.debug("preview SVG did not parse")
        return None

    box = renderer.defaultSize()
    if box.width() <= 0 or box.height() <= 0:
        box = size
    scale = min(size.width() / box.width(), size.height() / box.height())
    target = QSize(max(1, int(box.width() * scale)), max(1, int(box.height() * scale)))

    image = QImage(target, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    return QPixmap.fromImage(image)


def render_previews(cad: dict) -> tuple[Optional[str], Optional[str]]:
    """Return ``(symbol_svg, footprint_svg)`` for an EasyEDA CAD payload.

    Each renderer is tried separately: a part can have a symbol EasyEDA can draw
    and a footprint it cannot, and one failing must not cost the other. Imports
    are deferred because the vendored package only reaches ``sys.path`` once the
    legacy package has been imported, and because nothing needs it until a row
    is selected.
    """
    if not cad:
        return None, None
    symbol = footprint = None
    try:
        from easyeda2kicad.easyeda.easyeda_svg_renderer import (  # noqa: PLC0415
            render_footprint_svg,
            render_symbol_svg,
        )
    except ImportError:  # pragma: no cover - only when lib/ is missing
        log.debug("vendored easyeda2kicad SVG renderer not importable")
        return None, None

    try:
        symbol = render_symbol_svg(cad)
    except Exception:  # noqa: BLE001 - a part we cannot draw is not an error
        log.debug("symbol SVG render failed", exc_info=True)
    try:
        footprint = render_footprint_svg(cad)
    except Exception:  # noqa: BLE001
        log.debug("footprint SVG render failed", exc_info=True)
    return symbol, footprint


__all__ = ["TILE_SIZE", "PreviewTile", "render_previews", "render_svg"]
