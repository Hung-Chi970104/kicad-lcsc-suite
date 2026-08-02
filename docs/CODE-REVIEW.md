# kicad-lcsc-suite — full code review

Scope: every Python file outside `lib/` (vendored) and `.venv/`. ~28 000 LOC.
Baseline verified at working tree state of 2026-08-03 (3 commits + large
uncommitted diff).

Health check run before the review:

- `pytest` — **436 passed** in 4.2 s
- `ruff check --extend-exclude=lib` — **clean**
- `ruff format --check --exclude lib` — 4 files would reformat
  (`corrections.py`, `events.py`, `kicad_drc.py`, `partdetails.py`) — the four
  untouched upstream files `CLAUDE.md` already documents.

Two findings were verified empirically rather than by reading:

- `re.split("([0-9]+)", DataViewIconText(...))` raises `TypeError` under
  KiCad's bundled Python 3.9.13 + wx (finding **B2**).
- `... NOT IN ('D'Angelo')` raises `sqlite3.OperationalError` (finding **B1**).
  The empty-`IN ()` case I also suspected turns out to be *legal* in SQLite, so
  that half of the suspicion is withdrawn.

---

## 0. Executive summary

The codebase is two codebases wearing one coat.

**The fork's own code** (`lcsc/`, `schematicimport.py`, most of
`schematicexport.py`, `bom_estimation/`, the threading discipline added to
`mainwindow.py`) is unusually good for a KiCad plugin. It has written-down
invariants, staleness tokens on every async path, an explicit
"degrade-never-crash" policy for unofficial endpoints, a circuit breaker, and
pure logic cores that are unit-testable without KiCad. The comments explain
*why* — including several that record a bug that was already fixed once, which
is exactly what stops it coming back.

**The upstream base** (`store.py`, `library.py`, `fabrication.py`,
`corrections.py`, `partmapper.py`, `partdetails.py`, the shape of
`mainwindow.py`) is ordinary hobby-project code: string-interpolated SQL, a
connection per query, a 2 650-line dialog class, a bare `except:`, unguarded
file I/O for settings, and a logging setup that hijacks the host application.
Most of it works because the happy path is well-trodden, not because it is
defended.

Your migration instinct is half right. See §7 for the specific
recommendation, but the short version: **the parts worth rewriting from scratch
are not the parts you wrote.** Your `lcsc/` package has a small, explicit
coupling surface and would port in an afternoon. What is expensive to
re-derive is exactly the upstream stuff you'd be tempted to drop —
`fabrication.py`'s KiCad plot/drill API handling, `store.py`'s
board↔database reconciliation rules, and the v6/v7/v8 schematic format
branches. Those encode years of accumulated field knowledge, bugs and all.

Counts by severity:

| | Fork's code | Upstream | Total |
|---|---|---|---|
| High | 1 | 4 | 5 |
| Medium | 4 | 7 | 11 |
| Low / quality | 6 | 9 | 15 |

---

## 1. High severity

### H1 — "Refresh data" permanently breaks the explorer's thumbnails *(fork)*

`lcsc/explorer.py:1112` builds the renderer with a **bound method of the dict
object**:

```python
ThumbCell(self._thumbs.get, self.thumb_px)
```

`lcsc/explorer.py:2000`, in `_on_refresh`, **rebinds the attribute** rather
than clearing the dict:

```python
self._thumbs = {}
```

After that, `_thumb_ready` (`:2458`) writes into the *new* dict while the
renderer keeps reading the *old* one. Effect: press "Refresh data" once and
every thumbnail fetched from then on is invisible — rows show the empty
placeholder frame forever, while thumbnails cached before the Refresh keep
rendering (they live in the old dict). Only closing and reopening the explorer
recovers.

Note that `_thumb_ready:2457` gets this right for the cache cap — it uses
`.clear()`. The inconsistency is what makes it easy to miss.

- **Fix**: `self._thumbs.clear()`.
- **Blast radius**: none. `_on_refresh` is the only place that rebinds; nothing
  else holds a reference to the dict object. One-word change.

### H2 — Sorting the BOM / POP / Side columns raises `TypeError` *(upstream)*

`mainwindow.py:687-692` marks these columns sortable:

```python
bom.SetSortable(True)
dnp.SetSortable(True)
side.SetSortable(True)
pos.SetSortable(False)      # <- the one that was noticed
```

`datamodel.py:221-232` returns a `wx.dataview.DataViewIconText` for
`BOM_COL`, `POS_COL`, `DNP_COL` and `SIDE_COL`. `Compare` (`:267`) feeds that
straight into `natural_sort_key` (`:173`), which does
`re.split("([0-9]+)", s)`.

**Verified**: that call raises `TypeError: expected string or bytes-like
object` on KiCad's own interpreter. wxPython prints the traceback from inside
the sort callback and the resulting order is undefined. `pos` being the only
one set to `False` suggests someone hit this once and patched the single
column they were clicking.

- **Fix**: either set the other three to `SetSortable(False)`, or make
  `Compare` fall back to a stable key for icon columns (e.g. sort on the
  underlying 0/1 state rather than the icon).
- **Blast radius**: trivial either way. If you make them genuinely sortable,
  note that `GetValue` is also what the renderer consumes, so the coercion
  belongs in `Compare`, not in `GetValue`.

### H3 — User-typed values interpolated into SQL *(upstream)*

`library.py` builds six queries with f-strings:

| Line | Query | Untrusted input |
|---|---|---|
| 268 | `SELECT * FROM correction WHERE regex = '{regex}'` | user-typed regex |
| 277 | `DELETE FROM correction WHERE regex = '{regex}'` | user-typed regex |
| 287 | `UPDATE correction SET rotation = '{rotation}' … WHERE regex = '{regex}'` | user-typed regex |
| 336 | `SELECT * FROM mapping WHERE footprint = '{footprint}' AND value = '{value}'` | board-derived |
| 346 | `DELETE FROM mapping WHERE footprint = '{footprint}' AND value = '{value}'` | board-derived |
| 357 | `UPDATE mapping SET LCSC = '{LCSC}' WHERE footprint = '{footprint}' …` | board-derived |

plus `store.py:435`:

```python
refs = [f"'{fp.GetReference()}'" for fp in get_valid_footprints(self.board)]
cur.execute(f"DELETE FROM part_info WHERE reference NOT IN ({','.join(refs)})")
```

This is not a remote-code-execution story — it is a local plugin and the input
comes from the user's own board. It is a **correctness** story, and there is a
concrete failure chain:

1. `corrections.py:428` inserts a correction using the *parameterised*
   `insert_correction_data` — so a regex containing `'` gets in fine.
2. `get_correction_data` / `update_correction_data` / `delete_correction_data`
   are interpolated, so from then on that row can be neither found, edited nor
   deleted. Verified: `OperationalError: near "Angelo": syntax error`.
3. `library.fetch_remote_corrections:878` gates its inserts on
   `get_correction_data(...)` returning falsy, so a row it can't see gets
   re-inserted on every rebuild — silent duplicates.

The same applies to any footprint name or value containing an apostrophe
(`MFR's part`, `Ø3.2 mount`) reaching the mapping table.

Why nobody noticed: `pyproject.toml:ignore` lists **`S608` (hardcoded SQL
expression)** — twice, in fact, once in `select` and once in `ignore`, with
`ignore` winning. Ruff is configured not to tell you.

- **Fix**: parameterise the six queries; build a placeholder list for
  `clean_database` the way `store.get_assembly_enrichment_targets:295` already
  does correctly.
- **Blast radius**: contained. All six have the same signatures and callers;
  `get_correction_data` returns a row tuple either way. Removing `S608` from
  the ignore list afterwards is what stops it regrowing.

### H4 — Settings file I/O has no failure path *(upstream)*

`mainwindow.py:1699`:

```python
def load_settings(self):
    with open(os.path.join(PLUGIN_PATH, "settings.json"), encoding="utf-8") as j:
        self.settings = json.load(j)
```

and `:1739`:

```python
def save_settings(self):
    with open(..., "w", encoding="utf-8") as j:
        json.dump(self.settings, j)
```

`load_settings` is called from `__init__` before anything else. A missing,
truncated or non-JSON `settings.json` therefore makes the **entire plugin fail
to open**, with a raw traceback and no window. And `save_settings` is a
non-atomic truncate-then-write called from a dozen UI handlers, several of
them during teardown — so a crash or a full disk mid-write manufactures
exactly the state that bricks the next start.

`settings.json` lives in `PLUGIN_PATH`, i.e. inside the checked-out plugin
directory. On a symlinked install that is the user's git checkout.

- **Fix**: wrap the load in `try/except (OSError, ValueError)` falling back to
  `{}` (every consumer already uses `.get(section, {})`), and write via a
  temp file + `os.replace`.
- **Blast radius**: you have to decide what the defaults are. Today the shipped
  `settings.json` *is* the default, and the migration block at `:1704-1730`
  assumes the file parsed. A `{}` fallback flows through that block correctly
  (it uses `setdefault` throughout), so this is safer than it looks.

### H5 — The same endpoint is fetched twice per part by two different HTTP stacks *(architecture)*

There are two independent HTTP layers:

- `lcsc/api.py` — urllib, shared validating SSL context, 5-minute TTL cache,
  `_HostBreaker` circuit breaker, `{}`-on-failure contract.
- `lcsc_api.py` (49 lines) + `library.py` + `partdetails.py` — `requests`, no
  cache, no breaker, no shared context.

They hit **the same URL**:

- `lcsc/api.py:36` — `https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={}`
- `lcsc_api.py:21` — the identical string.

`mainwindow.init_store:928` starts both consumers at once:

```python
self.populate_footprint_list()
self.start_part_detail_refresh()     # -> lcsc/details.fetch_details -> jlc_assembly_detail
self.start_assembly_enrichment()     # -> LCSC_API.get_part_data     -> same URL, uncached
```

Both workers self-pace at 1 request/second (`PART_DETAIL_REQUEST_INTERVAL`
= 1.0 at `mainwindow.py:124`; `min_interval_seconds=1.0` at `:1174`) and they
do not know about each other. On a board with 200 distinct LCSC numbers,
opening the plugin issues roughly:

- 200 × `jlc_search` (from `details._search_exact`)
- 200 × `getComponentDetail` (cached, breaker-protected)
- 200 × `getComponentDetail` again (uncached, no breaker)

against an endpoint the code's own comments describe as rate-limiting. The
duplicate is pure waste, and it is the one that will earn the ban, because
it's the one with no breaker in front of it.

- **Fix**: point `enrichment/providers.LCSCAssemblyMetadataProvider` at
  `lcsc.api.jlc_assembly_detail`. The two payload shapes are the same JSON;
  the provider only reads `assemblyProcess` and `componentProductType`, both of
  which are in `data` — the only difference is `LCSC_API` wraps it as
  `{"success":…, "data": {"data": …}}` and `jlc_assembly_detail` already
  unwraps to the inner dict.
- **Blast radius**: `enrichment/providers.py:75-79` and its tests
  (`common/test_bom_estimator_enrichment.py`). The provider is already behind a
  `Protocol` and takes an injectable `api`, so this is the change the design
  was set up for. Afterwards, `lcsc_api.py` has only `partdetails.py` left as a
  consumer, and killing that removes `requests` from the assignment path
  entirely.

---

## 2. Medium severity

### M1 — Two of five assignment paths never write the footprint field *(upstream)*

`set_lcsc_value` (`footprint_helpers.py:24`) is called from exactly three
places: `assign_parts` (`mainwindow.py:1045`), `remove_lcsc_number` (`:1608`),
`_apply_schematic_numbers` (`:2686`).

It is **not** called from:

- `paste_part_lcsc` (`mainwindow.py:2269`) — right-click → Paste LCSC
- `search_foot_mapping` (`:2722`) — right-click → Find LCSC from Mappings

Both write `store.set_lcsc(...)` and `partlist_data_model.set_lcsc(...)` and
stop there. The number is therefore in the project database and on screen, but
not on the footprint, so it is absent from the `.kicad_pcb` file, invisible to
anything else that reads footprint fields, and lost entirely if the project
travels without `jlcpcb/project.db`.

I traced whether `Store.update_from_board` then clobbers it on the next open:
it does not (the `board_part["lcsc"]` empty / `db_part["lcsc"]` set combination
falls through both branches at `store.py:396-418` without updating). So the
data survives in the DB — it just never reaches the board.

- **Fix**: add the `set_lcsc_value(fp, lcsc)` call to both, with a `None`
  guard on `FindFootprintByReference`.
- **Blast radius**: also worth auditing the three existing call sites, which
  dereference `fp` without a `None` check (see M2).

### M2 — Unguarded `FindFootprintByReference` results *(upstream)*

`mainwindow.py:1426-1449` (`populate_footprint_list`) and `:1545`
(`OnFootprintSelected`) both do:

```python
fp = self.pcbnew.GetBoard().FindFootprintByReference(part["reference"])
...
str(fp.GetLayer())        # :1449
fp.SetSelected()          # :1546
```

`Store.clean_database` keeps the table in sync at construction time, so this is
normally safe. It stops being safe the moment the user deletes a footprint in
pcbnew while the plugin window is open — the store row survives until the next
`Store()` and the dereference raises `AttributeError`, inside a paint/selection
handler. `assign_parts:1044` and `_apply_schematic_numbers:2676` have the same
shape (the latter *does* guard, at `:2677`, and comments on why).

- **Fix**: guard-and-skip, matching `_apply_schematic_numbers`.
- **Blast radius**: local; the question is whether a missing footprint should
  also prune the store row — I'd say no, leave that to `clean_database`.

### M3 — "In stock only" hides rows LCSC refused to answer about *(fork)*

`lcsc/explorer.py:2153` documents the right policy:

```python
# A retail figure we have not fetched yet counts as "keep it" …
if hit.lcsc not in self._retail:
    return True
return (self._retail.get(hit.lcsc) or 0) > 0
```

But `_report_done:2592` records `self._retail[hit.lcsc] = report.retail_stock`,
which is `None` when the retail hosts are unreachable. So selecting a row while
retail is down converts that row from "not asked" (kept) to "asked, answer 0"
(hidden) on the next `_apply_filters`. The row the user just clicked
disappears.

This contradicts the module's own doctrine, stated at `api.py:414-425`:
"`None` means nobody answered — which is *not* the same as zero, and callers
must not render it as such."

- **Fix**: `_has_stock` should treat a recorded `None` the same as absent.
- **Blast radius**: `_update_status:2247` counts the same way and would want
  the same treatment; `_sorted:2176` is already fine (`None`→0 sorts last,
  which is what you want there).

### M4 — 3D-model paths break when the library root is nested *(fork)*

`lcsc/importer.py:127`:

```python
def _relative_to_project(self, path: Path) -> str:
    return path.relative_to(self.root.parent).as_posix()
```

`_model_uri` builds `${KIPRJMOD}/` + that. But `${KIPRJMOD}` is the *project*
directory, and `self.root.parent` is only the project directory when the
library root sits exactly one level inside it. `register_libraries:282` uses the
real `project_dir` for the lib-table URI, so the two disagree.

With the default root (`<proj>/lcsc-lib`) they coincide and everything works.
Point `settings["lcsc"]["library_root"]` at `<proj>/libs/lcsc-lib` — which the
Browse… button lets you do — and every imported footprint gets
`${KIPRJMOD}/lcsc-lib/LCSC.3dshapes/...` baked in, which does not exist, while
the lib-table entry is correct. Silent: KiCad just shows no 3D model.

- **Fix**: pass `project_dir` into `LcscImporter.__init__` and use it for both.
- **Blast radius**: three construction sites — `explorer._importer:2730`,
  `mainwindow.import_all_lcsc_libs:1874`, and tests. The constructor already
  takes a `project_relative` flag derived from `project_dir`, so the value is
  in hand at both call sites.

### M5 — `_TTLCache` never evicts *(fork)*

`lcsc/api.py:172-197`. Expired entries are dropped only when their own key is
read again (`get`, line 186). Nothing sweeps. A session that searches thirty
keywords retains thirty 100-hit JSON payloads plus every per-part detail
response for the life of the KiCad process.

The image cache next door (`:574`) has the opposite problem — it is bounded,
but it empties **wholesale** at 256 entries, so crossing the threshold drops a
full grid of thumbnails that then all refetch.

- **Fix**: sweep expired keys on `put`, or cap by count with FIFO eviction.
  For images, evict the oldest N rather than all.
- **Blast radius**: self-contained; `clear_cache()` already exists for the
  Refresh path.

### M6 — `library.migrate_mappings` can never succeed *(upstream)*

`library.py:900`:

```python
mcur.execute("INSERT INTO mapping VALUES (?, ?)", (r[0], r[1]))
```

The `mapping` table has three columns (`library.py:325`:
`'footprint','value','LCSC'`). The insert raises `OperationalError`, which the
enclosing `except sqlite3.OperationalError: return` at `:911` swallows. The
migration silently does nothing, every time.

- **Fix**: insert all three columns (`r[0], r[1], r[2]`) — assuming the legacy
  `parts.mapping` table had the same shape; verify against an old DB before
  trusting that.
- **Blast radius**: nil, it's dead-on-arrival today.

### M7 — Schematic writer's backup dance is not crash-safe *(fork, on upstream bones)*

`schematicexport.py:219-225`:

```python
backup = path + "_old"
if os.path.exists(backup):
    os.remove(backup)
os.rename(path, backup)
with open(path, "w", encoding="utf-8") as f:
    for line in newlines:
        f.write(line + "\n")
```

Three issues, in descending order of seriousness:

1. **Not atomic.** Between the `rename` and a successful write, the schematic
   does not exist at its own path. A disk-full or a permission error leaves the
   user with `foo.kicad_sch_old` and either nothing or a truncated
   `foo.kicad_sch`. Recoverable if you know the trick; alarming if you don't.
2. **Single-slot backups.** The pre-existing `_old` is deleted first, so two
   syncs destroy the last copy of the schematic as it was before the plugin
   ever touched it.
3. **Line endings are re-derived.** Every line is `rstrip()`ed on read and
   written back with `"\n"` in text mode, which becomes `\r\n` on Windows.
   Round-tripping a LF schematic on Windows rewrites every line of the file —
   a one-field change shows up as a whole-file diff in git.

The guards that *are* there (lock-file detection, only writing sheets that
actually changed, the per-reference confirmation dialog in
`mainwindow._confirm_export`) are good and clearly hard-won. This is the
remaining gap.

- **Fix**: write to `path + ".tmp"`, `os.replace` into place, keep the backup
  copy rather than the rename. Preserve the newline style by opening with
  `newline=""` and keeping the original terminators.
- **Blast radius**: `_commit` is called from all three format branches;
  `tests/test_schematic_sync.py` asserts on `_old` existing, so the test
  changes with it.

### M8 — Fabrication: bare `except:` and indiscriminate directory wipe *(upstream)*

`fabrication.py:242`:

```python
except:
    self.logger.info("WARNING footprint %s: original position used", ...)
```

Catches `KeyboardInterrupt` and `SystemExit` too, and hides whatever actually
went wrong (the realistic cause is a pad-less footprint making `pads[0]` raise
`IndexError`). `E722` is in the ruff ignore list, so this is invisible to lint.

`fabrication.py:302`:

```python
for f in os.listdir(self.gerberdir):
    os.remove(os.path.join(self.gerberdir, f))
```

`os.remove` on a subdirectory raises `IsADirectoryError`, aborting generation.
And it deletes *everything* in `jlcpcb/gerber/`, not just previously generated
artifacts.

- **Blast radius**: both are local. The directory wipe wants a
  `glob` on the extensions it actually produces.

### M9 — Root logger hijack *(upstream)*

`mainwindow.init_logger:2808`:

```python
root = logging.getLogger()
root.handlers.clear()
root.setLevel(logging.DEBUG)
```

This is the *host application's* root logger. Opening the plugin discards
whatever handlers KiCad (or another plugin) installed and turns global logging
up to DEBUG for the rest of the session. `quit_dialog:863-867` removes only its
own two handlers — it never restores what it destroyed.

The consequence beyond rudeness: every DEBUG record from every library —
`urllib3`, `requests` (both explicitly quieted at `:90-91`, which is a
tell that this hurt before), `PIL`, anything — gets formatted and marshalled
into the wx event queue by `LogBoxHandler.emit:2845`, one `wx.QueueEvent` per
record.

- **Fix**: attach to a package-scoped logger (`logging.getLogger(__package__)`)
  with `propagate` left alone, and never touch the root.
- **Blast radius**: every module does `logging.getLogger(__name__)`, and
  `__name__` is `kicad_lcsc_suite.foo`, so a package-scoped logger captures all
  of them with no other change. This is a clean, contained fix.

### M10 — `quit_dialog` destroys then calls `EndModal` *(upstream)*

`mainwindow.py:878`:

```python
self.Destroy()
self.EndModal(0)
```

`EndModal` on an already-destroyed, non-modal dialog (`plugin.py:44` calls
`Show()`, not `ShowModal()`). It is presumably a no-op in practice on the
platforms tested, but it is exactly the kind of call that raises a wx assertion
— which, per this project's own `AGENTS.md`, is fatal here.

### M11 — Non-daemon download thread and a division by zero *(upstream)*

`library.py:544` — `Thread(target=self._download_wrapper).start()` with no
`daemon=True`. A 750 MB download in flight blocks KiCad's exit. Every other
worker in the codebase is correctly daemonised.

`library.py:699` — `progress = f.tell() / size * 100` where
`size = int(r.headers.get("Content-Length", 0))`. A response without the header
raises `ZeroDivisionError`, caught by the broad handler at `:707` and reported
as "Failed to download chunk" — a misleading error for a successful transfer.

---

## 3. Low severity and code quality

**L1** `lcsc/api.py:324` — `_get_json` catches `(URLError, JSONDecodeError,
OSError)`. `http.client.HTTPException` (e.g. `IncompleteRead`) is none of
those and would propagate out of a worker thread, which is precisely the
failure mode `_post`/`_alive` exist to prevent elsewhere.

**L2** `lcsc/api.py:231-243` — `_HostBreaker.blocked` documents "let exactly
one request through to probe", but it pops `_open_until` for *every* caller
once the cooldown elapses, so all queued workers go through simultaneously.
Behaviour is fine; the comment is wrong, which matters in a file whose comments
are load-bearing.

**L3** `lcsc/explorer.py:2181` — `_sorted`'s min_qty branch is
`h.min_qty or 1`, so unknown minimums sort *first*, contradicting the
docstring two lines up ("Unknown values sort last in every mode").

**L4** `partdetails.py:239` — `wx.CallAfter` straight from a worker thread.
This is the exact call `lcsc/explorer._post:753` was written to replace, with a
40-line comment explaining why it produces stack traces in KiCad's console.
The lesson was learned in one file and not applied to the other.

**L5** `library.fetch_remote_corrections:866` — writes SQLite from a background
thread (started at `:168`) with no coordination against UI-thread readers.
SQLite's file locking makes it survivable; it is still the only place in the
codebase where a worker writes shared state directly, which `AGENTS.md`
forbids.

**L6** `library.get_parts_db_info:916` and friends call `sqlite3.connect()` on
`partsdb_file`, which **creates a 0-byte file** when the optional bulk DB is
absent. Harmless (`check_library:155` gates on size > 0) but it litters the
data directory on every start.

**L7** `core/version.py` ships a `test_version()` function inside a production
module, and imports `packaging` — a real third-party dependency that only works
because a copy is vendored in `lib/packaging/` and `__init__.py` prepends
`lib/` to `sys.path`. Both facts are surprising at the import site
(`schematicexport.py:30`), and `packaging.Version` is doing work that
`tuple(int(x) for x in v.split("."))` would do without the dependency.

**L8** `__init__.py:15` catches only `ImportError`. `JLCPCBPlugin().register()`
raises `wx._core.wxAssertionError` when a `wx.App` already exists — the case
`AGENTS.md` warns about for standalone probes — and that escapes.

**L9** `helpers.getWxWidgetsVersion:13` — `int(v.group(1).replace(".", ""))`
turns "3.2.4" into 324 and "3.1.5" into 315, then compares `> 315`. It happens
to order correctly for current versions and will not for "3.10.x" (3100 > 315,
fine) versus "3.2.10" (3210). Also `re.search` returning `None` raises
`AttributeError` with no fallback.

**L10** `datamodel.PartSelectorDataModel` (454-551), `partselector_columns.py`,
`search_escape.py` — dead code, deliberately retained per `AGENTS.md` to keep
the upstream diff small. Note `datamodel.py:17` still imports from
`partselector_columns`, so the dead module is load-bearing for the live one.

**L11** `pyproject.toml` — declares `name = "Kicad-jlcpcb-tools"`,
`requires-python = ">=3.10"` (the plugin runs on 3.9), and eight runtime
dependencies (`click`, `humanize`, `cachetools`, `ratelimit`, `tqdm`, `retry`,
`split_file_reader`, `requests`) that belong to `db_build`/`common`, not to the
plugin. `AGENTS.md` explains this, which is the right mitigation for a fork and
the wrong one for a fresh start.

**L12** Contradictory lint config: ruff deliberately disables `UP006`/`UP007`/
`UP035`/`UP045` to protect 3.9 syntax, while `.pre-commit-config.yaml` also
runs **pyupgrade** with no `--py3x-plus` flag. Today pyupgrade's default is
conservative enough not to collide, but the two tools are configured from
opposite premises and one flag change reintroduces 3.10-only syntax that ruff
has been told to leave alone.

**L13** `mainwindow.OnBomHide:1457` / `OnPosHide:1490` — each sets the same
bitmap twice, the first time with an empty filename (which constructs an empty
`wx.Bitmap` for nothing). Pure copy-paste residue, ~60 duplicated lines between
the two.

**L14** `bom_estimation/view.py:14` imports `_build_lcsc_quantities` and
`_safe_int` — private names — from `pricing.py`. The layering the module
docstrings describe ("view stays free of transport concerns") is real and worth
keeping; this crosses it.

**L15** `store.py` type annotations are wrong in several places:
`read_all() -> dict` and `read_bom_parts() -> dict` both return lists.

---

## 4. Efficiency

Ordered by how much they cost on a realistic 200–300 part board.

### E1 — One SQLite connection per operation *(upstream, `store.py`)*

Every one of ~25 methods opens `sqlite3.connect(self.dbfile)`, runs one
statement, commits and closes. `update_from_board:362` does `get_part` +
`create_part`/`update_part` **per footprint**, plus
`backfill_estimator_metadata` (another `get_part` when `db_part` is falsy) —
so 2–3 connect/commit cycles × every footprint on every open, before the
window paints. `set_lcsc`/`set_stock` are called in per-reference loops
(`assign_parts:1041`, `_apply_schematic_numbers:2684`), one connection each.

A single long-lived connection with an explicit transaction around
`update_from_board` would collapse hundreds of fsyncs into one. This is the
largest single win available and the most contained: `Store` already owns
`self.dbfile` and nothing outside it touches the connection.

### E2 — `find_index` is O(n) with a heavy constant *(upstream, `datamodel.py:284`)*

```python
return self.data.index([x for x in self.data if x[0] == ref].pop())
```

Two full scans: a comprehension over every row, then `list.index`, which
compares **entire 15-element rows** by value until it finds the object. Called
once per reference from `set_lcsc`, `set_part_details`, `set_bom_price` and
`set_enrichment_status`.

A BOM recompute calls `set_bom_price` for every reference
(`bom_widget.py:256-261`), so a 300-part board does 300 × (300-element scan +
300 row-equality comparisons) — ~180 000 list comparisons per recompute, and
recompute runs on assign, on every BOM/POS toggle, on every enrichment batch
and on every detail batch.

A `{reference: row}` dict makes it O(1). Blast radius: `AddEntry`,
`RemoveAll` and `remove_lcsc_number` must maintain it.

### E3 — BOM recompute walks the parts list three times *(fork)*

`BomEstimatorController.recompute:209` runs synchronously on the UI thread and
makes three passes with **three separate detail caches**:

- `_get_board_standard_context:152` — one `FindFootprintByReference` per part
- `calculate_bom_estimate` → `_PricingRunContext` cache
- `prepare_bom_price_labels` → its own `details_cache` (`view.py:232`)

So each unique LCSC number costs two `library.get_part_details` calls, each of
which is an E1-style SQLite connection. The coalescing latch
(`mainwindow.on_bom_data_changed:1109`) is good and already prevents the worst
of it; hoisting a single detail cache across the three passes would remove the
rest.

### E4 — `populate_footprint_list` *(upstream, `mainwindow.py:1417`)*

Calls `self.pcbnew.GetBoard()` inside the loop (`:1426`) rather than once, and
`library.get_part_details` per unique LCSC — again one SQLite connection each —
all on the UI thread while the list is being rebuilt. Runs on every BOM/POS
hide toggle and after every download.

### E5 — Icons are re-read from disk on every use *(upstream, `helpers.py:84`)*

`loadBitmapScaled` opens the PNG, calls `isDarkAppearance()` (which queries
system settings), converts to image, conditionally recolours, rescales and
re-wraps — every call. There are ~25 toolbar/icon calls in `JLCPCBTools.__init__`
alone, plus two per `PartListDataModel`. A module-level cache keyed on
`(filename, scale, dark)` is four lines.

### E6 — `fabrication._find_correction:136` rebuilds its anchored pattern list on
every call, and it is called 3× per footprint for rotation and 3× again for
position. With 40 corrections and 300 footprints that is 72 000 f-string
constructions per CPL generation. Hoist to a per-run precompute.

### E7 — Minor: `search_foot_mapping:2729` calls `get_mapping_data` twice for
the same key (once to test, once to read).

---

## 5. What is genuinely good (and should survive any migration)

Worth naming explicitly, because a rewrite tends to lose these first.

1. **The async discipline in `lcsc/explorer.py`.** `_post`/`_alive`
   (`:753`, `:777`) plus four independent staleness tokens, with `_cancel_pending`
   bumping all of them. Every callback checks both liveness and token. This is
   the correct pattern for a modeless dialog over an unreliable network, and the
   40-line comment explaining why `wx.CallAfter` alone is not safe is worth more
   than the code.

2. **Generation counters in `mainwindow`** (`assembly_enrichment_generation`,
   `part_detail_generation`), with the deliberate subtlety at `:1280-1288` that
   a *forced* refresh rides the current generation rather than opening a new
   one — so clicking a row during startup doesn't void the whole sweep. That is
   a bug someone hit and then reasoned about properly.

3. **`_HostBreaker`** (`lcsc/api.py:203`). Per-host circuit breaking with
   self-healing, and `retail_unreachable()` (`:428`) so the UI can say
   "nobody answered" instead of drawing a column of zeros. The
   `?` vs `…` vs `0` distinction is maintained consistently from the API layer
   through to the renderer — except for M3.

4. **The pure cores.** `bom_estimation/pricing.py`, `lcsc/details.py`,
   `schematicimport.py` and `lcsc/api.py`'s parsing half are wx-free and
   pcbnew-free by design, and that is exactly why 436 tests can run in 4
   seconds without KiCad.

5. **`schematicimport.py`** as a whole. Conservative (`^C\d+$` validation
   before importing anything), cycle-safe (`files_seen`), honest about lock
   files instead of refusing, and it separates "the schematic has no number for
   R1" from "the schematic has no R1" — which is the distinction the diff
   actually needs.

6. **The two-button schematic sync policy.** Never automatic, always with a
   per-reference preview naming what gets destroyed, plus the close-time prompt
   at `mainwindow:881`. This matches your recorded preference and is
   implemented as designed.

7. **`lcsc/theme.py`.** Resolved at call time so a mid-session appearance
   change is picked up, with light/dark pairs rather than one value plus a
   fudge.

---

## 6. Testing

436 tests, 4.2 s, all green. The distribution is the interesting part.

| Area | Coverage |
|---|---|
| `bom_estimation/` pricing + view | Strong (≈1 100 test LOC) |
| `lcsc/api.py` source selection, facets | Strong |
| `lcsc/details.py` | Strong |
| `schematicimport.py` / `schematicexport.py` | Strong (619 test LOC) |
| Part cache / detail refresh | Good |
| `common/` (db_build tooling) | Very strong (≈2 800 LOC) — **not plugin runtime code** |
| `store.py` | Only `test_store_pad_filter.py` |
| `library.py` SQL layer | None |
| `fabrication.py` | Corrections only; no generation-path test |
| Every wx dialog | None (by necessity — `scripts/gui_probe.py` is the substitute) |

The pattern is healthy: the fork's authors tested exactly what is cheap to test
and left the rest to the headless GUI probe. The gap that matters is
`store.py` + `library.py` — they are the two modules holding the data whose
loss the user would notice, they are pure-Python-plus-SQLite (so trivially
testable with a temp file), and they are where four of this review's findings
live. Roughly 300 lines of tests would cover both.

Note also that `common/`'s 2 800 test lines are testing GitHub-Action tooling
the plugin never imports at runtime — worth knowing before you read the
"436 tests" number as coverage of the plugin.

---

## 7. Migration assessment

You asked whether to rebuild from scratch and port your changes across. Here is
the honest arithmetic.

### What you would be leaving behind, and what it costs to re-derive

| Upstream asset | Re-derivation cost | Verdict |
|---|---|---|
| `fabrication.py` (511 lines) | **High.** KiCad plot-controller and Excellon API details, the v6/v7/v8.99 compatibility branches, JLC's 2048-char BOM row limit, aux-origin handling, the `JLC_`-prefixed layer convention, rotation/offset correction maths for bottom-side parts. Every line is a fact about somebody else's software. | Keep |
| `store.py`'s `update_from_board` reconciliation (55 lines) | **High.** The four-way branch on "what changed on the board vs what's in the DB", gated on the `lcsc_priority` setting, encodes real user expectations about which side wins. Easy to rewrite, hard to rewrite *correctly*. | Keep semantics, rewrite the plumbing |
| `corrections.py` + the remote corrections DB | **Medium.** The dialog is ugly but the data pipeline (Matthew Lai's CSV → local SQLite → CPL rotation) is a working integration. | Keep |
| Schematic v6/v7 format branches | **Medium.** You'd never write these from scratch, but users on KiCad 7 need them. | Keep |
| `mainwindow.py` dialog shell | **Low.** 2 650 lines of straight-line wx construction. Tedious, not hard. | Rewrite |
| `library.py`'s SQL layer | **Low.** ~400 lines of one-statement-per-connection CRUD you would write better in a third of the space. | Rewrite |
| `settings.py` (1 060 lines, a 680-line `__init__`) | **Low.** A declarative schema plus a generic form builder replaces almost all of it. | Rewrite |
| `partdetails.py`, `partmapper.py`, `partselector_columns.py`, `search_escape.py`, `PartSelectorDataModel` | **Zero.** Dead or superseded. | Drop |

### How coupled is your own code?

Less than you'd fear. `LcscExplorerDialog` touches its parent through exactly
seven things:

```text
parent.window          parent.scale_factor    parent.settings
parent.save_settings   parent.pcbnew          parent._part_selector
+ receives AssignPartsEvent
```

`lcsc/importer.py` needs only a root path and a project dir. `lcsc/api.py`,
`lcsc/details.py`, `lcsc/theme.py`, `lcsc/facetfilter.py`,
`lcsc/previewpanel.py` and `lcsc/photoviewer.py` import nothing from upstream
except `helpers.HighResWxSize` and `events.AssignPartsEvent`.
`schematicimport.py` imports two functions from `schematicexport.py` and
nothing else.

So the port surface for your work is: **one seven-member interface, one event
class, one size helper.** That is a day's work, not a project.

### Recommendation

**Do not do a from-scratch rewrite of the whole plugin.** The 20% of upstream
that is expensive to replace is load-bearing, and a rewrite puts you in the
position of reimplementing KiCad plotting API quirks — the least rewarding code
in the repo — while your actually-good code sits unused.

Instead, in this order:

1. **Fix the one-line bugs where they are** — H1, H2, M3 are each a few
   characters and there is no reason to carry them into anything. H4 and M9 are
   each under twenty lines and remove two whole classes of "the plugin won't
   open" report.

2. **Introduce a data-access layer.** One `Repository` object owning one
   connection, parameterised statements, an explicit transaction for
   `update_from_board`. This single change resolves H3, E1, L15 and most of the
   ambiguity in "which of the three stores is authoritative". It is the highest
   value-per-line change available and it does not require touching any UI.

3. **Collapse the two HTTP stacks** (H5). Once `enrichment/providers.py` goes
   through `lcsc/api.py`, `lcsc_api.py` has one consumer left; retire it with
   `partdetails.py` and `requests` leaves the assignment path.

4. **Then, if you still want it**, split the package in two — a `legacy/`
   holding `fabrication.py`, `store.py`, `corrections.py` and the v6/v7
   schematic branches behind a narrow tested interface, and everything else
   new. That gets you the clean codebase you want without re-deriving the
   fabrication knowledge, and it makes the legacy surface small enough to
   replace piecemeal later.

The thing worth designing properly if you do start something new is **C3: the
three sources of truth.** Right now an LCSC assignment lives in the footprint
field, in `jlcpcb/project.db`, and in the schematic symbol, and five different
call sites reconcile them with slightly different rules — which is precisely
how M1 happened (two of the five forgot one of the three). Decide which one is
authoritative, make the others projections of it, and route every mutation
through one function. That is the design decision that would justify a rewrite;
nothing else in this repo does.
