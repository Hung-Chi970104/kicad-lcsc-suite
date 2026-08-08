"""Tests for Phase 4 — the LCSC Explorer.

What is worth protecting here is not that the window opens. It is:

* **the fixture is a faithful stand-in, and cannot reach the wire.** Phase 3's
  lesson was that a fixture is only evidence to the extent it is *less*
  permissive than the thing it stands for, and trap 4 hid in the one respect
  where the board fixture was more permissive. So these tests assert that the
  captured payloads replay through ``api.py``'s own parsers, and that a URL the
  capture does not hold answers "nobody answered" rather than opening a socket;
* **``…`` and ``?`` and ``0`` stay three different things.** Not fetched, asked
  with no answer, and confirmed none. Collapsing any pair of them shows in-stock
  parts as dead, which is the bug this fork exists to fix;
* **the two inventories stay separate.** Different warehouses, different
  columns, different cards — and the fixture's own rows disagree, so a test can
  tell a working selector from a relabelled one;
* **facets are OR within an attribute and AND across attributes.** Any other
  reading makes multi-select useless;
* **the funnel is still single.** The Explorer is a second caller of
  ``SuiteController.assign_number``, not a second write path.

Rendered offscreen, so they need no display.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from lcsc_suite import app as app_module, kicad_bridge  # noqa: E402
from lcsc_suite.config import DEFAULTS, Settings  # noqa: E402
from lcsc_suite.controller import SuiteController  # noqa: E402
from lcsc_suite.parts import PartList, open_fixture_library  # noqa: E402
from lcsc_suite.search_source import (  # noqa: E402
    EXPLORER_FIXTURE,
    FixtureSource,
    LiveSource,
    build_source,
)
from lcsc_suite.shared import lcsc_api as api  # noqa: E402
from lcsc_suite.ui.explorer.detail import library_label  # noqa: E402
from lcsc_suite.ui.explorer.facets import (  # noqa: E402
    FACET_MAX_HEIGHT,
    FACET_MIN_HEIGHT,
    FacetPanel,
)
from lcsc_suite.ui.explorer.results import (  # noqa: E402
    COLUMN_INDEX,
    INLINE_DETAIL_MIN_PX,
    INLINE_DETAIL_PX,
    PENDING,
    STOCK_ROLE,
    UNKNOWN,
    ResultsModel,
    fit_columns,
    format_count,
    inline_detail_height,
)
from lcsc_suite.ui.explorer.tasks import THUMB_FILL_LIMIT  # noqa: E402
from lcsc_suite.ui.explorer.window import ExplorerWindow  # noqa: E402

BOARD_FIXTURE = (
    Path(__file__).resolve().parent.parent / "lcsc_suite" / "fixtures" / "board.json"
)


@pytest.fixture(scope="session", autouse=True)
def application():
    """Build the QApplication the widgets live in."""
    return app_module.build_application(theme_mode="light", offscreen=True)


@pytest.fixture(scope="session")
def source():
    """Return the captured result set. Session-scoped: it is a megabyte."""
    return FixtureSource()


@pytest.fixture
def hits(source):
    """Return the captured search, parsed by ``api.py``'s own parser."""
    _total, parsed = source.hits()
    return parsed


def settle(milliseconds: int = 400) -> None:
    """Run the event loop so queued signals and layout land."""
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()
    QApplication.processEvents()


def probe_settings(tmp_path) -> Settings:
    """Shipped defaults, written somewhere throwaway."""
    settings = Settings(path=str(tmp_path / "settings.json"))
    settings.values.clear()
    settings.values.update({key: dict(value) for key, value in DEFAULTS.items()})
    return settings


def make_window(tmp_path, source, **kwargs) -> ExplorerWindow:
    """Build an Explorer over the fixture and run its search to completion."""
    window = ExplorerWindow(
        None,
        source,
        settings=probe_settings(tmp_path),
        keyword=source.keyword,
        **kwargs,
    )
    window.show()
    window.start_search()
    settle(700)
    return window


# ---------------------------------------------------------------------------
# The fixture itself
# ---------------------------------------------------------------------------


def test_the_fixture_replays_through_the_real_parser(source, hits):
    """The capture is raw payloads, not stored ``SearchHit`` objects.

    If it were the latter, a change to how ``api.py`` reads a field would leave
    the fixture agreeing with the *old* reading forever.
    """
    total, _ = source.hits()
    assert total > len(hits) > 0
    first = hits[0]
    assert isinstance(first, api.SearchHit)
    assert first.lcsc.startswith("C")
    assert first.model and first.brand and first.package
    assert first.attributes, "the facet panel has nothing to build from"


def test_the_fixture_cannot_reach_the_network(source):
    """A URL the capture does not hold takes the "nobody answered" branch."""
    assert source.image("https://example.invalid/never-captured.jpg") is None
    assert api.lcsc_retail_detail("C000000000") == {}
    assert api.jlc_assembly_detail("C000000000") == {}


def test_an_uncaptured_host_is_reported_blocked(source):
    """The transport block is the host breaker, which refuses everything."""
    del source  # installed by the fixture's construction
    assert api.host_blocked("https://example.invalid/") is True


def test_retail_is_reachable_even_though_every_host_is_blocked(source):
    """The two questions are different, and conflating them emptied a column.

    ``api.retail_unreachable()`` asks the breaker, and the breaker here refuses
    everything — so the live implementation reported both retail sources down,
    the Explorer skipped its fill, and the whole LCSC retail column rendered
    ``…``. Caught by looking at the screenshot.
    """
    assert source.retail_unreachable() is False


def test_the_two_inventories_disagree_in_the_fixture(source, hits):
    """Otherwise the Inventory selector looks like it does nothing."""
    differ = 0
    for hit in hits:
        retail = source.retail_stock(hit.lcsc)
        if retail is not None and hit.stock is not None and retail != hit.stock:
            differ += 1
    assert differ > len(hits) // 2, "the selector would look inert"


def test_the_fixture_holds_a_confirmed_zero(source, hits):
    """A part with none in stock, distinct from one nobody answered about."""
    figures = [source.retail_stock(hit.lcsc) for hit in hits]
    assert 0 in figures, "the '0' cell state never appears"
    assert all(value is not None for value in figures), (
        "this capture answered for every row; a None here would be a '?'"
    )


def test_refresh_keeps_the_fixture(source):
    """Refresh re-queries; against a fixture that means the same answer back."""
    source.clear_cache()
    total, hits = source.hits()
    assert total and hits


def test_an_uncaptured_keyword_degrades_to_an_empty_grid(tmp_path, source):
    """The fixture answers one keyword, and says so rather than pretending.

    Worth pinning because it is the shape of every reachability failure this
    window has: a search nobody answered renders as no results, not as an error
    dialog and not as a stale grid from the previous keyword.
    """
    window = ExplorerWindow(None, source, settings=probe_settings(tmp_path))
    window.keyword.setText("something the capture never asked about")
    window.start_search()
    settle(500)
    assert window.model.rowCount() == 0
    assert "No parts found" in window.status.text()
    window.close()


def test_the_search_never_falls_back_to_the_vendored_client(source, monkeypatch):
    """The one hole the offline guarantee had, pinned shut.

    ``api.jlc_search`` falls back to the vendored ``easyeda2kicad`` client when
    the direct POST yields nothing, and that client has its own transport — it
    never passes the host breaker. So an uncaptured keyword used to leave the
    machine from a source whose whole contract is that it cannot.
    """

    def explode(*args, **kwargs):
        raise AssertionError("the vendored client was reached")

    monkeypatch.setattr(api, "_jlc_search_vendored", explode)
    assert source.search("a keyword the capture does not hold") == (0, [])


def test_build_source_defaults_to_live():
    """Forgetting the switch must not hand a user somebody else's capacitors."""
    assert isinstance(build_source(), LiveSource)
    assert not isinstance(build_source(), FixtureSource)
    assert isinstance(
        build_source(offline=True, fixture=EXPLORER_FIXTURE), FixtureSource
    )


# ---------------------------------------------------------------------------
# The model's three stock spellings
# ---------------------------------------------------------------------------


def test_not_fetched_reads_as_pending_not_as_zero(hits):
    """An unasked retail cell is ``…``."""
    model = ResultsModel()
    model.set_hits(hits[:3])
    model.set_show_retail(True)
    index = model.index(0, COLUMN_INDEX["retail_stock"])
    assert index.data() == PENDING
    assert model.asked_retail(hits[0].lcsc) is False


def test_asked_with_no_answer_reads_as_unknown(hits):
    """``None`` recorded against a part is ``?`` — nobody answered."""
    model = ResultsModel()
    model.set_hits(hits[:3])
    model.set_show_retail(True)
    model.set_retail(hits[0].lcsc, None)
    index = model.index(0, COLUMN_INDEX["retail_stock"])
    assert index.data() == UNKNOWN
    assert model.asked_retail(hits[0].lcsc) is True
    assert model.known_retail(hits[0].lcsc) is None


def test_a_confirmed_none_reads_as_zero(hits):
    """``0`` recorded against a part is ``0`` — a source said none."""
    model = ResultsModel()
    model.set_hits(hits[:3])
    model.set_show_retail(True)
    model.set_retail(hits[0].lcsc, 0)
    assert model.index(0, COLUMN_INDEX["retail_stock"]).data() == "0"


def test_the_stock_role_carries_the_number_not_the_text(hits):
    """The delegate colours from a number, so it never parses text back."""
    model = ResultsModel()
    model.set_hits(hits[:1])
    index = model.index(0, COLUMN_INDEX["jlc_stock"])
    assert index.data(STOCK_ROLE) == hits[0].stock
    assert index.data() == f"{hits[0].stock:,}"


def test_format_count_keeps_none_apart_from_zero():
    """The one formatting rule the whole module rests on."""
    assert format_count(None) == UNKNOWN
    assert format_count(0) == "0"
    assert format_count(1234567) == "1,234,567"


def test_the_retail_column_is_blank_when_it_is_not_on_show(hits):
    """A hidden inventory's column says nothing rather than ``…``."""
    model = ResultsModel()
    model.set_hits(hits[:2])
    model.set_show_retail(False)
    assert model.index(0, COLUMN_INDEX["retail_stock"]).data() == ""


# ---------------------------------------------------------------------------
# The inline detail row
# ---------------------------------------------------------------------------


def test_the_inline_row_is_a_real_row_that_holds_no_hit(hits):
    """Prove the inline layout is a spanned model row, not an overlay."""
    model = ResultsModel()
    model.set_hits(hits[:5])
    assert model.rowCount() == 5
    model.set_inline_row(1)
    assert model.rowCount() == 6
    assert model.inline_row() == 2
    assert model.hit_at(2) is None
    # The rows either side still resolve to the parts they showed before.
    assert model.hit_at(1) is hits[1]
    assert model.hit_at(3) is hits[2]


def test_the_inline_row_is_not_selectable(hits):
    """It is a detail pane, not a result."""
    model = ResultsModel()
    model.set_hits(hits[:3])
    model.set_inline_row(0)
    flags = model.flags(model.index(1, 0))
    assert not (flags & Qt.ItemFlag.ItemIsSelectable)


def test_removing_the_inline_row_restores_the_row_count(hits):
    """Switching layouts must not leave a phantom row behind."""
    model = ResultsModel()
    model.set_hits(hits[:4])
    model.set_inline_row(2)
    model.clear_inline_row()
    assert model.rowCount() == 4
    assert model.inline_row() == -1
    assert model.hit_at(3) is hits[3]


def test_row_of_accounts_for_the_inline_row(hits):
    """A retail figure has to land on the right row after one is inserted."""
    model = ResultsModel()
    model.set_hits(hits[:4])
    model.set_inline_row(0)
    assert model.row_of(hits[0].lcsc) == 0
    assert model.row_of(hits[1].lcsc) == 2


def test_the_inline_pane_never_fills_the_whole_viewport():
    """An expanded row that leaves no results around it is a page."""
    assert inline_detail_height(2000) == INLINE_DETAIL_PX
    assert inline_detail_height(100) == INLINE_DETAIL_MIN_PX
    assert inline_detail_height(500) == int(500 * 0.62)


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------


def test_facets_are_built_only_from_discriminating_attributes(hits):
    """A one-value attribute is not a filter, it is a fact about the search."""
    facets = api.build_facets(hits)
    assert facets
    assert all(len(values) >= 2 for values in facets.values())


def test_ticking_two_values_in_one_attribute_widens(hits):
    """OR within an attribute."""
    facets = api.build_facets(hits)
    name = "Tolerance"
    values = [value for value, _count in facets[name]]
    one = api.filter_hits(hits, {name: {values[0]}})
    two = api.filter_hits(hits, {name: {values[0], values[1]}})
    assert len(two) > len(one)


def test_adding_a_second_attribute_narrows(hits):
    """AND across attributes."""
    facets = api.build_facets(hits)
    tolerance = [value for value, _count in facets["Tolerance"]][0]
    voltage = [value for value, _count in facets["Voltage Rating"]][0]
    one = api.filter_hits(hits, {"Tolerance": {tolerance}})
    two = api.filter_hits(hits, {"Tolerance": {tolerance}, "Voltage Rating": {voltage}})
    assert len(two) <= len(one)


def test_the_facet_panel_collects_ticks_and_clears_them(hits):
    """The panel reports selections; ``api.filter_hits`` applies them."""
    panel = FacetPanel()
    panel.set_facets(api.build_facets(hits))
    control = panel.controls()["Tolerance"]
    values = [value for value, _count in control._values][:2]
    control.set_selected(values)
    control._on_toggled(True)
    assert panel.selected() == {"Tolerance": set(values)}
    panel.clear()
    assert panel.selected() == {}


def test_the_facet_panel_grows_for_the_attributes_it_has():
    """A fixed height had to suit the worst case and so suited no other one.

    At 74px a nine-attribute capacitor search showed two rows of five and hid
    the rest behind a scrollbar, which is the complaint that opened Phase 6.
    Now it counts its rows — bounded at both ends, because the panel competes
    with a result grid whose rows are 140px tall.
    """
    panel = FacetPanel()

    def height_for(count: int) -> int:
        panel.set_facets({f"Attribute {i}": [("a", 1), ("b", 2)] for i in range(count)})
        return panel._scroller.height()

    two, four, nine, twenty = (height_for(n) for n in (2, 4, 9, 20))
    assert two == FACET_MIN_HEIGHT + 1 or two < four, "one row is not the tallest"
    assert four > two, "four attributes need a second row and did not get one"
    assert nine > four, "nine attributes must be taller than four"
    assert nine == twenty == FACET_MAX_HEIGHT, "the cap is not holding"


def test_an_empty_facet_panel_does_not_reserve_a_row():
    """A search with no parametric data should cost the grid nothing."""
    panel = FacetPanel()
    panel.set_facets({})
    assert panel._scroller.height() == FACET_MIN_HEIGHT


def test_a_facet_button_summarises_rather_than_listing_everything(hits):
    """Five ticked values elide into an unreadable run; a count does not."""
    panel = FacetPanel()
    panel.set_facets(api.build_facets(hits))
    control = panel.controls()["Voltage Rating"]
    assert control.text() == "Any"
    every = [value for value, _count in control._values]
    control.set_selected(every[:2])
    assert "," in control.text()
    control.set_selected(every)
    assert control.text() == f"{len(every)} selected"


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def test_a_search_fills_the_grid_and_the_facets(tmp_path, source):
    """The whole first stage, end to end."""
    window = make_window(tmp_path, source)
    assert window.model.rowCount() == 100
    assert window.facets.controls()
    assert "18327 parts match" in window.status.text()
    window.close()


def test_the_inventory_selector_swaps_the_columns(tmp_path, source):
    """One collapses, the other appears — and neither is removed."""
    window = make_window(tmp_path, source)
    jlc = COLUMN_INDEX["jlc_stock"]
    retail = COLUMN_INDEX["retail_stock"]
    assert not window.results.isColumnHidden(jlc)
    assert window.results.isColumnHidden(retail)
    window.inventory.setCurrentIndex(1)
    settle(300)
    assert window.results.isColumnHidden(jlc)
    assert not window.results.isColumnHidden(retail)
    assert window.in_stock_only.text() == "In retail stock"
    window.close()


def test_no_retail_request_is_made_in_the_jlc_view(tmp_path, source):
    """Pick JLC assembly and the window issues no per-part lookups at all."""
    window = make_window(tmp_path, source)
    assert window.inventory_view() == "jlc"
    assert window.model.retail() == {}
    window.close()


def test_the_retail_view_fills_the_column(tmp_path, source):
    """And the figures are the other warehouse's, not the search response's."""
    window = make_window(tmp_path, source)
    window.inventory.setCurrentIndex(1)
    settle(1200)
    filled = window.model.retail()
    assert len(filled) >= 50
    hits = {hit.lcsc: hit.stock for hit in window.model.hits()}
    assert any(filled[code] != hits[code] for code in filled if code in hits)
    window.close()


def test_sorting_by_retail_uses_the_fetched_figures(tmp_path, source):
    """The one ordering that cannot come from the search response alone."""
    window = make_window(tmp_path, source)
    window.inventory.setCurrentIndex(1)
    settle(1200)
    window.sort_mode.setCurrentIndex(2)
    window.apply_filters()
    settle(300)
    ordered = [window.model.known_retail(hit.lcsc) or 0 for hit in window.model.hits()]
    assert ordered == sorted(ordered, reverse=True)
    window.close()


def test_sorting_by_price_puts_unpriced_parts_last(tmp_path, source):
    """A part we have no figure for must never displace one we do."""
    window = make_window(tmp_path, source)
    window.sort_mode.setCurrentIndex(3)
    window.apply_filters()
    settle(300)
    prices = [hit.price for hit in window.model.hits()]
    known = [p for p in prices if p is not None]
    assert known == sorted(known)
    assert prices[: len(known)] == known
    window.close()


def test_in_stock_only_keeps_unfetched_retail_rows(tmp_path, source):
    """Hiding rows we have not looked at yet would shrink the set as it filled."""
    window = make_window(tmp_path, source)
    window.inventory.setCurrentIndex(1)
    window.model.forget_fetched()
    window.in_stock_only.setChecked(True)
    window.apply_filters()
    assert window.model.rowCount() == 100
    window.close()


def test_the_facet_filter_narrows_the_grid(tmp_path, source):
    """The panel and the grid, wired together."""
    window = make_window(tmp_path, source)
    before = window.model.rowCount()
    control = window.facets.controls()["Voltage Rating"]
    chosen = [value for value, _count in control._values][:1]
    control.set_selected(chosen)
    control._on_toggled(True)
    window.apply_filters()
    settle(300)
    assert 0 < window.model.rowCount() < before
    window.close()


def test_thumbnails_are_decoded_and_bounded(tmp_path, source):
    """Bounded in breadth, and a decoded pixmap lands in the model."""
    window = make_window(tmp_path, source)
    settle(1500)
    decoded = [
        hit.lcsc for hit in window.model.hits() if window.model.has_thumbnail(hit.lcsc)
    ]
    assert decoded, "no thumbnail arrived at all"
    assert len(decoded) <= THUMB_FILL_LIMIT
    window.close()


def test_selecting_a_row_opens_the_detail_pane_and_fills_it(tmp_path, source):
    """Report, previews and photo, all three."""
    window = make_window(tmp_path, source)
    window.select_row(0)
    settle(1200)
    hit = window.model.hits()[0]
    assert hit.lcsc in window.detail.heading.text()
    assert window.detail.jlc_card.value() == hit.stock
    assert window.detail.parameters.rowCount() > 0
    assert window.detail.warnings.toPlainText()
    window.close()


def test_the_detail_card_names_the_library_type_for_people():
    """The report carries the wire spelling; the card once read "expand part"."""
    assert library_label("expand") == "Extended"
    assert library_label("base") == "Basic"
    assert library_label("Extended") == "Extended"
    assert library_label(None) == ""


def test_switching_to_inline_moves_the_pane_into_the_grid(tmp_path, source):
    """And switching back takes it out again, leaving no phantom row."""
    window = make_window(tmp_path, source)
    window.select_row(0)
    settle(500)
    assert window.model.inline_row() == -1

    window.detail_layout_choice.setCurrentIndex(1)
    settle(400)
    assert window.model.inline_row() >= 0

    window.detail_layout_choice.setCurrentIndex(0)
    settle(400)
    assert window.model.inline_row() == -1
    assert window.model.rowCount() == 100
    window.close()


def test_the_columns_fit_the_grid_at_every_width(tmp_path, source):
    """No horizontal scrollbar, which is what the wx squeeze machinery was for."""
    window = make_window(tmp_path, source)
    for width in (1470, 1100, 900):
        window.resize(width, 831)
        settle(200)
        fit_columns(window.results)
        visible = sum(
            window.results.columnWidth(index)
            for index in range(window.model.columnCount())
            if not window.results.isColumnHidden(index)
        )
        assert visible <= window.results.viewport().width() + 4, (
            f"columns overflow at {width}px"
        )
    window.close()


def test_the_photo_viewer_retargets_rather_than_stacking(tmp_path, source):
    """Clicking down a column of thumbnails moves one window."""
    window = make_window(tmp_path, source)
    hits = window.model.hits()
    window.open_photo_viewer(hits[0])
    first = window._photo_viewer
    settle(400)
    assert first is not None
    assert first.lcsc() == hits[0].lcsc

    window.open_photo_viewer(hits[1])
    settle(400)
    assert window._photo_viewer is first, "a second viewer was opened"
    assert first.lcsc() == hits[1].lcsc
    window.close()


def test_a_late_photo_does_not_overwrite_a_retargeted_viewer(tmp_path, source):
    """The token, which auto-disconnection does not replace."""
    window = make_window(tmp_path, source)
    hits = window.model.hits()
    window.open_photo_viewer(hits[0])
    viewer = window._photo_viewer
    window.open_photo_viewer(hits[1])
    stale_token = viewer._token - 1
    viewer._on_loaded(stale_token, hits[0].lcsc, b"not-an-image")
    assert viewer.lcsc() == hits[1].lcsc
    window.close()


def test_the_photo_falls_back_to_jlc_when_lcsc_holds_the_url(tmp_path, source):
    """LCSC 403s whole networks, taking its image CDN with it.

    The wx version tried only ``report.images`` — LCSC's own CDN — so a blocked
    network meant no picture even with a perfectly good JLC file id already in
    hand. §4 says photos come from JLC for exactly this reason.
    """
    del tmp_path
    _total, hits = source.hits()
    hit = hits[0]
    report = source.stock_report(hit.lcsc)
    urls = ExplorerWindow._photo_urls(hit, report)
    assert hit.photo_url in urls
    assert urls.index(hit.photo_url) >= len(report.images)


# ---------------------------------------------------------------------------
# The controller: still one funnel
# ---------------------------------------------------------------------------


def _controller(tmp_path, source):
    """Build a real controller over the fixture board and the fixture source."""
    with open(BOARD_FIXTURE, encoding="utf-8") as handle:
        board = kicad_bridge.FixtureBoard.from_dict(copy.deepcopy(json.load(handle)))
    board.relocate(str(tmp_path / "project"))
    settings = probe_settings(tmp_path)
    parts = PartList(board, settings=settings)
    parts.library = open_fixture_library(parts.owner, str(tmp_path / "data"))
    return SuiteController(board, parts, settings=settings, source=source)


def test_the_explorer_assigns_through_the_controllers_funnel(tmp_path, source):
    """Not a second write path — the same ``assign_number`` Phase 3 built."""
    controller = _controller(tmp_path, source)
    explorer = controller.build_explorer(["G1"], keyword=source.keyword)
    explorer.start_search()
    settle(700)
    explorer.select_row(0)
    settle(500)

    calls = []
    controller.assign_number = lambda refs, number, stock=None: calls.append(
        (list(refs), number, stock)
    )
    explorer.assign_requested.disconnect()
    explorer.assign_requested.connect(
        lambda number, stock: controller.assign_number(
            explorer.references, number, stock=stock
        )
    )
    explorer._on_assign()
    hit = explorer.model.hits()[0]
    assert calls == [(["G1"], hit.lcsc, hit.stock)]
    explorer.close()
    controller.window.close()


def test_the_explorer_write_reaches_the_board_and_the_database(tmp_path, source):
    """The funnel end to end, asserted by re-reading the board."""
    controller = _controller(tmp_path, source)
    explorer = controller.build_explorer(["G1"], keyword=source.keyword)
    explorer.start_search()
    settle(700)
    explorer.select_row(0)
    settle(500)
    hit = explorer.model.hits()[0]
    explorer._on_assign()

    written = {
        view.reference: view.lcsc
        for view in controller.board.footprints(refresh=True)
        if view.reference == "G1"
    }
    assert written == {"G1": hit.lcsc}
    rows = {row.reference: row for row in controller.parts.rows()}
    assert rows["G1"].lcsc == hit.lcsc
    explorer.close()
    controller.window.close()


def test_an_unknown_stock_figure_is_not_written_as_zero(tmp_path, source):
    """``None`` means the search reported nothing, and blank is how that reads."""
    controller = _controller(tmp_path, source)
    explorer = controller.build_explorer(["G1"], keyword=source.keyword)
    explorer.start_search()
    settle(700)
    explorer.select_row(0)
    settle(400)

    seen = []
    explorer.assign_requested.disconnect()
    explorer.assign_requested.connect(lambda number, stock: seen.append(stock))
    hit = explorer.model.hits()[0]
    hit.stock = None
    explorer._on_assign()
    assert seen == [None]
    explorer.close()
    controller.window.close()


def test_opening_the_explorer_twice_retargets_one_window(tmp_path, source):
    """Two would each hold a search, two fills and a photo window."""
    controller = _controller(tmp_path, source)
    controller.window.select_references(["G1"])
    controller.open_explorer()
    first = controller.explorer
    assert first is not None
    assert first.references == ["G1"]

    controller.window.select_references(["G2"])
    controller.open_explorer()
    assert controller.explorer is first, "a second Explorer was opened"
    assert first.references == ["G2"]
    first.close()
    controller.window.close()


def test_the_keyword_is_seeded_from_a_uniform_selection(tmp_path, source):
    """A mixed selection seeds nothing rather than picking one arbitrarily."""
    controller = _controller(tmp_path, source)
    views = {view.reference: view for view in controller.board.footprints()}
    # Same value *and* same package: "100nF 0402" and "100nF 0603" are two
    # different searches, so agreeing on the value alone is not agreement.
    keywords: dict[str, list[str]] = {}
    for reference in views:
        keywords.setdefault(controller.search_keyword([reference]), []).append(
            reference
        )
    uniform = next(refs for key, refs in keywords.items() if key and len(refs) >= 2)
    explorer = controller.build_explorer(uniform[:2])
    assert explorer.keyword.text() == controller.search_keyword(uniform[:1])
    explorer.close()
    mixed = controller.build_explorer(["G1", "R1"])
    assert mixed.keyword.text() == ""
    mixed.close()
    controller.window.close()


def test_the_keyword_carries_the_package_not_just_the_value(tmp_path, source):
    """Seed with the package too: `1uF` is a catalogue, `1uF 0805` is a search.

    The wx plugin's ``select_part`` rule. A value on its own matches fifteen
    thousand parts, which is a result list nobody scrolls.
    """
    controller = _controller(tmp_path, source)
    views = {view.reference: view for view in controller.board.footprints()}
    capacitor = next(
        view
        for view in views.values()
        if view.reference.startswith("C") and "Metric" in view.footprint
    )
    keyword = controller.search_keyword([capacitor.reference])
    assert keyword.startswith(capacitor.value)
    assert keyword.split()[-1].isdigit(), f"no package token in {keyword!r}"
    controller.window.close()


def test_a_resistor_value_is_seeded_with_the_ohm_sign(tmp_path, source):
    """390R, 390r and 390o are all how a schematic spells 390Ω."""
    controller = _controller(tmp_path, source)

    class _View:
        reference = "R99"
        footprint = "R_0402_1005Metric"
        value = "390R"

    assert controller._keyword_for(_View()) == "390Ω 0402"
    _View.value = "390"
    assert controller._keyword_for(_View()) == "390Ω 0402"
    _View.reference = "C99"
    assert controller._keyword_for(_View()) == "390 0402", "only resistors get Ω"
    controller.window.close()


def test_double_clicking_a_part_searches_the_explorer_for_it(tmp_path, source):
    """The complaint that opened Phase 6: nobody memorises LCSC numbers.

    A double-click used to open a text field asking for a number. It opens the
    catalogue with the row's own value and package already searched.
    """
    controller = _controller(tmp_path, source)
    reference = next(
        view.reference
        for view in controller.board.footprints()
        if controller.search_keyword([view.reference])
    )
    expected = controller.search_keyword([reference])

    controller.window.select_references([reference])
    controller.window.part_activated.emit([reference])

    explorer = controller.explorer
    assert explorer is not None, "the double-click opened no Explorer"
    assert explorer.keyword.text() == expected
    # Not equality: "Auto-select alike" is on by default, so clicking one
    # capacitor targets every identical one — which is the point of it.
    assert reference in explorer.references
    explorer.close()
    controller.window.close()


def test_the_toolbar_icon_does_not_replace_a_search_already_on_screen(tmp_path, source):
    """Keep the two asks apart: open the catalogue, versus search for a row."""
    controller = _controller(tmp_path, source)
    controller.open_explorer()
    explorer = controller.explorer
    explorer.keyword.setText("SS34")

    controller.window.select_references(["C1"])
    controller.open_explorer()  # the toolbar icon: no search argument
    assert explorer.keyword.text() == "SS34"

    controller.open_explorer(search=True)  # the double-click
    assert explorer.keyword.text() == controller.search_keyword(["C1"])
    explorer.close()
    controller.window.close()


def test_a_double_click_with_no_footprints_says_so_instead_of_assigning(
    tmp_path, source
):
    """The gesture a trackpad produces by accident must not fail silently."""
    window = make_window(tmp_path, source, references=[])
    seen = []
    window.assign_requested.connect(lambda *args: seen.append(args))
    window._on_row_activated(window.model.index(0, 1))
    assert seen == []
    assert "no footprint selected" in window.status.text()
    window.close()
