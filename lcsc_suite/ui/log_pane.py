"""The scrolling log pane at the bottom of the main window.

Same four columns as the wx plugin: timestamp, level, function, message. It is
the only place a user can see what a background worker did, which matters more
here than it did in-process — KiCad's console is not this app's console any more,
so an unlogged failure is invisible.

The one subtlety is threading. Log records arrive from ``QThreadPool`` workers,
and touching a widget off the UI thread is a crash. So the handler emits a Qt
signal instead: signal delivery across threads is queued through the receiving
object's event loop, which is exactly the marshalling ``wx.CallAfter`` did.
"""

from __future__ import annotations

from contextlib import suppress
from html import escape
import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from . import theme

#: Matches the wx plugin's log line so the two are comparable during the
#: migration: "12:34:56 INFO    populate_footprint_list Loaded 110 parts".
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(funcName)s %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"

#: Cap on retained lines. A DB download logs a line per chunk and a full part
#: fill one per part; unbounded, the pane becomes the app's memory profile.
MAX_LINES = 5000


class _Bridge(QObject):
    """Carries a formatted log line from any thread onto the UI thread."""

    line = Signal(str, int)


class LogHandler(logging.Handler):
    """A logging handler that appends to a :class:`LogPane`.

    Deliberately not holding the widget: a handler installed on the root logger
    outlives the window, and a stale reference to a deleted widget is the
    Qt equivalent of the ``RuntimeError`` a late ``wx.CallAfter`` raised.
    """

    def __init__(self, bridge: _Bridge) -> None:
        super().__init__()
        self._bridge = bridge
        self.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        """Format ``record`` and hand it to the UI thread."""
        # The window can go away between the log call and here. Dropping the
        # line is correct; raising would take the worker thread down with it,
        # which is the failure mode a bare wx.CallAfter had.
        with suppress(RuntimeError):
            self._bridge.line.emit(self.format(record), record.levelno)


class LogPane(QPlainTextEdit):
    """Read-only, monospaced, auto-scrolling log view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("log-pane")
        self.setReadOnly(True)
        self.setFont(theme.mono_font())
        self.setMaximumBlockCount(MAX_LINES)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # A *floor*, not the size — ``MainWindow`` sets the initial split. It has
        # to leave the per-part toolbar enough room for all ten of its buttons at
        # the default window size, because a splitter minimum wins over the sizes
        # asked for and the toolbar is what silently gives way: at 120 the
        # two-line BOM estimate pushed `Save mappings` behind an extension arrow.
        # Six lines of log is still a usable log, and the splitter drags.
        self.setMinimumHeight(96)

        self._bridge = _Bridge(self)
        self._bridge.line.connect(self._append, Qt.ConnectionType.QueuedConnection)
        self.handler = LogHandler(self._bridge)

    def install(self, level: int = logging.INFO) -> None:
        """Attach to the root logger, showing ``level`` and above.

        The handler carries the level itself rather than trusting the root's.
        Anything at all may have lowered that — ``derive_params`` calls
        ``basicConfig(DEBUG)`` at import — and this pane is the one place where
        the difference is visible to a user rather than to a log file.
        """
        root = logging.getLogger()
        self.handler.setLevel(level)
        root.addHandler(self.handler)
        if root.level > level:
            root.setLevel(level)

    def uninstall(self) -> None:
        """Detach from the root logger.

        Called on window close: a handler left behind keeps emitting into a
        deleted widget, and the app can be reopened from the toolbar button.
        """
        logging.getLogger().removeHandler(self.handler)

    def _append(self, line: str, level: int) -> None:
        """Append one line, colouring warnings and errors."""
        colour = None
        if level >= logging.ERROR:
            colour = theme.colour("bad")
        elif level >= logging.WARNING:
            colour = theme.colour("low")

        if colour is None:
            self.appendPlainText(line)
        else:
            # appendHtml is the only way to colour a single block; escape first,
            # because a log message can contain anything at all.
            self.appendHtml(
                f'<span style="color:{colour.name()}">{escape(line)}</span>'
            )
        self.moveCursor(QTextCursor.MoveOperation.End)
