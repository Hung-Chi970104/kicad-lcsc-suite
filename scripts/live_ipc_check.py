#!/usr/bin/env python3
"""Exercise every ``kicad_bridge`` write against a **running** KiCad.

The companion to `qt_probe.py`. That one proves what the UI looks like; this
proves the board actually changed — and the two answer different questions.

**Why this exists.** Everything in Phases 0-2 was verified against
`FixtureBoard`, which is fast, reproducible and runs in CI. The first time a
write crossed a real socket, in Phase 3, it failed immediately: an open commit
is invisible to a read, so the verification was comparing against the
pre-commit state and could never have passed (trap 4 in `kicad_bridge`). The
fixture had been *more permissive* than the API in exactly that respect, which
is the one way a fixture can hide a bug rather than catch it.

So: **run this whenever `kicad_bridge` changes.** It cannot go in CI — it needs
a running KiCad with a board open — and that is precisely why it has to be run
by hand rather than assumed.

    # 1. copy a board somewhere disposable
    mkdir -p /tmp/lcsc-live && cp your.kicad_pcb /tmp/lcsc-live/livecheck.kicad_pcb
    # 2. open the copy in KiCad's PCB Editor
    open -a "/Applications/KiCad/PCB Editor.app" /tmp/lcsc-live/livecheck.kicad_pcb
    # 3. run this
    .venv/bin/python scripts/live_ipc_check.py

Every value it changes is put back, and it never saves. It still **refuses to
run** unless the open board's project path is one you named with ``--allow``
(or a path containing "scratch"/"tmp"), because a verification tool that can
touch a real project is one bad afternoon from being a data-loss tool.

Exit status is nonzero if any assertion failed.
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lcsc_suite import kicad_bridge  # noqa: E402

#: Path fragments that mark a board as disposable. Deliberately crude — the
#: point is to make "I pointed it at my real project" take a conscious
#: ``--allow``, not to be clever about it.
DISPOSABLE = ("scratch", "tmp", "temp")

#: Numbers written during the run. Chosen to be obviously synthetic, so one left
#: behind by a crash is recognisable rather than mistaken for a real assignment.
PROBE_NUMBERS = ("C99999", "C88888", "C77777")


class Report:
    """Collects assertions so one failure does not hide the rest."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, condition: bool, detail: str = "") -> bool:
        """Record one assertion and return it."""
        print(
            f"  [{'PASS' if condition else 'FAIL'}] {label}"
            f"{f' — {detail}' if detail else ''}"
        )
        if not condition:
            self.failures.append(label)
        return condition

    def section(self, title: str) -> None:
        """Start a numbered block of assertions."""
        print(f"\n{title}")


def _is_disposable(path: str, allowed: list) -> bool:
    """Report whether this board is one we are permitted to write to."""
    lowered = path.lower()
    if any(fragment in lowered for fragment in DISPOSABLE):
        return True
    return any(os.path.abspath(entry) in os.path.abspath(path) for entry in allowed)


def check_existing_field(board, report: Report, footprints) -> None:
    """Update an LCSC field the footprint already has."""
    candidates = [view for view in footprints if view.lcsc_field]
    if not candidates:
        report.section("1. update an existing LCSC field — SKIPPED, none present")
        return
    victim = candidates[0]
    original = victim.lcsc
    report.section(
        f"1. update the existing LCSC field on {victim.reference} "
        f"(currently {original or 'blank'!r}, field {victim.lcsc_field!r})"
    )
    board.set_lcsc({victim.reference: PROBE_NUMBERS[0]})
    again = board.footprint(victim.reference)
    report.check(
        "the board reports the new number", again.lcsc == PROBE_NUMBERS[0], again.lcsc
    )
    # Reusing the field is what lets an existing JLC_PN field carry the number
    # instead of a second one appearing beside it.
    report.check(
        "the field was reused, not duplicated",
        again.lcsc_field == victim.lcsc_field,
        again.lcsc_field,
    )
    board.set_lcsc({victim.reference: original})
    report.check("restored", board.footprint(victim.reference).lcsc == original)


def check_field_creation(board, report: Report, footprints) -> None:
    """Create an LCSC field on a footprint that has none — trap 3."""
    candidates = [view for view in footprints if not view.lcsc_field]
    if not candidates:
        report.section("2. create an LCSC field — SKIPPED, every footprint has one")
        return
    victim = candidates[0]
    report.section(f"2. create an LCSC field on {victim.reference}, which has none")
    board.set_lcsc({victim.reference: PROBE_NUMBERS[1]})
    again = board.footprint(victim.reference)
    report.check(
        "the number is on the board", again.lcsc == PROBE_NUMBERS[1], again.lcsc
    )
    report.check("a field was created", bool(again.lcsc_field), again.lcsc_field)
    # Hidden, like the wx plugin's: assigning a number is not a request to put
    # it on the silkscreen.
    report.check("and it is hidden", again.lcsc_visible is False)
    board.set_lcsc({victim.reference: ""})
    report.check("cleared", board.footprint(victim.reference).lcsc == "")


def check_batch(board, report: Report, footprints) -> None:
    """Write several references in one call, as assigning a selection does."""
    batch = [view.reference for view in footprints[:3]]
    originals = {reference: board.footprint(reference).lcsc for reference in batch}
    report.section(f"3. assign {batch} in one call")
    board.set_lcsc(dict.fromkeys(batch, PROBE_NUMBERS[2]))
    report.check(
        "every reference took the number",
        all(board.footprint(r).lcsc == PROBE_NUMBERS[2] for r in batch),
    )
    board.set_lcsc(originals)
    report.check(
        "all restored",
        all(board.footprint(r).lcsc == originals[r] for r in batch),
    )


def check_attributes(board, report: Report, footprints) -> None:
    """Flip an exclusion attribute, which writes a plain boolean, not a field."""
    victim = footprints[0]
    report.section(f"4. flip exclude-from-BOM on {victim.reference}")
    before = board.footprint(victim.reference).exclude_from_bom
    board.set_exclude_from_bom({victim.reference: not before})
    report.check(
        "the attribute changed",
        board.footprint(victim.reference).exclude_from_bom is (not before),
    )
    board.set_exclude_from_bom({victim.reference: before})
    report.check(
        "restored", board.footprint(victim.reference).exclude_from_bom is before
    )


def check_validation(board, report: Report, footprints) -> None:
    """Refuse a value that is not an LCSC number, before touching the board."""
    report.section("5. reject a value that is not an LCSC number")
    try:
        board.set_lcsc({footprints[0].reference: "banana"})
    except ValueError:
        report.check("raised ValueError", True)
    else:
        report.check("raised ValueError", False, "it did not raise")


def main(argv=None) -> int:
    """Connect to a running KiCad and exercise every write helper."""
    parser = argparse.ArgumentParser(
        prog="live_ipc_check.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="PATH",
        help="treat boards under PATH as disposable. Repeatable. Only needed "
        "when the copy is somewhere without 'scratch' or 'tmp' in its path.",
    )
    args = parser.parse_args(argv)

    print("environment:", kicad_bridge.environment_report())
    try:
        board = kicad_bridge.connect()
    except kicad_bridge.NotConnected as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2

    info = board.info()
    print(f"\nboard:   {info.name}")
    print(f"project: {info.project_path}")
    print(f"kicad:   {info.kicad_version}")

    if not _is_disposable(info.project_path, args.allow):
        print(
            "\nREFUSING: that does not look like a disposable copy.\n"
            "This script writes to the open board. Open a copy from a scratch "
            "directory, or pass --allow with the directory holding it.",
            file=sys.stderr,
        )
        return 2

    footprints = board.footprints()
    print(f"footprints: {len(footprints)}")
    print(f"  with an LCSC field: {sum(1 for v in footprints if v.lcsc_field)}")
    print(f"  with none at all:   {sum(1 for v in footprints if not v.lcsc_field)}")
    if not footprints:
        print("\nNothing to test: this board has no footprints.", file=sys.stderr)
        return 2

    report = Report()
    check_existing_field(board, report, footprints)
    check_field_creation(board, report, footprints)
    check_batch(board, report, footprints)
    check_attributes(board, report, footprints)
    check_validation(board, report, footprints)

    if report.failures:
        print(f"\nFAILURES: {', '.join(report.failures)}", file=sys.stderr)
        print(
            "The board may still hold a probe number "
            f"({', '.join(PROBE_NUMBERS)}) — undo in KiCad and do not save.",
            file=sys.stderr,
        )
        return 1
    print("\nALL PASSED — every value was restored; do not save this board.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
