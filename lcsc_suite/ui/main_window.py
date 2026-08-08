"""The LCSC Suite main window — the Qt port of ``mainwindow.py``'s layout.

The parity target is the migration plan's §5.1, captured from the running wx
plugin at 1300x772. Everything there is reproduced control for control, with two
deliberate differences, both from §1:

* the upper-left toolbar's ``Generate`` button and its ``Auto`` layer dropdown
  are **gone**. Gerber and drill output is out of scope; another plugin the user
  already trusts does it. What remains is one ``Export BOM / CPL`` button — the
  two files that carry LCSC data and that nothing else can produce.
* every Gerber-plotting setting went with it, so the Settings dialog is much
  smaller (§5.3).

Layout, top to bottom:

    ┌ toolbar ── Export BOM / CPL ······················ right-hand group ┐
    │ Boards: [5]  ☐ Force Standard  [Help]                              │
    │ BOM Estimate (5 boards): …                                         │
    │ ┌ part table ───────────────────────────────┐ ┌ per-part toolbar ┐ │
    │ └───────────────────────────────────────────┘ └──────────────────┘ │
    │ ┌ log ──────────────────────────────────────────────────────────┐  │
    └────────────────────────────────────────────────────────────────────┘

The two schematic buttons stay **two explicit buttons**, each warning about what
it overwrites. Board↔schematic sync is never automatic: the two sides are
separate stores of the same fact and the plugin does not get to decide which one
wins.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import (
    QByteArray,
    QItemSelection,
    QItemSelectionModel,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..kicad_bridge import Board
from .delegates import MatchHighlightDelegate
from .icons import ICON_SIZE, icon
from .log_pane import LogPane
from .models.part_table import (
    COLUMNS as MODEL_COLUMNS,
    PARAMS,
    REFERENCE_ROLE,
    SORT_ROLE,
    PartTableModel,
)

log = logging.getLogger(__name__)

#: The wx window is 1300x772. Stated rather than derived, so the screenshots are
#: the same size on both platforms and a parity diff means something.
DEFAULT_SIZE = (1300, 772)

#: The window will not usefully shrink below this; the right-hand toolbar's
#: labels and the table's nine columns both need the width.
MINIMUM_SIZE = (1000, 600)

#: Width of the right-hand per-part toolbar. The wx original is 128px and elides
#: "Assign LCSC number" to "Assign … number"; 152 is what it takes for all ten
#: labels to read in full, which is worth 24px of table width.
PART_TOOLBAR_WIDTH = 152

#: Initial height of the log pane. The wx window gives it 150; 128 in Phase 1,
#: and 112 since Phase 6, because the ten per-part buttons need the difference
#: (see ``_build_part_toolbar``) and the splitter lets anyone who wants a taller
#: log drag for it.
#:
#: The second reduction is Phase 5's doing and was found by looking at a
#: screenshot. The BOM estimate summary is **two** lines where it used to be
#: one — an estimate has a cost breakdown and "no assigned BOM parts" does not —
#: and those 22px came out of the toolbar's budget, leaving it 9px short and
#: `Save mappings` behind an extension arrow. Exactly the "scrolled out of sight
#: on a default-sized window" problem §5.1 records about the wx original, which
#: is the one thing this toolbar was rebuilt to avoid.
LOG_HEIGHT = 112

#: Part-list columns, in §5.1's order, with the wx plugin's widths. Defined
#: alongside the model that fills them, so a new column cannot be added to one
#: and forgotten in the other.
COLUMNS = MODEL_COLUMNS


#: Shown on the Undo button when there is nothing to reverse. It says what the
#: button's scope *is*, because "Undo" next to a board editor that has its own
#: undo history is otherwise a fair question.
UNDO_TOOLTIP_EMPTY = (
    "Reverse the last change this window made — an assignment, a removal or a "
    "BOM/POS toggle. Nothing to reverse yet.\n\n"
    "This is not KiCad's undo: it puts back the project database as well as the "
    "board, which Cmd+Z in the PCB editor cannot do."
)

#: §5.1's row context menu: ``(id, label)``, ``None`` for a separator. Ids are
#: what a controller dispatches on; the labels are free to be reworded.
ROW_MENU = (
    ("enter-lcsc", "Enter LCSC number…"),
    ("copy-lcsc", "Copy LCSC"),
    ("paste-lcsc", "Paste LCSC"),
    (None, None),
    ("correction-by-reference", "Add correction by reference"),
    ("correction-by-package", "Add correction by package"),
    ("correction-by-name", "Add correction by name"),
    (None, None),
    ("find-mapping", "Find mapping"),
    ("add-mapping", "Add mapping"),
)


class MainWindow(QMainWindow):
    """Top-level window. Owns the board connection and every dialog."""

    #: Emitted when the board-count spin box settles on a new value.
    board_count_changed = Signal(int)
    #: Emitted with the selected references whenever the selection changes.
    selection_changed = Signal(list)
    #: Emitted as ``(entry id, references)`` when a row-menu entry is chosen.
    row_menu_triggered = Signal(str, list)
    #: Emitted with the selected references when a row is double-clicked.
    part_activated = Signal(list)
    #: Emitted after the part list has been rebuilt from the board and the
    #: project database. The BOM estimator recomputes on it rather than being
    #: called from each of the six places that reload — one connection cannot be
    #: forgotten the way a seventh call site can.
    parts_reloaded = Signal()
    #: Emitted as the window closes, before its state is saved. The controller
    #: uses it for the one thing that has to be offered on the way out: LCSC
    #: numbers changed this session live on the footprints only until "To
    #: schematic" is pressed, and a *removal* lives nowhere else at all. The
    #: close is not cancellable from here — the offer is a question, not a veto.
    about_to_close = Signal()

    def __init__(self, board: Board, settings=None, parts=None, parent=None) -> None:
        super().__init__(parent)
        self.board = board
        self.settings = settings
        #: The board/database/rows reconciler (``lcsc_suite.parts.PartList``).
        #: Optional so a probe or a test can build the window on its own.
        self.parts = parts
        # Latch: growing a selection to the alike parts fires selectionChanged
        # again, and without this the handler recurses.
        self._selecting_alike = False
        #: Row-menu ids the controller answers; None means all of them. See
        #: set_row_menu_enabled.
        self._enabled_row_menu: set | None = None
        self.setObjectName("lcsc-suite-main")

        info = board.info()
        self.board_info = info
        self.setWindowTitle(f"LCSC Suite — {info.name}")
        self.resize(*DEFAULT_SIZE)
        self.setMinimumSize(*MINIMUM_SIZE)

        self._build_toolbar()
        self._build_central()
        self._build_shortcuts()
        self._restore_geometry()

        self.log_pane.install()
        log.info("Board %s — %d footprints", info.name, len(board.footprints()))
        self.part_model.set_standard_trigger_highlighting_enabled(
            bool(self._setting("general", "highlight_standard_parts", True))
        )
        self.set_estimator_visible(
            bool(self._setting("general", "bom_estimator_show", True))
        )
        self.reload_parts(keep_selection=False)
        self.set_part_buttons_enabled(False)
        # The table, not the spin box, so the window does not open with a text
        # cursor blinking in the board count.
        self.part_table.setFocus()

    # -- construction -------------------------------------------------------

    def _build_toolbar(self) -> None:
        """Build the single top toolbar: left group, stretch, right group."""
        bar = QToolBar("Main", self)
        bar.setObjectName("main-toolbar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setIconSize(ICON_SIZE)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self.main_toolbar = bar

        # First in the left group, and a labelled button rather than only a
        # keyboard shortcut, because Cmd+Z is ambiguous the moment two windows
        # are involved: KiCad owns the board's undo history but this window is
        # the one with focus after a change is made here. See lcsc_suite.undo.
        self.undo_action = self._action(
            bar,
            "Undo",
            "mdi-undo.png",
            UNDO_TOOLTIP_EMPTY,
        )
        self.undo_action.setEnabled(False)
        # StandardKey, so this is Cmd+Z on macOS and Ctrl+Z elsewhere without
        # spelling either one out.
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        bar.addSeparator()

        self.export_action = self._action(
            bar,
            "Export BOM / CPL",
            "fabrication.png",
            "Write the BOM and the CPL (component placement list) for this "
            "board. Gerber and drill output is not this plugin's job — the BOM "
            "and CPL are the two files that carry the LCSC assignments.",
        )

        spacer = QWidget(bar)
        spacer.setSizePolicy(
            spacer.sizePolicy().horizontalPolicy().Expanding,
            spacer.sizePolicy().verticalPolicy(),
        )
        bar.addWidget(spacer)

        # Board <-> schematic lives up here rather than on the per-part toolbar:
        # it acts on the whole project, and the right-hand toolbar is already
        # long enough that a button at its end is scrolled out of sight.
        self.import_schematic_action = self._action(
            bar,
            "From schematic",
            "mdi-database-import-outline.png",
            "Copy the LCSC numbers in the schematic symbols onto the "
            "footprints, overwriting the numbers on the board",
        )
        self.export_schematic_action = self._action(
            bar,
            "To schematic",
            "mdi-database-export-outline.png",
            "Write the LCSC numbers assigned here into the schematic symbols, "
            "overwriting the numbers in the schematic",
        )
        bar.addSeparator()

        self.corrections_action = self._action(
            bar, "Corrections", "mdi-format-rotate-90.png", "Manage part corrections"
        )
        self.mappings_action = self._action(
            bar, "Mappings", "mdi-selection.png", "Manage part mappings"
        )
        bar.addSeparator()

        self.explorer_action = self._action(
            bar,
            "LCSC Explorer",
            "mdi-magnify.png",
            "Search LCSC/JLCPCB with parametric filters, compare assembly vs "
            "retail stock, preview and import symbols and footprints",
        )
        self.import_libs_action = self._action(
            bar,
            "Import libs",
            "mdi-database-import-outline.png",
            "Import symbols, footprints and 3D models for every LCSC part "
            "already assigned on this board",
        )
        bar.addSeparator()

        self.offline_db_action = self._action(
            bar,
            "Offline DB",
            "mdi-cloud-download-outline.png",
            "Optional: download the full JLCPCB parts database (~750 MB) for "
            "offline use. Part details are fetched from the API and cached "
            "locally, so this is not needed for normal work.",
        )
        self.settings_action = self._action(
            bar, "Settings", "mdi-cog-outline.png", "Manage settings"
        )

    def _build_central(self) -> None:
        """Build the estimator row, the table, the per-part toolbar and the log."""
        central = QWidget(self)
        central.setObjectName("central")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 4, 6, 6)
        layout.setSpacing(4)

        layout.addWidget(self._build_estimator_row(central))
        layout.addWidget(self.estimator_summary(central))

        # A splitter rather than the wx version's fixed division. Same default
        # proportions, but a long log or a long part list can be given room
        # without resizing the whole window — and the ten per-part buttons need
        # every pixel the table row can spare (see _build_part_toolbar).
        splitter = QSplitter(Qt.Orientation.Vertical, central)
        splitter.setObjectName("table-log-splitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)

        table_row = QWidget(splitter)
        table_layout = QHBoxLayout(table_row)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(4)
        table_layout.addWidget(self._build_table(table_row), 1)
        table_layout.addWidget(self._build_part_toolbar(table_row), 0)
        splitter.addWidget(table_row)

        self.log_pane = LogPane(splitter)
        splitter.addWidget(self.log_pane)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([DEFAULT_SIZE[1] - LOG_HEIGHT, LOG_HEIGHT])
        self.splitter = splitter
        layout.addWidget(splitter, 1)

        # The wx window has a gauge under the log for the parts-DB download.
        # Hidden until something long-running has progress to report, so it does
        # not read as a permanently empty bar.
        self.progress = QProgressBar(central)
        self.progress.setObjectName("progress")
        self.progress.setMaximumHeight(6)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        layout.addWidget(self.progress, 0)

        self.setCentralWidget(central)

    def _build_estimator_row(self, parent: QWidget) -> QWidget:
        """Build the `Boards: · Force Standard · Help` row."""
        row = QWidget(parent)
        row.setObjectName("estimator-row")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(QLabel("Boards:", row))

        self.boards_input = QSpinBox(row)
        self.boards_input.setObjectName("boards-input")
        # JLC's assembly pricing is quoted in multiples of five and the
        # estimator's ladders are keyed off that, so the step matches.
        self.boards_input.setRange(5, 10000)
        self.boards_input.setSingleStep(5)
        self.boards_input.setFixedWidth(90)
        self.boards_input.setValue(self._setting("general", "bom_estimator_boards", 5))
        self.boards_input.valueChanged.connect(self._on_board_count_changed)
        layout.addWidget(self.boards_input)

        self.force_standard = QCheckBox("Force Standard", row)
        self.force_standard.setObjectName("force-standard")
        self.force_standard.setToolTip(
            "Price the whole board at JLC's Standard assembly rate, whether or "
            "not any part triggers it"
        )
        self.force_standard.setChecked(
            bool(self._setting("general", "bom_estimator_force_standard", False))
        )
        self.force_standard.toggled.connect(self._on_force_standard_toggled)
        layout.addWidget(self.force_standard)

        self.estimator_help = QPushButton("Help", row)
        self.estimator_help.setToolTip("Show BOM estimator assumptions and limitations")
        layout.addWidget(self.estimator_help)

        layout.addStretch(1)
        self.estimator_row = row
        return row

    def estimator_summary(self, parent: QWidget) -> QLabel:
        """Build the status line under the estimator row."""
        board_count = self.boards_input.value()
        label = QLabel(
            f"BOM Estimate ({board_count} boards): no assigned BOM parts", parent
        )
        label.setObjectName("estimator-summary")
        label.setProperty("role", "status")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary_label = label
        return label

    def _build_table(self, parent: QWidget) -> QTableView:
        """Build the part list, its model and its sort proxy."""
        table = QTableView(parent)
        table.setObjectName("part-table")
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        table.setWordWrap(False)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(22)

        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setHighlightSections(False)
        # Interactive, with the widths from COLUMNS applied by set_model().
        # Unlike wx's native DataView, Qt keeps a width set before the window is
        # shown, so there is no "restate them in _on_first_shown" dance here.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        self.part_table = table

        self.part_model = PartTableModel(parent=self)
        # Sorting happens in a proxy over the model, not in SQL the way
        # store.set_order_by does it. Two reasons: a header click cannot then
        # disagree with what the database returned, and SORT_ROLE lets "JLC
        # Stock" sort numerically with an unknown below a confirmed zero — which
        # a string sort puts between "0" and "1".
        self.part_proxy = QSortFilterProxyModel(self)
        self.part_proxy.setSourceModel(self.part_model)
        self.part_proxy.setSortRole(SORT_ROLE)
        self.part_proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.set_model(self.part_proxy)

        # Match highlighting, on the one column that has anything to match. The
        # terms are the row's own value and footprint, so a lit-up cell is one
        # whose derived parameters agree with what the board declares — see
        # ui/delegates.py. Settings' "Highlight search matches" toggles it.
        self.params_delegate = MatchHighlightDelegate(
            self, enabled=bool(self._setting("highlighting", "matches", True))
        )
        table.setItemDelegateForColumn(PARAMS, self.params_delegate)

        table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        table.customContextMenuRequested.connect(self._on_context_menu)
        table.doubleClicked.connect(self._on_part_activated)
        return table

    def _on_part_activated(self, _index) -> None:
        """Report a double-clicked row, with what was selected when it happened."""
        self.part_activated.emit(self.selected_references())

    def set_model(self, model) -> None:
        """Attach a model and apply the column widths from :data:`COLUMNS`."""
        self.part_table.setModel(model)
        for index, (_, width) in enumerate(COLUMNS):
            if index < model.columnCount():
                self.part_table.setColumnWidth(index, width)
        self.part_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)

    # -- selection and the row menu ----------------------------------------

    def selected_references(self) -> list[str]:
        """Return the references of the selected rows, in view order."""
        model = self.part_table.model()
        return [
            model.data(model.index(index.row(), 0), REFERENCE_ROLE)
            for index in self.part_table.selectionModel().selectedRows()
        ]

    def select_references(self, references) -> None:
        """Select exactly ``references``, leaving the view scrolled to the first."""
        model = self.part_table.model()
        wanted = set(references)
        selection = QItemSelection()
        for row in range(model.rowCount()):
            if model.data(model.index(row, 0), REFERENCE_ROLE) in wanted:
                selection.select(
                    model.index(row, 0), model.index(row, model.columnCount() - 1)
                )
        self.part_table.selectionModel().select(
            selection,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )

    def _on_selection_changed(self, *_) -> None:
        """Enable the per-part actions, and pull in alike parts if asked to."""
        references = self.selected_references()
        self.set_part_buttons_enabled(bool(references))
        if (
            len(references) == 1
            and self.select_alike_action.isChecked()
            and not self._selecting_alike
        ):
            self._extend_to_alike(references[0])
        self.selection_changed.emit(self.selected_references())

    def _extend_to_alike(self, reference: str) -> None:
        """Grow a single-row selection to every part with the same value+footprint.

        Guarded by a latch: selecting those rows fires selectionChanged again,
        and without it the handler would recurse.
        """
        if self.parts is None:
            return
        alike = self.parts.alike(reference)
        if len(alike) <= 1:
            return
        self._selecting_alike = True
        try:
            self.select_references(alike)
        finally:
            self._selecting_alike = False

    def set_row_menu_enabled(self, entry_ids) -> None:
        """Declare which row-menu entries are live.

        Called by the controller with the ids it answers. The rest are greyed
        out rather than removed: they are part of the wx plugin's menu and come
        back as their dialogs land, and a menu that changes shape between
        releases is harder to relearn than one with a disabled entry in it.

        ``None`` means "everything", which is what a window built without a
        controller gets.
        """
        self._enabled_row_menu = None if entry_ids is None else set(entry_ids)

    def _on_context_menu(self, position) -> None:
        """Show §5.1's row menu and report which entry was chosen.

        The window builds the menu; the controller decides what the entries
        *do*. Each entry carries a stable id in its data rather than being
        matched on its label, so the wording can change without breaking the
        dispatch.
        """
        references = self.selected_references()
        if not references:
            return
        allowed = self._enabled_row_menu
        menu = QMenu(self)
        for entry_id, label in ROW_MENU:
            if entry_id is None:
                menu.addSeparator()
                continue
            action = menu.addAction(label)
            action.setData(entry_id)
            if allowed is not None and entry_id not in allowed:
                action.setEnabled(False)
        chosen = menu.exec(self.part_table.viewport().mapToGlobal(position))
        if chosen is not None:
            self.row_menu_triggered.emit(chosen.data(), references)

    def _build_part_toolbar(self, parent: QWidget) -> QToolBar:
        """Build the right-hand vertical toolbar of per-part actions."""
        bar = QToolBar("Part actions", parent)
        bar.setObjectName("part-toolbar")
        bar.setOrientation(Qt.Orientation.Vertical)
        bar.setMovable(False)
        bar.setFloatable(False)
        # Ten buttons, icon above label, in a 1300x772 window: at the top
        # toolbar's icon size they do not fit and Qt hides the last two behind
        # an extension arrow — which is exactly the "scrolled out of sight"
        # problem §5.1 records about the wx original. Tightening the padding
        # here is what makes all ten reachable without a wider window.
        bar.setIconSize(ICON_SIZE)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        bar.setStyleSheet(
            "QToolBar#part-toolbar { spacing: 0; padding: 0; }"
            "QToolBar#part-toolbar QToolButton { padding: 0 2px; }"
        )

        self.assign_action = self._action(
            bar,
            "Assign LCSC number",
            "mdi-database-search-outline.png",
            "Search the LCSC catalogue for these footprints, seeded with their "
            "value and package. Double-clicking a row does the same.\n\n"
            "To type a number you already have, use Enter LCSC number… in the "
            "row's right-click menu.",
        )
        self.remove_action = self._action(
            bar,
            "Remove LCSC number",
            "mdi-close-box-outline.png",
            "Remove the LCSC number from the selected footprints",
        )
        self.select_alike_action = self._action(
            bar,
            "Auto-select alike",
            "mdi-checkbox-multiple-marked.png",
            "Automatically select footprints with the same value and footprint",
            checkable=True,
        )
        self.select_alike_action.setChecked(
            bool(self._setting("general", "select_alike_auto", True))
        )
        self.toggle_bom_pos_action = self._action(
            bar,
            "Toggle BOM && POS",
            "bom-pos.png",
            "Toggle both exclude-from-BOM and exclude-from-POS",
        )
        self.toggle_bom_action = self._action(
            bar,
            "Toggle BOM",
            "mdi-format-list-bulleted.png",
            "Toggle the exclude-from-BOM attribute",
        )
        self.toggle_pos_action = self._action(
            bar,
            "Toggle POS",
            "mdi-crosshairs-gps.png",
            "Toggle the exclude-from-POS attribute",
        )
        self.part_details_action = self._action(
            bar,
            "Part details",
            "mdi-text-box-search-outline.png",
            "Show details of an assigned LCSC part",
        )
        self.hide_bom_action = self._action(
            bar,
            "Hide excluded BOM",
            "mdi-eye-off-outline.png",
            "Hide parts excluded from the BOM",
            checkable=True,
        )
        self.hide_pos_action = self._action(
            bar,
            "Hide excluded POS",
            "mdi-eye-off-outline.png",
            "Hide parts excluded from the position files",
            checkable=True,
        )
        self.save_mappings_action = self._action(
            bar,
            "Save mappings",
            "mdi-content-save-settings.png",
            "Remember every footprint+value -> LCSC assignment on this board",
        )

        bar.setFixedWidth(PART_TOOLBAR_WIDTH)
        self.part_toolbar = bar

        self.select_alike_action.toggled.connect(
            lambda checked: self._store_setting("general", "select_alike_auto", checked)
        )
        # The three exclusion toggles write to the board and the project
        # database, so they belong to the controller — see controller.py's rule.
        # The two Hide toggles do not: they filter what this window shows.
        self.hide_bom_action.toggled.connect(self._on_hide_bom_toggled)
        self.hide_pos_action.toggled.connect(self._on_hide_pos_toggled)
        return bar

    def _build_shortcuts(self) -> None:
        """Close on the shortcuts the wx dialog bound."""
        for sequence in ("Ctrl+W", "Ctrl+Q", "Shift+Esc"):
            action = QAction(self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(self.close)
            self.addAction(action)

    def _action(
        self,
        bar: QToolBar,
        text: str,
        icon_name: str,
        tooltip: str,
        checkable: bool = False,
    ) -> QAction:
        """Add one toolbar button, icon above label."""
        action = QAction(icon(icon_name), text, self)
        action.setToolTip(tooltip)
        action.setCheckable(checkable)
        bar.addAction(action)
        return action

    # -- state --------------------------------------------------------------

    def _setting(self, section: str, key: str, default):
        """Read a setting, tolerating there being no Settings object."""
        if self.settings is None:
            return default
        return self.settings.get(section, key, default)

    def _store_setting(self, section: str, key: str, value) -> None:
        """Persist a setting, tolerating there being no Settings object."""
        if self.settings is not None:
            self.settings.set(section, key, value)

    def set_undo_available(self, description: Optional[str]) -> None:
        """Enable the Undo button and name what it would reverse.

        The label stays ``Undo`` — a toolbar button whose text changes width
        makes the whole toolbar shuffle sideways after every action — so the
        action being reversed goes in the tooltip.
        """
        self.undo_action.setEnabled(bool(description))
        if description:
            self.undo_action.setToolTip(
                f"Reverse: {description}\n\n"
                "Puts back the board and the project database both. This is not "
                "KiCad's undo — reversing costs its own entry in KiCad's undo "
                "history rather than removing one."
            )
        else:
            self.undo_action.setToolTip(UNDO_TOOLTIP_EMPTY)

    def set_part_buttons_enabled(self, enabled: bool) -> None:
        """Enable or disable the actions that need a selection.

        ``Auto-select alike`` and the two ``Hide excluded`` toggles are modes,
        not per-part actions, so they stay live with nothing selected.
        """
        for action in (
            self.assign_action,
            self.remove_action,
            self.toggle_bom_pos_action,
            self.toggle_bom_action,
            self.toggle_pos_action,
            self.part_details_action,
        ):
            action.setEnabled(enabled)

    def set_summary_text(self, text: str) -> None:
        """Set the estimator status line."""
        self.summary_label.setText(text)

    def set_estimator_visible(self, visible: bool) -> None:
        """Show or hide the board-count row and the cost summary together.

        Both, because half an estimator is worse than none: the `Boards:` spin
        box on its own looks like a setting that does something, and the summary
        line on its own cannot be changed. Settings' `Show BOM cost estimator`
        drives this.
        """
        visible = bool(visible)
        self.estimator_row.setVisible(visible)
        self.summary_label.setVisible(visible)

    def set_progress(self, value: int | None) -> None:
        """Show a progress figure, or hide the bar when passed ``None``."""
        if value is None:
            self.progress.setVisible(False)
            self.progress.setValue(0)
            return
        self.progress.setVisible(True)
        self.progress.setValue(max(0, min(100, int(value))))

    # -- handlers -----------------------------------------------------------

    def reload_parts(self, keep_selection: bool = True) -> None:
        """Rebuild the part list from the board and the project database."""
        if self.parts is None:
            return
        selected = self.selected_references() if keep_selection else []
        self.parts.refresh_from_board()
        self.part_model.set_rows(self.parts.rows())
        if selected:
            self.select_references(selected)
        assigned = sum(1 for row in self.part_model.rows() if row.assigned)
        log.info("%d parts, %d assigned", self.part_model.rowCount(), assigned)
        self.parts_reloaded.emit()

    def _on_hide_bom_toggled(self, checked: bool) -> None:
        """Show or hide the parts excluded from the BOM."""
        self.hide_bom_action.setText(
            "Show excluded BOM" if checked else "Hide excluded BOM"
        )
        if self.parts is not None:
            self.parts.hide_excluded_bom = checked
        self.reload_parts()

    def _on_hide_pos_toggled(self, checked: bool) -> None:
        """Show or hide the parts excluded from the position files."""
        self.hide_pos_action.setText(
            "Show excluded POS" if checked else "Hide excluded POS"
        )
        if self.parts is not None:
            self.parts.hide_excluded_pos = checked
        self.reload_parts()

    def _on_board_count_changed(self, value: int) -> None:
        """Persist and republish the board count."""
        self._store_setting("general", "bom_estimator_boards", value)
        self.board_count_changed.emit(value)

    def _on_force_standard_toggled(self, checked: bool) -> None:
        """Persist the Force Standard toggle."""
        self._store_setting("general", "bom_estimator_force_standard", checked)

    def raise_to_front(self) -> None:
        """Come forward, because a second launch asked us to.

        Two instances would open the same project database and the same board
        and quietly overwrite each other, so the second process hands the job
        over rather than opening a window of its own.
        """
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def confirm(self, title: str, text: str, informative: str = "") -> bool:
        """Ask a yes/no question, defaulting to No.

        Defaulting to No because every caller of this is about to overwrite
        something the user cannot get back by pressing Ctrl+Z.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        if informative:
            box.setInformativeText(informative)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    # -- geometry -----------------------------------------------------------

    def _restore_geometry(self) -> None:
        """Restore the window size and position from settings.

        Stored as base64 rather than as numbers: ``saveGeometry`` encodes the
        screen the window was on and its maximised state as well, which a
        width/height pair cannot, and it degrades safely when the monitor
        arrangement has changed.
        """
        stored = self._setting("window", "main_geometry", "")
        if not stored:
            return
        try:
            self.restoreGeometry(QByteArray.fromBase64(stored.encode("ascii")))
        except (ValueError, UnicodeEncodeError):
            log.debug("Stored window geometry was unreadable; using the default")

    def _save_geometry(self) -> None:
        """Persist the window size and position."""
        encoded = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self._store_setting("window", "main_geometry", encoded)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        """Offer the schematic export, then save geometry and detach the log."""
        self.about_to_close.emit()
        self._save_geometry()
        self.log_pane.uninstall()
        super().closeEvent(event)
