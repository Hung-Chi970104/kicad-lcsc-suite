"""Filling the part-detail cache — the thing that makes three columns fill.

``PartList.rows()`` resolves Type / JLC Stock / LCSC Params from the **local**
cache and never the network, which is deliberate and documented: it runs once
per assigned part while the list is built on the UI thread, and serving a stale
row unconditionally is what makes an offline session work.

Nothing filled that cache. ``library.set_cached_part_details`` had exactly one
caller in the whole app — ``parts.open_fixture_library``, which the probe uses
to seed a throwaway directory for the screenshots. So the committed PNGs showed
the three columns populated while a real board showed them blank, and the
blankness looked like a rendering bug rather than an empty table. The wx plugin
had ``mainwindow.start_part_detail_refresh``; it went with the Phase 8 cutover
and was never replaced. This is the replacement.

It is deliberately the same shape as :class:`~lcsc_suite.ui.bom_estimator.
BomEstimator`'s enrichment pass, down to the constants, because the two are the
same problem: one request per *distinct* LCSC number against a host that rate
limits, spawned from a list rebuild that can happen six ways.

* **One worker and a sleep-first delay**, so ``REFRESH_INTERVAL`` alone bounds
  the request rate without a scheduler.
* **Bounded per pass**, and what gets dropped is logged. The next reload picks
  it up, because the cache goes on reporting those numbers as stale.
* **A generation token**, bumped when the assignments change, so a result that
  arrives after a reassignment cannot be written onto the reference that now
  holds a different part.
* **An empty answer is never written.** A host that 403s today would otherwise
  overwrite details fetched correctly yesterday, and three columns would go
  blank for a reason nothing on screen could explain. Refusals are remembered
  for the session instead, which is also what stops every reload re-asking a
  host that has already said no.

Where it does *not* follow the estimator: the fetch goes through the search
source rather than calling ``lcsc/details.py`` directly. That is the offline
guarantee — see :meth:`lcsc_suite.search_source.FixtureSource.part_details` for
why a fixture cannot be allowed down this path.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal

# The private name on purpose: this is the *same* computation the list does when
# it builds a row, not a second spelling of it. Params arriving late have to be
# re-matched against the row's own value and footprint or the highlight would be
# missing on exactly the rows this pass just filled in.
from ..parts import _match_terms
from ..shared import derive_params
from .explorer.tasks import Pool, bounded
from .models.part_table import as_stock

log = logging.getLogger(__name__)

#: How many distinct numbers one pass will look up. The estimator's bound and
#: the estimator's reason: an unbounded pass over a 300-reference board is how a
#: soft rate limit becomes a ban.
REFRESH_LIMIT = 120

#: Seconds between requests. Enforced by sleeping in the worker on a one-thread
#: pool, which serialises to at most one request a second.
REFRESH_INTERVAL = 1.0

#: A detail mapping has to carry at least one of these to be worth keeping.
#: ``fetch_details`` returns every key blank when both endpoints missed, and
#: caching that would make "nobody answered" indistinguishable from "asked and
#: it really is empty" for the next 24 hours.
ANSWER_FIELDS = ("type", "description", "package", "part_no", "stock", "price")


def is_answered(details) -> bool:
    """Report whether a detail lookup actually learned anything."""
    if not isinstance(details, dict):
        return False
    return any(str(details.get(field) or "") for field in ANSWER_FIELDS)


class PartDetailRefresher(QObject):
    """Fetches missing part details in the background and fills the columns."""

    #: Emitted when a batch drains, so the estimator can re-price with the
    #: ladders this pass just cached. Once per batch, never once per part.
    finished = Signal()

    def __init__(
        self,
        window,
        parts,
        source=None,
        interval: Optional[float] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.window = window
        self.parts = parts
        #: Where details come from. ``None`` disables the pass entirely, which
        #: is what a test that has not deliberately chosen a source gets — so
        #: no test can reach the network by omission.
        self.source = source
        #: Seconds between lookups, derived from the source for the same reason
        #: the estimator derives its own: a fixture has no host to be polite to.
        if interval is None:
            interval = 0.0 if getattr(source, "offline", False) else REFRESH_INTERVAL
        self.interval = float(interval)
        #: Numbers in flight, so a second pass does not queue them again.
        self._pending: set[str] = set()
        #: Numbers answered this session. The cache is the real memory; this
        #: covers the case where there is no library to write to, so that a
        #: broken data directory costs one lookup per number rather than one
        #: per reload forever.
        self._fetched: set[str] = set()
        #: Numbers this session asked about and got nothing for. See the module
        #: docstring — session-scoped on purpose, so reopening the window is the
        #: way to retry after fixing a connection.
        self._unanswered: set[str] = set()
        #: Bumped when the assignments change under an in-flight pass.
        self._token = 0
        #: What the assignments looked like when the last pass was queued.
        self._signature = None
        #: One worker, so ``interval`` alone bounds the request rate.
        self._pool = Pool("lcsc-details", 1, self._on_details)

    # -- the pass -----------------------------------------------------------

    def refresh(self) -> int:
        """Queue lookups for the numbers the cache is missing or stale on.

        Returns how many were queued. Cheap to call on every reload: a cache
        that already holds fresh rows for every assigned number queues nothing.
        """
        if self.source is None or self.parts is None:
            return 0
        # Detected here rather than announced by the caller, exactly as the
        # estimator does it: a rule the object enforces itself cannot be
        # forgotten at a call site that gets added later.
        signature = self._assignment_signature()
        if signature != self._signature:
            self.invalidate()
            self._signature = signature

        numbers = self._assigned_numbers()
        if not numbers:
            return 0
        wanted = [
            number
            for number in self._stale(numbers)
            if number not in self._pending
            and number not in self._unanswered
            and number not in self._fetched
        ]
        if not wanted:
            return 0

        queued = bounded(wanted, REFRESH_LIMIT)
        dropped = len(wanted) - len(queued)
        if dropped:
            log.info(
                "Looking up details for %d parts; %d more will follow on the "
                "next refresh",
                len(queued),
                dropped,
            )
        self._pending.update(queued)
        token = self._token
        source, interval = self.source, self.interval

        for index, lcsc in enumerate(queued):

            def work(number=lcsc, position=index):
                # Sleep first: the delay belongs *before* a request, so the very
                # first one is not free and an abandoned tail costs nothing.
                if interval and position:
                    time.sleep(interval)
                return source.part_details(number)

            self._pool.start(token, lcsc, work)
        return len(queued)

    def invalidate(self) -> None:
        """Abandon any in-flight lookups, because the assignments changed."""
        self._token += 1
        self._pending.clear()
        self._pool.clear()

    # -- results ------------------------------------------------------------

    def _on_details(self, token: int, lcsc, details) -> None:
        """Cache one answer and paint it into every row carrying that number."""
        if token != self._token:
            # Stale: the board changed while this was in flight, and the rows
            # this number was queued for may hold something else now. Nothing to
            # discard — invalidate() emptied the set when it bumped the token.
            return
        self._pending.discard(lcsc)
        if not is_answered(details):
            self._unanswered.add(lcsc)
            log.debug("No details for %s", lcsc)
        else:
            self._fetched.add(lcsc)
            self._store(details)
            self._apply(str(lcsc), details)
        if not self._pending:
            self.finished.emit()

    def _store(self, details: dict) -> None:
        """Write one answer into the part cache, if there is one to write to."""
        library = getattr(self.parts, "library", None)
        if library is None:
            return
        try:
            library.set_cached_part_details(details)
        except Exception:  # noqa: BLE001 - a broken cache costs a column, not the window
            log.debug(
                "Could not cache details for %s", details.get("lcsc"), exc_info=True
            )

    def _apply(self, lcsc: str, details: dict) -> None:
        """Fill the three columns on every row that carries ``lcsc`` right now.

        The rows are re-read rather than captured when the task was queued: a
        number can have been assigned to more rows, or cleared from some, while
        the request was in flight.
        """
        model = getattr(self.window, "part_model", None)
        if model is None:
            return
        params = derive_params.params_for_part(details)
        part_type = str(details.get("type") or "")
        stock = as_stock(details.get("stock"))

        for row in model.rows():
            if row.lcsc != lcsc:
                continue
            changes = {"params": params, "part_type": part_type}
            if stock is not None:
                # Only a figure that was actually answered. Writing ``None`` back
                # would turn a stock count the Explorer confirmed at assignment
                # time into the "?" that means nobody answered.
                changes["stock"] = stock
                self._remember_stock(row.reference, stock)
            # Re-matched against this row's own value and footprint, because the
            # terms are derived from the params that only just arrived.
            row.params = params
            changes["match_terms"] = _match_terms(row)
            model.update_row(row.reference, **changes)

    def _remember_stock(self, reference: str, stock: int) -> None:
        """Persist a fetched stock figure, so a reload does not lose it."""
        try:
            self.parts.store.set_stock(reference, int(stock))
        except Exception:  # noqa: BLE001 - the column is filled either way
            log.debug("Could not record stock for %s", reference, exc_info=True)

    # -- inputs -------------------------------------------------------------

    def _assigned_numbers(self) -> list:
        """Return the distinct LCSC numbers on the board, in a stable order."""
        try:
            parts = self.parts.store.read_all() or []
        except Exception:  # noqa: BLE001 - a broken store is reported by the reload path
            log.debug("Could not read the project database for a detail pass")
            return []
        return list(
            dict.fromkeys(
                str(part.get("lcsc") or "") for part in parts if part.get("lcsc")
            )
        )

    def _stale(self, numbers) -> list:
        """Return which of ``numbers`` the cache cannot answer freshly.

        With no library open there is no cache to ask, so every number is a
        target — bounded in practice by ``_fetched``, which is why that set
        exists.
        """
        library = getattr(self.parts, "library", None)
        if library is None:
            return list(numbers)
        try:
            return list(library.get_part_numbers_needing_refresh(numbers))
        except Exception:  # noqa: BLE001 - an unreadable cache means "ask again"
            log.debug("Could not check the part cache for staleness", exc_info=True)
            return list(numbers)

    def _assignment_signature(self):
        """Return a cheap summary of which reference carries which number."""
        try:
            parts = self.parts.store.read_all() or []
        except Exception:  # noqa: BLE001 - reported by the reload path
            return None
        return tuple(
            sorted(
                (str(part.get("reference") or ""), str(part.get("lcsc") or ""))
                for part in parts
            )
        )


__all__ = [
    "ANSWER_FIELDS",
    "REFRESH_INTERVAL",
    "REFRESH_LIMIT",
    "PartDetailRefresher",
    "is_answered",
]
