"""Background work for the Explorer: ``QThreadPool`` workers, Qt signals.

The replacement for the wx dialog's ``threading.Thread`` + ``wx.CallAfter``
pattern, and materially smaller than it, for one reason worth stating because it
recurs everywhere in this phase:

**A Qt signal to a destroyed receiver is not delivered, and does not raise.**
Qt severs a connection when either end is destroyed. The wx original needed
``_post()`` and ``_alive()`` — twenty lines plus a check at the top of every
callback — because ``wx.CallAfter`` reaches into the app object and the dialog's
proxy and raises *on the worker thread* once either has gone, where nothing
catches it and it lands as a traceback in KiCad's console. None of that
machinery has a job here.

What does carry over unchanged is the **staleness token**. Auto-disconnection
solves "the window is gone"; it says nothing about "these results are for the
previous search", which is a different problem and still ours. Every fill takes
a token, and :class:`Tokens` bumps them.

Concurrency stays bounded exactly as ARCHITECTURE.md §4 and the wx module set
it: separate pools, because the two fills have different budgets for different
reasons. Retail stock is one request per part against a host that rate-limits;
thumbnails are the heaviest and least decision-critical bytes in the window.
"""

from __future__ import annotations

from contextlib import suppress
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)

#: Retail stock is one HTTP request per part, so the fill is bounded on both
#: axes. Two workers, not five: five fetched a 100-row page in about ten seconds
#: and that was the problem — EasyEDA, the fallback that answers when
#: ``wmsc.lcsc.com`` will not, reads eight requests a second as abuse and then
#: 403s *every* request from that address for minutes. The fill was fast enough
#: to get the user banned from the data it was fetching.
RETAIL_FILL_LIMIT = 120
RETAIL_FILL_WORKERS = 2

#: Thumbnails: fewer rows and fewer workers than the numbers got. A picture
#: never decides a part choice on its own.
THUMB_FILL_LIMIT = 60
THUMB_FILL_WORKERS = 3


class _Signals(QObject):
    """Signals for one :class:`Task`.

    A separate object because ``QRunnable`` is not a ``QObject`` and so cannot
    carry signals itself. Parented to nothing and kept alive by the task.
    """

    #: ``(token, key, result)`` — key identifies which item finished.
    done = Signal(int, object, object)


class Task(QRunnable):
    """Run one callable on a pool and emit its result.

    Exceptions are swallowed to ``None`` deliberately. Every call this carries
    is a storefront lookup, and the module-wide rule is degrade, never crash: a
    part whose stock lookup raised is one cell that says "nobody answered", not
    a dead window. The traceback still reaches the log at debug level.
    """

    def __init__(self, token: int, key: Any, work: Callable[[], Any]) -> None:
        super().__init__()
        self.signals = _Signals()
        self._token = token
        self._key = key
        self._work = work

    def run(self) -> None:  # noqa: D102 - QRunnable override
        try:
            result = self._work()
        except Exception:  # noqa: BLE001 - one bad part, not a crash
            log.debug("background task for %r failed", self._key, exc_info=True)
            result = None
        self.signals.done.emit(self._token, self._key, result)


class Tokens:
    """The staleness guards, one per kind of fetch.

    Bumping a token is how an in-flight fill is abandoned: its results still
    arrive, and every handler drops them because the number no longer matches.
    Cheaper and more reliable than trying to cancel work already queued.
    """

    def __init__(self) -> None:
        self.search = 0
        self.detail = 0
        self.retail = 0
        self.thumb = 0

    def cancel_all(self) -> None:
        """Invalidate every in-flight fetch."""
        self.search += 1
        self.detail += 1
        self.retail += 1
        self.thumb += 1


class Pool:
    """A bounded thread pool that hands its results to one slot.

    ``start`` returns the token the work was queued under so a caller can keep
    it, and every task carries that token through to the receiving slot. The
    pool is *not* the app's global ``QThreadPool``: sharing that one would let a
    hundred queued thumbnails starve the reports the user is waiting on.
    """

    def __init__(self, name: str, workers: int, receiver: Callable) -> None:
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max(1, workers))
        self._pool.setObjectName(name)
        self._receiver = receiver
        #: Tasks are kept only until they have run; QRunnable auto-deletes, but
        #: the _Signals object must outlive the emit, so hold a reference.
        self._live: list[Task] = []

    def start(self, token: int, key: Any, work: Callable[[], Any]) -> None:
        """Queue ``work`` and deliver its result to the receiving slot."""
        task = Task(token, key, work)
        task.signals.done.connect(self._receiver)
        task.signals.done.connect(lambda *_: self._retire(task))
        self._live.append(task)
        self._pool.start(task)

    def _retire(self, task: Task) -> None:
        """Drop a finished task's signal object."""
        # A double-emit cannot happen, but a task retired twice must not raise
        # on a worker's completion path.
        with suppress(ValueError):
            self._live.remove(task)

    def drain(self, milliseconds: int = 2000) -> bool:
        """Wait for queued work to finish. For tests and orderly shutdown."""
        return self._pool.waitForDone(milliseconds)

    def clear(self) -> None:
        """Discard work that has not started yet."""
        self._pool.clear()


def bounded(items, limit: int) -> list:
    """Return the first ``limit`` of ``items``.

    A named helper rather than a slice at each call site so that a bound is
    never quietly absent: both fills are capped, and the caps are the reason a
    120-row page does not turn a soft rate limit into a ban.
    """
    return list(items)[:limit]


def optional_int(value: Any) -> Optional[int]:
    """Coerce to int, preserving ``None`` as "nobody answered"."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "RETAIL_FILL_LIMIT",
    "RETAIL_FILL_WORKERS",
    "THUMB_FILL_LIMIT",
    "THUMB_FILL_WORKERS",
    "Pool",
    "Task",
    "Tokens",
    "bounded",
    "optional_int",
]
