"""Tests for split_bom_designators — the 2048-char BOM Designator chunker.

JLCPCB's BOM parser truncates a Designator field past 2048 characters, silently
dropping the components after the cut. A 500-LED board is the ordinary case that
hits it, so the chunker splits a group across as many rows as it needs and every
row carries its own quantity.

These exercise the pure function directly, and then ``fab_rules.bom_rows``,
which is the caller that has to actually emit those extra rows. Both moved out
of ``fabrication.py`` in Phase 6 so the wx plugin and the Qt app ran the same
code; Phase 8 deleted the wx half, so the integration half of this file now
calls ``bom_rows`` where it used to drive ``Fabrication.generate_bom`` through a
fake board and a fake store. The behaviour asserted is unchanged — and it is
asserted one layer lower, where nothing has to be faked at all.
"""

from lcsc_suite.fab_rules import (
    BOM_DESIGNATOR_MAX_LEN as _BOM_DESIGNATOR_MAX_LEN,
    bom_rows,
    split_bom_designators,
)

# ---------------------------------------------------------------------------
# Unit tests for split_bom_designators()
# ---------------------------------------------------------------------------


def test_empty_list_returns_empty():
    """Empty input produces empty output."""
    assert split_bom_designators([]) == []


def test_single_ref_returns_one_chunk():
    """Single designator always stays in one chunk."""
    assert split_bom_designators(["R1"]) == [["R1"]]


def test_short_list_stays_in_one_chunk():
    """A list whose joined length is well under the limit stays in one chunk."""
    refs = [f"R{i}" for i in range(1, 11)]
    assert split_bom_designators(refs) == [refs]


def test_all_refs_preserved_across_chunks():
    """Every input designator must appear in exactly one output chunk."""
    refs = [f"LED{i}" for i in range(1, 501)]
    chunks = split_bom_designators(refs)
    flat = [r for chunk in chunks for r in chunk]
    assert flat == refs


def test_no_chunk_exceeds_max_len():
    """No chunk's joined string may exceed the limit."""
    refs = [f"LED{i}" for i in range(1, 501)]
    chunks = split_bom_designators(refs)
    for chunk in chunks:
        assert len(",".join(chunk)) <= _BOM_DESIGNATOR_MAX_LEN


def test_500_leds_requires_multiple_chunks():
    """500 LED designators produce more than one chunk (the issue-755 scenario)."""
    refs = [f"LED{i}" for i in range(1, 501)]
    assert len(",".join(refs)) > _BOM_DESIGNATOR_MAX_LEN, (
        "precondition: 500 LEDs joined should exceed the designator cap"
    )
    chunks = split_bom_designators(refs)
    assert len(chunks) > 1


def test_custom_max_len_respected():
    """Custom max_len parameter is honoured."""
    refs = ["A" * 10] * 5  # each ref is 10 chars; 5 of them joined = 54 chars
    chunks = split_bom_designators(refs, max_len=25)
    for chunk in chunks:
        assert len(",".join(chunk)) <= 25


def test_single_oversized_ref_gets_its_own_chunk():
    """A ref that is itself longer than max_len must still appear (in its own chunk)."""
    long_ref = "X" * 3000
    refs = ["R1", long_ref, "R2"]
    chunks = split_bom_designators(refs, max_len=2048)
    flat = [r for chunk in chunks for r in chunk]
    assert flat == refs
    assert long_ref in flat


def test_chunks_are_contiguous_and_ordered():
    """Order of designators must be preserved across chunk boundaries."""
    refs = [f"C{i:04d}" for i in range(1, 300)]
    chunks = split_bom_designators(refs)
    flat = [r for chunk in chunks for r in chunk]
    assert flat == refs


# ---------------------------------------------------------------------------
# The caller: one group, split across as many rows as it needs
# ---------------------------------------------------------------------------

N_LEDS = 500
_LED_REFS = [f"LED{i}" for i in range(1, N_LEDS + 1)]


def _rows(refs, lcsc="C25741", value="WS2812B", footprint="LED_0805"):
    """Return the BOM rows for one grouped part covering *refs*."""
    return bom_rows(
        [{"value": value, "refs": ",".join(refs), "footprint": footprint, "lcsc": lcsc}]
    )


def test_500_leds_keep_every_reference():
    """All 500 refs must appear, across however many rows it takes."""
    found = []
    for row in _rows(_LED_REFS):
        found.extend(row[1].split(","))
    assert sorted(found) == sorted(_LED_REFS)


def test_500_leds_no_row_exceeds_the_limit():
    """The limit is the whole reason this splits; no row may cross it."""
    for row in _rows(_LED_REFS):
        assert len(row[1]) <= _BOM_DESIGNATOR_MAX_LEN


def test_each_rows_quantity_is_its_own_reference_count():
    """A row's quantity counts that row, not the group it came from."""
    for row in _rows(_LED_REFS):
        assert row[4] == len(row[1].split(","))


def test_the_quantities_still_sum_to_the_whole_group():
    """Splitting must not lose or duplicate a component."""
    assert sum(row[4] for row in _rows(_LED_REFS)) == N_LEDS


def test_500_leds_really_do_split():
    """If this ever stops splitting, the tests above pass while proving nothing."""
    assert len(_rows(_LED_REFS)) > 1


def test_a_small_group_stays_one_row():
    """Nothing splits that does not have to."""
    rows = _rows([f"R{i}" for i in range(1, 11)], value="100k", footprint="R0402")
    assert len(rows) == 1
    assert rows[0][4] == 10
