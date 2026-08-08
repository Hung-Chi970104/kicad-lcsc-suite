"""Import the toolkit-free logic modules the wx plugin and this app both use.

Those modules live in ``kicad_lcsc_suite/`` — the legacy plugin's package, which
KiCad loads directly and which the Qt app borrows from until the Phase 8 cutover
promotes the survivors into this package.

Import *through this module*, never by poking at ``sys.path``:

    from lcsc_suite.shared import store, library
    from lcsc_suite.shared import lcsc_api

Everything reachable from here is documented in the migration plan's §3 as
"ported nearly unchanged". In particular ``lcsc/api.py`` is **copied, not
edited** — if a UI need seems to require an API change, change the UI.

This used to take thirty lines of ``importlib`` machinery, because the plugin
*was* the repository root and a directory named ``kicad-lcsc-suite`` cannot be
imported. Giving it a real package directory reduced the whole problem to the
``sys.path`` entry below.
"""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType

#: Repository root — the directory both packages live in.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The legacy plugin's package name, and its directory name under the root.
LEGACY_PACKAGE = "kicad_lcsc_suite"

#: The legacy package's directory. Its *data* lives here too — the icon set and
#: the wx plugin's settings file — so anything reaching for those must join onto
#: this, never onto :data:`REPO_ROOT`. Phase 8 moves the survivors into this
#: package and this constant goes away with the rest of the legacy half.
LEGACY_ROOT = os.path.join(REPO_ROOT, LEGACY_PACKAGE)

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def legacy(name: str) -> ModuleType:
    """Import ``name`` from the legacy package."""
    return importlib.import_module(f"{LEGACY_PACKAGE}.{name}")


# --- Pure logic, reused verbatim -------------------------------------------
#
# Grouped by the plan's §3 table so it stays obvious what is shared and what
# was rewritten. Anything imported here must be free of wx *and* of pcbnew.
#
# Importing the package runs its ``__init__``, which appends the vendored
# ``lib/`` to sys.path and swallows the wx/pcbnew import it then attempts. That
# is what makes this safe outside KiCad.

# The toolkit-free half of dataview_highlight.py: which spellings count as the
# same part (390R is 390Ω, 10uF is 10µF). NOT dataview_highlight itself — its
# `try: import wx` succeeds against a partial stub and the renderer below it
# then fails, which is exactly what a test suite produces.
highlight_terms = legacy("highlight_terms")
derive_params = legacy("derive_params")
# The BOM/CPL rules, split out of fabrication.py so this half can reach them.
# The rest of that module is the Gerber plot path, which is out of scope, and
# it imports pcbnew at the top — so importing fabrication here is not merely
# undesirable, it is impossible.
fab_rules = legacy("fab_rules")
dblib = legacy("dblib")
library = legacy("library")
schematicexport = legacy("schematicexport")
schematicimport = legacy("schematicimport")
store = legacy("store")

lcsc_api = legacy("lcsc.api")
lcsc_details = legacy("lcsc.details")
lcsc_importer = legacy("lcsc.importer")

bom_pricing = legacy("bom_estimation.pricing")
bom_view = legacy("bom_estimation.view")
bom_help_text = legacy("bom_estimation.help_text")
enrichment_providers = legacy("enrichment.providers")

__all__ = [
    "LEGACY_PACKAGE",
    "LEGACY_ROOT",
    "REPO_ROOT",
    "bom_help_text",
    "bom_pricing",
    "bom_view",
    "dblib",
    "derive_params",
    "enrichment_providers",
    "fab_rules",
    "highlight_terms",
    "lcsc_api",
    "lcsc_details",
    "lcsc_importer",
    "legacy",
    "library",
    "schematicexport",
    "schematicimport",
    "store",
]
