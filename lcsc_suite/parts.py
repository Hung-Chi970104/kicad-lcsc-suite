"""Reconciling the board, the project database and the part list.

The wx plugin does this inside ``mainwindow.populate_footprint_list``. Pulled out
here because the same sequence is wanted from three places (start-up, after an
assignment, after a BOM/POS toggle) and because none of it needs a widget.

The order matters and is not arbitrary:

1. read the board through :mod:`lcsc_suite.kicad_bridge` — snapshots, never
   handles;
2. hand those to ``store.update_from_parts``, which owns the reconciliation
   rules (the ``lcsc_priority`` setting, and "value or footprint changed, so the
   number is no longer trustworthy");
3. read the reconciled rows back out of the store, because *it* is the authority
   on what is assigned — the board can lag behind it and vice versa;
4. resolve Type / JLC Stock / LCSC Params from the **local** part cache only.

Step 4 never touches the network. It is called once per assigned part while the
list is being built on the UI thread, and it deliberately serves stale rows: a
day-old stock figure beats a blank column, and that is what makes an offline
session work. Refreshing the cache is a background job (Phase 4).
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Sequence

from .kicad_bridge import Board, FootprintView
from .shared import derive_params, store as store_module
from .ui.models.part_table import PartRow

log = logging.getLogger(__name__)


class _StoreOwner:
    """The ``parent`` that ``store.py`` and ``library.py`` expect.

    Both reach into ``parent.settings`` as a plain mapping-of-mappings, which is
    the contract that lets them be reused unchanged. This adapts our Settings
    object to it rather than changing them.
    """

    def __init__(self, settings) -> None:
        self._settings = settings

    @property
    def settings(self) -> dict:
        """Return the settings as the nested dict the logic modules expect."""
        if self._settings is None:
            return {}
        return self._settings.values


def board_part_records(footprints: Sequence[FootprintView]) -> list:
    """Turn bridge snapshots into the records ``store.update_from_parts`` wants."""
    return [
        {
            "reference": view.reference,
            "value": view.value,
            "footprint": view.footprint,
            "lcsc": view.lcsc,
            "exclude_from_bom": view.exclude_from_bom,
            "exclude_from_pos": view.exclude_from_pos,
            "pad_count": view.pad_count,
            "has_tht": view.has_tht,
            "assembly_flags": _assembly_flags(view),
        }
        for view in footprints
    ]


def _assembly_flags(view: FootprintView) -> str:
    """Serialise the estimator's assembly flags, as ``footprint_metadata`` does.

    Same key order and same JSON shape, so a project database written by the wx
    plugin and one written here are interchangeable — which they have to be while
    both halves are installed.
    """
    return json.dumps(
        {
            "exclude_from_bom": bool(view.exclude_from_bom),
            "exclude_from_pos": bool(view.exclude_from_pos),
            "is_dnp": bool(view.dnp),
        },
        sort_keys=True,
    )


class PartList:
    """The board, the project database and the displayed rows, kept in step."""

    def __init__(self, board: Board, settings=None, library=None) -> None:
        self.board = board
        self.settings = settings
        self.library = library
        self.owner = _StoreOwner(settings)
        info = board.info()
        # board=None: the store has no pcbnew object to read here, and is fed
        # bridge snapshots through update_from_parts instead.
        self.store = store_module.Store(self.owner, info.project_path, None)
        self.hide_excluded_bom = False
        self.hide_excluded_pos = False

    # -- reading ------------------------------------------------------------

    def refresh_from_board(self) -> None:
        """Re-read the board and reconcile the project database against it."""
        footprints = self.board.footprints(refresh=True)
        self.store.update_from_parts(board_part_records(footprints))

    def rows(self) -> list:
        """Build the displayed rows, honouring the two hide filters."""
        dnp = {view.reference: view.dnp for view in self.board.footprints()}
        details_by_lcsc: dict = {}
        rows = []
        for part in self.store.read_all():
            if self.hide_excluded_bom and part.get("exclude_from_bom"):
                continue
            if self.hide_excluded_pos and part.get("exclude_from_pos"):
                continue
            lcsc = part.get("lcsc") or ""
            if lcsc and lcsc not in details_by_lcsc:
                details_by_lcsc[lcsc] = self._details(lcsc)
            details = dict(details_by_lcsc.get(lcsc, {}))
            if details:
                details["params"] = derive_params.params_for_part(details)
            row = PartRow.from_store(part, details)
            row.dnp = bool(dnp.get(row.reference, False))
            rows.append(row)
        return rows

    def _details(self, lcsc: str) -> dict:
        """Resolve one part's details from local storage only.

        Never the network: this runs once per assigned part while the list is
        being built on the UI thread. With no library configured it returns
        nothing, and the Type / Stock / Params columns stay empty rather than the
        window refusing to open.
        """
        if self.library is None:
            return {}
        try:
            return self.library.get_part_details(lcsc) or {}
        except Exception:  # noqa: BLE001 - a broken cache must not block the list
            log.debug("Local detail lookup for %s failed", lcsc, exc_info=True)
            return {}

    # -- writing ------------------------------------------------------------

    def set_exclusions(
        self,
        references: Sequence[str],
        bom: Optional[bool] = None,
        pos: Optional[bool] = None,
    ) -> None:
        """Set BOM/POS exclusions on the board, then mirror them into the store.

        Board first, deliberately. The bridge verifies its writes by re-reading,
        so if the board did not take the change this raises before the database
        has been told otherwise — and a project database that disagrees with the
        board is exactly the state that makes a BOM wrong.
        """
        if bom is not None:
            self.board.set_exclude_from_bom(dict.fromkeys(references, bom))
            for reference in references:
                self.store.set_bom(reference, int(bool(bom)))
        if pos is not None:
            self.board.set_exclude_from_pos(dict.fromkeys(references, pos))
            for reference in references:
                self.store.set_pos(reference, int(bool(pos)))

    def toggle_exclusions(
        self, references: Sequence[str], bom: bool = False, pos: bool = False
    ) -> None:
        """Flip BOM and/or POS exclusion on each of ``references``.

        Per reference rather than in one batch with a single target state: a
        mixed selection has no single "toggled" state, and forcing one would
        silently include parts the user had excluded on purpose.
        """
        by_ref = {view.reference: view for view in self.board.footprints()}
        if bom:
            wanted = {
                ref: not by_ref[ref].exclude_from_bom
                for ref in references
                if ref in by_ref
            }
            self.board.set_exclude_from_bom(wanted)
            for reference, state in wanted.items():
                self.store.set_bom(reference, int(state))
        if pos:
            wanted = {
                ref: not by_ref[ref].exclude_from_pos
                for ref in references
                if ref in by_ref
            }
            self.board.set_exclude_from_pos(wanted)
            for reference, state in wanted.items():
                self.store.set_pos(reference, int(state))

    # -- queries ------------------------------------------------------------

    def alike(self, reference: str) -> list:
        """Return every reference sharing this one's value and footprint.

        What "Auto-select alike" acts on: the same part in the same package is
        almost always the same purchase, and assigning them one at a time is the
        tedium this plugin exists to remove.
        """
        by_ref = {view.reference: view for view in self.board.footprints()}
        target = by_ref.get(reference)
        if target is None:
            return [reference]
        return [
            view.reference
            for view in self.board.footprints()
            if view.value == target.value and view.footprint == target.footprint
        ]
