"""The parametric filter panel — LCSC's filter sidebar, rebuilt from attributes.

The Qt port of ``lcsc/facetfilter.py`` (a ``ComboCtrl`` with a ``CheckListBox``
popup) and of the explorer's ``_rebuild_facets``/``_on_facet_changed`` pair.

The semantics are the ones every parts catalogue uses and the only ones that
make multi-select worth having: **OR within an attribute, AND across
attributes**. Ticking ±1% and ±0.5% widens the tolerance allowed; also picking a
package narrows the result. That rule lives in ``api.filter_hits`` and is not
reimplemented here — this module only collects the ticks.

Counts shown against each value are over the **fetched** result set as a whole
and stay fixed while the user ticks. Recomputing them against the current
selection — what LCSC's "results remaining" does — would mean rebuilding the
controls on every toggle, including the one the user has open.

One wx workaround did not need porting. ``FILTER_DEBOUNCE_MS`` exists in the
original partly because re-filtering ran on every tick while the checkbox popup
was still open, and on wxOSX a popup whose owner is busy stops highlighting rows
and can read the next click as a click outside and dismiss itself — the control
looked like it was ignoring the mouse. A ``QMenu`` with checkable actions has no
such coupling. The debounce is kept anyway, because the other half of its
reason is still true: rebuilding the grid is not cheap and a burst of ticks
should cost one rebuild.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from .. import theme

#: How long a tick waits before the grid is rebuilt around it. Long enough to
#: collapse "pick three tolerances" into one rebuild, short enough not to feel
#: like lag.
FILTER_DEBOUNCE_MS = 220

#: How many attribute filters go on one row of the panel.
FACET_COLUMNS = 2

#: The panel never shrinks below this, so an empty one still reads as a panel
#: rather than as a stray line of text.
FACET_MIN_HEIGHT = 30

#: …and never grows past this, which is a little over four rows of controls.
#: The panel competes with the result grid for the same vertical space and the
#: grid's rows are 140px tall, so every pixel here is most of a visible result.
#: Four rows covers eight attributes, which is more than most categories carry;
#: past that it scrolls, and the whole panel collapses from ``Filters ▴``.
FACET_MAX_HEIGHT = 132


class FacetFilter(QToolButton):
    """One attribute's values as a checkable menu, summarised on the button."""

    #: ``(attribute, selected values)`` — emitted on every tick.
    changed = Signal(str, set)

    def __init__(self, name: str, values: list[tuple[str, int]], parent=None) -> None:
        super().__init__(parent)
        self._name = name
        self._values = list(values)
        self._selected: set[str] = set()

        self.setObjectName(f"facet-{name}")
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        # Bounded rather than Expanding. Left to stretch, three attributes in a
        # two-column grid put the word "Any" at one end of a 600px control and
        # its arrow at the other, which reads as a broken layout rather than as
        # a filter. A dropdown only needs to be as wide as its summary.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(130)
        self.setMaximumWidth(240)

        menu = QMenu(self)
        # Kept open across ticks: the gesture this control is for is picking
        # several values, and a menu that closes on the first one turns that
        # into one trip per value.
        menu.setToolTipsVisible(True)
        for value, count in self._values:
            action = menu.addAction(f"{value}  ({count})")
            action.setCheckable(True)
            action.setData(value)
            action.toggled.connect(self._on_toggled)
        menu.addSeparator()
        clear = menu.addAction("Clear this attribute")
        clear.triggered.connect(self.clear)
        self._menu = menu
        self.setMenu(menu)
        self._restate()

    @property
    def name(self) -> str:
        """The attribute this control filters on."""
        return self._name

    def selected(self) -> set:
        """Return the ticked values."""
        return set(self._selected)

    def set_selected(self, values) -> None:
        """Tick exactly ``values``, without emitting per action."""
        wanted = set(values or ())
        blocked = [action for action in self._menu.actions() if action.isCheckable()]
        for action in blocked:
            action.blockSignals(True)
            action.setChecked(action.data() in wanted)
            action.blockSignals(False)
        self._selected = {
            action.data() for action in blocked if action.data() in wanted
        }
        self._restate()

    def clear(self) -> None:
        """Untick everything and report the change once."""
        if not self._selected:
            return
        self.set_selected(())
        self.changed.emit(self._name, set())

    def _on_toggled(self, _checked: bool) -> None:
        """Recollect the ticks and report them."""
        self._selected = {
            action.data()
            for action in self._menu.actions()
            if action.isCheckable() and action.isChecked()
        }
        self._restate()
        self.changed.emit(self._name, set(self._selected))

    def _restate(self) -> None:
        """Summarise the selection on the button face.

        Names the values while they fit, and falls back to a count when they do
        not: "±1%, ±5%" is worth reading and "±1%, ±5%, ±10%, ±20%, ±0.5%" is
        not — at that width it elides to something that looks like a single
        long value.
        """
        if not self._selected:
            self.setText("Any")
            self.setToolTip(f"{self._name}: no filter — showing every value")
            return
        ordered = [value for value, _count in self._values if value in self._selected]
        joined = ", ".join(ordered)
        self.setText(joined if len(ordered) <= 2 else f"{len(ordered)} selected")
        self.setToolTip(f"{self._name}: {joined}")


class FacetPanel(QWidget):
    """The whole filter panel: one labelled control per discovered attribute."""

    #: Emitted when any attribute's ticks change.
    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._controls: dict[str, FacetFilter] = {}
        self._selected: dict[str, set] = {}

        outer = QGridLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self._scroller = QScrollArea(self)
        self._scroller.setWidgetResizable(True)
        self._scroller.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroller.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # Sized to the controls it actually holds, between the two bounds above,
        # by ``_fit_height`` on every rebuild. A fixed maximum had to be tight
        # enough for the worst case and so was wrong for every other one: at 74px
        # a nine-attribute capacitor search showed two rows of five and hid the
        # rest behind a scrollbar, while a three-attribute one wasted the second
        # row on nothing. Asking the grid how tall it wants to be costs neither.
        self._scroller.setMinimumHeight(FACET_MIN_HEIGHT)
        self._scroller.setMaximumHeight(FACET_MAX_HEIGHT)

        self._body = QWidget(self._scroller)
        self._grid = QGridLayout(self._body)
        self._grid.setContentsMargins(2, 2, 2, 2)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(7)
        self._scroller.setWidget(self._body)
        outer.addWidget(self._scroller, 0, 0)

        self._hint = QLabel("Run a search to populate filters.", self)
        self._hint.setProperty("role", "status")
        self._hint.setWordWrap(True)
        outer.addWidget(self._hint, 1, 0)

        self._clear_button = QPushButton("Clear filters", self)
        self._clear_button.clicked.connect(self.clear)
        self._clear_button.setEnabled(False)
        outer.addWidget(self._clear_button, 1, 1, alignment=Qt.AlignmentFlag.AlignRight)
        outer.setColumnStretch(0, 1)

    # -- contents ------------------------------------------------------------

    def set_facets(self, facets: dict) -> None:
        """Rebuild the controls for a new result set."""
        for control in self._controls.values():
            control.setParent(None)
            control.deleteLater()
        self._controls = {}
        self._selected = {}
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        if not facets:
            self._hint.setText(
                "No discriminating attributes in these results — the JLC parts "
                "library returned no parametric data for them."
            )
            self._clear_button.setEnabled(False)
            self._fit_height()
            return

        self._hint.setText(
            f"{len(facets)} attributes available; tick any number of values per "
            "attribute. Counts are over the fetched result set."
        )
        for position, name in enumerate(sorted(facets)):
            row, column = divmod(position, FACET_COLUMNS)
            label = QLabel(f"{name}:", self._body)
            label.setToolTip(name)
            control = FacetFilter(name, facets[name], self._body)
            control.changed.connect(self._on_changed)
            self._controls[name] = control
            self._grid.addWidget(label, row, column * 3)
            self._grid.addWidget(control, row, column * 3 + 1)
        # The trailing spacer column is what absorbs surplus width, so the label
        # and its control stay next to each other instead of drifting apart.
        for column in range(FACET_COLUMNS):
            self._grid.setColumnStretch(column * 3 + 2, 1)
        self._clear_button.setEnabled(False)
        self._fit_height()

    def _fit_height(self) -> None:
        """Give the panel the height its controls ask for, within the bounds.

        Counted from the rows rather than read off the layout. ``QGridLayout``
        answers ``sizeHint`` with its contents margins and nothing else until the
        event loop has processed the widgets just added to it — ``invalidate``
        and ``activate`` both leave it at 4px — so a panel sized from the hint
        collapsed to its minimum every time and scrolled everything but the first
        row. Multiplying out is not an approximation here: every row holds the
        same two kinds of widget, and the row height is asked of one of them.
        """
        rows = -(-len(self._controls) // FACET_COLUMNS)
        if not rows:
            self._scroller.setFixedHeight(FACET_MIN_HEIGHT)
            return
        sample = next(iter(self._controls.values()))
        margins = self._grid.contentsMargins()
        wanted = (
            rows * sample.sizeHint().height()
            + (rows - 1) * self._grid.verticalSpacing()
            + margins.top()
            + margins.bottom()
        )
        self._scroller.setFixedHeight(
            max(FACET_MIN_HEIGHT, min(FACET_MAX_HEIGHT, wanted))
        )

    def selected(self) -> dict:
        """Return ``{attribute: {values}}`` for every active constraint."""
        return {name: set(values) for name, values in self._selected.items() if values}

    def clear(self) -> None:
        """Untick every attribute, then report the change once."""
        if not self._selected:
            return
        self._selected = {}
        for control in self._controls.values():
            control.set_selected(())
        self._clear_button.setEnabled(False)
        self.changed.emit()

    def _on_changed(self, name: str, values: set) -> None:
        """Record one attribute's ticks."""
        if values:
            self._selected[name] = set(values)
        else:
            self._selected.pop(name, None)
        self._clear_button.setEnabled(bool(self._selected))
        self.changed.emit()

    def controls(self) -> dict:
        """Return the per-attribute controls. For tests and the probe."""
        return dict(self._controls)


class Debounce(QObject):
    """Collapse a burst of calls into one, after a pause.

    A named object rather than a bare ``QTimer`` because the *reason* matters
    and belongs somewhere readable: rebuilding the grid empties it, re-appends
    up to a hundred rows through custom delegates and relaunches two background
    fills. Doing that per tick while the user picks three tolerance values is
    three full rebuilds for one gesture.
    """

    def __init__(self, milliseconds: int, slot, parent=None) -> None:
        super().__init__(parent)
        from PySide6.QtCore import QTimer  # noqa: PLC0415 - keeps the import local

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(milliseconds)
        self._timer.timeout.connect(slot)

    def schedule(self) -> None:
        """Restart the wait. The last call in a burst is the one that lands."""
        self._timer.start()

    def cancel(self) -> None:
        """Drop a pending call — used when something ran the work directly."""
        self._timer.stop()

    def flush(self) -> None:
        """Run a pending call now, if one is waiting."""
        if self._timer.isActive():
            self._timer.stop()
            self._timer.timeout.emit()


def facet_summary(selected: dict) -> str:
    """Describe the active constraints in one line, for the status area."""
    if not selected:
        return ""
    parts = [f"{name} ({len(values)})" for name, values in sorted(selected.items())]
    return "Filtered by " + ", ".join(parts)


def muted(label: QLabel) -> QLabel:
    """Paint ``label`` in the quiet chrome colour and return it."""
    palette = label.palette()
    palette.setColor(label.foregroundRole(), theme.colour("muted"))
    label.setPalette(palette)
    return label


__all__ = [
    "FACET_COLUMNS",
    "FACET_MAX_HEIGHT",
    "FACET_MIN_HEIGHT",
    "FILTER_DEBOUNCE_MS",
    "Debounce",
    "FacetFilter",
    "FacetPanel",
    "facet_summary",
    "muted",
]
