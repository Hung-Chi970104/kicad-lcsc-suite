#!/usr/bin/env python3
"""Build a plugin dialog headlessly and dump its geometry.

UI regressions here — squeezed sizer panes, DataView columns that silently
collapse, ``wx.CallAfter`` callbacks landing on a destroyed window, and the
``wxAssertionError`` that aborts ``_build_ui()`` part-way — are invisible in a
diff and do not need KiCad running to reproduce. This builds the dialog
against a stub parent, lets the event loop settle, then prints the widget
tree and column widths so expected and actual numbers can be compared.

Run it with the **same interpreter KiCad uses**, because that is the wx build
whose assertions matter:

    # macOS
    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 scripts/gui_probe.py explorer
    # Linux
    python3 scripts/gui_probe.py explorer

Exit status is nonzero if the dialog raised while building.

Note: ``screencapture`` needs Screen Recording permission that CI and most
shells do not have, so assert on geometry and state, never on screenshots.
"""

import argparse
import dataclasses
import os
import sys
import tempfile
import time
import traceback

#: Repository root — the directory the plugin package lives in.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _add_package_to_path() -> None:
    """Make ``kicad_lcsc_suite`` importable from this checkout.

    The repository root is its parent directory, so putting that on the path is
    the whole job — the probe reads the working tree, not whatever happens to be
    installed in KiCad's plugin folder, which is what you want when the point is
    to look at the change you just made.

    This used to hunt KiCad's plugin directories for a symlink back to here and
    fall back to aliasing the repository root under an importable name, because
    the plugin *was* the root and a directory called ``kicad-lcsc-suite`` cannot
    be imported. Giving it a real package directory retired all of that.
    """
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)


_add_package_to_path()

import wx  # noqa: E402
import wx.dataview as dv  # noqa: E402

# The plugin modules are imported BEFORE any wx.App is created: importing the
# package calls JLCPCBPlugin().register(), which asserts on PgmOrNull() outside
# KiCad. Ruff sorts them below wx because the package is first-party; the order
# that matters is these against the wx.App below, not against each other.
from kicad_lcsc_suite import helpers, standalone_impl  # noqa: E402
from kicad_lcsc_suite.datamodel import (  # noqa: E402
    PartListDataModel,
    standard_trigger_colour,
    unassigned_colour,
)
from kicad_lcsc_suite.events import EVT_ASSIGN_PARTS_EVENT  # noqa: E402
from kicad_lcsc_suite.lcsc import api, explorer  # noqa: E402
from kicad_lcsc_suite.lcsc.explorer import LcscExplorerDialog  # noqa: E402
from kicad_lcsc_suite.mainwindow import (  # noqa: E402
    SELECTION_REFRESH_DELAY_MS,
    JLCPCBTools,
    find_open_main_window,
)
from kicad_lcsc_suite.schematicexport import lock_file_for  # noqa: E402
from kicad_lcsc_suite.schematicimport import (  # noqa: E402
    diff_against_board,
    read_schematic,
)
from kicad_lcsc_suite.settings import SettingsDialog  # noqa: E402


class StubParent(wx.Dialog):
    """Minimal stand-in for ``JLCPCBTools``.

    Must be a real ``wx.Dialog`` (the thing it replaces is one) and expose the
    attributes child dialogs reach for on their parent.
    """

    def __init__(self, project_path: str):
        super().__init__(None, title="gui_probe stub")
        self.window = wx.GetTopLevelParent(self)
        self.scale_factor = helpers.GetScaleFactor(self.window)
        self.settings = {}
        self.pcbnew = None
        self.project_path = project_path
        self._part_selector = None


#: Where ``--shot`` writes, or ``None`` when it was not asked for. Module level
#: because the capture happens deep inside each probe's ``inspect`` closure,
#: several frames below the argument parsing, and threading a directory through
#: every one of them would touch signatures that exist for other reasons.
SHOT_DIR = None


def capture(window, name: str) -> None:
    """Screenshot *window* into ``SHOT_DIR``, if one was asked for.

    This is what the Phase 8 parity gate compares the Qt screens against, and
    it is the reason the plan could ask for a side-by-side review at all: §5's
    UI inventory is prose, and no wx screenshot was ever committed.

    ``wx.WindowDC`` reads the window's own drawing surface rather than the
    screen, so this needs no Screen Recording permission — the file header's
    warning about ``screencapture`` does not apply. The window does have to be
    *shown* first, because an unshown window on macOS has nothing drawn in it
    yet; every caller here already shows it.

    Failures are reported and swallowed. A missing screenshot is worth knowing
    about, but it is not worth turning a passing dialog build into a failure —
    the build is what this script is primarily for.
    """
    if not SHOT_DIR:
        return
    try:
        window.Refresh()
        window.Update()
        wx.Yield()
        size = window.GetSize()
        bitmap = wx.Bitmap(size.width, size.height)
        memory = wx.MemoryDC(bitmap)
        memory.Blit(0, 0, size.width, size.height, wx.WindowDC(window), 0, 0)
        memory.SelectObject(wx.NullBitmap)
        os.makedirs(SHOT_DIR, exist_ok=True)
        target = os.path.join(SHOT_DIR, f"{name}.png")
        if bitmap.SaveFile(target, wx.BITMAP_TYPE_PNG):
            print(f"shot: {name} {size.width}x{size.height} -> {target}")
        else:
            print(f"shot: {name} FAILED to encode")
    except Exception as exc:  # noqa: BLE001 - a missing shot must not fail a build
        print(f"shot: {name} FAILED: {exc}")


def dump_tree(win, depth: int = 0) -> None:
    """Print the window tree with each widget's realised rectangle."""
    rect = win.GetRect()
    name = win.__class__.__name__
    print(
        f"{'  ' * depth}{name:<26} {rect.width:>5}x{rect.height:<5} @{rect.x},{rect.y}"
    )
    for child in win.GetChildren():
        dump_tree(child, depth + 1)


def dump_columns(ctrl, label: str) -> None:
    """Print DataView column widths — the values that silently collapse.

    ``cell=`` is the width the column's renderer asks to paint into, which is
    the width it actually gets: a renderer reporting less than its column wraps
    and clips text inside a box narrower than the space on screen.
    """
    print(f"--- {label} columns ---")
    for index, column in enumerate(ctrl.GetColumns()):
        title = column.GetTitle() or "(unnamed)"
        cell = "-"
        renderer = column.GetRenderer()
        if hasattr(renderer, "GetSize"):
            cell = renderer.GetSize().width
        print(
            f"  {index:>2} {title:<18} width={column.GetWidth():<5} "
            f"cell={str(cell):<5} hidden={column.IsHidden()}"
        )


def check_fits(dialog, label: str) -> None:
    """Assert a row fits the grid, so no horizontal scrollbar can appear.

    The native DataView lays every column out wider than it was told to and
    indents the first one, so set widths that add up to the client width still
    overflow it. A row wider than the grid is a horizontal scrollbar and a
    truncated last column, which is the thing being guarded against here.
    """
    grid = dialog.results
    if not grid.GetItemCount():
        return
    row = grid.GetItemRect(grid.RowToItem(0))
    client = grid.GetClientSize().width
    extent = row.x + row.width
    print(
        f"  {label:<22} row_extent={extent:<5} client={client:<5} "
        f"indent={dialog._grid_indent} overhead={dialog._cell_overhead} "
        f"header={dialog._header_px}"
    )
    if extent > client:
        raise AssertionError(
            f"{label}: rows are {extent - client}px wider than the grid — "
            "the header will grow a horizontal scrollbar"
        )


def _synthetic_hits():
    """Build search hits with parametric attributes, no network required.

    Facet controls only exist after a search, so probing them offline means
    feeding the dialog a result set directly. The attribute spread is chosen so
    every facet path is exercised: one value shared by all hits (dropped as
    non-discriminating), and two that split the set.
    """
    spec = [
        ("C1001", "RC0402FR-0710KL", "±1%", "0402", 100),
        ("C1002", "RC0402FR-0722KL", "±1%", "0402", 250),
        ("C1003", "RC0603FR-0710KL", "±0.5%", "0603", 0),
        ("C1004", "RC0603JR-0710KL", "±5%", "0603", 75),
    ]
    hits = []
    for lcsc, model, tolerance, package, stock in spec:
        hits.append(
            api.SearchHit(
                lcsc=lcsc,
                model=model,
                brand="YAGEO",
                package=package,
                category="Resistors",
                description=f"10kOhms {tolerance} {package} Thick Film Resistor",
                stock=stock,
                library_type="Basic",
                min_qty=1,
                reel_qty=5000,
                price=0.0012,
                datasheet="",
                attributes={
                    "Tolerance": tolerance,
                    "Package": package,
                    "Type": "Chip Resistor - Surface Mount",
                },
            )
        )
    return hits


def probe_explorer(parent, keyword: str, offline_facets: bool):
    """Build the LCSC Explorer and report on it."""
    dialog = LcscExplorerDialog(parent, parts={}, initial_keyword=keyword)
    dialog.Show()

    def inspect():
        # Every check runs inside the try: a failed assertion that escaped from
        # here would leave the dialog on screen and the main loop running, which
        # is not a test failure, it is a hang.
        try:
            capture(dialog, "wx-explorer")
            print("--- explorer geometry ---")
            dump_tree(dialog)
            dump_columns(dialog.results, "results")
            print(f"row height requested: {dialog.thumb_px}px thumb")
            print(
                f"rows={dialog.results.GetItemCount()} "
                f"retail_filled={len(dialog._retail)} "
                f"thumbs_asked={len(dialog._thumbs)} "
                f"thumbs_decoded={sum(1 for b in dialog._thumbs.values() if b)}"
            )

            if offline_facets:
                dump_facets(dialog)
            dump_panels(dialog)
            dump_cell_text(dialog)
            if offline_facets or dialog.results.GetItemCount():
                dump_inline_scroll(dialog)
                dump_detail_layout(dialog)
            check_activation(parent)
            check_photo_viewer(parent)
        except Exception:
            traceback.print_exc()
            probe_explorer.failed = True
        finally:
            dialog.Close()
            parent.Destroy()
            wx.GetApp().ExitMainLoop()

    return inspect


def dump_panels(dialog) -> None:
    """Drive the Filters / Details toggles and report what the grid gains.

    The two things worth catching here are a detail pane that will not split
    back in, and columns that fail to take up the width the closed pane frees
    — both of which look identical to "works fine" in a diff.
    """
    print("--- panel toggles ---")

    def state(label: str) -> None:
        grid = dialog.results.GetClientSize()
        # Inline, the detail panel sits inside the clipping window and is
        # positioned relative to it, so the clip is what carries the geometry.
        holder = dialog.inline_clip if dialog._inline_rows else dialog.right_panel
        detail = holder.GetRect()
        widths = {
            key: column.GetWidth() for key, column in dialog.result_columns.items()
        }
        print(
            f"  {label:<22} grid={grid.width:>5}x{grid.height:<4} "
            f"split={dialog.splitter.IsSplit()!s:<5} "
            f"layout={dialog._detail_layout:<5} "
            f"facets={dialog.facet_scroller.IsShown()!s:<5} "
            f"inline={dialog._inline_rows} "
            f"detail={holder.IsShown()!s:<5}@{detail.y}+{detail.height} "
            f"desc={widths['description']:<4} part={widths['part']:<4} "
            f"maker={widths['manufacturer']:<4} spacer={widths['spacer']}"
        )

    state("initial")
    check_fits(dialog, "initial")

    dialog._set_filters_shown(False)
    wx.Yield()
    state("filters hidden")

    dialog._set_filters_shown(True)
    wx.Yield()
    state("filters shown")

    # Selecting a row is what opens the pane, and a repeat click on the same
    # row is what closes it again. The click itself cannot be synthesised
    # here (wx.UIActionSimulator needs Accessibility permission), so the
    # toggle its handler performs is called directly.
    if dialog.results.GetItemCount():
        dialog.results.SelectRow(0)
        dialog._on_row_selected(None)
        wx.Yield()
        state("row selected")

        dialog._set_details_shown(not dialog._details_shown)
        wx.Yield()
        state("same row re-clicked")

        dialog._set_details_shown(not dialog._details_shown)
        wx.Yield()
        state("and again")

        dialog._set_detail_layout("below", persist=False)
        wx.Yield()
        state("details below")
        selected = dialog.results.GetSelectedRow()
        selected_rect = dialog.results.GetItemRect(dialog.results.RowToItem(selected))
        selected_bottom = (
            dialog.results.GetPosition().y + selected_rect.y + selected_rect.height
        )
        attachment_gap = dialog.inline_clip.GetPosition().y - selected_bottom
        print(f"  inline attachment gap: {attachment_gap}px")
        if dialog._inline_rows < 1 or not dialog.inline_clip.IsShown():
            raise AssertionError("inline detail panel did not open")
        if abs(attachment_gap) > 2:
            raise AssertionError("inline detail panel is detached from selected row")

        dialog._set_detail_layout("side", persist=False)
        wx.Yield()
        state("details beside")

    dialog._set_details_shown(False)
    dialog._set_filters_shown(False)
    wx.Yield()
    state("both hidden")
    dialog._set_filters_shown(True)

    # Column widths are now derived rather than fixed, so the stock-view
    # switch has to be re-checked: this is the path where the macOS DataView
    # used to collapse Description whenever a column was toggled.
    for index, (key, _label) in enumerate(explorer.STOCK_VIEWS):
        dialog.stock_view.SetSelection(index)
        dialog._on_stock_view_changed(None)
        wx.Yield()
        widths = {
            name: column.GetWidth() for name, column in dialog.result_columns.items()
        }
        print(
            f"  stock view {key:<7}      jlc={widths['jlc_stock']:<4} "
            f"retail={widths['retail_stock']:<4} desc={widths['description']:<4} "
            f"part={widths['part']}"
        )
        check_fits(dialog, f"stock view {key}")
    dialog.stock_view.SetSelection(0)
    dialog._on_stock_view_changed(None)


def dump_facets(dialog) -> None:
    """Feed synthetic results in, then exercise the multi-select facet controls.

    Forcing each popup open is the point: ``wx.ComboPopup.Create`` is called
    lazily on first show, so a build that cannot host one would otherwise pass
    this probe and assert in front of the user.
    """
    hits = _synthetic_hits()
    dialog._search_token += 1
    dialog._search_done(dialog._search_token, "probe", len(hits), hits)

    print("--- facets ---")
    for name, values in sorted(dialog._facets.items()):
        rendered = ", ".join(f"{value} ({count})" for value, count in values)
        print(f"  {name:<12} {rendered}")
    print(f"  rows populated: {dialog.results.GetItemCount()}")
    if dialog.results.GetItemCount():
        row_rect = dialog.results.GetItemRect(dialog.results.RowToItem(0))
        print(f"  realised row height: {row_rect.height}px")
        part_text = dialog.results.GetTextValue(0, explorer.COLUMN_INDEX["part"])
        description_text = dialog.results.GetTextValue(
            0, explorer.COLUMN_INDEX["description"]
        )
        if "…" in part_text or "…" in description_text:
            raise AssertionError("catalogue source text was pre-truncated")

    for name, control in sorted(dialog._facet_controls.items()):
        control.Popup()
        control.Dismiss()
        print(f"  popup ok: {name:<12} label={control.GetValue()!r}")

    # Tick two values of one attribute and confirm OR-within semantics.
    tolerance = dialog._facet_controls.get("Tolerance")
    if tolerance is not None:
        before = dialog.results.GetItemCount()
        tolerance._popup.set_checked_indices([0, 1])
        tolerance._on_popup_toggle()
        print(f"  after ticking 2 tolerances: label={tolerance.GetValue()!r}")
        print(f"  selected={dialog._selected_facets}")

        # A tick only *schedules* the rebuild — see FILTER_DEBOUNCE_MS — so the
        # grid is deliberately untouched until the timer fires. Both halves are
        # worth checking: rebuilding here is the stall that made the popups feel
        # dead, and never rebuilding would be a filter that does nothing.
        if dialog.results.GetItemCount() != before:
            raise AssertionError("a facet tick rebuilt the grid synchronously")
        dialog._on_filter_tick(None)  # what the timer does
        visible = dialog.results.GetItemCount()
        print(f"  rows visible once the debounce fires={visible} (expected 3)")
        if visible != 3:
            raise AssertionError(
                f"OR-within-attribute filtering left {visible} rows, expected 3"
            )


#: A real LCSC description, and the longest thing the grid has to render. The
#: catalogue renderers used to paint into a 100px box whatever their column's
#: width, which wrapped this to five lines and cut the last one mid-word — the
#: "Multilayer Ceramic Capacito" in the bug report.
SAMPLE_DESCRIPTION = (
    "10nF 50V X7R ±10% 0402 Multilayer Ceramic Capacitors MLCC - SMD/SMT"
)


def dump_cell_text(dialog) -> None:
    """Check the widest cell's text against the width it is actually given."""
    print("--- catalogue cell text ---")
    renderer = dialog.result_columns["description"].GetRenderer()
    cell = renderer.GetSize().width
    dc = wx.MemoryDC(wx.Bitmap(8, 8))
    dc.SetFont(dialog.results.GetFont())
    lines = explorer._wrapped_lines(dc, SAMPLE_DESCRIPTION, cell - 16, 5)
    print(f"  cell={cell}px lines={lines}")
    if any(line.endswith("…") for line in lines):
        raise AssertionError(f"description does not fit a {cell}px description cell")
    if " ".join(lines) != SAMPLE_DESCRIPTION:
        raise AssertionError("description lost text on the way through the wrapper")


def _long_result_set(rows: int):
    """Enough distinct hits to make the grid scroll."""
    hits = []
    template = _synthetic_hits()
    for index in range(rows):
        source = template[index % len(template)]
        hits.append(dataclasses.replace(source, lcsc=f"C{9000 + index}"))
    return hits


def dump_inline_scroll(dialog) -> None:
    """Scroll the grid out from under an open inline detail.

    The detail panel is an overlay on placeholder rows, so it only behaves like
    the tall row it stands in for if it *clips* against the grid — showing the
    half of itself that is on screen — rather than vanishing the moment it no
    longer fits whole. Native scrolling does not reliably send wx an event, so
    the tracking tick is called directly, which is what the timer does.
    """
    hits = _long_result_set(24)
    dialog._search_token += 1
    dialog._search_done(dialog._search_token, "probe", len(hits), hits)
    wx.Yield()

    print("--- inline detail while scrolling ---")
    dialog._set_detail_layout("below", persist=False)
    dialog.results.SelectRow(4)
    dialog._on_row_selected(None)
    wx.Yield()
    full = dialog._inline_rows * dialog.row_px

    def state(label: str, tick: bool = True):
        if tick:
            dialog._on_inline_tick(None)
        wx.Yield()
        clip = dialog.inline_clip.GetRect()
        panel = dialog.right_panel.GetRect()
        print(
            f"  {label:<26} clip={clip.height:>4}/{full} @{clip.y:<5} "
            f"shown={dialog.inline_clip.IsShown()!s:<5} panel_y={panel.y:<5} "
            f"panel_h={panel.height}"
        )
        return clip

    opened = state("opened on row 4")
    if not dialog.inline_clip.IsShown() or opened.height <= 0:
        raise AssertionError("inline detail did not open")

    # Nothing calls the tracker for this one — the timer has to. Bind EVT_TIMER
    # to the wrong source and the panel silently stops following its rows, which
    # is exactly what a trackpad scroll does in the real window: it moves the
    # rows without sending wx a scroll event.
    last = dialog._inline_after + dialog._inline_rows
    before = dialog.inline_clip.GetRect().y
    dialog.results.EnsureVisible(dialog.results.RowToItem(last + 1))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and dialog.inline_clip.GetRect().y == before:
        wx.Yield()
        time.sleep(0.02)
    tracked = state("timer tracked the scroll", tick=False)
    if tracked.y == before:
        raise AssertionError(
            "the inline panel did not follow its rows on the tracking timer"
        )

    partial = state("scrolled one row past")
    if dialog.inline_clip.IsShown() and partial.height >= full:
        print("  (still fully visible — grid is tall enough to hold it)")
    elif not dialog.inline_clip.IsShown():
        raise AssertionError(
            "inline detail disappeared instead of clipping to the visible rows"
        )
    elif dialog.right_panel.GetPosition().y >= 0:
        raise AssertionError("clipped inline detail did not slide under the header")

    dialog.results.EnsureVisible(dialog.results.RowToItem(len(hits) - 1))
    if state("scrolled far away").height > 0 and dialog.inline_clip.IsShown():
        raise AssertionError("inline detail is still on screen without its rows")

    # Back to the row the details belong to, not to the top of the list: rows
    # 0-3 are above the anchor, so the top of the list is a position where the
    # details are legitimately out of view.
    dialog.results.EnsureVisible(dialog.results.RowToItem(dialog._inline_after))
    back = state("scrolled back to its row")
    if not dialog.inline_clip.IsShown() or back.height <= 0:
        raise AssertionError("inline detail did not come back with its rows")

    dialog._set_detail_layout("side", persist=False)
    wx.Yield()
    if dialog.inline_clip.IsShown() or dialog._inline_rows:
        raise AssertionError("inline detail outlived the switch to the side panel")


def dump_detail_layout(dialog) -> None:
    """Report how wide each block of the detail pane ends up, in both layouts.

    The inline layout is where this goes wrong and does not look wrong in a
    diff: a control whose best size is its own text — the caveat box — claims
    that as a minimum width and starves the parameter table beside it, which
    then paints its two columns on top of each other.
    """
    print("--- detail pane blocks ---")
    for layout in ("below", "side"):
        dialog._set_detail_layout(layout, persist=False)
        if dialog.results.GetItemCount():
            dialog.results.SelectRow(0)
            dialog._on_row_selected(None)
        wx.Yield()
        panel = dialog.right_panel.GetSize()
        print(f"  {layout} — panel {panel.width}x{panel.height}")
        blocks = (
            ("symbol", dialog.symbol_preview),
            ("photo", dialog.photo_preview),
            ("jlc card", dialog.jlc_card),
            ("retail card", dialog.retail_card),
            ("caveats", dialog.warning_text),
            ("params", dialog.param_list),
        )
        # Screen coordinates, because these widgets sit at different depths:
        # the cards are children of a static box, the previews of the panel.
        origin = dialog.right_panel.GetScreenPosition()
        for name, widget in blocks:
            offset = widget.GetScreenPosition() - origin
            size = widget.GetSize()
            best = widget.GetBestSize()
            print(
                f"    {name:<12} {size.width:>4}x{size.height:<4} "
                f"@{offset.x},{offset.y} best={best.width}x{best.height}"
            )
        columns = " ".join(
            f"{column.GetTitle()}={column.GetWidth()}"
            for column in dialog.param_list.GetColumns()
        )
        print(f"    param columns: {columns}")
        for name, widget in blocks:
            offset = widget.GetScreenPosition() - origin
            size = widget.GetSize()
            if size.width < 40 or size.height < 30:
                raise AssertionError(
                    f"{layout}: {name} was squeezed to {size.width}x{size.height}"
                )
            if offset.x + size.width > panel.width + 2:
                raise AssertionError(
                    f"{layout}: {name} overflows the detail pane by "
                    f"{offset.x + size.width - panel.width}px"
                )


def check_activation(parent) -> None:
    """Double-clicking a result must assign its number and close the window.

    A trackpad produces this gesture by accident, so what it does matters: it
    used to import a library onto disk. Built as its own dialog because the
    check ends with that dialog closed.
    """
    print("--- double-click on a result ---")
    dialog = LcscExplorerDialog(parent, parts={"C6": "0.01uF 0402"})
    dialog.Show()
    hits = _synthetic_hits()
    dialog._search_token += 1
    dialog._search_done(dialog._search_token, "probe", len(hits), hits)
    wx.Yield()

    assigned = []
    parent.Bind(EVT_ASSIGN_PARTS_EVENT, lambda event: assigned.append(event.lcsc))
    dialog.results.SelectRow(1)
    dialog._on_row_activated(None)
    wx.Yield()
    closed = not bool(dialog)
    print(f"  assigned={assigned} closed={closed}")
    if assigned != ["C1002"]:
        raise AssertionError(f"expected C1002 to be assigned, got {assigned}")
    if not closed:
        raise AssertionError("the explorer stayed open after assigning")

    # With no footprints selected there is nothing to assign to, and the window
    # must stay put rather than closing on a gesture that did nothing.
    quiet = LcscExplorerDialog(parent, parts={})
    quiet.Show()
    quiet._search_token += 1
    quiet._search_done(quiet._search_token, "probe", len(hits), hits)
    wx.Yield()
    del assigned[:]
    quiet.results.SelectRow(0)
    quiet._on_row_activated(None)
    wx.Yield()
    print(f"  no references: assigned={assigned} open={bool(quiet)}")
    if assigned or not bool(quiet):
        raise AssertionError("activation with no footprints selected did something")
    quiet.Close()
    wx.Yield()


def check_photo_viewer(parent) -> None:
    """Clicking a thumbnail must open the viewer, not toggle the detail pane.

    The photo column shares its click handler with the pane toggle, so the two
    gestures are checked against each other: a click on the picture opens a
    window and leaves the pane alone, a click anywhere else still toggles.
    Network-free — the viewer is driven through its own methods, so what is
    under test is the wiring and the teardown, not LCSC's uptime.
    """
    print("--- thumbnail click ---")
    dialog = LcscExplorerDialog(parent, parts={})
    dialog.Show()
    hits = _synthetic_hits()
    dialog._search_token += 1
    dialog._search_done(dialog._search_token, "probe", len(hits), hits)
    wx.Yield()

    dialog.results.SelectRow(0)
    wx.Yield()
    pane_before = dialog._details_shown

    dialog._open_photo_viewer(hits[0])
    wx.Yield()
    viewer = dialog._photo_viewer
    print(
        f"  opened={bool(viewer)} title={viewer.GetTitle()!r} "
        f"pane_toggled={dialog._details_shown != pane_before}"
    )
    if not viewer:
        raise AssertionError("clicking a thumbnail did not open the viewer")
    if dialog._details_shown != pane_before:
        raise AssertionError("opening the viewer also toggled the detail pane")

    # A second thumbnail must retarget the one window rather than stack another.
    dialog._open_photo_viewer(hits[2])
    wx.Yield()
    same = dialog._photo_viewer is viewer
    print(
        f"  retargeted={same} lcsc={viewer.lcsc} heading={viewer.heading.GetLabel()!r}"
    )
    if not same:
        raise AssertionError("a second thumbnail opened a second window")
    if viewer.lcsc != "C1003":
        raise AssertionError(f"viewer still showing {viewer.lcsc}")

    # Stepping with no photo set yet must be inert, not an IndexError.
    viewer._step(1)
    viewer._step(-1)
    wx.Yield()

    # A late fetch landing after the window is gone is the crash this guards.
    stale_token = viewer._token
    viewer.Close()
    wx.Yield()
    viewer._photo_ready(stale_token, None)
    wx.Yield()
    print(f"  closed={not bool(dialog._photo_viewer)} survived_late_callback=True")

    dialog.Close()
    wx.Yield()


def probe_partlist(parent):
    """Report the row colour the part list assigns to each attention state."""
    model = PartListDataModel(parent.scale_factor)
    # reference, value, footprint, lcsc, type, stock, bom, pos, dnp, rot, side,
    # params, enrichment, price
    rows = [
        [
            "R1",
            "10k",
            "R_0402",
            "C25741",
            "Basic",
            "3296305",
            0,
            0,
            0,
            "0",
            "0",
            "",
            "",
            "",
        ],
        ["R2", "22k", "R_0402", "", "", "", 0, 0, 0, "0", "0", "", "", ""],
        ["H1", "MountingHole", "MH_3.2", "", "", "", 1, 0, 0, "0", "0", "", "", ""],
    ]
    for row in rows:
        model.AddEntry(row)
    model.set_standard_trigger_refs({"R1"})

    print("--- part list row colours ---")
    print(f"  standard trigger colour: {standard_trigger_colour().GetAsString()}")
    print(f"  unassigned colour:       {unassigned_colour().GetAsString()}")
    for row in model.get_all():
        attr = dv.DataViewItemAttr()
        applied = model.GetAttr(model.ObjectToItem(row), 0, attr)
        colour = attr.GetColour().GetAsString() if applied else "-"
        print(
            f"  {row[0]:<4} lcsc={row[3] or '(none)':<8} "
            f"bom={'in' if row[6] == model.bom_pos_icons[0] else 'out':<4} "
            f"highlighted={applied!s:<5} colour={colour}"
        )

    def inspect():
        parent.Destroy()
        wx.GetApp().ExitMainLoop()

    return inspect


SCHEMATIC_TEMPLATE = """(kicad_sch
\t(version 20260306)
\t(generator "eeschema")
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 10.16 20.32 0)
\t\t(property "Reference" "R1"
\t\t\t(at 12.7 17.78 0)
\t\t)
\t\t(property "Value" "100"
\t\t\t(at 12.7 20.32 0)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "00000000-0000-0000-0000-000000000001")
\t\t)
\t)
)
"""


def _lcsc_fields(path):
    """Return the LCSC field values a schematic file carries."""
    with open(path, encoding="utf-8") as f:
        return [
            line.split('"')[3]
            for line in f
            if line.strip().startswith('(property "LCSC"')
        ]


#: The three dialogs the Phase 8 parity gate needs a wx picture of and that
#: nothing else here builds. Each takes the main window plus one argument, and
#: none of them touches the network on construction.
#:
#: They are built *only* when ``--shot`` is given. This script's day job is
#: proving a dialog survives a wx layout pass, and adding three more dialogs to
#: every run would slow the check it exists for in order to serve a gate that
#: runs once a phase.
EXTRA_DIALOGS = (
    ("wx-corrections", "corrections", "CorrectionManagerDialog", "R_0402_1005Metric"),
    ("wx-mappings", "partmapper", "PartMapperManagerDialog", None),
    ("wx-part-details", "partdetails", "PartDetailsDialog", "C25741"),
)


def capture_dialogs(parent) -> None:
    """Build and screenshot the dialogs no other check here opens.

    Each is imported at call time rather than at the top of the file, because
    two of them are among the four upstream modules ``ruff format`` still wants
    to rewrite and importing them unconditionally would drag that into every
    run of this script.
    """
    if not SHOT_DIR:
        return
    import importlib

    for name, module_name, class_name, argument in EXTRA_DIALOGS:
        try:
            module = importlib.import_module(f"kicad_lcsc_suite.{module_name}")
            factory = getattr(module, class_name)
            dialog = factory(parent) if argument is None else factory(parent, argument)
            dialog.Show()
            wx.Yield()
            capture(dialog, name)
            dialog.Destroy()
            wx.Yield()
        except Exception as exc:  # noqa: BLE001 - one dialog must not stop the rest
            print(f"shot: {name} FAILED to build: {exc}")


def probe_mainwindow(project_path: str):
    """Build the main window against the standalone stubs and exercise it.

    Covers the two things that cannot be unit tested: that the dialog and the
    settings dialog survive a real wx layout pass, and that the schematic sync
    and single-window lookup behave when driven through the real window.
    """
    board_file = os.path.join(project_path, "probe.kicad_pcb")
    schematic = os.path.join(project_path, "probe.kicad_sch")
    with open(schematic, "w", encoding="utf-8") as f:
        f.write(SCHEMATIC_TEMPLATE)

    class ProbeBoard(standalone_impl.BoardStub):
        """Board stub that lives in a real (temporary) project directory."""

        def GetFileName(self):
            """Board filename."""
            return board_file

    class ProbePcbnew(standalone_impl.PcbnewStub):
        """pcbnew stub handing out the probe board."""

        def __init__(self):
            super().__init__()
            self.board = ProbeBoard()

    class ProbeKicad(standalone_impl.KicadStub):
        """KiCad stub handing out the probe pcbnew."""

        def __init__(self):
            super().__init__()
            self.pcbnew = ProbePcbnew()

    dialog = JLCPCBTools(None, kicad_provider=ProbeKicad())
    dialog.Show()
    wx.Yield()

    print("--- main window ---")
    print(f"  name={dialog.GetName()}")
    found = find_open_main_window()
    print(f"  found_by_lookup={found is dialog}")
    if found is not dialog:
        raise AssertionError("find_open_main_window() did not find the open window")

    settings = SettingsDialog(dialog)
    settings.Show()
    wx.Yield()
    capture(settings, "wx-settings")
    settings.Destroy()
    wx.Yield()
    capture_dialogs(dialog)

    print("--- schematic buttons ---")
    # Both live on the upper toolbar. On the right-hand one they were off the
    # bottom of the window: a wx.ToolBar does not scroll, so a tool past the
    # end of the space its sizer gives it simply is not there for the user.
    for tool_id, label in (
        (dialog.import_schematic_button.GetId(), "From schematic"),
        (dialog.export_schematic_button.GetId(), "To schematic"),
    ):
        tool = dialog.upper_toolbar.FindById(tool_id)
        if tool is None:
            raise AssertionError(f"{label} is not on the upper toolbar")
        print(f"  {tool.GetLabel():<16} enabled={tool.IsEnabled()}")
    for toolbar, name, axis in (
        (dialog.upper_toolbar, "upper_toolbar", "width"),
        (dialog.right_toolbar, "right_toolbar", "height"),
    ):
        needed = getattr(toolbar.GetBestSize(), axis)
        have = getattr(toolbar.GetSize(), axis)
        print(f"  {name:<16} {axis}: needs={needed} has={have}")
        if name == "upper_toolbar" and needed > have:
            raise AssertionError(
                f"{name} needs {needed - have}px more {axis} than it has — "
                "its last tools are cut off"
            )

    print("--- selection refresh ---")
    # Selecting a row refetches its details past the cache TTL, after a
    # debounce. Both halves are wx — a one-shot wx.Timer driven by a DataView
    # selection event — so this is the only place they get exercised. The
    # refresher itself is stubbed out: what is under test is whether the timer
    # fires at all, not the network call behind it.
    dialog.store.set_lcsc("R1", "C25741")
    dialog.populate_footprint_list()
    wx.Yield()
    forced = []

    def record_refresh(references=None, force=False):
        """Stand in for the refresher, recording what it was asked for."""
        forced.append((list(references or []), force))

    dialog.start_part_detail_refresh = record_refresh
    row = dialog.partlist_data_model.ObjectToItem(dialog.partlist_data_model.data[0])
    dialog.footprint_list.Select(row)
    dialog.OnFootprintSelected()
    deadline = time.time() + (SELECTION_REFRESH_DELAY_MS / 1000.0) + 2.0
    while not forced and time.time() < deadline:
        wx.Yield()
        time.sleep(0.02)
    print(f"  selected -> refresh={forced}")
    if forced != [(["R1"], True)]:
        raise AssertionError(
            f"selecting a row did not force a detail refresh for it (got {forced!r})"
        )
    del dialog.start_part_detail_refresh

    print("--- to schematic ---")
    dialog.store.set_lcsc("R1", "C25741")
    synced = dialog.sync_schematic()
    print(f"  assigned -> synced={synced} fields={_lcsc_fields(schematic)}")
    if _lcsc_fields(schematic) != ["C25741"]:
        raise AssertionError("assignment did not reach the schematic")

    dialog.store.set_lcsc("R1", "")
    dialog._schematic_cleared_refs.add("R1")
    dialog.sync_schematic()
    print(f"  removed  -> fields={_lcsc_fields(schematic)}")
    if _lcsc_fields(schematic) != [""]:
        raise AssertionError("removal did not reach the schematic")

    with open(lock_file_for(schematic), "w", encoding="utf-8") as f:
        f.write("{}")
    dialog.store.set_lcsc("R1", "C25741")
    synced = dialog.sync_schematic()
    print(f"  locked   -> synced={synced} fields={_lcsc_fields(schematic)}")
    if synced or _lcsc_fields(schematic) != [""]:
        raise AssertionError("a schematic open in the editor was written to")
    os.remove(lock_file_for(schematic))

    print("--- from schematic ---")
    # The confirmation is a modal message box, so drive the pieces around it:
    # the read, the diff and the apply are what touch the store and the model.
    dialog.sync_schematic()
    dialog.store.set_lcsc("R1", "")
    found = read_schematic([schematic])
    diff = diff_against_board(found.numbers, dialog.board_assignments())
    print(f"  read     -> {found.summary()}")
    print(f"  diff     -> added={diff.added} replaced={diff.replaced}")
    if diff.added != [("R1", "C25741")]:
        raise AssertionError("the schematic's number was not seen as an import")
    imported = dialog._apply_schematic_numbers(diff.assignments())
    wx.Yield()
    stored = dialog.store.get_part("R1")["lcsc"]
    print(f"  imported -> refs={imported} store={stored}")
    if imported != ["R1"] or stored != "C25741":
        raise AssertionError("the import did not reach the store")

    def inspect():
        try:
            capture(dialog, "wx-mainwindow")
            print("--- single window ---")
            # Closing with unexported changes puts up a modal question, which
            # nothing can answer headlessly. The point here is the teardown, so
            # close from the state the user reaches by pressing the button.
            dialog._schematic_sync_pending = False
            dialog.Close()
            wx.Yield()
            # Destroy() is deferred to idle, so the window is only really gone
            # once pending deletes have been drained.
            wx.GetApp().ProcessPendingEvents()
            wx.Yield()
            still_open = find_open_main_window()
            print(
                f"  closed -> lookup_returns_none={still_open is None} "
                f"schematic={_lcsc_fields(schematic)}"
            )
            if still_open is not None:
                raise AssertionError("a closed window is still found by the lookup")
            if _lcsc_fields(schematic) != ["C25741"]:
                raise AssertionError("closing rewrote the schematic on its own")
        except Exception:
            traceback.print_exc()
            probe_mainwindow.failed = True
        finally:
            wx.GetApp().ExitMainLoop()

    return inspect


def main() -> int:
    """Parse arguments, build the requested dialog, dump its state."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        nargs="?",
        default="explorer",
        choices=["explorer", "partlist", "mainwindow"],
        help="which dialog to build",
    )
    parser.add_argument(
        "--keyword",
        default="",
        help="seed the search box (triggers a live network search)",
    )
    parser.add_argument(
        "--offline-facets",
        action="store_true",
        help="inject synthetic results to exercise the facet controls offline",
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=600,
        help="how long to let the event loop run before inspecting",
    )
    parser.add_argument(
        "--project-path",
        default=os.getcwd(),
        help="stand-in for the board's project directory",
    )
    parser.add_argument(
        "--shot",
        metavar="DIR",
        help=(
            "also screenshot the dialog into DIR. This is how the wx originals "
            "for the Phase 8 parity gate were captured; see docs/screens/wx/."
        ),
    )
    args = parser.parse_args()
    global SHOT_DIR
    SHOT_DIR = args.shot

    app = wx.App(False)
    probe_explorer.failed = False
    probe_mainwindow.failed = False
    try:
        if args.target == "mainwindow":
            # Builds its own project directory; a stub parent would only add a
            # second top-level window for the singleton lookup to trip over.
            with tempfile.TemporaryDirectory() as project_path:
                inspect = probe_mainwindow(project_path)
                wx.CallLater(args.settle_ms, inspect)
                app.MainLoop()
        else:
            parent = StubParent(args.project_path)
            if args.target == "partlist":
                inspect = probe_partlist(parent)
            else:
                inspect = probe_explorer(parent, args.keyword, args.offline_facets)
            wx.CallLater(args.settle_ms, inspect)
            app.MainLoop()
    except Exception:
        traceback.print_exc()
        return 1

    if probe_explorer.failed or probe_mainwindow.failed:
        print(f"FAILED: {args.target} built, but a check did not hold")
        return 1
    print(f"OK: {args.target} built and torn down without wx assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
