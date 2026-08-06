"""Settings, moved out of the checkout and into a per-user config directory.

The wx plugin writes ``settings.json`` into ``PLUGIN_PATH`` — that is, into the
git checkout, where it is a tracked file the running plugin mutates. That works
only because the plugin *is* the checkout. Out of process it cannot stay there:
a frozen binary's install directory may be read-only, and two halves of a
migration writing to one file would fight.

So settings live in ``QStandardPaths.AppConfigLocation`` and the old file is
imported **once**, on first run, so nobody has to set their preferences twice.

Keys and defaults are the wx plugin's, minus everything Gerber (dropped in the
plan's §1). Unknown keys found in an imported file are kept rather than
discarded: the two halves coexist until the Phase 8 cutover, and silently
dropping a key the wx plugin still reads would be a data loss the user cannot
see.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any

from PySide6.QtCore import QStandardPaths

log = logging.getLogger(__name__)

APPLICATION_NAME = "LCSC Suite"
SETTINGS_FILENAME = "settings.json"

#: Gerber-plotting settings, dropped with the plot path itself. Listed so the
#: import step can strip them rather than carry dead keys forward for ever.
DROPPED_KEYS = {
    "gerber": (
        "tented_vias",
        "fill_zones",
        "force_drc",
        "plot_values",
        "plot_references",
        "subtract_mask_from_silk",
    ),
    "hooks": ("pre_script", "post_script", "timeout_seconds"),
}

DEFAULTS: dict[str, Any] = {
    "general": {
        # "LCSC numbers from database have priority"
        "lcsc_priority": False,
        # "Add parts without LCSC number to BOM/POS"
        "order_number": True,
        "select_alike_auto": True,
        "highlight_standard_parts": True,
        "bom_estimator_boards": 5,
        "bom_estimator_force_standard": False,
        "bom_estimator_show": True,
    },
    "highlighting": {
        "matches": True,
    },
    "library": {
        "selected_library": "current-parts",
        "data_path": "",
    },
    "lcsc": {
        "explorer_detail_layout": "side",
        "library_folder": "",
        "overwrite_existing": False,
    },
    "window": {
        # Filled in by the main window on close; see ui/main_window.py.
        "main_geometry": "",
        "explorer_geometry": "",
    },
}


def config_dir() -> str:
    """Return the per-user configuration directory, creating it if needed.

    ``AppConfigLocation`` is *already* qualified by the application and
    organisation names set on the QApplication, so do not append them again —
    doing so yields ``.../LCSC Suite/LCSC Suite/LCSC Suite/``.
    """
    path = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppConfigLocation
    )
    if not path:  # pragma: no cover - only on an unconfigured platform
        path = os.path.join(os.path.expanduser("~/.config"), APPLICATION_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path() -> str:
    """Return the full path of the settings file."""
    return os.path.join(config_dir(), SETTINGS_FILENAME)


def _merge(defaults: dict, loaded: dict) -> dict:
    """Overlay ``loaded`` onto ``defaults``, one level deep.

    Sections are merged rather than replaced so a settings file written by an
    older version does not lose the keys it never knew about.
    """
    result = copy.deepcopy(defaults)
    for section, values in (loaded or {}).items():
        if isinstance(values, dict) and isinstance(result.get(section), dict):
            result[section].update(values)
        else:
            result[section] = values
    return result


def _strip_dropped(settings: dict) -> dict:
    """Remove the settings that went with Gerber output."""
    for section, keys in DROPPED_KEYS.items():
        block = settings.get(section)
        if not isinstance(block, dict):
            continue
        for key in keys:
            block.pop(key, None)
        if not block:
            settings.pop(section, None)
    return settings


class Settings:
    """The application's settings, loaded once and saved on change."""

    def __init__(self, path: str | None = None, legacy_path: str | None = None) -> None:
        self.path = path or config_path()
        self.legacy_path = legacy_path
        self.imported_from: str | None = None
        self._values = self._load()

    # -- loading ------------------------------------------------------------

    def _load(self) -> dict:
        if os.path.isfile(self.path):
            return _merge(DEFAULTS, self._read(self.path))

        legacy = self.legacy_path
        if legacy and os.path.isfile(legacy):
            log.info("Importing settings from the wx plugin at %s", legacy)
            merged = _strip_dropped(_merge(DEFAULTS, self._read(legacy)))
            self.imported_from = legacy
            self._values = merged
            self.save()
            return merged

        return copy.deepcopy(DEFAULTS)

    @staticmethod
    def _read(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, json.JSONDecodeError):
            log.warning("Could not read settings from %s; using defaults", path)
            return {}
        return loaded if isinstance(loaded, dict) else {}

    # -- access -------------------------------------------------------------

    @property
    def values(self) -> dict:
        """The whole settings mapping.

        Exposed because ``library.py`` reads ``parent.settings`` directly, and
        keeping that contract is what lets it be reused unchanged.
        """
        return self._values

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """Read one setting, falling back to the shipped default."""
        block = self._values.get(section)
        if isinstance(block, dict) and key in block:
            return block[key]
        shipped = DEFAULTS.get(section, {})
        return shipped.get(key, default)

    def set(self, section: str, key: str, value: Any) -> None:
        """Write one setting and persist immediately.

        Immediately, because the app is launched from a toolbar button and may
        be closed by the window manager at any moment; a deferred save loses
        the last thing the user changed.
        """
        self._values.setdefault(section, {})[key] = value
        self.save()

    def save(self) -> None:
        """Write the settings file atomically."""
        temporary = f"{self.path}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self._values, handle, indent=2, sort_keys=True)
            os.replace(temporary, self.path)
        except OSError:
            log.exception("Could not save settings to %s", self.path)


def legacy_settings_path() -> str:
    """Where the wx plugin keeps its settings, for the one-time import."""
    from .shared import REPO_ROOT

    return os.path.join(REPO_ROOT, SETTINGS_FILENAME)
