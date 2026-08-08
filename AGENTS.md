# Agent guide — kicad-lcsc-suite

Orientation for coding agents (Claude Code, Codex, …). Read this first, then
jump to [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the code fits
together and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for how to build,
test and verify changes.

`CLAUDE.md` holds the rules that ship a broken plugin if you break them.

---

## What this repo is

A **KiCad 10 plugin**, not a library or a service. It is a fork of
`Bouni/kicad-jlcpcb-tools` with `uPesy/easyeda2kicad.py` vendored in
(`lcsc_suite/lib/easyeda2kicad/`) and an LCSC Explorer added on top. Upstream
commits are pinned in `UPSTREAM.txt`.

The UI runs **out of process**: a PySide6 application in its own virtualenv,
talking to KiCad over the IPC API. KiCad launches it from a manifest whose
`runtime.type` is `exec`, so it brings its own Python and KiCad does not care
which one. The in-process wxPython plugin it replaced was removed at the
Phase 8 cutover; see [docs/QT_MIGRATION_PLAN.md](docs/QT_MIGRATION_PLAN.md).

`install.sh` bootstraps the venv and symlinks `kicad_plugin/` into KiCad's
plugin directory, so edits are live after a KiCad restart. There is no build
step.

Entry points:

| File | Role |
|---|---|
| [`kicad_plugin/plugin.json`](kicad_plugin/plugin.json) | the IPC manifest KiCad reads; declares the toolbar button |
| [`kicad_plugin/run.sh`](kicad_plugin/run.sh) | the launcher. **Unsets `PYTHONHOME`** (trap 1) then runs the venv Python |
| [`lcsc_suite/__main__.py`](lcsc_suite/__main__.py) | the app. `--fixture PATH` runs it against a JSON board with no KiCad at all |

## Hard rules

1. **ruff-clean commits.** `ruff check --extend-exclude=lib` and
   `ruff format --check --exclude lib` must both pass. Both are clean at HEAD.
   When editing a file, reformat only the lines you intend to change.
2. **Never edit `lcsc_suite/lib/`.** Vendored upstream code, excluded from ruff
   and pre-commit. Changes belong upstream or in a wrapper.
3. **AGPL boundary.** `lcsc_suite/lib/easyeda2kicad/` is AGPL-3.0; the rest is
   MIT. See the licensing section of [README.md](README.md) before making the
   repo public or cutting a release.
4. **Dependencies are declared and they ship.** The app requires a one-time
   setup step — `install.sh` builds a `.venv` and pip-installs `PySide6` and
   `kicad-python`, pinned in `APP_REQUIREMENTS`. That is a deliberate product
   decision: users get a UI that behaves the same on macOS and Windows in
   exchange for running the installer once. Add further dependencies
   deliberately. `pyproject.toml`'s `dependencies` list belongs to the
   `db_build` tooling, not to the app.
5. **`lcsc/api.py` is copied, not edited.** If a UI need seems to require an API
   change, change the UI. §4 of the migration plan says why at length.

Two rules that used to be here are **gone**, and both were consequences of
running inside KiCad's interpreter: Python 3.9 syntax compatibility, and "no
runtime dependencies — must run on a bare KiCad install".

## Where things live

```text
lcsc_suite/            THE APPLICATION — out-of-process PySide6, own venv (3.12+)
  kicad_bridge.py      the only module that touches KiCad; closes all four IPC traps
  controller.py        SuiteController — the window reports, this decides and writes
  parts.py             board <-> project database <-> displayed rows, reconciled
  search_source.py     where the Explorer's data comes from: live, or the fixture
  undo.py              the app's own undo; KiCad's cannot reach the project database
  config.py            settings and the database directory, both per-user
  app.py               QApplication bootstrap (Fusion + palette + font)
  shared.py            names the toolkit-free logic layer; import through it
  ui/                  the widgets; ui/theme.py owns every colour
    explorer/          the LCSC Explorer — window, results, facets, detail, preview, tasks
    photo_viewer.py    full-size product photos, retargetable while open
  fixtures/board.json  a 110-footprint board for the probe, CI and the bridge tests
  fixtures/explorer/   one captured search (raw payloads + thumbnails); see below

  --- the logic layer: no toolkit, and mostly older than the migration ---
  lcsc/api.py          JLC assembly + LCSC retail + JLC search; StockReport, SearchHit,
                       the EasyEDA retail fallback and _HostBreaker
  lcsc/details.py      per-part details from the API
  lcsc/importer.py     EasyEDA -> KiCad library + sym-lib-table/fp-lib-table registration
  library.py           optional bulk parts DB, the API part cache, corrections, mappings
  store.py             per-project SQLite state (<board dir>/jlcpcb/project.db)
  fab_rules.py         the BOM/CPL rules: corrections, rotation, offsets, grouping
  schematicexport.py   writes the assigned LCSC numbers into the .kicad_sch symbols
  schematicimport.py   reads them back out
  derive_params.py     the LCSC Params column
  highlight_terms.py   which spellings count as the same part (390R is 390Ω)
  bom_estimation/      pricing and estimation logic + view formatting
  dblib/               the bulk parts-DB format definitions, shared with db_build/
  icons/               55 PNGs, recoloured for dark mode by ui/icons.py
  lib/                 VENDORED — easyeda2kicad (AGPL) and packaging. Do not edit.

kicad_plugin/          what gets symlinked into KiCad's plugins/ dir
db_build/              GitHub Action DB conversion (not plugin code)
scripts/qt_probe.py    renders any screen offscreen to docs/screens/*.png
scripts/compare_geometry.py  the cross-platform layout gate; CI runs it on Windows
scripts/live_ipc_check.py    proves the bridge's writes against a running KiCad
scripts/capture_explorer_fixture.py  ONE SHOT, run by hand. Spends live requests
tests/                 every test — the only pytest testpath
docs/screens/          committed PNGs, plus geometry.txt and wx/ (see below)
```

Full subsystem walkthrough: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**`docs/screens/wx/` holds six screenshots of the plugin that is gone.** They
are the Phase 8 parity evidence and the only picture of the wx UI that ever
existed — §5 of the plan describes it in prose and no capture was committed at
the time. They do not re-render; nothing can produce them any more.

**The app never touches the network in a probe or a test.**
`search_source.build_source()` defaults to the live endpoints; the probe and the
tests pass `FixtureSource`, which primes `api.py`'s own cache with one captured
search and installs a host breaker that refuses everything else. Same shape as
`Library(allow_network=False)`. The capture is *raw payloads*, replayed through
`api.py`'s real parsers — never stored `SearchHit` objects.

## Navigation shortcuts

- **A UI control behaves wrong** → the widget is under
  [`lcsc_suite/ui/`](lcsc_suite/ui/); what it *does* is in
  [`controller.py`](lcsc_suite/controller.py). The split is one line: the window
  builds, displays and reports; the controller decides and writes.
- **Stock numbers / warnings** → [`lcsc/api.py`](lcsc_suite/lcsc/api.py):
  `stock_report`, `_build_warnings`, `StockReport`.
- **A column or the thumbnails have gone blank** → check reachability before
  the UI. LCSC 403s whole networks, taking every `*.lcsc.com` host with it;
  JLCPCB's are unaffected. Retail stock falls through to EasyEDA and photos come
  from JLC's file service for exactly this reason. A one-line check:
  `curl -o /dev/null -w '%{http_code}\n' https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=C1592`
- **Search results or parametric filters** → `jlc_search`, `build_facets`,
  `filter_hits` in [`lcsc/api.py`](lcsc_suite/lcsc/api.py); the widget is
  [`ui/explorer/facets.py`](lcsc_suite/ui/explorer/facets.py). Filtering is
  **OR within an attribute, AND across attributes**.
- **Type / JLC Stock / LCSC Params columns** →
  [`lcsc/details.py`](lcsc_suite/lcsc/details.py) builds the detail mapping;
  `Library.get_part_details` resolves cache → optional bulk DB → `{}` and never
  blocks. The column reports **JLC assembly** stock, never LCSC retail.
- **BOM/CPL output** → [`fab_rules.py`](lcsc_suite/fab_rules.py) for the rules,
  [`export.py`](lcsc_suite/export.py) for where a position and an angle come
  from.
- **LCSC numbers missing from the Symbol Fields Table** →
  [`schematicexport.py`](lcsc_suite/schematicexport.py). Assignment writes the
  *footprint*; the export writes the *symbol*. It refuses to touch a schematic
  eeschema has open and never clears a number it was not told to.
- **A board that looks unassigned while the schematic has every number** →
  [`schematicimport.py`](lcsc_suite/schematicimport.py). Neither direction is
  automatic; both are toolbar buttons that show a per-reference diff and
  overwrite the other side only once confirmed.
- **A setting, or where the databases are** →
  [`config.py`](lcsc_suite/config.py). Both live in per-user directories, and
  `adopt_data_directory` explains why deriving either from a module's location
  is a bug that has already bitten twice.

## Invariants worth knowing before you edit

**Threading.** Every network call runs on a worker thread and results reach the
UI through a **queued Qt signal**, never by touching a widget directly. Workers
must never touch `store`, `library` or any widget.

**Staleness tokens.** Qt severs a connection to a destroyed receiver, so "the
window is gone" needs no guard. "These results are for the previous search"
still does — `ui/explorer/tasks.py` keeps the tokens, and any new async fetch
needs one.

**The board is written through `kicad_bridge` and nowhere else**, and every
write proves itself by re-reading after the commit is pushed. Four IPC traps
make that non-negotiable; the plan's §2 lists them.

**Dark mode.** Colours come from [`ui/theme.py`](lcsc_suite/ui/theme.py), never
literals. A value tuned on white turns to mud on dark.

**Degrade, never crash.** Every storefront endpoint here is unofficial and may
403, rate-limit or change shape. Failures render as `?` / `…` and log; they must
not raise into the event loop. **`?` means "nobody answered" and `0` means
"confirmed none"** — never let one render as the other. Any new endpoint goes
through `_get_json`/`fetch_image` so `_HostBreaker` can stop a doomed fill after
three failures instead of after a hundred.

## Working here

```bash
./install.sh                              # bootstraps .venv, links the plugin
.venv/bin/python -m pytest -q             # 789 tests
.venv/bin/ruff check --extend-exclude=lib && .venv/bin/ruff format --check --exclude lib
```

Every test file must also pass **on its own**:

```bash
for f in tests/test_*.py; do .venv/bin/python -m pytest -q "$f" >/dev/null || echo "FAILS ALONE: $f"; done
```

Verify GUI changes **with a screenshot**:

```bash
.venv/bin/python scripts/qt_probe.py --all --theme both   # writes docs/screens/*.png
```

Commit the updated PNGs in the same commit as the UI change. A geometry dump
(`--geometry`) is a supplement, never a substitute — mistaking one for the other
is the reason this migration happened.

*Board writes* — a screenshot says nothing about whether the board changed, and
the fixture cannot find every way the real API differs from it. After touching
`kicad_bridge.py`, open a **copy** of a board in KiCad and run:

```bash
.venv/bin/python scripts/live_ipc_check.py     # refuses non-disposable boards
```

Phase 3 found trap 4 this way, after two phases of green fixture tests.

## Things that look like bugs but aren't

- `pyproject.toml` says `requires-python = ">=3.10"` and lists `requests`,
  `click`, … — that is the **db_build tooling's** metadata, not the app's.
- `pyproject.toml` still pins `UP006/UP007/UP035/UP045` off. They were off for
  KiCad's Python 3.9, which nothing here runs in any more; turning them back on
  is a deliberate, separate change, not a drive-by.
- `.gitignore` does not ignore `lib/`, contradicting upstream. Deliberate: this
  fork vendors code in `lcsc_suite/lib/`.
- `jlcpcb/` in the working tree holds a downloaded parts database (~750 MB) and
  is gitignored. It is optional — the Download toolbar button is the only thing
  that fetches it.
- The project name is still `kicad-lcsc-suite` in `pyproject.toml` while the
  PCM identifier is `com.lcscsuite.plugin`. Different namespaces.
- `docs/CODE-REVIEW.md` names paths under `kicad_lcsc_suite/` that no longer
  exist. Deliberate: it records a review of a stated baseline, and rewriting its
  paths would misrepresent what was reviewed.
