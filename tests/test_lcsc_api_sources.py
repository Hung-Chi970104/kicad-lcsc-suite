"""Tests for the source fallbacks and blocked-host handling in `lcsc/api.py`.

Both storefront endpoints are unofficial, and one of them — `wmsc.lcsc.com` —
returns 403 to entire networks at a time. What this module guards is what the
plugin does about that:

* a host that refuses everything is tried a few times and then left alone, so
  filling a 120-row grid against a blocked endpoint costs a handful of
  requests instead of 120;
* retail stock falls through to EasyEDA's copy of the same figure, and a
  source answering "nothing" is never confused with a part having no stock;
* product photos come out of the search response's JLC file ids, so a grid of
  thumbnails needs no per-row JSON lookup at all.

Network access is stubbed throughout; nothing here touches the wire.
"""

import pytest

from lcsc_suite.lcsc import api


@pytest.fixture(autouse=True)
def _clean_caches():
    """Start every test with empty caches and every host re-armed."""
    api.clear_cache()
    yield
    api.clear_cache()


# ---------------------------------------------------------------------------
# The circuit breaker
# ---------------------------------------------------------------------------


def test_breaker_trips_after_repeated_failures():
    """A host that keeps refusing is skipped instead of asked again."""
    breaker = api._HostBreaker(threshold=3, cooldown=600.0)
    url = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=C1"

    for _ in range(2):
        breaker.record_failure(url)
        assert not breaker.blocked(url), "tripped before reaching the threshold"

    breaker.record_failure(url)
    assert breaker.blocked(url)


def test_breaker_is_per_host():
    """One blocked storefront must not take the working one down with it."""
    breaker = api._HostBreaker(threshold=2, cooldown=600.0)
    for _ in range(2):
        breaker.record_failure("https://wmsc.lcsc.com/x")

    assert breaker.blocked("https://wmsc.lcsc.com/y")
    assert not breaker.blocked("https://jlcpcb.com/y")


def test_breaker_reopens_after_cooldown():
    """The cooldown lets one probe through, so a block heals on its own."""
    breaker = api._HostBreaker(threshold=1, cooldown=0.0)
    breaker.record_failure("https://wmsc.lcsc.com/x")
    assert not breaker.blocked("https://wmsc.lcsc.com/x")


def test_breaker_success_clears_the_count():
    """Intermittent failures must not add up to a trip over a long session."""
    breaker = api._HostBreaker(threshold=3, cooldown=600.0)
    for _ in range(5):
        breaker.record_failure("https://wmsc.lcsc.com/x")
        breaker.record_success("https://wmsc.lcsc.com/x")
    assert not breaker.blocked("https://wmsc.lcsc.com/x")


def test_blocked_host_costs_no_request(monkeypatch):
    """Once tripped, `_get_json` must not reach the network at all."""
    calls = []

    def explode(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a blocked host was contacted")

    for _ in range(3):
        api._breaker.record_failure("https://wmsc.lcsc.com/x")
    monkeypatch.setattr(api.urllib.request, "urlopen", explode)

    assert api._get_json("https://wmsc.lcsc.com/anything") == {}
    assert calls == []


# ---------------------------------------------------------------------------
# Retail fallback
# ---------------------------------------------------------------------------


def test_retail_stock_prefers_the_retail_endpoint(monkeypatch):
    """When LCSC answers, EasyEDA is not asked — it is the narrower source."""
    monkeypatch.setattr(api, "lcsc_retail_detail", lambda _c: {"stockNumber": 1234})
    monkeypatch.setattr(
        api,
        "easyeda_retail",
        lambda _c: pytest.fail("EasyEDA queried while LCSC was answering"),
    )
    assert api.retail_stock("C1592") == 1234


def test_retail_stock_falls_back_to_easyeda(monkeypatch):
    """A blocked retail endpoint must not blank the column."""
    monkeypatch.setattr(api, "lcsc_retail_detail", lambda _c: {})
    monkeypatch.setattr(api, "easyeda_retail", lambda _c: {"stock": 417450})
    assert api.retail_stock("C1592") == 417450


def test_retail_stock_unknown_is_none_not_zero(monkeypatch):
    """Nobody answering is not the same fact as a part being out of stock."""
    monkeypatch.setattr(api, "lcsc_retail_detail", lambda _c: {})
    monkeypatch.setattr(api, "easyeda_retail", lambda _c: {})
    assert api.retail_stock("C1592") is None


def test_retail_reachable_until_every_source_trips():
    """One surviving source is still a source: the fill must keep going."""
    assert not api.retail_unreachable()

    for _ in range(3):
        api._breaker.record_failure(api.LCSC_RETAIL_DETAIL)
    assert api.host_blocked("wmsc.lcsc.com")
    assert not api.retail_unreachable(), "EasyEDA can still answer"


def test_retail_unreachable_when_both_sources_trip():
    """Every source refusing is a fact about the hosts, not about the parts.

    The explorer draws this differently from "no stock" — a column of `?` and a
    count of zero look identical otherwise. See `explorer._update_status`.
    """
    for url in (api.LCSC_RETAIL_DETAIL, api.EASYEDA_PRODUCT):
        for _ in range(3):
            api._breaker.record_failure(url)
    assert api.retail_unreachable()


def test_refresh_re_arms_the_retail_sources():
    """`clear_cache` is the Refresh button, and Refresh means "try again"."""
    for url in (api.LCSC_RETAIL_DETAIL, api.EASYEDA_PRODUCT):
        for _ in range(3):
            api._breaker.record_failure(url)
    api.clear_cache()
    assert not api.retail_unreachable()


def test_easyeda_retail_reads_the_szlcsc_block(monkeypatch):
    """The retail figures live under `szlcsc`, LCSC's pre-rename identity."""
    monkeypatch.setattr(
        api,
        "easyeda_product",
        lambda _c: {"szlcsc": {"stock": 417450, "price": 0.0237, "min": 50}},
    )
    assert api.easyeda_retail("C1592") == {
        "stock": 417450,
        "price": 0.0237,
        "min_buy": 50,
        "url": "",
    }


def test_easyeda_retail_on_a_part_with_no_block(monkeypatch):
    """A JLC-only part has no LCSC listing; that is `{}`, not a crash."""
    monkeypatch.setattr(api, "easyeda_product", lambda _c: {"title": "x"})
    assert api.easyeda_retail("C7442385") == {}


# ---------------------------------------------------------------------------
# Product photos
# ---------------------------------------------------------------------------


def test_jlc_image_url_from_an_access_id():
    """Access ids are numeric and arrive as both ints and strings."""
    assert api.jlc_image_url(8582976091031658496).endswith("8582976091031658496")
    assert api.jlc_image_url("8582976091031658496") == api.jlc_image_url(
        8582976091031658496
    )


@pytest.mark.parametrize("value", [None, "", "None", "null", "0", "   "])
def test_jlc_image_url_rejects_absent_ids(value):
    """A part with no photo must yield no URL, never a URL that 404s."""
    assert api.jlc_image_url(value) == ""


def test_assembly_photo_urls_collects_every_angle(monkeypatch):
    """`imageList` is the multi-shot set — front, back, reel."""
    monkeypatch.setattr(
        api,
        "jlc_assembly_detail",
        lambda _c: {
            "imageList": [
                {"productBigImageAccessId": "111"},
                {"productBigImageAccessId": "222"},
                {"productBigImageAccessId": "111"},  # duplicate, dropped
                {"productBigImageAccessId": None},  # absent, dropped
            ]
        },
    )
    urls = api.assembly_photo_urls("C1592")
    assert [url.rsplit("/", 1)[-1] for url in urls] == ["111", "222"]


def test_assembly_photo_urls_falls_back_to_the_top_level_id(monkeypatch):
    """Single-photo parts populate only the top-level id."""
    monkeypatch.setattr(
        api,
        "jlc_assembly_detail",
        lambda _c: {"imageList": [], "productBigImageAccessId": "999"},
    )
    assert api.assembly_photo_urls("C1592")[0].endswith("999")


# ---------------------------------------------------------------------------
# Search parsing — the fields the easyeda2kicad client drops
# ---------------------------------------------------------------------------


_SEARCH_ITEM = {
    "componentCode": "C1592",
    "componentModelEn": "CL10A105KO8NNNC",
    "componentBrandEn": "Samsung Electro-Mechanics",
    "componentSpecificationEn": "0603",
    "componentTypeEn": "Multilayer Ceramic Capacitors MLCC - SMD/SMT",
    "describe": "16V 1uF X5R +/-10% 0603 MLCC",
    "stockCount": 5851917,
    "componentLibraryType": "base",
    "minPurchaseNum": 1,
    "encapsulationNumber": 4000,
    "componentPrices": [{"startNumber": 1, "productPrice": 0.0168}],
    "dataManualUrl": "https://example.invalid/ds.pdf",
    "attributes": [
        {"attribute_name_en": "Tolerance", "attribute_value_name": "±10%"},
        {"attribute_name_en": "Voltage Rating", "attribute_value_name": "-"},
    ],
    "minImageAccessId": "8582976091031658496",
    "productBigImageAccessId": "8582976089978888192",
}


def _stub_search(monkeypatch, item):
    payload = {"data": {"componentPageInfo": {"total": 1, "list": [item]}}}
    monkeypatch.setattr(api, "_get_json", lambda *a, **k: payload)


def test_search_keeps_the_photo_ids(monkeypatch):
    """The whole point of the direct call: thumbnails without a JSON lookup."""
    _stub_search(monkeypatch, _SEARCH_ITEM)
    _total, hits = api.jlc_search("1uF 0603")

    assert hits[0].image_id == "8582976091031658496"
    assert hits[0].big_image_id == "8582976089978888192"
    assert hits[0].thumbnail_url.endswith("8582976091031658496")
    assert hits[0].photo_url.endswith("8582976089978888192")


def test_search_maps_library_type_to_the_estimator_spelling(monkeypatch):
    """`bom_estimation.pricing` compares against "Extended" exactly."""
    # Distinct keywords: responses are cached per query, so reusing one here
    # would assert against the first payload twice.
    _stub_search(monkeypatch, _SEARCH_ITEM)
    _total, hits = api.jlc_search("basic part")
    assert hits[0].library_type == "Basic"

    _stub_search(monkeypatch, dict(_SEARCH_ITEM, componentLibraryType="expand"))
    _total, hits = api.jlc_search("extended part")
    assert hits[0].library_type == "Extended"


def test_search_drops_placeholder_attributes(monkeypatch):
    """A dash is JLC's "not specified" and would become a useless facet."""
    _stub_search(monkeypatch, _SEARCH_ITEM)
    _total, hits = api.jlc_search("x")
    assert hits[0].attributes == {"Tolerance": "±10%"}


def test_search_falls_back_to_the_vendored_client(monkeypatch):
    """A shape change in the direct call must not break search outright."""
    monkeypatch.setattr(api, "_get_json", lambda *a, **k: {})
    monkeypatch.setattr(
        api,
        "_jlc_search_vendored",
        lambda *a, **k: (1, [_hit_stub()]),
    )
    total, hits = api.jlc_search("x")
    assert (total, hits[0].lcsc) == (1, "C1")


def _hit_stub():
    return api.SearchHit(
        lcsc="C1",
        model="m",
        brand="b",
        package="p",
        category="c",
        description="d",
        stock=1,
        library_type="Basic",
        min_qty=1,
        reel_qty=1,
        price=0.1,
        datasheet="",
    )


def test_photo_url_falls_back_to_the_thumbnail():
    """Some parts carry only the small id; a soft photo beats none."""
    hit = _hit_stub()
    hit.image_id = "123"
    assert hit.photo_url.endswith("123")


# ---------------------------------------------------------------------------
# Stock report
# ---------------------------------------------------------------------------


def test_stock_report_uses_the_fallback_when_retail_is_blocked(monkeypatch):
    """The detail pane's retail card must survive a blocked lcsc.com."""
    monkeypatch.setattr(api, "jlc_assembly_detail", lambda _c: {"stockCount": 500})
    monkeypatch.setattr(api, "lcsc_retail_detail", lambda _c: {})
    monkeypatch.setattr(
        api, "easyeda_retail", lambda _c: {"stock": 4000, "min_buy": 50, "price": 0.02}
    )
    monkeypatch.setattr(api, "assembly_photo_urls", lambda *a, **k: ["u"])

    report = api.stock_report("C1592")
    assert report.ok
    assert report.retail_stock == 4000
    assert report.retail_min_buy == 50
    assert report.retail_ladder == [(50, 0.02)]
    assert report.images == ["u"]


def test_stock_report_photos_come_from_jlc_when_retail_has_none(monkeypatch):
    """Photos are the failure everyone notices; JLC is the reachable source."""
    monkeypatch.setattr(api, "jlc_assembly_detail", lambda _c: {"stockCount": 1})
    monkeypatch.setattr(api, "lcsc_retail_detail", lambda _c: {})
    monkeypatch.setattr(api, "easyeda_retail", lambda _c: {})
    monkeypatch.setattr(api, "assembly_photo_urls", lambda *a, **k: ["a", "b"])

    assert api.stock_report("C1592").images == ["a", "b"]
