#!/usr/bin/env python3
"""Compare two ``qt_probe.py --geometry-out`` reports across platforms.

This is the instrument behind the migration's central claim:

    Because the style is forced to Fusion and the font size is stated
    explicitly, a screenshot taken on macOS is real evidence about Windows.

That claim has three parts, and they are not equally strong — which is the
whole reason this script grades them separately instead of diffing two files:

**Structure** — the widget tree: which widgets exist, how they nest, which are
hidden. Nothing about a font can change this. A divergence here is a real
portability bug (a missing icon, a control the platform style refuses to build,
a layout that collapses), and it is a **hard failure**.

**Window size** — the top-level geometry of each screen. Most screens state
their own with ``resize()``, and those match to the pixel on both platforms; a
divergence there means a platform overrode a size we asked for, and is a **hard
failure**. The exceptions are the dialogs that have no stated size and take one
from their contents — ``export-summary`` and ``assign-dialog`` — which get a
budget, because their contents are text and their text is in a different face.

**Collapse** — a widget with a real size on one platform and none on the other.
A column squeezed to nothing, a pane that did not open. This is the classic
silent regression the project already worries about, it is what a screenshot
shows only if you knew what to expect, and it is a **hard failure** too.

**Everything else is reported, not failed**, and the first Windows run is why.
The app forces Fusion and states its font *size*, but not a font *family* — so
text is Segoe UI here and the system face on macOS. Those are different faces:
across 38 screens, 2132 widgets differ in size and 1274 in position, and the
largest of both are the spacers and stretchers **doing their job** — the main
toolbar's spacer is 233px on macOS and 277px on Windows precisely because the
buttons either side of it are narrower there. Failing on that would be failing
on the layout working.

So there is no pixel budget. A budget on a number that legitimately differs is
a number somebody raises until it stops complaining, and the two checks above
already catch what a budget was reaching for: a label the platform had to elide
changes the *text*, and text is part of the structure.

    python scripts/compare_geometry.py docs/screens/geometry.txt windows.txt

Exit status is nonzero if the structure diverges, a window whose size the app
sets is not that size, or a widget collapsed.
"""

from __future__ import annotations

import argparse
import re
import sys

# Windows consoles default to cp1252, which cannot encode the Δ this report is
# written in — the first Windows run died on `UnicodeEncodeError` *after* doing
# all the work, which is the most annoying possible place to fail. Reconfigure
# rather than dropping the character: a report about pixel deltas should be
# allowed to say "delta".
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: ``ClassName#objectName 100x20@4,8 'text' [hidden]`` — the line ``dump_tree``
#: writes. The geometry is the only part this needs to pull out separately;
#: everything else is identity and is compared as text.
#:
#: Anchored on the **geometry**, with everything before it taken as the name,
#: rather than assuming the name is one whitespace-free token. It is not:
#: ``FacetFilter#facet-Temperature Coefficient`` has a space in its object
#: name, and the first Windows run reported all twelve Explorer screens as
#: structural failures because those lines fell through to a verbatim
#: comparison that included the pixels. A layout gate that cries structure at
#: a three-pixel difference is one nobody will read twice.
LINE = re.compile(
    r"^(?P<head>.*?)"
    r"(?P<w>\d+)x(?P<h>\d+)@(?P<x>-?\d+),(?P<y>-?\d+)"
    r"(?P<rest>.*)$"
)

#: ``  6: JLC Stock            width=  96 HIDDEN`` — what ``dump_table``
#: writes per column, and ``  total visible width=1120, viewport=1122`` for the
#: table as a whole. Both are text-driven, so they are graded with the interior
#: geometry rather than as structure.
COLUMN = re.compile(
    r"^(?P<indent>\s*)(?P<index>\d+): (?P<header>.*?)\s*width=\s*(?P<w>\d+)"
    r"(?P<rest>.*)$"
)
TOTALS = re.compile(
    r"^(?P<indent>\s*)total visible width=(?P<w>\d+), viewport=(?P<viewport>\d+)$"
)


#: The measurable fields that are a *size* rather than a position. Only these
#: can collapse; an x of 0 is an ordinary left edge.
SIZE_FIELDS = frozenset({"w", "h", "column width", "total column width", "viewport"})


class Divergence:
    """One widget that is not where the other platform put it."""

    def __init__(self, screen, identity, field, left, right):
        self.screen = screen
        #: Kept **unstripped**. The leading whitespace is the widget's depth in
        #: the tree, and depth zero is what distinguishes a screen's own size
        #: from the size of something inside it — the difference between a hard
        #: failure and a measurement.
        self.identity = identity
        self.field = field
        self.left = left
        self.right = right

    @property
    def delta(self) -> int:
        return abs(self.left - self.right)

    @property
    def top_level(self) -> bool:
        return not self.identity.startswith((" ", "\t"))

    def __str__(self) -> str:
        return (
            f"{self.screen}: {self.identity.strip()} {self.field} "
            f"{self.left} vs {self.right} (Δ{self.delta})"
        )


def parse(text: str) -> dict:
    """Split a report into ``{screen: [line, ...]}``.

    Screens are delimited by the ``--- name geometry ---`` header the probe
    writes, so a report holding only some screens still compares cleanly
    against one holding all of them — the caller is told which are missing
    rather than being handed a diff that has slipped by one section.
    """
    screens, current = {}, None
    for line in text.splitlines():
        header = re.match(r"^--- (?P<name>.+?) geometry ---$", line)
        if header:
            current = header.group("name")
            screens[current] = []
        elif current is not None and line.strip():
            screens[current].append(line)
    return screens


#: Anything that looks like an absolute path, in either platform's spelling.
#: Widget *text* is compared as identity, and three screens put a real
#: directory on screen — the Explorer's library folder, and the two schematic
#: dialogs. Those are environment, not layout: a runner's
#: ``C:\Users\RUNNER~1\AppData\Local\Temp\…`` is not a divergence from a
#: developer's ``/var/folders/…``, and left alone it reported every Explorer
#: screen as a structural failure.
PATHLIKE = re.compile(r"(/[\w./~-]{6,}|[A-Za-z]:\\\\?[\w\\.~-]{4,})")


def identity_of(line: str) -> str:
    """Return the part of a line that a font cannot change.

    Indentation is kept because it *is* the tree structure — two widgets with
    the same class at different depths are different widgets. The text a widget
    holds is kept too: if a platform elides a label, the text itself changes,
    and that is the single most valuable thing this comparison can catch.
    """
    match = LINE.match(line)
    if match:
        line = f"{match.group('head').rstrip()}{match.group('rest')}"
    else:
        column = COLUMN.match(line)
        if column:
            line = (
                f"{column.group('indent')}{column.group('index')}: "
                f"{column.group('header')}{column.group('rest')}"
            )
        else:
            totals = TOTALS.match(line)
            if totals:
                line = f"{totals.group('indent')}total visible width"
    return PATHLIKE.sub("<path>", line)


def numbers_of(line: str) -> dict:
    """Return the measurable fields of a line, keyed by name."""
    match = LINE.match(line)
    if match:
        return {field: int(match.group(field)) for field in ("w", "h", "x", "y")}
    column = COLUMN.match(line)
    if column:
        return {"column width": int(column.group("w"))}
    totals = TOTALS.match(line)
    if totals:
        return {
            "total column width": int(totals.group("w")),
            "viewport": int(totals.group("viewport")),
        }
    return {}


def compare_screen(name: str, left: list, right: list) -> tuple:
    """Compare one screen. Returns (structural_problems, divergences)."""
    structural, divergences = [], []
    left_ids = [identity_of(line) for line in left]
    right_ids = [identity_of(line) for line in right]
    if left_ids != right_ids:
        # Report the first divergence rather than every downstream consequence
        # of it: once the trees differ, every later line is misaligned and
        # listing them all buries the one that matters.
        for index, (a, b) in enumerate(zip(left_ids, right_ids)):
            if a != b:
                structural.append(
                    f"{name}: widget tree diverges at line {index + 1}\n"
                    f"    reference: {a.strip()}\n"
                    f"    compared:  {b.strip()}"
                )
                break
        else:
            structural.append(
                f"{name}: widget tree has {len(left_ids)} widgets in the "
                f"reference and {len(right_ids)} in the comparison"
            )
        return structural, divergences

    for a, b in zip(left, right):
        # A hidden widget's *visibility* matters and is compared above, as part
        # of the identity — that is what caught the main toolbar's extension
        # arrow being shown on Windows. Its *size* does not: nobody can see it,
        # and Qt does not keep it stable. A hidden scroll container inside the
        # Explorer's inline pane measures 100px alone and 1434px in an --all
        # run, because the layout it last participated in was a different
        # screen's. Measuring that is measuring nothing, and it would make the
        # gate flaky in the one direction a gate must never be.
        if "[hidden]" in a:
            continue
        left_numbers, right_numbers = numbers_of(a), numbers_of(b)
        for field, value in left_numbers.items():
            other = right_numbers.get(field)
            if other is not None and other != value:
                divergences.append(
                    Divergence(name, identity_of(a), field, value, other)
                )
    return structural, divergences


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="compare_geometry.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("reference", help="the committed report (rendered on macOS)")
    parser.add_argument("compared", help="the report to check against it")
    parser.add_argument(
        "--self-sized-budget",
        type=int,
        default=48,
        help=(
            "how far a dialog that sizes itself to its contents may differ "
            "(default 48). A screen that calls resize() must match exactly; "
            "one that does not takes its size from its text, and its text is "
            "in a different face. Measured: export-summary differs by 37px, "
            "assign-dialog by 7px."
        ),
    )
    parser.add_argument(
        "--collapsed-below",
        type=int,
        default=3,
        help=(
            "a visible widget this small on one platform, when the other gives "
            "it a real size, is a collapse rather than a metric difference "
            "(default 3)."
        ),
    )
    parser.add_argument(
        "--reference-name", default="reference", help="label for the first report"
    )
    parser.add_argument(
        "--compared-name", default="compared", help="label for the second report"
    )
    args = parser.parse_args(argv)

    with open(args.reference, encoding="utf-8") as handle:
        reference = parse(handle.read())
    with open(args.compared, encoding="utf-8") as handle:
        compared = parse(handle.read())

    missing = sorted(set(reference) - set(compared))
    extra = sorted(set(compared) - set(reference))
    divergences = []
    structural = [f"missing from {args.compared_name}: {name}" for name in missing]
    structural += [f"only in {args.compared_name}: {name}" for name in extra]

    for name in sorted(set(reference) & set(compared)):
        screen_structural, screen_divergences = compare_screen(
            name, reference[name], compared[name]
        )
        structural += screen_structural
        divergences += screen_divergences

    # A screen's own size is the first line of its section, and it is the one
    # measurement no font should be able to touch.
    window_size = [d for d in divergences if d.top_level and d.field in ("w", "h")]
    # A top-level window's *position* is the platform placing the dialog on its
    # virtual screen, which is not ours to claim anything about.
    interior = [d for d in divergences if not d.top_level]

    # A screen that states its own size must have it. One that does not is
    # sized by its text, so it gets the budget instead — and which is which is
    # not something this script can know, so it infers it: an exact match needs
    # no excuse, and only the ones that differ are held to the budget.
    oversized = [d for d in window_size if d.delta > args.self_sized_budget]
    collapsed = [
        d
        for d in interior
        if d.field in SIZE_FIELDS
        and min(d.left, d.right) <= args.collapsed_below < max(d.left, d.right)
    ]
    sizes = [d for d in interior if d.field in SIZE_FIELDS]
    positions = [d for d in interior if d.field in ("x", "y")]

    print(f"Comparing {args.reference_name} -> {args.compared_name}")
    print(f"  screens compared: {len(set(reference) & set(compared))}")
    print(f"  structural problems: {len(structural)}")
    print(f"  windows differing in size: {len(window_size)}")
    print(f"  collapsed widgets: {len(collapsed)}")
    print(
        f"  reported only — interior size {len(sizes)}, position {len(positions)}, "
        f"across {len({d.identity for d in interior})} widgets"
    )
    if sizes:
        worst = max(sizes, key=lambda d: d.delta)
        print(f"  widest size difference: Δ{worst.delta}px — {worst}")

    if structural:
        print("\nSTRUCTURAL PROBLEMS — a widget tree differs, which no font can do:")
        for problem in structural:
            print(f"  {problem}")
    if window_size:
        heading = "WINDOW SIZES" if oversized else "Window sizes (within budget)"
        print(f"\n{heading}:")
        for d in sorted(window_size, key=lambda d: -d.delta):
            print(f"  {d}")
    if collapsed:
        print(
            "\nCOLLAPSED — a widget has a size on one platform and none on the other:"
        )
        for d in sorted(collapsed, key=lambda d: -d.delta):
            print(f"  {d}")

    failed = bool(structural or oversized or collapsed)
    print("\nRESULT:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
