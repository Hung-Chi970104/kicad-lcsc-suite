"""The BOM and CPL rules, with nothing from pcbnew in them.

Extracted from :mod:`fabrication` so that the Qt app can write the same two
files without KiCad's interpreter. Both halves import *this* module, which is
the point: the wx plugin and the new app do not merely follow the same rules,
they run the same code, and a byte-comparison of their output is a test of the
seam rather than of the arithmetic.

**Everything here works in integer nanometres**, KiCad's own internal unit,
and converts to millimetres only in the last step before the number is
written. That is not fussiness. The printed figure is ``nanometres / 1e6``, and
a pipeline that divides early accumulates binary error until a position that
should read ``123.456789`` reads ``123.45678900000001`` — a different CSV for
the same board.

Three pcbnew behaviours are reproduced deliberately, all three measured against
KiCad 10.0.3 rather than assumed:

* ``FromMM(mm)`` is ``int(mm * 1e6)`` — it **truncates** toward zero.
* ``wxPoint(double, double)`` truncates too (``1.6`` becomes ``1``), so an
  offset is added as a float and cut, never rounded.
* ``BOX2I::GetCenter()`` is ``position + size // 2``, so the centre of a box of
  odd width sits one nanometre below the middle.

Rounding differently anywhere here changes the last digit of a placement
coordinate, which is how two files that are "obviously the same" fail to
compare equal.
"""

import math
import re

#: KiCad's internal units per millimetre.
IU_PER_MM = 1000000

#: JLC rejects BOM rows whose total length exceeds 2048 characters. We budget
#: 128 characters of headroom for the other fields (Comment, Footprint, LCSC,
#: Quantity) so the Designator chunk alone is capped at 1920 characters.
BOM_DESIGNATOR_MAX_LEN = 1920

#: The two files' column orders. JLC reads these by position, not by name.
BOM_HEADER = ["Comment", "Designator", "Footprint", "LCSC", "Quantity"]
CPL_HEADER = ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]


def from_mm(mm):
    """Convert millimetres to internal units, truncating as ``pcbnew.FromMM`` does."""
    return int(float(mm) * float(IU_PER_MM))


def to_mm(iu):
    """Convert internal units to millimetres, exactly as ``pcbnew.ToMM``."""
    return float(iu) / float(IU_PER_MM)


def box_center(boxes):
    """Return the centre of the merged bounding box of ``boxes``.

    ``boxes`` are ``(x, y, width, height)`` tuples in internal units. The merge
    and the halving are pcbnew's: ``BOX2I::Merge`` takes the extremes and
    ``GetCenter`` adds half the size with integer division, so the answer for a
    box of odd width is the pixel below the middle rather than the one above.
    """
    boxes = list(boxes)
    if not boxes:
        return None
    left = min(x for x, _y, _w, _h in boxes)
    top = min(y for _x, y, _w, _h in boxes)
    right = max(x + w for x, _y, w, _h in boxes)
    bottom = max(y + h for _x, y, _w, h in boxes)
    return (left + (right - left) // 2, top + (bottom - top) // 2)


def split_bom_designators(designators, max_len=BOM_DESIGNATOR_MAX_LEN):
    """Split reference designators into chunks whose joined length fits ``max_len``.

    JLCPCB rejects BOM rows whose total row length exceeds 2048 characters. The
    Designator field is capped below that limit to leave headroom for the other
    fields. When a single part has more references than fit, the row is
    duplicated with the designators spread across copies; each copy carries only
    its own count.
    """
    if not designators:
        return []
    chunks = []
    current = []
    current_len = 0
    for ref in designators:
        # Length if this ref were appended: len(ref) plus the comma separator
        added = len(ref) if not current else len(ref) + 1
        if current and current_len + added > max_len:
            chunks.append(current)
            current = [ref]
            current_len = len(ref)
        else:
            current.append(ref)
            current_len += added
    if current:
        chunks.append(current)
    return chunks


def find_correction(corrections, value):
    """Return ``(rotation, offset)`` for the first correction matching ``value``.

    Tries anchored match (pattern + ``$``) before falling back to unanchored, so
    ``SOT-23-3`` beats ``SOT-23`` when both patterns exist.
    """
    anchored = [(f"(?:{r})$", rot, off) for r, rot, off in corrections]
    for regex, rotation, offset in anchored:
        if re.search(regex, value):
            return rotation, offset
    for regex, rotation, offset in corrections:
        if re.search(regex, value):
            return rotation, offset
    return None


def match_for(corrections, names):
    """Return the first correction matching any of ``names``, in order.

    ``names`` is reference, then value, then the footprint's library item name —
    the order ``fabrication`` has always tried them in. The **first** name that
    matches wins outright; a later name is not consulted even when the winner
    carries no rotation and no offset. That is deliberate: a rule written
    against a reference is a statement about that part specifically, and letting
    a package rule override it would make the specific rule unusable.
    """
    for name in names:
        match = find_correction(corrections, str(name))
        if match:
            return match
    return None


def board_rotation(orientation_deg, bottom):
    """Return the footprint's rotation as the CPL counts it, before corrections.

    Bottom-side angles are mirrored on the Y axis, because a placement machine
    sees the board from underneath when it populates that side.
    """
    if bottom:
        return (180 - orientation_deg) % 360
    return orientation_deg


def corrected_rotation(orientation_deg, bottom, names, corrections):
    """Return the rotation to write, with any matching correction applied."""
    rotation = board_rotation(orientation_deg, bottom)
    match = match_for(corrections, names)
    if match is None:
        # Note that an uncorrected angle is *not* normalised: the wx plugin
        # writes -90.0 when KiCad says -90.0, and only the corrected branch
        # takes a modulus. Normalising here would change thousands of existing
        # rows for no reason anyone asked for.
        return rotation
    return (rotation + int(match[0])) % 360


def corrected_position(x_nm, y_nm, orientation_deg, bottom, names, corrections):
    """Return the position to write, with any matching offset applied.

    The offset is stated in the footprint's own frame, so it is rotated into the
    board's before it is added, and mirrored again on the bottom because that
    coordinate system is mirrored too.
    """
    match = match_for(corrections, names)
    if match is None:
        return x_nm, y_nm
    offset = match[1]
    if not offset or (offset[0] == 0 and offset[1] == 0):
        return x_nm, y_nm
    rotation = board_rotation(orientation_deg, bottom)
    radians = math.radians(rotation)
    offset_x = from_mm(offset[0]) * math.cos(radians) + from_mm(offset[1]) * math.sin(
        radians
    )
    offset_y = -from_mm(offset[0]) * math.sin(radians) + from_mm(offset[1]) * math.cos(
        radians
    )
    if bottom:
        offset_x = -offset_x
    # int(), not round(): this is where the wx plugin builds a wxPoint from two
    # doubles, and that constructor truncates.
    return int(x_nm + offset_x), int(y_nm + offset_y)


def cpl_row(reference, value, footprint, x_nm, y_nm, rotation, bottom):
    """Build one CPL row.

    Y is negated because KiCad's Y axis grows downwards and JLC's grows up.
    """
    return [
        reference,
        value,
        footprint,
        to_mm(x_nm),
        to_mm(y_nm) * -1,
        rotation,
        "bottom" if bottom else "top",
    ]


def bom_rows(parts, is_dnp=None, add_without_lcsc=True, on_skip=None):
    """Build every BOM row for ``parts``, grouped as ``read_bom_parts`` returns them.

    ``parts`` are the store's grouped records: one per (value, footprint, LCSC)
    with a comma-joined ``refs``. ``is_dnp`` answers "is this reference marked
    do-not-place"; those references are dropped from their group, and a group
    left with none is dropped entirely.
    """
    is_dnp = is_dnp or (lambda _reference: False)
    rows = []
    for part in parts:
        if not add_without_lcsc and not part["lcsc"]:
            if on_skip:
                on_skip("no-lcsc", part["refs"])
            continue
        components = []
        for component in part["refs"].split(","):
            if is_dnp(component):
                if on_skip:
                    on_skip("dnp", component)
                continue
            components.append(component)
        if not components:
            continue
        for chunk in split_bom_designators(components):
            rows.append(
                [
                    part["value"],
                    ",".join(chunk),
                    part["footprint"],
                    part["lcsc"],
                    len(chunk),
                ]
            )
    return rows


def consistency_warnings(parts):
    """Report LCSC numbers used by more than one distinct value.

    Returns an empty string when every number is used consistently, otherwise a
    block naming each offender and the groups that disagree.
    """
    lcsc_numbers = {}
    for item in parts:
        if not item["lcsc"]:
            continue
        lcsc_numbers.setdefault(item["lcsc"], []).append(
            {"refs": item["refs"], "values": item["value"]}
        )
    filtered = {key: value for key, value in lcsc_numbers.items() if len(value) > 1}
    result = ""
    for lcsc, items in filtered.items():
        result += f"{lcsc}:\n"
        for item in items:
            result += "  - {} -> {}\n".format(item["refs"], item["values"])
    return result


__all__ = [
    "BOM_DESIGNATOR_MAX_LEN",
    "BOM_HEADER",
    "CPL_HEADER",
    "IU_PER_MM",
    "board_rotation",
    "bom_rows",
    "box_center",
    "consistency_warnings",
    "corrected_position",
    "corrected_rotation",
    "cpl_row",
    "find_correction",
    "from_mm",
    "match_for",
    "split_bom_designators",
    "to_mm",
]
