"""Multi-select parametric filter control for the LCSC Explorer.

LCSC's own filter sidebar uses checkboxes, and that is the right shape for
this data: "±1% *or* ±0.5%" is an ordinary thing to want from a resistor
search, and a single-selection ``wx.Choice`` cannot express it at all.

The control is a ``wx.ComboCtrl`` whose popup is a ``wx.CheckListBox``,
rather than an inline list per attribute. A category routinely exposes six
or more attributes; inline lists tall enough to be usable would push the
result grid off the bottom of the window, which is the thing the facet panel
exists to help you read. Collapsed, each filter occupies one row and states
its own selection.

Clicking an item deliberately does *not* dismiss the popup — the whole point
is picking several values in one visit. A ``wx.Menu`` of check items, the
obvious lower-risk alternative, closes on every click and would make a
three-value selection a three-trip affair.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional, Sequence, Set, Tuple

import wx  # pylint: disable=import-error

logger = logging.getLogger(__name__)

#: Longest popup, in rows, before it starts scrolling. Tall enough to take a
#: tolerance or voltage list in one look, short enough to stay on screen.
MAX_POPUP_ROWS = 14

#: Beyond this many selected values the closed control switches from listing
#: them to counting them, so the row cannot grow without bound.
MAX_LABEL_VALUES = 2

ANY_LABEL = "Any"


def format_selection(values: Sequence[str], total: int) -> str:
    """Build the collapsed label for ``values`` out of ``total`` available."""
    if not values:
        return ANY_LABEL
    if len(values) == total and total > 1:
        return f"{ANY_LABEL} ({total})"
    if len(values) <= MAX_LABEL_VALUES:
        return ", ".join(values)
    return f"{len(values)} selected"


class _CheckListPopup(wx.ComboPopup):
    """A ``wx.CheckListBox`` acting as a ``wx.ComboCtrl`` popup."""

    def __init__(
        self,
        labels: List[str],
        on_toggle: Callable[[], None],
        label_getter: Callable[[], str],
    ):
        super().__init__()
        self._labels = labels
        self._on_toggle = on_toggle
        self._label_getter = label_getter
        self._listbox: Optional[wx.CheckListBox] = None

    # -- wx.ComboPopup interface -----------------------------------------

    def Init(self) -> None:
        """Reset state before the popup control is created."""
        self._listbox = None

    def Create(self, parent) -> bool:
        """Build the check list. Returning False would disable the popup."""
        self._listbox = wx.CheckListBox(parent, choices=self._labels)
        self._listbox.Bind(wx.EVT_CHECKLISTBOX, self._on_check)
        return True

    def GetControl(self):
        """Return the window wx should show as the popup."""
        return self._listbox

    def OnPopup(self) -> None:
        """Raise and focus the list once wx has shown it — macOS only.

        wxOSX shows a combo popup without raising it, and the hosted control
        then hit-tests mouse positions against the *combo's* origin instead of
        its own: rows never highlight under the pointer, clicks land on
        nothing, and the transient window can read a click on itself as a
        click outside and just dismiss. The visible symptom is a list whose
        checkboxes cannot be ticked at all. wxWidgets' own combo sample works
        around it by raising the window after showing it — see wxWidgets
        #15008, where this is diagnosed — and taking focus gives the keyboard
        (arrows plus space) as a second way in.

        Confined to wxOSX because it is a wxOSX defect: wxMSW's ``Show()``
        already raises the popup, which is the whole diagnosis, and it goes
        out of its way *not* to move focus into one. Doing either there would
        be meddling with a platform where this control already works.
        """
        super().OnPopup()
        if self._listbox is None or wx.Platform != "__WXMAC__":
            return
        top = self._listbox.GetTopLevelParent()
        if top is not None:
            top.Raise()
        self._listbox.SetFocus()

    def GetStringValue(self) -> str:
        """Return the text the combo should show.

        wx writes this back into the ComboCtrl every time the popup closes,
        so returning a constant here silently erases whatever the owner had
        set — dismissing the list without ticking anything blanked the row.
        Deferring to the owner's summary keeps the two in step.
        """
        return self._label_getter()

    def GetAdjustedSize(self, minWidth, prefHeight, maxHeight) -> wx.Size:
        """Size the popup to its contents, clamped to what wx will allow."""
        width = max(minWidth, 200)
        height = prefHeight
        if self._listbox is not None:
            best = self._listbox.GetBestSize()
            width = max(width, best.width + 8)
            rows = max(1, min(len(self._labels), MAX_POPUP_ROWS))
            row_height = max(1, best.height / max(1, len(self._labels) or 1))
            height = int(row_height * rows) + 8
        if maxHeight > 0:
            height = min(height, maxHeight)
        return wx.Size(width, max(48, height))

    # -- internals -------------------------------------------------------

    def _on_check(self, event) -> None:
        """Report the new selection without dismissing the popup."""
        event.Skip()
        self._on_toggle()

    def checked_indices(self) -> List[int]:
        """Return the indices currently ticked."""
        if self._listbox is None:
            return []
        return list(self._listbox.GetCheckedItems())

    def set_checked_indices(self, indices) -> None:
        """Tick exactly ``indices``, untick everything else."""
        if self._listbox is None:
            return
        self._listbox.SetCheckedItems(list(indices))


class FacetFilterCtrl(wx.ComboCtrl):
    """One attribute's filter: a collapsed summary over a checkbox list.

    ``values`` is the ``(value, count)`` sequence for the attribute. Counts
    are shown beside each option the way LCSC shows them, because "±1% (63)"
    tells you whether ticking it is worth the click and a bare "±1%" does not.
    """

    def __init__(
        self,
        parent,
        name: str,
        values: Sequence[Tuple[str, int]],
        on_change: Callable[[str, Set[str]], None],
    ):
        super().__init__(parent, style=wx.CB_READONLY)
        self.facet_name = name
        self._values = [str(value) for value, _count in values]
        self._on_change = on_change

        labels = [f"{value}  ({count})" for value, count in values]
        self._popup = _CheckListPopup(
            labels, self._on_popup_toggle, self._current_label
        )
        self.SetPopupControl(self._popup)
        self._refresh_label()
        self.SetToolTip(f"{name} — tick any number of values")

    # -- public API ------------------------------------------------------

    def selected(self) -> Set[str]:
        """Return the ticked values."""
        return {self._values[index] for index in self._popup.checked_indices()}

    def clear(self) -> None:
        """Untick everything and restate the collapsed label.

        Does not notify: the explorer clears every control in one pass and
        re-filters once at the end, rather than once per control.
        """
        self._popup.set_checked_indices([])
        self._refresh_label()

    # -- internals -------------------------------------------------------

    def _on_popup_toggle(self) -> None:
        """Push the new selection out to the owner."""
        self._refresh_label()
        self._on_change(self.facet_name, self.selected())

    def _current_label(self) -> str:
        """Build the collapsed summary from the current ticks."""
        chosen = [
            self._values[index] for index in sorted(self._popup.checked_indices())
        ]
        return format_selection(chosen, len(self._values))

    def _refresh_label(self) -> None:
        """Restate the collapsed summary on the control."""
        self.SetText(self._current_label())
