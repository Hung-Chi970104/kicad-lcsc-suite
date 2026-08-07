"""Tests for the IPC bridge, and for the read-back that makes it trustworthy.

The migration plan calls trap 2 the highest-severity risk in the whole project:
``board.update_items(field)`` returns success and changes nothing, and neither
spelling raises. A write path that trusts a return value is therefore a silent
data-loss bug — the user assigns a part, the UI says it worked, and the board
never learns about it.

So the point of this file is not "does assigning work". It is:

* ``test_silently_ignored_write_is_caught`` — with a backend that behaves
  exactly like the trap (update_items succeeds, board unchanged), does the
  bridge notice?
* ``test_failed_verification_leaves_board_unchanged`` — and does it leave
  nothing behind when it does?
* ``test_edit_without_expectation_is_rejected`` — can a future write helper be
  written that forgets to verify at all?

Everything else here is the ordinary reading and writing those three protect.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lcsc_suite import kicad_bridge as bridge

FIXTURE = (
    Path(__file__).resolve().parent.parent / "lcsc_suite" / "fixtures" / "board.json"
)


@pytest.fixture
def payload():
    """Return the committed fixture board, as a dict."""
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def board(payload):
    """Return a fixture board that honours writes, i.e. a well-behaved API."""
    return bridge.FixtureBoard.from_dict(copy.deepcopy(payload))


@pytest.fixture
def trapped_board(payload):
    """Return a board that reproduces trap 2: writes succeed, nothing changes."""
    return bridge.FixtureBoard.from_dict(
        copy.deepcopy(payload), honour_footprint_writes=False
    )


# ---------------------------------------------------------------------------
# The traps
# ---------------------------------------------------------------------------


def test_silently_ignored_write_is_caught(trapped_board):
    """A write that changes nothing must raise, not report success.

    This is trap 2. The backend here returns normally from ``update_items`` and
    leaves the board alone, which is precisely what the real API does when the
    write targets a child object instead of its parent footprint.
    """
    with pytest.raises(bridge.WriteVerificationError) as raised:
        trapped_board.set_lcsc({"C1": "C111111"})

    message = str(raised.value)
    assert "C1" in message
    assert "lcsc" in message
    # The message has to say what it expected, or the next person reads it as a
    # network blip and retries.
    assert "C111111" in message


def test_failed_verification_leaves_board_unchanged(trapped_board):
    """A failed write is put back; the board must read exactly as before."""
    before = {fp.reference: fp for fp in trapped_board.footprints()}

    with pytest.raises(bridge.WriteVerificationError):
        trapped_board.set_lcsc({"C1": "C111111", "C2": "C222222"})

    after = {fp.reference: fp for fp in trapped_board.footprints(refresh=True)}
    assert after == before


def test_a_failed_write_costs_two_undo_entries(trapped_board):
    """Trap 4's price, stated so a change to it is deliberate.

    A read cannot see an open commit, so the write has to be pushed before it
    can be verified — which means a failed one is undone by a *second* commit
    rather than by dropping the first. The board ends up unchanged either way;
    KiCad's undo history does not.
    """
    with pytest.raises(bridge.WriteVerificationError):
        trapped_board.set_lcsc({"C1": "C111111"})

    assert len(trapped_board.commits) == 2
    assert "Undo" in trapped_board.commits[1]


def test_a_failed_write_says_the_board_was_put_back(trapped_board):
    """The user's next question is "what state is my board in now?"."""
    with pytest.raises(bridge.WriteVerificationError) as raised:
        trapped_board.set_lcsc({"C1": "C111111"})

    assert "put back" in str(raised.value)


def test_an_open_commit_is_not_visible_to_a_read(board):
    """Trap 4 itself, in the fixture that has to reproduce it.

    If this ever passes with the assertion inverted, the fixture has become more
    permissive than the API and stops being evidence about it.
    """
    commit = board._begin()
    footprint = board._live_footprint("C1")
    board._lcsc_mutator("C1", "C111111")(footprint)
    board._commit(footprint)

    assert board.footprint("C1").lcsc != "C111111"

    board._push(commit, "probe")
    board.footprints(refresh=True)
    assert board.footprint("C1").lcsc == "C111111"


def test_edit_without_expectation_is_rejected():
    """An Edit that states nothing to verify cannot be constructed.

    The read-back is only load-bearing if no write helper can skip it, so the
    requirement lives in the type rather than in a review comment.
    """
    with pytest.raises(ValueError, match="nothing to verify"):
        bridge.Edit(reference="C1", mutate=lambda fp: None, expect={})


def test_commit_refuses_a_non_footprint(board):
    """``update_items`` must only ever be handed a parent footprint."""
    with pytest.raises(TypeError, match="trap 2"):
        board._commit(object())


def test_creating_a_field_is_verified_too(board):
    """Assigning to a footprint with no LCSC field at all still read-backs.

    Trap 3 — the field lives in the footprint's *definition*, not on the
    footprint — and the creation path is the one most likely to look successful
    while writing nowhere.
    """
    unassigned = next(fp for fp in board.footprints() if not fp.lcsc_field)
    board.set_lcsc({unassigned.reference: "C404"})

    after = board.footprint(unassigned.reference)
    assert after.lcsc == "C404"
    assert after.lcsc_field == "LCSC"
    # The wx plugin hides the field it creates; a visible C-number printed on
    # the silkscreen of every part is not what anyone wants.
    assert after.lcsc_visible is False


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_reads_the_whole_board(board):
    """Every valid footprint is listed, with the fields the part list needs."""
    footprints = board.footprints()
    assert len(footprints) == 110

    by_ref = {fp.reference: fp for fp in footprints}
    assert by_ref["C1"].value == "10uF"
    assert by_ref["C1"].footprint == "C_1206_3216Metric"
    assert by_ref["C1"].lcsc == "C13585"
    assert by_ref["C1"].assigned is True
    assert by_ref["B1"].assigned is False


def test_mechanical_parts_carry_their_exclusions(board):
    """BOM/POS exclusions read straight off the footprint attributes."""
    mounting_hole = board.footprint("B1")
    assert mounting_hole.exclude_from_bom is True
    assert mounting_hole.exclude_from_pos is True

    capacitor = board.footprint("C1")
    assert capacitor.exclude_from_bom is False
    assert capacitor.exclude_from_pos is False


def test_position_and_rotation_are_available_for_the_cpl(board):
    """The CPL needs position in mm and rotation in degrees."""
    part = board.footprint("C1")
    assert part.position_mm == (104.7, 80.725)
    assert part.orientation_deg == 90.0
    assert part.side == "top"


def test_a_view_is_a_snapshot_not_a_handle(board):
    """Mutating a returned view must not change the board."""
    view = board.footprint("C1")
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass, any raise will do
        view.lcsc = "C999999"


def test_unknown_reference_is_an_error_not_a_none(board):
    """Asking for a footprint that is not there fails loudly."""
    with pytest.raises(bridge.BridgeError, match="NOPE"):
        board.footprint("NOPE")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_assigns_and_clears_lcsc_numbers(board):
    """The ordinary path: set a number, then clear it."""
    board.set_lcsc({"C1": "C444444"})
    assert board.footprint("C1").lcsc == "C444444"

    board.set_lcsc({"C1": ""})
    assert board.footprint("C1").lcsc == ""


def test_rejects_something_that_is_not_an_lcsc_number(board):
    """Free text is not an assignment and must not reach the board."""
    with pytest.raises(ValueError, match="not an LCSC number"):
        board.set_lcsc({"C1": "10uF 0402"})
    assert board.footprint("C1").lcsc == "C13585"


def test_a_batch_is_one_undo_step(board):
    """Assigning several references produces a single commit."""
    board.set_lcsc({"C1": "C1", "C2": "C2", "C3": "C3"})
    assert len(board.commits) == 1
    assert "3 footprints" in board.commits[0]


def test_a_single_write_names_the_reference_in_its_undo_entry(board):
    """One change reads better in KiCad's undo history if it says what it was."""
    board.set_lcsc({"C1": "C1"})
    assert board.commits == ["Set LCSC: C1 -> C1"]


def test_toggles_bom_and_pos_exclusions(board):
    """BOM/POS toggles round-trip through the board."""
    board.set_exclude_from_bom({"C1": True})
    assert board.footprint("C1").exclude_from_bom is True

    board.set_exclude_from_pos({"C1": True})
    assert board.footprint("C1").exclude_from_pos is True

    board.set_exclude_from_bom({"C1": False})
    board.set_exclude_from_pos({"C1": False})
    refreshed = board.footprint("C1")
    assert refreshed.exclude_from_bom is False
    assert refreshed.exclude_from_pos is False


def test_an_empty_batch_is_not_a_commit(board):
    """Nothing to do means no undo entry."""
    board.set_lcsc({})
    assert board.commits == []


def test_reuses_an_existing_jlc_field_rather_than_adding_a_second(payload):
    """A board using ``JLC_PN`` keeps using it.

    Two fields carrying the same fact is how a round trip through the schematic
    starts to drift, and ``schematicexport`` shares this vocabulary for exactly
    that reason.
    """
    payload = copy.deepcopy(payload)
    for row in payload["footprints"]:
        if row["reference"] == "C1":
            row["lcsc"] = ""
            row["fields"] = [{"name": "JLC_PN", "value": "C13585", "visible": False}]
    board = bridge.FixtureBoard.from_dict(payload)

    assert board.footprint("C1").lcsc_field == "JLC_PN"
    board.set_lcsc({"C1": "C555555"})
    after = board.footprint("C1")
    assert after.lcsc_field == "JLC_PN"
    assert after.lcsc == "C555555"


def test_free_text_in_an_lcsc_field_reads_as_unassigned(payload):
    """A field named LCSC holding rubbish is not an assignment.

    But it is still the field to write into, so the number lands there rather
    than in a second field beside it.
    """
    payload = copy.deepcopy(payload)
    for row in payload["footprints"]:
        if row["reference"] == "C1":
            row["lcsc"] = ""
            row["fields"] = [{"name": "LCSC", "value": "see BOM", "visible": False}]
    board = bridge.FixtureBoard.from_dict(payload)

    part = board.footprint("C1")
    assert part.lcsc == ""
    assert part.lcsc_field == "LCSC"

    board.set_lcsc({"C1": "C13585"})
    assert board.footprint("C1").lcsc == "C13585"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_environment_report_leads_with_pythonhome(monkeypatch):
    """Trap 1's variable is named first, and the token is never echoed."""
    monkeypatch.setenv("PYTHONHOME", "/Applications/KiCad/…/Python.framework")
    monkeypatch.setenv("KICAD_API_TOKEN", "hunter2")
    report = bridge.environment_report()

    assert list(report)[0] == "PYTHONHOME"
    assert report["PYTHONHOME"].endswith("Python.framework")
    assert report["KICAD_API_TOKEN"] == "set"
    assert "hunter2" not in json.dumps(report)


def test_board_info_guesses_the_schematic_name(board):
    """The schematic filename is derived the same way the wx plugin derives it."""
    assert board.info().schematic_name == "tempctrl.kicad_sch"
