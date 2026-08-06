"""Module for writing assigned LCSC numbers back into the schematic.

The plugin runs inside pcbnew and can only reach the board. The Symbol Fields
Table, the BOM exporters and "Update PCB from Schematic" all read the
*schematic*, so an LCSC number that only exists on a footprint is invisible to
them — and gets wiped the next time the schematic is pushed to the board. This
module closes that gap by editing the ``.kicad_sch`` files directly.

Writing happens only when the user presses "To schematic"; nothing here runs
on its own. Two rules keep an explicit write from doing damage:

* A sheet is only rewritten when a value actually changed, so a sync that has
  nothing to do leaves every file untouched.
* A schematic that the Schematic Editor has open is skipped. KiCad drops a
  ``~<name>.kicad_sch.lck`` file next to a schematic it is editing, and the
  editor holds the whole document in memory — writing underneath it means our
  fields are lost the moment the user saves.

The opposite direction lives in :mod:`schematicimport`.
"""

import logging
import os
import os.path
import re
from typing import Dict, List, Optional, Set

try:
    from pcbnew import GetBuildVersion  # pylint: disable=import-error
except ImportError:  # pragma: no cover - the out-of-process Qt app has no pcbnew
    GetBuildVersion = None

from .core.version import is_version6, is_version7

#: KiCad locks an open document with a sibling file named ``~<name>.<ext>.lck``
#: (see ``lockfile.cpp``); eeschema does this for the root sheet of a project.
LOCK_PREFIX = "~"
LOCK_SUFFIX = ".lck"

#: Field names that have been used to carry an LCSC number over the years. A
#: schematic that already uses one of these keeps it; new fields are "LCSC".
LCSC_FIELD_NAMES = ("LCSC", "LCSC_PN", "JLC_PN")

#: Matches the value of a ``(property "Name" "value"`` line, capturing
#: everything up to the value so a substitution can keep the field name.
_PROPERTY_VALUE_RX = re.compile(r'(\(property\s+"[^"]*"\s+)"[^"]*"')


def is_lcsc_field(name: str) -> bool:
    """Whether a schematic property carries an LCSC number.

    Case-insensitive: a symbol whose field is spelled ``Lcsc`` must be
    *updated*, not shadowed by a second field named ``LCSC``. The importer
    matches on the same rule so the two directions agree on what they see.
    """
    return name.upper() in LCSC_FIELD_NAMES


def lock_file_for(path: str) -> str:
    """Return the path of the lock file KiCad uses for a document."""
    directory, name = os.path.split(path)
    return os.path.join(directory, f"{LOCK_PREFIX}{name}{LOCK_SUFFIX}")


def is_open_in_editor(path: str) -> bool:
    """Whether the Schematic Editor currently has this schematic open."""
    return os.path.isfile(lock_file_for(path))


def find_root_schematic(project_path: str, board_name: str) -> Optional[str]:
    """Locate the root sheet of the project a board belongs to.

    Sub-sheets do not have to be listed: the exporter follows the ``Sheetfile``
    properties of the root sheet into the rest of the hierarchy.
    """
    if not project_path or not os.path.isdir(project_path):
        return None

    if board_name:
        candidate = os.path.join(
            project_path, f"{os.path.splitext(board_name)[0]}.kicad_sch"
        )
        if os.path.isfile(candidate):
            return candidate

    try:
        entries = os.listdir(project_path)
    except OSError:
        return None

    # The root sheet is always named after the project, so a single .kicad_pro
    # in the directory names it even when the board file is called something
    # else entirely.
    projects = [name for name in entries if name.endswith(".kicad_pro")]
    if len(projects) == 1:
        candidate = os.path.join(
            project_path, f"{os.path.splitext(projects[0])[0]}.kicad_sch"
        )
        if os.path.isfile(candidate):
            return candidate

    sheets = [name for name in entries if name.endswith(".kicad_sch")]
    if len(sheets) == 1:
        return os.path.join(project_path, sheets[0])
    return None


def set_property_value(line: str, value: str) -> str:
    """Replace the value of a ``(property "Name" "value"`` line."""
    return _PROPERTY_VALUE_RX.sub(lambda m: f'{m.group(1)}"{value}"', line, count=1)


class SyncResult:
    """What one sync pass did, for logging and for the UI to report."""

    def __init__(self):
        self.updated = 0
        self.added = 0
        self.cleared = 0
        self.written: List[str] = []
        self.skipped_locked: List[str] = []
        self.missing: List[str] = []

    @property
    def changes(self) -> int:
        """Number of symbol fields that were written."""
        return self.updated + self.added + self.cleared

    def summary(self) -> str:
        """One-line human readable description of the pass."""
        if self.skipped_locked and not self.changes:
            return (
                "Schematic not updated: it is open in the Schematic Editor "
                f"({', '.join(os.path.basename(p) for p in self.skipped_locked)})"
            )
        if not self.changes:
            return "Schematic already up to date"
        parts = []
        if self.added:
            parts.append(f"{self.added} added")
        if self.updated:
            parts.append(f"{self.updated} updated")
        if self.cleared:
            parts.append(f"{self.cleared} cleared")
        files = ", ".join(os.path.basename(p) for p in self.written)
        return f"LCSC fields written to schematic ({', '.join(parts)}): {files}"


class SchematicExport:
    """A class to export Schematic files."""

    # This only works with KiCad v6/v7/v8+ files, if the format changes, this will probably break

    def __init__(
        self,
        parent,
        assignments: Optional[Dict[str, str]] = None,
        skip_locked: bool = True,
    ):
        """Set up an exporter for one sync pass.

        ``assignments`` maps a reference to the LCSC number it should end up
        with; an empty value clears the field. References that are absent are
        left exactly as they are — that is what keeps a sync from wiping
        numbers the schematic has but the board does not. Omitting the
        argument derives the mapping from the parent's store, which is the
        historical behaviour of this class.
        """
        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.skip_locked = skip_locked
        if assignments is None:
            assignments = {
                part["reference"]: part["lcsc"]
                for part in parent.store.read_all()
                if part.get("lcsc")
            }
        self._targets = assignments

    def load_schematic(self, paths, version: Optional[str] = None) -> SyncResult:
        """Write the assignments into the given schematics and their sub-sheets.

        ``version`` selects the KiCad file-format branch below. In-process it
        comes from ``pcbnew.GetBuildVersion()``; the out-of-process app has no
        pcbnew and passes the version it got over the IPC API instead. Absent
        both, the modern (8+) writer is used, which is the only one KiCad 10
        can produce anyway.
        """
        result = SyncResult()
        files_seen: Set[str] = set()
        if version is None:
            version = GetBuildVersion() if GetBuildVersion is not None else ""
        for path in paths:
            if self.skip_locked and is_open_in_editor(path):
                self.logger.warning(
                    "%s is open in the Schematic Editor, not writing to it", path
                )
                result.skipped_locked.append(path)
                continue
            if version and is_version6(version):
                self._update_schematic6(path, result)
            elif version and is_version7(version):
                self._update_schematic7(path, result)
            else:
                self._update_schematic(path, result, files_seen)
        return result

    def _read(self, path: str, result: SyncResult) -> Optional[List[str]]:
        """Read a sheet, recording rather than raising when it is not there."""
        if not os.path.isfile(path):
            self.logger.warning("Schematic %s does not exist, skipping", path)
            result.missing.append(path)
            return None
        self.logger.debug("Reading %s...", path)
        with open(path, encoding="utf-8") as f:
            return f.readlines()

    def _commit(
        self, path: str, newlines: List[str], changed: int, result: SyncResult
    ) -> None:
        """Write a rewritten sheet back, keeping the previous one as a backup.

        A sheet with no changes is not touched at all: rewriting untouched
        files would churn mtimes and throw away the previous backup — the only
        copy of the sheet as it was before the last real write — for nothing.
        """
        if not changed:
            self.logger.debug("No LCSC changes for %s", path)
            return
        backup = path + "_old"
        if os.path.exists(backup):
            os.remove(backup)
        os.rename(path, backup)
        with open(path, "w", encoding="utf-8") as f:
            for line in newlines:
                f.write(line + "\n")
        result.written.append(path)
        self.logger.info("Wrote %d LCSC change(s) to %s", changed, path)

    def _update_schematic6(self, path, result: SyncResult):
        """Only works with KiCad V6 files."""
        # Regex to look through schematic property, if we hit the pin section without finding a LCSC property, add it
        # keep track of property ids and Reference property location to use with new LCSC property
        propRx = re.compile(
            '\\(property\\s\\"(.*)\\"\\s\\"(.*)\\"\\s\\(id\\s(\\d+)\\)\\s\\(at\\s(-?\\d+(?:.\\d+)?\\s-?\\d+(?:.\\d+)?)\\s\\d+\\)'
        )
        pinRx = re.compile('\\(pin\\s\\"(.*)\\"\\s\\(')

        lines = self._read(path, result)
        if not lines:
            return

        lastID = -1
        lastLoc = ""
        lcscSeen = False
        newLcsc = None
        lastRef = ""

        newlines = []
        changed = 0
        partSection = False

        for line in lines:
            inLine = line.rstrip()
            outLine = inLine
            if "(symbol (lib_id" in inLine:  # skip library section
                partSection = True
            m = propRx.search(inLine)
            if m and partSection:
                key = m.group(1)
                value = m.group(2)
                lastID = int(m.group(3))

                # found a LCSC property, so update it if needed
                if is_lcsc_field(key):
                    lcscSeen = True
                    if newLcsc is not None and newLcsc != value:
                        self.logger.info("Updating %s on %s", newLcsc, lastRef)
                        outLine = set_property_value(outLine, newLcsc)
                        changed += 1
                        if newLcsc:
                            result.updated += 1
                        else:
                            result.cleared += 1

                if key == "Reference":
                    lastLoc = m.group(4)
                    lastRef = value
                    newLcsc = self._targets.get(value)
            # if we hit the pin section without finding a LCSC property, add it
            m = pinRx.search(inLine)
            if m:
                if not lcscSeen and newLcsc and lastLoc != "" and lastID != -1:
                    self.logger.info("added %s to %s", newLcsc, lastRef)
                    newTxt = f'    (property "LCSC" "{newLcsc}" (id {lastID + 1}) (at {lastLoc} 0)'
                    newlines.append(newTxt)
                    newlines.append("      (effects (font (size 1.27 1.27)) hide)")
                    newlines.append("    )")
                    changed += 1
                    result.added += 1
                lastID = -1
                lastLoc = ""
                lcscSeen = False
                newLcsc = None
                lastRef = ""
            newlines.append(outLine)

        self._commit(path, newlines, changed, result)

    def _update_schematic7(self, path, result: SyncResult):
        """Only works with KiCad V7 files."""
        # Regex to look through schematic property, if we hit the pin section without finding a LCSC property, add it
        # keep track of property ids and Reference property location to use with new LCSC property
        propRx = re.compile(
            '\\(property\\s\\"(.*)\\"\\s\\"(.*)\\"\\s\\(at\\s(-?\\d+(?:.\\d+)?\\s-?\\d+(?:.\\d+)?)\\s\\d+\\)'
        )
        pinRx = re.compile('\\(pin\\s\\"(.*)\\"\\s\\(')

        lines = self._read(path, result)
        if not lines:
            return

        lastLoc = ""
        lcscSeen = False
        newLcsc = None
        lastRef = ""

        newlines = []
        changed = 0
        partSection = False

        for line in lines:
            inLine = line.rstrip()
            outLine = inLine
            if "(symbol (lib_id" in inLine:  # skip library section
                partSection = True
            m = propRx.search(inLine)
            if m and partSection:
                key = m.group(1)
                value = m.group(2)

                # found a LCSC property, so update it if needed
                if is_lcsc_field(key):
                    lcscSeen = True
                    if newLcsc is not None and newLcsc != value:
                        self.logger.info("Updating %s on %s", newLcsc, lastRef)
                        outLine = set_property_value(outLine, newLcsc)
                        changed += 1
                        if newLcsc:
                            result.updated += 1
                        else:
                            result.cleared += 1

                if key == "Reference":
                    lastLoc = m.group(3)
                    lastRef = value
                    newLcsc = self._targets.get(value)
            # if we hit the pin section without finding a LCSC property, add it
            m = pinRx.search(inLine)
            if m:
                if not lcscSeen and newLcsc and lastLoc != "":
                    self.logger.info("added %s to %s", newLcsc, lastRef)
                    newTxt = f'    (property "LCSC" "{newLcsc}" (at {lastLoc} 0)'
                    newlines.append(newTxt)
                    newlines.append("      (effects (font (size 1.27 1.27)) hide)")
                    newlines.append("    )")
                    changed += 1
                    result.added += 1
                lastLoc = ""
                lcscSeen = False
                newLcsc = None
                lastRef = ""
            newlines.append(outLine)

        self._commit(path, newlines, changed, result)

    def _update_schematic(self, path, result: SyncResult, files_seen: Set[str]):
        """Only works with KiCad V8+ files."""
        # Regex to look through schematic property, if we hit the pin section without finding a LCSC property, add it
        # keep track of property ids and Reference property location to use with new LCSC property
        propRx = re.compile('\\(property\\s\\"(.*)\\"\\s"(.*)\\"')
        atRx = re.compile("\\(at\\s(-?\\d+(?:.\\d+)?\\s-?\\d+(?:.\\d+)?)\\s\\d+\\)")
        pinRx = re.compile('\\(pin\\s\\"(.*)\\"')

        # A sheet can be instantiated more than once in a hierarchy, and the
        # file behind every instance is the same file.
        real_path = os.path.realpath(path)
        if real_path in files_seen:
            return
        files_seen.add(real_path)

        # Checked again here because the hierarchy is discovered as we go: a
        # sub-sheet can be open in an editor of its own.
        if self.skip_locked and is_open_in_editor(path):
            self.logger.warning(
                "%s is open in the Schematic Editor, not writing to it", path
            )
            result.skipped_locked.append(path)
            return

        lines = self._read(path, result)
        if not lines:
            return

        lastLoc = ""
        lcscSeen = False
        newLcsc = None
        lastRef = ""

        newlines = []
        changed = 0
        partSection = False

        for i in range(0, len(lines) - 1):
            inLine = lines[i].rstrip()
            inLine2 = lines[i + 1].rstrip()
            outLine = inLine

            if "(symbol" in inLine and "(lib_id" in inLine2:  # skip library section
                partSection = True

            m = propRx.search(inLine)
            m2 = atRx.search(inLine2)
            if m and m2:
                key = m.group(1)
                # Sub-sheets are followed whether or not this sheet has symbols
                # of its own. A top sheet that is nothing but a hierarchy has
                # none, and gating the recursion on `partSection` left every
                # sheet below such a root unwritten.
                if key == "Sheetfile":
                    file_name = m.group(2)
                    dir_name = os.path.dirname(path)
                    self._update_schematic(
                        os.path.join(dir_name, file_name), result, files_seen
                    )
                elif partSection:
                    # found a LCSC property, so update it if needed
                    if is_lcsc_field(key):
                        lcscSeen = True
                        value = m.group(2)
                        if newLcsc is not None and newLcsc != value:
                            self.logger.info(
                                "Updating %s on %s in %s", newLcsc, lastRef, path
                            )
                            outLine = set_property_value(outLine, newLcsc)
                            changed += 1
                            if newLcsc:
                                result.updated += 1
                            else:
                                result.cleared += 1

                    if key == "Reference":
                        lastLoc = m2.group(1)
                        lastRef = m.group(2)
                        newLcsc = self._targets.get(lastRef)
            # if we hit the pin section without finding a LCSC property, add it
            m3 = pinRx.search(inLine)
            if m3 and partSection:
                if not lcscSeen and newLcsc and lastLoc != "":
                    self.logger.info("added %s to %s", newLcsc, lastRef)
                    newTxt = f'\t\t(property "LCSC" "{newLcsc}"\n\t\t\t(at {lastLoc} 0)'
                    newlines.append(newTxt)
                    newlines.append(
                        "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(hide yes)"
                    )
                    newlines.append("\t\t\t)")
                    newlines.append("\t\t)")
                    changed += 1
                    result.added += 1
                lastLoc = ""
                lcscSeen = False
                newLcsc = None
                lastRef = ""
            newlines.append(outLine)
        newlines.append(lines[len(lines) - 1].rstrip())

        self._commit(path, newlines, changed, result)
