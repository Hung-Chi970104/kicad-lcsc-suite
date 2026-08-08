"""Tests for writing assigned LCSC numbers back into ``.kicad_sch`` files."""

from pathlib import Path

import pytest

from lcsc_suite import schematicexport
from lcsc_suite.schematicexport import (
    SchematicExport,
    find_root_schematic,
    is_open_in_editor,
    lock_file_for,
    set_property_value,
)

# The version picks which of the three file-format branches runs. In KiCad it
# comes from ``pcbnew.GetBuildVersion``, which schematicexport binds at import
# time — so pin the bound name rather than stubbing ``sys.modules["pcbnew"]``.
# Whichever test module imported schematicexport first would otherwise decide
# this one's answer, and several of them install a bare ``MagicMock`` whose
# version string is a mock the format check cannot compare.
schematicexport.GetBuildVersion = lambda: "10.0.3"


def symbol(reference: str, lcsc=None) -> str:
    """Build one KiCad 8+ schematic symbol instance, optionally with a LCSC field."""
    lcsc_property = ""
    if lcsc is not None:
        lcsc_property = f"""		(property "LCSC" "{lcsc}"
			(at 10.16 20.32 0)
			(effects
				(font
					(size 1.27 1.27)
				)
				(hide yes)
			)
		)
"""
    return f"""	(symbol
		(lib_id "Device:C")
		(at 10.16 20.32 0)
		(unit 1)
		(property "Reference" "{reference}"
			(at 12.7 17.78 0)
			(effects
				(font
					(size 1.27 1.27)
				)
			)
		)
		(property "Value" "10uF"
			(at 12.7 20.32 0)
			(effects
				(font
					(size 1.27 1.27)
				)
			)
		)
{lcsc_property}		(pin "1"
			(uuid "00000000-0000-0000-0000-000000000001")
		)
		(pin "2"
			(uuid "00000000-0000-0000-0000-000000000002")
		)
	)
"""


def schematic(*symbols: str, sheet_file=None) -> str:
    """Wrap symbol blocks in a minimal KiCad 8+ schematic, optionally with a sub-sheet."""
    sheet = ""
    if sheet_file:
        sheet = f"""	(sheet
		(at 100 100)
		(property "Sheetname" "sub"
			(at 100 99 0)
		)
		(property "Sheetfile" "{sheet_file}"
			(at 100 106 0)
		)
	)
"""
    return (
        '(kicad_sch\n\t(version 20260306)\n\t(generator "eeschema")\n'
        + "".join(symbols)
        + sheet
        + ")\n"
    )


def write_schematic(tmp_path: Path, name: str, content: str) -> Path:
    """Write a schematic file and return its path."""
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class FakeStore:
    """Minimal Store stand-in exposing only what the exporter reads."""

    def __init__(self, parts):
        self.parts = parts

    def read_all(self):
        """Return the canned part rows."""
        return self.parts


class FakeParent:
    """Stand-in for the main window."""

    def __init__(self, parts=()):
        self.store = FakeStore(list(parts))


def lcsc_fields(path: Path):
    """Return the LCSC field values of a schematic, in file order."""
    return [
        line.split('"')[3]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith('(property "LCSC"')
    ]


def test_updates_an_existing_lcsc_field(tmp_path):
    """An assignment overwrites the number the symbol already carries."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1", "C111")))

    result = SchematicExport(FakeParent(), {"C1": "C222"}).load_schematic([str(path)])

    assert lcsc_fields(path) == ["C222"]
    assert (result.updated, result.added, result.cleared) == (1, 0, 0)
    assert result.written == [str(path)]


def test_adds_a_missing_lcsc_field(tmp_path):
    """A symbol without the field gets one, hidden, next to its reference."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1")))

    result = SchematicExport(FakeParent(), {"C1": "C222"}).load_schematic([str(path)])

    assert lcsc_fields(path) == ["C222"]
    assert (result.updated, result.added, result.cleared) == (0, 1, 0)
    assert "(hide yes)" in path.read_text(encoding="utf-8")


def test_explicit_clear_empties_the_field(tmp_path):
    """An empty assignment blanks the field rather than leaving a stale number."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1", "C111")))

    result = SchematicExport(FakeParent(), {"C1": ""}).load_schematic([str(path)])

    assert lcsc_fields(path) == [""]
    assert (result.updated, result.added, result.cleared) == (0, 0, 1)


def test_cleared_field_is_not_duplicated_on_reassignment(tmp_path):
    """Re-assigning after a clear reuses the empty field instead of adding one."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1", "")))

    SchematicExport(FakeParent(), {"C1": "C222"}).load_schematic([str(path)])

    assert lcsc_fields(path) == ["C222"]


def test_references_not_listed_are_left_alone(tmp_path):
    """A number the schematic has but the board does not is never wiped."""
    path = write_schematic(
        tmp_path,
        "board.kicad_sch",
        schematic(symbol("C1", "C111"), symbol("C2", "C333")),
    )

    result = SchematicExport(FakeParent(), {"C1": "C222"}).load_schematic([str(path)])

    assert lcsc_fields(path) == ["C222", "C333"]
    assert result.changes == 1


def test_unchanged_schematic_is_not_rewritten(tmp_path):
    """A no-op sync leaves the file and its mtime alone and writes no backup."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1", "C111")))
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns

    result = SchematicExport(FakeParent(), {"C1": "C111"}).load_schematic([str(path)])

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime
    assert not (tmp_path / "board.kicad_sch_old").exists()
    assert result.written == []
    assert result.changes == 0


def test_previous_content_is_kept_as_a_backup(tmp_path):
    """A rewritten sheet leaves the old one behind as <name>_old."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1", "C111")))
    before = path.read_text(encoding="utf-8")

    SchematicExport(FakeParent(), {"C1": "C222"}).load_schematic([str(path)])

    assert (tmp_path / "board.kicad_sch_old").read_text(encoding="utf-8") == before


def test_sub_sheets_are_followed(tmp_path):
    """Symbols on a hierarchical sub-sheet are updated too."""
    write_schematic(tmp_path, "sub.kicad_sch", schematic(symbol("C2")))
    root = write_schematic(
        tmp_path,
        "board.kicad_sch",
        schematic(symbol("C1", "C111"), sheet_file="sub.kicad_sch"),
    )

    result = SchematicExport(FakeParent(), {"C1": "C222", "C2": "C333"}).load_schematic(
        [str(root)]
    )

    assert lcsc_fields(root) == ["C222"]
    assert lcsc_fields(tmp_path / "sub.kicad_sch") == ["C333"]
    assert result.changes == 2
    assert len(result.written) == 2


def test_sub_sheets_of_a_symbol_less_root_are_written(tmp_path):
    """A root sheet that is nothing but a hierarchy still leads somewhere.

    Its own symbol list is empty, which used to stop the walk before it
    followed a single Sheetfile — leaving the whole design unwritten.
    """
    write_schematic(tmp_path, "sub.kicad_sch", schematic(symbol("C2")))
    root = write_schematic(
        tmp_path, "board.kicad_sch", schematic(sheet_file="sub.kicad_sch")
    )

    result = SchematicExport(FakeParent(), {"C2": "C333"}).load_schematic([str(root)])

    assert lcsc_fields(tmp_path / "sub.kicad_sch") == ["C333"]
    assert result.written == [str(tmp_path / "sub.kicad_sch")]


def test_a_lowercase_field_name_is_updated_not_duplicated(tmp_path):
    """Matching the field name case-insensitively avoids a second LCSC field."""
    path = write_schematic(
        tmp_path,
        "board.kicad_sch",
        schematic(symbol("C1", "C111")).replace('"LCSC"', '"Lcsc"'),
    )

    result = SchematicExport(FakeParent(), {"C1": "C222"}).load_schematic([str(path)])

    assert lcsc_fields(path) == []  # the field is named "Lcsc" now
    assert '(property "Lcsc" "C222"' in path.read_text(encoding="utf-8")
    assert (result.updated, result.added) == (1, 0)


def test_missing_sub_sheet_is_reported_not_raised(tmp_path):
    """A dangling Sheetfile reference is recorded and the rest still syncs."""
    root = write_schematic(
        tmp_path,
        "board.kicad_sch",
        schematic(symbol("C1"), sheet_file="gone.kicad_sch"),
    )

    result = SchematicExport(FakeParent(), {"C1": "C222"}).load_schematic([str(root)])

    assert lcsc_fields(root) == ["C222"]
    assert result.missing == [str(tmp_path / "gone.kicad_sch")]


def test_schematic_open_in_the_editor_is_not_written(tmp_path):
    """A locked schematic is skipped: the editor would overwrite us anyway."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1", "C111")))
    Path(lock_file_for(str(path))).write_text("{}", encoding="utf-8")
    before = path.read_bytes()

    result = SchematicExport(FakeParent(), {"C1": "C222"}).load_schematic([str(path)])

    assert path.read_bytes() == before
    assert result.skipped_locked == [str(path)]
    assert result.changes == 0


def test_locked_schematic_is_written_when_skipping_is_off(tmp_path):
    """The explicit "write anyway" path ignores the lock."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1", "C111")))
    Path(lock_file_for(str(path))).write_text("{}", encoding="utf-8")

    result = SchematicExport(
        FakeParent(), {"C1": "C222"}, skip_locked=False
    ).load_schematic([str(path)])

    assert lcsc_fields(path) == ["C222"]
    assert result.skipped_locked == []


def test_assignments_default_to_the_store(tmp_path):
    """Without an explicit mapping the store supplies the numbers."""
    path = write_schematic(
        tmp_path,
        "board.kicad_sch",
        schematic(symbol("C1", "C111"), symbol("C2", "C333")),
    )
    parent = FakeParent(
        [{"reference": "C1", "lcsc": "C222"}, {"reference": "C2", "lcsc": ""}]
    )

    SchematicExport(parent).load_schematic([str(path)])

    # C2 has no number in the store, which must not clear the schematic's.
    assert lcsc_fields(path) == ["C222", "C333"]


def test_lock_file_naming_matches_kicad(tmp_path):
    """KiCad names its lock ~<file name>.<ext>.lck next to the document."""
    path = tmp_path / "board.kicad_sch"

    assert lock_file_for(str(path)) == str(tmp_path / "~board.kicad_sch.lck")
    assert not is_open_in_editor(str(path))
    Path(lock_file_for(str(path))).write_text("{}", encoding="utf-8")
    assert is_open_in_editor(str(path))


def test_set_property_value_keeps_the_field_name():
    """Only the value is replaced, whatever it and the name contain."""
    line = '\t\t(property "LCSC" "C111"'

    assert set_property_value(line, "C222") == '\t\t(property "LCSC" "C222"'
    assert set_property_value(line, "") == '\t\t(property "LCSC" ""'
    assert (
        set_property_value('\t\t(property "JLC_PN" ""', "C1")
        == '\t\t(property "JLC_PN" "C1"'
    )


def test_root_schematic_is_found_next_to_the_board(tmp_path):
    """The usual case: schematic and board share a name."""
    (tmp_path / "board.kicad_sch").write_text("", encoding="utf-8")

    assert find_root_schematic(str(tmp_path), "board.kicad_pcb") == str(
        tmp_path / "board.kicad_sch"
    )


def test_root_schematic_falls_back_to_the_project_name(tmp_path):
    """A board named differently still resolves through the .kicad_pro."""
    (tmp_path / "project.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / "project.kicad_sch").write_text("", encoding="utf-8")
    (tmp_path / "sub.kicad_sch").write_text("", encoding="utf-8")

    assert find_root_schematic(str(tmp_path), "other.kicad_pcb") == str(
        tmp_path / "project.kicad_sch"
    )


def test_root_schematic_falls_back_to_a_lone_sheet(tmp_path):
    """One schematic in the directory is unambiguous even without a project file."""
    (tmp_path / "only.kicad_sch").write_text("", encoding="utf-8")

    assert find_root_schematic(str(tmp_path), "other.kicad_pcb") == str(
        tmp_path / "only.kicad_sch"
    )


@pytest.mark.parametrize("board", ["board.kicad_pcb", ""])
def test_no_root_schematic_when_ambiguous(tmp_path, board):
    """Several sheets and no project file: the caller has to ask."""
    (tmp_path / "one.kicad_sch").write_text("", encoding="utf-8")
    (tmp_path / "two.kicad_sch").write_text("", encoding="utf-8")

    assert find_root_schematic(str(tmp_path), board) is None
