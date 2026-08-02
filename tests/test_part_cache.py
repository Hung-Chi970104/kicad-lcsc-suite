"""Tests for the local part-detail cache that replaced the bulk DB dependency.

`Library.get_part_details` is called once per assigned part while the footprint
list is being built, on the UI thread. It must therefore resolve from local
storage only, and it must resolve *something* — a stale row beats a blank one,
and serving stale rows unconditionally is what lets the plugin work offline.

These exercise the real SQLite, because the upsert and the staleness query are
hand-written SQL and that is exactly the sort of thing that silently returns
the wrong set.
"""

from pathlib import Path
import sqlite3
import sys
import time
import types
from unittest.mock import MagicMock

_ROOT = Path(__file__).parent.parent

# library.py imports wx and requests at module load; neither is reached by the
# cache code paths, so stubs are enough. Same bootstrap as the other tests.
for _mod in ["wx", "wx.dataview", "requests"]:
    sys.modules.setdefault(_mod, MagicMock())

_pkg_name = "kicadplugin"
if _pkg_name not in sys.modules:
    _pkg = types.ModuleType(_pkg_name)
    _pkg.__path__ = [str(_ROOT)]
    sys.modules[_pkg_name] = _pkg

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import importlib  # noqa: E402

import pytest  # noqa: E402

library_module = importlib.import_module("kicadplugin.library")


class _FakeParent:
    """Stand-in for JLCPCBTools, exposing only what Library reaches for."""

    def __init__(self, data_dir: Path, project_dir: Path):
        self.settings = {"library": {"data_path": str(data_dir)}}
        self.project_path = str(project_dir)


@pytest.fixture
def lib(tmp_path):
    """Build a Library rooted in a temp dir, with no bulk database present."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    project_dir = tmp_path / "project"
    (project_dir / "jlcpcb").mkdir(parents=True)

    # Pre-create the small DBs so construction does not try to migrate them or
    # fetch remote corrections over the network.
    for name in ("corrections.db", "mappings.db"):
        (data_dir / name).write_bytes(b"placeholder")

    return library_module.Library(_FakeParent(data_dir, project_dir))


DETAILS = {
    "lcsc": "C25741",
    "stock": 3296305,
    "type": "Basic",
    "part_no": "0402WGF1003TCE",
    "description": "100kΩ ±1% 62.5mW Thick Film Resistor",
    "package": "0402",
    "category": "Resistors",
    "price": "1-:0.006500",
}


def test_a_missing_bulk_database_is_not_an_error(lib):
    """Absent parts DB means "no offline catalogue", not a broken install."""
    assert lib.has_bulk_database is False
    assert lib.state == library_module.LibraryState.INITIALIZED


def test_bulk_lookup_without_a_database_returns_empty_rather_than_raising(lib):
    """The old code path connected unconditionally and would blow up here."""
    assert lib.get_bulk_part_details("C25741") == {}


def test_cache_round_trips_every_detail_field(lib):
    """A cached row must come back byte-identical, minus its timestamp."""
    lib.set_cached_part_details(DETAILS)

    assert lib.get_cached_part_details("C25741") == DETAILS


def test_cached_row_carries_no_timestamp_key(lib):
    """A cached row and a bulk-DB row have to be interchangeable to consumers."""
    lib.set_cached_part_details(DETAILS)

    assert "fetched_at" not in lib.get_cached_part_details("C25741")


def test_get_part_details_prefers_the_cache(lib):
    """The resolver's first stop, and the only one that works with no bulk DB."""
    lib.set_cached_part_details(DETAILS)

    assert lib.get_part_details("C25741")["part_no"] == "0402WGF1003TCE"


def test_get_part_details_of_an_unknown_part_is_empty(lib):
    """Callers must read this as "not looked up yet", never as "no stock"."""
    assert lib.get_part_details("C99999999") == {}


def test_writing_the_same_part_twice_updates_rather_than_duplicates(lib):
    """The upsert is hand-written SQL; a broken conflict clause would double rows."""
    lib.set_cached_part_details(DETAILS)
    lib.set_cached_part_details({**DETAILS, "stock": 42, "type": "Extended"})

    with sqlite3.connect(lib.partcachedb_file) as con:
        rows = con.execute(
            "SELECT COUNT(*) FROM part_cache WHERE lcsc = 'C25741'"
        ).fetchone()

    assert rows[0] == 1
    refreshed = lib.get_cached_part_details("C25741")
    assert refreshed["stock"] == 42
    assert refreshed["type"] == "Extended"


def test_a_detail_mapping_with_no_lcsc_is_ignored(lib):
    """Without a key there is nothing to upsert against."""
    lib.set_cached_part_details({**DETAILS, "lcsc": ""})

    with sqlite3.connect(lib.partcachedb_file) as con:
        assert con.execute("SELECT COUNT(*) FROM part_cache").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_uncached_parts_need_a_refresh(lib):
    """Everything is stale before it has ever been fetched."""
    assert lib.get_part_numbers_needing_refresh(["C1", "C2"]) == ["C1", "C2"]


def test_a_freshly_cached_part_needs_no_refresh(lib):
    """Otherwise every window open would re-fetch the whole board."""
    lib.set_cached_part_details(DETAILS)

    assert lib.get_part_numbers_needing_refresh(["C25741"]) == []


def test_a_part_cached_beyond_the_ttl_needs_a_refresh(lib):
    """Stock moves, so a day-old row gets refreshed — but is still served meanwhile."""
    lib.set_cached_part_details(DETAILS)
    stale_stamp = int(time.time()) - library_module.PART_CACHE_TTL_SECONDS - 60
    with sqlite3.connect(lib.partcachedb_file) as con:
        con.execute(
            "UPDATE part_cache SET fetched_at = ? WHERE lcsc = 'C25741'", (stale_stamp,)
        )

    assert lib.get_part_numbers_needing_refresh(["C25741"]) == ["C25741"]
    # Crucially, still served: this is the offline story.
    assert lib.get_part_details("C25741")["part_no"] == "0402WGF1003TCE"


def test_staleness_query_reports_only_the_parts_that_need_work(lib):
    """A mixed board must not re-fetch the rows it already has."""
    lib.set_cached_part_details(DETAILS)

    assert lib.get_part_numbers_needing_refresh(["C25741", "C15849", "C13585"]) == [
        "C15849",
        "C13585",
    ]


def test_staleness_query_deduplicates_and_drops_blanks(lib):
    """Boards reuse the same part on dozens of references; fetch it once."""
    assert lib.get_part_numbers_needing_refresh(
        ["C15849", "C15849", "", None, "C13585"]
    ) == ["C15849", "C13585"]


def test_staleness_query_of_nothing_is_empty(lib):
    """A board with no assigned parts must not build a degenerate IN () clause."""
    assert lib.get_part_numbers_needing_refresh([]) == []
