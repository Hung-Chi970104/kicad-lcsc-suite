"""Tests for reading assigned LCSC numbers out of ``.kicad_sch`` files."""

import importlib.util
from pathlib import Path
import sys
import types

# The exporter tests already stub pcbnew and register the package alias; the
# importer needs the same package in place to resolve its relative import of
# schematicexport, and neither module may be imported as a bare file.
from .test_schematic_sync import lock_file_for, schematic, symbol, write_schematic

_ROOT = Path(__file__).parent.parent

_spec = importlib.util.spec_from_file_location(
    "kicadplugin.schematicimport", _ROOT / "schematicimport.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_mod.__package__ = "kicadplugin"
sys.modules["kicadplugin.schematicimport"] = _mod
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

diff_against_board = _mod.diff_against_board
read_schematic = _mod.read_schematic

assert isinstance(sys.modules["kicadplugin"], types.ModuleType)


def lib_symbols(*definitions: str) -> str:
    """Wrap symbol definitions in the ``lib_symbols`` block a real sheet opens with.

    Definitions carry a "Reference" property too — the prefix, ``C`` — and
    reading it as an assignment would invent a part called ``C``.
    """
    return "\t(lib_symbols\n" + "".join(definitions) + "\t)\n"


def definition(prefix: str, lcsc: str) -> str:
    """One entry of a ``lib_symbols`` block, with a stale LCSC field on it."""
    return f"""		(symbol "Device:C"
			(pin_numbers
				(hide yes)
			)
			(property "Reference" "{prefix}"
				(at 0.635 2.54 0)
			)
			(property "LCSC" "{lcsc}"
				(at 0.635 2.54 0)
			)
		)
"""


def test_reads_the_number_off_a_symbol(tmp_path):
    """The straightforward case: one symbol, one LCSC field."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1", "C111")))

    result = read_schematic([str(path)])

    assert result.numbers == {"C1": "C111"}
    assert result.references == {"C1"}
    assert result.read == [str(path)]


def test_a_symbol_without_a_number_is_still_seen(tmp_path):
    """Knowing the symbol exists is what tells an export it can write to it."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1")))

    result = read_schematic([str(path)])

    assert result.numbers == {}
    assert result.references == {"C1"}


def test_an_empty_field_is_not_a_number(tmp_path):
    """A field that was cleared reads as "no number", not as an empty one."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1", "")))

    result = read_schematic([str(path)])

    assert result.numbers == {}
    assert result.references == {"C1"}


def test_a_field_that_is_not_an_lcsc_number_is_ignored(tmp_path):
    """Free text in the LCSC field must not be assigned to a footprint."""
    path = write_schematic(
        tmp_path, "board.kicad_sch", schematic(symbol("C1", "see datasheet"))
    )

    result = read_schematic([str(path)])

    assert result.numbers == {}
    assert result.references == {"C1"}


def test_library_definitions_are_not_read_as_symbols(tmp_path):
    """``lib_symbols`` holds prefixes like "C", not references."""
    content = schematic(symbol("C1", "C111")).replace(
        '(generator "eeschema")\n',
        '(generator "eeschema")\n' + lib_symbols(definition("C", "C999")),
    )
    path = write_schematic(tmp_path, "board.kicad_sch", content)

    result = read_schematic([str(path)])

    assert result.numbers == {"C1": "C111"}
    assert result.references == {"C1"}


def test_alternative_field_names_are_read(tmp_path):
    """The other spellings an LCSC number has been kept under over the years."""
    content = schematic(symbol("C1", "C111")).replace('"LCSC"', '"JLC_PN"')
    path = write_schematic(tmp_path, "board.kicad_sch", content)

    assert read_schematic([str(path)]).numbers == {"C1": "C111"}


def test_field_name_case_does_not_matter(tmp_path):
    """A schematic spelling the field ``Lcsc`` means the same thing."""
    content = schematic(symbol("C1", "C111")).replace('"LCSC"', '"Lcsc"')
    path = write_schematic(tmp_path, "board.kicad_sch", content)

    assert read_schematic([str(path)]).numbers == {"C1": "C111"}


def test_sub_sheets_are_followed(tmp_path):
    """Symbols on a hierarchical sub-sheet are read too."""
    write_schematic(tmp_path, "sub.kicad_sch", schematic(symbol("C2", "C333")))
    root = write_schematic(
        tmp_path,
        "board.kicad_sch",
        schematic(symbol("C1", "C111"), sheet_file="sub.kicad_sch"),
    )

    result = read_schematic([str(root)])

    assert result.numbers == {"C1": "C111", "C2": "C333"}
    assert len(result.read) == 2


def test_sub_sheets_of_a_symbol_less_root_are_followed(tmp_path):
    """A root sheet that is nothing but a hierarchy still leads somewhere.

    Its own symbol list is empty, which used to be read as "there is nothing
    below here either" and left the whole design unseen.
    """
    write_schematic(tmp_path, "sub.kicad_sch", schematic(symbol("C2", "C333")))
    root = write_schematic(
        tmp_path, "board.kicad_sch", schematic(sheet_file="sub.kicad_sch")
    )

    assert read_schematic([str(root)]).numbers == {"C2": "C333"}


def test_a_sheet_instantiated_twice_is_read_once(tmp_path):
    """The same file behind two instances must not be parsed twice."""
    write_schematic(tmp_path, "sub.kicad_sch", schematic(symbol("C2", "C333")))
    root = write_schematic(
        tmp_path,
        "board.kicad_sch",
        schematic(symbol("C1"), sheet_file="sub.kicad_sch")
        # a second (sheet) block pointing at the same file
        + '\t(sheet\n\t\t(at 200 200)\n\t\t(property "Sheetfile" "sub.kicad_sch"\n'
        "\t\t\t(at 200 206 0)\n\t\t)\n\t)\n",
    )

    result = read_schematic([str(root)])

    assert result.read.count(str(tmp_path / "sub.kicad_sch")) == 1


def test_a_missing_sheet_is_reported_not_raised(tmp_path):
    """A dangling Sheetfile reference is recorded and the rest still reads."""
    root = write_schematic(
        tmp_path,
        "board.kicad_sch",
        schematic(symbol("C1", "C111"), sheet_file="gone.kicad_sch"),
    )

    result = read_schematic([str(root)])

    assert result.numbers == {"C1": "C111"}
    assert result.missing == [str(tmp_path / "gone.kicad_sch")]


def test_a_schematic_open_in_the_editor_is_read_but_flagged(tmp_path):
    """Reading is safe; the caller just has to say the copy may be stale."""
    path = write_schematic(tmp_path, "board.kicad_sch", schematic(symbol("C1", "C111")))
    Path(lock_file_for(str(path))).write_text("{}", encoding="utf-8")

    result = read_schematic([str(path)])

    assert result.numbers == {"C1": "C111"}
    assert result.locked == [str(path)]


def test_a_v6_style_single_line_symbol_is_read(tmp_path):
    """v6/v7 put lib_id and the field id on the property line; same answer."""
    path = tmp_path / "board.kicad_sch"
    path.write_text(
        "(kicad_sch (version 20211123) (generator eeschema)\n"
        '  (symbol (lib_id "Device:C") (at 10 20 0) (unit 1)\n'
        '    (property "Reference" "C1" (id 0) (at 12 17 0))\n'
        '    (property "LCSC" "C111" (id 4) (at 12 25 0))\n'
        '    (pin "1" (uuid 00000000-0000-0000-0000-000000000001))\n'
        "  )\n)\n",
        encoding="utf-8",
    )

    assert read_schematic([str(path)]).numbers == {"C1": "C111"}


def test_diff_splits_additions_from_replacements():
    """The three outcomes the confirmation has to tell apart."""
    numbers = {"C1": "C111", "C2": "C222", "C3": "C333", "C4": "C444"}
    board = {"C1": "", "C2": "C999", "C3": "C333", "R1": ""}

    diff = diff_against_board(numbers, board)

    assert diff.added == [("C1", "C111")]
    assert diff.replaced == [("C2", "C999", "C222")]
    assert diff.unchanged == ["C3"]
    assert diff.unknown == ["C4"]
    assert diff.changes == 2
    assert diff.assignments() == {"C1": "C111", "C2": "C222"}


def test_diff_of_a_board_that_already_agrees_is_empty():
    """Nothing to confirm, nothing to write."""
    diff = diff_against_board({"C1": "C111"}, {"C1": "C111"})

    assert diff.changes == 0
    assert diff.assignments() == {}
