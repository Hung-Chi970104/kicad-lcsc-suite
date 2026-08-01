#!/usr/bin/env bash
# Install kicad-lcsc-suite into KiCad's plugin directory (macOS / Linux).
#
# Symlinks rather than copies, so `git pull` on any machine updates the
# installed plugin with no reinstall step.
#
#   ./install.sh                 install into the newest KiCad found
#   ./install.sh 10.0            install into a specific KiCad version
#   ./install.sh --dir <path>    install into an explicit plugin directory
#   ./install.sh --uninstall     remove the symlink
#   ./install.sh --list          just show what would be detected
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Must be a valid Python identifier — the repo directory name has hyphens.
LINK_NAME="kicad_lcsc_suite"

UNINSTALL=0
LIST_ONLY=0
VERSION=""
EXPLICIT_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) UNINSTALL=1 ;;
        --list)      LIST_ONLY=1 ;;
        --dir)       shift; EXPLICIT_DIR="${1:-}" ;;
        -h|--help)   sed -n '2,12p' "$0"; exit 0 ;;
        *)           VERSION="$1" ;;
    esac
    shift
done

# KiCad's user data directory differs per platform. Check every plausible
# base rather than assuming, so this works on macOS, Linux and WSL.
case "$(uname -s)" in
    Darwin)
        BASES=("$HOME/Documents/KiCad")
        ;;
    *)
        BASES=(
            "${XDG_DATA_HOME:-$HOME/.local/share}/kicad"
            "$HOME/.local/share/kicad"
            "${XDG_DOCUMENTS_DIR:-$HOME/Documents}/KiCad"
        )
        ;;
esac

# Portable "highest version directory" — avoids `sort -V`, which is absent
# on some BSD/macOS sort builds.
newest_version_dir() {
    find "$1" -maxdepth 1 -mindepth 1 -type d 2>/dev/null \
        | while read -r d; do basename "$d"; done \
        | grep -E '^[0-9]+\.[0-9]+$' \
        | sort -t. -k1,1n -k2,2n \
        | tail -1
}

resolve_plugin_dir() {
    local base version
    for base in "${BASES[@]}"; do
        [ -d "$base" ] || continue
        if [ -n "$VERSION" ]; then
            version="$VERSION"
        else
            version="$(newest_version_dir "$base")"
        fi
        [ -n "$version" ] || continue
        echo "$base/$version/scripting/plugins"
        return 0
    done
    return 1
}

if [ -n "$EXPLICIT_DIR" ]; then
    PLUGIN_DIR="$EXPLICIT_DIR"
elif ! PLUGIN_DIR="$(resolve_plugin_dir)"; then
    echo "error: could not find a KiCad user directory. Looked in:" >&2
    printf '  %s\n' "${BASES[@]}" >&2
    echo >&2
    echo "Open KiCad once so it creates its directories, then re-run." >&2
    echo "Or pass one explicitly:  ./install.sh --dir /path/to/scripting/plugins" >&2
    exit 1
fi

TARGET="$PLUGIN_DIR/$LINK_NAME"

if [ "$LIST_ONLY" -eq 1 ]; then
    echo "source      : $SRC"
    echo "plugin dir  : $PLUGIN_DIR"
    echo "target      : $TARGET"
    if [ -L "$TARGET" ]; then
        echo "status      : installed -> $(readlink "$TARGET")"
    elif [ -e "$TARGET" ]; then
        echo "status      : occupied by a non-symlink"
    else
        echo "status      : not installed"
    fi
    exit 0
fi

if [ "$UNINSTALL" -eq 1 ]; then
    if [ -L "$TARGET" ]; then
        rm "$TARGET"
        echo "Removed $TARGET"
    elif [ -e "$TARGET" ]; then
        echo "error: $TARGET exists but is not a symlink; remove it by hand." >&2
        exit 1
    else
        echo "Nothing installed at $TARGET"
    fi
    exit 0
fi

mkdir -p "$PLUGIN_DIR"

if [ -L "$TARGET" ]; then
    rm "$TARGET"
elif [ -e "$TARGET" ]; then
    echo "error: $TARGET exists and is not a symlink." >&2
    echo "Move it aside, then re-run this script." >&2
    exit 1
fi

ln -s "$SRC" "$TARGET"

echo "Installed kicad-lcsc-suite"
echo "  source : $SRC"
echo "  target : $TARGET"
echo
echo "Restart KiCad, then open the PCB editor and use"
echo "  Tools -> External Plugins -> LCSC Suite"
