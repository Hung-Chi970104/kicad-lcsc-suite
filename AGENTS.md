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

A **KiCad action plugin**, not a library or a service. It is a fork of
`Bouni/kicad-jlcpcb-tools` with `uPesy/easyeda2kicad.py` vendored in
(`lib/easyeda2kicad/`) and an LCSC Explorer added on top. Upstream commits
are pinned in `UPSTREAM.txt`.

The repository root *is* the Python package: `install.sh` symlinks this
checkout into KiCad's plugin directory as `kicad_lcsc_suite`, so edits are
live after a KiCad restart. There is no build step.

Entry points:

| File | Role |
|---|---|
| [`__init__.py`](__init__.py) | Adds `lib/` to `sys.path`, registers the plugin. Import failures are swallowed so tests can import the package without KiCad. |
| [`plugin.py`](plugin.py) | `pcbnew.ActionPlugin` subclass; toolbar entry "LCSC Suite" → opens `JLCPCBTools`. |
| [`__main__.py`](__main__.py) | Standalone mode — runs the UI outside KiCad against `standalone_impl.KicadStub`. |
| [`selfcheck.py`](selfcheck.py) | CLI environment diagnostic (interpreter, wx, TLS, endpoints). |

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
   `lib/easyeda2kicad/`. Existing `lcsc/` code sticks to `urllib` + stdlib.
   The *new app* runs in its own venv and may depend on `PySide6` and
   `kicad-python`; add anything further deliberately, since every dependency
   ships to users. `pyproject.toml`'s `dependencies` list belongs to the
   `db_build`/`common` tooling, *not* to the wx plugin.
4. **Never edit `lib/`.** It is vendored upstream code, excluded from ruff
   and pre-commit. Changes belong upstream or in a wrapper.
5. **AGPL boundary.** `lib/easyeda2kicad/` is AGPL-3.0; the rest is MIT. See
   the licensing section of [README-LCSC-SUITE.md](README-LCSC-SUITE.md)
   before making the repo public or cutting a release.

## Where things live

```text
mainwindow.py          JLCPCBTools — the main dialog; owns Library, Store, Fabrication
lcsc/                  the LCSC Explorer feature (this fork's addition)
  api.py               JLC assembly + LCSC retail + JLC search; StockReport, SearchHit,
                       the EasyEDA retail fallback and _HostBreaker
  details.py           per-part details from the API (replaces the bulk-DB lookup)
  explorer.py          LcscExplorerDialog — search, facets, detail pane, import/assign
  facetfilter.py       multi-select parametric filter (ComboCtrl + CheckListBox popup)
  importer.py          EasyEDA -> KiCad library + sym-lib-table/fp-lib-table registration
  photoviewer.py       full-size product photos; opened by clicking a thumbnail
  previewpanel.py      symbol/footprint (wx.svg) and product-photo tiles
  theme.py             light/dark aware colours; use this, never hard-code a wx.Colour
library.py             optional bulk parts DB, the API part cache, corrections, mappings
store.py               per-project SQLite state (<board dir>/jlcpcb/project.db)
schematicexport.py     writes the assigned LCSC numbers into the .kicad_sch symbols
schematicimport.py     reads them back out; no wx, no pcbnew, so it is unit-testable
fabrication.py         Gerber/Excellon/BOM/CPL generation
datamodel.py           wx.dataview models for the part list and the part selector
settings.py            the settings dialog
corrections.py         rotation/offset correction manager dialog
partmapper.py          footprint→LCSC mapping manager dialog
helpers.py             PLUGIN_PATH, scaling, icons, dark-mode detection, natural sort
events.py              all custom wx events (worker thread → UI)
bom_estimation/        pure pricing/estimation logic + view formatting (no wx in pricing.py)
enrichment/            per-part metadata providers (assembly process lookups)
common/                parts-DB build & translation tooling + the bulk of the test suite
db_build/              GitHub Action DB conversion (Python ≥3.10, not plugin code)
lib/                   VENDORED — easyeda2kicad (AGPL) and packaging. Do not edit.
tests/, common/, dblib/  pytest testpaths (see pyproject.toml)
```

Full subsystem walkthrough: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Navigation shortcuts

- **A UI control behaves wrong** → find its handler in
  [`mainwindow.py`](mainwindow.py) (methods are grouped by feature; the
  toolbar handlers start around [`select_part`](mainwindow.py#L1412)) or in
  [`lcsc/explorer.py`](lcsc/explorer.py).
- **Stock numbers / warnings** → [`lcsc/api.py`](lcsc/api.py):
  `stock_report`, `_build_warnings`, `StockReport`.
- **A column or the thumbnails have gone blank** → check reachability before
  the UI. LCSC 403s whole networks, taking every `*.lcsc.com` host with it;
  JLCPCB's are unaffected. Retail stock falls through to EasyEDA
  (`api.retail_stock`) and photos come from JLC's file service
  (`api.jlc_image_url`) for exactly this reason — see
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §3. A one-line check:
  `curl -o /dev/null -w '%{http_code}\n' https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=C1592`
- **Search results or parametric filters** → `jlc_search`, `build_facets`,
  `filter_hits` in [`lcsc/api.py`](lcsc/api.py); the widget is
  [`lcsc/facetfilter.py`](lcsc/facetfilter.py). Filtering is **OR within an
  attribute, AND across attributes**.
- **Type / JLC Stock / LCSC Params columns, or BOM prices** →
  [`lcsc/details.py`](lcsc/details.py) builds the detail mapping;
  `Library.get_part_details` resolves cache → optional bulk DB → `{}` and never
  blocks; `mainwindow.start_part_detail_refresh` fills the cache off-thread,
  and `force=True` (what selecting a row does) refetches past the TTL.
  The column reports **JLC assembly** stock, never LCSC retail — hence its
  name.
  Which endpoint owns which field is **not** arbitrary — see
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §3 before changing a source.
- **Imported library lands in the wrong place** →
  [`lcsc/importer.py`](lcsc/importer.py): `LcscImporter.import_part`,
  `register_libraries`, `_ensure_lib_table_entry`.
- **LCSC numbers missing from the Symbol Fields Table** →
  [`schematicexport.py`](schematicexport.py) and
  `mainwindow.sync_schematic`. Assignment writes the *footprint*; the export
  writes the *symbol*. It refuses to touch a schematic that eeschema has
  open (`~<name>.kicad_sch.lck`) and never clears a number it was not told
  to — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §7.
- **A board that looks unassigned while the schematic has every number** →
  the opposite direction, [`schematicimport.py`](schematicimport.py) and
  `mainwindow.import_from_schematic`. Neither direction is automatic; both
  are toolbar buttons that show a per-reference diff and overwrite the other
  side only once confirmed.
- **Gerber/BOM/CPL output** → [`fabrication.py`](fabrication.py); rotation
  and offset fixes are `fix_rotation` / `fix_position`.
- **Part list columns/colours** → [`datamodel.py`](datamodel.py) plus
  [`dataview_highlight.py`](dataview_highlight.py) for match highlighting.
- **A setting** → the key lives in `settings.json` (gitignored, written to
  `PLUGIN_PATH`); the dialog is [`settings.py`](settings.py); defaults and
  migrations are in `JLCPCBTools.load_settings`
  ([mainwindow.py:1365](mainwindow.py#L1365)).

## Invariants worth knowing before you edit

**Threading.** Every network call runs on a worker thread. Results reach the
UI in exactly one of two ways, and mixing them up is a crash:

- `wx.PostEvent(self.parent, SomeEvent(...))` with events from
  [`events.py`](events.py) — used by `library.py` and `mainwindow.py`.
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
[`lcsc/theme.py`](lcsc/theme.py) (`colour()`, `stock_colour()`,
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

Verify GUI changes **with KiCad's own interpreter**, headlessly — build the
dialog against a stub parent, drive it with `wx.CallLater`, and assert on
geometry. `screencapture` is unavailable. Recipe in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#verifying-gui-changes-headlessly).

Check the environment a change assumes:

```bash
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 selfcheck.py --offline
```

## Things that look like bugs but aren't

- `pyproject.toml` says `requires-python = ">=3.10"` and lists `requests`,
  `click`, … — that is the **db_build/common tooling** metadata inherited
  from upstream. Plugin code is still 3.9 and dependency-free.
- `.gitignore` does not ignore `lib/`, contradicting upstream. Deliberate:
  this fork vendors code there.
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
