"""Module for reading assigned LCSC numbers out of the schematic.

The mirror of :mod:`schematicexport`. A schematic is very often *ahead* of the
board: parts get their LCSC field in eeschema, or a design arrives with the
numbers already in the symbols, and the footprints on the board carry nothing.
The plugin only ever looked at footprints, so those boards came up looking
entirely unassigned. This reads the ``.kicad_sch`` files so the numbers can be
pulled the other way.

Reading is done on the file, not on eeschema's in-memory copy, so a schematic
with unsaved changes hands back the numbers as they were last *saved*. The
caller is told which sheets were open (``ImportResult.locked``) so it can say
so rather than quietly importing stale values.

Deliberately free of ``wx`` and ``pcbnew``: what a symbol says is a question
about a text file, and keeping it that way makes the parser directly testable.
"""

import logging
import os
import os.path
import re
from typing import Dict, List, Set

from .schematicexport import is_lcsc_field, is_open_in_editor

logger = logging.getLogger(__name__)

#: An LCSC number and nothing else. The same shape ``footprint_helpers``
#: demands of a footprint field: a schematic field holding a description, a
#: URL or "TBD" is not an assignment and must not be imported as one.
LCSC_VALUE_RX = re.compile(r"^C\d+$")

#: ``(property "Name" "value"`` in any of the v6/v7/v8+ layouts. Reading needs
#: neither the field id nor its position, so unlike the exporter this one
#: pattern covers every file format the plugin supports.
_PROPERTY_RX = re.compile(r'\(property\s+"([^"]*)"\s+"([^"]*)"')


class ImportResult:
    """What one read of the schematic found."""

    def __init__(self):
        #: reference -> LCSC number, for symbols that carry a usable one
        self.numbers: Dict[str, str] = {}
        #: every reference seen, whether or not it has a number. Lets the
        #: caller tell "the schematic has no number for R1" apart from "the
        #: schematic has no R1".
        self.references: Set[str] = set()
        self.read: List[str] = []
        self.missing: List[str] = []
        self.locked: List[str] = []

    def summary(self) -> str:
        """One-line human readable description of the read."""
        if not self.read:
            return "No schematic could be read"
        return (
            f"Read {len(self.references)} symbol(s), "
            f"{len(self.numbers)} with an LCSC number, from "
            + ", ".join(os.path.basename(path) for path in self.read)
        )


def _starts_symbol_instance(line: str, next_line: str) -> bool:
    """Whether this line opens a placed symbol rather than a library one.

    Instances are the only ones with a ``lib_id`` — on the same line in v6/v7,
    on the next one from v8. Everything inside ``lib_symbols`` is a definition
    whose "Reference" property is a prefix like ``C``, not a real reference.
    """
    if "(symbol" not in line:
        return False
    return "(lib_id" in line or "(lib_id" in next_line


def _read_sheet(path: str, result: ImportResult, files_seen: Set[str]) -> None:
    """Collect the LCSC fields of one sheet, then of the sheets below it."""
    # A sheet can be instantiated more than once in a hierarchy, and the file
    # behind every instance is the same file.
    real_path = os.path.realpath(path)
    if real_path in files_seen:
        return
    files_seen.add(real_path)

    if not os.path.isfile(path):
        logger.warning("Schematic %s does not exist, skipping", path)
        result.missing.append(path)
        return
    if is_open_in_editor(path):
        # Not a reason to stop: the file is perfectly readable, it is just
        # potentially behind whatever is on screen in eeschema.
        result.locked.append(path)

    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    result.read.append(path)

    reference = ""
    lcsc = ""
    in_symbol = False

    def flush():
        """Record the symbol that just ended."""
        if not reference:
            return
        result.references.add(reference)
        if lcsc:
            # Last one wins, matching the exporter: it updates every LCSC-ish
            # field it passes, so the final value is what the file settles on.
            result.numbers[reference] = lcsc

    for index, raw in enumerate(lines):
        line = raw.rstrip()
        next_line = lines[index + 1].rstrip() if index + 1 < len(lines) else ""

        if _starts_symbol_instance(line, next_line):
            flush()
            reference = ""
            lcsc = ""
            in_symbol = True

        m = _PROPERTY_RX.search(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)

        # Sub-sheets are followed whether or not this sheet has symbols of its
        # own — a top sheet that is nothing but a hierarchy has none.
        if key == "Sheetfile":
            _read_sheet(os.path.join(os.path.dirname(path), value), result, files_seen)
        elif in_symbol:
            if key == "Reference":
                reference = value
            elif is_lcsc_field(key):
                if not value:
                    lcsc = ""
                elif LCSC_VALUE_RX.match(value):
                    lcsc = value
                else:
                    logger.debug(
                        "Ignoring %s field %r on %s: not an LCSC number",
                        key,
                        value,
                        reference or "an unnamed symbol",
                    )

    flush()


def read_schematic(paths) -> ImportResult:
    """Read the LCSC fields of the given schematics and their sub-sheets."""
    result = ImportResult()
    files_seen: Set[str] = set()
    for path in paths:
        _read_sheet(path, result, files_seen)
    return result


class SchematicDiff:
    """The changes importing a schematic would make to the board.

    Split three ways because they need three different things said about them:
    an addition is free, a replacement destroys a number the user may have
    picked by hand, and a reference the board does not have cannot be applied
    at all.
    """

    def __init__(self):
        #: (reference, incoming number) for footprints with no number yet
        self.added: List[tuple] = []
        #: (reference, current number, incoming number) — these get overwritten
        self.replaced: List[tuple] = []
        #: references in the schematic that no footprint on the board has
        self.unknown: List[str] = []
        #: references where board and schematic already agree
        self.unchanged: List[str] = []

    @property
    def changes(self) -> int:
        """Number of footprints that would be written."""
        return len(self.added) + len(self.replaced)

    def assignments(self) -> Dict[str, str]:
        """Map each reference that would change to the number to write."""
        numbers = dict(self.added)
        numbers.update({reference: lcsc for reference, _current, lcsc in self.replaced})
        return numbers


def diff_against_board(numbers: Dict[str, str], board: Dict[str, str]) -> SchematicDiff:
    """Work out what importing ``numbers`` would do to the board.

    ``board`` maps every reference the board has to its current LCSC number,
    empty string included — that is what distinguishes an unassigned footprint
    from one that is not on this board at all.
    """
    diff = SchematicDiff()
    for reference in sorted(numbers):
        lcsc = numbers[reference]
        if reference not in board:
            diff.unknown.append(reference)
        elif board[reference] == lcsc:
            diff.unchanged.append(reference)
        elif board[reference]:
            diff.replaced.append((reference, board[reference], lcsc))
        else:
            diff.added.append((reference, lcsc))
    return diff
