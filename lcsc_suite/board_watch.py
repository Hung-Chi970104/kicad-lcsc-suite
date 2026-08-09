"""Close this window when the board it was opened over is closed.

The app runs in its own process, so nothing tells it that KiCad has gone. Left
alone it sits there showing a part list for a board that is no longer open, and
every button on it writes to a socket that will not answer.

**Scoped to one board, because "KiCad closed" is the wrong question.** A user
with two projects open has two editors and two of these windows, and closing one
project must not take the other's window with it. Two things keep them apart:

* the *connection* pins the instance — ``kicad_bridge.connect()`` dials the
  socket named in ``KICAD_API_SOCKET``, which KiCad set when it launched us, so
  the questions asked here only ever reach the editor that started this process;
* the *board* pins the document — :meth:`~lcsc_suite.kicad_bridge.Board.still_open`
  matches on project path and board filename, not on "is any board open", so a
  second board open in the same instance is not mistaken for ours.

Polled rather than pushed: KiCad's IPC API has no "document closed" event to
subscribe to. That makes the tolerance below the interesting part, not the
timer.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget

from .kicad_bridge import NotConnected

log = logging.getLogger(__name__)

#: How often to ask. Two seconds is well below the point where a stale window
#: is confusing, and the question costs one small round trip on a local socket.
POLL_INTERVAL_MS = 2000

#: How long to wait for an answer before calling it a miss.
#:
#: **Its own deadline, deliberately shorter than the one writes use.** This poll
#: is unprompted and it runs on the GUI thread, so every millisecond it waits is
#: a millisecond the window does not paint — and the whole design already treats
#: a late answer as survivable, because :data:`MISS_TOLERANCE` exists. A write
#: is the opposite on both counts: the user asked for it, and waiting is better
#: than a false failure. Inheriting the write deadline made a stalled KiCad
#: freeze the window for five seconds per tick and turned the tolerance below
#: into fifteen seconds of wall clock rather than six.
POLL_TIMEOUT_MS = 800

#: How many consecutive "no" or "no answer" replies before we believe it.
#:
#: **Not paranoia — the single poll is genuinely unreliable.** KiCad serves the
#: API from its main loop, so anything that occupies that loop (a long DRC, a
#: plot, a file dialog, the beachball while a big board redraws) answers late or
#: not at all, and a request that times out is indistinguishable at this level
#: from one nobody was there to receive. Three in a row is six seconds of
#: silence, which no ordinary editor operation produces and a closed window
#: always does.
MISS_TOLERANCE = 3


class BoardWatcher(QObject):
    """Polls one board's liveness and reports when it goes away.

    Emits :attr:`board_closed` exactly once, then stops polling — the window is
    on its way out and further round trips would only find the same answer.
    """

    #: The board this watcher was built for is no longer open.
    board_closed = Signal()

    def __init__(
        self,
        board,
        interval_ms: int = POLL_INTERVAL_MS,
        tolerance: int = MISS_TOLERANCE,
        parent: QObject | None = None,
        timeout_ms: int = POLL_TIMEOUT_MS,
    ) -> None:
        super().__init__(parent)
        self._board = board
        self._tolerance = max(1, int(tolerance))
        self._timeout_ms = int(timeout_ms)
        self._misses = 0
        self._fired = False
        self._timer = QTimer(self)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self.poll)

    @property
    def misses(self) -> int:
        """Consecutive unconvincing replies so far. Reset by any good one."""
        return self._misses

    def start(self) -> None:
        """Begin polling."""
        if not self._fired:
            self._timer.start()

    def stop(self) -> None:
        """Stop polling. Idempotent, and safe to call from ``aboutToQuit``."""
        self._timer.stop()

    def poll(self) -> bool:
        """Ask once, and act if the answer has been the same too many times.

        Returns whether the board still looks open, which is what the tests
        assert on; the signal is the part the app uses.

        Skipped entirely while one of our own modal dialogs is up. The user is
        mid-answer in it — quite possibly in the "write these to the schematic?"
        question that closing raises — and pulling the window out from under
        that would lose the very changes the question exists to save. The board
        will still be closed at the next tick.

        Asked with :data:`POLL_TIMEOUT_MS` rather than the connection's own
        deadline, because this call blocks the thread that paints.
        """
        if self._fired:
            return False
        if QApplication.activeModalWidget() is not None:
            return True

        try:
            open_now = self._board.still_open(timeout_ms=self._timeout_ms)
        except NotConnected as exc:
            # Not the same as False, and deliberately not treated as such on the
            # strength of one reply: this is "KiCad did not answer", which a busy
            # editor produces too. Only the run of them below is evidence.
            log.debug("Board liveness check got no answer: %s", exc)
            open_now = False

        if open_now:
            self._misses = 0
            return True

        self._misses += 1
        if self._misses < self._tolerance:
            log.debug(
                "Board looks gone (%d of %d); waiting for confirmation",
                self._misses,
                self._tolerance,
            )
            return False

        self._fired = True
        self._timer.stop()
        log.info("The board this window was opened over has been closed.")
        self.board_closed.emit()
        return False


def close_window_with_board(board, window, application=None) -> BoardWatcher:
    """Wire a window to its board's lifetime and start watching. Returns the watcher.

    The window is closed rather than the application killed, so that everything
    a normal close does still happens: the geometry is saved, and the controller
    gets its last chance to offer writing unexported LCSC numbers into the
    schematic — which matters more here than on a deliberate close, because a
    removal that has not been exported lives only on a board that just went
    away.

    ``application`` is quit afterwards when given. The Explorer and the part
    details window are parented to the main window but are top-level, and Qt's
    ``quitOnLastWindowClosed`` counts them; without this the process would
    linger with a catalogue window open over a board that is gone. They are
    closed properly first — see :func:`_shut_down`.
    """
    watcher = BoardWatcher(board, parent=window)
    watcher.board_closed.connect(lambda: _shut_down(window, application))
    if application is not None:
        application.aboutToQuit.connect(watcher.stop)
    watcher.start()
    return watcher


def _shut_down(window, application) -> None:
    """Close our other windows, then this one, then take the process with it.

    **The order is the whole content of this function.** ``QApplication.quit()``
    leaves the event loop without running ``closeEvent`` on anything, so a
    window that is merely *open* at this moment never gets asked to close — and
    the Explorer's ``closeEvent`` is where in-flight fetches are cancelled, both
    thread pools cleared, and its geometry, ``overwrite_existing`` and
    ``library_folder`` written to settings. Quitting over the top of it silently
    threw all of that away, and it did so at the likeliest moment of all:
    closing the PCB is what a user does when they have finished shopping.

    Our windows go first, the main window second — its close can raise the
    "write these to the schematic?" prompt, and that must not open behind a
    catalogue window — and the quit last, as the backstop for anything that
    ignored its close.
    """
    for other in _our_windows(window):
        other.close()
    window.close()
    if application is not None:
        application.quit()


def _our_windows(window) -> list:
    """Return ``window``'s own top-level windows, front-most first.

    The Explorer and the part details window are modeless ``QDialog``s parented
    to the main window: children in Qt's object sense, separate windows in the
    user's. Found through the parent rather than through
    ``QApplication.topLevelWidgets()`` so that this only ever closes windows this
    window owns.

    Hidden ones are left alone — they have already had their ``closeEvent`` — and
    the order is reversed so the most recently built, which is the one in front,
    closes first.
    """
    return [
        child
        for child in reversed(window.findChildren(QWidget))
        if child.isWindow() and child.isVisible()
    ]


__all__ = [
    "MISS_TOLERANCE",
    "POLL_INTERVAL_MS",
    "POLL_TIMEOUT_MS",
    "BoardWatcher",
    "close_window_with_board",
]
