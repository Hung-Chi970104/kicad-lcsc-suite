"""The LCSC Explorer — one window for everything LCSC.

Combines what previously needed three separate tools:

* keyword search over the JLC parts library, with **real parametric facets**
  built from the attributes the API returns (LCSC's filter sidebar, rebuilt);
* **both** stock figures side by side — JLC assembly and LCSC retail — because
  they are separate inventories that routinely disagree;
* symbol / footprint previews rendered locally from EasyEDA data;
* one-click import of symbol + footprint + 3D model into a registered KiCad
  library, and assignment of the LCSC number to the selected footprints.
"""

from __future__ import annotations

import logging
from pathlib import Path
import threading
from typing import Dict, List, Optional
import webbrowser

import wx  # pylint: disable=import-error
import wx.dataview as dv  # pylint: disable=import-error

from ..events import AssignPartsEvent
from ..helpers import HighResWxSize
from . import api
from .importer import DEFAULT_LIB_NAME, LcscImporter, is_inside
from .previewpanel import SvgPreviewPanel

logger = logging.getLogger(__name__)

COLUMNS = [
    ("LCSC", 90),
    ("MFR Part", 190),
    ("Manufacturer", 130),
    ("Package", 110),
    ("JLC assy", 90),
    ("Lib", 80),
    ("Min qty", 70),
    ("Price", 80),
    ("Description", 340),
]

PAGE_SIZE = 100


class LcscExplorerDialog(wx.Dialog):
    """Search, inspect, import and assign LCSC parts."""

    def __init__(self, parent, initial_keyword: str = "", references=None):
        super().__init__(
            parent,
            id=wx.ID_ANY,
            title="LCSC Explorer",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(1580, 940)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        self.parent = parent
        self.references = list(references or [])
        self.logger = logging.getLogger(__name__)

        self._hits: List[api.SearchHit] = []
        self._visible: List[api.SearchHit] = []
        self._facets: Dict[str, List[str]] = {}
        self._facet_choices: Dict[str, wx.Choice] = {}
        self._selected_facets: Dict[str, str] = {}
        self._report: Optional[api.StockReport] = None
        self._search_token = 0
        self._detail_token = 0

        self._build_ui()
        self.Centre(wx.BOTH)

        if initial_keyword:
            self.keyword.SetValue(initial_keyword)
            self._start_search()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Assemble the dialog layout."""
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(self._build_search_bar(), 0, wx.EXPAND | wx.ALL, 5)

        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        splitter.SetMinimumPaneSize(320)

        left = wx.Panel(splitter)
        self.left_panel = left
        left_sizer = wx.BoxSizer(wx.VERTICAL)
        left_sizer.Add(self._build_facet_panel(left), 0, wx.EXPAND | wx.ALL, 4)
        left_sizer.Add(self._build_results(left), 1, wx.EXPAND | wx.ALL, 4)
        left.SetSizer(left_sizer)

        right = wx.Panel(splitter)
        self.right_panel = right
        right.SetSizer(self._build_detail_panel(right))

        splitter.SplitVertically(left, right, int(self.GetSize().x * 0.62))
        root.Add(splitter, 1, wx.EXPAND | wx.ALL, 5)
        root.Add(self._build_action_bar(), 0, wx.EXPAND | wx.ALL, 5)

        self.SetSizer(root)
        self.Layout()
        self._update_actions()

    def _build_search_bar(self) -> wx.Sizer:
        """Keyword entry, library-type filter and stock toggle."""
        box = wx.StaticBoxSizer(wx.HORIZONTAL, self, "Search LCSC / JLCPCB")

        self.keyword = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.keyword.SetHint("e.g. 22k 0805 0.1%   or   AD7124   or   C374726")
        self.keyword.Bind(wx.EVT_TEXT_ENTER, lambda _e: self._start_search())
        box.Add(self.keyword, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)

        self.search_button = wx.Button(self, label="Search")
        self.search_button.Bind(wx.EVT_BUTTON, lambda _e: self._start_search())
        box.Add(self.search_button, 0, wx.ALL, 4)

        self.lib_filter = wx.Choice(
            self, choices=["All", "Basic only", "Extended only"]
        )
        self.lib_filter.SetSelection(0)
        self.lib_filter.Bind(wx.EVT_CHOICE, lambda _e: self._start_search())
        box.Add(self.lib_filter, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)

        self.in_stock_only = wx.CheckBox(self, label="JLC stock > 0")
        self.in_stock_only.Bind(wx.EVT_CHECKBOX, lambda _e: self._apply_filters())
        box.Add(self.in_stock_only, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)

        self.refresh_button = wx.Button(self, label="Refresh")
        self.refresh_button.SetToolTip(
            "Drop cached stock/price data and re-query. Stock figures are "
            "cached for 5 minutes."
        )
        self.refresh_button.Bind(wx.EVT_BUTTON, self._on_refresh)
        box.Add(self.refresh_button, 0, wx.ALL, 4)

        return box

    def _build_facet_panel(self, parent) -> wx.Sizer:
        """Container for the dynamically-built parametric filters.

        The number of facets is only known after a search, so the controls
        live in a scrolled window with a bounded height — a category with a
        dozen attributes must not squeeze the result list off screen.
        """
        self.facet_box = wx.StaticBoxSizer(
            wx.VERTICAL, parent, "Parametric filters (from part attributes)"
        )

        self.facet_scroller = wx.ScrolledWindow(
            self.facet_box.GetStaticBox(), style=wx.VSCROLL
        )
        self.facet_scroller.SetScrollRate(0, 12)
        self.facet_scroller.SetMinSize(wx.Size(-1, int(self.parent.scale_factor * 96)))
        self.facet_grid = wx.FlexGridSizer(0, 4, 6, 8)
        self.facet_grid.AddGrowableCol(1, 1)
        self.facet_grid.AddGrowableCol(3, 1)
        self.facet_scroller.SetSizer(self.facet_grid)
        self.facet_box.Add(self.facet_scroller, 1, wx.EXPAND | wx.ALL, 4)

        self.facet_hint = wx.StaticText(
            self.facet_box.GetStaticBox(), label="Run a search to populate filters."
        )
        self.facet_box.Add(self.facet_hint, 0, wx.LEFT | wx.BOTTOM, 6)
        return self.facet_box

    def _build_results(self, parent) -> wx.Sizer:
        """Build the result list and its status line."""
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.status = wx.StaticText(parent, label="Ready.")
        sizer.Add(self.status, 0, wx.LEFT | wx.BOTTOM, 4)

        self.results = dv.DataViewListCtrl(
            parent, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_SINGLE
        )
        for label, width in COLUMNS:
            self.results.AppendTextColumn(
                label,
                width=int(self.parent.scale_factor * width),
                mode=dv.DATAVIEW_CELL_INERT,
            )
        self.results.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._on_row_selected)
        self.results.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, lambda _e: self._on_import())
        sizer.Add(self.results, 1, wx.EXPAND)
        return sizer

    def _build_detail_panel(self, parent) -> wx.Sizer:
        """Previews, dual-stock readout, warnings and parameters."""
        sizer = wx.BoxSizer(wx.VERTICAL)

        previews = wx.BoxSizer(wx.HORIZONTAL)
        self.symbol_preview = SvgPreviewPanel(parent, (300, 220), caption="Symbol")
        self.footprint_preview = SvgPreviewPanel(
            parent, (300, 220), caption="Footprint"
        )
        previews.Add(self.symbol_preview, 1, wx.EXPAND | wx.ALL, 3)
        previews.Add(self.footprint_preview, 1, wx.EXPAND | wx.ALL, 3)
        sizer.Add(previews, 0, wx.EXPAND)

        stock_box = wx.StaticBoxSizer(wx.VERTICAL, parent, "Availability")
        self.stock_box = stock_box
        # Two lines: "<LCSC>  <MFR part>" then the dual-stock summary. Reserve
        # both up front so the first real label does not overlap the warnings.
        self.stock_text = wx.StaticText(
            stock_box.GetStaticBox(), label="Select a part.\n "
        )
        font = self.stock_text.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        self.stock_text.SetFont(font)
        stock_box.Add(self.stock_text, 0, wx.EXPAND | wx.ALL, 4)

        self.warning_text = wx.TextCtrl(
            stock_box.GetStaticBox(),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_NONE,
        )
        self.warning_text.SetMinSize(wx.Size(-1, int(self.parent.scale_factor * 120)))
        stock_box.Add(self.warning_text, 1, wx.EXPAND | wx.ALL, 4)
        sizer.Add(stock_box, 0, wx.EXPAND | wx.ALL, 3)

        param_box = wx.StaticBoxSizer(wx.VERTICAL, parent, "Parameters")
        self.param_list = dv.DataViewListCtrl(
            param_box.GetStaticBox(), style=dv.DV_ROW_LINES | dv.DV_SINGLE
        )
        self.param_list.AppendTextColumn(
            "Parameter", width=int(self.parent.scale_factor * 190)
        )
        self.param_list.AppendTextColumn(
            "Value", width=int(self.parent.scale_factor * 220)
        )
        param_box.Add(self.param_list, 1, wx.EXPAND | wx.ALL, 3)
        sizer.Add(param_box, 1, wx.EXPAND | wx.ALL, 3)

        return sizer

    def _build_action_bar(self) -> wx.Sizer:
        """Import / assign / external-link buttons and the library path."""
        outer = wx.BoxSizer(wx.VERTICAL)

        lib_row = wx.BoxSizer(wx.HORIZONTAL)
        lib_row.Add(
            wx.StaticText(self, label="Import into:"),
            0,
            wx.ALL | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        self.lib_path_ctrl = wx.TextCtrl(self, value=self._default_lib_root())
        lib_row.Add(self.lib_path_ctrl, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        browse = wx.Button(self, label="Browse…")
        browse.Bind(wx.EVT_BUTTON, self._on_browse)
        lib_row.Add(browse, 0, wx.ALL, 4)
        self.overwrite_cb = wx.CheckBox(self, label="Overwrite existing")
        lib_row.Add(self.overwrite_cb, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        outer.Add(lib_row, 0, wx.EXPAND)

        row = wx.BoxSizer(wx.HORIZONTAL)
        self.import_button = wx.Button(self, label="Import symbol + footprint + 3D")
        self.import_button.Bind(wx.EVT_BUTTON, lambda _e: self._on_import())
        row.Add(self.import_button, 0, wx.ALL, 4)

        self.assign_button = wx.Button(self, label="Assign LCSC number")
        self.assign_button.Bind(wx.EVT_BUTTON, lambda _e: self._on_assign())
        row.Add(self.assign_button, 0, wx.ALL, 4)

        self.import_assign_button = wx.Button(self, label="Import + assign")
        self.import_assign_button.Bind(
            wx.EVT_BUTTON, lambda _e: self._on_import(assign_after=True)
        )
        row.Add(self.import_assign_button, 0, wx.ALL, 4)

        row.AddStretchSpacer()

        self.lcsc_link = wx.Button(self, label="LCSC page")
        self.lcsc_link.Bind(wx.EVT_BUTTON, lambda _e: self._open("lcsc"))
        row.Add(self.lcsc_link, 0, wx.ALL, 4)

        self.jlc_link = wx.Button(self, label="JLC page")
        self.jlc_link.Bind(wx.EVT_BUTTON, lambda _e: self._open("jlc"))
        row.Add(self.jlc_link, 0, wx.ALL, 4)

        self.datasheet_link = wx.Button(self, label="Datasheet")
        self.datasheet_link.Bind(wx.EVT_BUTTON, lambda _e: self._open("datasheet"))
        row.Add(self.datasheet_link, 0, wx.ALL, 4)

        close = wx.Button(self, wx.ID_CANCEL, label="Close")
        row.Add(close, 0, wx.ALL, 4)

        outer.Add(row, 0, wx.EXPAND)
        return outer

    # ------------------------------------------------------------------
    # Library root
    # ------------------------------------------------------------------

    def _default_lib_root(self) -> str:
        """Default to a project-local library so the design stays portable."""
        try:
            board_file = self.parent.pcbnew.GetBoard().GetFileName()
        except Exception:  # noqa: BLE001 - standalone / no board open
            board_file = ""
        configured = (
            self.parent.settings.get("lcsc", {}).get("library_root")
            if hasattr(self.parent, "settings")
            else None
        )
        if configured:
            return str(configured)
        if board_file:
            return str(Path(board_file).parent / "lcsc-lib")
        return str(Path.home() / "Documents" / "KiCad" / "lcsc-lib")

    def _importer(self) -> LcscImporter:
        """Build an importer for the currently configured library root."""
        root = self.lib_path_ctrl.GetValue().strip() or self._default_lib_root()
        try:
            board_file = self.parent.pcbnew.GetBoard().GetFileName()
        except Exception:  # noqa: BLE001
            board_file = ""
        project_dir = str(Path(board_file).parent) if board_file else ""
        project_relative = is_inside(str(root), project_dir)
        return LcscImporter(
            root=root, lib_name=DEFAULT_LIB_NAME, project_relative=project_relative
        )

    def _on_browse(self, _event) -> None:
        """Pick the directory the library triplet is written into."""
        with wx.DirDialog(
            self,
            "Choose the directory for the LCSC library",
            defaultPath=self.lib_path_ctrl.GetValue(),
        ) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.lib_path_ctrl.SetValue(dialog.GetPath())

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_refresh(self, _event) -> None:
        """Clear caches and re-run the current search."""
        api.clear_cache()
        self._start_search()

    def _start_search(self) -> None:
        """Kick off a background search for the current keyword."""
        keyword = self.keyword.GetValue().strip()
        if not keyword:
            self.status.SetLabel("Enter a keyword or an LCSC part number.")
            return

        self._search_token += 1
        token = self._search_token
        self.status.SetLabel(f"Searching for '{keyword}' …")
        self.search_button.Disable()

        part_type = {1: "base", 2: "expand"}.get(self.lib_filter.GetSelection())

        def work() -> None:
            total, hits = api.jlc_search(
                keyword=keyword, page_size=PAGE_SIZE, part_type=part_type
            )
            wx.CallAfter(self._search_done, token, keyword, total, hits)

        threading.Thread(target=work, daemon=True).start()

    def _search_done(
        self, token: int, keyword: str, total: int, hits: List[api.SearchHit]
    ) -> None:
        """Receive search results on the UI thread."""
        if token != self._search_token:
            return  # a newer search superseded this one
        self.search_button.Enable()
        self._hits = hits
        self._facets = api.build_facets(hits)
        self._selected_facets = {}
        self._rebuild_facets()
        self._apply_filters()

        if total > len(hits):
            self.status.SetLabel(
                f"{total} parts match '{keyword}'; showing the first {len(hits)}. "
                "Narrow the keyword to see the rest."
            )
        elif not hits:
            self.status.SetLabel(f"No parts found for '{keyword}'.")

    def _rebuild_facets(self) -> None:
        """Rebuild the parametric filter dropdowns for the current results."""
        self.facet_scroller.Freeze()
        try:
            self.facet_grid.Clear(delete_windows=True)
            self._facet_choices = {}

            if not self._facets:
                self.facet_hint.SetLabel(
                    "No discriminating attributes in these results — "
                    "the JLC parts library returned no parametric data for them."
                )
            else:
                parent = self.facet_scroller
                self.facet_hint.SetLabel(
                    f"{len(self._facets)} attributes available. "
                    "Filters apply to the fetched result set."
                )
                for name in sorted(self._facets):
                    values = self._facets[name]
                    self.facet_grid.Add(
                        wx.StaticText(parent, label=f"{name}:"),
                        0,
                        wx.ALIGN_CENTER_VERTICAL,
                    )
                    choice = wx.Choice(parent, choices=["Any"] + values)
                    choice.SetSelection(0)
                    choice.Bind(wx.EVT_CHOICE, self._on_facet_changed)
                    choice.facet_name = name  # type: ignore[attr-defined]
                    self._facet_choices[name] = choice
                    self.facet_grid.Add(choice, 1, wx.EXPAND)

                clear = wx.Button(parent, label="Clear filters")
                clear.Bind(wx.EVT_BUTTON, self._on_clear_facets)
                self.facet_grid.Add(clear, 0, wx.ALIGN_CENTER_VERTICAL)

            # The control count changed, so the scroller needs a fresh virtual
            # size and every ancestor needs to re-run its layout — otherwise
            # the new rows draw on top of each other.
            self.facet_grid.Layout()
            self.facet_scroller.FitInside()
            self.facet_box.Layout()
            self.left_panel.Layout()
            self.Layout()
        finally:
            self.facet_scroller.Thaw()

    def _on_facet_changed(self, event) -> None:
        """Record a facet selection and re-filter."""
        choice = event.GetEventObject()
        name = getattr(choice, "facet_name", None)
        if not name:
            return
        if choice.GetSelection() <= 0:
            self._selected_facets.pop(name, None)
        else:
            self._selected_facets[name] = choice.GetStringSelection()
        self._apply_filters()

    def _on_clear_facets(self, _event) -> None:
        """Reset every facet dropdown to 'Any'."""
        self._selected_facets = {}
        for choice in self._facet_choices.values():
            choice.SetSelection(0)
        self._apply_filters()

    def _apply_filters(self) -> None:
        """Apply facets and the stock toggle, then repopulate the list."""
        hits = api.filter_hits(self._hits, self._selected_facets)
        if self.in_stock_only.GetValue():
            hits = [h for h in hits if (h.stock or 0) > 0]
        self._visible = hits
        self._populate(hits)

    def _populate(self, hits: List[api.SearchHit]) -> None:
        """Fill the result grid."""
        self.results.DeleteAllItems()
        for hit in hits:
            price = f"${hit.price:.4f}" if hit.price is not None else "—"
            self.results.AppendItem(
                [
                    hit.lcsc,
                    hit.model,
                    hit.brand,
                    hit.package,
                    f"{hit.stock:,}" if hit.stock is not None else "?",
                    hit.library_type,
                    f"{hit.min_qty:,}" if hit.min_qty else "1",
                    price,
                    hit.description,
                ]
            )
        in_stock = sum(1 for h in hits if (h.stock or 0) > 0)
        self.status.SetLabel(
            f"{len(hits)} shown of {len(self._hits)} fetched — {in_stock} with "
            "JLC assembly stock."
        )
        self._update_actions()

    # ------------------------------------------------------------------
    # Detail pane
    # ------------------------------------------------------------------

    def _current_hit(self) -> Optional[api.SearchHit]:
        """Return the currently selected search hit, if any."""
        row = self.results.GetSelectedRow()
        if row == wx.NOT_FOUND or row >= len(self._visible):
            return None
        return self._visible[row]

    def _on_row_selected(self, _event) -> None:
        """Load availability and previews for the newly selected row."""
        hit = self._current_hit()
        self._update_actions()
        if hit is None:
            return

        self._detail_token += 1
        token = self._detail_token
        self._report = None
        self.stock_text.SetLabel(f"{hit.lcsc} — loading availability …")
        self.warning_text.SetValue("")
        self.param_list.DeleteAllItems()
        self.symbol_preview.clear("Loading …")
        self.footprint_preview.clear("Loading …")

        needed = max(1, len(self.references))

        def work() -> None:
            report = api.stock_report(hit.lcsc, needed_qty=needed)
            symbol_svg = footprint_svg = None
            try:
                # Deferred: vendored, and only needed when a row is selected.
                from easyeda2kicad.easyeda.easyeda_api import (  # noqa: PLC0415
                    EasyedaApi,
                )
                from easyeda2kicad.easyeda.easyeda_svg_renderer import (  # noqa: PLC0415
                    render_footprint_svg,
                    render_symbol_svg,
                )

                cad = EasyedaApi().get_cad_data_of_component(lcsc_id=hit.lcsc)
                if cad:
                    try:
                        symbol_svg = render_symbol_svg(cad)
                    except Exception:  # noqa: BLE001
                        logger.debug("symbol SVG render failed", exc_info=True)
                    try:
                        footprint_svg = render_footprint_svg(cad)
                    except Exception:  # noqa: BLE001
                        logger.debug("footprint SVG render failed", exc_info=True)
            except Exception:  # noqa: BLE001
                logger.debug("EasyEDA preview fetch failed", exc_info=True)
            wx.CallAfter(
                self._detail_done, token, hit, report, symbol_svg, footprint_svg
            )

        threading.Thread(target=work, daemon=True).start()

    def _detail_done(
        self,
        token: int,
        hit: api.SearchHit,
        report: api.StockReport,
        symbol_svg: Optional[str],
        footprint_svg: Optional[str],
    ) -> None:
        """Render the availability report and previews on the UI thread."""
        if token != self._detail_token:
            return
        self._report = report

        self.stock_text.SetLabel(f"{hit.lcsc}  {hit.model}\n{report.summary()}")

        lines: List[str] = []
        if report.retail_domestic is not None or report.retail_overseas is not None:
            lines.append(
                "LCSC warehouses — "
                f"domestic: {_fmt(report.retail_domestic)}, "
                f"overseas: {_fmt(report.retail_overseas)}"
            )
        if report.retail_ladder:
            qty = max(1, len(self.references))
            unit = api.unit_price_at(report.retail_ladder, qty)
            if unit is not None:
                lines.append(
                    f"LCSC retail unit price at qty {qty}: ${unit:.4f} "
                    f"(total ${unit * qty:.2f})"
                )
        lines.extend(report.warnings)
        self.warning_text.SetValue("\n".join(f"• {line}" for line in lines))

        self.param_list.DeleteAllItems()
        params = report.parameters or list(hit.attributes.items())
        for name, value in params:
            self.param_list.AppendItem([name, value])

        self.symbol_preview.set_svg(symbol_svg, "No EasyEDA symbol for this part")
        self.footprint_preview.set_svg(
            footprint_svg, "No EasyEDA footprint for this part"
        )
        # The availability label grew from one line to two — re-layout so the
        # warnings box is pushed down instead of drawing over it.
        self.stock_box.Layout()
        self.right_panel.Layout()
        self._update_actions()

    def _update_actions(self) -> None:
        """Enable/disable buttons based on the current selection."""
        has = self._current_hit() is not None
        for button in (
            self.import_button,
            self.assign_button,
            self.import_assign_button,
            self.lcsc_link,
            self.jlc_link,
        ):
            button.Enable(has)
        self.assign_button.Enable(has and bool(self.references))
        self.import_assign_button.Enable(has and bool(self.references))
        self.datasheet_link.Enable(
            has
            and bool(
                (self._report and self._report.datasheet)
                or (self._current_hit() and self._current_hit().datasheet)
            )
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _open(self, which: str) -> None:
        """Open the LCSC page, JLC page or datasheet in a browser."""
        hit = self._current_hit()
        if hit is None:
            return
        if which == "lcsc":
            url = f"https://www.lcsc.com/product-detail/{hit.lcsc}.html"
        elif which == "jlc":
            url = "https://jlcpcb.com/parts/componentSearch?searchTxt=" + hit.lcsc
        else:
            url = (self._report.datasheet if self._report else "") or hit.datasheet
        if url:
            webbrowser.open(url)

    def _on_import(self, assign_after: bool = False) -> None:
        """Import the selected part into the KiCad library."""
        hit = self._current_hit()
        if hit is None:
            return
        importer = self._importer()
        overwrite = self.overwrite_cb.GetValue()

        self.import_button.Disable()
        self.import_assign_button.Disable()
        self.status.SetLabel(f"Importing {hit.lcsc} …")

        try:
            board_file = self.parent.pcbnew.GetBoard().GetFileName()
        except Exception:  # noqa: BLE001
            board_file = ""
        project_dir = str(Path(board_file).parent) if board_file else ""

        def work() -> None:
            result = importer.import_part(hit.lcsc, overwrite=overwrite)
            actions = []
            if result.ok:
                actions = importer.register_libraries(project_dir=project_dir)
            wx.CallAfter(self._import_done, hit, result, actions, assign_after)

        threading.Thread(target=work, daemon=True).start()

    def _import_done(self, hit, result, actions, assign_after: bool) -> None:
        """Report the import outcome and optionally assign the part."""
        self.import_button.Enable()
        self._update_actions()
        self.status.SetLabel(result.describe())

        for line in actions:
            self.logger.info(line)

        if result.errors:
            wx.MessageBox(
                result.describe() + "\n\n" + "\n".join(actions),
                "LCSC import failed",
                style=wx.ICON_ERROR,
            )
            return

        message = result.describe()
        if actions:
            message += "\n\n" + "\n".join(actions)
        message += (
            "\n\nKiCad caches library tables at startup — if the new library "
            "does not appear in the symbol chooser, restart KiCad."
        )
        wx.MessageBox(message, "LCSC import", style=wx.ICON_INFORMATION)

        if assign_after:
            self._on_assign()

    def _on_assign(self) -> None:
        """Assign the selected LCSC number to the footprints we were opened for."""
        hit = self._current_hit()
        if hit is None or not self.references:
            return
        wx.PostEvent(
            self.parent,
            AssignPartsEvent(
                lcsc=hit.lcsc,
                type=hit.library_type,
                stock=hit.stock or 0,
                references=self.references,
            ),
        )
        self.status.SetLabel(
            f"Assigned {hit.lcsc} to {len(self.references)} footprint(s)."
        )


def _fmt(value: Optional[int]) -> str:
    """Format an optional integer for display."""
    return "?" if value is None else f"{value:,}"
