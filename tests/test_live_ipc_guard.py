"""Tests for the one guard that keeps ``live_ipc_check.py`` off a real board.

That script exists to write to a board and read the writes back, because a
fixture cannot prove the IPC API behaves (trap 4 survived two phases of green
fixture tests). Everything downstream of :func:`_is_disposable` therefore
*writes*, which makes this function the entire safety boundary — and it is not
covered by the script's own run, because a run that reaches the assertions has
already passed it.

It is tested here rather than there because it is pure: no KiCad, no socket.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "live_ipc_check.py")


def _load():
    """Import ``scripts/live_ipc_check.py`` without putting scripts/ on sys.path."""
    spec = importlib.util.spec_from_file_location("live_ipc_check", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    """Return the function under test."""
    return _load()._is_disposable


# --- the regression this file was written for -------------------------------


def test_a_project_named_temperature_is_not_a_temporary_directory(guard):
    """``temperature-controller`` is a real project and must be refused.

    The guard matched ``"temp"`` as a *substring*, so every board under
    ``~/Research/temperature-controller/`` passed it — including the board this
    fork was developed against. A name beginning with four particular letters is
    not consent, and the failure was silent and in the permissive direction.
    """
    assert not guard("/Users/someone/Research/temperature-controller/PCB", [])


@pytest.mark.parametrize(
    "path",
    [
        "/Users/someone/Research/template-library/PCB",
        "/Users/someone/tmpfs-experiments/board",
        "/Users/someone/scratchpad-notes/board",
        "/Users/someone/attempt-3/board",
    ],
)
def test_names_that_merely_contain_a_fragment_are_refused(guard, path):
    """``attempt`` contains ``temp``; none of these is a temporary directory."""
    assert not guard(path, [])


# --- what must still be allowed, or the tool is unusable --------------------


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/lcsc-live",  # noqa: S108 - the documented workflow's own path
        "/private/tmp/lcsc-live",  # noqa: S108 - what macOS resolves it to
        "/Users/someone/scratch/livecheck",
        "/Users/someone/temp/livecheck",
        "/Users/someone/Documents/TMP/livecheck",  # matched case-insensitively
    ],
)
def test_a_directory_actually_called_tmp_or_scratch_is_disposable(guard, path):
    """The documented workflow must keep working, or nobody runs the check."""
    assert guard(path, [])


# --- the --allow half, which had the same bug one step along ----------------


def test_allow_permits_the_named_directory_and_its_descendants(guard):
    """An explicit opt-in covers the directory named and everything under it."""
    assert guard("/opt/boards", ["/opt/boards"])
    assert guard("/opt/boards/project/pcb", ["/opt/boards"])


def test_allow_does_not_leak_to_a_sibling_sharing_its_prefix(guard):
    """``--allow /opt/a`` permitted ``/opt/abc`` when this was a substring test.

    Deliberately not built from ``tmp_path``: on Linux that lives under ``/tmp``,
    whose ``tmp`` component would satisfy the first rule and make this pass
    without exercising ``--allow`` at all.
    """
    assert not guard("/opt/abc", ["/opt/a"])


def test_nothing_allowed_and_nothing_disposable_is_a_refusal(guard):
    """The default answer is no, which is the only safe default here."""
    assert not guard("/Users/someone/Research/my-board", [])
