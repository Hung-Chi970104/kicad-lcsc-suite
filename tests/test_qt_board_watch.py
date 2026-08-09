"""Tests for following the board out of the editor.

The thing worth proving here is not "the window closes" — it is that it closes
for the *right* board. A user with two projects open has two editors and two of
these windows, and the failure this guards against is one of them taking the
other down with it. So the tests below are mostly about telling two boards
apart, and about not believing a single unanswered poll.

Rendered offscreen, so they need no display.
"""

from __future__ import annotations

import inspect
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

from PySide6.QtWidgets import QDialog, QWidget  # noqa: E402

from lcsc_suite import app as app_module, board_watch  # noqa: E402
from lcsc_suite.board_watch import BoardWatcher  # noqa: E402
from lcsc_suite.kicad_bridge import (  # noqa: E402
    FixtureBoard,
    NotConnected,
    _Ipc,
    _same_document,
    connect,
    open_board_paths,
)


@pytest.fixture(scope="session")
def application():
    """Build the one QApplication the tests share."""
    return app_module.build_application(theme_mode="light", offscreen=True)


# ---------------------------------------------------------------------------
# Stubs standing in for kipy
# ---------------------------------------------------------------------------


class FakeProject:
    """The project a document belongs to, as far as this code reads it."""

    def __init__(self, path: str) -> None:
        self.path = path


class FakeDocument:
    """What ``get_open_documents`` hands back, as far as this code cares."""

    def __init__(self, project_path: str, board_filename: str) -> None:
        self.project = FakeProject(project_path)
        self.board_filename = board_filename


class FakeBoard:
    """A kipy ``Board``, reduced to the one attribute ``still_open`` reads."""

    def __init__(self, document: FakeDocument) -> None:
        self.document = document


class FakeSocket:
    """The pynng socket kipy keeps, reduced to the two options we shorten."""

    def __init__(self, timeout_ms: int) -> None:
        self.recv_timeout = timeout_ms
        self.send_timeout = timeout_ms


class FakeClient:
    """kipy's ``KiCadClient``, reduced to where it keeps the deadline."""

    def __init__(self, timeout_ms: int) -> None:
        self._timeout_ms = timeout_ms
        self._conn = FakeSocket(timeout_ms)


class FakeKiCad:
    """One KiCad instance, with a list of open PCBs we can change under it."""

    def __init__(
        self,
        documents,
        raises: Exception | None = None,
        timeout_ms: int = 5000,
    ) -> None:
        self.documents = list(documents)
        self.raises = raises
        self.calls = 0
        self._client = FakeClient(timeout_ms)
        #: The deadline in force each time the question was actually put.
        self.deadlines: list = []

    def get_open_documents(self, doc_type):
        """Answer the liveness question, or fail the way a dead socket does."""
        self.calls += 1
        client = getattr(self, "_client", None)
        if client is not None:
            self.deadlines.append((client._timeout_ms, client._conn.recv_timeout))
        if self.raises is not None:
            raise self.raises
        return list(self.documents)


class StubBoard:
    """A board whose liveness we drive directly, for the watcher's own tests."""

    def __init__(self, answers) -> None:
        #: Each entry is either a bool or an exception to raise.
        self.answers = list(answers)
        self.asked = 0
        #: Every ``timeout_ms`` the watcher asked with.
        self.deadlines: list = []

    def still_open(self, timeout_ms=None) -> bool:
        """Return the next scripted answer, repeating the last one for ever."""
        self.asked += 1
        self.deadlines.append(timeout_ms)
        answer = self.answers[min(self.asked - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class RecordingWindow(QDialog):
    """A window that records the close it is asked to make.

    A ``QDialog`` with a parent, because that is what the Explorer and the part
    details window are: children in Qt's object sense, separate windows in the
    user's, and therefore invisible to a plain ``QApplication.quit()``.
    """

    def __init__(self, closed: list, name: str, parent=None) -> None:
        super().__init__(parent)
        self._closed = closed
        self._name = name

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Record it, the way the Explorer's does its real work in it."""
        self._closed.append(self._name)
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Telling two boards apart
# ---------------------------------------------------------------------------


def test_same_document_matches_the_same_board():
    """The specifier KiCad returns later is a fresh object, not the same one."""
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    again = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    assert _same_document(ours, again)


def test_same_document_normalises_the_project_path():
    """A trailing separator is the same directory, not a different project."""
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    spelled_differently = FakeDocument("/home/x/tempctrl/", "tempctrl.kicad_pcb")
    assert _same_document(ours, spelled_differently)


def test_same_document_separates_two_projects():
    """The case this whole module exists for."""
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    theirs = FakeDocument("/home/x/amplifier", "amplifier.kicad_pcb")
    assert not _same_document(ours, theirs)


def test_same_document_separates_two_boards_in_one_project():
    """Same project directory, different board file."""
    ours = FakeDocument("/home/x/proj", "main.kicad_pcb")
    theirs = FakeDocument("/home/x/proj", "panel.kicad_pcb")
    assert not _same_document(ours, theirs)


# ---------------------------------------------------------------------------
# _Ipc.still_open
# ---------------------------------------------------------------------------


def test_still_open_when_our_board_is_listed():
    """The ordinary case: our board is the one the editor still has open."""
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    kicad = FakeKiCad([FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")])
    assert _Ipc(kicad, FakeBoard(ours)).still_open()


def test_still_open_ignores_another_projects_board():
    """A second project open in the same instance is not ours.

    This is the regression that would show up as "closing project B closed
    project A's window", and it is invisible to any check that only asks
    whether *a* board is open.
    """
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    kicad = FakeKiCad([FakeDocument("/home/x/amplifier", "amplifier.kicad_pcb")])
    assert not _Ipc(kicad, FakeBoard(ours)).still_open()


def test_still_open_with_both_projects_listed():
    """Ours is open even when it is not the first document returned."""
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    kicad = FakeKiCad(
        [
            FakeDocument("/home/x/amplifier", "amplifier.kicad_pcb"),
            FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb"),
        ]
    )
    assert _Ipc(kicad, FakeBoard(ours)).still_open()


def test_still_open_with_no_boards_at_all():
    """KiCad is up, its last PCB window is not."""
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    assert not _Ipc(FakeKiCad([]), FakeBoard(ours)).still_open()


def test_still_open_reports_a_dead_connection_separately():
    """A silent socket must not arrive at the caller spelled as "board closed"."""
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    kicad = FakeKiCad([], raises=ConnectionRefusedError("socket is gone"))
    with pytest.raises(NotConnected):
        _Ipc(kicad, FakeBoard(ours)).still_open()


def test_fixture_board_is_always_open(application):
    """The probe and CI run against a board with no editor behind it."""
    board = FixtureBoard.from_dict({"board": {}, "footprints": []})
    assert board.still_open()
    # There is no round trip to put a deadline on, but the watcher passes one to
    # every board it is given and must not have to know which kind it has.
    assert board.still_open(timeout_ms=board_watch.POLL_TIMEOUT_MS)


# ---------------------------------------------------------------------------
# The deadline one poll gets
# ---------------------------------------------------------------------------


def test_the_poll_uses_its_own_deadline_not_the_write_one(application):
    """Unprompted, on the thread that paints — so it may not wait like a write.

    The window used to freeze for the full write timeout on every tick a stalled
    KiCad failed to answer, which also turned the three-miss tolerance from six
    seconds of wall clock into fifteen.
    """
    board = StubBoard([True])
    BoardWatcher(board).poll()
    assert board.deadlines == [board_watch.POLL_TIMEOUT_MS]

    write_deadline = inspect.signature(connect).parameters["timeout_ms"].default
    assert write_deadline > board_watch.POLL_TIMEOUT_MS


def test_still_open_shortens_the_deadline_and_puts_it_back():
    """Both places kipy keeps it, restored — every later write uses them."""
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    kicad = FakeKiCad([FakeDocument(*_key(ours))], timeout_ms=5000)

    assert _Ipc(kicad, FakeBoard(ours)).still_open(timeout_ms=800)

    assert kicad.deadlines == [(800, 800)], "the request went out on the old deadline"
    assert kicad._client._timeout_ms == 5000
    assert kicad._client._conn.recv_timeout == 5000
    assert kicad._client._conn.send_timeout == 5000


def test_the_deadline_is_restored_after_a_silent_socket():
    """The failing poll is exactly the one that must not leave 800ms behind.

    A write that inherited it would report a false failure — and the app's own
    rule is that a clean return value proves nothing, so a write that times out
    early looks like a board that refused the change.
    """
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    kicad = FakeKiCad([], raises=ConnectionRefusedError("socket is gone"))

    with pytest.raises(NotConnected):
        _Ipc(kicad, FakeBoard(ours)).still_open(timeout_ms=800)

    assert kicad._client._timeout_ms == 5000
    assert kicad._client._conn.recv_timeout == 5000


def test_the_poll_still_answers_when_kipy_moves_its_internals():
    """There is no public setter, so this degrades rather than raising.

    A kipy that keeps its deadline somewhere else costs the poll its shortcut,
    not its answer.
    """
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    kicad = FakeKiCad([FakeDocument(*_key(ours))])
    del kicad._client

    assert _Ipc(kicad, FakeBoard(ours)).still_open(timeout_ms=800)


def test_open_board_paths_lists_both_projects():
    """What ``live_ipc_check.py`` prints so a human can see the two apart."""
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    kicad = FakeKiCad(
        [
            FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb"),
            FakeDocument("/home/x/amplifier", "amplifier.kicad_pcb"),
        ]
    )
    assert open_board_paths(_Ipc(kicad, FakeBoard(ours))) == [
        os.path.join("/home/x/tempctrl", "tempctrl.kicad_pcb"),
        os.path.join("/home/x/amplifier", "amplifier.kicad_pcb"),
    ]


def test_open_board_paths_never_raises():
    """A diagnostic that fails the run it is diagnosing is worse than useless."""
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    kicad = FakeKiCad([], raises=ConnectionRefusedError("socket is gone"))
    assert open_board_paths(_Ipc(kicad, FakeBoard(ours))) == []
    assert open_board_paths(FixtureBoard.from_dict({"board": {}})) == []


# ---------------------------------------------------------------------------
# The watcher's tolerance
# ---------------------------------------------------------------------------


def test_one_missed_poll_is_not_enough(application):
    """A busy KiCad answers late; that must not close the window."""
    watcher = BoardWatcher(StubBoard([False]), tolerance=3)
    fired = []
    watcher.board_closed.connect(lambda: fired.append(True))

    assert watcher.poll() is False
    assert watcher.misses == 1
    assert not fired


def test_the_run_of_misses_has_to_be_consecutive(application):
    """One good reply resets the count, so intermittent lateness never fires."""
    board = StubBoard([False, True, False, False])
    watcher = BoardWatcher(board, tolerance=3)
    fired = []
    watcher.board_closed.connect(lambda: fired.append(True))

    watcher.poll()  # miss
    watcher.poll()  # hit, resets
    assert watcher.misses == 0
    watcher.poll()  # miss
    watcher.poll()  # miss
    assert watcher.misses == 2
    assert not fired


def test_closing_fires_after_the_tolerance(application):
    """A full run of misses is the evidence, and it fires on the last one."""
    watcher = BoardWatcher(StubBoard([False]), tolerance=3)
    fired = []
    watcher.board_closed.connect(lambda: fired.append(True))

    watcher.poll()
    watcher.poll()
    assert not fired
    watcher.poll()
    assert fired == [True]


def test_it_fires_only_once_and_stops_asking(application):
    """The window is on its way out; further round trips find the same answer."""
    board = StubBoard([False])
    watcher = BoardWatcher(board, tolerance=1)
    fired = []
    watcher.board_closed.connect(lambda: fired.append(True))

    watcher.poll()
    asked = board.asked
    watcher.poll()
    watcher.poll()
    assert fired == [True]
    assert board.asked == asked


def test_a_dead_connection_counts_as_a_miss(application):
    """Same evidence, same tolerance — but it must not raise out of the timer."""
    watcher = BoardWatcher(StubBoard([NotConnected("gone")]), tolerance=2)
    fired = []
    watcher.board_closed.connect(lambda: fired.append(True))

    assert watcher.poll() is False
    watcher.poll()
    assert fired == [True]


def test_polling_is_skipped_under_a_modal_dialog(application):
    """The user is mid-answer, quite possibly in the one that saves their work."""
    board = StubBoard([False])
    watcher = BoardWatcher(board, tolerance=1)
    fired = []
    watcher.board_closed.connect(lambda: fired.append(True))

    dialog = QDialog()
    dialog.setModal(True)
    dialog.show()
    application.processEvents()
    try:
        assert watcher.poll() is True
        assert board.asked == 0
        assert not fired
    finally:
        dialog.close()
        dialog.deleteLater()
        application.processEvents()

    # And it resumes once the dialog is gone.
    watcher.poll()
    assert fired == [True]


# ---------------------------------------------------------------------------
# Two windows, two boards
# ---------------------------------------------------------------------------


def test_one_project_closing_leaves_the_other_alone(application):
    """End to end over the two backends, at the level the user described it.

    Two KiCad instances, a board each, a watcher each. Close one board and only
    that watcher fires — no matter how many times the other is polled.
    """
    ours = FakeDocument("/home/x/tempctrl", "tempctrl.kicad_pcb")
    theirs = FakeDocument("/home/x/amplifier", "amplifier.kicad_pcb")

    our_kicad = FakeKiCad([FakeDocument(*_key(ours))])
    their_kicad = FakeKiCad([FakeDocument(*_key(theirs))])

    our_watcher = BoardWatcher(_Ipc(our_kicad, FakeBoard(ours)), tolerance=2)
    their_watcher = BoardWatcher(_Ipc(their_kicad, FakeBoard(theirs)), tolerance=2)
    closed = []
    our_watcher.board_closed.connect(lambda: closed.append("ours"))
    their_watcher.board_closed.connect(lambda: closed.append("theirs"))

    # The user closes our PCB window. Theirs is untouched.
    our_kicad.documents = []
    for _ in range(4):
        our_watcher.poll()
        their_watcher.poll()

    assert closed == ["ours"]
    assert their_watcher.misses == 0


def _key(document) -> tuple:
    """Rebuild a document's identity, so the fake returns a distinct object."""
    return (document.project.path, document.board_filename)


def test_close_window_with_board_closes_and_quits(application, monkeypatch):
    """The wiring, without waiting on a real timer."""
    quits = []
    monkeypatch.setattr(type(application), "quit", lambda self: quits.append(True))

    window = QWidget()
    window.show()
    watcher = board_watch.close_window_with_board(
        StubBoard([False]), window, application
    )
    try:
        for _ in range(board_watch.MISS_TOLERANCE):
            watcher.poll()
        application.processEvents()
        assert not window.isVisible()
        assert quits == [True]
    finally:
        window.deleteLater()
        application.processEvents()


def test_shutting_down_closes_our_own_windows_first(application, monkeypatch):
    """``quit()`` runs no ``closeEvent``, and the Explorer's is where work happens.

    Cancelling in-flight fetches, clearing both thread pools, and writing the
    explorer geometry, ``overwrite_existing`` and ``library_folder`` all live in
    it — so quitting over the top of an open Explorer silently discarded them,
    at the likeliest moment of all: closing the PCB is what a user does when they
    have finished shopping.

    The order is asserted too. The main window's close can raise the "write these
    to the schematic?" prompt, which must not appear behind a catalogue window.
    """
    order = []
    monkeypatch.setattr(type(application), "quit", lambda self: order.append("quit"))

    window = RecordingWindow(order, "main")
    explorer = RecordingWindow(order, "explorer", parent=window)
    stranger = RecordingWindow(order, "stranger")
    for widget in (window, explorer, stranger):
        widget.show()
    application.processEvents()

    try:
        watcher = board_watch.close_window_with_board(
            StubBoard([False]), window, application
        )
        for _ in range(board_watch.MISS_TOLERANCE):
            watcher.poll()
        application.processEvents()

        assert order == ["explorer", "main", "quit"]
        assert not explorer.isVisible()
        # And only ours: a top-level that is not this window's child is somebody
        # else's business, which is the same reasoning that scopes the watcher to
        # one board rather than to "KiCad".
        assert stranger.isVisible()
    finally:
        for widget in (stranger, explorer, window):
            widget.close()
            widget.deleteLater()
        application.processEvents()


def test_a_window_already_closed_is_not_closed_again(application, monkeypatch):
    """It has had its ``closeEvent``; running it again would re-save stale state."""
    order = []
    monkeypatch.setattr(type(application), "quit", lambda self: order.append("quit"))

    window = RecordingWindow(order, "main")
    explorer = RecordingWindow(order, "explorer", parent=window)
    window.show()
    explorer.show()
    application.processEvents()
    explorer.close()
    application.processEvents()
    assert order == ["explorer"]

    try:
        watcher = board_watch.close_window_with_board(
            StubBoard([False]), window, application
        )
        for _ in range(board_watch.MISS_TOLERANCE):
            watcher.poll()
        application.processEvents()
        assert order == ["explorer", "main", "quit"]
    finally:
        for widget in (explorer, window):
            widget.close()
            widget.deleteLater()
        application.processEvents()
