"""The only module in this app that touches KiCad.

Everything the UI knows about the board comes through here, as plain frozen
dataclasses. No ``kipy`` object ever leaves this module, and that is not
tidiness — it is how the three traps found in the architecture spike are made
unreachable rather than merely documented.

Trap 1 — **KiCad poisons the environment.**
    It hands its own ``PYTHONHOME`` down to ``exec`` plugins, which kills a
    venv Python with ``ModuleNotFoundError: No module named 'encodings'`` before
    a single line of ours runs. That one is closed in ``run.sh``, not here, but
    :func:`environment_report` names it so a failure-to-connect can be
    diagnosed without guessing.

Trap 2 — **the API silently ignores writes to the wrong object.**
    ``board.update_items(field)`` returns success and changes nothing; the
    write has to target the **parent footprint**. Neither spelling raises. Two
    structural defences here:

    * ``update_items`` is called in exactly one place, :meth:`_Ipc._commit`,
      whose signature accepts footprint instances only and asserts the type.
    * every write goes through :meth:`_Board.apply`, which re-reads the board
      after the mutation and compares it against what the caller said it
      wanted. A mismatch drops the commit and raises
      :class:`WriteVerificationError`. A clean return value is never accepted
      as evidence.

Trap 3 — **custom fields are not on the footprint.**
    ``LCSC`` lives in ``footprint.definition.items`` (surfaced through
    ``.texts_and_fields``), so creating one goes through
    ``definition.add_item`` after cloning an existing ``Field`` and clearing
    its id. :meth:`_Ipc._ensure_lcsc_field` is the only place that knows this.

Trap 4 — **an open commit is invisible to a read.**
    Found only when the first write crossed a real socket, during Phase 3.
    ``update_items`` applies immediately when no commit is open, but inside
    ``begin_commit()`` … ``push_commit()`` the board keeps answering
    ``get_footprints()`` from the *committed* state. So the obvious ordering —
    mutate, verify, then commit only if the board agrees — can never succeed:
    the read always returns the old value and every write looks like trap 2.

    :meth:`_Board.apply` therefore snapshots first, **pushes**, and only then
    verifies; on a mismatch it puts the snapshot back in a second commit. The
    cost is that a failed write leaves two entries in KiCad's undo history
    rather than none. ``drop_commit`` *does* roll back correctly — it is just
    unreachable as a response to a verification that cannot run yet.

Two backends implement the same :class:`Board` protocol:

``_Ipc``
    the real thing, over ``kipy``.
``FixtureBoard``
    an in-memory board loaded from a JSON fixture, used by ``qt_probe.py``,
    the CI screenshot job and the tests. It reproduces trap 2 faithfully — see
    :class:`FixtureBoard` — so a bridge test can prove the read-back assertion
    actually fires rather than assuming it would.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import json
import logging
import os
import re
from typing import Callable, Iterable, Optional, Protocol, Sequence

log = logging.getLogger(__name__)

#: Field names that have carried an LCSC number over the years. Matches
#: ``schematicexport.LCSC_FIELD_NAMES`` and ``footprint_helpers.get_lcsc_value``
#: — the board and the schematic must agree on what counts as an LCSC field or
#: a round trip drifts.
LCSC_FIELD_PATTERN = re.compile(r"lcsc|jlc", re.IGNORECASE)

#: What a *value* has to look like to be read back as an LCSC number. A field
#: holding free text is not an assignment.
LCSC_VALUE_PATTERN = re.compile(r"^C\d+$")

#: The same number embedded in something else — a pasted URL, a line copied out
#: of a datasheet. Used only to *interpret input*; what counts as an assignment
#: on the board is still :data:`LCSC_VALUE_PATTERN`.
LCSC_SEARCH_PATTERN = re.compile(r"C\d+", re.IGNORECASE)

#: The field created when a footprint has none. Hidden, like the wx plugin's.
DEFAULT_LCSC_FIELD = "LCSC"

#: A reference has to look like one to be listed at all; mirrors
#: ``footprint_helpers.get_valid_footprints``.
VALID_REFERENCE = re.compile(r"[\w\d-]+")

#: KiCad works in nanometres.
NM_PER_MM = 1_000_000


class BridgeError(RuntimeError):
    """Base class for every failure this module reports."""


class NotConnected(BridgeError):
    """KiCad could not be reached, or has no board open."""


class WriteVerificationError(BridgeError):
    """A write returned success but the board did not change.

    This is trap 2 caught in the act. The commit has been dropped, so the board
    is as it was; the bug is in this module, not in the caller.
    """


@dataclass(frozen=True)
class FootprintView:
    """Everything the UI is allowed to know about one placed footprint.

    Frozen and plain: a snapshot, not a handle. Mutating one changes nothing on
    the board, which is the point — there is no way to accidentally "save" a
    footprint by poking at an object the board handed out.
    """

    reference: str
    value: str
    #: Library item name only (``R_0402_1005Metric``), matching what
    #: ``store.py`` persists — not ``library:item``.
    footprint: str
    library: str
    #: ``""`` when the footprint carries no LCSC number.
    lcsc: str
    #: Name of the field the number lives in, or ``""`` when there is none yet.
    lcsc_field: str
    lcsc_visible: bool
    exclude_from_bom: bool
    exclude_from_pos: bool
    dnp: bool
    #: ``"top"`` or ``"bottom"`` — what the CPL calls a layer.
    side: str
    position_mm: tuple[float, float]
    orientation_deg: float
    pad_count: int
    has_tht: bool

    @property
    def assigned(self) -> bool:
        """Report whether this footprint carries an LCSC number."""
        return bool(self.lcsc)


@dataclass(frozen=True)
class BoardView:
    """Identity of the open board, and where its project lives."""

    name: str
    path: str
    project_path: str
    project_name: str
    kicad_version: str

    @property
    def schematic_name(self) -> str:
        """Best guess at the root schematic's filename, as the wx plugin does."""
        return f"{self.name.split('.')[0]}.kicad_sch"


@dataclass(frozen=True)
class Edit:
    """One requested change, paired with the state it must produce.

    ``expect`` is checked against a **freshly read** :class:`FootprintView`
    after the mutation lands. It exists so that no write helper can forget the
    read-back: :meth:`_Board.apply` refuses an :class:`Edit` without one.
    """

    reference: str
    #: Called with the backend's live footprint object. Backend-specific, and
    #: therefore only ever constructed inside a backend.
    mutate: Callable[[object], None]
    #: ``(attribute, value)`` pairs the re-read snapshot must match.
    expect: dict
    #: Human-readable, used in the undo entry and in error messages.
    describe: str = ""

    def __post_init__(self) -> None:
        if not self.expect:
            raise ValueError(
                f"Edit for {self.reference} has nothing to verify. Every write "
                "must state what the board should look like afterwards — see "
                "trap 2 in this module's docstring."
            )


class Board(Protocol):
    """What the UI may do to a board. Deliberately small."""

    def info(self) -> BoardView: ...

    def footprints(self) -> Sequence[FootprintView]: ...

    def set_lcsc(self, assignments: dict) -> Sequence[FootprintView]: ...

    def set_exclude_from_bom(self, states: dict) -> Sequence[FootprintView]: ...

    def set_exclude_from_pos(self, states: dict) -> Sequence[FootprintView]: ...


# ---------------------------------------------------------------------------
# Shared write plumbing
# ---------------------------------------------------------------------------


class _Board:
    """Behaviour common to both backends — chiefly, verifying every write."""

    def __init__(self) -> None:
        self._cache: Optional[list[FootprintView]] = None

    # -- reading ------------------------------------------------------------

    def info(self) -> BoardView:  # pragma: no cover - backends override
        raise NotImplementedError

    def footprints(self, refresh: bool = False) -> Sequence[FootprintView]:
        """Return a snapshot of every valid footprint on the board."""
        if refresh or self._cache is None:
            self._cache = list(self._read_footprints())
        return tuple(self._cache)

    def footprint(self, reference: str) -> FootprintView:
        """Return one footprint's snapshot, by reference."""
        for view in self.footprints():
            if view.reference == reference:
                return view
        raise BridgeError(f"No footprint {reference!r} on this board")

    def _read_footprints(self) -> Iterable[FootprintView]:  # pragma: no cover
        raise NotImplementedError

    # -- writing ------------------------------------------------------------

    def apply(self, edits: Sequence[Edit], message: str) -> Sequence[FootprintView]:
        """Apply ``edits`` as one undo step, then prove they landed.

        The order here is the whole point, and it is not the order you would
        write first (see trap 4 in this module's docstring):

        1. **snapshot** the attributes every edit touches, because they are the
           only way back if the write turns out not to have landed,
        2. open a commit,
        3. mutate the footprints and push each mutation at its **parent
           footprint**,
        4. **push the commit** — a read cannot see an open commit, so this has
           to happen before there is anything to verify,
        5. re-read the board and compare against every ``Edit.expect``,
        6. on a mismatch, put the snapshot back in a second commit and raise.

        A backend's ``update_items`` return value is never consulted. It says
        "success" for a write that changed nothing.
        """
        if not edits:
            return self.footprints()

        before = {
            view.reference: view
            for view in self._read_footprints()
            if view.reference in {edit.reference for edit in edits}
        }
        commit = self._begin()
        try:
            for edit in edits:
                target = self._live_footprint(edit.reference)
                edit.mutate(target)
                self._commit(target)
        except Exception:
            # Nothing has been pushed yet, so dropping really does leave the
            # board as it was. This is the one path where that still holds.
            self._drop(commit)
            self._cache = None
            raise

        self._push(commit, message)
        self._cache = None
        after = {view.reference: view for view in self._read_footprints()}
        mismatches = _verify(edits, after)
        if mismatches:
            restored = self._restore(before, edits)
            raise WriteVerificationError(
                "KiCad reported success but the board did not change:\n  "
                + "\n  ".join(mismatches)
                + (
                    "\nThe previous values have been put back; the board is as "
                    "it was, at the cost of two entries in the undo history."
                    if restored
                    else "\nPutting the previous values back ALSO failed. The "
                    "board is in an unknown state — undo in KiCad before "
                    "changing anything else."
                )
            )
        return self.footprints(refresh=True)

    def _restore(self, before: dict, edits: Sequence[Edit]) -> bool:
        """Put back the attributes ``edits`` touched. Returns whether it worked.

        Reached only when a pushed write failed its read-back. It cannot use
        :meth:`apply` — that would recurse on the same failure — so it verifies
        inline and reports rather than raising.

        One asymmetry worth knowing: if the failed write *created* an LCSC field
        where the footprint had none, restoring sets that field back to empty
        rather than removing it. The board reads as unassigned either way, which
        is what the snapshot promised; a stray empty field is not worth a second
        way for this path to fail.
        """
        commit = self._begin()
        try:
            for edit in edits:
                previous = before.get(edit.reference)
                if previous is None:
                    continue
                target = self._live_footprint(edit.reference)
                for attribute in edit.expect:
                    self._mutator_for(
                        edit.reference, attribute, getattr(previous, attribute)
                    )(target)
                self._commit(target)
        except Exception:
            self._drop(commit)
            self._cache = None
            log.exception("Could not put the previous values back")
            return False

        self._push(commit, "Undo the LCSC Suite write KiCad did not accept")
        self._cache = None
        after = {view.reference: view for view in self._read_footprints()}
        return all(
            getattr(after.get(edit.reference), attribute, None)
            == getattr(previous, attribute)
            for edit in edits
            for attribute in edit.expect
            if (previous := before.get(edit.reference)) is not None
        )

    def _mutator_for(self, reference: str, attribute: str, value):
        """Return the mutator that sets one snapshot attribute back."""
        if attribute == "lcsc":
            return self._lcsc_mutator(reference, value)
        return self._attribute_mutator(attribute, bool(value))

    # -- high-level write helpers ------------------------------------------
    #
    # Every one of these builds Edits with an `expect`, so the read-back in
    # apply() is not something a caller can skip.

    def set_lcsc(self, assignments: dict) -> Sequence[FootprintView]:
        """Assign (or, with an empty string, clear) LCSC numbers.

        Creating the field where none exists is trap 3 and is handled by the
        backend; this only states what the board must look like afterwards.
        """
        edits = []
        for reference, number in assignments.items():
            number = (number or "").strip()
            if number and not LCSC_VALUE_PATTERN.match(number):
                raise ValueError(
                    f"{number!r} is not an LCSC number (expected C followed by digits)"
                )
            edits.append(
                Edit(
                    reference=reference,
                    mutate=self._lcsc_mutator(reference, number),
                    expect={"lcsc": number},
                    describe=f"{reference} -> {number or '(cleared)'}",
                )
            )
        return self.apply(edits, _undo_message("LCSC", edits))

    def set_exclude_from_bom(self, states: dict) -> Sequence[FootprintView]:
        """Set the exclude-from-BOM attribute on the given references."""
        edits = [
            Edit(
                reference=reference,
                mutate=self._attribute_mutator("exclude_from_bom", bool(state)),
                expect={"exclude_from_bom": bool(state)},
                describe=f"{reference} BOM {'excluded' if state else 'included'}",
            )
            for reference, state in states.items()
        ]
        return self.apply(edits, _undo_message("exclude from BOM", edits))

    def set_exclude_from_pos(self, states: dict) -> Sequence[FootprintView]:
        """Set the exclude-from-position-files attribute on the given references."""
        edits = [
            Edit(
                reference=reference,
                mutate=self._attribute_mutator("exclude_from_pos", bool(state)),
                expect={"exclude_from_pos": bool(state)},
                describe=f"{reference} POS {'excluded' if state else 'included'}",
            )
            for reference, state in states.items()
        ]
        return self.apply(edits, _undo_message("exclude from POS", edits))

    def run_kicad_action(self, action: str):
        """Ask KiCad to run one of its own tool actions, by name.

        **For diagnostics only, and nothing in the app calls it.** ``kipy`` flags
        ``run_action`` as unstable — KiCad does not promise the action names — and
        an action operates on whatever the editor's state happens to be, which is
        not something a write helper can verify. It exists so
        ``scripts/live_ipc_check.py`` can ask the one question no documentation
        answers: whether KiCad's own undo covers a write made over IPC.

        The app's Undo button does *not* go through here. It reverses by writing
        the previous values back, because KiCad's history cannot reach the project
        database — see :mod:`lcsc_suite.undo`.
        """
        raise BridgeError(f"{type(self).__name__} cannot run KiCad actions")

    # -- backend hooks ------------------------------------------------------

    def _begin(self):  # pragma: no cover - backends override
        raise NotImplementedError

    def _push(self, commit, message: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _drop(self, commit) -> None:  # pragma: no cover
        raise NotImplementedError

    def _live_footprint(self, reference: str):  # pragma: no cover
        raise NotImplementedError

    def _commit(self, footprint) -> None:  # pragma: no cover
        raise NotImplementedError

    def _lcsc_mutator(self, reference: str, number: str):  # pragma: no cover
        raise NotImplementedError

    def _attribute_mutator(self, name: str, state: bool):  # pragma: no cover
        raise NotImplementedError


def _verify(edits: Sequence[Edit], after: dict) -> list[str]:
    """Compare the re-read board against what every edit asked for."""
    problems = []
    for edit in edits:
        view = after.get(edit.reference)
        if view is None:
            problems.append(f"{edit.reference}: vanished from the board")
            continue
        for attribute, wanted in edit.expect.items():
            got = getattr(view, attribute)
            if got != wanted:
                problems.append(
                    f"{edit.reference}: {attribute} is {got!r}, expected {wanted!r}"
                )
    return problems


def _undo_message(what: str, edits: Sequence[Edit]) -> str:
    """Name a commit the way KiCad's undo history reads best."""
    if len(edits) == 1:
        return f"Set {what}: {edits[0].describe}"
    return f"Set {what} on {len(edits)} footprints"


def _side_from_layer(layer: int) -> str:
    """Map a copper layer id to the side name the CPL uses."""
    # BL_F_Cu is 3 and BL_B_Cu is 34 in KiCad 10's board_types proto; anything
    # else means the footprint is not on an outer copper layer, which for a
    # placed component should not happen. Report top rather than inventing a
    # third value the CPL writer has never seen.
    return "bottom" if layer == _B_CU else "top"


_F_CU = 3
_B_CU = 34


# ---------------------------------------------------------------------------
# The real backend
# ---------------------------------------------------------------------------


class _Ipc(_Board):
    """Board access over KiCad 10's IPC API."""

    def __init__(self, kicad, board) -> None:
        super().__init__()
        self._kicad = kicad
        self._board = board
        self._live: dict = {}

    # -- reading ------------------------------------------------------------

    def info(self) -> BoardView:
        document = self._board.document
        project = document.project
        version = self._kicad.get_version()
        return BoardView(
            name=document.board_filename,
            path=os.path.join(project.path, document.board_filename),
            project_path=project.path,
            project_name=project.name,
            # `full_version` is the string `schematicexport` expects — the same
            # shape pcbnew.GetBuildVersion() returns in the wx plugin.
            kicad_version=getattr(version, "full_version", str(version)),
        )

    def run_kicad_action(self, action: str):
        """Run a KiCad tool action by name. Diagnostics only — see the base."""
        result = self._kicad.run_action(action)
        # Anything we read afterwards has to come off the board again: the action
        # changed the editor's state, not ours.
        self._cache = None
        self._live = {}
        return getattr(result, "status", result)

    def _read_footprints(self) -> Iterable[FootprintView]:
        self._live = {}
        for footprint in self._board.get_footprints():
            reference = footprint.reference_field.text.value
            if not VALID_REFERENCE.match(reference or ""):
                continue
            self._live[reference] = footprint
            yield _view_of(footprint, reference)

    def _live_footprint(self, reference: str):
        if reference not in self._live:
            # Populate the handle map; footprints() caches views, not handles.
            list(self._read_footprints())
        try:
            return self._live[reference]
        except KeyError:
            raise BridgeError(f"No footprint {reference!r} on this board") from None

    # -- commits ------------------------------------------------------------

    def _begin(self):
        return self._board.begin_commit()

    def _push(self, commit, message: str) -> None:
        self._board.push_commit(commit, message)

    def _drop(self, commit) -> None:
        try:
            self._board.drop_commit(commit)
        except Exception:  # noqa: BLE001 - a failed rollback must not mask the cause
            log.exception("Dropping the commit failed")

    def _commit(self, footprint) -> None:
        """Push one footprint's changes at the board.

        **This is the only ``update_items`` call in the application.** It takes
        footprint instances and nothing else: handing this a ``Field`` is trap
        2, and the API answers such a call with a cheerful success and no
        change at all.
        """
        from kipy.board_types import FootprintInstance

        if not isinstance(footprint, FootprintInstance):
            raise TypeError(
                f"update_items must target the parent FootprintInstance, not "
                f"{type(footprint).__name__}. Writing to a child object returns "
                "success and changes nothing (trap 2)."
            )
        self._board.update_items(footprint)

    # -- mutators -----------------------------------------------------------

    def _lcsc_mutator(self, reference: str, number: str):
        def mutate(footprint) -> None:
            field = self._ensure_lcsc_field(footprint)
            field.text.value = number

        return mutate

    def _attribute_mutator(self, name: str, state: bool):
        proto_name = {
            "exclude_from_bom": "exclude_from_bill_of_materials",
            "exclude_from_pos": "exclude_from_position_files",
            "dnp": "do_not_populate",
        }[name]

        def mutate(footprint) -> None:
            setattr(footprint.attributes, proto_name, state)

        return mutate

    def _ensure_lcsc_field(self, footprint):
        """Return the footprint's LCSC field, creating one if it has none.

        Trap 3: a custom field is not an attribute of the footprint, it is an
        item inside its *definition*. There is also no useful constructor —
        a ``Field`` built from scratch has no text attributes, layer or size,
        and lands invisible and unstyled. So clone one the footprint already
        has, clear the ids KiCad assigned it, and rename it.
        """
        for item in footprint.texts_and_fields:
            name = getattr(item, "name", None)
            if name and LCSC_FIELD_PATTERN.search(name):
                return item

        donor = self._field_donor(footprint)
        if donor is None:
            raise BridgeError(
                f"{footprint.reference_field.text.value} has no field to clone, "
                "so an LCSC field cannot be created for it."
            )
        new = copy.deepcopy(donor)
        # Two ids: the Field's own (which doubles as the *mandatory-field*
        # number — leave it set and KiCad treats the clone as that field) and
        # the underlying text item's KIID. Both have to go or the board ends up
        # with a duplicate.
        new.proto.ClearField("id")
        new.proto.text.ClearField("id")
        new.name = DEFAULT_LCSC_FIELD
        new.text.value = ""
        new.visible = False
        footprint.definition.add_item(new)
        return new

    @staticmethod
    def _field_donor(footprint):
        """Pick a field worth cloning: a user field first, Datasheet last."""
        candidates = [
            item
            for item in footprint.texts_and_fields
            if getattr(item, "name", None) and hasattr(item, "visible")
        ]
        for item in candidates:
            if item.name not in ("Reference", "Value", "Datasheet", "Description"):
                return item
        for preferred in ("Datasheet", "Description", "Value"):
            for item in candidates:
                if item.name == preferred:
                    return item
        return candidates[0] if candidates else None


def _view_of(footprint, reference: str) -> FootprintView:
    """Build the read-only snapshot for one kipy footprint."""
    lcsc, lcsc_field, lcsc_visible = _lcsc_of(footprint)
    attributes = footprint.attributes
    position = footprint.position
    pads = list(footprint.definition.pads)
    return FootprintView(
        reference=reference,
        value=footprint.value_field.text.value,
        footprint=footprint.definition.id.name,
        library=footprint.definition.id.library,
        lcsc=lcsc,
        lcsc_field=lcsc_field,
        lcsc_visible=lcsc_visible,
        exclude_from_bom=bool(attributes.exclude_from_bill_of_materials),
        exclude_from_pos=bool(attributes.exclude_from_position_files),
        dnp=bool(attributes.do_not_populate),
        side=_side_from_layer(footprint.layer),
        position_mm=(position.x / NM_PER_MM, position.y / NM_PER_MM),
        orientation_deg=float(footprint.orientation.degrees),
        pad_count=sum(1 for pad in pads if _counts_as_joint(pad)),
        has_tht=any(_counts_as_joint(pad) and _is_tht(pad) for pad in pads),
    )


def _lcsc_of(footprint) -> tuple[str, str, bool]:
    """Return ``(number, field name, visible)`` for a footprint's LCSC field.

    A field whose *name* looks like an LCSC field but whose value is not a
    number still counts as the field to write into — that is what lets an
    existing ``JLC_PN`` field be reused instead of a second one being created
    beside it. Only the *value* is filtered, so free text reads as unassigned.
    """
    for item in footprint.texts_and_fields:
        name = getattr(item, "name", None)
        if not name or not LCSC_FIELD_PATTERN.search(name):
            continue
        value = item.text.value
        matched = value if LCSC_VALUE_PATTERN.match(value or "") else ""
        return matched, name, bool(getattr(item, "visible", False))
    return "", "", False


def _counts_as_joint(pad) -> bool:
    """Report whether a pad is an electrical solder joint.

    Mirrors ``footprint_metadata.count_pad``: mechanical (non-plated) holes are
    not joints and must not inflate the estimator's pad count.
    """
    return pad.pad_type != _PT_NPTH


def _is_tht(pad) -> bool:
    """Report whether a pad is through-hole, for the estimator's TH flag."""
    if pad.pad_type == _PT_PTH:
        return True
    drill = getattr(pad.padstack, "drill", None)
    diameter = getattr(drill, "diameter", None)
    if diameter is None:
        return False
    return bool(getattr(diameter, "x", 0) or getattr(diameter, "y", 0))


_PT_PTH = 1
_PT_NPTH = 4


# ---------------------------------------------------------------------------
# The fixture backend
# ---------------------------------------------------------------------------


@dataclass
class _FixtureField:
    """A footprint field in the fixture board."""

    name: str
    value: str
    visible: bool = True


@dataclass
class _FixtureFootprint:
    """A placed footprint in the fixture board.

    Mutating one of these does **not** change the board: :class:`FixtureBoard`
    keeps the committed state separately and only copies a mutated footprint
    across when ``update_items`` is called with that footprint. That is how the
    fixture reproduces trap 2 — see :meth:`FixtureBoard._commit`.
    """

    reference: str
    value: str
    footprint: str
    library: str = "LCSC"
    fields: list = field(default_factory=list)
    exclude_from_bom: bool = False
    exclude_from_pos: bool = False
    dnp: bool = False
    side: str = "top"
    position_mm: tuple = (0.0, 0.0)
    orientation_deg: float = 0.0
    pad_count: int = 2
    has_tht: bool = False

    def lcsc_field(self) -> Optional[_FixtureField]:
        """Return the field an LCSC number lives in, if there is one."""
        for item in self.fields:
            if LCSC_FIELD_PATTERN.search(item.name):
                return item
        return None


class FixtureBoard(_Board):
    """A board loaded from JSON, for screenshots, CI and tests.

    It exists for two reasons.

    **Screenshots.** ``qt_probe.py`` has to render every screen with realistic
    content and no KiCad running, on a machine with no display. A stub that
    returns three rows would not show the column widths, the row colouring or
    the unassigned-part warnings that the parity review is looking at.

    **Proving the read-back works.** The verification in :meth:`_Board.apply`
    is only worth having if something demonstrates it firing. Constructing this
    with ``honour_footprint_writes=False`` makes it behave exactly like the
    real API's trap 2 — ``update_items`` succeeds and the board does not
    change — so a test can assert that :class:`WriteVerificationError` is
    raised instead of the bug passing silently.
    """

    def __init__(
        self,
        footprints: Sequence[_FixtureFootprint],
        info: BoardView,
        honour_footprint_writes: bool = True,
    ) -> None:
        super().__init__()
        self._committed = {fp.reference: fp for fp in footprints}
        self._order = [fp.reference for fp in footprints]
        self._info = info
        self._honour = honour_footprint_writes
        self._draft: dict = {}
        #: Staged by ``_commit``, made visible by ``_push``. See trap 4.
        self._pending: dict = {}
        self.commits: list = []

    # -- construction -------------------------------------------------------

    @classmethod
    def from_json(cls, path: str, **kwargs) -> FixtureBoard:
        """Load a fixture board from ``path``."""
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return cls.from_dict(payload, **kwargs)

    @classmethod
    def from_dict(cls, payload: dict, **kwargs) -> FixtureBoard:
        """Build a fixture board from a parsed fixture payload."""
        board = payload.get("board", {})
        info = BoardView(
            name=board.get("name", "fixture.kicad_pcb"),
            path=board.get("path", "/fixture/fixture.kicad_pcb"),
            project_path=board.get("project_path", "/fixture"),
            project_name=board.get("project_name", "fixture"),
            kicad_version=board.get("kicad_version", "10.0.3"),
        )
        footprints = []
        for row in payload.get("footprints", []):
            fields = [
                _FixtureField(
                    name=item.get("name", ""),
                    value=item.get("value", ""),
                    visible=bool(item.get("visible", True)),
                )
                for item in row.get("fields", [])
            ]
            if row.get("lcsc") and not any(
                LCSC_FIELD_PATTERN.search(f.name) for f in fields
            ):
                fields.append(
                    _FixtureField(DEFAULT_LCSC_FIELD, row["lcsc"], visible=False)
                )
            footprints.append(
                _FixtureFootprint(
                    reference=row["reference"],
                    value=row.get("value", ""),
                    footprint=row.get("footprint", ""),
                    library=row.get("library", "LCSC"),
                    fields=fields,
                    exclude_from_bom=bool(row.get("exclude_from_bom", False)),
                    exclude_from_pos=bool(row.get("exclude_from_pos", False)),
                    dnp=bool(row.get("dnp", False)),
                    side=row.get("side", "top"),
                    position_mm=tuple(row.get("position_mm", (0.0, 0.0))),
                    orientation_deg=float(row.get("orientation_deg", 0.0)),
                    pad_count=int(row.get("pad_count", 2)),
                    has_tht=bool(row.get("has_tht", False)),
                )
            )
        return cls(footprints, info, **kwargs)

    # -- reading ------------------------------------------------------------

    def info(self) -> BoardView:
        return self._info

    def relocate(self, project_path: str) -> None:
        """Point the fixture's project at a real directory.

        The committed fixture names a path that does not exist, deliberately —
        it must not depend on anything outside the checkout. But ``store.py``
        creates ``<project>/jlcpcb/project.db`` for real, so a probe or a test
        has to hand it somewhere writable first.
        """
        self._info = replace(
            self._info,
            project_path=project_path,
            path=os.path.join(project_path, self._info.name),
        )

    def _read_footprints(self) -> Iterable[FootprintView]:
        for reference in self._order:
            row = self._committed[reference]
            lcsc_field = row.lcsc_field()
            value = lcsc_field.value if lcsc_field else ""
            yield FootprintView(
                reference=row.reference,
                value=row.value,
                footprint=row.footprint,
                library=row.library,
                lcsc=value if LCSC_VALUE_PATTERN.match(value or "") else "",
                lcsc_field=lcsc_field.name if lcsc_field else "",
                lcsc_visible=bool(lcsc_field.visible) if lcsc_field else False,
                exclude_from_bom=row.exclude_from_bom,
                exclude_from_pos=row.exclude_from_pos,
                dnp=row.dnp,
                side=row.side,
                position_mm=tuple(row.position_mm),
                orientation_deg=row.orientation_deg,
                pad_count=row.pad_count,
                has_tht=row.has_tht,
            )

    def _live_footprint(self, reference: str):
        if reference not in self._committed:
            raise BridgeError(f"No footprint {reference!r} on this board")
        if reference not in self._draft:
            self._draft[reference] = copy.deepcopy(self._committed[reference])
        return self._draft[reference]

    # -- commits ------------------------------------------------------------

    def _begin(self):
        self._draft = {}
        self._pending = {}
        return object()

    def _push(self, commit, message: str) -> None:
        """Make the commit's changes visible — and not a moment sooner.

        Trap 4: the real board answers reads from its committed state while a
        commit is open, so the fixture does too. A fixture that is more
        permissive than the API is a fixture that lets a bug through, which is
        exactly what happened before this was modelled.
        """
        self.commits.append(message)
        self._committed.update(self._pending)
        self._pending = {}
        self._draft = {}

    def _drop(self, commit) -> None:
        self._pending = {}
        self._draft = {}

    def _commit(self, footprint) -> None:
        """Stage a mutated footprint for the open commit — or not.

        With ``honour_footprint_writes=False`` this does nothing at all while
        still returning normally, which is precisely what the real API does
        when a write targets a child object instead of its parent footprint.
        """
        if not isinstance(footprint, _FixtureFootprint):
            raise TypeError(
                "update_items must target the parent footprint, not "
                f"{type(footprint).__name__} (trap 2)."
            )
        if not self._honour:
            return
        self._pending[footprint.reference] = copy.deepcopy(footprint)

    # -- mutators -----------------------------------------------------------

    def _lcsc_mutator(self, reference: str, number: str):
        def mutate(footprint) -> None:
            existing = footprint.lcsc_field()
            if existing is None:
                footprint.fields.append(
                    _FixtureField(DEFAULT_LCSC_FIELD, number, visible=False)
                )
            else:
                existing.value = number

        return mutate

    def _attribute_mutator(self, name: str, state: bool):
        def mutate(footprint) -> None:
            setattr(footprint, name, state)

        return mutate


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------


def sanitize_lcsc(text: str) -> str:
    """Pull an LCSC number out of whatever the user handed us, or return ``""``.

    Mirrors ``mainwindow.sanitize_lcsc``: a clipboard rarely holds a bare
    ``C1524``. It holds a product URL, a spreadsheet cell with a trailing tab, or
    a line copied out of a datasheet — and all of those carry the number the user
    meant. The first match wins, and it is upper-cased because that is the
    spelling every store in this project persists.

    Anything with no number in it at all returns ``""``, which every caller
    treats as "do nothing" rather than as "clear the assignment". Clearing is
    :meth:`_Board.set_lcsc` with an empty string, and a failed paste must never
    be mistaken for one.
    """
    match = LCSC_SEARCH_PATTERN.search(text or "")
    return match.group(0).upper() if match else ""


def environment_report() -> dict:
    """Describe what this process inherited, for diagnosing a failed connect.

    ``PYTHONHOME`` is listed first because it is trap 1: KiCad hands its own
    down to ``exec`` plugins, and a venv Python dies on it before importing
    anything. If a user reports "the button does nothing", this is the first
    thing to look at.
    """
    return {
        "PYTHONHOME": os.environ.get("PYTHONHOME", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "KICAD_API_SOCKET": os.environ.get("KICAD_API_SOCKET", ""),
        "KICAD_API_TOKEN": "set" if os.environ.get("KICAD_API_TOKEN") else "",
    }


def connect(timeout_ms: int = 5000) -> Board:
    """Connect to a running KiCad and return its open board.

    Raises :class:`NotConnected` with something actionable in it — the API
    server is off by default in KiCad, and "connection refused" on its own
    sends people looking in the wrong place.
    """
    try:
        from kipy import KiCad
    except ImportError as exc:  # pragma: no cover - install.sh installs this
        raise NotConnected(
            "kicad-python is not installed in this environment. Run install.sh."
        ) from exc

    # kipy reads both of these from the environment on its own; passing them
    # explicitly keeps the failure mode visible when KiCad did not set them.
    socket = os.environ.get("KICAD_API_SOCKET") or None
    token = os.environ.get("KICAD_API_TOKEN") or None
    try:
        kicad = KiCad(
            socket_path=socket,
            client_name="lcsc-suite",
            kicad_token=token,
            timeout_ms=timeout_ms,
        )
        board = kicad.get_board()
    except Exception as exc:  # noqa: BLE001 - kipy raises several unrelated types
        raise NotConnected(
            f"Could not reach KiCad's IPC API ({exc}).\n"
            "Check that KiCad is running with a PCB open, and that the API "
            "server is enabled: Preferences -> Plugins -> Enable KiCad API."
        ) from exc
    return _Ipc(kicad, board)


def open_fixture(path: str, **kwargs) -> FixtureBoard:
    """Open a fixture board — the offline path used by the probe and CI."""
    return FixtureBoard.from_json(path, **kwargs)


__all__ = [
    "Board",
    "BoardView",
    "BridgeError",
    "Edit",
    "FixtureBoard",
    "FootprintView",
    "NotConnected",
    "WriteVerificationError",
    "connect",
    "environment_report",
    "open_fixture",
    "replace",
    "sanitize_lcsc",
]
