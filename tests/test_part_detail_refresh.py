"""Tests for the selection-driven refresh of the part list's detail columns.

The detail cache holds a row for 24 hours, which is what lets the Stock column
say something useful with no network. The cost is that a figure can be a day
stale with nothing on screen to admit it — and JLC restocks a common part by
millions overnight, so "0" can outlive its truth by most of a day.

Selecting a row now refetches it regardless of cache age. Three things about
that are worth pinning down, because each of them is a way for the feature to
turn into a request storm or to break the sweep it runs alongside:

* the cooldown, which is what stops clicking back and forth between two rows
  from being one API round trip per click;
* the generation counter, which a forced refresh must *not* bump — doing so
  would void every result still in flight from the startup sweep;
* the cap on how much of the board one selection may refresh.

The wx half — the debounce timer and the selection event — is covered by
``scripts/gui_probe.py mainwindow``, which drives the real widgets.
"""

from contextlib import contextmanager
import itertools
from pathlib import Path
import sys
import types
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).parent.parent

_ids = itertools.count(1000)


class _StubWidget:
    """Permissive stand-in for any wx class, including as a base class.

    A bare ``MagicMock`` for the ``wx`` module is enough for the other tests,
    which import modules of plain functions. It is not enough here:
    ``JLCPCBTools`` subclasses ``wx.Dialog``, and a class statement whose base
    is a mock produces something whose every attribute — the methods under
    test included — is itself a mock. The bases have to be real classes.
    """

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return MagicMock()


class _StubModule(types.ModuleType):
    """A wx-shaped module: constants are ints, everything else is a class."""

    def __getattr__(self, name):
        # Sizer flags and event ids are combined with ``|`` at import time in
        # places, which a class would not survive.
        value = next(_ids) if name.isupper() else type(name, (_StubWidget,), {})
        setattr(self, name, value)
        return value


@contextmanager
def _stubbed_wx():
    """Install the wx stubs for the duration of one import, then undo them.

    Left in place they would be inherited by every test module collected
    afterwards, several of which assert against their own ``MagicMock`` wx.
    ``mainwindow`` keeps its own references, so restoring costs nothing.
    """
    names = ["wx", "wx.dataview", "wx.adv", "wx.lib", "wx.lib.newevent"]
    saved = {name: sys.modules.get(name) for name in names}
    modules = {}
    for name in names:
        module = _StubModule(name)
        module.__path__ = []
        modules[name] = module
        sys.modules[name] = module
    for name in names[1:]:
        parent, _, leaf = name.rpartition(".")
        setattr(modules[parent], leaf, modules[name])
    # events.py unpacks this into an (event class, binder) pair at import.
    modules["wx.lib.newevent"].NewEvent = lambda: (
        type("Event", (_StubWidget,), {}),
        next(_ids),
    )
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


for _mod in ["pcbnew", "requests"]:
    sys.modules.setdefault(_mod, MagicMock())

# Under the wx stub only for the import: mainwindow builds classes off wx bases
# at module scope, but the refresh logic under test never touches the toolkit.
with _stubbed_wx():
    from kicad_lcsc_suite import mainwindow  # noqa: E402

PARTS = [
    {"reference": "R19", "lcsc": "C25744"},
    {"reference": "R20", "lcsc": "C25744"},
    {"reference": "R9", "lcsc": "C25741"},
    {"reference": "H1", "lcsc": ""},
]


class _FakeThread:
    """Records the worker it would have run instead of starting it."""

    started = []

    def __init__(self, target=None, args=(), daemon=False):
        self.target = target
        self.args = args

    def start(self):
        """Record the spawn; the worker itself is not under test here."""
        type(self).started.append(self.args)


class _FakeWindow:
    """The slice of JLCPCBTools the refresher actually touches.

    The real class is a ``wx.Dialog`` and cannot be built headlessly, so the
    methods under test are borrowed onto this stand-in and run against it.
    """

    start_part_detail_refresh = mainwindow.JLCPCBTools.start_part_detail_refresh
    _forced_refresh_due = mainwindow.JLCPCBTools._forced_refresh_due
    _on_selection_refresh_timer = mainwindow.JLCPCBTools._on_selection_refresh_timer

    def __init__(self, parts=PARTS, stale=()):
        self.store = MagicMock()
        self.store.read_all.return_value = list(parts)
        self.library = MagicMock()
        self.library.get_part_numbers_needing_refresh.return_value = list(stale)
        self.logger = MagicMock()
        self.pending_part_details = set()
        self.part_detail_generation = 0
        self._forced_part_details = {}
        self.partlist_data_model = MagicMock()
        self.footprint_list = MagicMock()
        self._part_detail_worker = MagicMock()

    def select(self, *references):
        """Make ``GetSelections`` report rows for ``references``."""
        items = [object() for _ in references]
        self.footprint_list.GetSelections.return_value = items
        lookup = dict(zip(items, references))
        self.partlist_data_model.get_reference.side_effect = lookup.get


@pytest.fixture(autouse=True)
def no_threads(monkeypatch):
    """Swap the worker thread for a recorder, and reset it between tests."""
    _FakeThread.started = []
    monkeypatch.setattr(mainwindow, "Thread", _FakeThread)
    return _FakeThread


def spawned_targets():
    """Return the ``{lcsc: [refs]}`` mapping of each worker that was spawned."""
    return [args[0] for args in _FakeThread.started]


def test_force_refetches_a_part_the_ttl_calls_fresh(no_threads):
    """The whole point: cache age is what forced refresh exists to ignore."""
    window = _FakeWindow(stale=[])  # nothing is stale as far as the TTL knows

    window.start_part_detail_refresh(["R19"], force=True)

    assert spawned_targets() == [{"C25744": ["R19", "R20"]}]
    # Both rows carrying the part are refreshed by the one fetch, not just the
    # row that was selected.


def test_unforced_refresh_still_obeys_the_ttl():
    """A fresh cache row is left alone on the normal path."""
    window = _FakeWindow(stale=[])

    window.start_part_detail_refresh(["R19"])

    assert spawned_targets() == []


def test_force_does_not_bump_the_generation():
    """A forced refresh must not void results still in flight.

    ``on_part_details_progress`` drops any event whose generation is not the
    current one. Bumping it here would mean one click during the startup sweep
    threw away every answer that sweep had left to deliver.
    """
    window = _FakeWindow(stale=["C25741"])
    window.start_part_detail_refresh()  # the startup sweep
    generation_during_sweep = window.part_detail_generation

    window.start_part_detail_refresh(["R19"], force=True)

    assert window.part_detail_generation == generation_during_sweep
    # ...whereas a reassignment — the mutation the guard exists for — does
    # open a new generation and void what came before.
    window.pending_part_details.clear()
    window.start_part_detail_refresh(["R9"])
    assert window.part_detail_generation == generation_during_sweep + 1


def test_cooldown_suppresses_a_second_forced_refresh(monkeypatch):
    """Clicking back and forth between two rows is not one round trip a click."""
    clock = [1000.0]
    monkeypatch.setattr(mainwindow.time, "monotonic", lambda: clock[0])
    window = _FakeWindow(stale=[])

    window.start_part_detail_refresh(["R19"], force=True)
    window.pending_part_details.clear()  # first fetch has come back
    clock[0] += mainwindow.SELECTION_REFRESH_COOLDOWN_SECONDS - 1
    window.start_part_detail_refresh(["R19"], force=True)

    assert len(spawned_targets()) == 1

    clock[0] += 2
    window.start_part_detail_refresh(["R19"], force=True)
    assert len(spawned_targets()) == 2


def test_a_part_already_being_fetched_is_not_fetched_again():
    """The in-flight set is honoured on the forced path too."""
    window = _FakeWindow(stale=[])
    window.pending_part_details.add("C25744")

    window.start_part_detail_refresh(["R19"], force=True)

    assert spawned_targets() == []


def test_selection_refresh_skips_a_whole_board_selection():
    """Select-all is a bulk gesture, not "tell me about this part"."""
    parts = [
        {"reference": f"R{index}", "lcsc": f"C{index}"}
        for index in range(mainwindow.SELECTION_REFRESH_MAX_PARTS + 1)
    ]
    window = _FakeWindow(parts=parts, stale=[])
    window.select(*[part["reference"] for part in parts])

    window._on_selection_refresh_timer()

    assert spawned_targets() == []


def test_selection_refresh_fires_for_a_small_selection():
    """A handful of rows is the case the feature is for."""
    window = _FakeWindow(stale=[])
    window.select("R19", "R9")

    window._on_selection_refresh_timer()

    assert spawned_targets() == [{"C25744": ["R19", "R20"], "C25741": ["R9"]}]


def test_selection_refresh_ignores_unassigned_rows():
    """A row with no LCSC number has nothing to fetch."""
    window = _FakeWindow(stale=[])
    window.select("H1")

    window._on_selection_refresh_timer()

    assert spawned_targets() == []


def test_cooldown_records_the_attempt_not_the_answer(monkeypatch):
    """A part that never answers must not be retried on every click."""
    clock = [1000.0]
    monkeypatch.setattr(mainwindow.time, "monotonic", lambda: clock[0])
    window = _FakeWindow(stale=[])

    assert window._forced_refresh_due("C25744") is True
    assert window._forced_refresh_due("C25744") is False
