"""The LCSC Explorer window — search, facets, results, details, import, assign.

The Qt port of ``lcsc/explorer.py``, the biggest single piece of the migration.

The fetch ordering is the wx module's and is not incidental. The keyword search
returns JLC assembly stock for a hundred rows in one request, but retail stock is
one request *per part*, so the window fills in progressively, cheapest and most
useful first:

1. the search, which paints the grid and hands out the photo ids;
2. in parallel, the row thumbnails and — in the LCSC retail view only — retail
   stock for the visible rows, each on its own bounded pool. They share no
   requests, so neither waits for the other;
3. the selected part's availability report;
4. its symbol and footprint drawings;
5. its photo, last always, because it is the one thing nobody needs in order to
   choose a part.

**One inventory at a time** is what keeps the cost of that honest: pick JLC
assembly and the window issues no retail requests at all. There used to be a
"Both inventories" option and it could not be made to work — a hundred extra
per-part lookups per search, re-fired on every filter change, which LCSC answers
with a 403 in some regions and EasyEDA answers with a rate-limit ban. The plan's
§5.2 still lists it; the running plugin dropped it, and so does this.

Assignment leaves through :attr:`ExplorerWindow.assign_requested` rather than
being performed here. Phase 3 settled that the window reports and the controller
writes, and ``SuiteController.assign_number`` is the single funnel every source
of a number goes through. Importing library assets *is* done here: it writes
files under a folder this window owns the field for, and touches neither the
board nor the project database.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
import webbrowser

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...shared import lcsc_api as api
from .. import theme
from ..photo_viewer import PhotoViewer
from .detail import DetailPane
from .facets import FILTER_DEBOUNCE_MS, Debounce, FacetPanel
from .preview import render_previews
from .results import (
    COLUMN_INDEX,
    HIT_ROLE,
    ROW_HEIGHT_PX,
    THUMB_PX,
    CatalogDelegate,
    ResultsModel,
    ResultsView,
    StockDelegate,
    ThumbnailDelegate,
    configure_header,
    decode_thumbnail,
    fit_columns,
    inline_detail_height,
)
from .tasks import (
    RETAIL_FILL_LIMIT,
    RETAIL_FILL_WORKERS,
    THUMB_FILL_LIMIT,
    THUMB_FILL_WORKERS,
    Pool,
    Tokens,
    bounded,
)

log = logging.getLogger(__name__)

#: The two inventories, as a mutually exclusive choice.
STOCK_VIEWS: list[tuple[str, str]] = [
    ("jlc", "JLC assembly"),
    ("retail", "LCSC retail"),
]

SORT_MODES: list[tuple[str, str]] = [
    ("relevance", "Best match"),
    ("jlc", "JLC assembly stock (high first)"),
    ("retail", "LCSC retail stock (high first)"),
    ("price", "Unit price (low first)"),
    ("min_qty", "Minimum quantity (low first)"),
]

LIBRARY_FILTERS: list[tuple[Optional[str], str]] = [
    (None, "All"),
    ("base", "Basic only"),
    ("expand", "Extended only"),
]

#: Where the selected part's details appear. ``below`` is the desktop equivalent
#: of JLCPCB's expanded result row; ``side`` is better on wide screens.
DETAIL_LAYOUTS: list[tuple[str, str]] = [
    ("side", "Side panel"),
    ("below", "Inline below"),
]

PAGE_SIZE = 100

#: Opening size. §5.2 measured the wx dialog at 1470x831.
DEFAULT_SIZE = (1470, 831)


class ExplorerWindow(QDialog):
    """Search, inspect, import and assign LCSC parts."""

    #: ``(number, stock)`` — the controller performs the write. ``stock`` is
    #: deliberately object-typed: ``None`` means the search reported no figure,
    #: and the part list draws that as blank rather than as "out of stock".
    assign_requested = Signal(str, object)

    def __init__(
        self,
        parent,
        source,
        settings=None,
        references=None,
        keyword: str = "",
        board_path: str = "",
    ) -> None:
        super().__init__(parent)
        self.source = source
        self.settings = settings
        self.references: list[str] = list(references or [])
        self.board_path = board_path

        self._tokens = Tokens()
        self._all_hits: list = []
        self._facets: dict = {}
        self._report = None
        self._photo_viewer: Optional[PhotoViewer] = None
        self._detail_layout = self._setting("explorer_detail_layout", "side")
        self._details_shown = False
        #: The throwaway widget the grid owns while the pane is inline. Never
        #: the pane itself — see ``_detach_inline``.
        self._inline_host: Optional[QWidget] = None
        #: Whether the gesture now in progress is what selected the current row.
        #: Set by the selection change, cleared by the next press. See
        #: ``eventFilter`` and ``_on_cell_clicked``.
        self._selected_by_this_press = False
        #: True once a gesture has turned into a double-click, until the next
        #: press. The release that ends a double-click emits ``clicked`` like
        #: any other, and without this the pane would toggle twice.
        self._double_click_gesture = False
        #: The hit under the cursor when the gesture began, which is the part
        #: the user aimed at. See ``eventFilter``.
        self._gesture_hit = None
        #: True while the pane is being moved. See ``_set_details_shown``.
        self._placing = False

        self.setWindowTitle("LCSC Explorer")
        self.resize(*DEFAULT_SIZE)

        # Pools and the debounce before the widgets: building the grid connects
        # the facet panel straight to the debounce.
        self._retail_pool = Pool("lcsc-retail", RETAIL_FILL_WORKERS, self._on_retail)
        self._thumb_pool = Pool("lcsc-thumbs", THUMB_FILL_WORKERS, self._on_thumbnail)
        self._search_pool = Pool("lcsc-search", 1, self._on_search_done)
        self._detail_pool = Pool("lcsc-detail", 3, self._on_detail_done)
        self._import_pool = Pool("lcsc-import", 1, self._on_import_finished)
        self._filter_debounce = Debounce(FILTER_DEBOUNCE_MS, self.apply_filters, self)
        self._import_assign = False

        self.model = ResultsModel(self)
        self._build_ui()
        # The combo is set to the restored preference during ``_build_ui``, and
        # it is set *before* its handler is connected — deliberately, so that
        # restoring a preference does not count as changing it. The cost is that
        # ``DetailPane`` never hears about it and stays in its constructor
        # default, so a session that had chosen "Inline below" reopened with the
        # column arrangement crammed into the inline row: previews at full width,
        # the availability card and the parameter table clipped below the fold.
        # Stated here rather than by connecting earlier, because the handler also
        # writes the setting back and re-places the pane.
        self.detail.set_layout_mode(self._detail_layout)
        self._restore_geometry()

        self._apply_inventory()
        self._update_target_label()
        self._update_actions()
        if keyword:
            self.keyword.setText(keyword)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Assemble the window."""
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        root.addWidget(self._build_search_box())

        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(self._build_results_side())
        self.detail = DetailPane(self._splitter)
        self.detail.photo_clicked.connect(self._on_detail_photo_clicked)
        self._splitter.addWidget(self.detail)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 2)
        # Built, then immediately put away. The grid is what this window is for,
        # and the detail pane costs it a third of its width for something you
        # only want once you have picked a candidate — so it stays closed until
        # a row is clicked.
        self.detail.hide()
        root.addWidget(self._splitter, 1)

        root.addWidget(self._build_action_bar())

    def _build_search_box(self) -> QWidget:
        """Build the keyword row and the filter row, in one titled group."""
        box = QGroupBox("Find parts", self)
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 4, 8, 6)
        outer.setSpacing(5)

        query = QHBoxLayout()
        query.setSpacing(6)
        self.keyword = QLineEdit(box)
        self.keyword.setPlaceholderText(
            "e.g. 22k 0805 0.1%   or   AD7124   or   C374726"
        )
        self.keyword.returnPressed.connect(self.start_search)
        query.addWidget(self.keyword, 1)

        self.search_button = QPushButton("Search", box)
        self.search_button.setDefault(True)
        self.search_button.clicked.connect(self.start_search)
        query.addWidget(self.search_button)

        self.refresh_button = QPushButton("Refresh data", box)
        self.refresh_button.setToolTip(
            "Drop cached stock/price data and re-query, and re-arm every host "
            "that has been refusing us. Stock figures are cached for 5 minutes. "
            "This is also the 'I fixed my connection' button."
        )
        self.refresh_button.clicked.connect(self._on_refresh)
        query.addWidget(self.refresh_button)
        outer.addLayout(query)

        options = QHBoxLayout()
        options.setSpacing(6)

        options.addWidget(QLabel("Inventory:", box))
        self.inventory = QComboBox(box)
        self.inventory.addItems([label for _key, label in STOCK_VIEWS])
        self.inventory.setToolTip(
            "JLC assembly and LCSC retail are separate warehouses whose stock "
            "routinely disagrees. Pick the one you are ordering from: the whole "
            "window — column, filter and detail card — reports on that one."
        )
        self.inventory.currentIndexChanged.connect(self._on_inventory_changed)
        options.addWidget(self.inventory)

        options.addSpacing(6)
        options.addWidget(QLabel("Library:", box))
        self.library_filter = QComboBox(box)
        self.library_filter.addItems([label for _key, label in LIBRARY_FILTERS])
        # A library change is a different *query*, not a different view of the
        # same results: the filter is a parameter of the search endpoint.
        self.library_filter.currentIndexChanged.connect(lambda _i: self.start_search())
        options.addWidget(self.library_filter)

        options.addSpacing(6)
        options.addWidget(QLabel("Sort:", box))
        self.sort_mode = QComboBox(box)
        self.sort_mode.addItems([label for _key, label in SORT_MODES])
        self.sort_mode.setMinimumWidth(210)
        self.sort_mode.currentIndexChanged.connect(lambda _i: self.apply_filters())
        options.addWidget(self.sort_mode)

        options.addSpacing(6)
        self.in_stock_only = QCheckBox("In JLC stock", box)
        self.in_stock_only.toggled.connect(lambda _c: self.apply_filters())
        options.addWidget(self.in_stock_only)
        options.addStretch(1)

        self.filter_toggle = QToolButton(box)
        self.filter_toggle.setText("Filters ▴")
        self.filter_toggle.setCheckable(True)
        self.filter_toggle.setChecked(True)
        self.filter_toggle.setToolTip(
            "Show or hide the parametric filters. Hidden, the result list gets "
            "their height."
        )
        self.filter_toggle.toggled.connect(self._on_filters_toggled)
        options.addWidget(self.filter_toggle)

        options.addSpacing(6)
        options.addWidget(QLabel("Details:", box))
        self.detail_layout_choice = QComboBox(box)
        self.detail_layout_choice.addItems([label for _key, label in DETAIL_LAYOUTS])
        self.detail_layout_choice.setCurrentIndex(
            next(
                i
                for i, (key, _label) in enumerate(DETAIL_LAYOUTS)
                if key == self._detail_layout
            )
        )
        self.detail_layout_choice.setToolTip(
            "Show selected-part details beside the catalogue, or in a full-width "
            "expanded row directly under the part like the JLCPCB parts library."
        )
        self.detail_layout_choice.currentIndexChanged.connect(
            self._on_detail_layout_changed
        )
        options.addWidget(self.detail_layout_choice)
        outer.addLayout(options)
        return box

    def _build_results_side(self) -> QWidget:
        """Build the facet panel, the status line and the grid."""
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.facet_box = QGroupBox("Parametric filters (from part attributes)", panel)
        facet_layout = QVBoxLayout(self.facet_box)
        facet_layout.setContentsMargins(8, 4, 8, 6)
        self.facets = FacetPanel(self.facet_box)
        self.facets.changed.connect(self._filter_debounce.schedule)
        facet_layout.addWidget(self.facets)
        layout.addWidget(self.facet_box)

        self.status = QLabel("Ready.", panel)
        self.status.setProperty("role", "status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.results = ResultsView(panel)
        self.results.setObjectName("results")
        self.results.setModel(self.model)
        self.results.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # No grid, matching the main window's part table. These rows are
        # catalogue cards — a photo, two lines of description, a price block —
        # and ruling them into five boxes fights the layout inside each cell.
        self.results.setShowGrid(False)
        self.results.setWordWrap(False)
        self.results.setAlternatingRowColors(True)
        self.results.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.results.verticalHeader().setVisible(False)
        self.results.verticalHeader().setDefaultSectionSize(ROW_HEIGHT_PX)
        self.results.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        configure_header(self.results)

        self.results.setItemDelegateForColumn(
            COLUMN_INDEX["photo"], ThumbnailDelegate(self.results)
        )
        for key, bold, lines in (
            ("part", True, 2),
            ("description", False, 3),
            ("manufacturer", False, 2),
            ("price", True, 1),
        ):
            self.results.setItemDelegateForColumn(
                COLUMN_INDEX[key],
                CatalogDelegate(self.results, bold=bold, primary_lines=lines),
            )
        for key in ("jlc_stock", "retail_stock"):
            self.results.setItemDelegateForColumn(
                COLUMN_INDEX[key], StockDelegate(self.results)
            )

        self.results.selectionModel().selectionChanged.connect(self._on_row_selected)
        self.results.doubleClicked.connect(self._on_row_activated)
        self.results.clicked.connect(self._on_cell_clicked)
        # Every press starts a fresh gesture. See ``eventFilter``.
        self.results.viewport().installEventFilter(self)
        layout.addWidget(self.results, 1)
        return panel

    def _build_action_bar(self) -> QWidget:
        """Build the target label, the library folder row and the buttons."""
        panel = QWidget(self)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self.target_label = QLabel("", panel)
        self.target_label.setFont(theme.bold(theme.base_font()))
        self.target_label.setWordWrap(True)
        outer.addWidget(self.target_label)

        library = QHBoxLayout()
        library.setSpacing(6)
        library.addWidget(QLabel("Library folder:", panel))
        self.library_path = QLineEdit(self._default_library_root(), panel)
        library.addWidget(self.library_path, 1)
        browse = QPushButton("Browse…", panel)
        browse.clicked.connect(self._on_browse)
        library.addWidget(browse)
        self.overwrite = QCheckBox("Overwrite existing", panel)
        self.overwrite.setChecked(bool(self._setting("overwrite_existing", False)))
        library.addWidget(self.overwrite)
        outer.addLayout(library)

        rule = QFrame(panel)
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFrameShadow(QFrame.Shadow.Plain)
        outer.addWidget(rule)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.import_assign_button = QPushButton("Import and assign", panel)
        self.import_assign_button.setFont(theme.bold(theme.base_font()))
        self.import_assign_button.setToolTip(
            "Import the symbol, footprint and 3D model, then assign this LCSC "
            "number to the selected board footprints."
        )
        self.import_assign_button.clicked.connect(lambda: self._on_import(assign=True))
        buttons.addWidget(self.import_assign_button)

        self.assign_button = QPushButton("Assign number only", panel)
        self.assign_button.setToolTip(
            "Assign the LCSC number without importing library assets."
        )
        self.assign_button.clicked.connect(self._on_assign)
        buttons.addWidget(self.assign_button)

        self.import_button = QPushButton("Import library assets", panel)
        self.import_button.setToolTip(
            "Import the EasyEDA symbol, footprint and 3D model without assigning it."
        )
        self.import_button.clicked.connect(lambda: self._on_import(assign=False))
        buttons.addWidget(self.import_button)
        buttons.addStretch(1)

        self.lcsc_link = QPushButton("Open LCSC", panel)
        self.lcsc_link.clicked.connect(lambda: self._open("lcsc"))
        buttons.addWidget(self.lcsc_link)
        self.jlc_link = QPushButton("Open JLCPCB", panel)
        self.jlc_link.clicked.connect(lambda: self._open("jlc"))
        buttons.addWidget(self.jlc_link)
        self.datasheet_link = QPushButton("Open datasheet", panel)
        self.datasheet_link.clicked.connect(lambda: self._open("datasheet"))
        buttons.addWidget(self.datasheet_link)

        close = QPushButton("Close", panel)
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        outer.addLayout(buttons)
        return panel

    # ------------------------------------------------------------------
    # Settings and geometry
    # ------------------------------------------------------------------

    def _setting(self, key: str, default):
        """Read one ``lcsc`` setting."""
        if self.settings is None:
            return default
        return self.settings.get("lcsc", key, default)

    def _store(self, key: str, value) -> None:
        """Write one ``lcsc`` setting."""
        if self.settings is not None:
            self.settings.set("lcsc", key, value)

    def _restore_geometry(self) -> None:
        """Put the window back where it was left."""
        if self.settings is None:
            return
        saved = self.settings.get("window", "explorer_geometry", "")
        if saved:
            from PySide6.QtCore import QByteArray  # noqa: PLC0415 - local to the use

            self.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii")))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Cancel every fetch, remember the geometry, close the photo window."""
        self._tokens.cancel_all()
        self._retail_pool.clear()
        self._thumb_pool.clear()
        if self.settings is not None:
            self.settings.set(
                "window",
                "explorer_geometry",
                bytes(self.saveGeometry().toBase64()).decode("ascii"),
            )
            self._store("overwrite_existing", self.overwrite.isChecked())
            self._store("library_folder", self.library_path.text().strip())
            self.settings.save()
        # The viewer is a child, so Qt would take it down anyway — but only
        # after this returns, leaving it briefly on screen with nothing behind.
        viewer, self._photo_viewer = self._photo_viewer, None
        if viewer is not None:
            viewer.close()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Target footprints
    # ------------------------------------------------------------------

    def set_references(self, references) -> None:
        """Re-target an open window at a new footprint selection."""
        self.references = list(references or [])
        self._update_target_label()
        self._update_actions()

    def _update_target_label(self) -> None:
        """Say which footprints an assign would land on."""
        if not self.references:
            self.target_label.setText(
                "No footprint selected — Import still works; select footprints "
                "in the main window to enable Assign."
            )
            return
        shown = ", ".join(self.references[:12])
        if len(self.references) > 12:
            shown += f", … (+{len(self.references) - 12})"
        self.target_label.setText(
            f"Assigning to {len(self.references)} footprint(s): {shown}"
        )

    # ------------------------------------------------------------------
    # Inventory, sorting, filtering
    # ------------------------------------------------------------------

    def inventory_view(self) -> str:
        """Return the active inventory key."""
        return STOCK_VIEWS[self.inventory.currentIndex()][0]

    def _shows(self, source: str) -> bool:
        """Report whether ``source`` is the inventory now selected."""
        return self.inventory_view() == source

    def _on_inventory_changed(self, _index: int) -> None:
        """Re-shape the window around the newly chosen inventory."""
        self._apply_inventory()
        self.apply_filters()

    def _apply_inventory(self) -> None:
        """Hide the column and the card the active view does not report on.

        The column collapses to width 0 rather than being removed, so switching
        back restores it. In wx that needed ``SetHidden`` *and* an explicit width
        because the macOS DataView ignores the former; ``setColumnHidden`` is
        enough here.
        """
        view = self.inventory_view()
        self.results.setColumnHidden(COLUMN_INDEX["jlc_stock"], view != "jlc")
        self.results.setColumnHidden(COLUMN_INDEX["retail_stock"], view != "retail")
        self.model.set_show_retail(view == "retail")
        self.detail.show_inventory(view)
        self.in_stock_only.setText(
            "In JLC stock" if view == "jlc" else "In retail stock"
        )
        # The hidden column's width has to go somewhere, and the text columns
        # are what it should go to.
        fit_columns(self.results)

    def _on_filters_toggled(self, shown: bool) -> None:
        """Show or hide the parametric filter panel."""
        self.filter_toggle.setText("Filters ▴" if shown else "Filters ▾")
        self.facet_box.setVisible(shown)

    def apply_filters(self) -> None:
        """Apply the facets, the stock toggle and the sort, then repopulate."""
        # A debounced pass may still be queued behind a direct call — the sort
        # choice and the stock toggle both come straight here — and running it
        # afterwards would rebuild the same grid a second time.
        self._filter_debounce.cancel()
        hits = api.filter_hits(self._all_hits, self.facets.selected())
        if self.in_stock_only.isChecked():
            hits = [hit for hit in hits if self._has_stock(hit)]
        self._set_details_shown(False)
        self.model.set_hits(self._sorted(hits))
        self._update_status()
        self._update_actions()
        # Both passes start together. Thumbnails used to wait for the stock
        # fill, because the photo URL came out of the retail response; the
        # search now carries the photo ids, so the two share nothing and making
        # pictures wait on a hundred sequential lookups just left the grid grey.
        self._start_thumbnails()
        if self._shows("retail"):
            self._start_retail_fill()

    def _has_stock(self, hit) -> bool:
        """Report whether ``hit`` has stock in the inventory now on show.

        A retail figure we do not have counts as "keep it", and **a recorded
        ``None`` is not a figure** — it is "asked, nobody answered", which is the
        one thing `api.py` insists must never be rendered as a zero. Reading the
        presence of the entry rather than its value hid every row LCSC had
        refused to answer about, so switching this filter on while the retail
        hosts were rate-limiting deleted the row the user had just clicked. The
        two states are deliberately one case here: absent and unanswered are both
        "we do not know", and a filter must not pretend to know.

        `asked_retail` keeps the other job — stopping `_start_retail_fill` from
        putting the same refused question twice.
        """
        if self._shows("jlc"):
            return (hit.stock or 0) > 0
        stock = self.model.known_retail(hit.lcsc)
        return stock is None or stock > 0

    def _sorted(self, hits: list) -> list:
        """Order ``hits`` by the active sort mode.

        Unknown values sort last in every mode, so a part we have no data for
        never displaces one we do.
        """
        mode = SORT_MODES[self.sort_mode.currentIndex()][0]
        if mode == "relevance":
            return list(hits)
        if mode == "jlc":
            return sorted(hits, key=lambda h: -(h.stock or 0))
        if mode == "retail":
            return sorted(hits, key=lambda h: -(self.model.known_retail(h.lcsc) or 0))
        if mode == "price":
            return sorted(
                hits, key=lambda h: h.price if h.price is not None else float("inf")
            )
        return sorted(hits, key=lambda h: h.min_qty or 1)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_refresh(self) -> None:
        """Clear caches, re-arm every host and re-run the current search."""
        self.source.clear_cache()
        self.model.forget_fetched()
        self.start_search()

    def search_for(self, keyword: str) -> None:
        """Put ``keyword`` in the box and run it.

        The entry point for the gestures that arrive naming a part — a
        double-clicked row in the part list, or ``Assign LCSC number`` over a
        selection. Separate from :meth:`start_search` because the keyword is
        replaced rather than read: re-targeting an open window at a different
        footprint must not keep searching for the old one.
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return
        self.keyword.setText(keyword)
        self.start_search()

    def start_search(self) -> None:
        """Kick off a background search for the current keyword."""
        keyword = self.keyword.text().strip()
        if not keyword:
            self.status.setText("Enter a keyword or an LCSC part number.")
            return
        self._tokens.search += 1
        self._tokens.retail += 1  # a new result set invalidates the retail fill
        token = self._tokens.search
        self.status.setText(f"Searching for '{keyword}' …")
        self.search_button.setEnabled(False)
        part_type = LIBRARY_FILTERS[self.library_filter.currentIndex()][0]
        source = self.source
        self._search_pool.start(
            token,
            keyword,
            lambda: source.search(keyword, page_size=PAGE_SIZE, part_type=part_type),
        )

    def _on_search_done(self, token: int, keyword, result) -> None:
        """Receive search results on the UI thread."""
        if token != self._tokens.search:
            return  # a newer search superseded this one
        self.search_button.setEnabled(True)
        total, hits = result if result else (0, [])
        self._all_hits = list(hits)
        self._facets = api.build_facets(self._all_hits)
        self.facets.set_facets(self._facets)
        self.apply_filters()
        if not hits:
            self.status.setText(f"No parts found for '{keyword}'.")
        elif total > len(hits):
            self.status.setText(
                f"{total} parts match '{keyword}'; showing the first {len(hits)}. "
                "Narrow the keyword to see the rest."
            )

    def _update_status(self) -> None:
        """Describe the visible result set in terms of the active view."""
        visible = self.model.hits()
        parts = [f"{len(visible)} shown of {len(self._all_hits)} fetched"]
        if self._shows("jlc"):
            count = sum(1 for hit in visible if (hit.stock or 0) > 0)
            parts.append(f"{count} with JLC assembly stock")
        else:
            count = sum(
                1 for hit in visible if (self.model.known_retail(hit.lcsc) or 0) > 0
            )
            asked = sum(1 for hit in visible if self.model.asked_retail(hit.lcsc))
            pending = len(visible) - asked
            # Telling a refused host from an empty warehouse is the whole point:
            # they look identical in the grid and only one means "pick another
            # part". Rows fill top-first, so a fill that stopped early has still
            # answered for what is on screen.
            unreachable = self.source.retail_unreachable()
            if unreachable and asked:
                parts.append(
                    f"{count} with LCSC retail stock — LCSC stopped answering "
                    f"after {asked} of {len(visible)} rows (rate limit); Refresh "
                    "to continue"
                )
            elif unreachable:
                parts.append(
                    "LCSC retail stock unavailable — both lcsc.com and "
                    "easyeda.com are refusing requests. Try again in a few "
                    "minutes, or switch Inventory to JLC assembly"
                )
            else:
                suffix = f" ({pending} still loading)" if pending else ""
                parts.append(f"{count} with LCSC retail stock{suffix}")
        self.status.setText(" — ".join(parts) + ".")

    # ------------------------------------------------------------------
    # Background fills
    # ------------------------------------------------------------------

    def _start_retail_fill(self) -> bool:
        """Fetch LCSC retail stock for the visible rows, bounded and paced.

        Checked before anything is queued, not only inside the workers: a fill
        that ends without recording anything leaves its rows pending, and when
        the user is sorting or filtering on retail stock the completion handler
        re-filters — which would start another fill over the same rows for as
        long as the hosts stay blocked.
        """
        if self.source.retail_unreachable():
            return False
        wanted = [
            hit.lcsc
            for hit in bounded(self.model.hits(), RETAIL_FILL_LIMIT)
            if hit.lcsc and not self.model.asked_retail(hit.lcsc)
        ]
        if not wanted:
            return False
        self._tokens.retail += 1
        token = self._tokens.retail
        source = self.source
        for lcsc in wanted:
            self._retail_pool.start(
                token, lcsc, lambda code=lcsc: source.retail_stock(code)
            )
        return True

    def _on_retail(self, token: int, lcsc, stock) -> None:
        """Write one retail figure into the grid. Runs on the UI thread."""
        if token != self._tokens.retail:
            return
        self.model.set_retail(lcsc, stock)
        self._update_status()

    def _start_thumbnails(self) -> None:
        """Fetch product thumbnails for the top of the grid.

        The photo URL is already in hand: the search response carries a file id
        for every row's primary shot, so this pass is pure image bytes with no
        JSON lookup in front of it.
        """
        wanted = [
            hit
            for hit in bounded(self.model.hits(), THUMB_FILL_LIMIT)
            if hit.lcsc and not self.model.has_thumbnail(hit.lcsc)
        ]
        if not wanted:
            return
        self._tokens.thumb += 1
        token = self._tokens.thumb
        source = self.source
        for hit in wanted:
            url = hit.thumbnail_url
            self._thumb_pool.start(
                token,
                hit.lcsc,
                lambda u=url, code=hit.lcsc: source.image(
                    u or api.retail_thumbnail_url(code)
                ),
            )

    def _on_thumbnail(self, token: int, lcsc, data) -> None:
        """Decode one thumbnail and repaint its cell. Runs on the UI thread.

        Decoded here rather than in the worker because ``QPixmap`` must be built
        on the GUI thread. A 224px JPEG is a fraction of a millisecond, so this
        does not stutter — and unlike wx, one cell can be invalidated on its
        own, so the batching-into-one-repaint dance is not needed either.
        """
        if token != self._tokens.thumb:
            return
        self.model.set_thumbnail(lcsc, decode_thumbnail(data, THUMB_PX))

    # ------------------------------------------------------------------
    # Selection and the detail pane
    # ------------------------------------------------------------------

    def current_hit(self):
        """Return the selected search hit, or ``None``."""
        rows = self.results.selectionModel().selectedRows()
        if not rows:
            return None
        return rows[0].data(HIT_ROLE)

    def select_row(self, row: int) -> None:
        """Select display row ``row``. For the probe and tests."""
        if 0 <= row < self.model.rowCount():
            self.results.selectRow(row)

    def _on_row_selected(self, *_) -> None:
        """Open the detail pane on the newly selected part and fill it.

        Inserting or removing the inline placeholder renumbers the rows under
        it, and the view answers that by re-emitting ``selectionChanged`` for a
        selection nobody moved. Placing the pane therefore arrives back here
        mid-placement; ``_placing`` is what tells the two apart.
        """
        if self._placing:
            return
        hit = self.current_hit()
        if hit is None:
            return
        self._selected_by_this_press = True  # the click to come must not close it
        self._tokens.detail += 1  # anything in flight is for the old row
        self._report = None
        self._update_actions()
        self._set_details_shown(True)
        self._load_details(hit)

    def _on_row_activated(self, index) -> None:
        """Qt's own ``doubleClicked``, which only some double-clicks produce.

        Kept connected for the gestures ``eventFilter`` does not claim — a
        double-click that began on the inline placeholder rather than on a part
        — and because it is the entry point the tests drive. The ordinary route
        is ``eventFilter``; see there for why this signal cannot be relied on.
        """
        self._activate(index.data(HIT_ROLE))

    def _activate(self, hit) -> None:
        """Assign ``hit``'s number to the footprints, then get out of the way.

        Double-click is the gesture a trackpad produces by accident, so it does
        the one thing here that is cheap to undo: it writes the number onto the
        selected footprints. Importing symbol, footprint and 3D model into a
        library on disk is a side effect nobody wants to discover they
        triggered, so it stays behind the buttons in the action bar.

        The number written is the *selected* row's, through :meth:`_on_assign`.
        That is the same part ``hit`` names — the press selects what it lands on
        — and going through the one assign path keeps this from becoming a
        second spelling of it.
        """
        if hit is None:
            return
        if not self.references:
            self.status.setText(
                f"{hit.lcsc}: no footprint selected, so there is nothing to "
                "assign — use the buttons below to import it into a library."
            )
            return
        self._on_assign()
        self.close()

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        """Start a fresh gesture on every press in the results viewport.

        Qt delivers the press here before the view acts on it, which is the only
        moment at which "was this row already the selected one?" can be asked.
        The alternative — leaving the flag set until a click consumes it — gets
        it wrong for a row reached with the arrow keys: the stale flag would eat
        the first click on it, and the pane would need clicking twice to close.

        **Double-click-to-assign is driven from here, not from Qt's
        ``doubleClicked``, because that signal does not arrive.**
        ``QAbstractItemView::mouseDoubleClickEvent`` emits it only while the
        index under the cursor still equals the one the press recorded, and
        opening the detail pane on the pressed row moves the grid out from
        under the cursor before the second click lands: inline, the placeholder
        row is dropped from above and re-inserted below, renumbering everything
        between; in the side panel, the grid narrows and the columns are refit.
        Either way the index differs, Qt takes the double-click for a fresh
        press, and nothing is ever assigned — which is precisely the report,
        "it is very often read as opening or closing the panel".

        So the part is recorded at the press, when the grid still holds still,
        and the ``MouseButtonDblClick`` that Qt does deliver reliably is what
        acts on it. The event is then consumed, so the view cannot go on to
        reinterpret it as the press that would move the selection again.
        """
        if watched is self.results.viewport():
            if event.type() == QEvent.Type.MouseButtonPress:
                self._selected_by_this_press = False
                self._double_click_gesture = False
                self._gesture_hit = self.results.indexAt(
                    event.position().toPoint()
                ).data(HIT_ROLE)
            elif event.type() == QEvent.Type.MouseButtonDblClick:
                self._double_click_gesture = True
                hit, self._gesture_hit = self._gesture_hit, None
                if hit is not None:
                    self._activate(hit)
                    return True
        return super().eventFilter(watched, event)

    def _on_cell_clicked(self, index) -> None:
        """Handle a click on a row, once the view has finished with it.

        Two gestures land here. A click on a thumbnail means "show me that
        picture" on any row, selected or not. A click anywhere else on the row
        whose details are already open closes them again — the same second click
        that collapses an expanded row in JLCPCB's parts library, and the only
        way back to a full-height grid without picking some other part first.

        ``clicked`` fires on release, *after* ``selectionChanged`` has already
        opened the pane for a newly picked row, so without
        ``_selected_by_this_press`` every first click would open the pane and
        shut it again in the same gesture.

        This acts on the spot rather than waiting to see whether a double-click
        follows. Holding it back for ``doubleClickInterval`` is the obvious way
        to keep the two gestures apart and it is the wrong one: the interval is
        half a second by default, every collapse pays it, and it buys only what
        recording the aimed-at row in ``eventFilter`` already gives for free.
        What a double-click must not do is toggle the pane *twice* — see below.
        """
        hit = index.data(HIT_ROLE)
        if index.column() == COLUMN_INDEX["photo"]:
            if hit is not None:
                self.open_photo_viewer(hit)
            return
        current = self.current_hit()
        if self._selected_by_this_press or hit is None or current is None:
            return
        if hit.lcsc != current.lcsc:
            return
        if self._double_click_gesture:
            # The release that *ends* a double-click emits ``clicked`` like any
            # other. Without this the pane would collapse on the first release
            # and reopen on the second, which is the flicker the gesture was
            # reported for. One toggle, the same one a single click performs.
            return
        self._set_details_shown(not self._details_shown)

    def _detach_inline(self) -> None:
        """Take the pane back out of the grid, then drop the placeholder row.

        **The order is the whole of this method.** ``setIndexWidget`` gives the
        widget to the *view*, and the view deletes what it owns: on
        ``setIndexWidget(index, None)``, on the row being removed, and on a model
        reset. It called ``deleteLater`` on ``self.detail`` in all three cases,
        so switching inventory, switching layout or merely clicking a second row
        destroyed the pane — after which every path through here raised
        ``Internal C++ object (DetailPane) already deleted``.

        So the view never owns the pane. It owns a throwaway ``_inline_host``
        that the pane sits inside, and the pane is reparented back to the
        splitter *before* anything is allowed to delete the host.
        """
        self.detail.setParent(self._splitter)
        self.detail.hide()
        row = self.model.inline_row()
        if row >= 0:
            self.results.setSpan(row, 0, 1, 1)
            self.results.setIndexWidget(self.model.index(row, 0), None)
        self._inline_host = None
        self.model.clear_inline_row()

    def _set_details_shown(self, shown: bool) -> None:
        """Show or hide the detail pane, guarding against re-entry.

        The guard is not defensive programming. ``beginRemoveRows`` reaches the
        selection model before it returns, which re-emits ``selectionChanged``,
        which lands in ``_on_row_selected`` and calls straight back into here —
        and the inner call happily installed the pane in a *new* host that the
        outer call then handed to ``setIndexWidget(index, None)``, deleting the
        pane with the host it was still inside. That is the crash.
        """
        if self._placing:
            return
        self._placing = True
        try:
            self._place_details(shown)
        finally:
            self._placing = False

    def _place_details(self, shown: bool) -> None:
        """Show or hide the detail pane, in whichever layout is selected."""
        self._details_shown = shown
        # Unconditional, in both layouts: the pane comes out of the grid before
        # anything else touches it. In ``side`` that is what puts it back in the
        # splitter, and in ``below`` it is what stops the view deleting it.
        self._detach_inline()
        if self._detail_layout == "side":
            self._splitter.insertWidget(1, self.detail)
            self.detail.setVisible(shown)
            if shown:
                # Stated rather than left to the stretch factors: the pane's own
                # size hint is three 140px preview tiles plus two stock cards, and
                # left to argue for that it took 780px of 1470 and pushed the
                # grid's numeric columns off the right-hand edge. Two thirds to
                # the catalogue is what the wx sash position was too.
                width = max(1, self._splitter.width())
                grid = int(width * 0.64)
                self._splitter.setSizes([grid, width - grid])
            return
        # Inline: a real row in the model, spanned across every column, with the
        # pane set on it as an index widget. It scrolls because it is a row.
        if not shown:
            return
        # Read after the detach, never before: removing the old placeholder
        # shifts every row under it up by one, and Qt renumbers the selection to
        # match. Anchoring on the row index taken beforehand put the pane one
        # part too low every time the previous placeholder sat above it.
        rows = self.results.selectionModel().selectedRows()
        if not rows:
            return
        self.model.set_inline_row(rows[0].row())
        row = self.model.inline_row()
        if row < 0:
            return
        self.results.setSpan(row, 0, 1, self.model.columnCount())
        self.results.setRowHeight(
            row,
            inline_detail_height(
                self.results.viewport().height(),
                self.detail.sizeHint().height(),
            ),
        )
        self._inline_host = QWidget(self.results)
        host_layout = QVBoxLayout(self._inline_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.addWidget(self.detail)
        self.detail.setVisible(True)
        self.results.setIndexWidget(self.model.index(row, 0), self._inline_host)

    def _on_detail_layout_changed(self, index: int) -> None:
        """Move the detail pane between the side panel and the inline row."""
        self._detail_layout = DETAIL_LAYOUTS[index][0]
        self._store("explorer_detail_layout", self._detail_layout)
        # Out of whichever host it is in before the other claims it.
        self._detach_inline()
        self.detail.set_layout_mode(self._detail_layout)
        self._set_details_shown(self._details_shown)

    def _load_details(self, hit) -> None:
        """Load availability, previews and photo for ``hit``.

        Three independent fetches rather than one chained job: the numbers a
        decision rests on arrive in a fraction of a second, and the drawings and
        the photo fill in behind them instead of holding them up.
        """
        self.detail.show_pending(hit)
        token = self._tokens.detail
        source = self.source
        quantity = max(1, len(self.references))
        self._detail_pool.start(
            token,
            ("report", hit),
            lambda: source.stock_report(hit.lcsc, needed_qty=quantity),
        )
        self._detail_pool.start(
            token, ("previews", hit), lambda: render_previews(source.cad_data(hit.lcsc))
        )

    def _on_detail_done(self, token: int, key, result) -> None:
        """Route one finished detail fetch to the pane. Runs on the UI thread."""
        if token != self._tokens.detail:
            return
        kind, hit = key
        if kind == "report":
            if result is None:
                return
            self._report = result
            self.detail.show_report(hit, result, max(1, len(self.references)))
            self.model.set_retail(hit.lcsc, result.retail_stock)
            self._update_actions()
            self._start_photo(token, hit, result)
        elif kind == "previews":
            symbol, footprint = result if result else (None, None)
            self.detail.show_previews(symbol, footprint)
        elif kind == "photo":
            self.detail.show_photo(result)

    def _start_photo(self, token: int, hit, report) -> None:
        """Fetch the product photo — the lowest-priority thing in the window."""
        urls = self._photo_urls(hit, report)
        if not urls:
            self.detail.photo_preview.clear("No photo for this part")
            return
        source = self.source

        def work():
            for url in urls:
                data = source.image(url)
                if data:
                    return data
            return None

        self._detail_pool.start(token, ("photo", hit), work)

    @staticmethod
    def _photo_urls(hit, report) -> list[str]:
        """Return the photo URLs to try, best first.

        The report's own images come first — they are LCSC's 900px product
        shots. JLC's file service is appended behind them, and that is not
        padding: LCSC 403s whole networks, taking its image CDN with it, which
        is precisely why §4 says photos come from JLC. The wx version tried only
        ``report.images``, so a blocked CDN meant no picture even though the id
        for a perfectly good one was already in hand.
        """
        urls = list(report.images if report else [])
        for candidate in (hit.photo_url, hit.thumbnail_url):
            if candidate and candidate not in urls:
                urls.append(candidate)
        return urls

    # ------------------------------------------------------------------
    # Photo viewer
    # ------------------------------------------------------------------

    def open_photo_viewer(self, hit) -> None:
        """Show ``hit``'s photo full size, reusing an already-open viewer."""
        if hit is None:
            return
        subtitle = " · ".join(p for p in (hit.model, hit.brand, hit.package) if p)
        url = hit.photo_url or hit.thumbnail_url
        if self._photo_viewer is None:
            self._photo_viewer = PhotoViewer(self, self.source)
            self._photo_viewer.finished.connect(self._on_viewer_closed)
        self._photo_viewer.show_part(hit.lcsc, subtitle, url)
        self._photo_viewer.show()
        self._photo_viewer.raise_()

    def _on_viewer_closed(self, *_) -> None:
        """Forget the closed viewer so the next click opens a fresh one."""
        self._photo_viewer = None

    def _on_detail_photo_clicked(self) -> None:
        """Enlarge the detail pane's photo tile."""
        self.open_photo_viewer(self.current_hit())

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _update_actions(self) -> None:
        """Enable and disable the action bar for the current selection."""
        hit = self.current_hit()
        has = hit is not None
        for button in (self.import_button, self.lcsc_link, self.jlc_link):
            button.setEnabled(has)
        self.assign_button.setEnabled(has and bool(self.references))
        self.import_assign_button.setEnabled(has and bool(self.references))
        self.datasheet_link.setEnabled(
            has
            and bool(
                (self._report and self._report.datasheet) or (hit and hit.datasheet)
            )
        )

    def _open(self, which: str) -> None:
        """Open the LCSC page, the JLC page or the datasheet in a browser."""
        hit = self.current_hit()
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

    def _on_assign(self) -> None:
        """Report the selected number for the controller to write."""
        hit = self.current_hit()
        if hit is None or not self.references:
            return
        # Not ``or 0``: a search that reported no figure means we do not know
        # the stock, and the part list draws that as blank rather than claiming
        # the part is out of stock.
        self.assign_requested.emit(hit.lcsc, hit.stock)
        self.status.setText(
            f"Assigned {hit.lcsc} to {len(self.references)} footprint(s)."
        )

    # -- library import ------------------------------------------------------

    def _default_library_root(self) -> str:
        """Default to a project-local library so the design stays portable."""
        configured = self._setting("library_folder", "")
        if configured:
            return str(configured)
        if self.board_path:
            return str(Path(self.board_path).parent / "lcsc-lib")
        return str(Path.home() / "Documents" / "KiCad" / "lcsc-lib")

    def _on_browse(self) -> None:
        """Pick the directory the library triplet is written into."""
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose the directory for the LCSC library", self.library_path.text()
        )
        if chosen:
            self.library_path.setText(chosen)

    def _importer(self):
        """Build an importer for the configured library root."""
        from ...shared import lcsc_importer  # noqa: PLC0415 - local to the use

        root = self.library_path.text().strip() or self._default_library_root()
        project_dir = str(Path(self.board_path).parent) if self.board_path else ""
        return lcsc_importer.LcscImporter(
            root=root,
            lib_name=lcsc_importer.DEFAULT_LIB_NAME,
            project_relative=lcsc_importer.is_inside(str(root), project_dir),
        )

    def _on_import(self, assign: bool = False) -> None:
        """Import the selected part's symbol, footprint and 3D model."""
        hit = self.current_hit()
        if hit is None:
            return
        importer = self._importer()
        overwrite = self.overwrite.isChecked()
        project_dir = str(Path(self.board_path).parent) if self.board_path else ""
        self.import_button.setEnabled(False)
        self.import_assign_button.setEnabled(False)
        self.status.setText(f"Importing {hit.lcsc} …")

        def work():
            result = importer.import_part(hit.lcsc, overwrite=overwrite)
            actions = (
                importer.register_libraries(project_dir=project_dir)
                if result.ok
                else []
            )
            return result, actions

        # Its own pool: an import is a long, single, user-initiated job and has
        # no business queueing behind sixty thumbnails.
        self._import_assign = assign
        self._import_pool.start(0, hit.lcsc, work)

    def _on_import_finished(self, _token: int, _key, result) -> None:
        """Adapt the pool's ``(token, key, result)`` to the outcome handler."""
        self._on_import_done(result, self._import_assign)

    def _on_import_done(self, result, assign: bool) -> None:
        """Report the import outcome and optionally assign the part."""
        self.import_button.setEnabled(True)
        self._update_actions()
        if result is None:
            self.status.setText("Import failed; see the log.")
            return
        outcome, actions = result
        self.status.setText(outcome.describe())
        for line in actions:
            log.info(line)
        if outcome.errors:
            QMessageBox.critical(
                self,
                "LCSC import failed",
                outcome.describe() + "\n\n" + "\n".join(actions),
            )
            return
        message = outcome.describe()
        if actions:
            message += "\n\n" + "\n".join(actions)
        message += (
            "\n\nKiCad caches library tables at startup — if the new library "
            "does not appear in the symbol chooser, restart KiCad."
        )
        QMessageBox.information(self, "LCSC import", message)
        if assign:
            self._on_assign()


__all__ = [
    "DETAIL_LAYOUTS",
    "LIBRARY_FILTERS",
    "PAGE_SIZE",
    "SORT_MODES",
    "STOCK_VIEWS",
    "ExplorerWindow",
]
