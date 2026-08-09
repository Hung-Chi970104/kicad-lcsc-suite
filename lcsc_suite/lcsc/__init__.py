"""LCSC suite — parametric search, dual stock, and library import.

Runs in the app's own interpreter (3.12+, its own venv), like everything else
under ``lcsc_suite``. The EasyEDA converter is the installed ``easyeda2kicad``
distribution, pinned by the installers; it used to be vendored under ``../lib``
and is not any more, so that this repository ships no AGPL-3.0 code. Every
import of it is made lazily, inside the function that needs it, and every one
of those degrades to a reported error when the package is absent.
"""

import os
import sys

# ``core.version`` imports ``packaging``, which is still vendored in ../lib as a
# fallback for an environment that has no installed copy. Put it last-resort
# rather than first: an installed packaging should win, and nothing else in the
# directory is ours to shadow with.
_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.append(_LIB)
