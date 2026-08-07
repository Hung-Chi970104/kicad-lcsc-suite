"""The legacy wx plugin, and the logic modules both halves of the suite share.

Importing this package registers the pcbnew toolbar action, because that side
effect *is* KiCad's plugin contract for scripting plugins. But the package is
also imported for its toolkit-free logic — by the Qt app through
``lcsc_suite.shared``, and by the test suite — and those callers must not drag
in the entire wx dialog tree to reach ``store.py``.

Hence the guard below. ``ImportError`` alone is not enough to tell the two
apart: a test that stubs ``pcbnew`` with a ``MagicMock`` imports ``plugin``
quite happily, and then ``class JLCPCBTools(wx.Dialog)`` builds a class whose
every method is a mock — which is both a wasted import and a genuinely
confusing failure several modules downstream.
"""

import os
import sys

lib_path = os.path.join(os.path.dirname(__file__), "lib")
if lib_path not in sys.path:
    sys.path.append(lib_path)


def _inside_kicad() -> bool:
    """Report whether a real, file-backed ``pcbnew`` is importable.

    A stub has no ``__file__``; KiCad's compiled extension module does. That is
    the cheapest honest way to tell "running inside pcbnew" from "something
    stubbed the module out".
    """
    try:
        import pcbnew  # pylint: disable=import-error
    except ImportError:
        return False
    return getattr(pcbnew, "__file__", None) is not None


if __name__ != "__main__" and _inside_kicad():
    from .plugin import JLCPCBPlugin  # noqa: I001, E402

    JLCPCBPlugin().register()
