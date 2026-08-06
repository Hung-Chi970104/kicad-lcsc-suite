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
