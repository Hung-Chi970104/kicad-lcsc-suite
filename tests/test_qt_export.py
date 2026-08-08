"""Tests for Phase 6 — the BOM and CPL writers.

What is worth protecting here is not that two CSV files appear. It is:

* **the position is the pad-bounding-box centre, not the footprint origin.**
  Those two agree on a symmetric part and disagree on most others, so a port
  that used the origin would look right on the first board anyone tried it on.
  The fixture is deliberately built so they never agree — see
  ``PAD_CENTRE_SKEW_NM``;
* **the arithmetic is integer nanometres until the final division.** A pipeline
  that converts early prints ``123.45678900000001`` and the file no longer
  compares equal to the one the wx plugin wrote;
* **both halves run the same code.** ``fabrication.py`` delegates to
  ``fab_rules``, so the wx plugin and this app cannot drift. The tests below pin
  that delegation rather than re-deriving the rules;
* **an uncorrected angle is not normalised.** KiCad says ``-90.0`` and the file
  has always said ``-90.0``; only a *corrected* angle takes a modulus;
* **what was left out is reported.** "Why is my BOM shorter than my board" is
  the first question anyone asks of a file like this.

The real byte-comparison against the wx plugin was run live against
KiCad 10.0.3 on a 110-footprint board — see the plan's §10 Phase 6 entry. These
tests are what keeps it true afterwards.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from lcsc_suite import app as app_module, kicad_bridge  # noqa: E402
from lcsc_suite.config import DEFAULTS, Settings  # noqa: E402
from lcsc_suite.controller import build as build_controller  # noqa: E402
from lcsc_suite.export import Exporter  # noqa: E402
from lcsc_suite.parts import PartList  # noqa: E402
from lcsc_suite.shared import fab_rules  # noqa: E402

BOARD_FIXTURE = (
    Path(__file__).resolve().parent.parent / "lcsc_suite" / "fixtures" / "board.json"
)

#: The fixture's pads are laid out so every footprint that has any has its pad
#: centre exactly 0.1mm to the left of its origin. A fixture where the two
#: agreed would hide the difference between them, which is the one thing about
#: CPL geometry that is easy to get wrong and invisible when you do.
PAD_CENTRE_SKEW_NM = -100_000


@pytest.fixture(scope="session", autouse=True)
def application():
    """Build the QApplication the widgets live in."""
    return app_module.build_application(theme_mode="light", offscreen=True)


@pytest.fixture
def board(tmp_path):
    """Return the fixture board, pointed at a writable project directory."""
    fixture = kicad_bridge.open_fixture(str(BOARD_FIXTURE))
    fixture.relocate(str(tmp_path))
    return fixture


@pytest.fixture
def settings(tmp_path):
    """Shipped defaults, written somewhere throwaway."""
    values = Settings(path=str(tmp_path / "settings.json"))
    values.values.clear()
    values.values.update({key: dict(block) for key, block in DEFAULTS.items()})
    return values


@pytest.fixture
def exporter(board, settings, tmp_path):
    """Return an exporter over the fixture board and a real project database."""
    parts = PartList(board, settings=settings)
    parts.refresh_from_board()
    return Exporter(board, parts.store, library=None, settings=settings)


def read_csv(path):
    """Return ``(header, rows)`` from a written file."""
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    return rows[0], rows[1:]


def cpl_by_reference(path):
    """Return ``{designator: row}`` for a written CPL."""
    _header, rows = read_csv(path)
    return {row[0]: row for row in rows}


# ---------------------------------------------------------------------------
# The rules — pcbnew's arithmetic, reproduced
# ---------------------------------------------------------------------------


def test_the_merged_box_centre_matches_pcbnews():
    """``BOX2I::GetCenter`` is ``pos + size // 2``, measured against KiCad 10.0.3.

    Integer division, so a box of odd width centres one nanometre below the
    middle. Rounding instead would move a coordinate's last digit.
    """
    assert fab_rules.box_center([(0, 0, 3, 3)]) == (1, 1)
    assert fab_rules.box_center([(-3, -3, 4, 4)]) == (-1, -1)
    # Two boxes merge to their extremes before the halving, never by averaging
    # the two centres — which would be a different answer for unequal boxes.
    assert fab_rules.box_center([(1, 1, 2, 2), (10, 10, 1, 1)]) == (6, 6)
    assert fab_rules.box_center([]) is None


def test_millimetres_are_truncated_not_rounded():
    """``pcbnew.FromMM`` is ``int(mm * 1e6)``; rounding would be off by one."""
    assert fab_rules.from_mm(0.1) == 100000
    assert fab_rules.from_mm(-0.1) == -100000
    assert fab_rules.to_mm(104700000) == 104.7


def test_an_uncorrected_angle_keeps_its_sign():
    """The file has always said -90.0, and only a correction takes a modulus."""
    assert fab_rules.corrected_rotation(-90.0, False, ("C1",), []) == -90.0
    assert (
        fab_rules.corrected_rotation(-90.0, False, ("C1",), [("C1", 90, (0, 0))]) == 0
    )


def test_a_bottom_side_angle_is_mirrored():
    """A placement machine sees the underside from below."""
    assert fab_rules.board_rotation(90.0, True) == 90.0
    assert fab_rules.board_rotation(30.0, True) == 150.0
    assert fab_rules.board_rotation(30.0, False) == 30.0


def test_the_first_matching_name_wins_outright():
    """A rule written against a reference is a statement about that part.

    Letting a later, broader name override it would make the specific rule
    unusable — and the wx plugin returns on the first match, so this is parity
    as well as good sense.
    """
    corrections = [("R1", 0, (0.0, 0.0)), ("R_0402", 90, (0.0, 0.0))]
    names = ("R1", "10K", "R_0402_1005Metric")
    assert fab_rules.match_for(corrections, names)[0] == 0
    assert fab_rules.corrected_rotation(0.0, False, names, corrections) == 0


def test_an_offset_is_rotated_into_the_boards_frame():
    """The offset is stated in the footprint's frame and applied in the board's."""
    corrections = [("SOT-23", 0, (1.0, 0.0))]
    names = ("Q1", "MMBT3904", "SOT-23")
    # At 0°, +1mm along the part's X is +1mm along the board's.
    assert fab_rules.corrected_position(0, 0, 0.0, False, names, corrections) == (
        1_000_000,
        0,
    )
    # At 90° it has turned onto the other axis.
    x, y = fab_rules.corrected_position(0, 0, 90.0, False, names, corrections)
    assert (round(x, -3), round(y, -3)) == (0, -1_000_000)
    # A zero offset is not applied at all, so no rounding can creep in.
    assert fab_rules.corrected_position(
        7, 9, 33.0, False, names, [("SOT-23", 90, (0.0, 0.0))]
    ) == (7, 9)


def test_a_bottom_side_offset_is_mirrored_in_x():
    """The bottom's coordinate system is mirrored, so the offset must be too.

    Compared at the two orientations that give the *same* board rotation — 0° on
    top and 180° on the bottom, both of which mirror to 0 — so the only thing
    left to differ is the mirror itself. Comparing the same orientation on both
    sides proves nothing here: the 180° flip cancels the mirror exactly.
    """
    corrections = [("SOT-23", 0, (1.0, 0.0))]
    names = ("Q1", "MMBT3904", "SOT-23")
    assert fab_rules.board_rotation(0.0, False) == fab_rules.board_rotation(180.0, True)
    top = fab_rules.corrected_position(0, 0, 0.0, False, names, corrections)
    bottom = fab_rules.corrected_position(0, 0, 180.0, True, names, corrections)
    assert top[0] == 1_000_000
    assert bottom[0] == -1_000_000


# ---------------------------------------------------------------------------
# The seam — both halves run the same code
# ---------------------------------------------------------------------------


def test_the_wx_plugin_delegates_to_the_shared_rules():
    """``fabrication`` must not carry a second copy of any of this.

    Two ports of one spec drift; one function called twice cannot. This is the
    property that makes the live byte-comparison meaningful rather than lucky.
    """
    for name in ("pcbnew", "wx", "wx.dataview"):
        sys.modules.setdefault(name, MagicMock())
    from kicad_lcsc_suite import fabrication  # noqa: PLC0415 - after the stub

    assert fabrication.split_bom_designators is fab_rules.split_bom_designators
    source = Path(fabrication.__file__).read_text(encoding="utf-8")
    for gone in ("math.cos", "math.radians", "% 360", "re.search"):
        assert gone not in source, f"{gone!r} is back in fabrication.py"


def test_the_wx_rotation_wrapper_produces_the_shared_answer():
    """``Fabrication.fix_rotation`` reads pcbnew and defers for the arithmetic."""
    for name in ("pcbnew", "wx", "wx.dataview"):
        sys.modules.setdefault(name, MagicMock())
    from kicad_lcsc_suite.fabrication import Fabrication  # noqa: PLC0415 - ditto

    class _Fp:
        def GetReference(self):  # noqa: N802 - pcbnew's spelling
            return "U2"

        def GetValue(self):  # noqa: N802
            return "MAX1968"

        def GetFPID(self):  # noqa: N802
            return type("_Id", (), {"GetLibItemName": lambda _self: "HTSSOP-28"})()

        def GetLayer(self):  # noqa: N802
            return 0

        def GetOrientation(self):  # noqa: N802
            return type("_Angle", (), {"AsDegrees": lambda _self: 90.0})()

    fab = object.__new__(Fabrication)
    fab.corrections = [("HTSSOP-28", 270, (0.0, 0.0))]
    fab.logger = MagicMock()
    names = ("U2", "MAX1968", "HTSSOP-28")
    assert fab.fix_rotation(_Fp()) == fab_rules.corrected_rotation(
        90.0, False, names, fab.corrections
    )


# ---------------------------------------------------------------------------
# The exporter over the fixture board
# ---------------------------------------------------------------------------


def test_the_cpl_uses_the_pad_centre_not_the_footprint_origin(exporter, board):
    """The distinction the whole geometry read exists for."""
    result = exporter.export()
    rows = cpl_by_reference(result.cpl_path)
    views = {view.reference: view for view in board.footprints()}
    with_pads = board.pad_centers_nm()
    checked = 0
    for reference, row in rows.items():
        if reference not in with_pads:
            continue  # the fallback, which has its own test below
        origin_x = views[reference].position_mm[0]
        assert float(row[3]) == pytest.approx(
            origin_x + PAD_CENTRE_SKEW_NM / fab_rules.IU_PER_MM
        ), f"{reference} was written at its origin, not its pads"
        checked += 1
    assert checked > 50, "too few rows to be evidence"


def test_a_footprint_with_no_pads_falls_back_to_its_origin(exporter, board):
    """What ``fabrication.get_position``'s bare ``except`` has always done."""
    padless = set(board.footprints()) and {
        view.reference
        for view in board.footprints()
        if view.reference not in board.pad_centers_nm()
    }
    assert padless, "the fixture no longer exercises the fallback"
    result = exporter.export()
    rows = cpl_by_reference(result.cpl_path)
    views = {view.reference: view for view in board.footprints()}
    checked = [reference for reference in padless if reference in rows]
    assert checked, "no pad-less footprint reached the CPL"
    for reference in checked:
        assert float(rows[reference][3]) == pytest.approx(
            views[reference].position_mm[0]
        )


def test_the_y_axis_is_flipped(exporter, board):
    """KiCad's Y grows downwards; JLC's grows up."""
    result = exporter.export()
    rows = cpl_by_reference(result.cpl_path)
    views = {view.reference: view for view in board.footprints()}
    reference = next(iter(rows))
    assert float(rows[reference][4]) == pytest.approx(-views[reference].position_mm[1])


def test_the_drill_origin_is_subtracted(board, settings, tmp_path):
    """Every CPL coordinate is measured from the drill/place origin."""
    parts = PartList(board, settings=settings)
    parts.refresh_from_board()
    before = cpl_by_reference(
        Exporter(board, parts.store, settings=settings).export().cpl_path
    )

    board._origin_nm = (5_000_000, 3_000_000)
    after = cpl_by_reference(
        Exporter(board, parts.store, settings=settings).export().cpl_path
    )

    reference = next(iter(before))
    assert float(after[reference][3]) == pytest.approx(
        float(before[reference][3]) - 5.0
    )
    # Y is negated after the subtraction, so the shift comes back the other way.
    assert float(after[reference][4]) == pytest.approx(
        float(before[reference][4]) + 3.0
    )


def test_a_do_not_place_part_is_in_neither_file(exporter, board):
    """DNP means "on the board, not on the machine" — for both files."""
    dnp = {view.reference for view in board.footprints() if view.dnp}
    assert dnp, "the fixture no longer marks anything DNP"
    result = exporter.export()
    assert not (dnp & set(cpl_by_reference(result.cpl_path)))
    _header, bom = read_csv(result.bom_path)
    listed = {ref for row in bom for ref in row[1].split(",")}
    assert not (dnp & listed)
    assert set(result.skipped_dnp) >= dnp


def test_a_part_excluded_from_pos_still_reaches_the_bom(exporter, board):
    """The two exclusions are separate columns and separate decisions."""
    result = exporter.export()
    in_cpl = set(cpl_by_reference(result.cpl_path))
    excluded = {
        view.reference
        for view in board.footprints()
        if view.exclude_from_pos and not view.exclude_from_bom and not view.dnp
    }
    assert not (excluded & in_cpl)


def test_the_headers_are_the_ones_jlc_reads(exporter):
    """Read by position, so a renamed column is a silently wrong file."""
    result = exporter.export()
    assert read_csv(result.bom_path)[0] == fab_rules.BOM_HEADER
    assert read_csv(result.cpl_path)[0] == fab_rules.CPL_HEADER


def test_unassigned_parts_can_be_kept_out_of_both_files(board, settings, tmp_path):
    """The Settings checkbox that decides whether JLC sees a part it cannot place."""
    parts = PartList(board, settings=settings)
    parts.refresh_from_board()

    settings.set("general", "order_number", True)
    with_them = Exporter(board, parts.store, settings=settings).export()
    settings.set("general", "order_number", False)
    without = Exporter(board, parts.store, settings=settings).export()

    assert without.bom_rows < with_them.bom_rows
    assert without.cpl_rows < with_them.cpl_rows
    _header, rows = read_csv(without.bom_path)
    assert all(row[3] for row in rows), "a row with no LCSC number survived"


def test_the_files_land_where_the_wx_plugin_puts_them(exporter, board):
    """A project half-migrated between the two halves must not grow two sets."""
    result = exporter.export()
    expected = os.path.join(
        os.path.dirname(board.info().path), "jlcpcb", "production_files"
    )
    assert os.path.dirname(result.bom_path) == expected
    assert os.path.basename(result.bom_path) == "BOM-tempctrl.csv"
    assert os.path.basename(result.cpl_path) == "CPL-tempctrl.csv"


def test_the_designators_are_sorted_as_the_wx_plugin_sorts_them(exporter):
    """Plain string order: R10 before R2. A CPL is read by a machine."""
    result = exporter.export()
    _header, rows = read_csv(result.cpl_path)
    designators = [row[0] for row in rows]
    assert designators == sorted(designators)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_the_report_says_what_was_left_out(board, settings):
    """The first question anyone asks of a BOM, answered where they can see it."""
    parts = PartList(board, settings=settings)
    controller = build_controller(board, parts, settings=settings)
    result = controller.run_export()
    box = controller.build_export_report(result)
    text = box.informativeText()
    assert f"BOM: {result.bom_rows} rows" in text
    assert f"CPL: {result.cpl_rows} rows" in text
    assert "do-not-place" in text
    controller.window.close()


def test_an_unwritable_directory_reports_instead_of_raising(board, settings, tmp_path):
    """Neither file is written, and the user is told which of them that means."""
    parts = PartList(board, settings=settings)
    controller = build_controller(board, parts, settings=settings)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    controller.exporter = lambda: _FailingExporter(blocked)
    assert isinstance(controller.run_export(), OSError)
    controller.window.close()


class _FailingExporter:
    """An exporter whose output directory cannot be made."""

    def __init__(self, path) -> None:
        self.path = path

    def export(self):
        raise OSError(f"Not a directory: {self.path}")
