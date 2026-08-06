"""One window per board, enforced across processes.

The wx plugin could look for an existing window with
``wx.GetTopLevelWindows()``, because it ran inside KiCad. Out of process there is
nothing to look at: KiCad's toolbar button runs ``run.sh`` on every click, and
each click is a fresh process.

Two instances would open the same project database and the same board and
quietly overwrite each other, so the second process has to find the first and
ask it to come forward. A ``QLocalServer`` does both jobs at once: binding it
*is* the lock, and it doubles as the channel for "raise yourself".

Scoped per board rather than per application: two KiCad windows with different
boards open are two separate pieces of work and should get a window each.
"""

from __future__ import annotations

import hashlib
import logging
import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

#: How long to wait for the incumbent to acknowledge. Generous, because it may
#: be busy; still short enough that a stale socket does not hang the launch.
CONNECT_TIMEOUT_MS = 700

RAISE_MESSAGE = b"raise\n"


def _socket_name(board_path: str) -> str:
    """Return a per-board socket name.

    Hashed, because socket names are length-limited on some platforms and a
    board path is not, and because a path can contain characters a socket name
    cannot.
    """
    digest = hashlib.sha1(
        os.path.normcase(os.path.abspath(board_path)).encode("utf-8")
    ).hexdigest()[:16]
    return f"lcsc-suite-{digest}"


class SingleInstance(QObject):
    """Holds the lock for this board, and reports requests to be raised."""

    #: Emitted when another launch asked this window to come forward.
    raise_requested = Signal()

    def __init__(self, board_path: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = _socket_name(board_path)
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        """Try to become the one instance for this board.

        Returns ``True`` if we are it. ``False`` means another process already
        has a window open for this board and has been asked to raise it.
        """
        if notify_existing(self.name):
            return False

        server = QLocalServer(self)
        # A process killed with SIGKILL leaves its socket file behind and the
        # next launch would refuse to start for ever. notify_existing() above
        # has already established that nobody is listening, so removing it is
        # safe — and doing it only *after* that check is what keeps this from
        # stealing the lock from a live instance.
        QLocalServer.removeServer(self.name)
        if not server.listen(self.name):
            log.warning(
                "Could not claim the single-instance socket %s (%s); "
                "continuing without the guard.",
                self.name,
                server.errorString(),
            )
            return True
        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    def release(self) -> None:
        """Give up the lock."""
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(self.name)
            self._server = None

    def _on_connection(self) -> None:
        """Answer a second launch by asking the window to come forward."""
        if self._server is None:  # pragma: no cover - closed mid-callback
            return
        connection = self._server.nextPendingConnection()
        if connection is None:  # pragma: no cover - spurious wake-up
            return
        connection.readyRead.connect(lambda: self._read(connection))
        connection.disconnected.connect(connection.deleteLater)

    def _read(self, connection: QLocalSocket) -> None:
        """Handle one message from another launch."""
        if bytes(connection.readAll().data()).startswith(b"raise"):
            self.raise_requested.emit()
        connection.disconnectFromServer()


def notify_existing(name: str) -> bool:
    """Ask an existing instance to raise itself; report whether one answered."""
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(CONNECT_TIMEOUT_MS):
        return False
    socket.write(RAISE_MESSAGE)
    socket.flush()
    socket.waitForBytesWritten(CONNECT_TIMEOUT_MS)
    socket.disconnectFromServer()
    return True
