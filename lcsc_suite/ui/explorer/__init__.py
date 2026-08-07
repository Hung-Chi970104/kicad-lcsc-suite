"""The LCSC Explorer, split into the pieces the plan's §3 asked for.

``lcsc/explorer.py`` was 2918 lines in one file. It comes across as:

===========================  =======================================
``window.py``                the dialog: search, filters, fills, actions
``results.py``               the grid's model and its four cell delegates
``facets.py``                the parametric filter panel
``detail.py``                the selected part's pane and its stock cards
``preview.py``               the symbol/footprint/photo tiles
``tasks.py``                 ``QThreadPool`` plumbing and the staleness tokens
===========================  =======================================

The photo viewer is a sibling rather than a member — ``ui/photo_viewer.py`` —
because it is a top-level window the main window could also open.
"""

from .window import ExplorerWindow

__all__ = ["ExplorerWindow"]
