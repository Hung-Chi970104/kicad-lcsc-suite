"""Tests for the Stock column's text conversion in `datamodel.py`.

The part list's Stock column is declared to wx as a ``"string"``, and a
``wxDataViewCtrl`` throws away any cell whose variant type does not match the
column's — an ``int`` handed to the model renders as an empty cell and logs a
line no user ever sees. That is not something a wx-free test can observe, so
what is pinned here is the contract that prevents it: whatever the model is
given for that column, it stores text.

`datamodel` cannot be imported without wx, so the module is loaded against a
stub big enough for its import-time class definitions. The stubs are filled in
attribute by attribute rather than replacing an existing ``wx`` module,
because another test file in this suite installs its own bare one.
"""

import importlib
from pathlib import Path
import sys
import types

_ROOT = Path(__file__).parent.parent


def _stub_wx() -> None:
    """Install just enough of wx for `datamodel` to import."""
    wx = sys.modules.get("wx") or types.ModuleType("wx")
    dataview = sys.modules.get("wx.dataview") or types.ModuleType("wx.dataview")
    for name in ("Colour", "Bitmap"):
        if not hasattr(wx, name):
            setattr(wx, name, type(name, (), {}))
    for name in (
        "PyDataViewModel",
        "DataViewCustomRenderer",
        "DataViewIconText",
    ):
        if not hasattr(dataview, name):
            setattr(dataview, name, type(name, (), {}))
    if not hasattr(dataview, "NullDataViewItem"):
        dataview.NullDataViewItem = object()
    wx.dataview = dataview
    sys.modules["wx"] = wx
    sys.modules["wx.dataview"] = dataview


_stub_wx()

_pkg_name = "kicadplugin"
if _pkg_name not in sys.modules:
    _pkg = types.ModuleType(_pkg_name)
    _pkg.__path__ = [str(_ROOT)]
    sys.modules[_pkg_name] = _pkg

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

stock_text = importlib.import_module(f"{_pkg_name}.datamodel").stock_text


def test_an_integer_stock_figure_becomes_text():
    """The explorer assigns parts with an int; the column needs a string."""
    assert stock_text(4657795) == "4657795"


def test_a_string_stock_figure_is_left_alone():
    """The API detail cache and the bulk database both hand over strings."""
    assert stock_text("4657795") == "4657795"


def test_a_confirmed_zero_is_shown():
    """Out of stock is a fact worth drawing, and it is not the same as blank."""
    assert stock_text(0) == "0"


def test_an_unknown_stock_figure_stays_blank():
    """Never render "None" at a user: not-looked-up shows as an empty cell."""
    assert stock_text(None) == ""
    assert stock_text("") == ""
