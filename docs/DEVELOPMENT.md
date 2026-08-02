# Development

How to set up, test, lint, run and debug this plugin. Companion to
[../AGENTS.md](../AGENTS.md) (rules and navigation) and
[ARCHITECTURE.md](ARCHITECTURE.md) (how the code fits together).

Every command below was run against this checkout; the noted baselines are
what you should actually see.

---

## 1. Two interpreters, and which is which

This trips people up constantly. You need both.

| | Interpreter | Used for |
|---|---|---|
| **Dev** | any Python ≥3.10 venv | `pytest`, `ruff`, the `db_build`/`common` tooling |
| **Runtime** | KiCad's bundled Python **3.9** | anything that imports `wx` or `pcbnew`, and every GUI probe |

KiCad's Python:

```bash
# macOS
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
# Windows
"C:\Program Files\KiCad\10.0\bin\python.exe"
# Linux — usually the system python3
```

It ships `wx` 4.2.2a1 (with `wx.svg`), `pcbnew`, `requests` and `certifi`.
It does **not** ship `pytest` or `ruff`, and you should not add them to it.

Plugin code must be 3.9-compatible. Note the nuance: PEP 585 builtin
generics (`list[str]`, `dict[str, int]`, `tuple[int, int]`) **do** work on
3.9 and are used freely. What does not work is PEP 604 unions — `str | None`
raises `TypeError` at runtime. Use `Optional[str]`, or add
`from __future__ import annotations` at the top of the module, which is how
`partselector_columns.py`, `dataview_highlight.py` and `lcsc/api.py` get away
with modern annotation syntax.

## 2. Setup

Neither `pytest` nor `ruff` is preinstalled on this machine.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"      # pulls cachetools, requests, tqdm, … + pytest + ruff
.venv/bin/pip install "ruff==0.14.14"  # match the pre-commit pin — see §4
```

`.venv/` is gitignored. The editable install is only for the test suite's
sake: the *plugin* still has no installable dependencies, and adding one is
a rule violation ([AGENTS.md](../AGENTS.md#hard-rules)).

## 3. Tests

```bash
.venv/bin/python -m pytest          # 311 passed in ~4s
.venv/bin/python -m pytest -q common/test_bom_estimator.py
.venv/bin/python -m pytest -q -k "stock or retail"
```

`testpaths` is `["tests", "common", "dblib"]` (pyproject.toml) — note that
**most tests live next to the code in `common/`**, not in `tests/`.
`common/conftest.py` puts the repo root on `sys.path` so test modules can
import `bom_estimation`, `enrichment`, etc. without a path preamble.

Everything under test is deliberately wx-free: `bom_estimation/pricing.py`,
`common/translate.py`, `dataview_highlight.py`, `fabrication.split_bom_designators`
and friends. `events.py` falls back to a dummy event factory when wx is
absent, which is what keeps the importing modules testable.

**There is no automated coverage of the wx dialogs.** That is what §5 is for.

## 4. Lint

```bash
.venv/bin/ruff check --extend-exclude=lib
.venv/bin/ruff format --check --exclude lib
```

Three things you need to know:

**Always pass the `lib` exclusion.** Plain `ruff check` walks the vendored
`lib/` and reports **3445 errors**. `.pre-commit-config.yaml` passes
`--extend-exclude=lib` / `exclude: '(^|/)lib'`; do the same by hand or you
will drown in noise from code you must not touch.

**Pin ruff to 0.14.14**, the version in `.pre-commit-config.yaml`. Newer
ruff (0.16.x) adds `PLR0917` and flags two pre-existing files
(`common/test_componentdb.py`, `db_build/jlcparts_db_convert.py`), plus it
reformats differently. Chasing those is churn.

**`ruff format --check` is not clean at HEAD.** Four untouched upstream
files would be reformatted: `corrections.py`, `events.py`, `kicad_drc.py`,
`partdetails.py`. **Leave them alone.** `CLAUDE.md`'s "only reformat lines
you are intentionally changing" exists precisely to keep the fork's diff
against upstream readable. `ruff check` *does* pass clean at HEAD — that is
the gate to keep green.

Pre-commit is configured (ruff, pyupgrade, markdownlint) if you want it:

```bash
.venv/bin/pip install pre-commit && .venv/bin/pre-commit run --all-files
```

## 5. Verifying GUI changes headlessly

Layout bugs are invisible in a diff and do not need KiCad running.
[`scripts/gui_probe.py`](../scripts/gui_probe.py) builds a dialog against a
stub parent, lets the event loop settle, then dumps the widget tree and
DataView column widths.

```bash
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 scripts/gui_probe.py explorer
```

Expected tail:

```text
--- results columns ---
   0 (unnamed)          width=112   cell=114   hidden=False
   1 Part               width=212   cell=212   hidden=False
   2 Description        width=411   cell=411   hidden=False
   3 Manufacturer / Package width=175 cell=175  hidden=False
   ...
  realised row height: 140px
  initial                row_extent=1425  client=1450  indent=16 overhead=17 header=33
OK: explorer built and torn down without wx assertions
```

Column 0 is the 108px product thumbnail and has no header text. The 140px rows
stack related metadata so the same width shows more useful context: model over
LCSC/type, manufacturer over package, and price over minimum order.

Four things in that output are assertions, not decoration, and each one is a
bug that looks like "works fine" in a diff:

- `cell=` must equal `width=`. It is what the column's renderer asks to paint
  into, and on macOS it is what it gets — a renderer reporting less wraps and
  clips its text inside a box narrower than the column.
- `row_extent` must not exceed `client`. The native control adds `indent` before
  the first cell and `overhead` to every column, so widths that add up to the
  client width still overflow it into a horizontal scrollbar.
- `--- catalogue cell text ---` wraps a real LCSC description at the current
  column width and fails if it does not fit in five lines.
- `--- detail pane blocks ---` measures each block of the detail pane in both
  layouts and fails if one was squeezed or pushed outside the pane.

The probe also switches selected-part details between the side panel and an
inline expanded row, scrolls the grid out from under the open inline row (it has
to clip, not disappear), and double-clicks a result (which must assign the LCSC
number and close the window, never import to disk).

Three targets and the flags that matter:

```bash
# multi-select facets, offline: injects synthetic hits, then opens and
# dismisses every popup — ComboPopup.Create runs lazily on first show, so a
# probe that never opens one proves nothing about whether wx can host it
… scripts/gui_probe.py explorer --offline-facets

# part-list row colours: which attention state wins, and in what colour
… scripts/gui_probe.py partlist

# the main window itself, against the standalone stubs in a throwaway
# project: the settings dialog builds, both schematic buttons are reachable
# and write/clear/read/refuse-when-locked, and the window is found once
… scripts/gui_probe.py mainwindow
```

Expected `mainwindow` tail:

```text
--- main window ---
  name=kicad_lcsc_suite_main_window
  found_by_lookup=True
--- schematic buttons ---
  From schematic   enabled=True
  To schematic     enabled=True
  upper_toolbar    width: needs=797 has=1290
  right_toolbar    height: needs=462 has=481
--- to schematic ---
  assigned -> synced=True fields=['C25741']
  removed  -> fields=['']
  locked   -> synced=False fields=['']
--- from schematic ---
  read     -> Read 1 symbol(s), 1 with an LCSC number, from probe.kicad_sch
  diff     -> added=[('R1', 'C25741')] replaced=[]
  imported -> refs=['R1'] store=C25741
--- single window ---
  closed -> lookup_returns_none=True schematic=['C25741']
OK: mainwindow built and torn down without wx assertions
```

Three lines carry the weight:

- `locked -> synced=False` — with a `~probe.kicad_sch.lck` beside it the
  schematic must come back **unchanged**, because eeschema holds its own copy
  and would overwrite anything written there.
- `needs=… has=…` — a `wx.ToolBar` does not scroll, so a tool past the end of
  the space its sizer gives it is simply not on screen. This is how the
  schematic button spent a release invisible at the bottom of the right-hand
  toolbar (`needs=508 has=481`).
- `closed -> schematic=[…]` unchanged from the last explicit write — closing
  the window must not sync anything on its own.

Expected `partlist` tail — the amber/red split is the whole point, and no
highlight at all on a part that is excluded from the BOM:

```text
  R1   lcsc=C25741  bom=in   highlighted=True  colour=rgb(240, 160, 96)
  R2   lcsc=(none)  bom=in   highlighted=True  colour=rgb(255, 130, 130)
  H1   lcsc=(none)  bom=out  highlighted=False colour=-
```

Other flags: `--keyword 22k` (seeds a live search), `--settle-ms 2000` (let
async fetches land), `--project-path <dir>`.

What this catches that reading code does not: squeezed sizer panes, DataView
columns that silently collapse to zero, `wx.CallAfter` callbacks landing on a
destroyed window, and the `wxAssertionError` that aborts `_build_ui()`
part-way and leaves a blank window with no error message.

Two constraints baked into the script, worth understanding before you write
your own probe:

- **Import plugin modules before creating `wx.App`.** `__init__.py` calls
  `JLCPCBPlugin().register()`, which asserts on `PgmOrNull()` outside KiCad.
- **The checkout is not importable by its own name** — `kicad-lcsc-suite`
  has hyphens. The probe imports through the installed symlink
  (`~/Documents/KiCad/<ver>/scripting/plugins/kicad_lcsc_suite`), falling
  back to aliasing the directory into `sys.modules`.

`screencapture` requires Screen Recording permission this environment does
not have. **Assert on geometry and state, never on screenshots.**

## 6. Running the real thing

**In KiCad.** `install.sh` symlinks the checkout in, so there is no
reinstall step — edit, restart KiCad, then
**PCB editor → Tools → External Plugins → LCSC Suite**.

```bash
./install.sh              # newest KiCad found
./install.sh --list       # show detection, change nothing
./install.sh --uninstall
```

KiCad caches the plugin module *and* library tables at startup, so a restart
is needed for both code changes and freshly imported libraries.

**Standalone**, outside KiCad, against `standalone_impl.KicadStub`:

```bash
cd ~/Documents/KiCad/10.0/scripting/plugins
/Applications/KiCad/.../bin/python3 -m kicad_lcsc_suite
```

The stub implements only the `pcbnew` surface actually used. If you reach
for a new `pcbnew` call, extend `standalone_impl.py` in the same commit or
standalone mode breaks silently.

## 7. Environment diagnostics

```bash
/Applications/KiCad/.../bin/python3 selfcheck.py            # includes network probes
/Applications/KiCad/.../bin/python3 selfcheck.py --offline  # skip them
```

Reports interpreter version, wx / `wx.svg` / `wx.dataview`, `pcbnew`, TLS
trust, the vendored converter, and live reachability of both storefronts.
Exits nonzero if something the plugin needs is missing. Run this first when
a bug report says "stock shows `?`" or "previews are blank".

## 8. Databases while developing

| Want | Do |
|---|---|
| Drop the optional bulk parts DB | `rm jlcpcb/*parts*.db` — no re-download follows; use the Download button |
| Force the API part cache to refetch | `rm jlcpcb/partcache.db`, or `update part_cache set fetched_at = 0` |
| Inspect what the API resolved for a part | `sqlite3 jlcpcb/partcache.db 'select * from part_cache where lcsc = "C15849"'` |
| Reset one board's plugin state | delete `<board dir>/jlcpcb/project.db` |
| Inspect assignments | `sqlite3 <board dir>/jlcpcb/project.db 'select * from part_info'` |
| Reset plugin settings | delete `settings.json` at the repo root |
| Undo a lib-table edit | restore the `*.lcsc-suite.bak` the importer wrote |

Adding a per-part field? Extend `Store.PART_INFO_ESTIMATOR_COLUMNS` —
`ensure_part_info_columns` `ALTER TABLE`s it in on open. Never write a
destructive migration; these files live in users' board directories.

## 9. Debugging

Logging goes to the main window's log panel via `LogBoxHandler`
([mainwindow.py:2123](../mainwindow.py#L2123)), so `logging.getLogger(__name__)`
output is visible in the UI. Every module already has a `self.logger`.

Common failure signatures:

| Symptom | Look at |
|---|---|
| Dialog opens blank or half-built | a `wxAssertionError` aborted `_build_ui()` — check sizer alignment flags against orientation |
| `RuntimeError` deep in the wx event loop | a `wx.CallAfter` landed on a destroyed window — missing `_alive()` guard |
| Stale results overwrite fresh ones | missing staleness-token check (`_search_token` / `_detail_token` / `_retail_token` / `assembly_enrichment_generation`) |
| Stock shows `?` | TLS trust — `selfcheck.py`, or set `LCSC_CA_BUNDLE` |
| Retail column stuck on `…` | LCSC detail endpoint unreachable or rate-limiting; **Refresh** retries |
| Plugin missing from the menu | directory name has hyphens, or KiCad was not restarted |
| Import not in the symbol chooser | KiCad caches lib-tables at startup — restart |
| Column widths collapse | widths set before the window was shown; restate in `_on_first_shown` |

## 10. Before you commit

```bash
.venv/bin/ruff check --extend-exclude=lib          # must pass clean
.venv/bin/ruff format --check --exclude lib        # only your files; 4 are dirty at HEAD
.venv/bin/python -m pytest                         # 311 passed
```

Plus, if you touched a dialog:

```bash
/Applications/KiCad/.../bin/python3 scripts/gui_probe.py explorer
```

And check the 3.9 floor on anything new — no `X | None` without
`from __future__ import annotations`, no `match`/`case`.
