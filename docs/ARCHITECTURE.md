# Architecture

How kicad-lcsc-suite is put together. Companion to [../AGENTS.md](../AGENTS.md)
(rules and navigation) and [DEVELOPMENT.md](DEVELOPMENT.md) (how to run and
test it).

---

## 1. The shape of the thing

This is a **KiCad plugin**, described here as it stands today: an in-process wx
action plugin, alongside the out-of-process PySide6 app that is replacing it
(see [QT_MIGRATION_PLAN.md](QT_MIGRATION_PLAN.md)). There is no server, no build
artifact and no installed copy — `install.sh` symlinks the two package
directories into KiCad's plugin directories, so the working tree *is* what runs.

```text
KiCad (pcbnew)
  └── imports kicad_lcsc_suite/__init__.py     -> adds lib/ to sys.path
        └── plugin.JLCPCBPlugin.register()      -> toolbar button "LCSC Suite"
              └── Run() -> mainwindow.JLCPCBTools(None)   [the whole wx UI]
```

Registration is guarded on a **real** `pcbnew` being importable, because the
same package is imported for its logic modules by the Qt app and by the tests,
and neither should pull in the wx dialog tree to reach `store.py`.

`Run()` fires on **every** toolbar click, so it first looks for a window that
is already up (`find_open_main_window`, matched on the dialog's wx name) and
raises that instead. Two instances would open the same project database and
the same board and quietly overwrite each other. The lookup goes through
`wx.GetTopLevelWindows()` rather than a module-level reference so that
KiCad's "Refresh Plugins", which re-imports this package, cannot orphan the
window it is trying to find.

`JLCPCBTools` is a single `wx.Dialog` that owns everything:

```text
JLCPCBTools (mainwindow.py, ~2100 lines)
├── self.pcbnew        KicadProvider -> real pcbnew, or standalone_impl.KicadStub
├── self.settings      dict loaded from PLUGIN_PATH/settings.json
├── self.library       Library      — the downloaded JLCPCB parts database
├── self.store         Store        — per-project SQLite state for this board
├── self.fabrication   Fabrication  — Gerber/Excellon/BOM/CPL writer
├── self.partlist_data_model   PartListDataModel (datamodel.py)
├── self.bom_estimator         BomEstimatorWidget/Controller (bom_widget.py)
└── self._part_selector        LcscExplorerDialog singleton (lcsc/explorer.py)
```

Everything else is a dialog launched from it (`settings.py`,
`corrections.py`, `partmapper.py`, `partdetails.py`, `lcsc/explorer.py`).

## 2. The two halves of the codebase

The fork has a clear seam, and it matters for how you approach a change.

Everything below lives under `kicad_lcsc_suite/` unless stated otherwise.

**Upstream (`Bouni/kicad-jlcpcb-tools`)** — the board-centric half.
`mainwindow.py`, `store.py`, `library.py`, `fabrication.py`, `datamodel.py`,
`settings.py`, `corrections.py`, `partmapper.py`, `partdetails.py`,
`schematicexport.py`. Style is older: broad `except`, wx idioms from the 3.x
era, module-level SQL. Keep diffs surgical here — the fork tracks upstream
via `UPSTREAM.txt` and gratuitous rewrites make future merges expensive.

**This fork (`lcsc/`)** — the part-selection half, written fresh. Typed,
docstring-heavy, stdlib-only, defensive about threads and teardown. New
feature work generally belongs here.

`dblib/`, `bom_estimation/`, `enrichment/` are the pure-logic packages, UI-free
by design — which is what lets the Qt app import them unchanged through
`lcsc_suite.shared`. The parts-database build tooling is `db_build/` (with its
own `common/` library) at the repository root, and is not plugin code. Every
test for any of it is in `tests/`.

**The third half is `lcsc_suite/`** — the PySide6 app. It imports *from* the
above and never the other way round.

## 3. Data sources — three, and they disagree

This is the domain fact the whole `lcsc/` package exists to express.

| Source | Endpoint | What it knows |
|---|---|---|
| **JLC assembly** | `cart.jlcpcb.com` | what the SMT service can place: assembly stock, library type (Basic/Preferred/Extended), MPQ, attrition, **product photo ids** |
| **LCSC retail** | `wmsc.lcsc.com` detail endpoint | what you can buy loose: retail stock, warehouse split, price ladder, parameters, photos |
| **EasyEDA product** | `easyeda.com/api/products/…` | the `szlcsc` block — LCSC retail stock, price and min buy, from a reachable host. **Retail fallback only** |
| **JLC parts library search** | JLC search API | keyword search + real per-part attributes (used for parametric facets); the only source of a `description` in the shape `derive_params` parses, of the `Basic`/`Extended` library type, and of a photo id per row |
| **Local parts DB** | `jlcpcb/*.db`, downloaded | **optional** offline catalogue (`library.py`) |

Assembly and retail stock are **different inventories** and routinely differ
by orders of magnitude. `lcsc/api.py::StockReport` carries both plus derived
warnings (`UNAVAILABLE`, `Assembly-blocked`, `Assembly-only`, divergence
factor) built in `_build_warnings`. Never collapse the two into one "stock"
number — that is the bug this fork was written to fix.

### Reachability, and what it costs

LCSC's `wmsc.lcsc.com` *bulk* endpoint returns 403 to anonymous clients, so
bulk search goes through the JLC parts library. Only the *detail* endpoint
ever worked anonymously, which is why retail stock is backfilled row by row
instead of arriving with the search results.

That endpoint is not dependable either: LCSC 403s **whole networks**, and when
it does, every `*.lcsc.com` host goes with it — the API, the product pages and
the `assets.lcsc.com` image CDN. JLCPCB's hosts are unaffected, so the design
rule is that **nothing user-visible may depend on `lcsc.com` alone**:

- **retail stock** falls through to EasyEDA's `szlcsc` block, the same
  warehouse figure from a different host (`api.retail_stock`). What EasyEDA
  does *not* carry is the parametric list, the price ladder or the
  domestic/overseas split, so the detail pane degrades rather than switching
  over wholesale.
- **product photos** come from JLC's file service,
  `jlcpcb.com/api/file/downloadByFileSystemAccessId/{id}`, addressed by the
  opaque ids the search and assembly payloads hand out (`api.jlc_image_url`).
  The search response carries one per row, so a grid of thumbnails costs no
  JSON lookups at all — only rows whose id is empty fall back to a per-part
  assembly lookup, which is where a photo filed under `imageList` turns up.
- **`_HostBreaker`** stops the retry storm. Three consecutive hard failures
  trip a host open for ten minutes, during which its requests fail locally at
  zero cost; one success closes it again. Without it, a 120-row fill against a
  blocked host is 120 round trips to learn the same fact 120 times — and on a
  rate-limiter (EasyEDA sits behind CloudFront and throttles), it is what
  turns a soft throttle into a ban. `api.clear_cache()` re-arms every host, so
  the Refresh button is also the "I fixed my connection" button.

A source answering nothing is **not** a part having no stock. `retail_stock`
returns `None` for the first and `0` for the second, and the grid renders them
as `?` and `0` — conflating them shows in-stock parts as unavailable.

The same distinction has to survive at *column* scale, which is what
`api.retail_unreachable()` is for: a hundred `?` cells and a "0 with LCSC retail
stock" status line are indistinguishable from a hundred genuinely dead parts.
When every retail source is tripped open, the explorer stops filling, leaves the
rows pending and says so in the status line. This is also why the
**Inventory** switch offers one warehouse at a time rather than both — see
`explorer.STOCK_VIEWS`: a combined view had to backfill retail for every row of
every search, which is precisely the burst that earns the ban.

**Which source owns which field** matters, and the overlaps are not
interchangeable. [`lcsc/details.py`](../kicad_lcsc_suite/lcsc/details.py) resolves the part
list's Type/Stock/LCSC Params columns and the estimator's prices, taking:

- **library type** from the *search* endpoint. The assembly endpoint spells it
  `base`/`expand` where the search endpoint and the bulk DB both say
  `Basic`/`Extended`, and `bom_estimation.pricing` compares it to `"Extended"`
  exactly — take the wrong one and every Extended part silently loses its
  per-reel feeder fee.
- **price ladder** from the *assembly* endpoint (`prices`, as
  `startNumber`/`productPrice`). That is the ladder the bulk DB carried and the
  one a JLC assembly order is billed at. LCSC retail prices are a different
  transaction and are a fallback only.
- **category** from the assembly endpoint's `firstTypeNameEn`, which already
  uses the bulk DB's coarse vocabulary. The search endpoint returns a
  second-category string instead ("Chip Resistor - Surface Mount"), which
  `details.canonical_category` maps back when the assembly detail is silent.

## 4. Databases

Five SQLite databases with distinct lifecycles. Confusing them is a common
source of "why did my change not persist".

| DB | Path | Owner | Lifetime |
|---|---|---|---|
| **Parts** | `jlcpcb/current-parts-fts5.db` (~750 MB) | `library.py` | **optional**, downloaded on demand, gitignored |
| **Part cache** | `jlcpcb/partcache.db` | `library.py` | API part details, one row per LCSC number, refreshed past `PART_CACHE_TTL_SECONDS` |
| **Project** | `<board dir>/jlcpcb/project.db` | `store.py` | per board; holds LCSC assignments, BOM/POS exclusions, stock, generation counter |
| **Corrections** | `jlcpcb/corrections.db` (global) or the project DB (local) | `library.py` | rotation/offset fixes, switchable global↔local |
| **Mappings** | `jlcpcb/mappings.db` | `library.py` | footprint+value → LCSC memory |

**The parts DB is not required.** `Library.get_part_details` resolves from the
part cache first, then the bulk DB if one is present, then gives up — and it
**never** touches the network, because it is called once per assigned part
while the footprint list is being built on the UI thread. Serving a *stale*
cache row unconditionally is deliberate: that is what makes an offline session
work, and a day-old stock figure beats a blank column. Filling and refreshing
the cache is `mainwindow.start_part_detail_refresh`'s job, on a worker thread,
paced at `PART_DETAIL_REQUEST_INTERVAL`.

Serving a stale row has a cost the TTL alone cannot pay off: JLC restocks a
common part by millions overnight, so a cached `0` can outlive its truth by
most of a day, and the column says nothing about its own age. **Selecting a
row therefore refetches it regardless of cache age** —
`start_part_detail_refresh(references, force=True)`, debounced by
`SELECTION_REFRESH_DELAY_MS` so arrow-keying down the list is one refresh and
not one per row, and rate-limited per part by
`SELECTION_REFRESH_COOLDOWN_SECONDS` (the API layer's own cache lifetime,
below which there is nothing new to learn). Two constraints on that path:

- it is capped at `SELECTION_REFRESH_MAX_PARTS` distinct LCSC numbers, so
  select-all is a bulk gesture rather than a request storm;
- a forced refresh **must not** bump `part_detail_generation`.
  `on_part_details_progress` discards any event whose generation is not the
  current one, so bumping it would mean one click during the startup sweep
  threw away every answer that sweep had left to deliver. Reassignment — the
  mutation the guard exists for — still bumps it.

`Library.has_bulk_database` records whether a catalogue is present. There is no
`UPDATE_NEEDED` state any more — an absent parts DB is a missing optional
extra, not a condition to be repaired before the window can be used, and it no
longer triggers an unrequested three-quarter-gigabyte download at start-up.

**Parts DB variants** are declared in [`dblib/__init__.py`](../kicad_lcsc_suite/dblib/__init__.py)
(`DatabaseConfig`): `current-parts-fts5.db` (default, excludes parts unstocked
>1 year), `parts-fts5.db` (all), `basic-parts-fts5.db`, and an empty one. The
user picks one in Settings; `Library.refresh_library_config` re-resolves paths
and re-runs `check_library`. Databases ship as split zip chunks and are
reassembled by [`unzip_parts.py`](../kicad_lcsc_suite/unzip_parts.py).

**`Library.search` is gone.** It was upstream's FTS5 full-text search, and the
LCSC Explorer's live API search replaced the part selector that called it. It
had no remaining callers. `search_escape.py`, `partselector_columns.py` and
`PartSelectorDataModel` are the leftovers of the same feature and are still
present but equally unreferenced — deliberately left alone to keep the upstream
diff small.

Bulk lookups now go through `Library.get_bulk_part_details`, which is FTS5 on a
single LCSC number and returns `{}` rather than raising when the file is absent
or half-written.

**Project DB migrations** are additive and idempotent:
`Store.ensure_part_info_columns` `ALTER TABLE`s in any missing estimator
column on open. Add new per-part fields there, never with a destructive
migration — users' boards carry these files.

## 5. Threading and the UI

Every network or disk-heavy operation runs off the UI thread. Two marshalling
mechanisms coexist; use the one the surrounding module uses.

**Custom wx events** ([`events.py`](../kicad_lcsc_suite/events.py)) — upstream's mechanism,
used by `library.py` (download/unzip progress) and `mainwindow.py`
(enrichment). Worker calls `wx.PostEvent(self.parent, SomeEvent(...))`; the
dialog binds a handler. `events.py` degrades to a dummy factory when wx is
absent so the logic modules stay importable under pytest.

**`wx.CallAfter`** — the `lcsc/` mechanism. Worker computes, then
`wx.CallAfter(self._handler, token, payload)`.

Three rules for anything asynchronous:

1. **Workers touch nothing shared.** No `store`, no `library`, no widgets.
   They compute and hand the result back. The enrichment worker's docstring
   in [`mainwindow.py`](../kicad_lcsc_suite/mainwindow.py#L992) states this explicitly.
2. **Every result carries a staleness token.** `LcscExplorerDialog` has
   `_search_token`, `_detail_token`, `_retail_token`, all bumped by
   `_cancel_pending()`; `mainwindow` has `assembly_enrichment_generation`.
   Handlers compare and drop stale payloads. Without this, a slow reply from
   a superseded search overwrites a newer one — or writes metadata onto a
   reference the user has since reassigned.
3. **Check `_alive()` before touching a widget.** The explorer is modeless
   and can be destroyed mid-fetch; a `CallAfter` landing on a deleted C++
   object raises `RuntimeError` inside the event loop.
   `LcscExplorerDialog._alive()` tests the dialog *and* a child, because
   top-level `Destroy()` is deferred to idle and children go first.

Bursty UI updates are coalesced rather than throttled: `on_bom_data_changed`
sets a `_bom_recompute_scheduled` latch and defers one recompute through
`wx.CallAfter`.

Retail backfill is the most involved case
([`explorer.py`](../kicad_lcsc_suite/lcsc/explorer.py#L985)): a bounded pool of 5 concurrent
fetches over the first 120 rows, each row painting as it arrives, with a
reaper thread posting completion. Deliberately not a `ThreadPoolExecutor` —
its workers are non-daemon and would keep KiCad alive on exit.

## 6. wx gotchas specific to KiCad

KiCad's bundled wxPython (4.2.2a1 / wxWidgets 3.2.8) has **assertions
enabled**, and wxPython raises them as `wx._core.wxAssertionError`. A single
inconsistent call therefore aborts whatever was running — typically
`_build_ui()` stops half-way and the user gets a broken or missing window
with no error message.

Confirmed triggers:

- `sizer.Add(win, 0, wx.ALIGN_CENTER_VERTICAL)` on a **vertical**
  `BoxSizer`, and the mirror case (horizontal alignment on a horizontal
  sizer). Audit every alignment flag against its sizer's orientation.
- Importing the plugin package after `wx.App` exists — `__init__.py` calls
  `JLCPCBPlugin().register()`, which asserts on `PgmOrNull()` outside KiCad.
  In standalone probes, import plugin modules *before* creating `wx.App`.

Two more layout facts, both learned the hard way:

- **DataView column widths set during construction are discarded** — the
  native control has not been realised yet. Restate them once the window is
  shown (`_on_first_shown`, reached via `wx.CallAfter` from `__init__`).
- **The explorer's column widths are derived, not fixed.** The numbers in
  `COLUMNS` are base widths; `_resize_columns` shares whatever the grid has
  spare over the text columns listed in `FLEX_WEIGHTS`, and takes it back per
  `SHRINK_ORDER` — spacer, then text, then figures — down to the `MIN_SHARE`
  floors when the grid is too narrow. Below those floors it stops and lets a
  horizontal scrollbar appear; above them a row always fits the window.
  The filter panel collapses via the Filters toggle (`_set_filters_shown`, a
  sizer show/hide) and the detail pane opens by selecting a part and closes on
  a repeat click on it (`_set_details_shown`) — it has no button of its own.
  Details live either in a vertical splitter or as an inline expanded row
  directly under the selection. Inline mode inserts blank DataView rows, moves
  the following results down, and overlays the detail panel on that reserved
  space. Every one of those states has to recompute widths. It is driven off
  the grid's own `EVT_SIZE`, coalesced through a `_resize_scheduled` latch, and
  skipped when neither the widths nor the hidden set changed.
- **A custom renderer paints into the size *it* asks for**, not into its
  column. macOS hands `Render` a rect as wide as `GetSize().width` and leaves
  the rest of the column blank, so a renderer that does not know its column
  wraps and clips text inside that box — a 100px box in a 470px column, which
  is what truncated "Multilayer Ceramic Capacitor" to "Capacito".
  `_resize_columns` pushes each width into its renderer (`set_cell_width`).
- **The native DataView is wider than the widths you set it.** It keeps an
  indent before the first cell and adds a fixed padding to every column — 16px
  and 17px a column on macOS — so a header whose numbers add up to exactly the
  client width overflows it by 135px and grows a scrollbar. Both are measured
  off a laid-out row by `_measure_grid_metrics` (from the column origins, not
  the row width: the last column absorbs the leftover, which would feed back
  in) and are zero on the generic DataView.
- **A `wxBoxSizer`'s own minimum multiplies each stretchable item's minimum by
  the *total* proportion**, so a 350px block at proportion 6-of-15 claims
  875px. Get that wrong and the sizer decides the space is insufficient and
  shrinks the item with no natural width of its own to defend itself — the
  parameter table went to 85px this way. Blocks with a natural width (the
  previews, the stock cards) go in at proportion 0 for this reason.
- **`GetItemRect` returns `(0, 0, 0, 0)` for a scrolled-out row**, which is no
  use for placing an overlay meant to scroll *with* the rows. `_row_top`
  derives any row's position from `GetTopItem`, which is visible by definition.
  Native scroll notifications are not dependable either — a trackpad scroll or
  an `EnsureVisible` can move rows with no event — so the open inline panel
  re-checks its position on a timer (`INLINE_TRACK_MS`) and clips itself to the
  visible slice inside `inline_clip` instead of hiding when it no longer fits
  whole.
- **HiDPI**: size everything through `HighResWxSize(window, size)` and
  `GetScaleFactor` from [`helpers.py`](../kicad_lcsc_suite/helpers.py); icons through
  `loadIconScaled`.

**Colour** must come from [`lcsc/theme.py`](../kicad_lcsc_suite/lcsc/theme.py) —
`colour(name)`, `stock_colour(count)`, `card_background()`, `blend()`. KiCad
follows the desktop light/dark appearance and a literal tuned on white is
unreadable on dark. `helpers.isDarkAppearance()` is the underlying probe.

Palette names carry meaning and are not interchangeable. `bad` is the error
red — zero stock, and a BOM part with no LCSC number. `standard` is the amber
advisory for a part that pushes the board into Standard-mode pricing: nothing
is broken, it just costs more. Those two shared `bad` once, which made a
pricing note indistinguishable from a failure.

**Multi-select filters** live in
[`lcsc/facetfilter.py`](../kicad_lcsc_suite/lcsc/facetfilter.py): a `wx.ComboCtrl` whose popup
is a `wx.CheckListBox`. Two non-obvious constraints, both found by
`scripts/gui_probe.py`:

- wx writes `ComboPopup.GetStringValue()` back into the control every time the
  popup closes, so the popup must defer to the owner's summary. Returning a
  constant blanks the row as soon as the user opens and closes the list.
- `ComboPopup.Create` runs lazily on first show, so a probe that never opens
  the popup cannot tell you whether this wx build can host one. `Popup()` /
  `Dismiss()` in the probe is what makes that failure mode visible.

## 7. Feature flows

### Assign a part (the main loop)

```text
user selects footprints in pcbnew
  -> mainwindow.select_part / open_lcsc_explorer
       builds {reference: search string}
  -> LcscExplorerDialog (singleton; re-targeted via update_for if already open)
       _start_search      -> api.jlc_search        (thread, _search_token)
       build_facets       -> multi-select filters from real attributes
       row selected       -> api.stock_report      (thread, _detail_token)
                          -> easyeda previews      (thread)
                          -> product photo         (thread, never blocking)
       retail backfill    -> api.retail_stock x N  (2-way pool, _retail_token;
                                                    LCSC retail view only)
       thumbnail backfill -> api.fetch_image x N   (3-way pool, _thumb_token)
       thumbnail clicked  -> PhotoViewerDialog     (thread, _token)
  -> "Assign" -> wx.PostEvent(AssignPartsEvent) -> mainwindow.assign_parts
       -> store.set_lcsc  -> BomDataChangedEvent -> coalesced BOM recompute
       -> start_part_detail_refresh -> lcsc.details.fetch_details (thread)
            -> PartDetailsProgressEvent -> library.set_cached_part_details
```

The retail and thumbnail passes start **together**, and share nothing: the
search response already carries each row's photo id, so a thumbnail needs no
JSON in front of it. Thumbnails used to wait for the stock fill to settle,
back when the photo URL came out of the retail response and starting early
would only have duplicated its requests — that ordering is now just a grey
grid for the length of the stock fill, or forever when retail is unreachable
and the pass never finishes.

`ThumbCell` holds the LCSC *code*, not a bitmap: photos arrive long after rows
do, and there is no valid "empty bitmap" to sit in while waiting. Clicking one
opens [`lcsc/photoviewer.py`](../kicad_lcsc_suite/lcsc/photoviewer.py) on the 900px original,
with the part's other angles (`api.assembly_photo_urls`) behind the arrow
keys. The viewer is modeless and reused — clicking a second thumbnail
retargets the open window rather than stacking another.

Every worker-thread result goes to the UI through `_post`, not a bare
`wx.CallAfter`. A fetch routinely outlives the dialog, and `wx.CallAfter`
raises **on the worker thread** once the window is gone, where nothing catches
it and it lands as a traceback in KiCad's console.

Facets are built from the **fetched** result set (≤100 parts), not from all
of LCSC. That is a deliberate limitation, not a bug — narrowing the keyword
pulls a different slice.

### Board ↔ schematic

An LCSC number assigned here lands on the **footprint**. The Symbol Fields
Table, the schematic BOM exporters and "Update PCB from Schematic" all read
the **symbol**, so a number that only exists on the board is invisible to
them — and is overwritten the next time the schematic is pushed to the PCB. A
schematic that arrives with its LCSC fields already filled has the mirror
problem: nothing put those numbers on the footprints, so the board comes up
looking entirely unassigned.

Two upper-toolbar buttons, one module each, and **neither runs on its own**.
There is no automatic sync in either direction: the two sides are separate
stores of the same fact and the plugin does not get to decide which one wins.

```text
"To schematic"   -> export_to_schematic -> sync_schematic(interactive=True)
       _schematic_paths        find_root_schematic: <board>.kicad_sch, else
                               the .kicad_pro name, else a lone sheet
       is_open_in_editor       ~<name>.kicad_sch.lck -> refuse (or override)
       _confirm_export         reads the sheets to name what gets overwritten
  -> SchematicExport(assignments).load_schematic([root])       [schematicexport.py]
       follows Sheetfile into the hierarchy, <name>_old backup

"From schematic" -> import_from_schematic
  -> read_schematic([root])                                    [schematicimport.py]
       same Sheetfile walk, LCSC-ish fields matching ^C\d+$
  -> diff_against_board(numbers, board_assignments())
       added / replaced / unknown / unchanged
       _confirm_import shows it; nothing is written until confirmed
  -> _apply_schematic_numbers   store.set_lcsc + set_lcsc_value(fp) + model
       board is changed in memory only — the user saves the PCB
```

Rules that keep either direction from eating data:

* **Absent means "leave alone".** Going out, `schematic_assignments()` lists
  only references that *have* a number plus ones the user explicitly removed,
  so a schematic ahead of the PCB keeps its numbers. Coming in, only symbols
  with a value matching `^C\d+$` are imported — an empty field, free text or a
  reference with no footprint changes nothing.
* **A locked schematic is never written.** eeschema keeps the whole document
  in memory; writing underneath it means the fields vanish on the user's next
  save. Reading it *is* allowed — the import warns that the disk copy may lag
  what is on screen rather than refusing.
* **No change, no write.** A sheet with nothing to update is left
  byte-for-byte alone, backup included.
* **Confirm before overwriting.** Both directions build a per-reference diff
  (`old -> new`) and put it in front of the user first. Whichever side you
  pick wins outright; nothing is merged.

`schematicexport.is_lcsc_field` is the shared vocabulary — `LCSC`, `LCSC_PN`,
`JLC_PN`, case-insensitively. If the two directions disagreed on what counts
as an LCSC field, an import would read a field the export refuses to update
and a round trip would silently drift.

`_schematic_sync_pending` is not a scheduler, only a flag: it lets
`quit_dialog` notice that assignments were never exported and ask once, which
matters because `_schematic_cleared_refs` — the removals — lives in memory and
is gone when the window closes.

### Import symbol + footprint + 3D

`lcsc/importer.py::LcscImporter.import_part` drives the vendored converter
and writes a library triplet, project-local by default:

```text
<board dir>/lcsc-lib/LCSC.kicad_sym
<board dir>/lcsc-lib/LCSC.pretty/
<board dir>/lcsc-lib/LCSC.3dshapes/
```

`register_libraries` / `_ensure_lib_table_entry` then add entries to the
project's `sym-lib-table` and `fp-lib-table` using `${KIPRJMOD}`, backing up
any table it edits to `*.lcsc-suite.bak`. Import outside the project dir and
it registers globally with an absolute path instead (`is_inside` decides).
**KiCad caches lib-tables at startup**, so a fresh import needs a restart to
appear in the chooser — expected, not a bug to fix.

### Generate fabrication data

`mainwindow.generate_fabrication_data` is a sequence of
`run_generation_step` calls, each reporting into the progress gauge:

```text
part consistency check  (same LCSC, different values -> confirm)
order-number placeholder check
pre hook                (HOOKS.md; nonzero exit -> Continue/Cancel dialog)
optional DRC            (run_drc_before_gerber_export)
fabrication.generate_geber / generate_excellon / zip_gerber_excellon
fabrication.generate_bom / generate_cpl
store.increment_generation_count
post hook               (only after all three artifacts succeed)
```

CPL positions pass through `fix_rotation` / `fix_position`, which apply the
corrections DB — regex on footprint name → rotation and offset. This is the
part most sensitive to KiCad version differences.

### BOM estimation

Deliberately layered so the arithmetic is testable without wx:

```text
bom_estimation/pricing.py   pure: calculate_bom_estimate, get_unit_price, ...
bom_estimation/view.py      pure: formatting + view models
bom_estimation/help_text.py copy
bom_widget.py               wx glue: BomEstimatorWidget + BomEstimatorController
```

Keep `pricing.py` free of wx and of transport. Missing metadata is filled by
`enrichment/providers.py` (`LCSCAssemblyMetadataProvider`, rate-limited to
1 req/s) on a worker thread, results landing via
`AssemblyEnrichmentProgressEvent` and persisted with
`store.set_assembly_metadata`.

## 8. Parts-database build pipeline

Not shipped in the plugin — it produces the databases users download.

```text
JLC public API
  -> common/jlcapi.py        JlcApi, CategoryFetch, Component
  -> common/componentdb.py   ComponentsDatabase (cache.sqlite3-compatible)
  -> common/translate.py     ComponentTranslator: normalise, compress prices
  -> common/partsdb.py       Generate: FTS5 parts DB per dblib config
  -> common/filemgr.py       split into GitHub-sized chunks, upload
  -> db_build/               the GitHub Action wrapper (Python >= 3.10)
```

`common/progress.py` provides the nested progress-bar abstraction shared by
these steps. See [`db_build/README.md`](../db_build/README.md) for the
variants and their filters.

## 9. Compatibility surface

Kept deliberately small so the plugin survives KiCad upgrades:

- **Only `GetBoard().GetFileName()`** is required from the `pcbnew` API for
  core operation, which is why KiCad 7–10 all work.
  `standalone_impl.KicadStub` mirrors the subset actually used — extend it
  whenever you reach for a new `pcbnew` call, or standalone mode breaks.
- **Paths** compare via `normcase`/`abspath` so `C:\Users` and `c:/users`
  match on Windows.
- **Plugin dir discovery** is per-platform: `~/Documents/KiCad/<ver>` on
  macOS/Windows, `$XDG_DATA_HOME/kicad/<ver>` on Linux.
- **TLS trust** falls back `LCSC_CA_BUNDLE` → `SSL_CERT_FILE` →
  `REQUESTS_CA_BUNDLE` → certifi → interpreter default → distro bundles
  (`lcsc/api.py::ssl_context`). Verification is **never** disabled; with no
  anchors, requests fail loudly. KiCad's macOS Python has no system trust
  store, which is the entire reason this chain exists.
- **Degradation**: unofficial endpoints may 403, rate-limit or change shape.
  Failures must render as `?` or `…` and log — never raise into the event
  loop, never be confused with a confirmed zero.
