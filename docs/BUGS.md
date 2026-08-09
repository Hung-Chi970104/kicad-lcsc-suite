# Known bugs — kicad-lcsc-suite

Every defect currently known to be in the tree, with the file and line to find
it at. Verified against the working tree on **2026-08-09**, not copied forward
from an older document: each entry below was re-read in the code that ships
today.

**`BUGS.md`, not `TODO.md`, on purpose.** Everything here is a thing that is
already wrong. Work that is merely *not done yet* — the PyInstaller freeze, the
`Missing prices N` gap — lives in [QT_MIGRATION_PLAN.md](QT_MIGRATION_PLAN.md)
§10, and only gets an entry here because §5 below names what a reader would
otherwise come looking for.

Its ancestor is [CODE-REVIEW.md](CODE-REVIEW.md) (2026-08-03), which is a
**pre-migration** document: most of its High and Medium findings lived in
`mainwindow.py`, `datamodel.py`, `fabrication.py` and `partdetails.py`, and the
Phase 8 cutover deleted those files. §6 lists which of its findings died that
way, so nobody re-derives a fix for a file that is gone.

Order is by what it costs a user, not by where it lives.

---

## 1. Ours — in the Qt application

### B1 — The retail poll can freeze the window for 800ms a tick *(mitigated, not closed)*

[`board_watch.py:90`](../lcsc_suite/board_watch.py#L90) polls on the **GUI
thread**. The 5s write deadline it used to inherit turned a stalled KiCad into a
5s freeze per tick, and made the three-miss tolerance ~15s of wall clock instead
of the ~6s its comment claims. It now asks with its own
`POLL_TIMEOUT_MS = 800`, so the worst case is 3 × 800ms.

The freeze is smaller, **not gone**: the poll is still synchronous on the thread
that paints. The proper fix is moving it off the GUI thread, and that needs its
own IPC connection — kipy's client is one `pynng.Req0` socket shared by every
read and write in the app, so a second thread polling through it would interleave
request/response pairs with the user's writes. Deliberately not attempted as part
of a bug fix.

### B2 — `_update_status` counts a refusal as an answered row

[`window.py:705`](../lcsc_suite/ui/explorer/window.py#L705). `asked` counts every
row with a *recorded* retail figure, and a recorded `None` means "asked, nobody
answered". So when LCSC starts refusing partway down the grid, the status line
says "LCSC stopped answering after 40 of 100 rows" when it answered for 20 and
refused 20. The advice it gives ("Refresh to continue") is still the right
advice, and `pending` — `len(visible) - asked` — under-reports "still loading"
by the same rows. Cosmetic, and left alone deliberately while fixing B12: the
`unreachable and asked` branch needs a count of rows *attempted*, so this wants a
second counter rather than a changed one.

### B3 — Unknown minimum quantity sorts first

[`window.py:630`](../lcsc_suite/ui/explorer/window.py#L630) —
`sorted(hits, key=lambda h: h.min_qty or 1)`. A part with no known minimum sorts
as if its minimum were 1, i.e. to the top, against the docstring four lines above
that says "Unknown values sort last in every mode". The other three modes honour
it. One line.

---

## 2. Inherited — in the upstream logic layer that survived the cutover

These are in files the migration kept because re-deriving them is expensive
(`AGENTS.md`, "What this repo is"). They are still ours to fix; nothing upstream
is going to.

### B4 — Six interpolated SQL statements, and the duplicate corrections they cause

[`library.py:289`](../lcsc_suite/library.py#L289),
[`:298`](../lcsc_suite/library.py#L298),
[`:308`](../lcsc_suite/library.py#L308),
[`:357`](../lcsc_suite/library.py#L357),
[`:367`](../lcsc_suite/library.py#L367),
[`:378`](../lcsc_suite/library.py#L378) build `WHERE regex = '{regex}'` and
`WHERE footprint = '{footprint}' AND value = '{value}'` with f-strings.

The failure chain is concrete, and it is a correctness story rather than a
security one (the input is the user's own board):

1. corrections are *inserted* through a parameterised statement, so a regex
   containing `'` gets into the table fine;
2. `get_correction_data` / `update_correction_data` / `delete_correction_data`
   are interpolated, so from then on that row can be neither found, edited nor
   deleted — `sqlite3.OperationalError: near "Angelo": syntax error`;
3. `fetch_remote_corrections` gates its inserts on `get_correction_data(...)`
   being falsy, so a row it cannot see is **re-inserted on every rebuild**.

Same for any footprint or value carrying an apostrophe (`MFR's part`) reaching
the mapping table. Fix: parameterise all six. `pyproject.toml` lists **`S608`**
in `ignore`, which is why lint has never mentioned it; removing it afterwards is
what stops the pattern regrowing. `store.py:490` already builds a placeholder
list correctly and is the model to copy.

### B5 — `migrate_mappings` has never once succeeded

[`library.py:922`](../lcsc_suite/library.py#L922) inserts two values into the
three-column `mapping` table (`footprint`, `value`, `LCSC`, created at
[`:411`](../lcsc_suite/library.py#L411)). The resulting `OperationalError` is
swallowed by the `except sqlite3.OperationalError: return` around it, so the
legacy-mappings migration silently does nothing, every time. Fix is `(r[0], r[1],
r[2])` — but verify the legacy `parts.mapping` shape against an old database
before trusting that, because the swallow means nobody has ever seen this run.

### B6 — 3D-model paths break when the library root is nested

[`importer.py:130`](../lcsc_suite/lcsc/importer.py#L130) —
`path.relative_to(self.root.parent)` — where `_model_uri` prefixes
`${KIPRJMOD}/`. But `${KIPRJMOD}` is the **project** directory, and
`self.root.parent` is only that when the library root sits exactly one level
inside it. `register_libraries` uses the real project directory for the
lib-table URI, so the two disagree.

With the default root (`<proj>/lcsc-lib`) they coincide and everything works.
Point the setting at `<proj>/libs/lcsc-lib` — which the Browse… button lets you
do — and every imported footprint gets a model path that does not exist, while
the lib-table entry is correct. Silent: KiCad just shows no 3D model. Fix: pass
the project directory in and use it for both.

### B7 — The schematic writer's backup dance is not crash-safe

[`schematicexport.py:226`](../lcsc_suite/schematicexport.py#L226), in `_commit`,
three problems in descending order:

1. **Not atomic.** `os.rename(path, backup)` then open-for-write means the
   schematic does not exist at its own path mid-write. Disk full or a permission
   error leaves the user with `foo.kicad_sch_old` and a truncated or missing
   `foo.kicad_sch`.
2. **Single-slot backup.** The pre-existing `_old` is removed first, so two syncs
   destroy the last copy of the sheet as it was before this app ever touched it.
3. **Line endings are re-derived.** Lines are `rstrip()`ed on read and written
   back with `"\n"` in text mode, which becomes `\r\n` on Windows — so a
   one-field change rewrites every line of a LF schematic.

Fix: write `path + ".tmp"`, `os.replace` into place, *copy* the backup rather
than renaming, and open with `newline=""` keeping the original terminators.
`tests/test_schematic_sync.py` asserts `_old` exists, so it changes with this.

### B8 — The 750MB download thread is not a daemon, and divides by zero

[`library.py:565`](../lcsc_suite/library.py#L565) —
`Thread(target=self._download_wrapper).start()` with no `daemon=True`. A download
in flight blocks the process from exiting. Every other worker in the codebase is
daemonised.

[`library.py:711`](../lcsc_suite/library.py#L711) —
`size = int(r.headers.get("Content-Length", 0))` then `f.tell() / size * 100`. A
response without the header raises `ZeroDivisionError`, which the broad handler
below reports to the user as "Failed to download chunk" — a misleading error for
a transfer that was fine.

### B9 — Minor, in the same file

- [`library.py:937`](../lcsc_suite/library.py#L937) — `sqlite3.connect(self.partsdb_file)`
  **creates a 0-byte file** when the optional bulk parts database is absent.
  Harmless (`check_library` gates on size > 0) but it litters the data directory
  on every start.
- [`library.py:190`](../lcsc_suite/library.py#L190) /
  [`:887`](../lcsc_suite/library.py#L887) — `fetch_remote_corrections` writes
  SQLite from a background thread with no coordination against UI-thread readers.
  SQLite's file locking makes it survivable; it is still the only place a worker
  writes shared state directly, which `AGENTS.md` forbids.
- `store.py` opens a **connection per operation**, 16 of them, and
  `update_from_parts` does 2–3 connect/commit cycles per footprint on every
  reload. Not a bug — an efficiency finding (CODE-REVIEW E1), recorded here
  because the fix is the same `Repository` object that closes B4.

---

## 3. `lcsc/api.py` — copied, not edited

`CLAUDE.md` and `AGENTS.md` rule 5: this file is a copy, and a UI need does not
license editing it. All three are therefore **stated, not scheduled** — closing
any of them is a deliberate decision to fork the file.

- **`_TTLCache` never evicts.** [`api.py:172-197`](../lcsc_suite/lcsc/api.py#L172-L197).
  Expired entries are dropped only when their own key is read again; nothing
  sweeps. Thirty searched keywords retain thirty 100-hit payloads for the life of
  the process.
- **The image cache empties wholesale.** [`api.py:574`](../lcsc_suite/lcsc/api.py#L574)
  — `if len(_image_cache) >= _MAX_CACHED_IMAGES: _image_cache.clear()`. Crossing
  256 drops a full grid of thumbnails that then all refetch. Evicting the oldest
  N would not.
- **`http.client.HTTPException` escapes.** [`api.py:324`](../lcsc_suite/lcsc/api.py#L324)
  catches `(URLError, JSONDecodeError, OSError)`. An `IncompleteRead` is none of
  those and would propagate out of a worker thread — the exact failure mode the
  rest of the module exists to prevent.

---

## 4. Owed verification

Not bugs. Things asserted but never watched happen, which is how this project
defines a promise.

- **The "no" half of the board watcher has never been run by hand.**
  `scripts/live_ipc_check.py` §0 proves only that an *open* board reports itself
  open. Still to do, and not scriptable: close **only** the PCB window under a
  running app and watch the app follow it; then repeat with two projects open and
  confirm the other window survives. B10 and B11 below both changed this path, so
  it is owed twice over.
- **`live_ipc_check.py` has not been re-run since `kicad_bridge.still_open`
  gained its `timeout_ms` parameter.** CLAUDE.md requires it whenever
  `kicad_bridge.py` changes, and it needs KiCad open on a *copy* of a board,
  because it writes.

  What *has* been proven live, read-only, against KiCad 10 on 2026-08-09: the
  shortened deadline works through real kipy — `still_open(timeout_ms=800)`
  answered for the open board, and `_client._timeout_ms` and the pynng socket's
  `recv_timeout`/`send_timeout` were all back at 5000 afterwards. That is the
  half the unit tests cannot prove, because they stand in for kipy's internals
  with fakes and a fake cannot be wrong about an attribute name in the same way.
  The write paths are unchanged by that commit; re-run the full check anyway
  before trusting the bridge for a release.

## 5. Deferred features, so they are not mistaken for bugs

Both from [QT_MIGRATION_PLAN.md](QT_MIGRATION_PLAN.md) §10, and both deliberate:

- **The PyInstaller freeze**, which is what turns PCM from "the schema allows it"
  into an install path a user can use. The only thing between this and a public
  release.
- **`Missing prices N` in the BOM writer.** The estimate line reports the gap;
  the BOM writer does not, and never did in the wx plugin either. An unpriced
  part is still a BOM row.
- **Library import bypasses the offline guarantee.** `LcscImporter` uses
  `easyeda2kicad`'s own transport rather than `search_source`. Nothing reaches
  the wire today because no test or probe clicks Import; route it through
  `search_source` if one ever does.

---

## 6. Fixed

### B10 — `app.quit()` skipped the Explorer's `closeEvent` *(fixed 2026-08-09)*

`board_watch._shut_down` closed the main window and then called
`QApplication.quit()`, which leaves the event loop **without** running
`closeEvent` on any other top-level window. The Explorer's `closeEvent` is not
decoration: it cancels in-flight fetches, clears both thread pools and persists
the explorer geometry, `overwrite_existing` and `library_folder`. All of it was
skipped whenever the Explorer — or the part details window, same shape — was open
at the moment the board went away, which is the *likeliest* moment, because
closing the PCB is what a user does when they are done shopping.

Fixed by closing our own top-level windows explicitly, front-most first, before
the main window and before the quit. See `_our_windows` in
[`board_watch.py`](../lcsc_suite/board_watch.py) and
`test_shutting_down_closes_our_own_windows_first` in
[`tests/test_qt_board_watch.py`](../tests/test_qt_board_watch.py).

### B11 — The liveness poll inherited the 5s write deadline *(mitigated 2026-08-09 — see B1)*

### B12 — "In stock only" hid the rows LCSC had refused to answer about *(fixed 2026-08-09)*

`_has_stock` asked `model.asked_retail()`, which is true for a *recorded* `None`
— and `None` is what `set_retail` stores when the retail hosts refuse. So the
row went from "not asked" (kept) to "asked, answer 0" (hidden) the moment a
refusal was recorded against it, and the row the user had just clicked vanished
from under the cursor. Worst exactly when retail is down, i.e. when the filter is
least able to mean anything.

It also contradicted the doctrine the API layer states outright: `None` means
nobody answered, "which is *not* the same as zero, and callers must not render it
as such". Fixed at [`window.py:602`](../lcsc_suite/ui/explorer/window.py#L602) by
reading the figure rather than its presence — absent and `None` are one case now.
`asked_retail` keeps its meaning and its job, which is stopping
`_start_retail_fill` from asking a refusing host twice.

---

## 7. What the migration closed, so it is not re-derived

From [CODE-REVIEW.md](CODE-REVIEW.md), all **resolved by the Phase 8 cutover**
rather than by a fix — the file each lived in is gone:

| Finding | Where it lived | Why it is gone |
|---|---|---|
| H1 thumbnails die after Refresh | `lcsc/explorer.py` (wx) | replaced by `ui/explorer/`; `forget_fetched` rebinds a dict nothing else holds |
| H2 sorting BOM/POP/Side raises `TypeError` | `mainwindow.py`, `datamodel.py` | `QSortFilterProxyModel` plus `SORT_ROLE` |
| H4 settings I/O has no failure path | `mainwindow.py` | `config.Settings` |
| H5 same endpoint fetched twice by two HTTP stacks | `lcsc_api.py`, `partdetails.py` | both deleted; `requests` survives only in `library.py`'s downloader |
| M1 two of five assignment paths never wrote the footprint | `mainwindow.py` | one funnel — `controller.assign_number`, and its docstring says so |
| M2 unguarded `FindFootprintByReference` | `mainwindow.py` | no pcbnew in this process; the bridge verifies every write |
| M8 bare `except:` and the gerber directory wipe | `fabrication.py` | rewritten as `export.py` + `fab_rules.py` |
| M9 root logger hijack | `mainwindow.py` | `app.configure_logging`, own process |
| M10 `Destroy()` then `EndModal` | `mainwindow.py` | wx-only |
| L4 `wx.CallAfter` from a worker | `partdetails.py` | queued Qt signals |
| L8 `wx.App` assertion escapes | `__init__.py` | no wx |
| L10 dead part-selector modules | four files | deleted |
| E2–E5 datamodel scans, icon reloads | wx UI | new model layer |

M3 (the in-stock filter) and M5–M7 did **not** die that way — M3 was ported and
is B12 above, M5 is in §3, M6 is B5 and M7 is B7.
