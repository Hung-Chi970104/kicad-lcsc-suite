"""The product name, and the things that repeat it where Python cannot reach.

``kicad_plugin/plugin.json`` and ``PCM/metadata.template.json`` are read by
KiCad and by the Plugin and Content Manager *before* any module here is
imported, so they cannot refer to :data:`lcsc_suite.ui.brand.APP_NAME` — they
spell the name out, and nothing but this file stops the three copies drifting.

The failure mode is quiet in every case, which is why these are tests rather
than a note in a README: a manifest naming an icon that is not there gives a
blank toolbar button, and KiCad reports nothing at all.

The last test guards the opposite of a rename: ``config.APPLICATION_NAME`` is a
storage key that deliberately did **not** change, and the reason is only
written down in comments that a future rename would be editing anyway.
"""

from __future__ import annotations

import json
import os
import re

import lcsc_suite
from lcsc_suite import config
from lcsc_suite.ui import brand

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_JSON = os.path.join(ROOT, "kicad_plugin", "plugin.json")
PCM_METADATA = os.path.join(ROOT, "PCM", "metadata.template.json")

#: ``metadata.template.json`` is a template, and two of its placeholders stand
#: where JSON wants a *number* — so the file is deliberately not valid JSON
#: until ``PCM/create_pcm_archive.sh`` fills it in. Zero them rather than
#: reading the file as text: the point of these tests is to check fields, and a
#: substring search would pass on a file whose braces no longer balance.
_UNQUOTED_PLACEHOLDER = re.compile(r":\s*[A-Z_]+_HERE")


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return json.loads(_UNQUOTED_PLACEHOLDER.sub(": 0", text))


def test_plugin_manifest_names_the_product() -> None:
    """KiCad's manifest and :mod:`brand` agree on the name and the blurb."""
    manifest = _load(PLUGIN_JSON)
    assert manifest["name"] == brand.APP_NAME
    assert manifest["description"] == brand.APP_TAGLINE
    action = manifest["actions"][0]
    assert action["name"] == brand.APP_NAME
    assert brand.APP_NAME in action["description"]


def test_pcm_metadata_names_the_product() -> None:
    """The PCM listing agrees too, and shares the plugin's identifier."""
    metadata = _load(PCM_METADATA)
    manifest = _load(PLUGIN_JSON)
    assert metadata["name"] == brand.APP_NAME
    # A PCM package whose identifier differs from the manifest's installs as
    # one plugin and updates as another.
    assert metadata["identifier"] == manifest["identifier"]


def test_every_icon_the_manifest_names_exists() -> None:
    """Each declared icon is a real file.

    ``scripts/make_brand_icons.py`` writes these and they are committed. A
    manifest entry pointing at a path that does not exist costs the toolbar
    button its image and produces no error anywhere.
    """
    action = _load(PLUGIN_JSON)["actions"][0]
    declared = action["icons-light"] + action["icons-dark"]
    assert declared, "the manifest declares no icons at all"
    for relative in declared:
        path = os.path.join(ROOT, "kicad_plugin", relative)
        assert os.path.isfile(path), f"{relative} is named by plugin.json but missing"
        assert os.path.getsize(path) > 0, f"{relative} is empty"


def test_brand_reexports_the_root_name_without_drift() -> None:
    """``ui.brand`` re-exports the root's name rather than restating it."""
    assert brand.APP_NAME is lcsc_suite.APP_NAME
    assert brand.APP_TAGLINE is lcsc_suite.APP_TAGLINE


def test_the_bridge_stays_free_of_qt() -> None:
    """``kicad_bridge`` imports no Qt, which is why the name lives at the root.

    The bridge writes the product name into KiCad's undo history, so it needs
    :data:`APP_NAME`. Had the name stayed in ``ui.brand`` — which draws the mark
    and therefore imports ``QPainter`` — reaching for it would have dragged Qt
    into the one module that deliberately has none of it, and into
    ``scripts/live_ipc_check.py`` with it. Hence the root, and hence this test:
    the reason is only recorded in comments, which a future edit is free to
    delete.
    """
    source = os.path.join(ROOT, "lcsc_suite", "kicad_bridge.py")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "PySide6" not in text
    assert "from .ui" not in text


def test_the_settings_key_did_not_follow_the_rebrand() -> None:
    """``config.APPLICATION_NAME`` is a storage key and must not track the name.

    It is half the ``QStandardPaths`` key under which existing installs keep
    their settings and their optional 750MB parts database. Renaming it to
    match the product does not migrate either one — it silently starts over in
    an empty directory, which is indistinguishable from a first run until
    somebody goes looking for a download they already made.

    If a future rename genuinely wants to move that data, the thing to write is
    a migration in ``config.adopt_data_directory`` and a change to this test —
    in that order.
    """
    assert config.APPLICATION_NAME == "LCSC Suite"
    assert config.APPLICATION_NAME != brand.APP_NAME
