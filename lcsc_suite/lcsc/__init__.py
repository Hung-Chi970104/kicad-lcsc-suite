"""LCSC suite — parametric search, dual stock, and library import.

Runs in the app's own interpreter (3.12+, its own venv), like everything else
under ``lcsc_suite``. The EasyEDA converter is the installed ``easyeda2kicad``
distribution, pinned by the installers; it used to be vendored under ``../lib``
and is not any more, so that this repository ships no AGPL-3.0 code. Every
import of it is made lazily, inside the function that needs it, and every one
of those degrades to a reported error when the package is absent.

The vendored-``packaging`` fallback used to be registered here. It now lives in
``lcsc_suite/__init__.py``, which runs before this module and before every other
import path that reaches ``core.version`` — this one did not.
"""
