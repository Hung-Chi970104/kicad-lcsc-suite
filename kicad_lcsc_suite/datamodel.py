"""Implementation of the Datamodel for the parts list with natural sort."""

import logging
import re

import wx  # pylint: disable=import-error
import wx.dataview as dv

from .dataview_highlight import (
    decode_highlighted_value,
    encode_highlighted_value,
    expand_footprint,
    expand_value,
)
from .helpers import loadIconScaled
from .lcsc import theme
from .partselector_columns import COLUMN_INDEX, MODEL_COLUMN_TYPES


def stock_text(value) -> str:
    """Render a stock figure as the Stock column's text.

    Every text column of this model is declared to wx as a ``"string"``, and
    ``wxDataViewRenderer`` *silently discards* a cell whose variant type does
    not match the column's — an ``int`` written here renders as a blank cell,
    not as a number, with nothing but a ``wxLogDebug`` line to say so. The
    part list is fed from several places (the bulk database, the API detail
    cache, the explorer's assignment event) and only some of them hand over
    strings, so the coercion belongs here rather than at each caller.

    ``None`` and ``""`` both mean "we have not looked", which is not the same
    fact as a confirmed zero and must stay blank.
    """
    if value is None or value == "":
        return ""
    return str(value)


def standard_trigger_colour() -> wx.Colour:
    """Row colour for parts that push the board into Standard-mode pricing.

    Resolved per call rather than stored as a constant: the deep red this
    used to hard-code was picked against a white list background and turns
    into unreadable mud on a dark one, and KiCad follows the desktop
    appearance, which the user can change while the plugin is open.

    Advisory amber rather than ``bad`` red. These rows are not broken — they
    cost more to assemble — and red made them indistinguishable from the
    genuine error state below.
    """
    return theme.colour("standard")


def unassigned_colour() -> wx.Colour:
    """Row colour for a BOM part with no LCSC number.

    This is the one actionable failure the list can show: the part is going
    into the BOM and JLC has nothing to place. Parts excluded from the BOM
    (mounting holes, fiducials, test points) are silently fine without a
    number and are never marked.
    """
    return theme.colour("bad")


class PartListDataModel(dv.PyDataViewModel):
    """Datamodel for use with the DataViewCtrl of the mainwindow."""

    # The TRAILING_SPACER_COL is used to ensure that the last visible column
    # (PRICE_COL) doesn't stretch when the control is wider than the total
    # column width. It contains an empty string and is hidden from view, but
    # it allows the PRICE_COL to maintain a consistent width.
    columns = {
        "REF_COL": 0,
        "VALUE_COL": 1,
        "FP_COL": 2,
        "LCSC_COL": 3,
        "TYPE_COL": 4,
        "STOCK_COL": 5,
        "BOM_COL": 6,
        "POS_COL": 7,
        "DNP_COL": 8,
        "ROT_COL": 9,
        "SIDE_COL": 10,
        "PARAMS_COL": 11,
        "ENRICH_COL": 12,
        "PRICE_COL": 13,
        "TRAILING_SPACER_COL": 14,
    }

    def __init__(self, scale_factor):
        super().__init__()
        self.data = []
        self.standard_trigger_refs = set()
        self.standard_trigger_highlighting_enabled = True

        self.bom_pos_icons = [
            loadIconScaled(
                "mdi-check-color.png",
                scale_factor,
            ),
            loadIconScaled(
                "mdi-close-color.png",
                scale_factor,
            ),
        ]
        self.side_icons = [
            loadIconScaled(
                "TOP.png",
                scale_factor,
            ),
            loadIconScaled(
                "BOT.png",
                scale_factor,
            ),
        ]
        self.logger = logging.getLogger(__name__)

    # The following methods implement row-level highlighting for parts that
    # trigger the Standard mode pricing.  (e.g. parts on more than one side,
    # or parts that are flagged by JLC as 'standard assembly')
    def set_standard_trigger_refs(self, refs):
        """Set references that should be highlighted as Standard-mode triggers."""
        self.standard_trigger_refs = set(refs or [])

    def set_standard_trigger_highlighting_enabled(self, enabled):
        """Enable or disable Standard-mode trigger row highlighting."""
        self.standard_trigger_highlighting_enabled = bool(enabled)

    def GetAttr(self, item, col, attr):
        """Colour a row by the one thing about it that needs attention.

        Two states, and they cannot overlap: a Standard-mode trigger is by
        definition a part that *has* an LCSC number, and an unassigned part
        has none. Unassigned wins the ordering anyway because it is the more
        urgent of the two.
        """
        del col
        row = self.ItemToObject(item)
        if not row:
            return False

        if self._is_unassigned_bom_part(row):
            attr.SetColour(unassigned_colour())
            if hasattr(attr, "SetBold"):
                attr.SetBold(True)
            return True

        if not self.standard_trigger_highlighting_enabled:
            return False
        ref = str(row[self.columns["REF_COL"]] or "")
        if ref not in self.standard_trigger_refs:
            return False

        attr.SetColour(standard_trigger_colour())
        if hasattr(attr, "SetBold"):
            attr.SetBold(True)
        return True

    def _is_unassigned_bom_part(self, row) -> bool:
        """Report whether ``row`` is headed for the BOM without an LCSC number.

        Read straight off the row rather than from a set pushed in by the BOM
        controller: both facts already live in the model, and a separate set
        would go stale on every BOM toggle. ``BOM_COL`` holds an icon by this
        point — index 0 is the tick, meaning included — which is the same
        identity check ``toggle_bom`` makes.
        """
        if str(row[self.columns["LCSC_COL"]] or "").strip():
            return False
        return row[self.columns["BOM_COL"]] == self.bom_pos_icons[0]

    @staticmethod
    def natural_sort_key(s):
        """Return a tuple that can be used for natural sorting."""
        return [
            int(text) if text.isdigit() else text.lower()
            for text in re.split("([0-9]+)", s)
        ]

    def GetColumnCount(self):
        """Get number of columns."""
        return len(self.columns)

    def GetColumnType(self, col):
        """Get type of each column."""
        columntypes = (
            "string",
            "string",
            "string",
            "string",
            "string",
            "string",
            "wxDataViewIconText",
            "wxDataViewIconText",
            "wxDataViewIconText",
            "string",
            "wxDataViewIconText",
            "string",
            "string",
            "string",
            "string",
        )
        return columntypes[col]

    def GetChildren(self, parent, children):
        """Get child items of a parent."""
        if not parent:
            for row in self.data:
                children.append(self.ObjectToItem(row))
            return len(self.data)
        return 0

    def IsContainer(self, item):
        """Check if tem is a container."""
        return not item

    def GetParent(self, item):
        """Get parent item."""
        return dv.NullDataViewItem

    def GetValue(self, item, col):
        """Get value of an item."""
        row = self.ItemToObject(item)
        if col in [
            self.columns["BOM_COL"],
            self.columns["POS_COL"],
            self.columns["DNP_COL"],
            self.columns["SIDE_COL"],
        ]:
            icon = row[col]
            return dv.DataViewIconText("", icon)
        return row[col]

    def _encode_params_value(
        self,
        reference: str,
        value: str,
        footprint: str,
        params: str,
    ) -> str:
        """Store params display text together with row-specific highlight terms."""
        value_terms = expand_value(reference, value)
        footprint_terms = expand_footprint(reference, footprint)
        return encode_highlighted_value(
            params,
            [*value_terms, *footprint_terms],
        )

    @staticmethod
    def _decode_params_value(value: str) -> str:
        """Return only the visible params text from an encoded params cell value."""
        return decode_highlighted_value(value)[0]

    def SetValue(self, value, item, col):
        """Set value of an item."""
        row = self.ItemToObject(item)
        if col in [
            self.columns["BOM_COL"],
            self.columns["POS_COL"],
            self.columns["DNP_COL"],
            self.columns["SIDE_COL"],
        ]:
            return False
        row[col] = value
        return True

    def Compare(self, item1, item2, column, ascending):
        """Override to implement natural sorting."""
        val1 = self.GetValue(item1, column)
        val2 = self.GetValue(item2, column)

        if column == self.columns["PARAMS_COL"]:
            val1 = self._decode_params_value(val1)
            val2 = self._decode_params_value(val2)

        key1 = self.natural_sort_key(val1)
        key2 = self.natural_sort_key(val2)

        if ascending:
            return (key1 > key2) - (key1 < key2)
        else:
            return (key2 > key1) - (key2 < key1)

    def find_index(self, ref):
        """Get the index of a part within the data list by its reference."""
        try:
            return self.data.index([x for x in self.data if x[0] == ref].pop())
        except (ValueError, IndexError):
            return None

    def get_bom_pos_icon(self, state: str):
        """Get an icon for a state."""
        return self.bom_pos_icons[int(state)]

    def get_side_icon(self, side: str):
        """Get The side for a layer number."""
        return self.side_icons[0] if side == "0" else self.side_icons[1]

    def AddEntry(self, data: list):
        """Add a new entry to the data model."""
        if len(data) <= self.columns["PRICE_COL"]:
            data.append("")
        if len(data) <= self.columns["TRAILING_SPACER_COL"]:
            data.append("")
        else:
            data[self.columns["TRAILING_SPACER_COL"]] = ""

        data[self.columns["STOCK_COL"]] = stock_text(data[self.columns["STOCK_COL"]])
        data[self.columns["BOM_COL"]] = self.get_bom_pos_icon(
            data[self.columns["BOM_COL"]]
        )
        data[self.columns["POS_COL"]] = self.get_bom_pos_icon(
            data[self.columns["POS_COL"]]
        )
        data[self.columns["DNP_COL"]] = self.get_bom_pos_icon(
            data[self.columns["DNP_COL"]]
        )
        data[self.columns["SIDE_COL"]] = self.get_side_icon(
            data[self.columns["SIDE_COL"]]
        )
        data[self.columns["PARAMS_COL"]] = self._encode_params_value(
            reference=str(data[self.columns["REF_COL"]] or ""),
            value=str(data[self.columns["VALUE_COL"]] or ""),
            footprint=str(data[self.columns["FP_COL"]] or ""),
            params=str(data[self.columns["PARAMS_COL"]] or ""),
        )
        self.data.append(data)
        self.ItemAdded(dv.NullDataViewItem, self.ObjectToItem(data))

    def RemoveAll(self):
        """Remove all entries from the data model."""
        self.data.clear()
        self.Cleared()

    def get_all(self):
        """Get tall items."""
        return self.data

    def get_reference(self, item):
        """Get the reference of an item."""
        return self.ItemToObject(item)[self.columns["REF_COL"]]

    def get_value(self, item):
        """Get the value of an item."""
        return self.ItemToObject(item)[self.columns["VALUE_COL"]]

    def get_lcsc(self, item):
        """Get the lcsc of an item."""
        return self.ItemToObject(item)[self.columns["LCSC_COL"]]

    def get_footprint(self, item):
        """Get the footprint of an item."""
        return self.ItemToObject(item)[self.columns["FP_COL"]]

    def select_alike(self, item):
        """Select all items that have the same value and footprint."""
        obj = self.ItemToObject(item)
        alike = []
        for data in self.data:
            if data[1:3] == obj[1:3]:
                alike.append(self.ObjectToItem(data))
        return alike

    def set_lcsc(self, ref, lcsc, type, stock, params):
        """Set an lcsc number, type and stock for given reference."""
        if (index := self.find_index(ref)) is None:
            return
        item = self.data[index]
        item[self.columns["LCSC_COL"]] = lcsc
        item[self.columns["TYPE_COL"]] = type
        item[self.columns["STOCK_COL"]] = stock_text(stock)
        item[self.columns["PARAMS_COL"]] = self._encode_params_value(
            reference=str(item[self.columns["REF_COL"]] or ""),
            value=str(item[self.columns["VALUE_COL"]] or ""),
            footprint=str(item[self.columns["FP_COL"]] or ""),
            params=str(params or ""),
        )
        item[self.columns["ENRICH_COL"]] = ""
        item[self.columns["PRICE_COL"]] = ""
        self.ItemChanged(self.ObjectToItem(item))

    def set_part_details(self, ref, type, stock, params):
        """Refresh the details columns for a reference, leaving the rest alone.

        Narrower than :meth:`set_lcsc` on purpose: this runs when background
        detail refresh lands newer stock for a part the user has *not* touched,
        and ``set_lcsc`` would clear the enrichment status and the BOM price
        alongside it — visibly resetting columns that did not change.
        """
        if (index := self.find_index(ref)) is None:
            return
        item = self.data[index]
        item[self.columns["TYPE_COL"]] = type
        item[self.columns["STOCK_COL"]] = stock_text(stock)
        item[self.columns["PARAMS_COL"]] = self._encode_params_value(
            reference=str(item[self.columns["REF_COL"]] or ""),
            value=str(item[self.columns["VALUE_COL"]] or ""),
            footprint=str(item[self.columns["FP_COL"]] or ""),
            params=str(params or ""),
        )
        self.ItemChanged(self.ObjectToItem(item))

    def set_bom_price(self, ref, price_label):
        """Set BOM price text for a given part reference."""
        if (index := self.find_index(ref)) is None:
            return
        item = self.data[index]
        item[self.columns["PRICE_COL"]] = price_label
        self.ItemChanged(self.ObjectToItem(item))

    def set_enrichment_status(self, ref, status):
        """Set enrichment status text for a given part reference."""
        if (index := self.find_index(ref)) is None:
            return
        item = self.data[index]
        item[self.columns["ENRICH_COL"]] = status
        self.ItemChanged(self.ObjectToItem(item))

    def remove_lcsc_number(self, item):
        """Remove the LCSC number of an item."""
        obj = self.ItemToObject(item)
        obj[self.columns["LCSC_COL"]] = ""
        obj[self.columns["TYPE_COL"]] = ""
        obj[self.columns["STOCK_COL"]] = ""
        obj[self.columns["PARAMS_COL"]] = ""
        obj[self.columns["ENRICH_COL"]] = ""
        obj[self.columns["PRICE_COL"]] = ""
        self.ItemChanged(self.ObjectToItem(obj))

    def toggle_bom(self, item):
        """Toggle BOM for a given item."""
        obj = self.ItemToObject(item)
        if obj[self.columns["BOM_COL"]] == self.bom_pos_icons[0]:
            obj[self.columns["BOM_COL"]] = self.bom_pos_icons[1]
        else:
            obj[self.columns["BOM_COL"]] = self.bom_pos_icons[0]
        self.ItemChanged(self.ObjectToItem(obj))

    def toggle_pos(self, item):
        """Toggle POS for a given item."""
        obj = self.ItemToObject(item)
        if obj[self.columns["POS_COL"]] == self.bom_pos_icons[0]:
            obj[self.columns["POS_COL"]] = self.bom_pos_icons[1]
        else:
            obj[self.columns["POS_COL"]] = self.bom_pos_icons[0]
        self.ItemChanged(self.ObjectToItem(obj))

    def toggle_bom_pos(self, item):
        """Toggle BOM and POS for a given item."""
        self.toggle_bom(item)
        self.toggle_pos(item)


class PartSelectorDataModel(dv.PyDataViewModel):
    """Datamodel for use with the DataViewCtrl of the partselector modal window."""

    def __init__(self):
        super().__init__()
        self.data = []
        self.columns = dict(COLUMN_INDEX)

        self.logger = logging.getLogger(__name__)

    @staticmethod
    def natural_sort_key(s):
        """Return a tuple that can be used for natural sorting."""
        return [
            int(text) if text.isdigit() else text.lower()
            for text in re.split("([0-9]+)", s)
        ]

    def GetColumnCount(self):
        """Get number of columns."""
        return len(self.columns)

    def GetColumnType(self, col):
        """Get type of each column."""
        return MODEL_COLUMN_TYPES[col]

    def GetChildren(self, parent, children):
        """Get child items of a parent."""
        if not parent:
            for row in self.data:
                children.append(self.ObjectToItem(row))
            return len(self.data)
        return 0

    def IsContainer(self, item):
        """Check if tem is a container."""
        return not item

    def GetParent(self, item):
        """Get parent item."""
        return dv.NullDataViewItem

    def GetValue(self, item, col):
        """Get value of an item."""
        row = self.ItemToObject(item)
        return row[col]

    def SetValue(self, value, item, col):
        """Set value of an item."""
        row = self.ItemToObject(item)
        row[col] = value
        return True

    def Compare(self, item1, item2, column, ascending):
        """Override to implement natural sorting."""
        val1 = self.GetValue(item1, column)
        val2 = self.GetValue(item2, column)

        key1 = self.natural_sort_key(val1)
        key2 = self.natural_sort_key(val2)

        if ascending:
            return (key1 > key2) - (key1 < key2)
        else:
            return (key2 > key1) - (key2 < key1)

    def find_index(self, ref):
        """Get the index of a part within the data list by its reference."""
        try:
            return self.data.index([x for x in self.data if x[0] == ref].pop())
        except (ValueError, IndexError):
            return None

    def AddEntry(self, data: list):
        """Add a new entry to the data model."""
        self.data.append(data)
        self.ItemAdded(dv.NullDataViewItem, self.ObjectToItem(data))

    def RemoveAll(self):
        """Remove all entries from the data model."""
        self.data.clear()
        self.Cleared()

    def get_all(self):
        """Get tall items."""
        return self.data

    def get_lcsc(self, item):
        """Get the reference of an item."""
        return self.ItemToObject(item)[self.columns["lcsc"]]

    def get_type(self, item):
        """Get the reference of an item."""
        return self.ItemToObject(item)[self.columns["type"]]

    def get_stock(self, item):
        """Get the reference of an item."""
        return self.ItemToObject(item)[self.columns["stock"]]
