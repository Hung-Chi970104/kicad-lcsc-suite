"""Tests for the background fill behind Type / JLC Stock / LCSC Params.

Those three columns read the local part cache and never the network — that is
deliberate, and ``PartList.rows()`` documents why. What was missing is anything
that ever *wrote* that cache: the only caller of
``library.set_cached_part_details`` was the probe's fixture seeding, so the
committed screenshots showed the columns filled while a real board showed them
blank. These tests pin the replacement.

What is worth protecting here is not that a worker starts. It is that:

* **an unanswered lookup is never written.** A host that 403s today must not
  overwrite details fetched correctly yesterday, and it must not blank a stock
  figure the Explorer confirmed at assignment time;
* **the fixture source cannot reach the network.** ``details.fetch_details``
  goes through ``api.jlc_search``, whose vendored fallback carries its own
  transport and so never passes the host breaker — the same hole
  ``FixtureSource.search`` was written to close;
* **params arriving late still highlight.** The match terms are derived from the
  params, so a row filled in by this pass and one filled in by a rebuild have to
  end up identical.

Rendered offscreen, so they need no display.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

from PySide6.QtWidgets import QApplication  # noqa: E402

from lcsc_suite import app as app_module, kicad_bridge  # noqa: E402
from lcsc_suite.config import Settings  # noqa: E402
from lcsc_suite.controller import SuiteController  # noqa: E402
from lcsc_suite.parts import PartList, open_library  # noqa: E402
from lcsc_suite.search_source import FixtureSource  # noqa: E402
from lcsc_suite.ui.part_detail_refresh import (  # noqa: E402
    REFRESH_INTERVAL,
    PartDetailRefresher,
    is_answered,
)

FIXTURE = (
    Path(__file__).resolve().parent.parent / "lcsc_suite" / "fixtures" / "board.json"
)

#: A reference the fixture ships assigned, with a value and a footprint the
#: derived params below deliberately corroborate.
ASSIGNED = "R1"
ASSIGNED_NUMBER = "C17513"

#: What the JLC search endpoint hands back for a 1kΩ 0805, in the shape
#: ``lcsc/details.py`` produces and ``derive_params`` reads.
ANSWER = {
    "lcsc": ASSIGNED_NUMBER,
    "stock": "5027753",
    "type": "Basic",
    "part_no": "0805W8F1001T5E",
    "description": "125mW Thick Film Resistors 150V ±100ppm/℃ ±1% 1kΩ",
    "package": "0805",
    "category": "Resistors",
    "price": "1-:0.0012",
}


@pytest.fixture(scope="session", autouse=True)
def application():
    """Build the QApplication the widgets live in."""
    return app_module.build_application(theme_mode="light", offscreen=True)


class _StubSource:
    """A source that answers from a dict and records what it was asked."""

    offline = True

    def __init__(self, answers=None) -> None:
        self.answers = answers or {}
        self.asked: list[str] = []

    def part_details(self, lcsc: str) -> dict:
        self.asked.append(lcsc)
        return dict(self.answers.get(lcsc, {}))

    def assembly_detail(self, lcsc: str) -> dict:
        """Answer the estimator's half of the same source with nothing."""
        del lcsc
        return {}


@pytest.fixture
def board(tmp_path):
    """Return the fixture board, with a writable project directory."""
    with open(FIXTURE, encoding="utf-8") as handle:
        result = kicad_bridge.FixtureBoard.from_dict(copy.deepcopy(json.load(handle)))
    result.relocate(str(tmp_path))
    return result


@pytest.fixture
def parts(board, tmp_path):
    """Return a reconciler over an *empty* throwaway part cache.

    Empty rather than seeded from ``fixtures/part_details.json``: a cache that
    already answers every number is precisely the state in which this pass is
    supposed to do nothing, so seeding it would make every test below vacuous.
    """
    settings = Settings(path=str(tmp_path / "settings.json"))
    result = PartList(board, settings=settings)
    result.owner.settings.setdefault("library", {})["data_path"] = str(
        tmp_path / "library"
    )
    result.library = open_library(result.owner)
    result.refresh_from_board()
    return result


@pytest.fixture
def window(board, parts, tmp_path):
    """Return a main window whose model holds the fixture's rows."""
    controller = SuiteController(board, parts, settings=parts.settings)
    try:
        controller.window.reload_parts()
        yield controller.window
    finally:
        controller.window.close()


def _refresher(window, parts, source):
    """Build a refresher over the window and drain it in one go."""
    return PartDetailRefresher(window, parts, source=source)


def _drain(refresher) -> None:
    """Wait for the pool and deliver its queued signals."""
    refresher._pool.drain(4000)
    QApplication.processEvents()


def _row(window, reference):
    """Return the model row for ``reference``."""
    model = window.part_model
    return model.part(model.row_for(reference))


# ---------------------------------------------------------------------------
# The fill
# ---------------------------------------------------------------------------


def test_no_source_means_no_lookup(window, parts):
    """Omitting a source has to mean "no network", not "the default one"."""
    assert PartDetailRefresher(window, parts).refresh() == 0


def test_the_three_columns_fill_from_what_was_fetched(window, parts):
    """The whole bug: nothing wrote the cache, so nothing ever filled these."""
    assert _row(window, ASSIGNED).params == ""

    source = _StubSource({ASSIGNED_NUMBER: ANSWER})
    refresher = _refresher(window, parts, source)
    assert refresher.refresh() > 0
    _drain(refresher)

    row = _row(window, ASSIGNED)
    assert row.part_type == "Basic"
    assert row.stock == 5027753
    assert row.params == "1kΩ ±1% 0805"


def test_what_was_fetched_survives_a_rebuild(window, parts):
    """Written to the cache, not just painted — otherwise a reload loses it."""
    refresher = _refresher(window, parts, _StubSource({ASSIGNED_NUMBER: ANSWER}))
    refresher.refresh()
    _drain(refresher)

    window.reload_parts()
    assert _row(window, ASSIGNED).params == "1kΩ ±1% 0805"
    assert parts.library.get_cached_part_details(ASSIGNED_NUMBER)["type"] == "Basic"


def test_params_arriving_late_still_highlight(window, parts):
    """The terms are derived from the params, so they have to be recomputed.

    ``1K`` in an ``R_0805_2012Metric`` lights up ``1kΩ`` and ``0805``. A row
    filled in by this pass and one filled in by a rebuild must agree, or the
    highlight would be missing on exactly the rows it just filled.
    """
    refresher = _refresher(window, parts, _StubSource({ASSIGNED_NUMBER: ANSWER}))
    refresher.refresh()
    _drain(refresher)

    filled = _row(window, ASSIGNED).match_terms
    assert "1kω" in filled
    assert "0805" in filled

    window.reload_parts()
    assert _row(window, ASSIGNED).match_terms == filled


def test_one_request_per_distinct_number(window, parts):
    """Twenty identical resistors are one lookup, not twenty."""
    refresher = _refresher(window, parts, _StubSource({ASSIGNED_NUMBER: ANSWER}))
    refresher.refresh()
    _drain(refresher)
    assert refresher.source.asked.count(ASSIGNED_NUMBER) == 1


def test_a_cached_number_is_not_fetched_again(window, parts):
    """A second pass over a filled cache queues nothing."""
    refresher = _refresher(window, parts, _StubSource({ASSIGNED_NUMBER: ANSWER}))
    refresher.refresh()
    _drain(refresher)
    asked = len(refresher.source.asked)
    assert refresher.refresh() == 0
    assert len(refresher.source.asked) == asked


# ---------------------------------------------------------------------------
# What an unanswered lookup must not cost
# ---------------------------------------------------------------------------


def test_an_answer_is_what_carries_a_fact(window, parts):
    """``fetch_details`` returns every key blank when both endpoints missed."""
    assert is_answered(ANSWER)
    assert not is_answered({"lcsc": ASSIGNED_NUMBER, "stock": "", "type": ""})
    assert not is_answered({})
    assert not is_answered(None)


def test_an_unanswered_lookup_does_not_overwrite_the_cache(window, parts):
    """A 403 today must not wipe what worked yesterday."""
    good = _refresher(window, parts, _StubSource({ASSIGNED_NUMBER: ANSWER}))
    good.refresh()
    _drain(good)

    # A fresh refresher, because the first one now knows the number is cached.
    silent = _refresher(window, parts, _StubSource())
    silent._fetched.clear()
    silent.refresh()
    _drain(silent)
    assert parts.library.get_cached_part_details(ASSIGNED_NUMBER)["type"] == "Basic"


def test_an_answer_without_stock_leaves_the_figure_alone(window, parts):
    """``?`` means nobody answered, and a confirmed figure must not become one.

    The Explorer records a stock count at assignment time. A detail lookup that
    resolves a description but no stock — the retail-only fallback does exactly
    that — has learned nothing about stock, and writing its blank back would
    turn a real number into "nobody answered".
    """
    parts.store.set_stock(ASSIGNED, 1854)
    window.reload_parts()
    assert _row(window, ASSIGNED).stock == 1854

    answer = {**ANSWER, "stock": ""}
    refresher = _refresher(window, parts, _StubSource({ASSIGNED_NUMBER: answer}))
    refresher.refresh()
    _drain(refresher)

    row = _row(window, ASSIGNED)
    assert row.stock == 1854
    assert row.params == "1kΩ ±1% 0805"


def test_a_number_that_answered_nothing_is_not_asked_twice(window, parts):
    """Nothing was cached, so the cache keeps offering it; this is what stops it."""
    refresher = _refresher(window, parts, _StubSource())
    refresher.refresh()
    _drain(refresher)
    asked = len(refresher.source.asked)
    assert asked > 0
    assert refresher.refresh() == 0
    assert len(refresher.source.asked) == asked


# ---------------------------------------------------------------------------
# Staleness, pacing and the offline guarantee
# ---------------------------------------------------------------------------


def test_a_result_from_before_a_reassignment_is_dropped(window, parts):
    """The token guard: a number can move between spawn and delivery."""
    refresher = _refresher(window, parts, _StubSource({ASSIGNED_NUMBER: ANSWER}))
    refresher.refresh()
    refresher.invalidate()
    _drain(refresher)
    assert _row(window, ASSIGNED).params == ""


def test_the_pacing_is_dropped_for_a_fixture_but_kept_for_a_live_source(window, parts):
    """A capture has no host to be polite to; a rate-limited endpoint does."""
    assert _refresher(window, parts, _StubSource()).interval == 0.0

    class _Live(_StubSource):
        offline = False

    assert _refresher(window, parts, _Live()).interval == REFRESH_INTERVAL


def test_the_fixture_source_cannot_answer_a_detail_lookup():
    """The offline guarantee, and the reason it needs its own override.

    ``details.fetch_details`` reaches ``api.jlc_search``, which falls back to the
    vendored client — a second transport that never sees the host breaker. So a
    number the capture does not hold would go straight out to the network from
    the one source whose contract is that it cannot.
    """
    assert FixtureSource().part_details("C17513") == {}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_controller_runs_the_pass_on_every_rebuild(board, parts):
    """One connection, so none of the six reload sites has to remember it."""
    source = _StubSource({ASSIGNED_NUMBER: ANSWER})
    controller = SuiteController(board, parts, settings=parts.settings, source=source)
    try:
        assert controller.detail_refresher is not None
        _drain(controller.detail_refresher)
        assert ASSIGNED_NUMBER in source.asked
        assert _row(controller.window, ASSIGNED).part_type == "Basic"
    finally:
        controller.window.close()


def test_a_controller_without_a_source_starts_no_pass(board, parts):
    """Same rule as the estimator: no source, no network."""
    controller = SuiteController(board, parts, settings=parts.settings)
    try:
        assert controller.detail_refresher.refresh() == 0
    finally:
        controller.window.close()
