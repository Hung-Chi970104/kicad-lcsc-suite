"""Full-size product photos, in a window that can be pointed at another part.

The Qt port of ``lcsc/photoviewer.py``. Opened by clicking a thumbnail in the
Explorer's grid or the photo tile in its detail pane.

Two behaviours are load-bearing and both are carried over:

* **It retargets rather than stacking.** The gesture this exists for is clicking
  down a column of thumbnails comparing packages, and that should move one
  window, not bury the screen in them. :meth:`PhotoViewer.show_part` is what the
  Explorer calls whether or not a viewer is already open.
* **A late photo must not land on a closed window.** In wx that needed an
  ``_alive()`` check inside every callback, because a ``wx.CallAfter`` onto a
  destroyed C++ object raises inside the event loop. Qt severs a connection when
  either end is destroyed, so the fetch simply goes undelivered — but the
  *token* is still needed, because a photo for the part the viewer was showing
  a moment ago would otherwise overwrite the one it was retargeted to.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
)

from . import theme
from .explorer.tasks import Pool

log = logging.getLogger(__name__)

#: The window opens at this size and can be resized. Big enough that a marking
#: or a polarity band is legible, which is the entire reason to enlarge a photo.
DEFAULT_SIZE = (640, 680)


class PhotoViewer(QDialog):
    """One product photo at full size, retargetable while open."""

    def __init__(
        self, parent, source, lcsc: str = "", subtitle: str = "", url: str = ""
    ):
        super().__init__(parent)
        self._source = source
        self._token = 0
        self._lcsc = ""

        self.setWindowTitle("Product photo")
        self.resize(*DEFAULT_SIZE)
        # Non-modal: the point is to compare it against the grid behind it.
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self.subtitle = QLabel(" ", self)
        self.subtitle.setProperty("role", "status")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.subtitle)

        self._scroller = QScrollArea(self)
        self._scroller.setWidgetResizable(True)
        self._scroller.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas = QLabel("No photo for this part", self._scroller)
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setFont(theme.scaled(theme.base_font(), 1.0))
        self._scroller.setWidget(self.canvas)
        layout.addWidget(self._scroller, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self._pool = Pool("photo-viewer", 1, self._on_loaded)
        self._pixmap: Optional[QPixmap] = None
        if lcsc:
            self.show_part(lcsc, subtitle, url)

    # -- contents ------------------------------------------------------------

    def show_part(self, lcsc: str, subtitle: str, url: str) -> None:
        """Point the window at ``lcsc`` and fetch its photo.

        Bumping the token first is what makes retargeting safe: the previous
        part's download may still be in flight, and without this it would land
        afterwards and replace the photo the user just asked for.
        """
        self._token += 1
        self._lcsc = lcsc
        self.setWindowTitle(f"{lcsc} — product photo")
        self.subtitle.setText(subtitle or " ")
        self._pixmap = None
        self.canvas.setPixmap(QPixmap())
        self.canvas.setText("Loading photo …")
        if not url:
            self.canvas.setText("No photo for this part")
            return
        token = self._token
        self._pool.start(token, lcsc, lambda: self._source.image(url))

    def lcsc(self) -> str:
        """Return the part currently on show."""
        return self._lcsc

    def _on_loaded(self, token: int, key, data) -> None:
        """Show a downloaded photo, if it is still the one being asked for."""
        del key
        if token != self._token:
            return
        if not data:
            self.canvas.setText("Photo unavailable")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.canvas.setText("Photo unavailable")
            return
        self._pixmap = pixmap
        self._rescale()

    def _rescale(self) -> None:
        """Fit the photo to the window without ever enlarging it.

        Upscaling a 900px product shot to a maximised window turns a sharp
        picture into a blurry one, and the thing being looked for — a marking, a
        pin-one dot — is exactly what blurs first.
        """
        if self._pixmap is None or self._pixmap.isNull():
            return
        area = self._scroller.viewport().size()
        pixmap = self._pixmap
        if pixmap.width() > area.width() or pixmap.height() > area.height():
            pixmap = pixmap.scaled(
                area,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.canvas.setText("")
        self.canvas.setPixmap(pixmap)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Refit the photo when the window changes size."""
        super().resizeEvent(event)
        self._rescale()


__all__ = ["DEFAULT_SIZE", "PhotoViewer"]
