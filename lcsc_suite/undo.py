"""One session's worth of reversible writes, and why this exists at all.

KiCad has its own undo history, and :meth:`kicad_bridge._Board.apply` is careful
to put exactly one entry in it per action. That is necessary and it is not
sufficient, for three reasons found the first time someone tried to undo a
removal:

1. **The keystroke goes to the wrong window.** Right after clicking ``Remove
   LCSC number`` the focused window is *ours*, and Cmd+Z in our window is not
   KiCad's Cmd+Z. Nothing happens, and nothing explains why.
2. **KiCad's undo cannot reach the project database.** A removal writes the
   board *and* clears the number and stock figure in ``project.db``. Undoing the
   board half in pcbnew leaves the database still saying the part is
   unassigned — and the part table reads the database, so a successful undo
   looks like a failed one.
3. **Nothing re-reads the board after an external change.** ``PartList``
   reconciles on our own writes. A change made in pcbnew — an undo included —
   is invisible here until something asks the board again.

So this module is the app's own undo: a stack of ``(description, revert)``
pairs, where ``revert`` puts *both* halves back by going through the same
verified write helpers the original action used. It is not a duplicate of
KiCad's stack; it is the only one that covers everything an action changed.

Two consequences worth stating:

* **A reversal is a new write, not a rollback.** It goes through
  :meth:`kicad_bridge._Board.apply` like anything else, so it is verified by
  re-reading and it costs its own entry in KiCad's undo history. Reversing an
  assignment therefore leaves *two* KiCad entries, not zero. That is the honest
  price of being able to reverse the database too.
* **A snapshot can go stale.** If the board is changed in pcbnew after an action
  was recorded, reverting re-applies the *recorded* previous value rather than
  the one KiCad now has. It converges on the same result when the outside change
  was the equivalent undo, which is the common case, and it never destroys more
  than the action it names — but it is a restore, not a merge.

Deliberately toolkit-free: the controller decides *when* to record and the
window only asks what the top entry is called, so nothing here needs Qt.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

#: How many actions back the button can walk. Deep enough that a session's work
#: is recoverable, bounded because each entry pins a snapshot of what it touched
#: and an unbounded stack in a window people leave open for days is a leak.
DEPTH = 50


@dataclass(frozen=True)
class Reversal:
    """One recorded action and the callable that puts it back.

    ``description`` is user-facing — it goes in the button's tooltip and in the
    log line — so it names the action in the terms the user performed it in
    (``assign C25741 to R1-R3``), not in the terms the write happened in.
    """

    description: str
    revert: Callable[[], None]


class UndoStack:
    """Last-in, first-out stack of :class:`Reversal`.

    ``push`` and ``pop`` only; there is no redo. Redo would need the *forward*
    state as well as the backward one, and an action reversed by mistake is one
    the user can simply perform again — which is not true of the removal that
    prompted any of this.
    """

    def __init__(self, depth: int = DEPTH) -> None:
        self._entries: deque = deque(maxlen=depth)

    def push(self, description: str, revert: Callable[[], None]) -> None:
        """Record an action that has already succeeded."""
        self._entries.append(Reversal(description, revert))

    def pop(self) -> Optional[Reversal]:
        """Take the most recent entry off, or ``None`` if there is none."""
        return self._entries.pop() if self._entries else None

    def peek(self) -> Optional[Reversal]:
        """Look at the most recent entry without removing it."""
        return self._entries[-1] if self._entries else None

    @property
    def description(self) -> Optional[str]:
        """What the next reversal would undo, or ``None`` if nothing would."""
        top = self.peek()
        return None if top is None else top.description

    def clear(self) -> None:
        """Forget everything. Nothing calls this yet; a board reload would."""
        self._entries.clear()

    def __len__(self) -> int:
        """Report how many actions back the stack currently reaches."""
        return len(self._entries)

    def __bool__(self) -> bool:
        """Report whether there is anything to reverse."""
        return bool(self._entries)


__all__ = ["DEPTH", "Reversal", "UndoStack"]
