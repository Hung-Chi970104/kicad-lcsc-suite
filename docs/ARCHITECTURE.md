# Architecture

How kicad-lcsc-suite is put together. Companion to [../AGENTS.md](../AGENTS.md)
(rules and navigation) and [DEVELOPMENT.md](DEVELOPMENT.md) (how to run and
test it).

---

## 1. The shape of the thing

This is a **KiCad 10 plugin** whose UI runs **out of process**. There is no
server and no build artifact — `install.sh` builds a virtualenv and symlinks
`kicad_plugin/` into KiCad's plugin directory, so the working tree *is* what
runs.

```text
KiCad 10 (pcbnew)                        LCSC Suite (separate process)
│                                         │
├─ reads plugins/lcsc_suite/plugin.json   │
│    └─ toolbar button "LCSC Suite"       │
│                                         │
├─ on click: exec run.sh ───────────────► launcher clears PYTHONHOME,
│    env: KICAD_API_SOCKET                │  runs the venv's python -m lcsc_suite
│         KICAD_API_TOKEN                 │
│                                         ├─ PySide6 UI (Fusion style)
└─◄──── IPC API (kipy) ──────────────────┤─ lcsc/ network layer
     board, footprints, fields, commits   ├─ SQLite stores
                                          └─ BOM / CPL writers
```

`runtime.type: "exec"` is the key: KiCad launches **any executable**, so the app
brings its own Python and KiCad does not care which. That also means swapping
the venv for a frozen binary later is a change to one shell script.

KiCad reruns the launcher on **every** toolbar click, so there is no window for
a second invocation to find. [`single_instance.py`](../lcsc_suite/single_instance.py)
is a `QLocalServer` whose *binding* is the lock and which doubles as the "come
forward" channel. It is scoped **per board**: two windows on one board would
share a project database and quietly overwrite each other.

The object graph is deliberately flatter than the dialog it replaced:

```text
SuiteController (controller.py)   — decides and writes
├── board       kicad_bridge._Board  — the only thing that touches KiCad
├── parts       PartList             — board <-> project DB <-> displayed rows
│     ├── store    Store             — per-project SQLite state for this board
│     └── library  Library           — parts DB, part cache, corrections, mappings
├── settings    Settings             — per-user config directory
├── undo        UndoStack            — reaches the database, which KiCad's cannot
├── estimator   BomEstimator
├── explorer    ExplorerWindow       — one, re-targeted
└── window      MainWindow           — builds, displays and reports. Nothing else.
```

That last line is the rule the whole UI is organised around: **the window
builds, displays and reports; the controller decides and writes.** Every dialog
is opened from the controller, and every write to the board, the project
database or the mappings table goes through it — with one refinement: a dialog
whose entire purpose is to edit one store (Settings, Mappings, Corrections) owns
its own writes, because routing those through the controller would add
pass-through methods that decide nothing.


## 2. Two lineages in one package

The fork has a clear seam, and it matters for how you approach a change. It is
no longer a *directory* seam — the Phase 8 cutover collapsed the two packages
into `lcsc_suite/` — but the lineages are still there and still worth knowing.

**Upstream (`Bouni/kicad-jlcpcb-tools`)** — the board-centric half:
`store.py`, `library.py`, `schematicexport.py`, `schematicimport.py`,
`derive_params.py`, `dblib/`, `bom_estimation/`. Style is older: broad
`except`, module-level SQL. Keep diffs surgical here — the fork tracks upstream
via `UPSTREAM.txt` and gratuitous rewrites make future merges expensive.

**This fork (`lcsc/`)** — the part-selection half, written fresh. Typed,
docstring-heavy, stdlib-only, defensive about threads and teardown.

**The migration (`ui/`, `controller.py`, `kicad_bridge.py`, `parts.py`,
`undo.py`, `export.py`, `schematic.py`, `search_source.py`)** — everything that
used to be a wx dialog, rewritten. `fab_rules.py` is a special case: it is
upstream's BOM/CPL arithmetic, extracted from `fabrication.py` so that the two
halves ran the same code during the migration rather than two ports of one
spec.

`shared.py` names the toolkit-free logic layer across all three lineages.
Importing through it is what keeps the boundary visible now that the directory
no longer draws it.

The parts-database build tooling is `db_build/` (with its own `common/`
library) at the repository root, and is not plugin code. Every test for any of
it is in `tests/`.


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
interchangeable. [`lcsc/details.py`](../lcsc_suite/lcsc/details.py) resolves the part
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
the cache is the Explorer's job, on a worker pool, paced by the host breaker
and the fill caps rather than by a fixed interval.

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

**Parts DB variants** are declared in [`dblib/__init__.py`](../lcsc_suite/dblib/__init__.py)
(`DatabaseConfig`): `current-parts-fts5.db` (default, excludes parts unstocked
>1 year), `parts-fts5.db` (all), `basic-parts-fts5.db`, and an empty one. The
user picks one in Settings; `Library.refresh_library_config` re-resolves paths
and re-runs `check_library`. Databases ship as split zip chunks and are
reassembled by [`unzip_parts.py`](../lcsc_suite/unzip_parts.py).

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

Every network or disk-heavy operation runs off the UI thread, and results reach
the UI through exactly one mechanism: a **queued Qt signal**.

`QThreadPool` workers live in
[`ui/explorer/tasks.py`](../lcsc_suite/ui/explorer/tasks.py). A worker computes
and emits; the connection is queued, so the slot runs on the UI thread.
[`events.py`](../lcsc_suite/events.py) is the older path, still used by
`library.py` and `unzip_parts.py` for download and unzip progress: those modules
call `events.post(destination, event)` and the destination re-emits it as a Qt
signal. Dispatching there rather than in the caller is what keeps those two
modules free of any toolkit at all.

Three rules for anything asynchronous:

1. **Workers touch nothing shared.** No `store`, no `library`, no widgets. They
   compute and hand the result back.
2. **Every result carries a staleness token.** A slow reply from a superseded
   search must not overwrite a newer one, and must not write metadata onto a
   reference the user has since reassigned. Qt severs a connection to a
   destroyed receiver, so "the window is gone" needs no guard — but "these
   results are for the previous search" still does, and that is what the tokens
   in `tasks.py` are.
3. **A widget inside a view belongs to the view.** `setIndexWidget` hands
   ownership over, and the view deletes what it owns — on `setIndexWidget(idx,
   None)`, on row removal, and on a model reset. The Explorer's inline detail
   pane sits inside a throwaway host for exactly this reason; getting it wrong
   was a segfault with no traceback, not an exception. See the plan's §10.

Bounded fills rather than unbounded ones: retail backfill is 120 rows over 2
workers, thumbnails 60 over 3. Two rather than five because EasyEDA's throttle
turns a burst into a ban, and `_HostBreaker` stops a doomed fill after three
failures instead of after a hundred.

## 6. KiCad-specific gotchas

The wx assertions that used to live here are gone with the toolkit. What
replaced them are the IPC API's four traps, and they are worse, because three
of the four **return success**:

1. **KiCad poisons the environment.** It hands its own `PYTHONHOME` to `exec`
   plugins, which kills a venv Python with `ModuleNotFoundError: No module
   named 'encodings'`. `kicad_plugin/run.sh` unsets it.
2. **The API silently ignores writes to the wrong object.** `update_items` on a
   *field* returns success and changes nothing; it must be called on the parent
   **footprint**. Neither spelling raises.
3. **Custom fields are not on the footprint.** They live in
   `footprint.definition.items`. Creating one goes through
   `definition.add_item(field)`; clone an existing `Field` and
   `proto.ClearField("id")` rather than constructing one.
4. **An open commit is invisible to a read.** Between `begin_commit()` and
   `push_commit()` the board answers `get_footprints()` from the *committed*
   state, so verifying before committing compares against the old state and
   makes every write look like it failed — looking exactly like trap 2.

`kicad_bridge.py` wraps all four so no caller can hit them: `_Board.apply`
snapshots, commits, pushes, verifies by re-reading, and restores the snapshot in
a second commit on mismatch. The price is that a failed write costs two entries
in KiCad's undo history rather than none; the board ends up unchanged either
way.

**KiCad's undo cannot reach the project database**, which is why
[`undo.py`](../lcsc_suite/undo.py) exists. A removal clears the board field
*and* the number in `project.db`; undoing only the board half leaves the table
still saying unassigned. A reversal here is a new verified write, not a
rollback.


## 7. Feature flows

### Assign a part (the main loop)

```text
user selects rows in the part table (or double-clicks one)
  -> SuiteController.open_explorer
       builds the keyword from the row's value + package ("1uF 0805", not "1uF")
  -> ExplorerWindow (one; re-targeted if already open)
       search             -> api.jlc_search        (pool, search token)
       build_facets       -> multi-select filters from real attributes
       row selected       -> api.stock_report      (pool, detail token)
                          -> easyeda previews      (pool)
                          -> product photo         (pool, never blocking)
       retail backfill    -> api.retail_stock x N  (2-way pool, retail token;
                                                    LCSC retail view only)
       thumbnail backfill -> api.fetch_image x N   (3-way pool, thumb token)
       thumbnail clicked  -> PhotoViewer           (retargetable while open)
  -> "Assign" -> assign_requested signal -> SuiteController.assign_number
       -> board first (verified re-read), project database second
       -> UndoStack records one entry per action
       -> estimator recompute; Type / Stock / Params re-resolve from the cache
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
opens [`lcsc/photoviewer.py`](../lcsc_suite/lcsc/photoviewer.py) on the 900px original,
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

### Export the BOM and CPL

Gerber and drill output is **out of scope** — another plugin handles
fabrication. What remains is the two files that carry LCSC data, and nothing
else can produce them, because nothing else knows this project's assignments.

```text
SuiteController.export_bom_cpl
  consistency check     (same LCSC, different values -> confirm)
  Exporter.export       -> fab_rules.bom_rows   (grouping, DNP, the 1920-char
                                                 designator chunker)
                        -> fab_rules.cpl_row    (corrections, rotation, offsets)
  report                -> what was written, and what was left out
```

Three pieces of geometry decide whether the CPL is right, and all three were
measured against KiCad's own Python rather than assumed:

- the position is the **centre of the merged bounding box of every pad**, not
  the footprint origin. Those agree on a symmetric part and disagree on most
  others, so getting it wrong looks plausible on the first board you try;
- `FromMM` **truncates** (`int(mm * 1e6)`), and `BOX2I::GetCenter` is
  `position + size // 2`, so a box of odd width centres one nanometre low;
- arithmetic stays in **integer nanometres until the last line**, because
  dividing early turns `123.456789` into `123.45678900000001`.

`Board.pad_centers_nm()` asks KiCad for every pad box on the board in one round
trip — 379 pads, not 110 requests — and only when a CPL is actually written.

### BOM estimation

Deliberately layered so the arithmetic is testable without wx:

```text
bom_estimation/pricing.py   pure: calculate_bom_estimate, get_unit_price, ...
bom_estimation/view.py      pure: formatting + view models
bom_estimation/help_text.py copy
ui/bom_estimator.py         Qt glue: the summary line and the enrichment pass
```

Keep `pricing.py` free of any toolkit and of transport. Missing metadata is filled by
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

- **The whole of KiCad is reached through `kicad_bridge.py`** and nothing else.
  It is a small surface — board name, footprints and their fields, the
  drill/place origin, pad boxes, commits — which is what makes the IPC API's
  churn between 10.x point releases survivable, and what makes `FixtureBoard` a
  credible stand-in. `kicad-python` is pinned for the same reason.
- **KiCad 10.0 is the floor.** The IPC API does not exist before it, and the
  plugin manifest KiCad reads to launch this app is a KiCad 10 feature.
- **Paths** compare via `normcase`/`abspath` so `C:\Users` and `c:/users`
  match on Windows.
- **Plugin dir discovery** is per-platform: `~/Documents/KiCad/<ver>/plugins`
  on macOS/Windows, `$XDG_DATA_HOME/kicad/<ver>/plugins` on Linux.
- **Settings and databases live in per-user directories**, never beside the
  code. Deriving either from a module's own location has broken twice — see
  `config.adopt_data_directory` — and a frozen binary's install directory may
  be read-only.
- **Layout is identical across platforms and that is checked**, not asserted:
  Fusion is forced, font sizes are explicit, and CI renders every screen on
  `windows-latest` and compares the widget tree against the committed macOS
  reference.
- **TLS trust** falls back `LCSC_CA_BUNDLE` → `SSL_CERT_FILE` →
  `REQUESTS_CA_BUNDLE` → certifi → interpreter default → distro bundles
  (`lcsc/api.py::ssl_context`). Verification is **never** disabled; with no
  anchors, requests fail loudly. KiCad's macOS Python has no system trust
  store, which is the entire reason this chain exists.
- **Degradation**: unofficial endpoints may 403, rate-limit or change shape.
  Failures must render as `?` or `…` and log — never raise into the event
  loop, never be confused with a confirmed zero.
