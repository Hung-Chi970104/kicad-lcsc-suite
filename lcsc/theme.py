"""Theme-aware colours for the LCSC UI.

KiCad follows the desktop light/dark appearance, so any colour the plugin
hard-codes has to work on both. A value picked to read well on white — the
dark red the part list used for Standard-mode rows, say — turns into
near-invisible mud on a dark background.

Everything here is resolved *at call time* rather than at import time: the
user can flip appearance while KiCad is running, and wx hands us the new
system colours immediately.

Two families of colour live here:

``status``
    How healthy a number is — in stock, running low, none at all, unknown.

``source``
    Which inventory a number came from. JLC assembly and LCSC retail are
    separate warehouses that routinely disagree, so they get distinct hues
    and keep them everywhere they appear: column headers, detail cards and
    the availability summary.
"""

from __future__ import annotations

from typing import Dict, Tuple

import wx  # pylint: disable=import-error

#: (light, dark) RGB pairs. The dark variants are deliberately desaturated
#: and lifted in luminance — saturated hues vibrate against dark greys.
_PALETTE: Dict[str, Tuple[Tuple[int, int, int], Tuple[int, int, int]]] = {
    # Status
    "ok": ((14, 116, 60), (94, 214, 138)),
    "low": ((140, 92, 0), (240, 190, 90)),
    "bad": ((176, 20, 40), (255, 130, 130)),
    "unknown": ((130, 130, 130), (140, 140, 148)),
    # Inventory identity
    "jlc": ((0, 105, 122), (90, 200, 220)),
    "retail": ((106, 58, 178), (198, 158, 255)),
    # Cost advisory — a part that pushes the board into Standard-mode pricing.
    # Deliberately *not* "bad": nothing is broken, it just costs more, and
    # reusing the error red made a pricing note indistinguishable from a
    # zero-stock failure. Deeper and redder than "low" so the two never read
    # as the same signal when they appear in one window.
    "standard": ((166, 90, 12), (240, 160, 96)),
    # Chrome
    "muted": ((110, 110, 116), (150, 150, 158)),
    "rule": ((208, 208, 214), (72, 72, 80)),
}

#: Below this many pieces a part is flagged as "low" rather than "in stock".
LOW_STOCK_THRESHOLD = 100


def is_dark() -> bool:
    """Report whether the desktop is currently using a dark appearance.

    ``GetAppearance`` landed in wxWidgets 3.1; older builds fall back to
    measuring the window background, which is what the appearance API
    reports on anyway.
    """
    appearance = getattr(wx.SystemSettings, "GetAppearance", None)
    if appearance is not None:
        try:
            return bool(appearance().IsUsingDarkBackground())
        except Exception:  # noqa: BLE001 - not every build implements this
            pass
    return luminance(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)) < 0.5


def luminance(colour: wx.Colour) -> float:
    """Return the perceived luminance of ``colour`` in the 0..1 range."""
    return (
        0.299 * colour.Red() + 0.587 * colour.Green() + 0.114 * colour.Blue()
    ) / 255.0


def colour(name: str) -> wx.Colour:
    """Return the palette entry ``name`` for the current appearance."""
    light, dark = _PALETTE.get(name, _PALETTE["muted"])
    return wx.Colour(*(dark if is_dark() else light))


def stock_state(count) -> str:
    """Classify a stock figure into a palette status name.

    ``None`` means "not fetched yet or the endpoint did not answer", which is
    materially different from a confirmed zero and must not be shown in the
    same colour as one.
    """
    if count is None:
        return "unknown"
    if count <= 0:
        return "bad"
    if count < LOW_STOCK_THRESHOLD:
        return "low"
    return "ok"


def stock_colour(count) -> wx.Colour:
    """Return the colour a stock figure should be drawn in."""
    return colour(stock_state(count))


def blend(first: wx.Colour, second: wx.Colour, ratio: float) -> wx.Colour:
    """Mix two colours, ``ratio`` being the weight of ``second``."""
    ratio = max(0.0, min(1.0, ratio))
    return wx.Colour(
        int(first.Red() + (second.Red() - first.Red()) * ratio),
        int(first.Green() + (second.Green() - first.Green()) * ratio),
        int(first.Blue() + (second.Blue() - first.Blue()) * ratio),
    )


def card_background() -> wx.Colour:
    """Background for a raised panel, nudged away from the window colour."""
    window = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
    target = wx.Colour(255, 255, 255) if is_dark() else wx.Colour(0, 0, 0)
    return blend(window, target, 0.06)


def bold(font: wx.Font) -> wx.Font:
    """Return ``font`` in bold, without mutating the original."""
    out = wx.Font(font)
    out.SetWeight(wx.FONTWEIGHT_BOLD)
    return out


def scaled(font: wx.Font, factor: float) -> wx.Font:
    """Return ``font`` resized by ``factor``."""
    out = wx.Font(font)
    out.SetPointSize(max(7, int(round(font.GetPointSize() * factor))))
    return out
