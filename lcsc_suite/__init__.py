"""EasyAssembly — the out-of-process PySide6 application.

This package runs in its **own** interpreter (3.12+, its own venv) and talks to
KiCad over the IPC API. None of the Python 3.9 / no-dependencies rules that
bind the legacy wx plugin apply here; see ``docs/QT_MIGRATION_PLAN.md``.

Nothing in here may import ``wx`` or ``pcbnew``. Board access goes through
:mod:`lcsc_suite.kicad_bridge` and nothing else.

The package is still called ``lcsc_suite`` while the product is called
EasyAssembly. That is an import path, not a name on screen, and renaming it
would churn every module and test in the repository to change nothing a user
can see. :data:`APP_NAME` below is the name a user sees.
"""

#: The product name. Defined here, at the toolkit-free root, rather than in
#: :mod:`lcsc_suite.ui.brand` where the rest of the identity lives, because
#: :mod:`lcsc_suite.kicad_bridge` needs it for the commit messages it writes
#: into KiCad's undo history — and the bridge imports no Qt at all, which is a
#: property worth more than keeping the brand in one file.
#: ``ui.brand`` re-exports it, so ``brand.APP_NAME`` remains the way UI code
#: asks for it.
APP_NAME = "EasyAssembly"

#: One line, for where a name alone is too terse: the PCM listing, an about box.
#: Still names LCSC and JLCPCB — the product name dropped them, but this is the
#: sentence people actually search for.
APP_TAGLINE = "LCSC/JLCPCB part assignment, library import and BOM/CPL output"

__all__ = ["APP_NAME", "APP_TAGLINE", "__version__"]

#: Kept in step with the repository ``VERSION`` file at release time; read
#: lazily rather than at import so a missing file cannot break start-up.
__version__ = "0.1.0-qt"
