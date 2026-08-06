"""Import the toolkit-free logic modules that still live at the repo root.

The repository root *is* a Python package — but its directory name has
hyphens, so it cannot be imported by name, and its modules use relative
imports (``from .helpers import ...``) that only resolve inside a package. The
legacy wx plugin gets around this because ``install.sh`` symlinks the checkout
into KiCad's plugin directory under the importable name ``kicad_lcsc_suite``.

The Qt app cannot rely on that symlink existing (Phase 8 removes it), so it
registers the same alias itself, from the checkout, using importlib. This is
the identical trick ``scripts/gui_probe.py`` uses.

Import *through this module*, never by poking at ``sys.path``:

    from lcsc_suite.shared import store, library
    from lcsc_suite.shared import lcsc_api

Everything reachable from here is documented in the migration plan's §3 as
"ported nearly unchanged". In particular ``lcsc/api.py`` is **copied, not
edited** — if a UI need seems to require an API change, change the UI.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from types import ModuleType

#: Repository root — the directory the legacy package lives in.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The importable alias the root package's relative imports need.
LEGACY_PACKAGE = "kicad_lcsc_suite"


def _register_legacy_package() -> ModuleType:
    """Make ``kicad_lcsc_suite`` importable from this checkout."""
    existing = sys.modules.get(LEGACY_PACKAGE)
    if existing is not None:
        return existing

    # Already importable (the wx plugin's symlink is on sys.path)? Prefer that,
    # so a developer running both halves does not end up with two copies of the
    # same modules loaded under different identities.
    try:
        return importlib.import_module(LEGACY_PACKAGE)
    except ImportError:
        pass

    spec = importlib.util.spec_from_file_location(
        LEGACY_PACKAGE,
        os.path.join(REPO_ROOT, "__init__.py"),
        submodule_search_locations=[REPO_ROOT],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - checkout is broken
        raise ImportError(f"cannot locate the legacy package at {REPO_ROOT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[LEGACY_PACKAGE] = module
    # The root ``__init__`` appends ``lib/`` (the vendored easyeda2kicad) to
    # sys.path and swallows the wx/pcbnew import it then attempts, so this is
    # safe outside KiCad.
    spec.loader.exec_module(module)
    return module


_legacy = _register_legacy_package()


def legacy(name: str) -> ModuleType:
    """Import ``name`` from the legacy root package."""
    return importlib.import_module(f"{LEGACY_PACKAGE}.{name}")


# --- Pure logic, reused verbatim -------------------------------------------
#
# Grouped by the plan's §3 table so it stays obvious what is shared and what
# was rewritten. Anything imported here must be free of wx *and* of pcbnew.

derive_params = legacy("derive_params")
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
    "REPO_ROOT",
    "bom_help_text",
    "bom_pricing",
    "bom_view",
    "dblib",
    "derive_params",
    "enrichment_providers",
    "lcsc_api",
    "lcsc_details",
    "lcsc_importer",
    "legacy",
    "library",
    "schematicexport",
    "schematicimport",
    "store",
]
