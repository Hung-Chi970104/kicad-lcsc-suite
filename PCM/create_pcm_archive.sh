#!/bin/sh

# heavily inspired by https://github.com/4ms/4ms-kicad-lib/blob/master/PCM/make_archive.sh
#
# PACKAGING STATUS — read before cutting a release.
#
# The Phase 8 question "can PCM ship an IPC-runtime plugin at all" is answered:
# **yes.** The v1 schema has `"runtime": {"enum": ["swig", "ipc"]}` on a package
# version, and a `platforms` array of windows/macos/linux, which is how a
# package carrying a per-platform binary is declared. KiCad's own add-on docs
# say the same. The metadata template sets `runtime: ipc` accordingly.
#
# What is **not** answered is the harder half: this archive ships Python source,
# and the app needs its own interpreter with PySide6 and kicad-python in it.
# PCM unpacks files; it does not build a virtualenv. So a PCM install works
# today only for somebody who already has that environment, which is nobody who
# is installing through PCM.
#
# There is a second obstacle, smaller and easy to miss because the first one
# hides it. **This archive's layout does not match what run.sh expects.** The
# launcher resolves the app as `$(dirname run.sh)/../lcsc_suite` — correct for
# the development install, where install.sh symlinks `kicad_plugin/` into KiCad
# and the package really is one level up, in the checkout. In the archive the
# two are *siblings*: `plugins/run.sh` next to `plugins/lcsc_suite/`. So
# REPO_ROOT lands one directory above the package, where there is neither a venv
# nor an importable `lcsc_suite`, and the button does nothing.
#
# The fix for both is the PyInstaller freeze the plan's §8 already schedules for
# "before any public release": because runtime.type is `exec`, swapping the venv
# for a frozen binary touches only kicad_plugin/run.sh — which is the same file
# the layout question has to be settled in, so settle them together. `platforms`
# is then how the three builds are published. Until that exists, install.sh is
# the supported path and this script produces an archive for testing, not for
# users.
set -eu

if [ $# -lt 1 ]; then
	echo "Usage: $0 <version>"
	exit 1
fi

VERSION=$1
ARCHIVE_DIR="PCM/archive"
PLUGINS_DIR="$ARCHIVE_DIR/plugins"
RESOURCES_DIR="$ARCHIVE_DIR/resources"
ZIP_FILE="PCM/KiCAD-PCM-$VERSION.zip"
METADATA_FILE="$ARCHIVE_DIR/metadata.json"

# The metadata step below parses JSON, so it needs an interpreter. The venv's is
# preferred because it is the one the project is developed against; python3 is
# the fallback for a release cut on a machine with no venv built.
if [ -x ".venv/bin/python" ]; then
	PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
	PYTHON="python3"
else
	echo "No python3 found; needed to write and validate metadata.json." >&2
	exit 1
fi

echo "Clean up old files"
rm -f PCM/*.zip
rm -rf "$ARCHIVE_DIR"

echo "Create folder structure for ZIP"
mkdir -p "$PLUGINS_DIR" "$RESOURCES_DIR"

# The whole plugin is one directory, so this is a single copy rather than the
# file-and-directory enumeration it used to be — a list that silently shipped a
# broken plugin whenever a new module was added and not added here.
#
# What goes in changed at the Phase 8 cutover: the wx package is gone, and what
# ships is the Qt app plus the manifest and launcher KiCad reads to start it.
echo "Copy the plugin package"
cp -R lcsc_suite/. "$PLUGINS_DIR/lcsc_suite"
cp -R kicad_plugin/. "$PLUGINS_DIR"

echo "Prune caches from packaged plugin"
find "$PLUGINS_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$PLUGINS_DIR" -type f -name '*.pyc' -delete

cp PCM/icon.png "$RESOURCES_DIR"
cp PCM/metadata.template.json "$METADATA_FILE"

echo "Write version info to file"
echo "$VERSION" > "$PLUGINS_DIR/lcsc_suite/VERSION"

# The four download_*/install_size fields describe a zip that does not exist
# yet, so they are placeholders in the template and are dropped from the copy
# that goes *inside* the archive. Their real values belong to the repository
# index, and are printed at the end of this script for whatever rebuilds it.
#
# This was five `sed` edits: four line deletions, plus one that removed the
# comma after `kicad_version` because that line was the last one left standing.
# Adding `"runtime": "ipc"` after it at the Phase 8 cutover falsified that
# assumption and the script began emitting a metadata.json with a *missing*
# comma after `kicad_version` and a *trailing* one after `runtime` — invalid
# JSON, which PCM rejects outright, written silently by a script no test runs.
#
# So: delete the placeholder lines, then repair whatever comma the deletions
# stranded before a closing brace or bracket, wherever it is. That is
# insensitive to field order, which is what went wrong. Then parse the result —
# the check that was missing is what turns the next mistake of this shape into a
# failed release build instead of a package users cannot install.
echo "Write archive metadata.json"
"$PYTHON" - "$METADATA_FILE" "$VERSION" <<'PY'
import json
import re
import sys

path, version = sys.argv[1], sys.argv[2]

#: Placeholders for the repository-index-only fields, matched as whole lines.
REPO_ONLY = ("SHA256_HERE", "DOWNLOAD_SIZE_HERE", "DOWNLOAD_URL_HERE", "INSTALL_SIZE_HERE")

with open(path, encoding="utf-8") as handle:
    text = handle.read()

text = text.replace("VERSION_HERE", version)
text = "".join(
    line
    for line in text.splitlines(keepends=True)
    if not any(placeholder in line for placeholder in REPO_ONLY)
)
# A comma is only ever stranded *before* a closer, so this cannot eat a real
# separator: valid JSON never has one there in the first place.
text = re.sub(r",(\s*[}\]])", r"\1", text)

try:
    metadata = json.loads(text)
except json.JSONDecodeError as error:
    sys.exit(f"metadata.json is not valid JSON after templating: {error}\n{text}")

# PCM identifies a package by these three; a release that quietly loses one
# installs as something else, or not at all.
for key in ("identifier", "name", "type"):
    if not metadata.get(key):
        sys.exit(f"metadata.json has no {key!r}")
if not metadata.get("versions"):
    sys.exit("metadata.json has no versions")
for entry in metadata["versions"]:
    if entry.get("version") != version:
        sys.exit(f"version entry says {entry.get('version')!r}, expected {version!r}")

with open(path, "w", encoding="utf-8") as handle:
    handle.write(text)
PY

echo "Zip PCM archive"
(cd "$ARCHIVE_DIR" && zip -r "../KiCAD-PCM-$VERSION.zip" .)

echo "Gather data for repo rebuild"
DOWNLOAD_SHA256=$(shasum --algorithm 256 "$ZIP_FILE" | awk '{print $1}')
DOWNLOAD_SIZE=$(wc -c < "$ZIP_FILE" | tr -d '[:space:]')
DOWNLOAD_URL="https:\/\/github.com\/Hung-Chi970104\/kicad-lcsc-suite\/releases\/download\/$VERSION\/KiCAD-PCM-$VERSION.zip"
INSTALL_SIZE=$(unzip -l "$ZIP_FILE" | awk 'END{print $1}')

if [ -n "${GITHUB_ENV:-}" ]; then
	echo "VERSION=$VERSION" >> "$GITHUB_ENV"
	echo "DOWNLOAD_SHA256=$DOWNLOAD_SHA256" >> "$GITHUB_ENV"
	echo "DOWNLOAD_SIZE=$DOWNLOAD_SIZE" >> "$GITHUB_ENV"
	echo "DOWNLOAD_URL=$DOWNLOAD_URL" >> "$GITHUB_ENV"
	echo "INSTALL_SIZE=$INSTALL_SIZE" >> "$GITHUB_ENV"
else
	echo "VERSION=$VERSION"
	echo "DOWNLOAD_SHA256=$DOWNLOAD_SHA256"
	echo "DOWNLOAD_SIZE=$DOWNLOAD_SIZE"
	echo "DOWNLOAD_URL=$DOWNLOAD_URL"
	echo "INSTALL_SIZE=$INSTALL_SIZE"
fi

