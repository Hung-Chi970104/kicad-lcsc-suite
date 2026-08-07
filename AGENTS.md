# Agent guide — kicad-lcsc-suite

Orientation for coding agents (Claude Code, Codex, …). Read this first, then
jump to [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the code fits
together and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for how to build,
test and verify changes.

`CLAUDE.md` holds the two non-negotiable rules (Python 3.9, ruff-clean).
They are repeated below because breaking either one ships a plugin that
does not load.

---

## What this repo is

A **KiCad plugin**, not a library or a service. It is a fork of
`Bouni/kicad-jlcpcb-tools` with `uPesy/easyeda2kicad.py` vendored in
(`kicad_lcsc_suite/lib/easyeda2kicad/`) and an LCSC Explorer added on top.
Upstream commits are pinned in `UPSTREAM.txt`.

`install.sh` symlinks both halves into KiCad's plugin directories, so edits are
live after a KiCad restart. There is no build step.

Entry points:

| File | Role |
|---|---|
| [`kicad_lcsc_suite/__init__.py`](kicad_lcsc_suite/__init__.py) | Adds `lib/` to `sys.path`, and registers the toolbar action **only when a real pcbnew is importable** — so importing the package for its logic modules does not drag in the whole wx UI. |
| [`kicad_lcsc_suite/plugin.py`](kicad_lcsc_suite/plugin.py) | `pcbnew.ActionPlugin` subclass; toolbar entry "LCSC Suite" → opens `JLCPCBTools`. |
| [`kicad_lcsc_suite/__main__.py`](kicad_lcsc_suite/__main__.py) | Standalone mode — runs the wx UI outside KiCad against `standalone_impl.KicadStub`. |
| [`lcsc_suite/__main__.py`](lcsc_suite/__main__.py) | The Qt app. `--fixture` runs it against `fixtures/board.json` with no KiCad at all. |

## Hard rules

0. **A migration is underway.** The UI is moving out-of-process to PySide6 on
   KiCad 10's IPC API — see
   [docs/QT_MIGRATION_PLAN.md](docs/QT_MIGRATION_PLAN.md). Rules 1 and 3 below
   are scoped to the **legacy wx plugin** and do not constrain `lcsc_suite/`.
   Gerber and drill generation are **out of scope** and being removed; BOM and
   CPL stay.
1. **Python 3.9 — legacy wx plugin only.** KiCad bundles 3.9 (verified:
   3.9.13 on macOS, still true on **KiCad 10.0.3**). In the wx plugin use
   `Optional[X]`, `Dict[...]`, `List[...]` — never `X | None`, never
   `match`/`case`, never `typing.Self`/`TypeAlias`/`ParamSpec`.
   `pyproject.toml` pins `UP006/UP007/UP035/UP038/UP045` **off** so ruff
   cannot rewrite these into 3.10-only syntax. Do not re-enable them while the
   wx plugin lives.
   *Exceptions:* `db_build/` (GitHub Actions, ≥3.10) and `lcsc_suite/` (own
   venv, 3.12+).
2. **ruff-clean commits.** `ruff check` and `ruff format --check` must pass.
   When editing a file, reformat only the lines you intend to change.
3. **Dependencies: legacy plugin none, new app declared.** The *wx plugin*
   must run on a bare KiCad install — nothing the user has to `pip install`.
   Available there: the stdlib, `wx` (incl. `wx.svg`), `pcbnew`, and what
   KiCad ships (`requests`, `certifi`), plus the vendored
   `kicad_lcsc_suite/lib/easyeda2kicad/`. Existing `lcsc/` code sticks to
   `urllib` + stdlib.

   The *new app* **does** require a one-time setup step, and that is a product
   decision, not an oversight: `install.sh` / `install.ps1` bootstrap a `.venv`
   and pip-install `PySide6` and `kicad-python`. Users gain a UI that behaves
   identically on macOS and Windows in exchange for running the installer once.
   A PyInstaller freeze replaces the venv before any public release; because
   `runtime.type` is `exec`, that swap touches only `kicad_plugin/run.sh`.
   Add further dependencies deliberately — every one ships to users. The venv's
   contents are pinned in `install.sh`'s `APP_REQUIREMENTS`.

   `pyproject.toml`'s `dependencies` list belongs to the `db_build` tooling
   (including its `common/` library), and to neither half of the plugin.
4. **Never edit `kicad_lcsc_suite/lib/`.** It is vendored upstream code,
   excluded from ruff and pre-commit. Changes belong upstream or in a wrapper.
5. **AGPL boundary.** `kicad_lcsc_suite/lib/easyeda2kicad/` is AGPL-3.0; the
   rest is MIT. See the licensing section of [README.md](README.md) before
   making the repo public or cutting a release.

## Where things live

Two application packages, one per half of the migration. **Which one you are in
decides the interpreter, the toolkit and the verification tool** — see the table
in [CLAUDE.md](CLAUDE.md).

```text
lcsc_suite/            THE NEW APP — out-of-process PySide6, own venv (3.12+)
  kicad_bridge.py      the only module that touches KiCad; closes all three IPC traps
  shared.py            THE ONLY WAY IN to kicad_lcsc_suite's logic modules
  parts.py             board ↔ project database ↔ displayed rows, reconciled
  config.py            settings in the per-user config dir, imported once from the wx plugin
  app.py               QApplication bootstrap (Fusion + palette + font)
  ui/                  the widgets; ui/theme.py is the Qt port of lcsc/theme.py
  fixtures/board.json  a 110-footprint board for the probe, CI and the bridge tests

kicad_lcsc_suite/      THE LEGACY PLUGIN — in-process wx, KiCad's bundled 3.9.
                       Also holds the logic and assets both halves share, until
                       the Phase 8 cutover promotes the survivors into lcsc_suite/.
  mainwindow.py        JLCPCBTools — the main dialog; owns Library, Store, Fabrication
  lcsc/                the LCSC Explorer feature (this fork's addition)
    api.py             JLC assembly + LCSC retail + JLC search; StockReport, SearchHit,
                       the EasyEDA retail fallback and _HostBreaker
    details.py         per-part details from the API (replaces the bulk-DB lookup)
    explorer.py        LcscExplorerDialog — search, facets, detail pane, import/assign
    facetfilter.py     multi-select parametric filter (ComboCtrl + CheckListBox popup)
    importer.py        EasyEDA -> KiCad library + sym-lib-table/fp-lib-table registration
    photoviewer.py     full-size product photos; opened by clicking a thumbnail
    previewpanel.py    symbol/footprint (wx.svg) and product-photo tiles
    theme.py           light/dark aware colours; use this, never hard-code a wx.Colour
  library.py           optional bulk parts DB, the API part cache, corrections, mappings
  store.py             per-project SQLite state (<board dir>/jlcpcb/project.db)
  schematicexport.py   writes the assigned LCSC numbers into the .kicad_sch symbols
  schematicimport.py   reads them back out; no wx, no pcbnew, so it is unit-testable
  fabrication.py       Gerber/Excellon/BOM/CPL generation
  datamodel.py         wx.dataview models for the part list and the part selector
  settings.py          the settings dialog
  corrections.py       rotation/offset correction manager dialog
  partmapper.py        footprint→LCSC mapping manager dialog
  helpers.py           PLUGIN_PATH, scaling, icons, dark-mode detection, natural sort
  events.py            worker thread → UI; dispatches to Qt or wx, so both halves use it
  bom_estimation/      pure pricing/estimation logic + view formatting (no wx in pricing.py)
  enrichment/          per-part metadata providers (assembly process lookups)
  dblib/               the bulk parts-DB format definitions, shared with db_build/
  icons/               the icon set, shared with the Qt app via shared.LEGACY_ROOT
  lib/                 VENDORED — easyeda2kicad (AGPL) and packaging. Do not edit.
  VERSION              read by helpers.py at runtime; PCM rewrites it on release

kicad_plugin/          what gets installed into KiCad's plugins/ dir (the Qt app)
  plugin.json          the IPC API manifest; runtime.type = exec
  run.sh / run.cmd     launcher; unsets PYTHONHOME (trap 1) then runs the venv Python
db_build/              GitHub Action DB conversion (Python ≥3.10, not plugin code)
  common/              its parts-DB build & translation library
scripts/qt_probe.py    renders any Qt screen offscreen to docs/screens/*.png
scripts/gui_probe.py   the same job for the wx dialogs, under KiCad's Python
tests/                 every test, for both halves — the only pytest testpath
```

Full subsystem walkthrough: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Importing across the halves** goes one way only, and through one door:
`lcsc_suite.shared` imports from `kicad_lcsc_suite`, never the reverse, and
nothing adds a `sys.path` entry of its own. Tests reach both by name because
[tests/conftest.py](tests/conftest.py) puts the repository root on the path.

## Navigation shortcuts

- **A UI control behaves wrong** → find its handler in
  [`mainwindow.py`](kicad_lcsc_suite/mainwindow.py) (methods are grouped by feature; the
  toolbar handlers start around [`select_part`](kicad_lcsc_suite/mainwindow.py#L1412)) or in
  [`lcsc/explorer.py`](kicad_lcsc_suite/lcsc/explorer.py).
- **Stock numbers / warnings** → [`lcsc/api.py`](kicad_lcsc_suite/lcsc/api.py):
  `stock_report`, `_build_warnings`, `StockReport`.
- **A column or the thumbnails have gone blank** → check reachability before
  the UI. LCSC 403s whole networks, taking every `*.lcsc.com` host with it;
  JLCPCB's are unaffected. Retail stock falls through to EasyEDA
  (`api.retail_stock`) and photos come from JLC's file service
  (`api.jlc_image_url`) for exactly this reason — see
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §3. A one-line check:
  `curl -o /dev/null -w '%{http_code}\n' https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=C1592`
- **Search results or parametric filters** → `jlc_search`, `build_facets`,
  `filter_hits` in [`lcsc/api.py`](kicad_lcsc_suite/lcsc/api.py); the widget is
  [`lcsc/facetfilter.py`](kicad_lcsc_suite/lcsc/facetfilter.py). Filtering is **OR within an
  attribute, AND across attributes**.
- **Type / JLC Stock / LCSC Params columns, or BOM prices** →
  [`lcsc/details.py`](kicad_lcsc_suite/lcsc/details.py) builds the detail mapping;
  `Library.get_part_details` resolves cache → optional bulk DB → `{}` and never
  blocks; `mainwindow.start_part_detail_refresh` fills the cache off-thread,
  and `force=True` (what selecting a row does) refetches past the TTL.
  The column reports **JLC assembly** stock, never LCSC retail — hence its
  name.
  Which endpoint owns which field is **not** arbitrary — see
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §3 before changing a source.
- **Imported library lands in the wrong place** →
  [`lcsc/importer.py`](kicad_lcsc_suite/lcsc/importer.py): `LcscImporter.import_part`,
  `register_libraries`, `_ensure_lib_table_entry`.
- **LCSC numbers missing from the Symbol Fields Table** →
  [`schematicexport.py`](kicad_lcsc_suite/schematicexport.py) and
  `mainwindow.sync_schematic`. Assignment writes the *footprint*; the export
  writes the *symbol*. It refuses to touch a schematic that eeschema has
  open (`~<name>.kicad_sch.lck`) and never clears a number it was not told
  to — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §7.
- **A board that looks unassigned while the schematic has every number** →
  the opposite direction, [`schematicimport.py`](kicad_lcsc_suite/schematicimport.py) and
  `mainwindow.import_from_schematic`. Neither direction is automatic; both
  are toolbar buttons that show a per-reference diff and overwrite the other
  side only once confirmed.
- **Gerber/BOM/CPL output** → [`fabrication.py`](kicad_lcsc_suite/fabrication.py); rotation
  and offset fixes are `fix_rotation` / `fix_position`.
- **Part list columns/colours** → [`datamodel.py`](kicad_lcsc_suite/datamodel.py) plus
  [`dataview_highlight.py`](kicad_lcsc_suite/dataview_highlight.py) for match highlighting.
- **A setting** → the key lives in `settings.json` (gitignored, written to
  `PLUGIN_PATH`); the dialog is [`settings.py`](kicad_lcsc_suite/settings.py); defaults and
  migrations are in `JLCPCBTools.load_settings`
  ([mainwindow.py:1365](kicad_lcsc_suite/mainwindow.py#L1365)).

## Invariants worth knowing before you edit

**Threading.** Every network call runs on a worker thread. Results reach the
UI in exactly one of two ways, and mixing them up is a crash:

- `wx.PostEvent(self.parent, SomeEvent(...))` with events from
  [`events.py`](kicad_lcsc_suite/events.py) — used by `library.py` and `mainwindow.py`.
- `wx.CallAfter(self._handler, token, ...)` — used throughout
  `lcsc/explorer.py`.

Workers must never touch `store`, `library` or any widget directly.

**Staleness tokens.** `LcscExplorerDialog` keeps `_search_token`,
`_detail_token`, `_retail_token`; `_cancel_pending()` bumps all three so
in-flight workers drop their results. `mainwindow` uses
`assembly_enrichment_generation` for the same purpose. Any new async fetch
needs the same guard, plus an `_alive()` check — a modeless dialog can be
destroyed while a fetch is in flight, and a `CallAfter` landing on a deleted
C++ object raises `RuntimeError` inside wx's event loop.

**wx assertions are fatal here.** KiCad's bundled wxWidgets has assertions
enabled and wxPython raises them as `wx._core.wxAssertionError`, so one bad
call aborts `_build_ui()` part-way and the user gets a blank or missing
window with no error. Known trigger: `wx.ALIGN_CENTER_VERTICAL` on a
**vertical** `BoxSizer` (and the horizontal mirror). Audit alignment flags
against sizer orientation whenever you add UI.

**Column widths don't stick before the window is shown.** Native DataView
discards widths set during construction. `LcscExplorerDialog` restates them
in `_on_first_shown`, deferred via `wx.CallAfter`.

**Dark mode.** KiCad follows the desktop appearance. Colours must come from
[`lcsc/theme.py`](kicad_lcsc_suite/lcsc/theme.py) (`colour()`, `stock_colour()`,
`card_background()`), not literals — a value tuned on white turns to mud on
dark.

**Import order in standalone probes.** Import plugin modules *before*
creating `wx.App`; `__init__.py` calls `JLCPCBPlugin().register()`, which
asserts if a `wx.App` already exists outside KiCad.

**Degrade, never crash.** Every storefront endpoint here is unofficial and may
403, rate-limit or change shape. Failures render as `?` / `…` and log; they
must not raise into the event loop. `?` means "nobody answered" and `0` means
"confirmed none" — never let one render as the other. Any new endpoint goes
through `_get_json`/`fetch_image` so `_HostBreaker` can stop a doomed fill
after three failures instead of after a hundred.

**Posting from a worker.** Use `self._post(handler, *args)`, not a bare
`wx.CallAfter`. `wx.CallAfter` raises on the *worker* thread once the dialog
is gone — nothing catches it there and it lands as a traceback in KiCad's
console. `_post` checks `_alive()`, swallows the teardown race, and returns
`False` so the loop can stop pulling work it cannot deliver.

## Working here

```bash
python3 -m venv .venv && .venv/bin/pip install pytest ruff   # not preinstalled
.venv/bin/python -m pytest        # testpaths: tests/, common/, dblib/
.venv/bin/ruff check && .venv/bin/ruff format --check
```

Verify GUI changes **with a screenshot**, in both halves.

*New Qt app* — renders offscreen, no display and no permissions needed:

```bash
./install.sh --app                              # bootstraps .venv once
.venv/bin/python scripts/qt_probe.py --all      # writes docs/screens/*.png
```

Commit the updated PNG in the same commit as the UI change. A geometry dump
(`--geometry`) is a supplement, never a substitute — mistaking one for the
other is the reason this migration exists.

*Legacy wx plugin* — build the dialog against a stub parent with **KiCad's own
interpreter**, because that is the wx build whose assertions matter:

```bash
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 scripts/gui_probe.py explorer
```

wx windows can be captured offscreen too (`wx.WindowDC` → `wx.Bitmap`), and
`screencapture` does work on the current dev machine. But a macOS screenshot of
a **wx** window says nothing about Windows, because wx wraps native controls —
which is precisely the limitation Qt fixes. Recipe in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#verifying-gui-changes-headlessly).

Check reachability before blaming the UI — LCSC 403s whole networks:

```bash
curl -o /dev/null -w '%{http_code}\n' 'https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=C1592'
```

## Things that look like bugs but aren't

- `pyproject.toml` says `requires-python = ">=3.10"` and lists `requests`,
  `click`, … — that is the **db_build tooling's** metadata. The wx plugin is
  still 3.9 and dependency-free.
- `.gitignore` does not ignore `lib/`, contradicting upstream. Deliberate:
  this fork vendors code in `kicad_lcsc_suite/lib/`.
- `jlcpcb/` in the working tree holds the downloaded databases (~750 MB) and
  the small `partcache.db`, and is gitignored. Deleting the bulk DB does **not**
  trigger a re-download any more — it is optional, and the Download toolbar
  button is the only thing that fetches it.
- `search_escape.py`, `partselector_columns.py` and
  `datamodel.PartSelectorDataModel` have no callers. They are the remains of
  upstream's part selector, which the LCSC Explorer replaced. Left in place to
  keep the upstream diff small; delete them only as a deliberate cleanup.
- `settings.json` at the repo root is runtime state, gitignored via
  `/settings.json`.
- The project name is still `Kicad-jlcpcb-tools` in `pyproject.toml` and the
  main window title still says "JLCPCB Tools"; only the plugin's toolbar
  entry was renamed to "LCSC Suite" so it can coexist with upstream.
