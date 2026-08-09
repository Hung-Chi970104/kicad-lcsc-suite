# kicad-lcsc-suite project instructions

**Start with [AGENTS.md](AGENTS.md)** — repo orientation, where everything
lives, and the invariants that bite. Then
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the subsystems fit
together and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for setup, tests,
lint and headless GUI verification.

## Code quality

All commits must be ruff-clean. Run both before committing:

```bash
ruff check --extend-exclude=lib
ruff format --check --exclude lib
```

The exclusion is not optional — without it you get 3160 errors out of the
vendored `lcsc_suite/lib/packaging/`. Use `ruff==0.14.14`, matching
`.pre-commit-config.yaml`.

Both commands pass clean at HEAD. They did not before Phase 8: four upstream
files (`corrections.py`, `events.py`, `kicad_drc.py`, `partdetails.py`) were
permanently listed as "would reformat, leave them alone", and all four went
with the wx plugin. If `ruff format --check` reports anything now, it is yours.

When making changes to a file, only reformat lines you are intentionally
changing.

## One application, one interpreter

The migration is **done**. The UI is an out-of-process **PySide6** app driven
over KiCad 10's IPC API, and the in-process wxPython plugin is gone as of the
Phase 8 cutover — read
**[docs/QT_MIGRATION_PLAN.md](docs/QT_MIGRATION_PLAN.md)** §10 for what that
moved and what it settled, and §2 for the four IPC traps that each cost real
time to find.

| | The app (`lcsc_suite/`) |
|---|---|
| Interpreter | own venv, **3.12+** |
| Toolkit | PySide6 (Fusion style) |
| KiCad | 10.0 or newer, API server enabled |
| Verify with | `scripts/qt_probe.py` |

Two rules that used to constrain this repo and **no longer apply**: Python 3.9
compatibility (`Optional[X]` over `X | None`, no `match`/`case`) and "no runtime
dependencies". Both belonged to KiCad's bundled interpreter, which nothing here
runs in any more. `pyproject.toml` still pins `UP006/UP007/UP035/UP045` off;
turning them back on is a deliberate, separate change.

`lcsc_suite/shared.py` names the toolkit-free logic layer — `store`, `library`,
`lcsc/api.py`, the BOM rules — and importing through it keeps that boundary
visible. `lcsc/api.py` is **copied, not edited**: if a UI need seems to require
an API change, change the UI.

## Verifying UI changes

**Never claim a UI change works without looking at a screenshot.** Geometry
dumps miss what users see; that mistake is the reason this migration happened.

```bash
.venv/bin/python scripts/qt_probe.py --all --theme both   # docs/screens/*.png
```

Commit the updated PNGs in the same commit as the UI change, so the diff shows
the visual change. `--geometry` is a supplement, never a substitute.

Cross-platform layout is a **CI gate**, not a promise:

```bash
.venv/bin/python scripts/qt_probe.py --all --theme both --geometry-out mine.txt
.venv/bin/python scripts/compare_geometry.py docs/screens/geometry.txt mine.txt
```

`docs/screens/geometry.txt` is the committed macOS reference; the `windows` job
in `.github/workflows/qt-screens.yml` renders on `windows-latest` and compares
against it. It gates on the widget tree, on the size of every window that
states one, and on collapse — **not** on pixel equality, because the app pins a
font size and not a font family. `scripts/compare_geometry.py`'s docstring has
the measurements behind that decision.

## Writing to the board over IPC

`board.update_items(field)` returns success and **silently changes nothing**.
Writes must target the parent footprint. Always assert by re-reading the
board — a clean return value proves nothing.

**Re-read only after `push_commit`.** An open commit is invisible to a read, so
verifying between `begin_commit()` and `push_commit()` compares against the old
state and makes every write look like it failed. Push, then verify, then put the
previous values back if it did not land. See the plan's §2 for all four traps.

Re-run `scripts/live_ipc_check.py` against a **copy** of a real board whenever
`kicad_bridge.py` changes.
