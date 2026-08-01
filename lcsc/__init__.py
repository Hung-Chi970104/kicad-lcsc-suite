"""LCSC suite — parametric search, dual stock, and library import.

Additions layered on top of upstream kicad-jlcpcb-tools. Everything in this
package is Python 3.9 compatible (the oldest interpreter KiCad 7-10 bundle)
and depends only on what KiCad ships — wx, and optionally certifi — plus the
vendored, zero-dependency easyeda2kicad in ../lib.
"""

import os
import sys

# The package __init__ normally puts ../lib on sys.path, but this package is
# also imported directly by tests and by the standalone entry point. Make the
# vendored easyeda2kicad importable either way, and put it first so our pinned
# copy wins over any differently-versioned one another plugin may have added.
_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)
