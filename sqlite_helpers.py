"""SQLite row factories and collations, free of any GUI toolkit.

Split out of :mod:`helpers` for the Qt migration. ``helpers`` imports ``wx`` at
module scope, so anything importing it drags the toolkit in — which is fine in
the legacy plugin and fatal in the out-of-process app, whose interpreter has no
wx at all. ``store.py`` and ``library.py`` need only these two functions from
it, and neither has anything to do with a GUI.

:mod:`helpers` re-exports both names so existing wx-plugin imports keep working.
"""

import re


def natural_sort_collation(a, b):
    """Natural sort collation for use in sqlite."""
    if a == b:
        return 0

    def convert(text):
        return int(text) if text.isdigit() else text.lower()

    def alphanum_key(key):
        return [convert(c) for c in re.split("([0-9]+)", key)]

    natorder = sorted([a, b], key=alphanum_key)
    return -1 if natorder.index(a) == 0 else 1


def dict_factory(cursor, row) -> dict:
    """Row factory that returns a dict."""
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d
