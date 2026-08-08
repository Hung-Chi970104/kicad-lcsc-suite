#!/usr/bin/env bash
# Install kicad-lcsc-suite into KiCad's plugin directories (macOS / Linux).
#
# One half now. The Phase 8 cutover removed the in-process wxPython plugin and
# with it `--wx`; what installs is the out-of-process PySide6 application, which
# bootstraps a virtualenv, pip-installs PySide6 + kicad-python, and registers an
# IPC API plugin in <kicad>/<ver>/plugins/lcsc_suite so KiCad shows a toolbar
# button. See docs/QT_MIGRATION_PLAN.md.
#
#   ./install.sh                 install into the newest KiCad found
#   ./install.sh 10.0            target a specific KiCad version
#   ./install.sh --dir <path>    target an explicit KiCad version directory
#   ./install.sh --uninstall     remove whatever this script installed
#   ./install.sh --list          just show what would be detected
#
# `--app` is still accepted and does nothing, so a script or a note that carries
# it keeps working. `--wx` is refused with a message rather than ignored: it
# used to install something, and silently installing nothing is the worse
# answer.
#
# NOTE: this adds a one-time setup step (a virtualenv) that the wx plugin never
# had. That is a deliberate product decision recorded in the migration plan's
# §8: users gain a UI that behaves the same on macOS and Windows in exchange for
# running this script once. KiCad 10 or newer is required — the IPC API this
# talks to does not exist before it.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_LINK_NAME="lcsc_suite"
# Where the wx plugin used to be linked. Kept so --uninstall can still clean up
# after an install made before the cutover; nothing writes it any more.
LEGACY_WX_LINK_NAME="kicad_lcsc_suite"
VENV="$SRC/.venv"
# Pinned rather than floating: the IPC API is young and its wire format has
# changed between KiCad 10.x point releases. A smoke test that fails loudly on
# drift is better than a UI that half works.
APP_REQUIREMENTS=("PySide6>=6.7,<7" "kicad-python>=0.4,<1")
MIN_PYTHON="3.12"

UNINSTALL=0
LIST_ONLY=0
VERSION=""
EXPLICIT_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --app)       ;;  # accepted and a no-op: there is only one half now
        --wx)        echo "The wx plugin was removed at the Phase 8 cutover." >&2
                     echo "See docs/QT_MIGRATION_PLAN.md; this installs the Qt app." >&2
                     exit 2 ;;
        --uninstall) UNINSTALL=1 ;;
        --list)      LIST_ONLY=1 ;;
        --dir)       shift; EXPLICIT_DIR="${1:-}" ;;
        -h|--help)   sed -n '2,25p' "$0"; exit 0 ;;
        *)           VERSION="$1" ;;
    esac
    shift
done

# KiCad's user data directory differs per platform. Check every plausible base
# rather than assuming, so this works on macOS, Linux and WSL.
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

# Portable "highest version directory" — avoids `sort -V`, which is absent on
# some BSD/macOS sort builds.
newest_version_dir() {
    find "$1" -maxdepth 1 -mindepth 1 -type d 2>/dev/null \
        | while read -r d; do basename "$d"; done \
        | grep -E '^[0-9]+\.[0-9]+$' \
        | sort -t. -k1,1n -k2,2n \
        | tail -1
}

resolve_kicad_dir() {
    local base version
    for base in "${BASES[@]}"; do
        [ -d "$base" ] || continue
        if [ -n "$VERSION" ]; then
            version="$VERSION"
        else
            version="$(newest_version_dir "$base")"
        fi
        [ -n "$version" ] || continue
        echo "$base/$version"
        return 0
    done
    return 1
}

if [ -n "$EXPLICIT_DIR" ]; then
    KICAD_DIR="$EXPLICIT_DIR"
elif ! KICAD_DIR="$(resolve_kicad_dir)"; then
    echo "error: could not find a KiCad user directory. Looked in:" >&2
    printf '  %s\n' "${BASES[@]}" >&2
    echo >&2
    echo "Open KiCad once so it creates its directories, then re-run." >&2
    echo "Or pass one explicitly:  ./install.sh --dir /path/to/KiCad/10.0" >&2
    exit 1
fi

WX_DIR="$KICAD_DIR/scripting/plugins"
APP_DIR="$KICAD_DIR/plugins"
WX_TARGET="$WX_DIR/$LEGACY_WX_LINK_NAME"
APP_TARGET="$APP_DIR/$APP_LINK_NAME"

describe() {
    local target="$1"
    if [ -L "$target" ]; then
        echo "installed -> $(readlink "$target")"
    elif [ -e "$target" ]; then
        echo "occupied by a non-symlink"
    else
        echo "not installed"
    fi
}

if [ "$LIST_ONLY" -eq 1 ]; then
    echo "source      : $SRC"
    echo "KiCad dir   : $KICAD_DIR"
    echo "app target  : $APP_TARGET"
    echo "  status    : $(describe "$APP_TARGET")"
    echo "venv        : $VENV"
    if [ -x "$VENV/bin/python" ]; then
        echo "  python    : $("$VENV/bin/python" --version 2>&1)"
        echo "  PySide6   : $("$VENV/bin/python" -c 'import PySide6;print(PySide6.__version__)' 2>&1 || true)"
    else
        echo "  python    : absent"
    fi
    echo "wx target   : $WX_TARGET (removed at the cutover)"
    echo "  status    : $(describe "$WX_TARGET")"
    exit 0
fi

link_or_die() {
    local target="$1" source="$2"
    mkdir -p "$(dirname "$target")"
    if [ -L "$target" ]; then
        rm "$target"
    elif [ -e "$target" ]; then
        echo "error: $target exists and is not a symlink." >&2
        echo "Move it aside, then re-run this script." >&2
        exit 1
    fi
    ln -s "$source" "$target"
}

unlink_if_ours() {
    local target="$1"
    if [ -L "$target" ]; then
        rm "$target"
        echo "Removed $target"
    elif [ -e "$target" ]; then
        echo "warning: $target exists but is not a symlink; left alone." >&2
    else
        echo "Nothing installed at $target"
    fi
}

if [ "$UNINSTALL" -eq 1 ]; then
    unlink_if_ours "$APP_TARGET"
    # Unconditional: an install predating the cutover left a link here.
    unlink_if_ours "$WX_TARGET"
    echo
    echo "The virtualenv at $VENV was left in place; delete it by hand if you"
    echo "want it gone."
    exit 0
fi

# --- the new Qt app --------------------------------------------------------

find_python() {
    # PySide6 needs >= 3.10 and this app targets 3.12+. Prefer the newest
    # interpreter available rather than whatever `python3` happens to be.
    local candidate
    for candidate in python3.14 python3.13 python3.12 python3; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

check_api_server() {
    # KiCad ships with the IPC API server *off*, and the app cannot connect
    # without it. A clear instruction here beats a silent failure to connect.
    local prefs
    case "$(uname -s)" in
        Darwin) prefs="$HOME/Library/Preferences/kicad/$(basename "$KICAD_DIR")/kicad_common.json" ;;
        *)      prefs="${XDG_CONFIG_HOME:-$HOME/.config}/kicad/$(basename "$KICAD_DIR")/kicad_common.json" ;;
    esac
    if [ ! -f "$prefs" ]; then
        echo "note: could not find $prefs to check the API server setting."
        echo "      Make sure Preferences -> Plugins -> Enable KiCad API is ticked."
        return
    fi
    if grep -q '"enable_server"[[:space:]]*:[[:space:]]*true' "$prefs"; then
        echo "KiCad API server : enabled"
    else
        echo
        echo "!! KiCad's API server is DISABLED. The LCSC Suite button will not"
        echo "!! be able to reach your board until you enable it:"
        echo "!!"
        echo "!!     KiCad -> Preferences -> Plugins -> Enable KiCad API"
        echo "!!"
        echo "!! ($prefs)"
        echo
    fi
}

if [ ! -x "$VENV/bin/python" ]; then
    if ! PYTHON="$(find_python)"; then
        echo "error: no Python >= $MIN_PYTHON found." >&2
        echo "PySide6 needs at least 3.10, and this app targets $MIN_PYTHON+." >&2
        echo "KiCad's own bundled Python (3.9) cannot run it — that is the" >&2
        echo "whole reason the app runs out of process." >&2
        exit 1
    fi
    echo "Creating virtualenv at $VENV using $PYTHON"
    "$PYTHON" -m venv "$VENV"
fi
echo "Installing app dependencies"
"$VENV/bin/python" -m pip install --upgrade --quiet pip
"$VENV/bin/python" -m pip install --quiet "${APP_REQUIREMENTS[@]}"

chmod +x "$SRC/kicad_plugin/run.sh"
link_or_die "$APP_TARGET" "$SRC/kicad_plugin"

echo "Installed the LCSC Suite app"
echo "  source : $SRC/kicad_plugin"
echo "  target : $APP_TARGET"
echo "  python : $("$VENV/bin/python" --version 2>&1)"
check_api_server

# --- the legacy wx plugin, if one is still linked ---------------------------
#
# Not installed any more — removed. But an install from before the cutover left
# a symlink pointing at a directory that no longer exists, and KiCad logs an
# import error for it on every start. Clearing it is the one thing this script
# still has to say about the wx half.

if [ -L "$WX_TARGET" ]; then
    unlink_if_ours "$WX_TARGET"
    echo "Removed the old wx plugin link"
    echo "  target : $WX_TARGET"
fi

echo
echo "Restart KiCad. In the PCB editor you will find:"
echo "  the 'LCSC Suite' toolbar button (the new Qt app)"
