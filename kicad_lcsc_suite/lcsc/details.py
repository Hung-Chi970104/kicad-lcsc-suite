"""Per-part details, resolved from the live API instead of a 750 MB download.

The main part list needs six facts about every assigned LCSC number — library
type, stock, manufacturer part number, description, package and a price ladder
— to fill its Type/Stock/LCSC Params columns and to feed the BOM estimator.

Historically those came out of the bulk parts database, because upstream's
part *selector* searched that database and the per-part lookup was a free ride
on something already downloaded. The LCSC Explorer replaced that selector with
live API search, which left a three-quarter-gigabyte mirror of the entire LCSC
catalogue in place to answer one row lookup per assigned part — a few hundred
rows on a busy board.

This module answers those lookups from the API instead. The bulk database
stays supported as a fallback for anyone who wants offline search, but nothing
requires it any more.

Everything here is stdlib-only and free of wx, so the shape conversion and the
price-band encoding are unit-testable without KiCad.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from . import api

logger = logging.getLogger(__name__)

#: The keys the rest of the plugin expects from a detail lookup. This is the
#: bulk database's own column aliasing (see ``Library.get_part_details``), kept
#: byte-for-byte so both sources are interchangeable to every consumer.
DETAIL_FIELDS: Tuple[str, ...] = (
    "lcsc",
    "stock",
    "type",
    "part_no",
    "description",
    "package",
    "category",
    "price",
)


def encode_price_bands(ladder: List[Tuple[int, float]]) -> str:
    """Encode a ``(quantity, unit_price)`` ladder as price bands.

    The format is the one ``bom_estimation.pricing.get_unit_price`` parses:
    ``"1-9:0.12,10-99:0.08,100-:0.05"``, bounds closed on both ends, the last
    band open-ended. LCSC hands out ladder break points, so each band ends one
    piece below the next break.

    Returns ``""`` for an empty ladder, which the estimator reads as "no price
    known" rather than as free.
    """
    if not ladder:
        return ""

    ordered = sorted(ladder, key=lambda entry: entry[0])
    bands = []
    for index, (quantity, price) in enumerate(ordered):
        lower = max(1, int(quantity))
        if index + 1 < len(ordered):
            upper = max(lower, int(ordered[index + 1][0]) - 1)
            bands.append(f"{lower}-{upper}:{price:.6f}")
        else:
            bands.append(f"{lower}-:{price:.6f}")
    return ",".join(bands)


def _empty_details() -> Dict[str, Any]:
    """Return a detail mapping with every expected key present but blank."""
    return dict.fromkeys(DETAIL_FIELDS, "")


#: Coarse family -> substrings of the JLC category that imply it.
#:
#: ``derive_params.params_for_part`` switches on the bulk database's **First
#: Category**, a five-way vocabulary ("Resistors", "Capacitors", …). The JLC
#: search endpoint returns something closer to the second category — "Chip
#: Resistor - Surface Mount", "Multilayer Ceramic Capacitors MLCC - SMD/SMT" —
#: so the two need reconciling or the Params column silently degrades to a bare
#: part number. Capacitors happened to survive on the substring "Capacitors"
#: being present in their JLC category; resistors did not, because JLC writes
#: "Resistor" singular.
#:
#: Order matters. An LED's JLC category is "Light Emitting Diodes (LED)", which
#: matches both families; its useful parameter is the colour, so the
#: optoelectronic reading has to win, exactly as the first-category vocabulary
#: intended.
_CATEGORY_FAMILIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Optoelectronics", ("led", "light emitting", "optocoupler", "photocoupler")),
    ("Resistors", ("resistor", "potentiometer", "rheostat", "varistor")),
    ("Capacitors", ("capacitor", "mlcc", "supercapacitor")),
    ("Inductors", ("inductor", "choke", "ferrite bead", "common mode filter")),
    ("Diodes", ("diode", "rectifier", "zener", "schottky")),
)


def canonical_category(category: str) -> str:
    """Map a JLC category onto the coarse family ``params_for_part`` expects.

    Unrecognised categories are returned unchanged, which lands them in that
    function's catch-all branch — a part number plus the package, the same
    fallback an unfamiliar first category always produced.
    """
    lowered = (category or "").casefold()
    if not lowered:
        return ""
    for family, needles in _CATEGORY_FAMILIES:
        if any(needle in lowered for needle in needles):
            return family
    return category


def details_from_hit(
    hit: api.SearchHit, ladder: Optional[List[Tuple[int, float]]] = None
) -> Dict[str, Any]:
    """Build a detail mapping from a search hit, plus an optional price ladder.

    A search hit already carries everything except a quantity-tiered price:
    the JLC search reports one figure. When ``ladder`` is supplied — LCSC's
    retail price breaks — it wins, because the estimator's whole job is to
    price a run of boards and a flat unit price makes that wrong at every
    quantity above the first break.
    """
    details = _empty_details()
    details.update(
        {
            "lcsc": hit.lcsc,
            # Strings throughout, matching what the bulk database returned:
            # Stock feeds a DataView column declared as "string", and an int
            # there is not the same thing to wx.
            "stock": "" if hit.stock is None else str(hit.stock),
            "type": hit.library_type,
            "part_no": hit.model,
            "description": hit.description,
            "package": hit.package,
            "category": canonical_category(hit.category),
        }
    )
    if ladder:
        details["price"] = encode_price_bands(ladder)
    elif hit.price is not None:
        details["price"] = f"1-:{hit.price:.6f}"
    return details


def details_from_assembly(assembly: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the fields the JLC assembly endpoint is authoritative for.

    Only keys it actually answered are present, so this can be layered over a
    search-derived mapping without blanking anything.

    Note what is deliberately *not* taken from here: ``componentLibraryType``
    spells the library type ``base``/``expand``, where the search endpoint and
    the bulk database both say ``Basic``/``Extended`` — and
    ``bom_estimation.pricing`` compares it to ``"Extended"`` exactly. Taking
    the assembly spelling would silently drop the per-reel feeder fee from
    every estimate.
    """
    out: Dict[str, Any] = {}

    stock = _as_int(assembly.get("stockCount"))
    if stock != "":
        out["stock"] = str(stock)

    # The coarse family, in the same vocabulary the bulk database's "First
    # Category" used — so no keyword mapping is needed when this is available.
    category = str(assembly.get("firstTypeNameEn") or "")
    if category:
        out["category"] = category

    ladder = api.assembly_price_ladder(assembly)
    if ladder:
        out["price"] = encode_price_bands(ladder)

    model = str(assembly.get("componentModelEn") or "")
    if model:
        out["part_no"] = model

    return out


def fetch_details(lcsc: str) -> Dict[str, Any]:
    """Fetch one part's details from the API, blocking on the network.

    **Worker thread only.** Two endpoints, both of which the explorer already
    talks to, so a part inspected in the last five minutes costs nothing here.
    Each is used for what it is actually authoritative about:

    * the **JLC parts-library search**, keyed on the LCSC code — the only
      endpoint returning a ``description`` in the shape
      ``derive_params.params_for_part`` was written against, plus the
      ``Basic``/``Extended`` library type the estimator compares on;
    * the **JLC assembly detail** — stock, the coarse category, and the
      quantity-tiered price ladder for an assembly order, which is the ladder
      the bulk database carried and the one a BOM estimate needs. LCSC retail
      prices describe a different transaction and are only a fallback here.

    Returns ``{}`` when the part cannot be resolved at all, which callers must
    treat as "ask again later", never as "this part has no stock".
    """
    code = api.normalize_lcsc(lcsc)
    if not code:
        return {}

    hit = _search_exact(code)
    assembly = api.jlc_assembly_detail(code)

    if hit is None and not assembly:
        # Both JLC endpoints missed. Retail may still know the part, which
        # gives a name and a price if not the params.
        return _details_from_retail(
            code, api.retail_price_ladder(api.lcsc_retail_detail(code))
        )

    details = details_from_hit(hit) if hit is not None else _empty_details()
    details["lcsc"] = code
    details.update(details_from_assembly(assembly))

    if not details["price"]:
        # No assembly ladder and no flat search price: retail is better than
        # pricing the part at nothing.
        details["price"] = encode_price_bands(
            api.retail_price_ladder(api.lcsc_retail_detail(code))
        )
    return details


def _search_exact(code: str) -> Optional[api.SearchHit]:
    """Return the search hit whose LCSC code is exactly ``code``.

    A keyword search for ``C25741`` can return near matches, so the code is
    re-checked rather than trusting the first row.
    """
    try:
        _total, hits = api.jlc_search(keyword=code, page_size=20)
    except Exception:  # noqa: BLE001 - unofficial endpoint, never fatal
        logger.debug("detail search for %s failed", code, exc_info=True)
        return None
    normalized = api.normalize_lcsc(code)
    for hit in hits:
        if api.normalize_lcsc(hit.lcsc) == normalized:
            return hit
    return None


def _details_from_retail(code: str, ladder: List[Tuple[int, float]]) -> Dict[str, Any]:
    """Build what details we can when only the retail endpoint answered."""
    retail = api.lcsc_retail_detail(code)
    if not retail:
        return {}
    details = _empty_details()
    stock = _as_int(retail.get("stockNumber"))
    details.update(
        {
            "lcsc": code,
            "stock": "" if stock == "" else str(stock),
            "part_no": str(retail.get("productModel") or ""),
            "package": str(retail.get("encapStandard") or ""),
            "price": encode_price_bands(ladder),
        }
    )
    return details


def _as_int(value: Any) -> Any:
    """Coerce to int, or ``""`` when the value is not a number.

    Blank rather than ``None``: the part list renders these straight into a
    string column, where ``None`` would read as the word "None".
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return ""
