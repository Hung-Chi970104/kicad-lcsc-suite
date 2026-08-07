"""Tests for the part-list model and the board/database reconciler.

The three things worth protecting here are semantic, not cosmetic:

* ``?`` (nobody answered) never renders as ``0`` (confirmed none), in the cell
  or in the sort. Conflating them shows in-stock parts as dead;
* red means "in the BOM with no LCSC number" and amber means "costs more to
  assemble"; a part *excluded* from the BOM is never marked at all;
* a BOM/POS toggle reaches the board first and the project database second, so a
  write the board refused cannot leave the two disagreeing.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

from PySide6.QtCore import Qt  # noqa: E402

from lcsc_suite import (
    app as app_module,  # noqa: E402
    kicad_bridge,  # noqa: E402
)
from lcsc_suite.config import Settings  # noqa: E402
from lcsc_suite.parts import (  # noqa: E402
    PartList,
    _match_terms,
    _StoreOwner,
    board_part_records,
    open_fixture_library,
    open_library,
)
from lcsc_suite.ui import theme  # noqa: E402
from lcsc_suite.ui.delegates import MatchHighlightDelegate  # noqa: E402
from lcsc_suite.ui.models.part_table import (  # noqa: E402
    BOM,
    FOOTPRINT,
    LCSC,
    MATCH_TERMS_ROLE,
    PARAMS,
    POS,
    REF,
    SORT_ROLE,
    STOCK,
    TYPE,
    VALUE,
    PartRow,
    PartTableModel,
)

FIXTURE = (
    Path(__file__).resolve().parent.parent / "lcsc_suite" / "fixtures" / "board.json"
)


@pytest.fixture(scope="session", autouse=True)
def application():
    """Build the QApplication the colours are resolved against."""
    return app_module.build_application(theme_mode="light", offscreen=True)


@pytest.fixture
def board(tmp_path):
    """Return the fixture board, with a writable project directory."""
    with open(FIXTURE, encoding="utf-8") as handle:
        result = kicad_bridge.FixtureBoard.from_dict(copy.deepcopy(json.load(handle)))
    result.relocate(str(tmp_path))
    return result


@pytest.fixture
def part_list(board, tmp_path):
    """Return a reconciler over the fixture board."""
    settings = Settings(path=str(tmp_path / "settings.json"))
    result = PartList(board, settings=settings)
    result.refresh_from_board()
    return result


def _cell(model, row, column, role=Qt.ItemDataRole.DisplayRole):
    return model.data(model.index(row, column), role)


def _highlighted_row(reference, value, footprint, params):
    """Build a row the way ``PartList.rows()`` does, terms and all.

    The model carries the terms; it does not derive them. Keeping that split is
    why this helper exists rather than a ``PartRow`` that expands its own value.
    """
    row = PartRow(reference, value, footprint, params=params)
    row.match_terms = _match_terms(row)
    return row


# ---------------------------------------------------------------------------
# "?" is not "0"
# ---------------------------------------------------------------------------


def test_unknown_stock_shows_a_question_mark():
    """Nobody answered — which is not the same as no stock."""
    model = PartTableModel([PartRow("C1", "10uF", "C_0402", lcsc="C1525", stock=None)])
    assert _cell(model, 0, STOCK) == "?"


def test_confirmed_zero_shows_zero():
    """A source said there is none. That is a different fact."""
    model = PartTableModel([PartRow("C1", "10uF", "C_0402", lcsc="C1525", stock=0)])
    assert _cell(model, 0, STOCK) == "0"


def test_an_unassigned_part_has_no_stock_cell_at_all():
    """There is no part to have stock *of* yet, so "?" would read as a failure."""
    model = PartTableModel([PartRow("B1", "Mounting hole", "MountingHole")])
    assert _cell(model, 0, STOCK) == ""


def test_stock_sorts_numerically_with_unknown_below_zero():
    """A string sort puts "?" and "1,000" in nonsense places."""
    model = PartTableModel(
        [
            PartRow("C1", "", "", lcsc="C1", stock=None),
            PartRow("C2", "", "", lcsc="C2", stock=0),
            PartRow("C3", "", "", lcsc="C3", stock=1000),
        ]
    )
    keys = [_cell(model, row, STOCK, SORT_ROLE) for row in range(3)]
    # "we do not know" is less informative than "there is none", so it sorts
    # below it rather than between the real figures.
    assert keys == [-1, 0, 1000]


def test_large_stock_figures_are_thousands_separated():
    """Six digits of unbroken stock are unreadable at a glance."""
    model = PartTableModel([PartRow("C1", "", "", lcsc="C1", stock=1234567)])
    assert _cell(model, 0, STOCK) == "1,234,567"


def test_unknown_stock_says_why_in_its_tooltip():
    """A "?" with no explanation gets read as a dead part."""
    model = PartTableModel([PartRow("C1", "", "", lcsc="C1", stock=None)])
    tooltip = _cell(model, 0, STOCK, Qt.ItemDataRole.ToolTipRole)
    assert "not the same as no stock" in tooltip


# ---------------------------------------------------------------------------
# Row colouring
# ---------------------------------------------------------------------------


def test_a_bom_part_with_no_number_is_marked():
    """The one actionable failure the list can show."""
    model = PartTableModel([PartRow("U1", "STM32", "LQFP48")])
    assert _cell(model, 0, REF, Qt.ItemDataRole.ForegroundRole) == theme.colour("bad")
    assert _cell(model, 0, REF, Qt.ItemDataRole.FontRole).bold()


def test_a_part_excluded_from_the_bom_is_never_marked():
    """Mounting holes, fiducials and test points are fine without a number."""
    model = PartTableModel([PartRow("B1", "M3", "MountingHole", exclude_from_bom=True)])
    assert _cell(model, 0, REF, Qt.ItemDataRole.ForegroundRole) is None


def test_a_standard_mode_trigger_is_amber_not_red():
    """Advisory, not a failure.

    Nothing is broken about a Standard-mode part; it just costs more to
    assemble. These two shared a red once, which made a pricing note
    indistinguishable from a part JLC cannot place.
    """
    model = PartTableModel([PartRow("R1", "1K", "R_0402", lcsc="C1")])
    model.set_standard_trigger_refs({"R1"})

    colour = _cell(model, 0, REF, Qt.ItemDataRole.ForegroundRole)
    assert colour == theme.colour("standard")
    assert colour != theme.colour("bad")


def test_the_standard_advisory_can_be_turned_off():
    """It is a Settings toggle, so the model has to honour it."""
    model = PartTableModel([PartRow("R1", "1K", "R_0402", lcsc="C1")])
    model.set_standard_trigger_refs({"R1"})
    model.set_standard_trigger_highlighting_enabled(False)
    assert _cell(model, 0, REF, Qt.ItemDataRole.ForegroundRole) is None


def test_a_missing_number_outranks_the_pricing_advisory():
    """A part JLC cannot place matters more than one that costs extra."""
    model = PartTableModel([PartRow("U1", "STM32", "LQFP48")])
    model.set_standard_trigger_refs({"U1"})
    assert _cell(model, 0, REF, Qt.ItemDataRole.ForegroundRole) == theme.colour("bad")


# ---------------------------------------------------------------------------
# Columns and rows
# ---------------------------------------------------------------------------


def test_bom_and_pos_render_as_a_tick_or_nothing(part_list):
    """As the wx icon columns did."""
    model = PartTableModel(part_list.rows())
    row = model.row_for("B1")
    assert _cell(model, row, BOM) == ""
    row = model.row_for("C1")
    assert _cell(model, row, BOM) == "✓"


def test_references_sort_naturally():
    """R2 before R10, as helpers.natural_sort_collation has it."""
    model = PartTableModel(
        [PartRow("R10", "", ""), PartRow("R2", "", ""), PartRow("R1", "", "")]
    )
    keys = [_cell(model, row, REF, SORT_ROLE) for row in range(3)]
    assert sorted(keys) == [["r", 1, ""], ["r", 2, ""], ["r", 10, ""]]


def test_set_part_details_fills_the_three_api_columns():
    """Type / JLC Stock / LCSC Params arrive later than the row does."""
    model = PartTableModel([PartRow("C1", "10uF", "C_0402", lcsc="C1525")])
    assert model.set_part_details("C1", "Basic", 5000, "10uF 16V 0402") is True
    assert _cell(model, 0, STOCK) == "5,000"


def test_details_for_a_row_that_has_since_been_filtered_out_are_dropped():
    """Routine, not exceptional: "Hide excluded BOM" can drop a row mid-fetch."""
    model = PartTableModel([PartRow("C1", "10uF", "C_0402", lcsc="C1525")])
    assert model.set_part_details("NOPE", "Basic", 1, "") is False


# ---------------------------------------------------------------------------
# Reconciling the board with the project database
# ---------------------------------------------------------------------------


def test_the_board_populates_the_project_database(part_list):
    """Every footprint becomes a row."""
    rows = part_list.rows()
    assert len(rows) == 110
    assert sum(1 for row in rows if row.assigned) == 93


def test_assembly_flags_match_what_the_wx_plugin_writes(board):
    """Both halves are installed until the cutover and share the project DB.

    A different key order or a different JSON shape here would make the
    estimator think every part's metadata was stale on every switch between the
    two.
    """
    records = board_part_records(board.footprints())
    flags = json.loads(
        next(r for r in records if r["reference"] == "R12")["assembly_flags"]
    )
    assert list(flags) == ["exclude_from_bom", "exclude_from_pos", "is_dnp"]
    assert flags["is_dnp"] is True


def test_dnp_comes_from_the_board_not_the_database(part_list):
    """The store has no DNP column; it is a live board attribute."""
    rows = {row.reference: row for row in part_list.rows()}
    assert rows["R12"].dnp is True
    assert rows["R11"].dnp is False


def test_hide_filters_drop_rows_rather_than_grey_them(part_list):
    """What the two "Hide excluded" toggles do."""
    part_list.hide_excluded_bom = True
    references = {row.reference for row in part_list.rows()}
    assert "B1" not in references
    assert "C1" in references


def test_toggling_bom_writes_the_board_and_then_the_database(part_list):
    """The board is written before the project database.

    The bridge verifies its writes, so one the board refused raises before the
    database has been told otherwise.
    """
    assert part_list.board.footprint("C1").exclude_from_bom is False

    part_list.toggle_exclusions(["C1"], bom=True)

    assert part_list.board.footprint("C1").exclude_from_bom is True
    assert bool(part_list.store.get_part("C1")["exclude_from_bom"]) is True


def test_a_mixed_selection_toggles_each_part_on_its_own_state(part_list):
    """Each part flips from its own state, not to a shared target.

    A mixed selection has no single "toggled" state, and forcing one would
    silently re-include a part the user had excluded on purpose.
    """
    part_list.toggle_exclusions(["C1"], bom=True)

    part_list.toggle_exclusions(["C1", "C2"], bom=True)

    assert part_list.board.footprint("C1").exclude_from_bom is False
    assert part_list.board.footprint("C2").exclude_from_bom is True


def test_a_refused_write_leaves_the_database_alone(board, tmp_path):
    """Trap 2, seen from the caller's side.

    With a board that reports success and changes nothing, the bridge raises —
    and because the board is written before the database, the two do not end up
    disagreeing. A project database that disagrees with the board is how a BOM
    comes out wrong.
    """
    with open(FIXTURE, encoding="utf-8") as handle:
        trapped = kicad_bridge.FixtureBoard.from_dict(
            copy.deepcopy(json.load(handle)), honour_footprint_writes=False
        )
    trapped.relocate(str(tmp_path))
    settings = Settings(path=str(tmp_path / "settings.json"))
    parts = PartList(trapped, settings=settings)
    parts.refresh_from_board()

    with pytest.raises(kicad_bridge.WriteVerificationError):
        parts.toggle_exclusions(["C1"], bom=True)

    assert bool(parts.store.get_part("C1")["exclude_from_bom"]) is False


def test_alike_finds_the_same_part_in_the_same_package(part_list):
    """What "Auto-select alike" acts on."""
    alike = part_list.alike("R2")
    values = {part_list.board.footprint(ref).value for ref in alike}
    assert values == {"100K"}
    assert "R2" in alike
    assert len(alike) > 1


def test_alike_on_something_unique_returns_just_itself(part_list):
    """A one-off part must not drag anything else into the selection."""
    assert part_list.alike("J5") == ["J5"]


def test_lcsc_column_shows_the_number_the_board_carries(part_list):
    """The store is the authority, but it was reconciled from the board."""
    model = PartTableModel(part_list.rows())
    assert _cell(model, model.row_for("C1"), LCSC) == "C13585"


# ---------------------------------------------------------------------------
# The part libraries, and the three columns they fill
# ---------------------------------------------------------------------------


def test_without_a_library_the_api_columns_are_blank_not_broken(part_list):
    """No data directory costs three columns; it must not stop the window."""
    assert part_list.library is None
    model = PartTableModel(part_list.rows())
    row = model.row_for("C1")

    assert _cell(model, row, TYPE) == ""
    # "?" because the part *has* a number and nobody has answered about it.
    assert _cell(model, row, STOCK) == "?"


def test_a_broken_data_directory_returns_no_library_rather_than_raising(tmp_path):
    """An unreadable cache is a missing column, not a failure to start."""
    settings = Settings(path=str(tmp_path / "settings.json"))
    # A file where the data directory should be: Library cannot mkdir over it.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    settings.values.setdefault("library", {})["data_path"] = str(blocked)

    assert open_library(_StoreOwner(settings, project_path=str(tmp_path))) is None


def test_the_fixture_library_fills_type_stock_and_params(part_list, tmp_path):
    """The three columns resolve from the local cache, with no network."""
    part_list.library = open_fixture_library(part_list.owner, str(tmp_path / "libdata"))
    model = PartTableModel(part_list.rows())
    row = model.row_for("C1")

    assert _cell(model, row, TYPE) == "Basic"
    assert _cell(model, row, STOCK) == "4,665,998"
    assert _cell(model, row, PARAMS) == "10uF 50V 1206"


def test_a_part_absent_from_the_cache_still_reads_as_unknown(part_list, tmp_path):
    """Seven of the fixture's numbers are uncached on purpose.

    They are what keeps "?" — nobody answered — visible in the same screenshot
    as real figures, so the two can be told apart by eye.
    """
    part_list.library = open_fixture_library(part_list.owner, str(tmp_path / "libdata"))
    model = PartTableModel(part_list.rows())
    row = model.row_for("R3")

    assert _cell(model, row, LCSC) == "C137969"
    assert _cell(model, row, STOCK) == "?"
    assert _cell(model, row, TYPE) == ""


def test_opening_the_libraries_never_reaches_the_network(part_list, tmp_path):
    """Phase 2 resolves from local storage only; fetching is Phase 4's job."""
    part_list.settings.values.setdefault("library", {})["data_path"] = str(
        tmp_path / "libdata"
    )
    part_list.open_libraries()

    assert part_list.library is not None
    assert part_list.library.allow_network is False


# ---------------------------------------------------------------------------
# Match highlighting — the row's own value and footprint, not a search
# ---------------------------------------------------------------------------


def test_match_terms_come_from_the_rows_own_value_and_footprint():
    """A lit-up cell is one whose derived params agree with the board."""
    row = PartRow("R2", "100K", "R_0402_1005Metric", params="100kΩ ±1% 0402")

    terms = _match_terms(row)

    assert "100k" in terms  # the value
    assert "0402" in terms  # the package, extracted from the footprint name


def test_match_terms_carry_the_ohm_and_micro_spellings():
    """`390R` is `390Ω` and `10uF` is `10µF`; the cell uses whichever LCSC did.

    This is the reason the expansion is shared with the wx plugin rather than
    rewritten — the equivalences have a long tail and getting one wrong shows up
    as a cell that simply never highlights.
    """
    assert "100kω" in _match_terms(
        PartRow("R2", "100K", "R_0402_1005Metric", params="100kΩ ±1% 0402")
    )
    assert "1μf" in _match_terms(
        PartRow("C10", "1uF", "C_0603_1608Metric", params="1uF 50V 0603")
    )


def test_short_terms_are_dropped():
    """One- and two-character terms match almost anything and are noise."""
    assert _match_terms(PartRow("R1", "1", "R_0402_1005Metric", params="1Ω 0402")) == (
        "0402",
    )


def test_a_row_with_no_params_has_nothing_to_highlight():
    """No text in the cell, so no terms — and no work done building them."""
    assert _match_terms(PartRow("B1", "M3 Mounting Hole", "MountingHole_3.2mm")) == ()


def test_part_list_rows_carry_their_highlight_terms(part_list, tmp_path):
    """The wiring: without this the delegate is painted nothing to work with."""
    part_list.library = open_fixture_library(part_list.owner, str(tmp_path / "lib"))
    row = next(r for r in part_list.rows() if r.reference == "R2")

    assert row.params
    assert "0402" in row.match_terms


def test_only_the_params_column_offers_highlight_terms():
    """A delegate on any other column must find nothing to paint."""
    model = PartTableModel(
        [_highlighted_row("R2", "100K", "R_0402_1005Metric", "100kΩ ±1% 0402")]
    )

    assert _cell(model, 0, PARAMS, MATCH_TERMS_ROLE)
    for column in (REF, VALUE, FOOTPRINT, LCSC, TYPE, STOCK, BOM, POS):
        assert _cell(model, 0, column, MATCH_TERMS_ROLE) is None


def test_the_delegate_finds_the_spans_the_cell_should_tint():
    """The join between the model's terms and the painted runs."""
    model = PartTableModel(
        [_highlighted_row("R2", "100K", "R_0402_1005Metric", "100kΩ ±1% 0402")]
    )
    delegate = MatchHighlightDelegate()
    index = model.index(0, PARAMS)
    text = index.data(Qt.ItemDataRole.DisplayRole)

    spans = delegate._spans(text, index)

    assert [text[start:end] for start, end in spans] == ["100kΩ", "0402"]


def test_highlighting_off_paints_nothing_specially():
    """Settings' toggle has to reach the delegate, not just the setting file."""
    model = PartTableModel(
        [_highlighted_row("R2", "100K", "R_0402_1005Metric", "100kΩ ±1% 0402")]
    )
    delegate = MatchHighlightDelegate(enabled=False)
    index = model.index(0, PARAMS)

    assert delegate._spans(index.data(Qt.ItemDataRole.DisplayRole), index) == []

    delegate.set_enabled(True)
    assert delegate._spans(index.data(Qt.ItemDataRole.DisplayRole), index) != []


def test_a_selected_row_uses_a_different_highlight_colour():
    """The theme's teal disappears into the selection fill."""
    assert theme.highlight_ink(selected=True) != theme.highlight_ink(selected=False)


def test_match_highlighting_is_not_the_standard_mode_amber():
    """Two different meanings that can appear on the same row.

    A standard-mode trigger colours the whole row and says "this costs more";
    a match tints runs inside one cell and says "this corroborates". One colour
    for both would read as one meaning — the mistake red and amber made once.
    """
    assert theme.highlight_ink() != theme.standard_trigger_colour()
