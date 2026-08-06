#!/bin/sh
# Launch the LCSC Suite app for KiCad's "exec" plugin runtime.
#
# TRAP 1 — the single most confusing first-day failure available.
#
# KiCad hands its *own* interpreter's environment down to exec plugins. A venv
# Python started with KiCad's PYTHONHOME dies instantly with
#
#     ModuleNotFoundError: No module named 'encodings'
#
# before it can run a line of ours, and KiCad reports nothing at all — the
# toolbar button simply does nothing. Clearing these four variables is what
# lets the app bring its own Python.
unset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE PYTHONSTARTUP

# Resolve this script's real location, so the checkout can live anywhere.
# `readlink -f` is not portable to macOS's BSD userland, hence the loop — and
# the loop alone is not enough either: install.sh symlinks the *directory*, not
# the script, so `$0` is a real file inside a symlinked parent. `cd -P` +
# `pwd -P` is what resolves that, and getting it wrong points REPO_ROOT at
# KiCad's plugins folder, where there is no venv.
target="$0"
while [ -L "$target" ]; do
    link="$(readlink "$target")"
    case "$link" in
        /*) target="$link" ;;
        *)  target="$(dirname "$target")/$link" ;;
    esac
done
PLUGIN_DIR="$(cd -P "$(dirname "$target")" && pwd -P)"
REPO_ROOT="$(cd -P "$PLUGIN_DIR/.." && pwd -P)"

VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/lcsc-suite"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/plugin.log"

if [ ! -x "$VENV_PYTHON" ]; then
    {
        echo "--- $(date) ---"
        echo "No app virtualenv at $VENV_PYTHON."
        echo "Run ./install.sh in $REPO_ROOT to create it."
    } >>"$LOG"
    exit 1
fi

# The app is not pip-installed into the venv (the checkout *is* the source, the
# same arrangement the wx plugin uses), so point Python at it explicitly rather
# than relying on the working directory KiCad happens to hand us. This is set
# *after* the unset above, on purpose: we are replacing KiCad's value, not
# keeping it.
PYTHONPATH="$REPO_ROOT"
export PYTHONPATH

# stdout and stderr are the only channel there is: KiCad discards both, so a
# traceback during start-up is invisible unless it lands in a file.
{
    echo "--- $(date) --- launching LCSC Suite"
    exec "$VENV_PYTHON" -m lcsc_suite "$@"
} >>"$LOG" 2>&1
