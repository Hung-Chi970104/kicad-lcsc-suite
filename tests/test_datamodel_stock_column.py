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

import sys
import types


class _StubModule(types.ModuleType):
    """A wx-shaped module that answers to anything asked of it.

    ``datamodel`` pulls in ``dataview_highlight``, which reads sizer and
    alignment constants (``wx.ALIGN_LEFT`` and friends) at class-definition
    time. Enumerating them by hand means this file only imports when some
    *other* test module happens to have installed a broader stub first, which
    is not a dependency worth having. Constants come back as ints because they
    get combined with ``|``; everything else as a class, because it gets
    subclassed.
    """

    def __getattr__(self, name):
        value = 0 if name.isupper() else type(name, (), {})
        setattr(self, name, value)
        return value


def _stub_wx() -> None:
    """Install just enough of wx for `datamodel` to import.

    Only fills the gaps in whatever is already there: another test module may
    have installed its own ``wx``, and replacing it would strip the attributes
    that one configured.
    """
    wx = sys.modules.get("wx") or _StubModule("wx")
    dataview = sys.modules.get("wx.dataview") or _StubModule("wx.dataview")
    if not hasattr(dataview, "NullDataViewItem"):
        dataview.NullDataViewItem = object()
    wx.dataview = dataview
    sys.modules["wx"] = wx
    sys.modules["wx.dataview"] = dataview


_stub_wx()

from kicad_lcsc_suite.datamodel import stock_text  # noqa: E402


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
