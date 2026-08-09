"""The BOM cost estimator — the presenter half of ``bom_widget.py`` (§5.8).

The widget half is already in the main window: the `Boards:` spin box, the
`Force Standard` checkbox, the `Help` button and the two-line summary have been
there since Phase 1. What was missing is the thing that fills them in, and the
two consequences of its absence that Phase 2 and Phase 3 both recorded as open:
``PartTableModel.set_standard_trigger_refs()`` was called by nobody, so the amber
Standard-mode advisory was unreachable, and the summary line said "no assigned
BOM parts" on every board including full ones.

**None of the arithmetic is here.** `bom_estimation/pricing.py` and
`bom_estimation/view.py` are toolkit-free, already tested, and imported through
`shared.py` unchanged — they are the "ported nearly unchanged" half of plan §3
and this is the fifty lines of glue the wx version wrapped them in.

The one real port is :func:`board_standard_context`. The wx controller walks the
board through pcbnew — ``FindFootprintByReference``, ``IsFlipped()``,
``GetLayer() != F_Cu`` — to learn which sides are populated. Over IPC there is no
board object to walk: ``FootprintView.side`` is already ``"top"`` or ``"bottom"``,
computed once by the bridge, so the walk becomes a dictionary lookup and the two
``suppress`` blocks that existed because SWIG objects die between layout rebuilds
have nothing to guard.

What *is* preserved exactly is which parts get marked. ``trigger_references``
holds only the parts individually responsible for Standard mode; the other three
signals (a manual override, quantity ≥ 50, both sides populated) are properties
of the whole board and no single part is to blame for any of them. Marking every
row on a two-sided board painted the entire list and told the user nothing —
``build_standard_mode_context`` is deliberately never handed the full reference
list so it cannot go back to doing that.
"""

from __future__ import annotations

from contextlib import suppress
import json
import logging
import time
from typing import Optional

from ..shared import bom_help_text, bom_view
from .explorer.tasks import Pool, bounded

log = logging.getLogger(__name__)

#: How many parts one enrichment pass will look up. The same bound and the same
#: reason as the Explorer's retail fill: this is one request per *distinct
#: number* against a host that rate-limits, and an unbounded pass on a
#: 300-reference board is how a soft limit becomes a ban. What gets dropped is
#: logged, never silently truncated — the next pass picks it up, because the
#: store only ever reports the rows still missing data.
ENRICH_LIMIT = 120

#: Seconds between requests, matching ``LCSCAssemblyMetadataProvider``'s
#: ``min_interval_seconds=1.0``. Enforced by sleeping in the worker on a
#: one-thread pool, which serialises to at most one request a second without
#: needing a scheduler.
ENRICH_INTERVAL = 1.0


def assembly_flags(part) -> dict:
    """Read the estimator flags ``store.py`` persists as JSON on each part."""
    flags: dict = {}
    with suppress(TypeError, ValueError, json.JSONDecodeError):
        flags = json.loads(part.get("assembly_flags") or "{}")
    return flags if isinstance(flags, dict) else {}


def board_standard_context(
    parts,
    sides: dict,
    board_count: int,
    force_standard: bool,
    assembly_count=None,
) -> dict:
    """Work out whether this board prices as Standard, and which parts say so.

    ``sides`` is ``{reference: "top" | "bottom"}`` from the bridge. A reference
    the board does not have is skipped rather than defaulted to the top side:
    the part list can outlive a footprint deleted in KiCad while this window was
    open, and inventing a side for it would fabricate a two-sided board.
    """
    populated: set[str] = set()
    smt_populated: set[str] = set()
    standard_refs: set[str] = set()

    for part in parts:
        if part.get("exclude_from_bom") or not str(part.get("lcsc") or ""):
            continue
        flags = assembly_flags(part)
        if flags.get("is_dnp") or flags.get("exclude_from_pos"):
            # Nothing is placed for these, so they cost no assembly and cannot
            # make a side "populated".
            continue
        reference = str(part.get("reference") or "")
        side = sides.get(reference)
        if not reference or side is None:
            continue

        populated.add(side)
        is_tht = False
        with suppress(TypeError, ValueError):
            is_tht = bool(int(part.get("has_tht") or 0))
        if not is_tht:
            smt_populated.add(side)
        with suppress(TypeError, ValueError):
            if int(part.get("component_product_type")) != 0:
                standard_refs.add(reference)

    return bom_view.build_standard_mode_context(
        manual_enabled=bool(force_standard),
        board_count=board_count,
        populated_sides=populated,
        smt_populated_sides=smt_populated,
        standard_part_refs=standard_refs,
        assembly_count=assembly_count,
    )


def normalise_metadata(detail: dict) -> tuple[str, Optional[int]]:
    """Pull ``(assembly process, component product type)`` off an assembly record.

    ``LCSCAssemblyMetadataProvider._normalize`` in two lines, reading the same
    two keys off the same payload. It is not reused because the provider owns
    its own transport (``lcsc_api.LCSC_API``), which is exactly the shape the
    offline guarantee forbids — the search source already fetches this record,
    behind the host breaker and out of the shared cache.

    ``component_product_type`` is what decides the amber Standard-mode advisory:
    anything non-zero is a part JLC will not place on the Economic line.
    """
    if not isinstance(detail, dict):
        return "", None
    process = str(detail.get("assemblyProcess") or "")
    raw = detail.get("componentProductType")
    product_type: Optional[int] = None
    with suppress(TypeError, ValueError):
        if raw is not None and not isinstance(raw, bool):
            product_type = int(raw)
    return process, product_type


def help_text() -> tuple[str, str]:
    """Return the shared estimator help as ``(title, body)``."""
    return (
        bom_help_text.BOM_ESTIMATOR_HELP_TITLE,
        bom_help_text.get_bom_estimator_help_text(),
    )


class BomEstimator:
    """Recomputes the cost estimate and pushes it into the window.

    Holds no widgets and takes no decisions about the board — it reads the
    project database, asks the local part cache for prices and writes three
    things back: the summary line, the per-reference price labels and the set of
    Standard-mode trigger references.

    **The part cache only, never the network.** Same rule as
    ``PartList.rows()``: this runs on the UI thread every time the list is
    rebuilt or the board count changes, and a stale price beats a frozen window.
    A part the cache has never seen contributes ``N/A`` and is counted in the
    summary's ``Missing prices``, which is the honest answer and is already what
    the summary line is for.
    """

    def __init__(
        self, window, parts, source=None, interval: Optional[float] = None
    ) -> None:
        self.window = window
        self.parts = parts
        #: Where :meth:`enrich` gets assembly records. ``None`` disables the
        #: pass entirely, which is what a test that only cares about the
        #: arithmetic wants — and what any caller that has not deliberately
        #: chosen a source gets, so no test can reach the network by omission.
        self.source = source
        #: Seconds between lookups. Derived from the source rather than passed,
        #: because there is exactly one thing the pacing is for and the source
        #: is what knows whether it applies: a fixture has no host to be polite
        #: to, and making a probe sleep 29 seconds per screen for a rate limit
        #: that does not exist is how the pacing would end up being removed.
        if interval is None:
            interval = 0.0 if getattr(source, "offline", False) else ENRICH_INTERVAL
        self.interval = float(interval)
        #: Per-recompute memo, so a board with twenty identical capacitors asks
        #: the cache once rather than twenty times.
        self._details: dict[str, dict] = {}
        #: One worker, so ``interval`` alone bounds the request rate.
        self._pool = Pool("lcsc-enrich", 1, self._on_enriched)
        #: Numbers currently being looked up, so a second pass does not queue
        #: them again. The wx plugin keeps the same set for the same reason.
        self._pending: set[str] = set()
        #: Numbers this session asked about and got nothing for. Kept because
        #: the *store* cannot know: nothing was written, so it goes on reporting
        #: them as targets, and without this every reload would re-ask a host
        #: that has already said no. Session-scoped on purpose — reopening the
        #: window is the natural way to retry after fixing a connection, and it
        #: is the same gesture as the Explorer's `Refresh data`.
        self._unanswered: set[str] = set()
        #: Bumped when the board changes under an in-flight pass. Results
        #: carrying an older token are dropped — an assignment between spawn and
        #: delivery would otherwise write one part's metadata onto another's
        #: reference. The wx version calls this a "generation".
        self._token = 0
        #: What the assignments looked like when the last pass was queued. See
        #: :meth:`enrich`.
        self._signature = None

    # -- inputs ---------------------------------------------------------------

    def board_count(self) -> int:
        """Return the number of bare boards the estimate is for."""
        return int(self.window.boards_input.value())

    def assembly_count(self) -> int:
        """Return how many of those boards JLC populates.

        Falls back to the board count for a window that predates the second
        spin box — the probe builds trimmed windows, and an estimator that
        raised on one would take the screenshots with it.
        """
        spin = getattr(self.window, "assembly_input", None)
        return self.board_count() if spin is None else int(spin.value())

    def force_standard(self) -> bool:
        """Whether the user has forced Standard-mode pricing."""
        return bool(self.window.force_standard.isChecked())

    def part_details(self, lcsc: str) -> dict:
        """Look one part's cached details up, once per recompute."""
        if lcsc not in self._details:
            self._details[lcsc] = self.parts.details_for(lcsc)
        return self._details[lcsc]

    def sides(self) -> dict:
        """Return ``{reference: side}`` from the board."""
        return {view.reference: view.side for view in self.parts.board.footprints()}

    # -- output ---------------------------------------------------------------

    def recompute(self) -> Optional[dict]:
        """Recalculate and apply the estimate. Returns the view model, or ``None``.

        ``None`` when there is nothing to price — no parts at all, or none that
        are both assigned and in the BOM. Those two cases say different things
        in the summary line and neither is an error: a board nobody has assigned
        yet is the normal starting state.
        """
        self._details.clear()
        model = self.window.part_model
        board_count = self.board_count()
        assembly_count = self.assembly_count()

        try:
            parts = list(self.parts.store.read_all() or [])
        except Exception:  # noqa: BLE001 - a broken store must not kill the window
            log.warning(
                "Could not read the project database for the estimate", exc_info=True
            )
            return None

        billable = [
            part
            for part in parts
            if not part.get("exclude_from_bom") and str(part.get("lcsc") or "")
        ]
        if not billable:
            model.set_standard_trigger_refs(set())
            quantity = bom_view.format_quantity(board_count, assembly_count)
            self.window.set_summary_text(
                f"BOM Estimate ({quantity}): "
                f"{'no parts' if not parts else 'no assigned BOM parts'}"
            )
            return None

        context = board_standard_context(
            parts,
            self.sides(),
            board_count,
            self.force_standard(),
            assembly_count,
        )
        view_model = bom_view.build_bom_estimate_view_model(
            parts, board_count, self.part_details, context, assembly_count
        )

        for reference, label in bom_view.prepare_bom_price_labels(
            parts, board_count, self.part_details, assembly_count
        ).items():
            model.set_bom_price(str(reference), label)

        model.set_standard_trigger_refs(view_model["highlight_refs"])
        self.window.set_summary_text(view_model["summary_label"])
        return view_model

    # -- enrichment -----------------------------------------------------------
    #
    # ``component_product_type`` is the one input to the estimate that is on
    # neither the board nor the part cache: it comes from JLC's assembly record,
    # one request per distinct number. Without it ``standard_part_refs`` is
    # always empty, the board never prices as Standard for a part-level reason,
    # and the amber advisory the part table has drawn since Phase 2 is
    # unreachable — which is exactly what the migration plan's resume list said
    # was still open.

    def enrich(self) -> int:
        """Fetch the missing assembly metadata in the background.

        Returns how many numbers were queued. Cheap to call on every reload: the
        store reports only the rows that still have no metadata, and numbers
        already in flight are filtered out, so a second pass over a filled
        database queues nothing.
        """
        if self.source is None:
            return 0
        # Detected here rather than announced by the caller: an assignment
        # between spawning a lookup and its result arriving would write one
        # part's metadata onto a reference that now holds another's, and a rule
        # the estimator enforces itself cannot be forgotten at a fourth call
        # site the way an `invalidate()` next to every write can.
        signature = self._assignment_signature()
        if signature != self._signature:
            self.invalidate()
            self._signature = signature
        try:
            targets = self.parts.store.get_assembly_enrichment_targets() or {}
        except Exception:  # noqa: BLE001 - an older project database may lack the columns
            log.debug("Could not list enrichment targets", exc_info=True)
            return 0
        skip = self._pending | self._unanswered
        wanted = {lcsc: refs for lcsc, refs in targets.items() if lcsc not in skip}
        if not wanted:
            return 0

        queued = bounded(sorted(wanted), ENRICH_LIMIT)
        dropped = len(wanted) - len(queued)
        if dropped:
            log.info(
                "Looking up assembly metadata for %d parts; %d more will follow "
                "on the next refresh",
                len(queued),
                dropped,
            )
        self._pending.update(queued)
        token = self._token
        source, interval = self.source, self.interval

        for index, lcsc in enumerate(queued):

            def work(number=lcsc, position=index):
                # Sleep first, not last: the delay belongs *before* a request so
                # the very first one is not free and a cancelled tail costs
                # nothing. Position 0 goes straight out.
                if interval and position:
                    time.sleep(interval)
                return normalise_metadata(source.assembly_detail(number))

            self._pool.start(token, lcsc, work)
        return len(queued)

    def _on_enriched(self, token: int, lcsc, result) -> None:
        """Persist one lookup and, when the batch drains, recompute."""
        if token != self._token:
            # A stale result: the board changed while this was in flight, and
            # the references this number was queued for may point elsewhere now.
            # Nothing to discard — invalidate() emptied the set when it bumped
            # the token, and removing a name the *current* batch is waiting on
            # would drain it early.
            return
        self._pending.discard(lcsc)
        if result is not None:
            process, product_type = result
            if process or product_type is not None:
                for reference in self._references_for(lcsc):
                    self.parts.store.set_assembly_metadata(
                        reference, process, product_type
                    )
            else:
                # Nobody answered. **Do not write the empty result**, which is
                # what the wx plugin does: an endpoint that 403s today would
                # overwrite metadata fetched correctly yesterday, and the
                # estimate would silently drop back to Economic for a reason
                # nothing on screen could explain. The cost of not writing is
                # that the store keeps reporting this number as a target, so
                # remember the refusal for the session instead.
                self._unanswered.add(lcsc)
                log.debug("No assembly metadata for %s", lcsc)
        if not self._pending:
            # Once per batch, not once per part: the estimate is a whole-board
            # figure and recomputing it ninety times would repaint the list
            # ninety times to show the same answer.
            self.recompute()

    def _references_for(self, lcsc: str) -> list[str]:
        """Return the references carrying ``lcsc`` right now.

        Re-read rather than captured when the task was queued. A number can have
        been assigned to more rows, or cleared from some, while the request was
        in flight, and writing metadata to a reference that no longer holds this
        part is the mistake the token guards against at the coarse level and
        this one closes at the fine level.
        """
        try:
            parts = self.parts.store.read_all() or []
        except Exception:  # noqa: BLE001 - the recompute path already reports this
            return []
        return [
            str(part.get("reference") or "")
            for part in parts
            if str(part.get("lcsc") or "") == lcsc and part.get("reference")
        ]

    def _assignment_signature(self):
        """Return a cheap summary of which reference carries which number."""
        try:
            parts = self.parts.store.read_all() or []
        except Exception:  # noqa: BLE001 - the recompute path already reports this
            return None
        return tuple(
            sorted(
                (str(part.get("reference") or ""), str(part.get("lcsc") or ""))
                for part in parts
            )
        )

    def invalidate(self) -> None:
        """Abandon any in-flight lookups, because the assignments changed."""
        self._token += 1
        self._pending.clear()
        self._pool.clear()


__all__ = [
    "ENRICH_INTERVAL",
    "ENRICH_LIMIT",
    "BomEstimator",
    "assembly_flags",
    "board_standard_context",
    "help_text",
    "normalise_metadata",
]
