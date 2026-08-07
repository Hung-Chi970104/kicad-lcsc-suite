"""Tests for the LCSC Explorer's parametric facet building and filtering.

The filters went from single-selection dropdowns to multi-select checkbox
lists, which changed the filtering contract from "one value per attribute,
ANDed" to "OR within an attribute, AND across attributes". That is the
semantics every parts catalogue uses and the only one that makes multi-select
worth having, so it is worth pinning down.

`lcsc.api` is stdlib-only; the widget layer on top of it needs wx and is
covered by `scripts/gui_probe.py` instead.
"""

from kicad_lcsc_suite.lcsc import api


def _hit(lcsc: str, **attributes):
    """Build a SearchHit carrying only the attributes a facet test cares about."""
    return api.SearchHit(
        lcsc=lcsc,
        model=f"MPN-{lcsc}",
        brand="YAGEO",
        package=attributes.get("Package", ""),
        category="Resistors",
        description="",
        stock=100,
        library_type="Basic",
        min_qty=1,
        reel_qty=5000,
        price=0.001,
        datasheet="",
        attributes=dict(attributes),
    )


RESISTORS = [
    _hit("C1", Tolerance="±1%", Package="0402"),
    _hit("C2", Tolerance="±1%", Package="0402"),
    _hit("C3", Tolerance="±0.5%", Package="0603"),
    _hit("C4", Tolerance="±5%", Package="0603"),
]


# ---------------------------------------------------------------------------
# build_facets
# ---------------------------------------------------------------------------


def test_build_facets_returns_values_with_counts():
    """Counts drive the "±1% (63)" labels; a bare value does not say if it is worth a click."""
    facets = api.build_facets(RESISTORS)

    assert facets["Tolerance"] == [("±1%", 2), ("±0.5%", 1), ("±5%", 1)]
    assert facets["Package"] == [("0402", 2), ("0603", 2)]


def test_build_facets_drops_attributes_that_cannot_discriminate():
    """An attribute every hit shares filters nothing and only costs a row."""
    hits = [
        _hit("C1", Type="Chip Resistor", Tolerance="±1%"),
        _hit("C2", Type="Chip Resistor", Tolerance="±5%"),
    ]

    facets = api.build_facets(hits)

    assert "Type" not in facets
    assert "Tolerance" in facets


def test_build_facets_orders_by_frequency_then_naturally():
    """Commonest options first, so the useful ones sit at the top of the list."""
    hits = [
        _hit("C1", Voltage="50V"),
        _hit("C2", Voltage="16V"),
        _hit("C3", Voltage="16V"),
        _hit("C4", Voltage="6.3V"),
    ]

    assert api.build_facets(hits)["Voltage"] == [("16V", 2), ("6.3V", 1), ("50V", 1)]


def test_build_facets_of_empty_result_set_is_empty():
    """No results, no filters — and no crash on the way there."""
    assert api.build_facets([]) == {}


# ---------------------------------------------------------------------------
# filter_hits
# ---------------------------------------------------------------------------


def test_no_selection_keeps_every_hit():
    """An untouched filter panel must not narrow anything."""
    assert api.filter_hits(RESISTORS, {}) == RESISTORS


def test_multiple_values_of_one_attribute_are_ORed():
    """Ticking a second tolerance widens the result set — the point of multi-select."""
    result = api.filter_hits(RESISTORS, {"Tolerance": {"±1%", "±0.5%"}})

    assert [hit.lcsc for hit in result] == ["C1", "C2", "C3"]


def test_values_across_attributes_are_ANDed():
    """Adding a second attribute narrows, even while each attribute ORs internally."""
    result = api.filter_hits(
        RESISTORS, {"Tolerance": {"±1%", "±0.5%"}, "Package": {"0603"}}
    )

    assert [hit.lcsc for hit in result] == ["C3"]


def test_an_empty_value_set_is_inactive_not_match_nothing():
    """Unticking the last box must restore the full set, not empty the grid."""
    result = api.filter_hits(RESISTORS, {"Tolerance": set(), "Package": {"0402"}})

    assert [hit.lcsc for hit in result] == ["C1", "C2"]


def test_a_bare_string_value_is_treated_as_a_single_selection():
    """Guards the old single-select shape: iterating a string would match nothing.

    `set("±1%")` explodes into individual characters, so a caller still passing
    a plain string would silently filter every row away instead of failing.
    """
    result = api.filter_hits(RESISTORS, {"Tolerance": "±1%"})

    assert [hit.lcsc for hit in result] == ["C1", "C2"]


def test_a_hit_missing_the_attribute_is_excluded():
    """Absent is not a match — a part that never declared a tolerance is not ±1%."""
    hits = [*RESISTORS, _hit("C5", Package="0402")]

    result = api.filter_hits(hits, {"Tolerance": {"±1%"}})

    assert [hit.lcsc for hit in result] == ["C1", "C2"]


def test_filter_returns_a_new_list_rather_than_the_input():
    """Callers keep the unfiltered set around; handing back the original aliases it."""
    result = api.filter_hits(RESISTORS, {})

    assert result is not RESISTORS
