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

Pass the vendored-code exclusion or you get 3445 errors from `lib/`:

```bash
ruff check --extend-exclude=lib
ruff format --check --exclude lib
```

Use `ruff==0.14.14`, matching `.pre-commit-config.yaml`. `ruff check` passes
clean at HEAD; `ruff format --check` does **not** — four untouched upstream
files (`corrections.py`, `events.py`, `kicad_drc.py`, `partdetails.py`) would
be reformatted. Leave them alone.

## Python version compatibility

KiCad's internal Python interpreter is **Python 3.9**. All code that is executed in the
plugin must be 3.9-compatible.

- Use `Optional[X]` instead of `X | None`
- No `match`/`case` statements
- No use of `typing.Self`, `typing.TypeAlias`, `typing.ParamSpec`, or other 3.10+ additions

PEP 585 builtin generics (`list[str]`, `dict[str, int]`) *are* fine on 3.9.
PEP 604 unions are not, unless the module opens with
`from __future__ import annotations`.

Note that files in the db_build directory use python >= 3.10, that is executed as a
github action and is not subject to the Python 3.9 guidance.

## Verifying UI changes

wx dialogs have no automated coverage, and KiCad's wxWidgets raises
assertions as Python exceptions — one bad sizer flag aborts a dialog
mid-build with no error message. Probe dialogs headlessly with KiCad's own
interpreter before claiming a UI change works:

```bash
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 scripts/gui_probe.py explorer
```
