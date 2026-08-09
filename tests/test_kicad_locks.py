"""Tests for telling a live KiCad lock from one a dead session left behind.

The bug these exist for: KiCad was force-quit with a schematic open, left
``~board.kicad_sch.lck`` next to it, and every "To schematic" afterwards
refused with "close the Schematic Editor and try again" — of an editor that
was not running. The lock file is the only signal KiCad publishes and it
carries no process id, so what separates the two cases is that the lock was
written *before the running KiCad started*.
"""

import getpass
import json
import os
from pathlib import Path
import socket as socket_module

from lcsc_suite import kicad_locks


def lock(path: Path, when: float, owner=None) -> Path:
    """Write a lock file for ``path``, stamped ``when``, as KiCad would."""
    payload = {
        "hostname": socket_module.gethostname(),
        "username": getpass.getuser(),
    }
    payload.update(owner or {})
    lock_path = Path(kicad_locks.lock_file_for(str(path)))
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(lock_path, (when, when))
    return lock_path


def test_no_lock_file_is_no_lock(tmp_path):
    """The ordinary case: nothing next to the schematic, nothing to say."""
    state = kicad_locks.inspect(str(tmp_path / "board.kicad_sch"), session_start=100.0)

    assert state.state == kicad_locks.NONE
    assert not state.exists
    assert not state.held


def test_lock_written_after_the_session_started_is_held(tmp_path):
    """A lock this KiCad could have written is treated as an open editor."""
    path = tmp_path / "board.kicad_sch"
    lock(path, when=200.0)

    state = kicad_locks.inspect(str(path), session_start=100.0)

    assert state.state == kicad_locks.HELD
    assert state.held


def test_lock_older_than_the_session_is_stale(tmp_path):
    """The reported bug: a leftover from a KiCad that is no longer running."""
    path = tmp_path / "board.kicad_sch"
    lock(path, when=50.0)

    state = kicad_locks.inspect(str(path), session_start=100.0)

    assert state.state == kicad_locks.STALE
    assert state.stale
    assert not state.held
    assert "~board.kicad_sch.lck" in state.describe()


def test_an_undatable_session_leaves_every_lock_held(tmp_path):
    """No socket to date the session by: assume the worst, as before.

    Being wrong this way costs a dialog. Being wrong the other way overwrites
    edits only eeschema's in-memory copy holds, so the unknown case keeps the
    behaviour this module replaced.
    """
    path = tmp_path / "board.kicad_sch"
    lock(path, when=50.0)

    state = kicad_locks.inspect(str(path), session_start=None)

    assert state.state == kicad_locks.HELD
    assert state.held


def test_a_lock_from_another_user_is_never_stale(tmp_path):
    """Somebody else's session cannot be dated by our KiCad's socket."""
    path = tmp_path / "board.kicad_sch"
    lock(path, when=50.0, owner={"username": "someone-else"})

    state = kicad_locks.inspect(str(path), session_start=100.0)

    assert state.state == kicad_locks.FOREIGN
    assert state.foreign
    assert state.held


def test_a_lock_from_another_host_is_never_stale(tmp_path):
    """A shared drive: the holder is on a machine whose clock is not ours."""
    path = tmp_path / "board.kicad_sch"
    lock(path, when=50.0, owner={"hostname": "someone-elses-laptop"})

    state = kicad_locks.inspect(str(path), session_start=100.0)

    assert state.state == kicad_locks.FOREIGN


def test_a_fully_qualified_hostname_is_still_this_machine(tmp_path):
    """``wxGetHostName()`` writes ``Mac`` where Python may say ``Mac.local``."""
    path = tmp_path / "board.kicad_sch"
    short = socket_module.gethostname().split(".")[0]
    lock(path, when=50.0, owner={"hostname": f"{short}.local"})

    state = kicad_locks.inspect(str(path), session_start=100.0)

    assert not state.foreign
    assert state.state == kicad_locks.STALE


def test_an_unparseable_lock_is_treated_as_ours(tmp_path):
    """Older KiCad wrote an empty file. That names no stranger."""
    path = tmp_path / "board.kicad_sch"
    lock_path = Path(kicad_locks.lock_file_for(str(path)))
    lock_path.write_text("", encoding="utf-8")
    os.utime(lock_path, (50.0, 50.0))

    state = kicad_locks.inspect(str(path), session_start=100.0)

    assert state.owner == {}
    assert not state.foreign
    assert state.state == kicad_locks.STALE


def test_inspect_all_dates_every_sheet_against_one_session(tmp_path):
    """One answer for the hierarchy, not one per sheet."""
    root = tmp_path / "root.kicad_sch"
    sub = tmp_path / "sub.kicad_sch"
    lock(root, when=50.0)
    lock(sub, when=200.0)

    states = kicad_locks.inspect_all([str(root), str(sub)], session_start=100.0)

    assert [state.state for state in states] == [kicad_locks.STALE, kicad_locks.HELD]
    assert {state.session_start for state in states} == {100.0}


def test_the_socket_dates_the_running_session(tmp_path, monkeypatch):
    """KiCad recreates its IPC socket at start-up, so its mtime is the start."""
    socket_path = tmp_path / "kicad" / "api.sock"
    socket_path.parent.mkdir()
    socket_path.write_text("", encoding="utf-8")
    os.utime(socket_path, (1234.0, 1234.0))
    monkeypatch.setenv("KICAD_API_SOCKET", f"ipc://{socket_path}")

    assert kicad_locks.kicad_session_start() == 1234.0


def test_no_socket_means_no_session_start(tmp_path, monkeypatch):
    """A fixture run, or a KiCad built without the API server."""
    monkeypatch.setenv("KICAD_API_SOCKET", str(tmp_path / "nothing" / "api.sock"))

    assert kicad_locks.kicad_session_start() is None
