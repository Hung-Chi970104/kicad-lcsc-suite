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

**Window size** — the top-level geometry of each screen. Screens state their
own size with ``resize()``, so this is font-independent too, and a divergence
means a platform is overriding a size we asked for. Also a **hard failure**.

**Interior geometry** — every widget beneath the top level. This is where a
font family legitimately shows through: Qt sets a button's width from the text
it holds, and the default family is the platform's, not ours. A few pixels on a
label is Segoe UI being a different face from the macOS system font, and
failing on it would mean failing forever. So these are **measured**, and the
gate is a budget, not equality.

The budget matters more than it looks. Text metrics moving a control by two
pixels is fine; moving it by forty means the label no longer fits and the
platform has elided it — which is exactly the class of bug wxWidgets shipped
and this migration exists to end. So the gate is on the **worst** single
divergence, not the average, because an average hides precisely the one control
that broke.

    python scripts/compare_geometry.py docs/screens/geometry.txt windows.txt

Exit status is nonzero if the structure diverges, a window size diverges, or an
interior divergence exceeds ``--tolerance``.
"""

from __future__ import annotations

import argparse
import re
import sys

#: ``ClassName#objectName 100x20@4,8 'text' [hidden]`` — the line ``dump_tree``
#: writes. The geometry is the only part this needs to pull out separately;
#: everything else is identity and is compared as text.
LINE = re.compile(
    r"^(?P<indent>\s*)(?P<identity>\S+)\s+"
    r"(?P<w>\d+)x(?P<h>\d+)@(?P<x>-?\d+),(?P<y>-?\d+)"
    r"(?P<rest>.*)$"
)

#: ``  col 3 'JLC Stock' 96`` — what ``dump_table`` writes per column. Column
#: widths are text-driven, so they are graded with the interior geometry.
COLUMN = re.compile(r"^(?P<indent>\s*)col\s+(?P<index>\d+)\s+(?P<rest>.*?)(?P<w>\d+)$")


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


def identity_of(line: str) -> str:
    """Return the part of a line that a font cannot change.

    Indentation is kept because it *is* the tree structure — two widgets with
    the same class at different depths are different widgets. The text a widget
    holds is kept too: if a platform elides a label, the text itself changes,
    and that is the single most valuable thing this comparison can catch.
    """
    match = LINE.match(line)
    if match:
        return f"{match.group('indent')}{match.group('identity')}{match.group('rest')}"
    column = COLUMN.match(line)
    if column:
        return f"{column.group('indent')}col {column.group('index')} {column.group('rest')}"
    return line


def numbers_of(line: str) -> dict:
    """Return the measurable fields of a line, keyed by name."""
    match = LINE.match(line)
    if match:
        return {field: int(match.group(field)) for field in ("w", "h", "x", "y")}
    column = COLUMN.match(line)
    if column:
        return {"width": int(column.group("w"))}
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
        "--tolerance",
        type=int,
        default=12,
        help=(
            "how many pixels one widget may move before it is a failure "
            "(default 12). Text metrics move things a little; a control that "
            "has been elided or wrapped moves a lot."
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

    print(f"Comparing {args.reference_name} -> {args.compared_name}")
    print(f"  screens compared: {len(set(reference) & set(compared))}")
    print(f"  structural problems: {len(structural)}")
    print(f"  window-size divergences: {len(window_size)}")
    print(f"  interior divergences: {len(interior)}")
    if interior:
        worst = max(interior, key=lambda d: d.delta)
        moved = sorted({d.identity for d in interior})
        print(f"  widgets affected: {len(moved)}")
        print(f"  worst divergence: Δ{worst.delta}px — {worst}")
        buckets = {}
        for d in interior:
            buckets[d.delta] = buckets.get(d.delta, 0) + 1
        spread = ", ".join(f"Δ{k}×{v}" for k, v in sorted(buckets.items())[:10])
        print(f"  spread: {spread}")

    if structural:
        print("\nSTRUCTURAL PROBLEMS — a widget tree differs, which no font can do:")
        for problem in structural:
            print(f"  {problem}")
    if window_size:
        print("\nWINDOW SIZES DIVERGED — a screen is not the size it asked for:")
        for d in sorted(window_size, key=lambda d: -d.delta):
            print(f"  {d}")
    over = sorted(
        (d for d in interior if d.delta > args.tolerance), key=lambda d: -d.delta
    )
    if over:
        print(f"\nOVER TOLERANCE (>{args.tolerance}px) — {len(over)} widgets:")
        for d in over[:40]:
            print(f"  {d}")
        if len(over) > 40:
            print(f"  ... and {len(over) - 40} more")

    failed = bool(structural or window_size or over)
    print("\nRESULT:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
