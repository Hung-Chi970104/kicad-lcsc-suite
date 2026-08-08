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
# The fix is the PyInstaller freeze the plan's §8 already schedules for "before
# any public release": because runtime.type is `exec`, swapping the venv for a
# frozen binary touches only kicad_plugin/run.sh, and `platforms` is then how
# the three builds are published. Until that exists, install.sh is the supported
# path and this script produces an archive for testing, not for users.
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

sed_inplace() {
	sed -i.bak "$1" "$2"
	rm -f "$2.bak"
}

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

echo "Modify archive metadata.json"
sed_inplace "s/VERSION_HERE/$VERSION/g" "$METADATA_FILE"
sed_inplace "s/\"kicad_version\": \"10.0\",/\"kicad_version\": \"10.0\"/g" "$METADATA_FILE"
for placeholder in SHA256_HERE DOWNLOAD_SIZE_HERE DOWNLOAD_URL_HERE INSTALL_SIZE_HERE; do
	sed_inplace "/$placeholder/d" "$METADATA_FILE"
done

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

