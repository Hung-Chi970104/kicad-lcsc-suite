"""Full-size product photo viewer for the LCSC Explorer.

The grid and the detail pane both show a photo small enough to answer one
question — "is this the part shape I expected?" — and too small for the next
one, which is usually about a marking, a pin-1 dot or which way the polarity
band faces. This is where that second look happens: the 900px original,
scaled to the window, with the part's other angles behind arrow keys.

Photos load off the UI thread and the window is usable before they arrive, so
opening it never blocks on the network. It is deliberately modeless — the
common gesture is to leave it open and click down the grid, comparing.
"""

from __future__ import annotations

import logging
import threading
from typing import List, Optional

import wx  # pylint: disable=import-error

from ..helpers import HighResWxSize
from . import api, theme
from .previewpanel import BitmapPreviewPanel

logger = logging.getLogger(__name__)

#: Initial window size, in unscaled pixels. Tall enough that a 900px square
#: photo has somewhere to go, small enough not to cover the explorer it was
#: opened from.
VIEWER_SIZE = (620, 700)


class PhotoViewerDialog(wx.Dialog):
    """A resizable window showing one part's product photos at full size.

    Constructed with whatever URL the caller already had — the grid always has
    one, and using it means the first photo can start downloading immediately.
    The rest of the set is discovered in the background, because finding out
    that a part has four photos costs a request that is wasted on the many
    parts that have one.
    """

    def __init__(self, parent, lcsc: str, subtitle: str = "", first_url: str = ""):
        super().__init__(
            parent,
            title=f"{lcsc} — product photo",
            size=HighResWxSize(parent, wx.Size(*VIEWER_SIZE)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.lcsc = lcsc
        self._urls: List[str] = [first_url] if first_url else []
        self._index = 0
        #: Bumped by :meth:`show_part`, so a fetch for the previous part
        #: cannot paint over the one now on screen.
        self._token = 0
        self._destroyed = False

        self._build_ui(subtitle)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.CentreOnParent()
        self._load(reset=True)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self, subtitle: str) -> None:
        """Lay out the caption, the photo and the navigation strip."""
        outer = wx.BoxSizer(wx.VERTICAL)

        self.heading = wx.StaticText(self, label=self.lcsc)
        self.heading.SetFont(theme.bold(self.heading.GetFont()))
        outer.Add(self.heading, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

        self.subtitle = wx.StaticText(self, label=subtitle)
        self.subtitle.SetForegroundColour(theme.colour("muted"))
        outer.Add(self.subtitle, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.photo = BitmapPreviewPanel(self, min_size=(360, 360))
        self.photo.clear("Loading photo…")
        outer.Add(self.photo, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        nav = wx.BoxSizer(wx.HORIZONTAL)
        self.prev_button = wx.Button(self, label="‹ Previous")
        self.next_button = wx.Button(self, label="Next ›")
        self.counter = wx.StaticText(self, label="")
        self.counter.SetForegroundColour(theme.colour("muted"))
        close_button = wx.Button(self, wx.ID_CLOSE, "Close")

        # ALIGN_CENTER_VERTICAL is legal here and only here: this sizer is
        # horizontal. On the vertical one above it, KiCad's wx raises.
        nav.Add(self.prev_button, 0, wx.ALIGN_CENTER_VERTICAL)
        nav.Add(self.next_button, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 6)
        nav.Add(self.counter, 1, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 12)
        nav.Add(close_button, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(nav, 0, wx.EXPAND | wx.ALL, 10)

        self.SetSizer(outer)
        self.Layout()

        self.prev_button.Bind(wx.EVT_BUTTON, lambda _e: self._step(-1))
        self.next_button.Bind(wx.EVT_BUTTON, lambda _e: self._step(1))
        close_button.Bind(wx.EVT_BUTTON, lambda _e: self.Close())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_part(self, lcsc: str, subtitle: str = "", first_url: str = "") -> None:
        """Retarget an already-open viewer at a different part."""
        if not self._alive():
            return
        self.lcsc = lcsc
        self._urls = [first_url] if first_url else []
        self._index = 0
        self.SetTitle(f"{lcsc} — product photo")
        self.heading.SetLabel(lcsc)
        self.subtitle.SetLabel(subtitle)
        self.photo.clear("Loading photo…")
        self._load(reset=True)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _step(self, delta: int) -> None:
        """Move ``delta`` photos through the set, wrapping at both ends."""
        if len(self._urls) < 2:
            return
        self._index = (self._index + delta) % len(self._urls)
        self._load(reset=False)

    def _on_key(self, event) -> None:
        """Arrow keys page through the photos; Escape closes the window."""
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.Close()
        elif key in (wx.WXK_LEFT, wx.WXK_UP):
            self._step(-1)
        elif key in (wx.WXK_RIGHT, wx.WXK_DOWN):
            self._step(1)
        else:
            event.Skip()

    def _refresh_nav(self) -> None:
        """Restate the counter and enable navigation once there is a set."""
        count = len(self._urls)
        self.counter.SetLabel(
            f"Photo {self._index + 1} of {count}" if count > 1 else ""
        )
        for button in (self.prev_button, self.next_button):
            button.Enable(count > 1)

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def _load(self, reset: bool) -> None:
        """Fetch the current photo, and on ``reset`` the rest of the set too."""
        self._token += 1
        token = self._token
        lcsc = self.lcsc
        url = self._urls[self._index] if self._urls else ""

        def work() -> None:
            data = None
            try:
                if url:
                    data = api.fetch_image(url)
                    if not self._post(self._photo_ready, token, data):
                        return
                if not reset:
                    return
                # Discover the other angles. Done after the first photo is on
                # screen so the window fills in immediately rather than
                # waiting on a request the user may never need.
                urls = api.assembly_photo_urls(lcsc)
                if url and url in urls:
                    # Keep the caller's URL at the front: it is the shot they
                    # clicked, and it is the one already in the image cache.
                    urls.remove(url)
                    urls.insert(0, url)
                elif url:
                    urls.insert(0, url)
                self._post(self._set_urls, token, urls, data is None)
            except Exception:  # noqa: BLE001 - a photo is never worth a crash
                logger.debug("photo load for %s failed", lcsc, exc_info=True)
                self._post(self._photo_ready, token, None)

        threading.Thread(target=work, daemon=True, name="LcscPhotoViewer").start()

    def _set_urls(self, token: int, urls: List[str], fetch_first: bool) -> None:
        """Adopt the discovered photo set. Runs on the UI thread."""
        if not self._alive() or token != self._token:
            return
        self._urls = urls
        self._index = 0
        self._refresh_nav()
        if not urls:
            self.photo.clear("No photo available for this part")
        elif fetch_first:
            # The caller had no URL to start from, so nothing is loaded yet.
            self._load(reset=False)

    def _photo_ready(self, token: int, data: Optional[bytes]) -> None:
        """Show a fetched photo. Runs on the UI thread."""
        if not self._alive() or token != self._token:
            return
        self.photo.set_image_bytes(data, "Photo unavailable")
        self._refresh_nav()

    # ------------------------------------------------------------------
    # Lifetime
    # ------------------------------------------------------------------

    def _post(self, handler, *args) -> bool:
        """Deliver a worker's result to the UI thread, if the window survives.

        Same guard as the explorer's: photo fetches routinely outlive this
        window, and ``wx.CallAfter`` raises on the worker thread once the
        dialog is gone. Returns whether the callback was posted.
        """
        try:
            if not self._alive():
                return False
            wx.CallAfter(handler, *args)
            return True
        except (RuntimeError, AssertionError):  # pragma: no cover - teardown race
            return False

    def _alive(self) -> bool:
        """Report whether the C++ window is still there to be painted."""
        try:
            return not self._destroyed and bool(self)
        except RuntimeError:  # pragma: no cover - depends on teardown timing
            return False

    def _on_close(self, _event) -> None:
        """Mark the window dead before it goes, so in-flight fetches drop."""
        self._destroyed = True
        self._token += 1
        self.Destroy()
