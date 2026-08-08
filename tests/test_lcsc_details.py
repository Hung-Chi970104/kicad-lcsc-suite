"""Tests for the API-sourced part-detail conversion in `lcsc/details.py`.

`lcsc.details` replaced the bulk parts database as the source of the part
list's Type/Stock/LCSC Params columns and of the BOM estimator's prices. Two
things there have to be exactly right or the estimate is silently wrong:

* the detail mapping must carry the same keys the bulk database aliased its
  columns to, because every consumer reads it by key;
* the price-band encoding must round-trip through the estimator's own parser.

Both are stdlib-only, so they run without wx or KiCad.
"""

from lcsc_suite import derive_params
from lcsc_suite.bom_estimation import pricing
from lcsc_suite.lcsc import api, details


def _hit(**overrides):
    """Build a SearchHit with sensible defaults, overridable per test."""
    fields = {
        "lcsc": "C25741",
        "model": "0402WGF1003TCE",
        "brand": "UNI-ROYAL",
        "package": "0402",
        "category": "Resistors",
        # Ohm symbol, as LCSC actually writes it — `derive_params` matches on
        # it and would extract no resistance from a spelled-out "Ohms".
        "description": "100kΩ ±1% 62.5mW Thick Film Resistor",
        "stock": 3296305,
        "library_type": "Basic",
        "min_qty": 1,
        "reel_qty": 10000,
        "price": 0.0065,
        "datasheet": "",
        "attributes": {"Tolerance": "±1%"},
    }
    fields.update(overrides)
    return api.SearchHit(**fields)


# ---------------------------------------------------------------------------
# Price band encoding
# ---------------------------------------------------------------------------


def test_encode_price_bands_closes_each_band_below_the_next_break():
    """Bands are closed on both ends, the last one open, matching JLC's convention."""
    encoded = details.encode_price_bands([(1, 0.12), (10, 0.08), (100, 0.05)])

    assert encoded == "1-9:0.120000,10-99:0.080000,100-:0.050000"


def test_encode_price_bands_sorts_an_unordered_ladder():
    """LCSC ladder order is not guaranteed, so the encoder imposes one."""
    encoded = details.encode_price_bands([(100, 0.05), (1, 0.12), (10, 0.08)])

    assert encoded == "1-9:0.120000,10-99:0.080000,100-:0.050000"


def test_encode_price_bands_of_empty_ladder_is_blank_not_zero():
    """No ladder means "price unknown"; a 0 band would price the BOM as free."""
    assert details.encode_price_bands([]) == ""


def test_encoded_bands_round_trip_through_the_estimator_parser():
    """The encoder's output must be readable by `get_unit_price`, tier for tier.

    This is the join between the two halves — a format drift on either side
    would show up as an estimate that is quietly wrong rather than as an error.
    """
    encoded = details.encode_price_bands([(1, 0.12), (10, 0.08), (100, 0.05)])

    assert pricing.get_unit_price(1, encoded) == 0.12
    assert pricing.get_unit_price(9, encoded) == 0.12
    assert pricing.get_unit_price(10, encoded) == 0.08
    assert pricing.get_unit_price(99, encoded) == 0.08
    assert pricing.get_unit_price(100, encoded) == 0.05
    assert pricing.get_unit_price(10_000, encoded) == 0.05


def test_single_entry_ladder_encodes_as_one_open_band():
    """A flat price applies at every quantity, not just the first."""
    encoded = details.encode_price_bands([(1, 0.0065)])

    assert encoded == "1-:0.006500"
    assert pricing.get_unit_price(5000, encoded) == 0.0065


# ---------------------------------------------------------------------------
# Detail mapping shape
# ---------------------------------------------------------------------------


def test_details_from_hit_fills_every_expected_field():
    """A cached row and a bulk-database row must be interchangeable by key."""
    result = details.details_from_hit(_hit())

    assert set(result) == set(details.DETAIL_FIELDS)
    assert result["lcsc"] == "C25741"
    assert result["type"] == "Basic"
    # A string, as the bulk database returned it: Stock feeds a DataView
    # column declared "string" and wx does not accept an int there.
    assert result["stock"] == "3296305"
    assert result["part_no"] == "0402WGF1003TCE"
    assert result["package"] == "0402"
    assert result["category"] == "Resistors"


def test_details_from_hit_feeds_params_derivation():
    """The mapping must carry what `params_for_part` needs, or Params stays blank.

    Description and category are the reason the detail fetch goes through the
    JLC search endpoint at all — the retail endpoint returns neither.
    """

    result = details.details_from_hit(_hit())

    assert derive_params.params_for_part(result) == "100kΩ ±1% 0402"


def test_retail_ladder_beats_the_search_endpoints_flat_price():
    """A real ladder wins: a flat price is wrong above the first break."""
    result = details.details_from_hit(
        _hit(price=0.0065), ladder=[(1, 0.01), (100, 0.004)]
    )

    assert result["price"] == "1-99:0.010000,100-:0.004000"


def test_flat_price_is_used_when_no_ladder_was_fetched():
    """With no retail answer the search figure still beats showing nothing."""
    result = details.details_from_hit(_hit(price=0.0065), ladder=None)

    assert result["price"] == "1-:0.006500"


def test_missing_stock_becomes_blank_not_none():
    """`None` would render as the word "None" in the string Stock column."""
    result = details.details_from_hit(_hit(stock=None, price=None))

    assert result["stock"] == ""
    assert result["price"] == ""


# ---------------------------------------------------------------------------
# Assembly-sourced fields
# ---------------------------------------------------------------------------

#: Trimmed to the keys under test, but the values are verbatim from a live
#: `cart.jlcpcb.com` response for C15849.
ASSEMBLY = {
    "stockCount": 14243277,
    "componentLibraryType": "base",
    "firstTypeNameEn": "Capacitors",
    "componentModelEn": "CL10A105KB8NNNC",
    "prices": [
        {"startNumber": 1, "endNumber": 499, "productPrice": 0.0154},
        {"startNumber": 500, "endNumber": 1499, "productPrice": 0.0142},
        {"startNumber": 1500, "endNumber": 3999, "productPrice": 0.0136},
    ],
}


def test_assembly_ladder_is_used_for_the_price():
    """A BOM estimate prices an assembly order, so it needs the assembly ladder."""
    result = details.details_from_assembly(ASSEMBLY)

    assert result["price"] == "1-499:0.015400,500-1499:0.014200,1500-:0.013600"


def test_assembly_supplies_the_coarse_category_directly():
    """`firstTypeNameEn` already uses the vocabulary params switches on."""
    assert details.details_from_assembly(ASSEMBLY)["category"] == "Capacitors"


def test_assembly_does_not_supply_the_library_type():
    """It spells the type `base`/`expand`; the estimator compares to `Extended`.

    Layering the assembly spelling over a search-derived mapping would turn
    every Extended part into a non-match and silently drop the feeder fee.
    """
    assert "type" not in details.details_from_assembly(ASSEMBLY)


def test_assembly_overrides_leave_search_only_fields_intact():
    """The two sources layer; neither blanks what only the other knows."""
    merged = details.details_from_hit(_hit())
    merged.update(details.details_from_assembly(ASSEMBLY))

    assert merged["type"] == "Basic"  # from the search hit
    assert merged["description"]  # from the search hit
    assert merged["category"] == "Capacitors"  # from the assembly detail
    assert merged["stock"] == "14243277"  # from the assembly detail


def test_assembly_with_no_answers_overrides_nothing():
    """An unreachable endpoint must not wipe the fields the search filled."""
    assert details.details_from_assembly({}) == {}


def test_malformed_price_entries_are_skipped_not_fatal():
    """The endpoint is unofficial; a shape change must degrade, not raise."""
    result = details.details_from_assembly(
        {
            "prices": [
                "not a dict",
                {"startNumber": None, "productPrice": 0.1},
                {"startNumber": 1, "productPrice": 0.05},
            ]
        }
    )

    assert result["price"] == "1-:0.050000"


def test_zero_assembly_stock_is_kept_as_a_confirmed_zero():
    """Zero stock is a fact; blank means "not looked up". They must not merge."""
    result = details.details_from_assembly({**ASSEMBLY, "stockCount": 0})

    assert result["stock"] == "0"


# ---------------------------------------------------------------------------
# Category reconciliation
# ---------------------------------------------------------------------------


def test_canonical_category_maps_jlc_categories_onto_the_coarse_families():
    """The API's fine-grained category has to be reduced to what params switches on.

    Every string here is a real `category` value the JLC search endpoint
    returned. Without the mapping only capacitors worked, and only because
    "Capacitors" happens to appear verbatim in their category — JLC writes
    "Resistor" singular, so resistors fell through to a bare part number.
    """
    assert details.canonical_category("Chip Resistor - Surface Mount") == "Resistors"
    assert (
        details.canonical_category("Multilayer Ceramic Capacitors MLCC - SMD/SMT")
        == "Capacitors"
    )
    assert details.canonical_category("Inductors (SMD)") == "Inductors"
    assert details.canonical_category("Schottky Diodes") == "Diodes"


def test_canonical_category_prefers_the_optoelectronic_reading_for_leds():
    """An LED's category names a diode, but its useful parameter is the colour."""
    assert (
        details.canonical_category("Light Emitting Diodes (LED)") == "Optoelectronics"
    )


def test_canonical_category_passes_unknown_categories_through():
    """Unrecognised families must reach the part-number fallback, not be mislabelled."""
    assert (
        details.canonical_category("Microcontroller Units (MCUs)")
        == "Microcontroller Units (MCUs)"
    )
    assert details.canonical_category("") == ""


def test_resistor_params_survive_the_round_trip_from_a_jlc_category():
    """Regression: this produced "0402WGF1003TCE 0402" before the mapping existed."""

    result = details.details_from_hit(
        _hit(
            category="Chip Resistor - Surface Mount",
            description="-55℃~+155℃ 100kΩ 50V 62.5mW Thick Film Resistor ±1% 0402",
        )
    )

    assert derive_params.params_for_part(result) == "100kΩ ±1% 0402"
