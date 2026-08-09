"""The product identity: the name, and the mark, in one place.

Everything user-facing that says what this application *is* resolves to
:data:`APP_NAME` — the window title, the identity bar, the Qt application
display name, ``scripts/make_brand_icons.py``. The name itself is defined at
the package root and re-exported here; :mod:`lcsc_suite.kicad_bridge` needs it
too and must not import Qt. The KiCad-side strings
(``kicad_plugin/plugin.json``, ``PCM/metadata.template.json``) are JSON that
KiCad reads before any of this is imported, so they repeat the name by hand and
``tests/test_brand.py`` holds them to it.

**The name is not the storage key.** ``config.APPLICATION_NAME`` is still
``"LCSC Suite"`` and must stay that way: it is one half of the
``QStandardPaths`` key under which every user's settings and their optional
750 MB parts database already live. Renaming it does not migrate that data, it
abandons it. See ``config.adopt_data_directory`` for what recovering from that
costs — it has been paid twice already.

The mark is drawn rather than loaded. A vector definition in twenty lines
survives being asked for a 16px favicon, a 48px PCM tile and a HiDPI identity
bar without four files drifting out of step, and it takes the appearance's ink
colour for free — which a PNG of a dark glyph cannot do on a dark toolbar.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap

from .. import APP_NAME, APP_TAGLINE
from . import theme

# Re-exported so UI code can ask ``brand`` for the whole identity — name, blurb
# and mark — without needing to know that the two strings live at the package
# root for the benefit of the toolkit-free modules. See lcsc_suite/__init__.py.
__all__ = ["APP_NAME", "APP_TAGLINE", "mark", "tile"]

#: Geometry of the mark, in a 0..1 unit box: an integrated circuit seen from
#: above, with a pin-1 dot. Stated as fractions so one definition serves 16px
#: and 512px, and named so the numbers below are not four magic constants.
_BODY = QRectF(0.26, 0.26, 0.48, 0.48)
_BODY_RADIUS = 0.09
#: Where the three pins per side sit, and how far they reach past the body.
_PIN_OFFSETS = (0.355, 0.50, 0.645)
_PIN_REACH = 0.115
_PIN_THICKNESS = 0.075
#: The pin-1 dot, knocked *out* of the body rather than drawn on top, so the
#: mark reads correctly on any background including the indigo tile.
_DOT_CENTRE = QPointF(0.385, 0.385)
_DOT_RADIUS = 0.055


def _glyph_path(size: float) -> QPainterPath:
    """Return the chip outline, scaled to a ``size``x``size`` box.

    One path with the dot subtracted, rather than a fill followed by a clear:
    ``CompositionMode_Clear`` punches a genuine hole through whatever is beneath
    it, which on the indigo tile would expose the window instead of the tile.
    """
    body = QPainterPath()
    body.addRoundedRect(
        QRectF(
            _BODY.x() * size,
            _BODY.y() * size,
            _BODY.width() * size,
            _BODY.height() * size,
        ),
        _BODY_RADIUS * size,
        _BODY_RADIUS * size,
    )
    for centre in _PIN_OFFSETS:
        top = (centre - _PIN_THICKNESS / 2) * size
        height = _PIN_THICKNESS * size
        left = QPainterPath()
        left.addRect(
            QRectF((_BODY.x() - _PIN_REACH) * size, top, _PIN_REACH * size, height)
        )
        body = body.united(left)
        right = QPainterPath()
        right.addRect(QRectF(_BODY.right() * size, top, _PIN_REACH * size, height))
        body = body.united(right)

    dot = QPainterPath()
    dot.addEllipse(
        QPointF(_DOT_CENTRE.x() * size, _DOT_CENTRE.y() * size),
        _DOT_RADIUS * size,
        _DOT_RADIUS * size,
    )
    return body.subtracted(dot)


def _canvas(size: int, ratio: int) -> QPixmap:
    """Return a transparent pixmap of ``size`` logical pixels at ``ratio``x.

    The pixmap is ``size * ratio`` *device* pixels, but note what that means for
    the caller: a ``QPainter`` on a pixmap with a device pixel ratio works in
    **logical** coordinates, so everything drawn onto this is drawn against
    ``size``, never ``size * ratio``. Scaling the path by the ratio as well
    draws it three times too large, which lands one corner of the glyph in the
    canvas and the rest outside it — and looks, at 18px, like a rendering
    artefact rather than a wrong number.
    """
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.setDevicePixelRatio(float(ratio))
    pixmap.fill(Qt.GlobalColor.transparent)
    return pixmap


def mark(size: int = 20, ink: Optional[QColor] = None, ratio: int = 3) -> QPixmap:
    """Return the bare glyph in ``ink``, defaulting to the brand indigo.

    For in-window use: the identity bar draws it against the base colour, where
    a tile would look like a button somebody forgot to make clickable.
    """
    pixmap = _canvas(size, ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(ink if ink is not None else theme.brand())
    # `size`, not `size * ratio` — see _canvas.
    painter.drawPath(_glyph_path(size))
    painter.end()
    return pixmap


def tile(size: int = 48, ratio: int = 1, ink: Optional[QColor] = None) -> QPixmap:
    """Return the mark knocked out of a filled indigo tile.

    For use *outside* this application, where there is no palette to agree with
    and the background is somebody else's: KiCad's toolbar, the PCM listing, a
    desktop icon. The glyph is white because the tile is dark, in both
    appearances — this one is allowed to be a fixed pair.
    """
    pixmap = _canvas(size, ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)

    # Logical again — see _canvas. Equal to `size` at the default ratio of 1,
    # which is why the tiles came out right while the identity bar did not.
    edge = float(size)
    painter.setBrush(ink if ink is not None else QColor(*theme.brand_rgb()))
    painter.drawRoundedRect(QRectF(0, 0, edge, edge), edge * 0.22, edge * 0.22)

    # Inset, so the glyph has air inside the tile rather than touching its
    # corners — at 24px the pins would otherwise merge with the rounding.
    inset = edge * 0.11
    painter.translate(inset, inset)
    painter.setBrush(QColor(255, 255, 255))
    painter.drawPath(_glyph_path(edge - inset * 2))
    painter.end()
    return pixmap
