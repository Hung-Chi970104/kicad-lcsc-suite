"""Shared pytest setup: put the repository root on ``sys.path``.

That is the whole job. Everything under test is a real package reachable from
the root — ``lcsc_suite`` (the wx plugin and the logic both halves share),
``lcsc_suite`` (the Qt app) and ``db_build`` (the database-build Action) — so a
test imports them by name and nothing else is needed.

It used to take considerably more. The plugin *was* the repository root, whose
directory name has hyphens and therefore cannot be imported, so each test file
carried a preamble that fabricated a package called ``kicadplugin`` pointing at
the root and then reached modules through ``importlib.import_module``. Ten files
had a copy. Giving the plugin a real directory retired all of it.
"""

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
