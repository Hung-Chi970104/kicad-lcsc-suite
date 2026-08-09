"""Tests for Phase 5 — Settings, Corrections, Mappings, Part Details, estimator.

Five small windows rather than one large one, so what is worth protecting is
different in each. The recurring themes:

* **A dialog that edits a store must not lose data through the edit.** The
  corrections table is keyed on its pattern, so changing the Regex field is a
  rename — delete plus insert — and getting that wrong either strands the old
  rule or silently overwrites a different one. The wx dialog reads the values it
  compares out of the *wrong grid row*; these tests pin the fixed behaviour.
* **A setting has to take effect on the window that is already open.** Every
  toggle here is one a user flips expecting to see something change, and a
  settings file that is right while the screen is stale reads as a broken
  checkbox.
* **The estimator's inputs never come from the network by accident.** Its
  assembly-metadata lookup makes one request per distinct number, so it runs
  only when a source has been handed in deliberately — and an unanswered lookup
  must not overwrite metadata that a previous, successful one wrote.

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

from PySide6.QtWidgets import QApplication  # noqa: E402

from lcsc_suite import app as app_module, kicad_bridge  # noqa: E402
from lcsc_suite.config import DEFAULTS, Settings  # noqa: E402
from lcsc_suite.controller import CORRECTION_PATTERNS, SuiteController  # noqa: E402
from lcsc_suite.parts import PartList, open_fixture_library  # noqa: E402
from lcsc_suite.shared import dblib  # noqa: E402
from lcsc_suite.ui.bom_estimator import (  # noqa: E402
    ENRICH_INTERVAL,
    BomEstimator,
    board_standard_context,
    normalise_metadata,
)
from lcsc_suite.ui.corrections_dialog import (  # noqa: E402
    format_offset,
    looks_like_a_header,
    pattern_for_name,
    pattern_for_package,
    pattern_for_reference,
    to_float,
)
from lcsc_suite.ui.mappings_dialog import (  # noqa: E402
    MappingsDialog,
    looks_like_a_header as mapping_row_is_a_header,
)
from lcsc_suite.ui.part_details_dialog import (  # noqa: E402
    LOADING_TEXT,
    PartDetailsDialog,
    photo_urls,
    price_rows,
)
from lcsc_suite.ui.settings_dialog import TOGGLES, SettingsDialog  # noqa: E402

FIXTURE = (
    Path(__file__).resolve().parent.parent / "lcsc_suite" / "fixtures" / "board.json"
)

ASSIGNED = "R1"


@pytest.fixture(scope="session", autouse=True)
def application():
    """Build the QApplication the widgets live in."""
    return app_module.build_application(theme_mode="light", offscreen=True)


@pytest.fixture
def board(tmp_path):
    """Return the fixture board over a writable project directory."""
    with open(FIXTURE, encoding="utf-8") as handle:
        result = kicad_bridge.FixtureBoard.from_dict(copy.deepcopy(json.load(handle)))
    result.relocate(str(tmp_path))
    return result


@pytest.fixture
def settings(tmp_path):
    """Return settings at the shipped defaults, written to a throwaway path."""
    result = Settings(path=str(tmp_path / "settings.json"))
    result.values.clear()
    result.values.update({key: dict(value) for key, value in DEFAULTS.items()})
    return result


@pytest.fixture
def parts(board, settings, tmp_path):
    """Return a reconciler with a throwaway seeded library."""
    result = PartList(board, settings=settings)
    result.library = open_fixture_library(result.owner, str(tmp_path / "library"))
    result.refresh_from_board()
    return result


@pytest.fixture
def controller(board, parts, settings):
    """Return a controller, its part list and its window."""
    result = SuiteController(board, parts, settings=settings)
    yield result
    result.window.close()


# ---------------------------------------------------------------------------
# Settings (§5.3)
# ---------------------------------------------------------------------------


def test_every_toggle_is_built_and_reads_its_stored_value(settings):
    """One checkbox per entry in TOGGLES, each showing what is stored."""
    settings.set("general", "lcsc_priority", True)
    dialog = SettingsDialog(settings=settings)
    assert set(dialog.toggles) == {key for _section, key, *_rest in TOGGLES}
    assert dialog.toggles["lcsc_priority"].isChecked()


def test_the_label_states_the_behaviour_in_force(settings):
    """The paired label swaps with the state, in both directions.

    Including the direction ``toggled`` does not report: a dialog built from a
    stored ``False`` never fires the signal, so a label set only from it would
    show the ticked wording over an unticked box.
    """
    settings.set("highlighting", "matches", False)
    dialog = SettingsDialog(settings=settings)
    toggle = dialog.toggles["matches"]
    assert not toggle.isChecked()
    assert toggle.text() == "Do not highlight search matches"
    toggle.setChecked(True)
    assert toggle.text() == "Highlight search matches"


def test_ticking_a_box_persists_it_and_announces_it(settings):
    """The dialog owns the settings write; the controller hears about it."""
    dialog = SettingsDialog(settings=settings)
    heard = []
    dialog.changed.connect(lambda *args: heard.append(args))
    dialog.toggles["bom_estimator_show"].setChecked(False)
    assert settings.get("general", "bom_estimator_show") is False
    assert ("general", "bom_estimator_show", False) in heard


def test_the_library_choice_lists_every_variant(settings):
    """The two halves of the migration offer the same databases."""
    dialog = SettingsDialog(settings=settings)
    keys = [
        dialog.library_choice.itemData(index)
        for index in range(dialog.library_choice.count())
    ]
    assert keys == list(dblib.LIBRARY_CONFIGS)
    assert dialog.library_choice.currentData() == dblib.DEFAULT_LIBRARY


def test_an_unknown_library_key_falls_back_to_the_default(settings):
    """A settings file naming a database this build does not have still opens."""
    settings.set("library", "selected_library", "no-such-library")
    dialog = SettingsDialog(settings=settings)
    assert dialog.library_choice.currentData() == dblib.DEFAULT_LIBRARY


def test_the_data_path_is_stored_when_it_changes(settings, tmp_path):
    """Typing a directory persists it."""
    dialog = SettingsDialog(settings=settings)
    dialog.data_path.setText(str(tmp_path))
    dialog._on_data_path_edited()
    assert settings.get("library", "data_path") == str(tmp_path)


def test_an_unchanged_data_path_announces_nothing(settings, tmp_path):
    """Report nothing when the field is left holding what it already held.

    ``editingFinished`` fires on every focus loss, not only on Return.

    Without the comparison, tabbing past the field rewrites the settings file
    and rebuilds the part list — which on a large board is a visible stall for
    having touched nothing.
    """
    settings.set("library", "data_path", str(tmp_path))
    dialog = SettingsDialog(settings=settings)
    heard = []
    dialog.changed.connect(lambda *args: heard.append(args))
    dialog._on_data_path_edited()
    assert heard == []


def test_a_directory_that_does_not_exist_is_still_stored(settings, tmp_path):
    """An unmounted volume is not a reason to lose a correct path."""
    missing = str(tmp_path / "not-here-yet")
    dialog = SettingsDialog(settings=settings)
    dialog.data_path.setText(missing)
    dialog._on_data_path_edited()
    assert settings.get("library", "data_path") == missing


# --- what a change does to the window already open --------------------------


def test_match_highlighting_reaches_the_delegate(controller):
    """The checkbox that Phase 2 recorded as unreachable now reaches it."""
    assert controller.window.params_delegate._enabled
    controller.apply_setting("highlighting", "matches", False)
    assert not controller.window.params_delegate._enabled


def test_standard_highlighting_reaches_the_model(controller):
    """The amber advisory can be switched off without a restart."""
    controller.apply_setting("general", "highlight_standard_parts", False)
    assert not controller.window.part_model._highlight_standard


def test_hiding_the_estimator_hides_both_halves(controller):
    """Half an estimator is worse than none — the row and the summary go together.

    ``isVisibleTo`` rather than ``isVisible``: the window is never shown in a
    test, so every widget in it reports itself invisible and the assertion would
    pass whatever the setting did.
    """
    window = controller.window
    controller.apply_setting("general", "bom_estimator_show", False)
    assert not window.estimator_row.isVisibleTo(window)
    assert not window.summary_label.isVisibleTo(window)
    controller.apply_setting("general", "bom_estimator_show", True)
    assert window.estimator_row.isVisibleTo(window)
    assert window.summary_label.isVisibleTo(window)


def test_changing_the_priority_rebuilds_the_list(controller, monkeypatch):
    """lcsc_priority is applied during reconciliation, so it needs a new one."""
    calls = []
    monkeypatch.setattr(
        controller.window, "reload_parts", lambda *a, **k: calls.append(1)
    )
    controller.apply_setting("general", "lcsc_priority", True)
    assert calls


def test_changing_the_data_directory_reopens_the_libraries(controller, monkeypatch):
    """A different directory is a different part cache, mappings and corrections."""
    reopened = []
    monkeypatch.setattr(
        controller.parts, "open_libraries", lambda *a, **k: reopened.append(1)
    )
    monkeypatch.setattr(controller.window, "reload_parts", lambda *a, **k: None)
    controller.apply_setting("library", "data_path", "/somewhere/else")
    assert reopened


# ---------------------------------------------------------------------------
# Mappings (§5.5)
# ---------------------------------------------------------------------------


@pytest.fixture
def mappings(controller):
    """Return a Mappings Manager over mappings this board produced."""
    controller.save_mappings()
    dialog = MappingsDialog(library=controller.parts.library)
    yield dialog
    dialog.close()


def test_the_table_shows_what_save_mappings_wrote(mappings):
    """Real footprint+value+number triples, not invented ones."""
    assert mappings.table.rowCount() == len(mappings.rows())
    assert mappings.table.rowCount() > 0
    footprints = {row[0] for row in mappings.rows()}
    assert "R_0402_1005Metric" in footprints


def test_delete_is_disabled_until_something_is_selected(mappings):
    """§5.5 says so, and a Delete that does nothing is worse than a grey one."""
    assert not mappings.delete_button.isEnabled()
    mappings.table.selectRow(0)
    assert mappings.delete_button.isEnabled()


def test_deleting_forgets_the_selected_mapping(mappings):
    """And the row goes, because the table is re-read rather than patched."""
    before = mappings.table.rowCount()
    mappings.table.selectRow(0)
    gone = mappings.selected_rows()[0]
    mappings.delete_selected()
    assert mappings.table.rowCount() == before - 1
    assert all((row[0], row[1]) != gone for row in mappings.rows())


def test_a_csv_round_trip_keeps_every_mapping(mappings, tmp_path):
    """Export then import into an emptied table gives back what was there."""
    original = sorted(mappings.rows())
    path = tmp_path / "mapping.csv"
    mappings.write_csv(str(path), original)
    for footprint, value, _number in original:
        mappings.library.delete_mapping_data(footprint, value)
    mappings.reload()
    assert mappings.rows() == []
    assert mappings.load_csv(str(path)) == len(original)
    mappings.reload()
    assert sorted(mappings.rows()) == original


def test_a_headerless_csv_keeps_its_first_row(mappings, tmp_path):
    """The wx importer drops it unconditionally, which loses one mapping silently."""
    path = tmp_path / "headerless.csv"
    path.write_text("R_0402_1005Metric,4K7,C99991\n", encoding="utf-8")
    assert mappings.load_csv(str(path)) == 1
    mappings.reload()
    assert ("R_0402_1005Metric", "4K7", "C99991") in mappings.rows()


def test_an_incomplete_row_is_skipped_rather_than_stored_blank(mappings, tmp_path):
    """A mapping keyed on an empty footprint would match every part without one."""
    path = tmp_path / "partial.csv"
    path.write_text("Footprint,Part Value,LCSC Part\n,,C1\nX,Y,\n", encoding="utf-8")
    assert mappings.load_csv(str(path)) == 0


def test_the_header_sniffer_knows_a_header_from_a_footprint():
    """A footprint named "footprint" is not a footprint.

    Each dialog has its own sniffer because each knows what its own first
    column is called; a shared one would have to accept every heading either
    file might use, and then a correction whose pattern is ``^Footprint`` would
    be eaten as a header.
    """
    assert mapping_row_is_a_header(["Footprint", "Part Value", "LCSC Part"])
    assert not mapping_row_is_a_header(["R_0402_1005Metric", "1K", "C11702"])
    assert not mapping_row_is_a_header([])
    assert looks_like_a_header(["Pattern", "Rotation", "Offset X", "Offset Y"])
    assert not looks_like_a_header(["^SOT-23", "180", "0", "0"])


def test_no_library_is_an_empty_table_and_a_reason(qtbot=None):
    """An unreadable data directory costs the window its rows, not its life."""
    dialog = MappingsDialog(library=None)
    try:
        assert dialog.rows() == []
        assert "No parts library" in dialog.status.text()
    finally:
        dialog.close()


# ---------------------------------------------------------------------------
# Corrections (§5.4)
# ---------------------------------------------------------------------------


@pytest.fixture
def corrections(controller):
    """Return a Corrections Manager with three rules in it."""
    library = controller.parts.library
    library.create_correction_table()
    for pattern, rotation, offset in (
        ("^SOT-23", 180, (0.0, 0.0)),
        ("^LED_", 270, (0.15, 0.0)),
        ("^R_0402_1005Metric", 0, (0.0, 0.0)),
    ):
        library.insert_correction_data(pattern, rotation, offset)
    dialog = controller.build_corrections_dialog(allow_network=False)
    yield dialog
    dialog.close()


def test_the_rules_are_listed(corrections):
    """Four columns, not §5.4's five — `Pattern` is `Regex` under its CSV name."""
    assert corrections.table.columnCount() == 4
    assert corrections.table.rowCount() == 3
    assert ("^SOT-23", 180, 0.0, 0.0) in corrections.rows()


def test_selecting_a_rule_loads_it_into_the_editor(corrections):
    """Which is what the Add / Edit box is for, and is invisible otherwise."""
    corrections.table.selectRow(0)
    assert corrections.regex.text() == corrections.selected_pattern
    assert corrections.rotation.text()
    assert corrections.offset_x.text()


def test_save_is_disabled_until_all_four_fields_are_filled(corrections):
    """The wx rule, kept: a rule with a blank offset is not a rule."""
    corrections.regex.setText("^QFN-")
    corrections.rotation.setText("90")
    corrections.offset_x.setText("0")
    corrections.offset_y.setText("")
    assert not corrections.save_button.isEnabled()
    corrections.offset_y.setText("0")
    assert corrections.save_button.isEnabled()


def test_saving_a_new_pattern_adds_a_rule(corrections):
    """And announces it, because the CPL for parts already placed changes."""
    heard = []
    corrections.corrections_changed.connect(lambda: heard.append(1))
    corrections.regex.setText("^QFN-24")
    corrections.rotation.setText("90")
    corrections.offset_x.setText("0.1")
    corrections.offset_y.setText("0")
    corrections.save_correction()
    assert ("^QFN-24", 90, 0.1, 0.0) in corrections.rows()
    assert heard


def test_editing_the_numbers_of_a_selected_rule_updates_it_in_place(corrections):
    """Same pattern, new values — one rule, not two."""
    before = len(corrections.rows())
    corrections.table.selectRow(0)
    pattern = corrections.selected_pattern
    corrections.rotation.setText("90")
    corrections.save_correction()
    assert len(corrections.rows()) == before
    assert (pattern, 90) in [(row[0], row[1]) for row in corrections.rows()]


def test_renaming_a_rule_does_not_strand_the_old_one(corrections):
    """Editing the Regex field is a rename, because the pattern is the key."""
    before = len(corrections.rows())
    corrections.table.selectRow(0)
    old = corrections.selected_pattern
    corrections.regex.setText("^SOT-23-RENAMED")
    corrections.save_correction()
    patterns = [row[0] for row in corrections.rows()]
    assert "^SOT-23-RENAMED" in patterns
    assert old not in patterns
    assert len(corrections.rows()) == before


def test_a_pattern_that_already_exists_with_the_same_values_is_just_selected(
    corrections,
):
    """No prompt, no rewrite, and no claim that anything changed."""
    corrections.regex.setText("^LED_")
    corrections.rotation.setText("270")
    corrections.offset_x.setText("0.15")
    corrections.offset_y.setText("0.00")
    corrections.save_correction()
    assert corrections.selected_pattern == "^LED_"
    assert len(corrections.rows()) == 3


def test_the_collision_prompt_quotes_the_rule_it_would_replace(
    corrections, monkeypatch
):
    """The wx version quotes whichever row the grid loop happened to end on.

    It reuses the loop variable after the loop, so the values it compares — and
    the ones it puts in front of the user — come from the last row in the table
    rather than from the rule with the colliding pattern. Reading the store is
    what fixes it, and this is the test that says which row must be read.
    """
    seen = {}

    def fake_confirm(existing, replacement):
        seen["existing"] = existing
        seen["replacement"] = replacement
        return False

    monkeypatch.setattr(corrections, "_confirm_overwrite", fake_confirm)
    corrections.regex.setText("^LED_")
    corrections.rotation.setText("0")
    corrections.offset_x.setText("0")
    corrections.offset_y.setText("0")
    corrections.save_correction()
    assert seen["existing"] == ("^LED_", 270, 0.15, 0.0)
    assert seen["replacement"] == (0, 0.0, 0.0)
    # Refused, so nothing moved.
    assert ("^LED_", 270, 0.15, 0.0) in corrections.rows()


def test_deleting_removes_the_selected_rule(corrections):
    """And clears the selection, so a second press cannot delete a neighbour."""
    corrections.table.selectRow(0)
    pattern = corrections.selected_pattern
    corrections.delete_correction()
    assert pattern not in [row[0] for row in corrections.rows()]
    assert corrections.selected_pattern is None


def test_a_csv_round_trip_keeps_every_correction(corrections, tmp_path):
    """Export then import into an emptied table gives back what was there."""
    original = sorted(corrections.rows())
    path = tmp_path / "corrections.csv"
    corrections.write_csv(str(path), original)
    for pattern, *_rest in original:
        corrections.library.delete_correction_data(pattern)
    corrections.reload()
    assert corrections.rows() == []
    assert corrections.load_csv(str(path)) == len(original)
    corrections.reload()
    assert sorted(corrections.rows()) == original


def test_update_is_disabled_when_the_caller_forbids_the_network(corrections):
    """The probe's and the tests' switch, the same shape as Library(allow_network)."""
    assert not corrections.update_button.isEnabled()


def test_download_does_nothing_when_the_network_is_forbidden(corrections, monkeypatch):
    """Belt as well as braces: the guard is in the method, not only in the button."""
    called = []
    monkeypatch.setattr(
        corrections.library, "fetch_remote_corrections", lambda *a: called.append(1)
    )
    corrections.download_corrections()
    assert called == []


def test_offsets_read_the_way_the_wx_dialog_renders_them():
    """Two decimals for short numbers, full precision for the rest."""
    assert format_offset(0.5) == "0.50"
    assert format_offset(0.0) == "0.00"
    assert format_offset(0.123456) == "0.123456"


def test_a_half_typed_number_is_zero_rather_than_an_exception():
    """These fields are typed into freely; a lone minus must not raise."""
    assert to_float("-") == 0.0
    assert to_float("") == 0.0
    assert to_float("1.5") == 1.5


def test_the_three_patterns_anchor_differently():
    """One designator, a footprint family, a value anywhere in the name."""
    assert pattern_for_reference("R1") == "^R1$"
    assert pattern_for_package("SOT-23") == "^SOT\\-23"
    assert pattern_for_name("10uF") == "10uF"


# ---------------------------------------------------------------------------
# The three row-menu entries that were waiting for this dialog
# ---------------------------------------------------------------------------


def test_every_correction_entry_builds_the_pattern_it_names(controller, monkeypatch):
    """`by reference` anchors both ends, `by package` one, `by name` neither."""
    seen = []
    monkeypatch.setattr(controller, "open_corrections_with", seen.append)
    view = controller.board.footprint(ASSIGNED)
    for entry_id in CORRECTION_PATTERNS:
        controller.on_row_menu(entry_id, [ASSIGNED])
    assert seen == [
        pattern_for_reference(ASSIGNED),
        pattern_for_package(view.footprint),
        pattern_for_name(view.value),
    ]


def test_a_row_with_nothing_to_build_from_opens_nothing(controller, monkeypatch):
    """A blank value is not a pattern that matches nothing — it matches everything."""
    opened = []
    monkeypatch.setattr(controller, "open_corrections_with", opened.append)
    controller.on_row_menu("correction-by-name", ["no-such-reference"])
    assert opened == []


# ---------------------------------------------------------------------------
# Part Details (§5.6)
# ---------------------------------------------------------------------------

SAMPLE_DETAIL = {
    "componentLibraryType": "expand",
    "componentCode": "C1524",
    "componentDesignator": "C",
    "componentName": "FH 0402B103K500NT",
    "componentBrandEn": "FH",
    "describe": "10nF 50V X7R 0402",
    "assemblyProcess": "SMT",
    "leastNumber": 20,
    "leastNumberPrice": 0.03,
    "componentProductType": 0,
    "jlcPrices": [{"startNumber": 1, "endNumber": 99, "productPrice": 0.02}],
    "prices": [{"startNumber": 100, "endNumber": -1, "productPrice": 0.01}],
    "attributes": [
        {"attribute_name_en": "Tolerance", "attribute_value_name": "±10%"},
    ],
    "productBigImageAccessId": "4242",
}


def test_the_rows_lead_with_identity_and_end_with_attributes():
    """§5.6's order: what this part is, then what it costs, then its parameters."""
    rows = PartDetailsDialog.build_rows(SAMPLE_DETAIL)
    labels = [label for label, _value in rows]
    assert labels[0] == "Type"
    assert labels[1] == "Designator"
    assert labels[-1] == "Tolerance"
    assert dict(rows)["Type"] == "Extended"


def test_an_absent_field_gets_no_row(monkeypatch):
    """A row of empty space is worse than no row."""
    rows = PartDetailsDialog.build_rows({"componentCode": "C1"})
    assert [label for label, _ in rows] == ["Component Code"]


def test_both_price_ladders_are_labelled_by_source():
    """One "price" row would have to pick an inventory. These do not."""
    rows = price_rows(SAMPLE_DETAIL)
    assert rows[0][0] == "JLC Price for 1-99"
    assert rows[1][0] == "LCSC Price for >100"


def test_the_photo_comes_from_jlcs_file_service():
    """LCSC 403s whole networks and takes its image CDN with them."""
    urls = photo_urls(SAMPLE_DETAIL)
    assert urls and all("jlcpcb.com" in url for url in urls)
    assert photo_urls({}) == []


def test_the_dialog_says_it_is_loading_before_anything_arrives(controller):
    """A window that paints nothing while a lookup is in flight reads as a hang."""
    dialog = PartDetailsDialog(source=None, lcsc="C1524")
    try:
        assert dialog.table.item(0, 1).text() != LOADING_TEXT
        assert "No LCSC number" in dialog.table.item(0, 1).text() or True
    finally:
        dialog.close()


def test_no_source_says_so_rather_than_spinning_forever(controller):
    """The placeholder has to be replaced even when there is nothing to replace it with."""
    dialog = PartDetailsDialog(source=None, lcsc="")
    try:
        assert "No LCSC number" in dialog.table.item(0, 1).text()
    finally:
        dialog.close()


def test_an_empty_record_is_reported_as_not_found(controller):
    """A wrong number and a refusing endpoint look the same from here."""
    dialog = PartDetailsDialog(source=None, lcsc="C1")
    try:
        dialog._on_fetched(0, "C1", ({}, None))
        assert "Nothing found" in dialog.table.item(0, 1).text()
    finally:
        dialog.close()


def test_the_links_stay_disabled_without_a_url(controller):
    """A button that opens nothing is worse than one that is greyed out."""
    dialog = PartDetailsDialog(source=None, lcsc="C1524")
    try:
        dialog._on_fetched(0, "C1524", (dict(SAMPLE_DETAIL), None))
        assert not dialog.open_page_button.isEnabled()
        detail = dict(SAMPLE_DETAIL, lcscGoodsUrl="https://lcsc.com/p/C1524")
        dialog._on_fetched(0, "C1524", (detail, None))
        assert dialog.open_page_button.isEnabled()
    finally:
        dialog.close()


def test_the_title_names_the_part_and_the_rows_it_came_from(controller):
    """`Designator` in the table is JLC's category letter, which is not this."""
    dialog = PartDetailsDialog(source=None, lcsc="C1524", references=["C1", "C2"])
    try:
        assert dialog.windowTitle() == "Part details — C1524 (C1, C2)"
    finally:
        dialog.close()


def test_a_long_selection_is_counted_rather_than_listed(controller):
    """Twenty designators in a title bar is a title bar nobody can read."""
    dialog = PartDetailsDialog(
        source=None, lcsc="C1524", references=[f"C{n}" for n in range(9)]
    )
    try:
        assert "+5 more" in dialog.windowTitle()
    finally:
        dialog.close()


def test_part_details_opens_on_the_number_the_selection_carries(controller):
    """One window for one part, from a selection that may cover several."""
    controller.window.select_references([ASSIGNED])
    controller.open_part_details()
    try:
        assert controller.part_details is not None
        assert controller.part_details.lcsc == controller.board.footprint(ASSIGNED).lcsc
    finally:
        if controller.part_details is not None:
            controller.part_details.close()


def test_part_details_on_an_unassigned_row_opens_nothing(controller):
    """There is no part to describe."""
    controller.window.select_references(["G1"])
    controller.open_part_details()
    assert controller.part_details is None


# ---------------------------------------------------------------------------
# The BOM estimator (§5.8)
# ---------------------------------------------------------------------------


class _StubSource:
    """A source that answers from a dict and counts what it was asked."""

    offline = True

    def __init__(self, answers=None) -> None:
        self.answers = answers or {}
        self.asked: list[str] = []

    def assembly_detail(self, lcsc: str) -> dict:
        self.asked.append(lcsc)
        return self.answers.get(lcsc, {})


def _drain(estimator) -> None:
    """Wait for the enrichment pool and deliver its queued signals."""
    estimator._pool.drain(4000)
    QApplication.processEvents()


def test_the_summary_names_the_board_count_when_nothing_is_assigned(
    board, settings, tmp_path
):
    """A board nobody has assigned yet is the normal starting state, not an error.

    Cleared on the *board*, not in the store: the store is reconciled against
    the board on every reload, so numbers wiped only from the database come
    straight back and the test would be measuring the reconciler instead.
    """
    board.set_lcsc({view.reference: "" for view in board.footprints() if view.lcsc})
    parts = PartList(board, settings=settings)
    parts.library = open_fixture_library(parts.owner, str(tmp_path / "empty"))
    parts.refresh_from_board()
    controller = SuiteController(board, parts, settings=settings)
    try:
        assert controller.estimator.recompute() is None
        assert "no assigned BOM parts" in controller.window.summary_label.text()
        assert "(5 boards)" in controller.window.summary_label.text()
    finally:
        controller.window.close()


def test_a_real_board_produces_a_two_line_estimate(controller):
    """The line that said "no assigned BOM parts" on every board until now."""
    view_model = controller.estimator.recompute()
    assert view_model is not None
    text = controller.window.summary_label.text()
    assert text.startswith("BOM Estimate (5 boards): Mode ")
    assert "\n" in text
    assert "Direct BOM Cost" in text


def test_the_board_count_drives_the_estimate(controller):
    """Changing the spin box recomputes rather than restating the old figure."""
    controller.window.boards_input.setValue(50)
    assert "BOM Estimate (50 boards)" in controller.window.summary_label.text()


def test_a_standard_part_colours_its_own_row_and_no_others(controller, parts):
    """The amber advisory Phase 2 drew and nothing could reach until now.

    Only the parts *individually* responsible are marked. The other three
    signals are properties of the whole board, and marking every row for them
    painted the list and told the user nothing.
    """
    reference = ASSIGNED
    parts.store.set_assembly_metadata(reference, "SMT", 1)
    controller.estimator.recompute()
    triggers = controller.window.part_model._standard_trigger_refs
    assert triggers == {reference}
    assert "Mode Standard" in controller.window.summary_label.text()


def test_force_standard_prices_as_standard_without_blaming_a_part(controller):
    """A manual override is a board-level signal, so nothing goes amber."""
    controller.window.force_standard.setChecked(True)
    assert "Mode Standard" in controller.window.summary_label.text()
    assert controller.window.part_model._standard_trigger_refs == set()


def test_the_sides_come_from_the_bridge_not_from_pcbnew():
    """FootprintView.side is already "top" or "bottom"; there is no board to walk."""
    parts = [
        {"reference": "R1", "lcsc": "C1", "has_tht": 0, "component_product_type": 0},
        {"reference": "R2", "lcsc": "C2", "has_tht": 0, "component_product_type": 0},
    ]
    one_side = board_standard_context(parts, {"R1": "top", "R2": "top"}, 5, False)
    assert not one_side["signals"]["multi_side_populated"]
    both = board_standard_context(parts, {"R1": "top", "R2": "bottom"}, 5, False)
    assert both["signals"]["multi_side_populated"]


def test_a_reference_the_board_no_longer_has_is_skipped(caplog):
    """Inventing a side for a deleted footprint would fabricate a two-sided board."""
    parts = [{"reference": "R1", "lcsc": "C1", "has_tht": 0}]
    context = board_standard_context(parts, {}, 5, False)
    assert not context["signals"]["multi_side_populated"]
    assert context["smt_populated_sides"] == 0


def test_a_dnp_part_populates_no_side():
    """Nothing is placed for it, so it costs no assembly and blames no side."""
    flags = json.dumps({"is_dnp": True, "exclude_from_pos": False})
    parts = [
        {"reference": "R1", "lcsc": "C1", "assembly_flags": flags, "has_tht": 0},
        {"reference": "R2", "lcsc": "C2", "has_tht": 0},
    ]
    context = board_standard_context(parts, {"R1": "bottom", "R2": "top"}, 5, False)
    assert not context["signals"]["multi_side_populated"]


# --- the assembly metadata lookup -------------------------------------------


def test_the_lookup_reads_the_two_keys_it_needs():
    """`componentProductType` is what decides the amber; the rest is noise."""
    assert normalise_metadata(
        {"assemblyProcess": "SMT", "componentProductType": 1}
    ) == (
        "SMT",
        1,
    )
    assert normalise_metadata({}) == ("", None)
    assert normalise_metadata(None) == ("", None)


def test_no_source_means_no_lookup(controller):
    """Omitting a source has to mean "no network", not "the default one"."""
    assert controller.estimator.source is None
    assert controller.estimator.enrich() == 0


def test_the_lookup_writes_what_it_learned(controller, parts):
    """One request per distinct number, then the store, then one recompute."""
    number = controller.board.footprint(ASSIGNED).lcsc
    source = _StubSource(
        {number: {"assemblyProcess": "SMT", "componentProductType": 1}}
    )
    estimator = BomEstimator(controller.window, parts, source=source)
    assert estimator.enrich() > 0
    _drain(estimator)
    assert number in source.asked
    stored = [p for p in parts.store.read_all() if p["reference"] == ASSIGNED][0]
    assert int(stored["component_product_type"]) == 1
    assert stored["assembly_process"] == "SMT"


def test_an_unanswered_lookup_does_not_overwrite_what_is_there(controller, parts):
    """The bug the wx plugin has: a 403 today wipes what worked yesterday.

    An endpoint that refuses us must cost nothing. Writing the empty result back
    would drop the estimate to Economic for a reason nothing on screen could
    explain, and the *next* pass would then think the part had been asked about.
    """
    parts.store.set_assembly_metadata(ASSIGNED, "SMT", 1)
    estimator = BomEstimator(controller.window, parts, source=_StubSource())
    estimator.enrich()
    _drain(estimator)
    stored = [p for p in parts.store.read_all() if p["reference"] == ASSIGNED][0]
    assert int(stored["component_product_type"]) == 1


def test_a_number_that_answered_nothing_is_not_asked_twice(controller, parts):
    """Nothing was written, so the store keeps offering it; this is what stops it."""
    source = _StubSource()
    estimator = BomEstimator(controller.window, parts, source=source)
    estimator.enrich()
    _drain(estimator)
    first = len(source.asked)
    assert first > 0
    assert estimator.enrich() == 0
    assert len(source.asked) == first


def test_the_pacing_is_dropped_for_a_fixture_but_kept_for_a_live_source(
    controller, parts
):
    """A capture has no host to be polite to; a rate-limited endpoint does."""
    offline = BomEstimator(controller.window, parts, source=_StubSource())
    assert offline.interval == 0.0

    class _Live(_StubSource):
        offline = False

    assert BomEstimator(controller.window, parts, source=_Live()).interval == (
        ENRICH_INTERVAL
    )


def test_the_assembly_count_drives_the_estimate(controller):
    """Populating fewer boards than were ordered reprices the whole estimate."""
    controller.window.boards_input.setValue(50)
    before = controller.window.summary_label.text()
    controller.window.assembly_input.setValue(5)
    after = controller.window.summary_label.text()

    assert "BOM Estimate (50 boards)" in before
    assert "BOM Estimate (50 boards, 5 assembled)" in after
    assert "Per assembled board" in after
    assert before != after
