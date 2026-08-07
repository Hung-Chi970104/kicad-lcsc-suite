# kicad-lcsc-suite project instructions

**Start with [AGENTS.md](AGENTS.md)** — repo orientation, where everything
lives, and the invariants that bite. Then
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the subsystems fit
together and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for setup, tests,
lint and headless GUI verification.

The two rules below are inherited from upstream and are non-negotiable.

## Code quality

All commits must be ruff-clean. Run `ruff check` and `ruff format --check` before committing.
When making changes to a file, only reformat lines you are intentionally changing.

Pass the vendored-code exclusion or you get 3445 errors from
`kicad_lcsc_suite/lib/`:

```bash
ruff check --extend-exclude=lib
ruff format --check --exclude lib
```

Use `ruff==0.14.14`, matching `.pre-commit-config.yaml`. `ruff check` passes
clean at HEAD; `ruff format --check` does **not** — four untouched upstream
files (`corrections.py`, `events.py`, `kicad_drc.py`, `partdetails.py`, all
under `kicad_lcsc_suite/`) would be reformatted. Leave them alone.

## Migration in progress

The UI is being rewritten as an out-of-process **PySide6** app driven over
KiCad 10's IPC API. Read **[docs/QT_MIGRATION_PLAN.md](docs/QT_MIGRATION_PLAN.md)**
before touching UI code — it carries the screen inventory, the phase order, and
four IPC traps that each cost real time to find.

Which rules apply depends on which half you are in:

| | Legacy wx plugin (`kicad_lcsc_suite/`) | New app (`lcsc_suite/`) |
|---|---|---|
| Interpreter | KiCad's bundled **Python 3.9** | own venv, **3.12+** |
| Toolkit | wxPython | PySide6 (Fusion style) |
| Verify with | `scripts/gui_probe.py` | `scripts/qt_probe.py` |

`lcsc_suite/shared.py` is the only sanctioned route from the new app into the
old package's logic modules. Imports go that way and no other; nothing adds a
`sys.path` entry of its own.

Keep the legacy plugin working until the plan's Phase 8 cutover.

## Python version compatibility

**Legacy wx plugin only** — that is, `kicad_lcsc_suite/`. KiCad's internal
Python interpreter is **Python 3.9**, so anything the wx plugin imports must be
3.9-compatible.

- Use `Optional[X]` instead of `X | None`
- No `match`/`case` statements
- No use of `typing.Self`, `typing.TypeAlias`, `typing.ParamSpec`, or other 3.10+ additions

PEP 585 builtin generics (`list[str]`, `dict[str, int]`) *are* fine on 3.9.
PEP 604 unions are not, unless the module opens with
`from __future__ import annotations`.

This does **not** apply to `lcsc_suite/` (own interpreter), `db_build/`
(GitHub Action), or shared logic modules once nothing on 3.9 imports them.
Note that most of `kicad_lcsc_suite/` *is* shared logic the Qt app imports, so
it stays on 3.9 until the Phase 8 cutover.

## Verifying UI changes

**Never claim a UI change works without looking at a screenshot.** Geometry
dumps miss what users see; that mistake is the reason this migration exists.

New Qt app — renders with no display, no permissions, works in CI:

```bash
.venv/bin/python scripts/qt_probe.py explorer   # writes docs/screens/explorer.png
```

Commit the updated PNG in the same commit as the UI change, so the diff shows
the visual change.

Legacy wx dialogs — KiCad's wxWidgets raises assertions as Python exceptions,
and one bad sizer flag aborts a dialog mid-build with no error message:

```bash
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 scripts/gui_probe.py explorer
```

wx windows can also be screenshotted offscreen (`wx.WindowDC` → `wx.Bitmap`),
and `screencapture` works on macOS. Note that a macOS screenshot of a **wx**
window says nothing about Windows, because wx wraps native controls — that
limitation is precisely what Qt fixes.

## Writing to the board over IPC

`board.update_items(field)` returns success and **silently changes nothing**.
Writes must target the parent footprint. Always assert by re-reading the
board — a clean return value proves nothing.

**Re-read only after `push_commit`.** An open commit is invisible to a read, so
verifying between `begin_commit()` and `push_commit()` compares against the old
state and makes every write look like it failed. Push, then verify, then put the
previous values back if it did not land. See the plan's §2 for all four traps.
