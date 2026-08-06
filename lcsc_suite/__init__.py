"""LCSC Suite — the out-of-process PySide6 application.

This package runs in its **own** interpreter (3.12+, its own venv) and talks to
KiCad over the IPC API. None of the Python 3.9 / no-dependencies rules that
bind the legacy wx plugin apply here; see ``docs/QT_MIGRATION_PLAN.md``.

Nothing in here may import ``wx`` or ``pcbnew``. Board access goes through
:mod:`lcsc_suite.kicad_bridge` and nothing else.
"""

__all__ = ["__version__"]

#: Kept in step with the repository ``VERSION`` file at release time; read
#: lazily rather than at import so a missing file cannot break start-up.
__version__ = "0.1.0-qt"
