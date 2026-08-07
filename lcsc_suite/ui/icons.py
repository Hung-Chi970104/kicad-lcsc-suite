"""Toolbar icons, recoloured for the current appearance.

The icon set is 24px solid-black line art with an alpha channel, drawn for a
light toolbar. On a dark one it has to be repainted or it is black on
near-black. The wx plugin does this with ``wx.Image.Replace``; here it is a
per-pixel recolour that keeps alpha, so antialiased edges stay soft.

Two things the wx version had to do by hand are gone:

* **Disabled icons.** ``settings.py`` drew a red X over the bitmap because wx
  would not dim a ``wx.Bitmap`` for a disabled tool. Qt renders the disabled
  state from the same ``QIcon``, so that code is deleted rather than ported.
* **HiDPI scaling.** Qt picks the right device-pixel-ratio pixmap from a
  ``QIcon`` on its own. The source art is 24px, so a 2x pixmap is generated
  here by smooth upscaling — the honest limitation of a raster icon set, and
  the reason ``icons/svg`` would be a better source if it were complete.
"""

from __future__ import annotations

from functools import lru_cache
import logging
import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap

from ..shared import LEGACY_ROOT
from . import theme

log = logging.getLogger(__name__)

#: The icon set the wx plugin already ships, reused as-is. It lives inside the
#: legacy package because that package is what gets installed into KiCad.
ICON_DIR = os.path.join(LEGACY_ROOT, "icons")

#: Art this app needs and the wx plugin never had, searched *first*. New icons go
#: here rather than into the legacy set, because Phase 8 deletes that package and
#: anything of ours left inside it would go with it.
APP_ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons")

#: Where :func:`icon` looks, in order.
ICON_DIRS = (APP_ICON_DIR, ICON_DIR)

if not os.path.isdir(ICON_DIR):
    # One missing icon is a typo and is tolerated below. The whole directory
    # missing means the path is wrong, and every toolbar button silently loses
    # its image — a change that reads as a deliberate restyling in a screenshot
    # rather than as a bug. Say so once, loudly.
    log.error("Icon directory %s does not exist; toolbars will have no icons", ICON_DIR)

#: Toolbar icon size in logical pixels. The source art's natural size.
ICON_SIZE = QSize(24, 24)

#: Larger, for the right-hand toolbar's text-under-icon buttons.
LARGE_ICON_SIZE = QSize(28, 28)


def _ink() -> QColor:
    """Colour to repaint the black line art in.

    The theme's own button-label colour, so icons and the labels under them
    match exactly — and white if that colour is itself dark, which would leave
    every icon invisible.
    """
    ink = theme.chrome("text")
    luminance = (0.299 * ink.red() + 0.587 * ink.green() + 0.114 * ink.blue()) / 255.0
    if luminance < 0.5:
        return QColor(255, 255, 255)
    return ink


def _recolour(image: QImage, ink: QColor) -> QImage:
    """Repaint every fully-black pixel in ``image`` as ``ink``, keeping alpha."""
    out = image.convertToFormat(QImage.Format.Format_ARGB32)
    red, green, blue = ink.red(), ink.green(), ink.blue()
    for y in range(out.height()):
        for x in range(out.width()):
            pixel = out.pixelColor(x, y)
            if pixel.alpha() == 0:
                continue
            if pixel.red() == 0 and pixel.green() == 0 and pixel.blue() == 0:
                out.setPixelColor(x, y, QColor(red, green, blue, pixel.alpha()))
    return out


def _find(filename: str) -> str:
    """Resolve one icon name against :data:`ICON_DIRS`, app directory first."""
    for directory in ICON_DIRS:
        path = os.path.join(directory, filename)
        if os.path.isfile(path):
            return path
    return os.path.join(ICON_DIR, filename)


@lru_cache(maxsize=256)
def _load(filename: str, dark: bool) -> QIcon:
    """Load and cache one icon for one appearance."""
    path = _find(filename)
    icon = QIcon()
    image = QImage(path)
    if image.isNull():
        # A missing icon is a typo in a toolbar definition, not a reason to
        # abort building the window. An empty QIcon leaves the label visible.
        return icon
    if dark:
        image = _recolour(image, _ink())
    base = QPixmap.fromImage(image)
    icon.addPixmap(base)
    icon.addPixmap(
        base.scaled(
            base.size() * 2,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    )
    return icon


def icon(filename: str) -> QIcon:
    """Return the named icon from ``icons/``, recoloured if the theme is dark."""
    return _load(filename, theme.is_dark())
