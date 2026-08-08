"""The toolkit-free logic layer, named in one place.

Until the Phase 8 cutover these modules lived in ``kicad_lcsc_suite/`` — the wx
plugin's package, which KiCad loaded directly and which this app borrowed from
through thirty lines of ``importlib``. They live here now, and this module is
what is left of that door: an aggregator that says which modules are *logic*
rather than UI.

That distinction is still worth stating. Everything reachable from here is
free of any toolkit, is covered by the test suite directly, and in most cases
predates the migration untouched:

    from lcsc_suite.shared import store, library
    from lcsc_suite.shared import lcsc_api

In particular ``lcsc/api.py`` is **copied, not edited** — three disagreeing
stock sources, the host breaker and the ``None`` vs ``0`` distinction are the
domain knowledge this fork exists for. If a UI need seems to require an API
change, change the UI.

Importing through here rather than reaching for the modules directly keeps one
list of what the logic layer *is*, which is the thing that stopped the two
halves drifting while they coexisted and is the thing a future extraction would
start from.
"""

from __future__ import annotations

from . import (
    dblib,
    derive_params,
    fab_rules,
    highlight_terms,
    library,
    schematicexport,
    schematicimport,
    store,
)
from .bom_estimation import (
    help_text as bom_help_text,
    pricing as bom_pricing,
    view as bom_view,
)
from .lcsc import api as lcsc_api, details as lcsc_details, importer as lcsc_importer

__all__ = [
    "bom_help_text",
    "bom_pricing",
    "bom_view",
    "dblib",
    "derive_params",
    "fab_rules",
    "highlight_terms",
    "lcsc_api",
    "lcsc_details",
    "lcsc_importer",
    "library",
    "schematicexport",
    "schematicimport",
    "store",
]
