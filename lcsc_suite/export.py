"""Writing the BOM and the CPL — the two files that carry LCSC data.

Phase 6. Not fabrication: Gerber and drill output was dropped from this
migration's scope (see the plan's §1), because another plugin the user already
trusts produces it. These two stay because nothing else can produce them — no
other tool knows this project's LCSC assignments — and because the CPL is what
consumes the rotation and offset corrections, so dropping it would cascade into
deleting the whole Corrections subsystem.

**The rules are not reimplemented here.** They live in
``kicad_lcsc_suite/fab_rules.py`` and the wx plugin runs the same functions, so
the two halves cannot drift apart the way two ports of the same spec would.
What this module owns is the *sourcing*: where a reference, a position and an
angle come from now that there is no ``pcbnew`` to ask.

Three things about that sourcing are worth knowing:

* **The position is the pad-bounding-box centre, not the footprint origin.** A
  footprint's origin is wherever its author put it; the machine needs the middle
  of the part. ``Board.pad_centers_nm`` reads it, and a footprint with no pads
  falls back to the origin — which is what ``fabrication.get_position`` does in
  its bare ``except``.
* **Everything is integer nanometres until the last line.** See ``fab_rules``.
* **The store, not the board, decides what goes in.** ``exclude_from_pos``,
  the grouping, the values and the LCSC numbers are all read from
  ``project.db``, exactly as the wx plugin reads them; the board contributes
  geometry and the do-not-place flag.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
from typing import Optional

from .shared import fab_rules

log = logging.getLogger(__name__)

#: Where JLC expects to find them, and where the wx plugin has always written
#: them: ``<project>/jlcpcb/production_files``. Kept identical so that a project
#: half-migrated between the two halves does not end up with two sets.
OUTPUT_DIR = os.path.join("jlcpcb", "production_files")


@dataclass
class ExportResult:
    """What an export produced, and what it left out.

    The skipped counts are not diagnostics — they are the answer to "why is my
    BOM shorter than my board?", which is the first question anyone asks of a
    file like this, and the wx plugin only answers it in the log pane.
    """

    bom_path: str = ""
    cpl_path: str = ""
    bom_rows: int = 0
    cpl_rows: int = 0
    parts: int = 0
    skipped_dnp: list = field(default_factory=list)
    skipped_no_lcsc: list = field(default_factory=list)
    skipped_no_position: list = field(default_factory=list)
    warnings: str = ""

    def summary(self) -> str:
        """One line naming both files and the counts in them."""
        return (
            f"BOM: {self.bom_rows} rows · CPL: {self.cpl_rows} rows · "
            f"written to {os.path.dirname(self.bom_path) or '?'}"
        )


class Exporter:
    """Writes the BOM and the CPL for one board."""

    def __init__(self, board, store, library=None, settings=None) -> None:
        self.board = board
        self.store = store
        self.library = library
        self.settings = settings

    # -- where ---------------------------------------------------------------

    def output_dir(self) -> str:
        """Return the directory both files go in, creating it if need be."""
        info = self.board.info()
        directory = os.path.join(os.path.dirname(info.path), OUTPUT_DIR)
        Path(directory).mkdir(parents=True, exist_ok=True)
        return directory

    def paths(self) -> tuple:
        """Return ``(bom path, cpl path)`` for this board."""
        directory = self.output_dir()
        stem = Path(self.board.info().name).stem
        return (
            os.path.join(directory, f"BOM-{stem}.csv"),
            os.path.join(directory, f"CPL-{stem}.csv"),
        )

    # -- inputs --------------------------------------------------------------

    def corrections(self) -> list:
        """Return the correction table the CPL applies.

        Empty when no library is open rather than an error: a board with no
        rotation corrections is the common case, and refusing to write a CPL
        because the corrections database could not be opened would be a strange
        way to report that.
        """
        if self.library is None:
            return []
        try:
            return list(self.library.get_all_correction_data())
        except Exception:  # noqa: BLE001 - a missing table must not stop the file
            log.warning("Corrections unavailable; writing the CPL without them")
            return []

    def _add_without_lcsc(self) -> bool:
        """Report whether unassigned parts belong in the two files.

        ``general.order_number`` is the key Phase 5's Settings dialog writes for
        "Add parts without LCSC number to BOM/POS". The name is inherited and
        describes nothing; it is kept because renaming it would silently reset
        the preference of everyone who has already set it. The wx plugin spells
        the same choice ``gerber.lcsc_bom_cpl``.
        """
        if self.settings is None:
            return True
        return bool(self.settings.get("general", "order_number", True))

    # -- writing -------------------------------------------------------------

    def export(self) -> ExportResult:
        """Write both files and report what went into them."""
        bom_path, cpl_path = self.paths()
        result = ExportResult(bom_path=bom_path, cpl_path=cpl_path)
        parts = list(self.store.read_bom_parts())
        result.parts = len(parts)
        result.warnings = fab_rules.consistency_warnings(parts)
        self._write_bom(bom_path, parts, result)
        self._write_cpl(cpl_path, result)
        log.info(
            "Exported %d BOM rows and %d CPL rows to %s",
            result.bom_rows,
            result.cpl_rows,
            os.path.dirname(bom_path),
        )
        return result

    def _write_bom(self, path: str, parts, result: ExportResult) -> None:
        """Write the grouped BOM."""
        dnp = {view.reference for view in self.board.footprints() if view.dnp}

        def on_skip(reason: str, subject: str) -> None:
            if reason == "dnp":
                result.skipped_dnp.append(subject)
            else:
                result.skipped_no_lcsc.append(subject)

        rows = fab_rules.bom_rows(
            parts,
            is_dnp=lambda reference: reference in dnp,
            add_without_lcsc=self._add_without_lcsc(),
            on_skip=on_skip,
        )
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, delimiter=",")
            writer.writerow(fab_rules.BOM_HEADER)
            writer.writerows(rows)
        result.bom_rows = len(rows)

    def _write_cpl(self, path: str, result: ExportResult) -> None:
        """Write the placement file, with corrections applied."""
        corrections = self.corrections()
        origin_x, origin_y = self.board.origin_nm()
        centers = self.board.pad_centers_nm()
        add_without_lcsc = self._add_without_lcsc()
        rows = []

        # Sorted by reference, as the wx plugin sorts ``board.Footprints()``.
        # Not natural-sorted: R10 comes before R2 in the file it has always
        # written, and a CPL is read by a machine.
        for view in sorted(self.board.footprints(), key=lambda v: v.reference):
            if view.dnp:
                result.skipped_dnp.append(view.reference)
                continue
            part = self._part(view.reference)
            if part is None:
                # No row in the project database. The wx plugin skips these
                # silently; they are footprints the store has never seen, which
                # in practice means the database is stale.
                result.skipped_no_position.append(view.reference)
                continue
            if part["exclude_from_pos"] == 1:
                continue
            if not add_without_lcsc and not part["lcsc"]:
                result.skipped_no_lcsc.append(view.reference)
                continue

            center = centers.get(view.reference)
            if center is None:
                center = (
                    int(view.position_mm[0] * fab_rules.IU_PER_MM),
                    int(view.position_mm[1] * fab_rules.IU_PER_MM),
                )
            bottom = view.side == "bottom"
            names = (view.reference, view.value, view.footprint)
            x, y = fab_rules.corrected_position(
                center[0] - origin_x,
                center[1] - origin_y,
                view.orientation_deg,
                bottom,
                names,
                corrections,
            )
            rows.append(
                fab_rules.cpl_row(
                    part["reference"],
                    part["value"],
                    part["footprint"],
                    x,
                    y,
                    fab_rules.corrected_rotation(
                        view.orientation_deg, bottom, names, corrections
                    ),
                    bottom,
                )
            )

        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, delimiter=",")
            writer.writerow(fab_rules.CPL_HEADER)
            writer.writerows(rows)
        result.cpl_rows = len(rows)

    def _part(self, reference: str) -> Optional[dict]:
        """Return the store's record for ``reference``, or ``None``."""
        part = self.store.get_part(reference)
        return part or None


__all__ = ["OUTPUT_DIR", "ExportResult", "Exporter"]
