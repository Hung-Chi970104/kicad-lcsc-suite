"""Contains helper function used all over the plugin."""

import os
from pathlib import Path
import re

import wx  # pylint: disable=import-error
import wx.dataview  # pylint: disable=import-error

from .sqlite_helpers import dict_factory, natural_sort_collation

PLUGIN_PATH = Path(__file__).resolve().parent


def getWxWidgetsVersion():
    """Get wx widgets version."""
    v = re.search(r"wxWidgets\s([\d\.]+)", wx.version())
    v = int(v.group(1).replace(".", ""))
    return v


def getVersion():
    """READ Version from file."""
    if not os.path.isfile(os.path.join(PLUGIN_PATH, "VERSION")):
        return "unknown"
    with open(os.path.join(PLUGIN_PATH, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def GetOS():
    """Get String with OS type."""
    return wx.PlatformInformation.Get().GetOperatingSystemIdName()


def GetScaleFactor(window):
    """Workaround if wxWidgets Version does not support GetDPIScaleFactor, for Mac OS always return 1.0."""
    if "Apple Mac OS" in GetOS():
        return 1.0
    if hasattr(window, "GetDPIScaleFactor"):
        return window.GetDPIScaleFactor()
    return 1.0


def HighResWxSize(window, size):
    """Workaround if wxWidgets Version does not support FromDIP."""
    if hasattr(window, "FromDIP"):
        return window.FromDIP(size)
    return size


def isDarkAppearance():
    """Report whether the desktop is using a dark appearance.

    ``GetAppearance`` landed in wxWidgets 3.1; older builds fall back to
    measuring the window background, which is what that API reports on.
    """
    appearance = getattr(wx.SystemSettings, "GetAppearance", None)
    if appearance is not None:
        try:
            return bool(appearance().IsUsingDarkBackground())
        except Exception:  # noqa: BLE001 - not every build implements this
            pass
    return _luminance(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)) < 0.5


def _luminance(colour):
    """Return the perceived luminance of a wx.Colour in the 0..1 range."""
    return (
        0.299 * colour.Red() + 0.587 * colour.Green() + 0.114 * colour.Blue()
    ) / 255.0


def _iconInk():
    """Colour to repaint the black icon line art in on a dark toolbar.

    Prefer the theme's own button-label colour so icons and the labels under
    them match exactly. If that colour is itself dark — which would leave the
    icons invisible, the bug this guards against — fall back to white.
    """
    ink = wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNTEXT)
    if not ink.IsOk() or _luminance(ink) < 0.5:
        return wx.Colour(255, 255, 255)
    return ink


def loadBitmapScaled(filename, scale=1.0, static=False):
    """Load a scaled bitmap, handle differences between Kicad versions."""
    if filename:
        path = os.path.join(PLUGIN_PATH, "icons", filename)
        bmp = wx.Bitmap(path)
        w, h = bmp.GetSize()
        img = bmp.ConvertToImage()
        # The icon set is solid black line art with an alpha channel, drawn
        # for a light toolbar. On a dark one it has to be repainted, or it is
        # black on near-black. Replace() leaves alpha alone, so antialiased
        # edges keep their softness.
        if isDarkAppearance():
            ink = _iconInk()
            img.Replace(0, 0, 0, ink.Red(), ink.Green(), ink.Blue())
        # Scaling used to sit inside the appearance check, so on a wx build
        # without GetAppearance the scale argument was silently ignored.
        bmp = wx.Bitmap(img.Scale(max(1, int(w * scale)), max(1, int(h * scale))))
    else:
        bmp = wx.Bitmap()
    if getWxWidgetsVersion() > 315 and not static:
        return wx.BitmapBundle(bmp)
    return bmp


def loadIconScaled(filename, scale=1.0):
    """Load a scaled icon, handle differences between Kicad versions."""
    bmp = loadBitmapScaled(filename, scale=scale, static=False)
    if getWxWidgetsVersion() > 315:
        return bmp
    return wx.Icon(bmp)


# `natural_sort_collation` and `dict_factory` moved to sqlite_helpers so that
# store.py and library.py can be imported without wx, which the out-of-process
# Qt app requires. Re-exported here for existing wx-plugin importers.
_ = (dict_factory, natural_sort_collation)
