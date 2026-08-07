"""The part list's model — the Qt port of ``datamodel.PartListDataModel``.

Nine columns, in the order the migration plan's §5.1 specifies. The rows come
from ``store.py`` (the per-project SQLite state), enriched with the Type / JLC
Stock / LCSC Params values that ``lcsc/details.py`` resolves.

Three things here carry real meaning and are not presentation choices:

**``?`` is not ``0``.** ``?`` means nobody answered — the endpoint 403'd, the
host breaker is open, the part has not been looked up yet. ``0`` means a source
confirmed there is no stock. Rendering one as the other shows in-stock parts as
dead, and shows dead parts as merely unknown. ``store.py`` keeps them apart
(``None`` versus ``0``) and so does this.

**Row colouring says two different things.** ``unassigned`` red is the one
actionable failure the list can show: the part is going into the BOM and JLC has
nothing to place. ``standard_trigger`` amber is advisory — that part pushes the
board into Standard-mode assembly pricing, which is not broken, it just costs
more. Those two shared a red once, which made a pricing note indistinguishable
from a failure.

**Parts excluded from the BOM are never marked unassigned.** Mounting holes,
fiducials and test points are fine without a number.

Unlike wx's ``DataViewCtrl``, sorting is a `QSortFilterProxyModel` over this
model rather than SQL in ``store.set_order_by``, so a click on a header cannot
disagree with what the database returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
from typing import Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

from .. import theme

log = logging.getLogger(__name__)

#: Column indices. Named rather than numbered at the use site — the wx model
#: addressed its columns by integer and every reordering was a hunt.
REF = 0
VALUE = 1
FOOTPRINT = 2
PARAMS = 3
LCSC = 4
TYPE = 5
STOCK = 6
BOM = 7
POS = 8

#: (heading, default width). §5.1's order, the wx plugin's widths.
COLUMNS: tuple[tuple[str, int], ...] = (
    ("Ref", 60),
    ("Value (Name)", 150),
    ("Footprint", 250),
    ("LCSC Params", 170),
    ("LCSC", 100),
    ("Type", 100),
    # Named for the warehouse it reports on. This is JLC *assembly* stock — what
    # JLC will place on a board — never LCSC retail, a separate warehouse whose
    # count routinely disagrees by orders of magnitude.
    ("JLC Stock", 110),
    ("BOM", 55),
    ("POS", 55),
)

#: What a cell shows when nobody has answered yet. Distinct from "0".
UNKNOWN = "?"

#: BOM/POS are shown as a tick or nothing, as the wx icon columns were.
INCLUDED = "✓"
EXCLUDED = ""

#: Custom role: the raw value a sort should use, so "JLC Stock" sorts
#: numerically and an unknown sorts below a confirmed zero rather than
#: alphabetically between "0" and "1".
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 1

#: Custom role: the row's reference, for finding a row from a selection without
#: caring which column was clicked.
REFERENCE_ROLE = int(Qt.ItemDataRole.UserRole) + 2

#: Custom role: the terms the LCSC Params cell should highlight. Exposed as a
#: role rather than recomputed in the delegate so the model stays the one place
#: that knows what a row contains.
MATCH_TERMS_ROLE = int(Qt.ItemDataRole.UserRole) + 3


@dataclass
class PartRow:
    """One row of the part list.

    Deliberately not the store's dict: the store's row is the persisted shape
    and this is the displayed shape, and the two drift (``stock`` is ``None`` or
    an int in the store; here it also has to survive being "not fetched yet").
    """

    reference: str
    value: str
    footprint: str
    lcsc: str = ""
    #: Basic / Preferred / Extended, from the *search* endpoint. The assembly
    #: endpoint spells it base/expand, and `bom_estimation.pricing` compares to
    #: "Extended" exactly — take the wrong one and every Extended part silently
    #: loses its per-reel feeder fee.
    part_type: str = ""
    #: ``None`` = nobody answered; an int = a source confirmed this figure.
    stock: Optional[int] = None
    params: str = ""
    exclude_from_bom: bool = False
    exclude_from_pos: bool = False
    dnp: bool = False
    #: Highlight terms for the LCSC Params cell, when match highlighting is on.
    match_terms: Sequence[str] = field(default_factory=tuple)

    @property
    def assigned(self) -> bool:
        """Report whether this part carries an LCSC number."""
        return bool(self.lcsc)

    @property
    def needs_a_number(self) -> bool:
        """Report whether this row is the one actionable failure the list shows.

        A part headed for the BOM with no LCSC number: JLC has nothing to place.
        Excluded parts are fine without one and are never marked.
        """
        return not self.exclude_from_bom and not self.assigned

    @classmethod
    def from_store(cls, part: dict, details: Optional[dict] = None) -> PartRow:
        """Build a row from a ``store.read_all()`` record plus part details."""
        details = details or {}
        stock = part.get("stock")
        if stock in ("", None):
            stock = details.get("stock", "")
        return cls(
            reference=part["reference"],
            value=part.get("value", ""),
            footprint=part.get("footprint", ""),
            lcsc=part.get("lcsc") or "",
            part_type=details.get("type", ""),
            stock=as_stock(stock),
            params=details.get("params", ""),
            exclude_from_bom=bool(part.get("exclude_from_bom")),
            exclude_from_pos=bool(part.get("exclude_from_pos")),
            dnp=bool(part.get("dnp")),
        )


def as_stock(value) -> Optional[int]:
    """Coerce a stock figure, keeping "nobody answered" distinct from zero.

    Public because it is the only spelling of this rule. ``store.create_part``
    defaults the column to the empty string rather than to SQL ``NULL``, so a
    figure read back out is ``''``, ``None``, or a number as text depending on
    how the row got there — and every caller that hands one to
    ``PartList.assign`` has to normalise it the same way or ``int('')`` raises.
    """
    if value in ("", None):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class PartTableModel(QAbstractTableModel):
    """The nine-column part list."""

    def __init__(self, rows: Optional[Sequence[PartRow]] = None, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[PartRow] = list(rows or ())
        #: References the BOM estimator says push the board into Standard mode.
        self._standard_trigger_refs: set[str] = set()
        self._highlight_standard = True
        #: Per-reference price label, filled by the estimator. Kept out of
        #: PartRow because it is recomputed on a different cadence from the row.
        self._prices: dict[str, str] = {}

    # -- Qt model interface -------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt override
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt override
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(  # noqa: N802 - Qt override
        self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole
    ):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section][0]
        if role == Qt.ItemDataRole.ToolTipRole:
            return _HEADER_TOOLTIPS.get(section)
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(row, column)
        if role == SORT_ROLE:
            return self._sort_key(row, column)
        if role == REFERENCE_ROLE:
            return row.reference
        if role == MATCH_TERMS_ROLE:
            # Only the one column the delegate paints; everywhere else this is
            # None so a stray delegate cannot start highlighting a whole row.
            return list(row.match_terms) if column == PARAMS else None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return self._alignment(column)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(row, column)
        if role == Qt.ItemDataRole.FontRole:
            return self._font(row)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(row, column)
        return None

    # -- presentation -------------------------------------------------------

    def _display(self, row: PartRow, column: int):
        if column == REF:
            return row.reference
        if column == VALUE:
            return row.value
        if column == FOOTPRINT:
            return row.footprint
        if column == PARAMS:
            return row.params
        if column == LCSC:
            return row.lcsc
        if column == TYPE:
            return row.part_type
        if column == STOCK:
            if not row.lcsc:
                # Blank, not "?": there is no part to have stock *of* yet, and a
                # "?" here reads as a lookup that failed.
                return ""
            # "?" means nobody answered; "0" means a source said none. Collapsing
            # the two is the bug this fork exists to fix.
            if row.stock is None:
                return UNKNOWN
            return f"{row.stock:,}"
        if column == BOM:
            return EXCLUDED if row.exclude_from_bom else INCLUDED
        if column == POS:
            return EXCLUDED if row.exclude_from_pos else INCLUDED
        return None

    def _sort_key(self, row: PartRow, column: int):
        """Return the value a sort should compare, not the string shown."""
        if column == REF:
            return _natural_key(row.reference)
        if column == STOCK:
            # An unknown sorts below a confirmed zero: "we do not know" is less
            # informative than "there is none", and -1 keeps it out of the way of
            # the real figures.
            return -1 if row.stock is None else row.stock
        if column == BOM:
            return not row.exclude_from_bom
        if column == POS:
            return not row.exclude_from_pos
        return self._display(row, column) or ""

    @staticmethod
    def _alignment(column: int):
        if column in (STOCK,):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if column in (REF, LCSC, TYPE, BOM, POS):
            return int(Qt.AlignmentFlag.AlignCenter)
        return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def _foreground(self, row: PartRow, column: int) -> Optional[QColor]:
        """Colour a cell — row-wide state first, then the stock figure."""
        if row.needs_a_number:
            return theme.unassigned_colour()
        if self._highlight_standard and row.reference in self._standard_trigger_refs:
            return theme.standard_trigger_colour()
        if column == STOCK and row.lcsc:
            # Only for an assigned part: an unassigned row has no stock figure to
            # be unknown *about*, and painting its empty cell grey reads as a
            # failed lookup.
            return theme.stock_colour(row.stock)
        return None

    def _font(self, row: PartRow) -> Optional[QFont]:
        """Bold the rows whose colour is carrying a message."""
        if row.needs_a_number or (
            self._highlight_standard and row.reference in self._standard_trigger_refs
        ):
            return theme.bold(theme.base_font())
        return None

    def _tooltip(self, row: PartRow, column: int) -> Optional[str]:
        if column == STOCK:
            if not row.lcsc:
                return None
            if row.stock is None:
                return (
                    "No source answered for this part.\n"
                    "That is not the same as no stock — LCSC 403s whole networks, "
                    "and a tripped host breaker also reads as unknown."
                )
            return f"{row.stock:,} in JLC's assembly warehouse (not LCSC retail)"
        if column == REF and row.needs_a_number:
            return "In the BOM with no LCSC number — JLC has nothing to place here"
        if column == REF and row.dnp:
            return "Marked Do Not Populate on the board"
        if column in (VALUE, FOOTPRINT, PARAMS):
            return self._display(row, column) or None
        return None

    # -- content ------------------------------------------------------------

    def set_rows(self, rows: Sequence[PartRow]) -> None:
        """Replace every row."""
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> Sequence[PartRow]:
        """Return the current rows."""
        return tuple(self._rows)

    def row_for(self, reference: str) -> Optional[int]:
        """Return the row index of ``reference``, or ``None``."""
        for index, row in enumerate(self._rows):
            if row.reference == reference:
                return index
        return None

    def part(self, row_index: int) -> PartRow:
        """Return the row at ``row_index``."""
        return self._rows[row_index]

    def update_row(self, reference: str, **changes) -> bool:
        """Apply ``changes`` to one row and repaint it.

        Returns ``False`` when the reference is not in the list — which happens
        routinely, because a "Hide excluded BOM" filter can drop a row while a
        detail fetch for it is still in flight.
        """
        index = self.row_for(reference)
        if index is None:
            return False
        row = self._rows[index]
        for name, value in changes.items():
            setattr(row, name, value)
        self._emit_row_changed(index)
        return True

    def set_part_details(
        self, reference: str, part_type: str, stock, params: str
    ) -> bool:
        """Fill in the three columns ``lcsc/details.py`` resolves."""
        return self.update_row(
            reference, part_type=part_type, stock=as_stock(stock), params=params
        )

    def set_standard_trigger_refs(self, references) -> None:
        """Set which references push the board into Standard-mode pricing."""
        new = set(references or ())
        if new == self._standard_trigger_refs:
            return
        changed = new ^ self._standard_trigger_refs
        self._standard_trigger_refs = new
        for reference in changed:
            index = self.row_for(reference)
            if index is not None:
                self._emit_row_changed(index)

    def set_standard_trigger_highlighting_enabled(self, enabled: bool) -> None:
        """Turn the Standard-mode advisory colour on or off."""
        if bool(enabled) == self._highlight_standard:
            return
        self._highlight_standard = bool(enabled)
        if self._rows:
            self._emit_row_changed(0, len(self._rows) - 1)

    def set_bom_price(self, reference: str, label: str) -> None:
        """Record a per-part price label from the estimator."""
        self._prices[reference] = label

    def bom_price(self, reference: str) -> str:
        """Return the estimator's price label for ``reference``."""
        return self._prices.get(reference, "")

    def _emit_row_changed(self, first: int, last: Optional[int] = None) -> None:
        last = first if last is None else last
        self.dataChanged.emit(
            self.index(first, 0),
            self.index(last, len(COLUMNS) - 1),
            [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.ForegroundRole,
                Qt.ItemDataRole.FontRole,
                Qt.ItemDataRole.ToolTipRole,
            ],
        )


def _natural_key(text: str):
    """Sort R2 before R10, the way ``helpers.natural_sort_collation`` does."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"([0-9]+)", text or "")
    ]


_HEADER_TOOLTIPS = {
    STOCK: (
        "JLC assembly stock — what the SMT service can place.\n"
        "Not LCSC retail, which is a different warehouse and routinely "
        "disagrees by orders of magnitude.\n"
        "'?' means nobody answered; '0' means a source confirmed none."
    ),
    PARAMS: "Key parameters pulled out of the LCSC description",
    TYPE: "JLC library type: Basic, Preferred or Extended",
    BOM: "Included in the BOM",
    POS: "Included in the position (CPL) file",
}
