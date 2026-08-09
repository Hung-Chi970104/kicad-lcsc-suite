#!/usr/bin/env python3
"""Render the brand mark to the PNG files KiCad and the PCM read.

Run after changing anything in ``lcsc_suite/ui/brand.py``:

    .venv/bin/python scripts/make_brand_icons.py

Four toolbar icons plus the PCM tile. KiCad wants light and dark variants at
24 and 48px; it picks by the *editor's* appearance, which is not necessarily
this app's, so both variants are generated from one definition and neither is
hand-touched.

Both variants are currently the same artwork, and that is on purpose rather
than an oversight: the mark is a white glyph on a filled indigo tile, which
holds its contrast on KiCad's light toolbar and its dark one alike. The two
files exist because the manifest schema asks for them, and generating them
separately means the day the dark one *does* need to differ, nothing has to be
restructured to allow it.

The output is committed. Nothing at runtime regenerates it — KiCad reads these
files before any Python here is imported.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lcsc_suite.app import build_application  # noqa: E402
from lcsc_suite.ui import brand  # noqa: E402

#: ``(relative path, edge in px)``. The two 24s and two 48s are what
#: ``kicad_plugin/plugin.json`` names; ``PCM/icon.png`` is the 64px tile the
#: Plugin and Content Manager lists.
TARGETS = (
    ("kicad_plugin/icons/easyassembly-24.png", 24),
    ("kicad_plugin/icons/easyassembly-48.png", 48),
    ("kicad_plugin/icons/easyassembly-dark-24.png", 24),
    ("kicad_plugin/icons/easyassembly-dark-48.png", 48),
    ("PCM/icon.png", 64),
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report which files would change without writing any of them",
    )
    args = parser.parse_args()

    # Offscreen: QPixmap needs a QGuiApplication, and this must not open a
    # window on a machine that has one.
    build_application(offscreen=True)

    stale = []
    for relative, edge in TARGETS:
        path = os.path.join(ROOT, relative)
        pixmap = brand.tile(edge)
        if args.check:
            if not os.path.exists(path):
                stale.append(f"{relative} (missing)")
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not pixmap.save(path, "PNG"):
            print(f"failed to write {relative}", file=sys.stderr)
            return 1
        print(f"wrote {relative} ({edge}px)")

    if stale:
        print("\n".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
