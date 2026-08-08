"""Tests for the Qt app's settings file and its one-time import.

The wx plugin writes ``settings.json`` into the git checkout — a tracked file
the running plugin mutates. The Qt app cannot keep it there (a frozen binary's
install directory may be read-only), so it moves to a per-user config directory
and imports the old file once.

Two things have to hold or a user notices:

* the import happens exactly once, and never overwrites settings the user has
  since changed in the new app;
* keys the *new* app does not recognise are carried across rather than dropped.
  Both halves are installed until the Phase 8 cutover, and silently discarding
  something the wx plugin still reads is invisible data loss.
"""

from __future__ import annotations

import json

import pytest

from lcsc_suite import config
from lcsc_suite.config import DEFAULTS, Settings


@pytest.fixture
def legacy(tmp_path):
    """Write a settings file in the wx plugin's shape and return its path."""
    path = tmp_path / "legacy-settings.json"
    path.write_text(
        json.dumps(
            {
                "general": {"lcsc_priority": True, "bom_estimator_boards": 12},
                # Everything that went with Gerber output.
                "gerber": {
                    "tented_vias": True,
                    "fill_zones": True,
                    "force_drc": True,
                    "plot_values": True,
                    "plot_references": True,
                    "subtract_mask_from_silk": True,
                    "lcsc_bom_cpl": True,
                },
                "hooks": {"pre_script": "/bin/true", "timeout_seconds": 30},
                # A section the Qt app has no defaults for.
                "partselector": {"preferred": True},
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def test_defaults_when_nothing_exists(tmp_path):
    """A fresh install gets the shipped defaults and writes no file yet."""
    target = tmp_path / "settings.json"
    settings = Settings(path=str(target))

    assert settings.get("general", "bom_estimator_boards") == 5
    assert settings.imported_from is None
    assert not target.exists()


def test_imports_the_wx_plugin_settings_once(tmp_path, legacy):
    """The old file is read on first run and the result persisted."""
    target = tmp_path / "settings.json"
    settings = Settings(path=str(target), legacy_path=legacy)

    assert settings.imported_from == legacy
    assert settings.get("general", "lcsc_priority") is True
    assert settings.get("general", "bom_estimator_boards") == 12
    assert target.exists()

    # Second run reads the new file and does not import again, even though the
    # legacy file is still sitting there.
    again = Settings(path=str(target), legacy_path=legacy)
    assert again.imported_from is None
    assert again.get("general", "bom_estimator_boards") == 12


def test_a_later_change_survives_the_legacy_file_still_existing(tmp_path, legacy):
    """Importing must never clobber what the user has since changed."""
    target = tmp_path / "settings.json"
    Settings(path=str(target), legacy_path=legacy).set(
        "general", "bom_estimator_boards", 3
    )

    reopened = Settings(path=str(target), legacy_path=legacy)
    assert reopened.get("general", "bom_estimator_boards") == 3


def test_gerber_and_hook_settings_are_dropped_on_import(tmp_path, legacy):
    """Settings that went with the plot path do not come across.

    Except ``lcsc_bom_cpl``: the BOM and CPL writers are explicitly *kept*
    (plan §1), so their one setting is kept with them.
    """
    settings = Settings(path=str(tmp_path / "settings.json"), legacy_path=legacy)

    gerber = settings.values.get("gerber", {})
    assert "tented_vias" not in gerber
    assert "force_drc" not in gerber
    assert "subtract_mask_from_silk" not in gerber
    assert gerber.get("lcsc_bom_cpl") is True
    assert "hooks" not in settings.values


def test_unrecognised_sections_are_carried_across(tmp_path, legacy):
    """A key the Qt app does not know is not a key it may throw away."""
    settings = Settings(path=str(tmp_path / "settings.json"), legacy_path=legacy)
    assert settings.values["partselector"] == {"preferred": True}


def test_missing_keys_fall_back_to_the_shipped_default(tmp_path):
    """A section present but incomplete still answers for its other keys."""
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps({"general": {"lcsc_priority": True}}), encoding="utf-8"
    )
    settings = Settings(path=str(target))

    assert settings.get("general", "lcsc_priority") is True
    assert settings.get("general", "bom_estimator_boards") == 5
    assert settings.get("highlighting", "matches") is True


def test_a_corrupt_file_is_survivable(tmp_path):
    """Half-written JSON falls back to defaults rather than failing to start."""
    target = tmp_path / "settings.json"
    target.write_text("{not json", encoding="utf-8")
    settings = Settings(path=str(target))

    assert settings.get("general", "bom_estimator_boards") == 5


def test_defaults_carry_no_gerber_plotting_keys():
    """The dropped settings are gone from the shipped defaults too."""
    assert "hooks" not in DEFAULTS
    assert set(DEFAULTS.get("gerber", {})) <= {"lcsc_bom_cpl"}


# ---------------------------------------------------------------------------
# adopt_data_directory
#
# This is the function that decides where a user's parts databases are, and one
# of the candidates it looks in is up to 750MB. It had no tests, which is the
# wrong shape of gap for code whose own docstring records having got this wrong
# once already: deriving the directory from a module's location silently moved a
# user's data every time the package moved, and orphaned the download.
#
# The candidate list is also the last thing pointing into the deleted wx
# plugin's directory, so "the legacy directory is gone" is a state these tests
# have to cover rather than assume.
# ---------------------------------------------------------------------------


def _database(directory, name: str, size: int) -> None:
    """Create *directory* and put a ``name`` of ``size`` bytes in it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(b"\0" * size)


def test_nothing_to_adopt_leaves_the_path_empty(tmp_path, monkeypatch):
    """No candidate on disk is not an error, and writes nothing."""
    monkeypatch.setattr(
        config,
        "LEGACY_DATA_DIRECTORIES",
        (str(tmp_path / "gone"), str(tmp_path / "also-gone")),
    )
    target = tmp_path / "settings.json"
    settings = Settings(path=str(target))

    assert config.adopt_data_directory(settings) == ""
    assert settings.get("library", "data_path") == ""
    # Nothing was decided, so nothing is persisted — the next run gets to look
    # again, which is what should happen once a database has been downloaded.
    assert not target.exists()


def test_a_directory_holding_databases_is_adopted_and_persisted(tmp_path, monkeypatch):
    """The answer is written to settings, so it stops depending on layout."""
    candidate = tmp_path / "jlcpcb"
    _database(candidate, "current-parts-fts5.db", 4096)
    monkeypatch.setattr(config, "LEGACY_DATA_DIRECTORIES", (str(candidate),))
    target = tmp_path / "settings.json"
    settings = Settings(path=str(target))

    assert config.adopt_data_directory(settings) == str(candidate)
    # Persisted, not just remembered: the whole point is that a later run does
    # not have to work it out again from wherever the package happens to be.
    assert json.loads(target.read_text(encoding="utf-8"))["library"][
        "data_path"
    ] == str(candidate)


def test_the_largest_candidate_wins(tmp_path, monkeypatch):
    """Where two hold data the expensive one is adopted, not the first found."""
    small = tmp_path / "old-root" / "jlcpcb"
    large = tmp_path / "new-root" / "jlcpcb"
    _database(small, "partcache.db", 8192)
    _database(large, "current-parts-fts5.db", 1_000_000)
    # Deliberately listed smallest-first, so passing cannot be an artefact of
    # the order the tuple happens to be written in.
    monkeypatch.setattr(config, "LEGACY_DATA_DIRECTORIES", (str(small), str(large)))
    settings = Settings(path=str(tmp_path / "settings.json"))

    assert config.adopt_data_directory(settings) == str(large)


def test_a_directory_with_no_databases_is_not_adopted(tmp_path, monkeypatch):
    """An empty shell of a directory is not data, and a 0-byte file is not either.

    Both really occur: the wx plugin's own ``jlcpcb/`` is left behind holding a
    zero-length ``current-parts-fts5.db`` from a download that never ran, and
    adopting that would point the app at nothing while looking configured.
    """
    hollow = tmp_path / "hollow"
    _database(hollow, "current-parts-fts5.db", 0)
    (hollow / "notes.txt").write_text("not a database", encoding="utf-8")
    monkeypatch.setattr(config, "LEGACY_DATA_DIRECTORIES", (str(hollow),))
    settings = Settings(path=str(tmp_path / "settings.json"))

    assert config.adopt_data_directory(settings) == ""


def test_a_configured_path_is_never_second_guessed(tmp_path, monkeypatch):
    """Adoption is first-run only; it may not overrule a chosen directory."""
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    bigger = tmp_path / "jlcpcb"
    _database(bigger, "current-parts-fts5.db", 1_000_000)
    monkeypatch.setattr(config, "LEGACY_DATA_DIRECTORIES", (str(bigger),))
    settings = Settings(path=str(tmp_path / "settings.json"))
    settings.set("library", "data_path", str(chosen))

    # Even though the candidate has data and the configured directory has none.
    assert config.adopt_data_directory(settings) == str(chosen)


def test_the_wx_plugin_directory_being_deleted_is_not_an_error(tmp_path, monkeypatch):
    """The state the repository is left in once the legacy directory goes.

    Both of the things that still point into it — the settings import and the
    database candidates — must read a missing directory as "nothing to do"
    rather than raising. This is the whole of what the deletion changes for a
    user who has already run the app once.
    """
    monkeypatch.setattr(
        config, "LEGACY_SETTINGS_PATHS", (str(tmp_path / "deleted" / "settings.json"),)
    )
    monkeypatch.setattr(
        config, "LEGACY_DATA_DIRECTORIES", (str(tmp_path / "deleted" / "jlcpcb"),)
    )

    settings = Settings(
        path=str(tmp_path / "settings.json"),
        legacy_path=config.legacy_settings_path(),
    )

    assert settings.imported_from is None
    assert settings.get("general", "bom_estimator_boards") == 5
    assert config.adopt_data_directory(settings) == ""
