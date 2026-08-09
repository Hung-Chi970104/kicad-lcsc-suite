"""Whether KiCad really holds a document, or only left a lock file behind.

KiCad marks a document it has open by dropping a sibling file named
``~<name>.<ext>.lck`` (``common/lockfile.cpp``). That file is the whole signal:
it records the owner's username and hostname and **nothing else** — no process
id, no port, nothing that says the writer is still alive — and it outlives the
session that wrote it whenever KiCad is force-quit or crashes. A real one::

    {"hostname":"Mac","username":"blabla"}

Reading "the file exists" as "the Schematic Editor has it open" therefore
refuses to write to schematics that nobody has open, and the advice that comes
with the refusal — close eeschema and try again — cannot be followed, because
it is already closed. That is the bug this module exists to fix.

What tells a live lock from a leftover is **when it was written**. KiCad
recreates its IPC socket every time it starts, so the socket's timestamp dates
the running session, and a lock written before that belongs to a session that
is gone. Measured on KiCad 10.0.3: ``/tmp/kicad/api.sock``'s mtime and birth
time both match the process start to the second, and a schematic lock left by
the previous session was eighteen minutes older.

**Asking KiCad directly does not work**, which is worth recording so nobody
spends the afternoon finding out again. The IPC API has exactly the right call
— ``GetOpenDocuments(DOCTYPE_SCHEMATIC)`` — and KiCad 10.0.3 answers it with
``no handler available for request of type kiapi.common.commands.
GetOpenDocuments``. The same call with ``DOCTYPE_PCB`` returns the open board,
so the handler is registered per editor and eeschema does not register one.
There is no version of "ask the application" available here.

The failure direction is deliberate. When the session cannot be dated — no
socket on disk, a platform that does not leave one — every lock reads as
**held**, which is exactly what this code did before it could date anything.
Being wrong that way costs a dialog the user can dismiss; being wrong the other
way overwrites edits that only eeschema's copy of the document holds.
"""

from __future__ import annotations

from dataclasses import dataclass
import getpass
import json
import logging
import os
import platform
import socket as socket_module
import tempfile
from typing import Optional

log = logging.getLogger(__name__)

#: KiCad locks an open document with a sibling file named ``~<name>.<ext>.lck``
#: (see ``lockfile.cpp``); eeschema does this for the root sheet of a project.
LOCK_PREFIX = "~"
LOCK_SUFFIX = ".lck"

#: No lock file next to the document.
NONE = "none"
#: A lock this KiCad session could have written. Treat the document as open.
HELD = "held"
#: A lock written before the running KiCad started, so nothing holds it now.
STALE = "stale"
#: A lock naming another user or another machine. Somebody else's, and this
#: process cannot date their session — always treated as open.
FOREIGN = "foreign"


def lock_file_for(path: str) -> str:
    """Return the path of the lock file KiCad uses for a document."""
    directory, name = os.path.split(path)
    return os.path.join(directory, f"{LOCK_PREFIX}{name}{LOCK_SUFFIX}")


def read_lock(path: str) -> dict:
    """Return the owner recorded in a document's lock file.

    ``{}`` for a lock that is missing, empty or not JSON — every one of which
    means "no owner named", not "owned by nobody". Older KiCad wrote an empty
    file here, so an unparseable lock is treated as this user's rather than as
    a stranger's.
    """
    try:
        with open(lock_file_for(path), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _short_host(name: str) -> str:
    """Reduce a hostname to the part KiCad and Python agree on.

    ``wxGetHostName()`` writes the short name (``Mac``) where
    ``socket.gethostname()`` may return the fully qualified one
    (``Mac.local``). Comparing the leading label case-insensitively stops a
    machine being mistaken for a stranger on the strength of a domain suffix.
    """
    return (name or "").split(".")[0].casefold()


def _socket_candidates():
    """Yield the paths KiCad's IPC socket could be at, best guess first.

    ``KICAD_API_SOCKET`` is what KiCad hands its own plugins and it names the
    session that launched us, so it wins outright: a second KiCad running on
    the same machine has its own socket and dating this session against that
    one would be worse than not dating it at all. The rest mirror
    ``kipy.kicad._default_socket_path`` — deliberately re-derived rather than
    imported, because a private function of a pinned dependency is not
    something to build a safety check on.
    """
    configured = (os.environ.get("KICAD_API_SOCKET") or "").strip()
    if configured:
        yield configured.removeprefix("ipc://")
        return

    if platform.system() == "Windows":
        yield os.path.join(tempfile.gettempdir(), "kicad", "api.sock")
        return

    home = os.environ.get("HOME") or ""
    if home:
        yield os.path.join(home, ".var/app/org.kicad.KiCad/cache/tmp/kicad/api.sock")
    # kipy hardcodes /tmp on every non-Windows platform, so this is the real
    # answer on macOS, where TMPDIR points at a per-user directory instead.
    # S108: KiCad chose this path, and nothing here writes to it or trusts its
    # contents — only its modification time is read.
    yield "/tmp/kicad/api.sock"  # noqa: S108
    yield os.path.join(tempfile.gettempdir(), "kicad", "api.sock")


def kicad_session_start() -> Optional[float]:
    """When the running KiCad started, or ``None`` if that cannot be told.

    ``None`` is the honest answer whenever the socket is missing — a KiCad
    built without the API server, an app run against a fixture with no KiCad at
    all — and every caller treats it as "assume the lock is live".
    """
    for candidate in _socket_candidates():
        try:
            return os.path.getmtime(candidate)
        except OSError:
            continue
    return None


@dataclass(frozen=True)
class LockState:
    """What is known about one document's lock, and how sure of it we are."""

    #: The document, not the lock file.
    path: str
    lock_path: str
    #: When the lock was written, or ``None`` when there is no lock.
    lock_time: Optional[float]
    #: When the running KiCad started, or ``None`` when that is unknown.
    session_start: Optional[float]
    #: ``{"username": ..., "hostname": ...}`` as KiCad wrote it.
    owner: dict

    @property
    def exists(self) -> bool:
        """Whether there is a lock file at all."""
        return self.lock_time is not None

    @property
    def foreign(self) -> bool:
        """Whether the lock names somebody else, or another machine."""
        if not self.exists:
            return False
        username = str(self.owner.get("username") or "")
        hostname = str(self.owner.get("hostname") or "")
        if username and username.casefold() != getpass.getuser().casefold():
            return True
        return bool(
            hostname
            and _short_host(hostname) != _short_host(socket_module.gethostname())
        )

    @property
    def stale(self) -> bool:
        """Whether the lock outlived the session that wrote it."""
        if not self.exists or self.foreign or self.session_start is None:
            return False
        return self.lock_time < self.session_start

    @property
    def held(self) -> bool:
        """Whether an editor should be assumed to have this document open."""
        return self.exists and not self.stale

    @property
    def state(self) -> str:
        """One of :data:`NONE`, :data:`STALE`, :data:`FOREIGN`, :data:`HELD`."""
        if not self.exists:
            return NONE
        if self.foreign:
            return FOREIGN
        return STALE if self.stale else HELD

    def describe(self) -> str:
        """Say why this lock is being ignored, for the log."""
        return (
            f"{os.path.basename(self.lock_path)} was left behind by a KiCad "
            "session that ended before this one started, so nothing has "
            f"{os.path.basename(self.path)} open. Ignoring it; the file is "
            "safe to delete."
        )


#: Distinguishes "the caller passed no session start" from "the caller passed
#: ``None`` because it knows the session cannot be dated". The second is a
#: test asking for the pre-dating behaviour and must not trigger a lookup.
_UNSET = object()


def inspect(path: str, session_start=_UNSET) -> LockState:
    """Read the lock state of one document."""
    if session_start is _UNSET:
        session_start = kicad_session_start()
    lock_path = lock_file_for(path)
    try:
        lock_time: Optional[float] = os.path.getmtime(lock_path)
    except OSError:
        lock_time = None
    return LockState(
        path=path,
        lock_path=lock_path,
        lock_time=lock_time,
        session_start=session_start,
        owner=read_lock(path) if lock_time is not None else {},
    )


def inspect_all(paths, session_start=_UNSET) -> list:
    """Read the lock state of several documents against one session start.

    One socket lookup for the whole hierarchy rather than one per sheet, and —
    more importantly — one *answer*: dating half a hierarchy against a KiCad
    that restarted mid-loop is a way to get two different verdicts for the same
    session.
    """
    if session_start is _UNSET:
        session_start = kicad_session_start()
    return [inspect(path, session_start) for path in paths]


def is_open_in_editor(path: str) -> bool:
    """Whether an editor currently has this document open."""
    return inspect(path).held


__all__ = [
    "FOREIGN",
    "HELD",
    "LOCK_PREFIX",
    "LOCK_SUFFIX",
    "NONE",
    "STALE",
    "LockState",
    "inspect",
    "inspect_all",
    "is_open_in_editor",
    "kicad_session_start",
    "lock_file_for",
    "read_lock",
]
